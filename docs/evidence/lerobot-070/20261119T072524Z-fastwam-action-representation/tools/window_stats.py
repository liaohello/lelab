"""Per-window displacement statistics used to label evaluation windows.

A window is the 32-step action chunk that starts at an evaluation anchor. Its
"dynamism" is measured from the *ground truth* only, as

    displacement(dim) = max_t |gt[t, dim] - gt[0, dim]|

so the label describes what the demonstration does, not what a model predicts.
Thresholds are frozen from the **train** split before any dev window is labelled
(PLAN §"实验与验收口径"), and the dev split is only labelled with them.

Outputs a JSON with the percentile table for both splits plus the frozen
thresholds, so the choice is auditable rather than post-hoc.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

LEFT_JOINTS = [0, 1, 2, 3, 4]
LEFT_GRIPPER = 5
HORIZON = 32
ANCHOR_FRACTIONS = (0.0, 0.25, 0.5, 0.75)


def load_split(root: Path) -> tuple[dict[int, np.ndarray], dict[int, int]]:
    """episode -> [T, 12] action array (train/dev membership comes from splits.json)."""
    table = pq.read_table(root / "data/chunk-000/file-000.parquet",
                          columns=["action", "episode_index"])
    actions = np.asarray(table["action"].to_pylist(), dtype=np.float64)
    episodes = np.asarray(table["episode_index"].to_pylist())
    out: dict[int, np.ndarray] = {}
    for ep in np.unique(episodes):
        out[int(ep)] = actions[episodes == ep]
    return out


def windows(series: dict[int, np.ndarray], episodes: list[int]) -> list[dict]:
    rows = []
    for ep in episodes:
        arr = series[ep]
        length = arr.shape[0]
        for frac in ANCHOR_FRACTIONS:
            anchor = min(int(length * frac), length - 1)
            chunk = arr[anchor:anchor + HORIZON]
            if chunk.shape[0] == 0:
                continue
            # A window that runs past the episode end is only as long as the data;
            # record how much of it is real so short windows can be excluded.
            joint_disp = float(np.max(np.abs(chunk[:, LEFT_JOINTS] - chunk[0, LEFT_JOINTS])))
            grip_disp = float(np.max(np.abs(chunk[:, LEFT_GRIPPER] - chunk[0, LEFT_GRIPPER])))
            rows.append({
                "episode": int(ep),
                "anchor": int(anchor),
                "steps": int(chunk.shape[0]),
                "left_joint_displacement": joint_disp,
                "left_gripper_displacement": grip_disp,
            })
    return rows


def summarise(values: list[float]) -> dict:
    arr = np.asarray(values, dtype=np.float64)
    return {
        "n": int(arr.size),
        "min": float(arr.min()),
        "p25": float(np.percentile(arr, 25)),
        "median": float(np.percentile(arr, 50)),
        "p75": float(np.percentile(arr, 75)),
        "p90": float(np.percentile(arr, 90)),
        "max": float(arr.max()),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--splits", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--joint-threshold", type=float, default=None,
                    help="freeze the joint-displacement threshold instead of deriving it")
    ap.add_argument("--gripper-threshold", type=float, default=None)
    args = ap.parse_args()

    splits = json.loads(args.splits.read_text())
    series = load_split(args.root)
    # The derived view names the held-out split `validation`; accept `dev` too.
    dev_key = "validation" if "validation" in splits else "dev"
    train_rows = windows(series, [int(e) for e in splits["train"]])
    dev_rows = windows(series, [int(e) for e in splits[dev_key] if int(e) in series])

    train_joint = summarise([r["left_joint_displacement"] for r in train_rows])
    train_grip = summarise([r["left_gripper_displacement"] for r in train_rows])

    # Freeze from train percentiles: the median splits "the arm mostly stays put"
    # from "the arm actually moves" without tuning on dev.
    joint_thr = args.joint_threshold if args.joint_threshold is not None else round(train_joint["median"], 2)
    grip_thr = args.gripper_threshold if args.gripper_threshold is not None else round(
        max(train_grip["median"], 1.0), 2)

    def tag(rows: list[dict]) -> dict:
        joint_dyn = [r for r in rows if r["left_joint_displacement"] >= joint_thr]
        grip_dyn = [r for r in rows if r["left_gripper_displacement"] >= grip_thr]
        both = [r for r in joint_dyn if r["left_gripper_displacement"] >= grip_thr]
        return {
            "windows": len(rows),
            "joint_dynamic": len(joint_dyn),
            "gripper_dynamic": len(grip_dyn),
            "joint_and_gripper_dynamic": len(both),
            "joint_static": len(rows) - len(joint_dyn),
        }

    report = {
        "kind": "window-dynamism-stats",
        "root": str(args.root),
        "horizon": HORIZON,
        "anchor_fractions": list(ANCHOR_FRACTIONS),
        "left_joint_dims": LEFT_JOINTS,
        "left_gripper_dim": LEFT_GRIPPER,
        "thresholds": {
            "left_joint_displacement_deg": joint_thr,
            "left_gripper_displacement_pct": grip_thr,
            "source": ("explicit" if args.joint_threshold is not None or args.gripper_threshold is not None
                       else "train median (frozen before dev was labelled)"),
        },
        "train": {
            "joint_displacement_deg": train_joint,
            "gripper_displacement_pct": train_grip,
            "tags": tag(train_rows),
        },
        "dev": {
            "tags": tag(dev_rows),
            "per_window": dev_rows,
        },
        "train_per_window": train_rows,
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({
        "thresholds": report["thresholds"],
        "train_joint": train_joint, "train_gripper": train_grip,
        "train_tags": report["train"]["tags"], "dev_tags": report["dev"]["tags"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
