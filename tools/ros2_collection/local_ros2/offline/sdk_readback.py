#!/usr/bin/env python3
"""OFFLINE ONLY: read the exported dataset back through the local LeRobot SDK.

The page, the stored rows and the SDK have to agree axis by axis; a number that only
appears in one of them is not evidence. This script opens the dataset with the project's
own editable LeRobot checkout (no PyPI substitute) and compares its values against the
viewer read-back report written by ``verify_viewer.py``.

The dataset stays ``simulated_offline`` and not training-eligible; nothing here is read
as hardware measurement or as a training result.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True, help="HF_LEROBOT_HOME of the offline export")
    parser.add_argument("--repo", required=True, help="repo id, e.g. lelab/ros-mobile-...")
    parser.add_argument("--viewer-report", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    # Set before importing LeRobot: the SDK resolves its dataset root at import time.
    os.environ["HF_LEROBOT_HOME"] = str(pathlib.Path(args.home).resolve())
    sys.path.insert(0, str(ROOT / "app_ros_mobile"))
    from lerobot.datasets.lerobot_dataset import LeRobotDataset  # noqa: E402

    dataset = LeRobotDataset(args.repo)
    features = dataset.meta.features
    state = features["observation.state"]
    action = features["action"]
    report: dict = {
        "repo_id": args.repo,
        "root": str(dataset.root),
        "frames": len(dataset),
        "fps": dataset.fps,
        "episodes": int(dataset.meta.total_episodes),
        "state_dim": int(state["shape"][0]),
        "action_dim": int(action["shape"][0]),
        "state_names": state["names"],
        "state_units": (state.get("info") or {}).get("units"),
        "state_source": (state.get("info") or {}).get("source"),
        "action_names": action["names"],
    }
    sidecar = pathlib.Path(dataset.root) / "meta" / "lelab_export_identity.json"
    report["identity"] = json.loads(sidecar.read_text()) if sidecar.exists() else None

    by_episode: dict[int, dict] = {}
    for index in range(len(dataset)):
        row = dataset[index]
        episode = int(row["episode_index"])
        values = {
            "frame_index": int(row["frame_index"]),
            "state": [round(float(v), 7) for v in row["observation.state"]],
            "action": [round(float(v), 7) for v in row["action"]],
        }
        slot = by_episode.setdefault(episode, {"first": values, "last": values, "count": 0})
        slot["last"] = values
        slot["count"] += 1
    report["sdk_episodes"] = {str(k): v for k, v in sorted(by_episode.items())}

    viewer = json.loads(pathlib.Path(args.viewer_report).read_text())
    checks = []
    worst = 0.0
    for entry in viewer.get("episodes") or []:
        index = int(entry["episode_index"])
        sdk = by_episode.get(index)
        checks.append({"episode_index": index,
                       "page_frames": entry.get("page_points"),
                       "sdk_frames": None if sdk is None else sdk["count"],
                       "frames_match": sdk is not None and sdk["count"] == entry.get("page_points")})
        if sdk is None:
            continue
        for key in ("first_row", "last_row"):
            page_row, sdk_row = entry.get(key), sdk["first"] if key == "first_row" else sdk["last"]
            for field in ("state", "action"):
                for axis, (left, right) in enumerate(zip(page_row[field], sdk_row[field])):
                    delta = abs(float(left) - float(right))
                    worst = max(worst, delta)

    report["comparison"] = {"per_episode": checks, "max_abs_axis_delta": worst,
                            "tolerance": 1e-6}
    report["passed"] = (bool(checks) and all(c["frames_match"] for c in checks)
                        and worst <= 1e-6
                        and report["state_dim"] == 6 and report["action_dim"] == 6
                        and (report["identity"] or {}).get("data_identity") == "simulated_offline"
                        and (report["identity"] or {}).get("training_eligible") is False)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "frames": report["frames"],
                      "episodes": report["episodes"], "state_dim": report["state_dim"],
                      "action_dim": report["action_dim"],
                      "max_abs_axis_delta": worst}, default=str), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
