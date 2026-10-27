"""Data-quality gate for recorded LeRobot datasets.

Answers three questions before a recording batch is uploaded for training:

1. which episodes are problematic (pauses, jerky motion, hesitation);
2. which joints are problematic (range, overshoot, action/state lag);
3. whether the batch is consistent (start pose, camera features, calibration).

Usage::

    python -m lelab.utils.dataset_quality local/red_bowl_20260919_152702
    python -m lelab.utils.dataset_quality ~/Desktop/lelab/data/lerobot/local/new_run --json
    python -m lelab.utils.dataset_quality --root ~/Desktop/lelab/data/lerobot/local   # list all

Exit code is 1 when at least one ``error`` finding is present, so the command
can gate "upload to train server / start training".
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

DEFAULT_ROOT = Path(
    os.environ.get("LELAB_DATASET_ROOT")
    or os.environ.get("HF_LEROBOT_HOME")
    or Path.home() / "Desktop" / "lelab" / "data" / "lerobot"
)

# A frame counts as "still" when every joint moves less than this between
# consecutive frames. Units follow the dataset (degrees for SO-101).
STILL_DELTA = 0.2
MIN_EPISODE_S = 8.0
MAX_STILL_RATIO = 0.35
MAX_STILL_RUN_S = 2.0
MAX_START_DEVIATION = 15.0
# Action-vs-state gap: a large median means the leader and follower disagree
# (calibration/mapping), a large 95th percentile means the follower lagged
# behind fast commands while recording.
MAX_TRACKING_P50 = 5.0
MAX_TRACKING_P95 = 30.0
MAX_REVERSAL_FRACTION = 0.30
JERK_STEP = 5.0

SO101_JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]


def _joint_names(info: dict[str, Any]) -> list[str]:
    features = info.get("features", {})
    names = features.get("action", {}).get("names")
    if names:
        return [name.replace(".pos", "") for name in names]
    return [f"joint_{index}" for index in range(features.get("action", {}).get("shape", [0])[0])]


def load_dataset(dataset_dir: Path) -> dict[str, Any]:
    info = json.loads((dataset_dir / "meta" / "info.json").read_text(encoding="utf-8"))
    stats_path = dataset_dir / "meta" / "stats.json"
    stats = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.is_file() else {}
    files = sorted(glob.glob(str(dataset_dir / "data" / "**" / "*.parquet"), recursive=True))
    if not files:
        raise FileNotFoundError(f"no parquet files under {dataset_dir}/data")
    columns = ["action", "episode_index", "frame_index"]
    parts = []
    for path in files:
        try:
            parts.append(pd.read_parquet(path, columns=[*columns, "observation.state"]))
        except KeyError:
            parts.append(pd.read_parquet(path, columns=columns))
    frame = pd.concat(parts, ignore_index=True)
    return {"dir": dataset_dir, "info": info, "stats": stats, "frames": frame}


def _longest_still_run(still: np.ndarray) -> int:
    best = current = 0
    for flag in still:
        current = current + 1 if flag else 0
        best = max(best, current)
    return best


def analyze(
    dataset_dir: Path, *, calibration: Path | None = None, margin: float = -4.0
) -> dict[str, Any]:
    data = load_dataset(dataset_dir)
    info, frames = data["info"], data["frames"]
    joints = _joint_names(info)
    fps = float(info.get("fps") or 30.0)
    episodes: list[dict[str, Any]] = []
    starts: list[np.ndarray] = []
    actions_by_episode: dict[int, np.ndarray] = {}

    for episode_index, group in frames.groupby("episode_index"):
        group = group.sort_values("frame_index")
        actions = np.stack(group["action"].to_numpy()).astype(float)
        deltas = np.abs(np.diff(actions, axis=0)) if len(actions) > 1 else np.zeros((0, len(joints)))
        still = deltas.max(axis=1) < STILL_DELTA if len(deltas) else np.zeros(0, dtype=bool)
        signed = np.sign(np.diff(actions, axis=0)) if len(actions) > 1 else np.zeros((0, len(joints)))
        signed[signed == 0] = 1.0
        reversals = (np.diff(signed, axis=0) != 0).mean(axis=0) if len(signed) > 1 else np.zeros(len(joints))
        record: dict[str, Any] = {
            "episode": int(episode_index),
            "frames": len(actions),
            "duration_s": round(len(actions) / fps, 1),
            "still_ratio": round(float(still.mean()), 3) if len(still) else 0.0,
            "longest_still_s": round(_longest_still_run(still) / fps, 2),
            "max_step": {joint: round(float(deltas[:, i].max()), 2) for i, joint in enumerate(joints)}
            if len(deltas)
            else dict.fromkeys(joints, 0.0),
            "jerky_steps": {
                joint: int((deltas[:, i] > JERK_STEP).sum()) for i, joint in enumerate(joints)
            }
            if len(deltas)
            else dict.fromkeys(joints, 0),
            "reversal": {joint: round(float(reversals[i]), 3) for i, joint in enumerate(joints)},
        }
        starts.append(actions[0])
        actions_by_episode[int(episode_index)] = actions
        episodes.append(record)

    start_matrix = np.stack(starts)
    median_start = np.median(start_matrix, axis=0)
    for record, start in zip(episodes, start_matrix, strict=True):
        deviation = np.abs(start - median_start)
        record["start_deviation"] = {
            joint: round(float(deviation[i]), 1) for i, joint in enumerate(joints)
        }
        record["start_deviation_max"] = round(float(deviation.max()), 1)

    tracking = None
    if "observation.state" in frames.columns:
        tracking = {}
        for episode_index, group in frames.groupby("episode_index"):
            actions = np.stack(group["action"].to_numpy()).astype(float)
            observed = np.stack(group["observation.state"].to_numpy()).astype(float)
            gap = np.abs(actions - observed)
            tracking[int(episode_index)] = {
                joint: {
                    "p50": round(float(np.percentile(gap[:, i], 50)), 2),
                    "p95": round(float(np.percentile(gap[:, i], 95)), 2),
                }
                for i, joint in enumerate(joints)
            }

    findings = _findings(
        episodes,
        joints,
        info,
        tracking,
        start_matrix,
        calibration,
        margin,
        actions_by_episode,
    )
    severities = [finding["severity"] for finding in findings]
    return {
        "dataset": str(dataset_dir),
        "episodes": episodes,
        "joints": joints,
        "fps": fps,
        "total_frames": int(info.get("total_frames") or len(frames)),
        "total_episodes": int(info.get("total_episodes") or len(episodes)),
        "tracking_p95": tracking,
        "findings": findings,
        "verdict": "error" if "error" in severities else "warning" if "warning" in severities else "ok",
    }


def _calibrated_limits(calibration: Path, joints: list[str]) -> dict[str, tuple[float, float]] | None:
    """Command-space limits from a calibration file (SO-101 layout)."""
    if joints != SO101_JOINTS:
        return None
    try:
        from lerobot.motors import Motor, MotorCalibration, MotorNormMode
        from lerobot.motors.feetech import FeetechMotorsBus
    except Exception:  # pragma: no cover - lerobot not available
        return None
    raw = json.loads(calibration.read_text(encoding="utf-8"))
    if set(raw) != set(SO101_JOINTS):
        return None
    parsed = {name: MotorCalibration(**values) for name, values in raw.items()}
    body = MotorNormMode.DEGREES
    motors = {name: Motor(index + 1, "sts3215", body) for index, name in enumerate(SO101_JOINTS[:5])}
    motors["gripper"] = Motor(6, "sts3215", MotorNormMode.RANGE_0_100)
    bus = FeetechMotorsBus(port="/dev/null", motors=motors, calibration=parsed)
    limits: dict[str, tuple[float, float]] = {}
    for name in SO101_JOINTS:
        motor_id = motors[name].id
        low = bus._normalize({motor_id: parsed[name].range_min})[motor_id]
        high = bus._normalize({motor_id: parsed[name].range_max})[motor_id]
        limits[name] = (min(low, high), max(low, high))
    return limits


def _findings(
    episodes: list[dict[str, Any]],
    joints: list[str],
    info: dict[str, Any],
    tracking: dict[int, dict[str, float]] | None,
    start_matrix: np.ndarray,
    calibration: Path | None,
    margin: float,
    actions_by_episode: dict[int, np.ndarray],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    def add(severity: str, code: str, message: str, **extra: Any) -> None:
        findings.append({"severity": severity, "code": code, "message": message, **extra})

    for record in episodes:
        episode = record["episode"]
        if record["duration_s"] < MIN_EPISODE_S:
            add(
                "warning",
                "episode_too_short",
                f"Episode {episode} is only {record['duration_s']}s long.",
                episode=episode,
            )
        if record["still_ratio"] > MAX_STILL_RATIO:
            add(
                "warning",
                "episode_mostly_still",
                f"Episode {episode} is still for {record['still_ratio'] * 100:.0f}% of its frames.",
                episode=episode,
            )
        if record["longest_still_s"] > MAX_STILL_RUN_S:
            add(
                "warning",
                "episode_long_pause",
                f"Episode {episode} holds still for {record['longest_still_s']}s in a row.",
                episode=episode,
            )
        jerky = {joint: count for joint, count in record["jerky_steps"].items() if count}
        if jerky:
            add(
                "warning",
                "episode_jerky_steps",
                f"Episode {episode} commands >{JERK_STEP} units in one frame on {jerky}.",
                episode=episode,
                joints=jerky,
            )
        shaky = {
            joint: value for joint, value in record["reversal"].items()
            if value > MAX_REVERSAL_FRACTION
        }
        if shaky:
            add(
                "warning",
                "episode_direction_oscillation",
                f"Episode {episode} reverses direction on {shaky} of its steps.",
                episode=episode,
                joints=shaky,
            )
        if record["start_deviation_max"] > MAX_START_DEVIATION:
            worst = sorted(record["start_deviation"].items(), key=lambda item: -item[1])[:2]
            add(
                "warning",
                "episode_start_pose_off",
                f"Episode {episode} starts {record['start_deviation_max']} units away from the "
                f"dataset median pose (worst joints: {worst}).",
                episode=episode,
            )
        if tracking is not None:
            gaps = tracking.get(episode, {})
            lagging = {
                joint: values["p95"] for joint, values in gaps.items()
                if values["p95"] > MAX_TRACKING_P95
            }
            offset = {
                joint: values["p50"] for joint, values in gaps.items()
                if values["p50"] > MAX_TRACKING_P50
            }
            if lagging:
                add(
                    "warning",
                    "episode_action_state_lag",
                    f"Episode {episode} commands run far ahead of the measured state on {lagging} "
                    "(95th percentile gap, units): the follower lagged behind fast motions.",
                    episode=episode,
                    joints=lagging,
                )
            if offset:
                add(
                    "warning",
                    "episode_action_state_offset",
                    f"Episode {episode} keeps a constant gap between command and state on {offset} "
                    "(median gap, units): check the leader/follower calibration or mapping.",
                    episode=episode,
                    joints=offset,
                )

    short = [record["episode"] for record in episodes if record["duration_s"] < MIN_EPISODE_S]
    if len(episodes) < 20:
        add(
            "warning",
            "batch_small",
            f"Only {len(episodes)} episodes recorded; ACT tasks usually need 40+ "
            "(30-60 minutes of demonstrations).",
        )
    if short and len(short) == len(episodes):
        add("error", "all_episodes_too_short", "Every episode is shorter than the 8s minimum.")

    for i, joint in enumerate(joints):
        spread = float(np.max(start_matrix[:, i]) - np.min(start_matrix[:, i]))
        if spread > 2 * MAX_START_DEVIATION:
            add(
                "warning",
                "batch_start_pose_spread",
                f"Start pose of '{joint}' varies by {spread:.1f} units across episodes.",
                joint=joint,
            )

    features = info.get("features", {})
    for key, feature in features.items():
        if key.startswith("observation.images."):
            shape = feature.get("shape")
            if shape and shape[0] not in (480, 720) and len(shape) == 3:
                add(
                    "info",
                    "camera_resolution",
                    f"{key} is recorded at {shape}; make sure the checkpoint expects the same.",
                )

    if calibration is not None:
        limits = _calibrated_limits(calibration, joints)
        if limits is None:
            add("info", "calibration_skipped", f"Could not derive limits from {calibration}.")
        else:
            for i, joint in enumerate(joints):
                low, high = limits[joint]
                effective_low, effective_high = low + margin, high - margin
                worst = 0.0
                offenders: list[int] = []
                for episode, actions in actions_by_episode.items():
                    column = actions[:, i]
                    over = max(
                        float(effective_low - column.min()),
                        float(column.max() - effective_high),
                        0.0,
                    )
                    if over > 0:
                        offenders.append(episode)
                        worst = max(worst, over)
                if offenders:
                    add(
                        "warning",
                        "batch_outside_calibration",
                        f"{len(offenders)} episode(s) command '{joint}' outside the calibrated "
                        f"range (margin {margin:+g}, worst overshoot {worst:.1f} units; "
                        f"episodes {offenders[:5]}). Recording beyond the calibration makes the "
                        "runtime guard clip those actions.",
                        joint=joint,
                    )
    return findings


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"dataset     : {report['dataset']}",
        f"episodes    : {report['total_episodes']}  frames: {report['total_frames']}  "
        f"fps: {report['fps']}",
        f"verdict     : {report['verdict'].upper()}",
    ]
    episodes = report["episodes"]
    if episodes:
        lines.append("")
        lines.append(
            f"{'ep':>3} {'frames':>6} {'sec':>5} {'still%':>7} {'maxPause':>8} "
            f"{'startDev':>8} {'jerky':>6} {'revMax':>7}"
        )
        for record in sorted(episodes, key=lambda item: -item["still_ratio"]):
            jerky = sum(record["jerky_steps"].values())
            rev = max(record["reversal"].values()) if record["reversal"] else 0.0
            lines.append(
                f"{record['episode']:>3} {record['frames']:>6} {record['duration_s']:>5.1f} "
                f"{record['still_ratio'] * 100:>6.1f}% {record['longest_still_s']:>7.2f}s "
                f"{record['start_deviation_max']:>8.1f} {jerky:>6} {rev:>7.3f}"
            )
    findings = report.get("findings") or []
    if findings:
        lines.append("")
        lines.append("== findings ==")
        order = {"error": 0, "warning": 1, "info": 2}
        for finding in sorted(findings, key=lambda item: order.get(item["severity"], 3)):
            lines.append(f"[{finding['severity']:<7}] {finding['code']}: {finding['message']}")
    return "\n".join(lines)


def _resolve(argv_dataset: str | None, root: Path) -> Path | None:
    if not argv_dataset:
        return None
    candidate = Path(argv_dataset).expanduser()
    if candidate.exists():
        return candidate
    return root / argv_dataset


def _iter_datasets(root: Path):
    """Yield ``(repo_id, directory)`` for datasets one or two levels below root."""
    seen = set()
    for pattern in ("*/meta/info.json", "*/*/meta/info.json"):
        for info_path in sorted(root.glob(pattern)):
            dataset_dir = info_path.parent.parent
            if dataset_dir in seen:
                continue
            seen.add(dataset_dir)
            yield dataset_dir.relative_to(root).as_posix(), dataset_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LeLab dataset quality report")
    parser.add_argument("dataset", nargs="?", help="dataset directory or repo id under --root")
    parser.add_argument(
        "--root", default=str(DEFAULT_ROOT), help="directory holding local datasets"
    )
    parser.add_argument("--calibration", help="robot calibration JSON (range check)")
    parser.add_argument(
        "--calibration-margin",
        type=float,
        default=-4.0,
        help="same value as the runtime guard: positive shrinks, negative expands",
    )
    parser.add_argument("--json", action="store_true", help="print the raw report as JSON")
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser()

    dataset = _resolve(args.dataset, root)
    if dataset is None:
        if not root.is_dir():
            print(f"dataset root not found: {root}", file=sys.stderr)
            return 2
        for repo_id, entry in _iter_datasets(root):
            info = json.loads((entry / "meta" / "info.json").read_text(encoding="utf-8"))
            print(
                f"{repo_id:<48} episodes={info.get('total_episodes')} "
                f"frames={info.get('total_frames')}"
            )
        return 0
    if not (dataset / "meta" / "info.json").is_file():
        print(f"not a LeRobot dataset: {dataset}", file=sys.stderr)
        return 2

    calibration = Path(args.calibration).expanduser() if args.calibration else None
    report = analyze(dataset, calibration=calibration, margin=args.calibration_margin)
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print(render_text(report))
    return 1 if report["verdict"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
