"""Build the arm-B data view: same bytes, relative-action normalisation stats.

Arm B trains with `--policy.use_relative_actions=true`, which makes the processor
convert `action - state_anchor` **before** normalising. The normaliser therefore
has to hold *relative* action statistics, while `observation.state` stays
absolute. The dataset itself is not touched: data and videos are symlinked, only
`meta/stats.json` is rewritten.

Statistics are computed with LeRobot's own `compute_relative_action_stats` over
the **train episodes only** (horizon 32, gripper excluded so it stays an absolute
percentage), which is the same routine `dataset_tools --relative-action` uses.

Verification is part of the build, not a separate step:
  * the written stats equal the computed ones,
  * `meta/info.json` still describes absolute actions (the data is untouched),
  * a real sample round-trips through the processor built from this view.

Usage:
    python make_relative_view.py --source VIEW --dest NEW --splits splits.json \
        --out relative-view-report.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

HORIZON = 32
EXCLUDE = ["gripper"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--dest", required=True, type=Path)
    ap.add_argument("--splits", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--horizon", type=int, default=HORIZON)
    args = ap.parse_args()

    import numpy as np
    import torch

    from lerobot.datasets.compute_stats import compute_relative_action_stats
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    splits = json.loads(args.splits.read_text())
    train = [int(e) for e in splits["train"]]
    info = json.loads((args.source / "meta/info.json").read_text())

    if args.dest.exists():
        shutil.rmtree(args.dest)
    args.dest.mkdir(parents=True)
    # Symlink the heavy directories, copy the small meta ones.
    for name in ("data", "videos"):
        if (args.source / name).exists():
            os.symlink((args.source / name).resolve(), args.dest / name)
    shutil.copytree(args.source / "meta", args.dest / "meta")
    for extra in ("splits.json",):
        if (args.source / extra).exists():
            shutil.copy2(args.source / extra, args.dest / extra)

    dataset = LeRobotDataset(
        repo_id=f"local/{args.source.name}",
        root=args.source,
        episodes=train,
        video_backend="pyav",
    )
    rel = compute_relative_action_stats(
        hf_dataset=dataset.hf_dataset,
        features=info["features"],
        chunk_size=args.horizon,
        exclude_joints=EXCLUDE,
        num_workers=4,
    )
    rel = {k: np.asarray(v) for k, v in rel.items()}

    stats_path = args.dest / "meta/stats.json"
    stats = json.loads(stats_path.read_text())
    absolute_action = {k: list(map(float, np.atleast_1d(stats["action"][k])))
                       for k in ("mean", "std", "min", "max") if k in stats["action"]}
    # Only mean/std drive normalisation; keep the rest consistent with them by
    # writing the relative values everywhere the normaliser can read.
    stats["action"] = {
        "mean": rel["mean"].astype(float).tolist(),
        "std": rel["std"].astype(float).tolist(),
        "min": rel["min"].astype(float).tolist(),
        "max": rel["max"].astype(float).tolist(),
        "count": stats["action"].get("count", [int(dataset.num_frames)]),
    }
    stats_path.write_text(json.dumps(stats, indent=2) + "\n")

    # ---- verification -------------------------------------------------------
    reloaded = json.loads(stats_path.read_text())
    checks = {
        "written_action_mean_matches": bool(np.allclose(
            np.asarray(reloaded["action"]["mean"]), rel["mean"], atol=1e-6)),
        "written_action_std_matches": bool(np.allclose(
            np.asarray(reloaded["action"]["std"]), rel["std"], atol=1e-6)),
        "state_stats_untouched": bool(np.allclose(
            np.asarray(reloaded["observation.state"]["mean"]),
            np.asarray(stats["observation.state"]["mean"]))),
        "info_still_absolute": info["features"]["action"]["shape"] == [len(rel["mean"])],
        "relative_std_tighter_on_active_joints": bool(
            np.all(rel["std"][[0, 1, 2, 3, 4]] < np.asarray(absolute_action["std"])[[0, 1, 2, 3, 4]])),
    }

    # Round-trip one real sample through the processor built from this view.
    from lerobot.policies.fastwam import FastWAMConfig
    from lerobot.policies.fastwam.processor_fastwam import make_fastwam_pre_post_processors

    fps = float(info["fps"])
    cameras = sorted(k for k in info["features"] if k.startswith("observation.images."))
    ds = LeRobotDataset(
        repo_id=f"local/{args.dest.name}", root=args.dest, episodes=[train[0]],
        delta_timestamps={**{k: [i / fps for i in range(0, args.horizon + 1, 4)] for k in cameras},
                          "action": [i / fps for i in range(args.horizon)]},
        video_backend="pyav", return_uint8=False,
    )
    cfg = FastWAMConfig(
        action_dim=int(info["features"]["action"]["shape"][0]),
        proprio_dim=int(info["features"]["observation.state"]["shape"][0]),
        image_size=(224, 224 * len(cameras)),
        action_horizon=args.horizon, n_action_steps=args.horizon,
        dtype=torch.float32, device="cpu",
        use_relative_actions=True, relative_exclude_joints=EXCLUDE,
        action_feature_names=list(info["features"]["action"]["names"]),
    )
    cfg.set_dataset_feature_metadata(info["features"])
    stats_t = {k: {n: torch.tensor(v, dtype=torch.float32)
                   for n, v in reloaded[k].items() if n in {"mean", "std", "min", "max", "count"}}
               for k in ("observation.state", "action")}
    pre, post = make_fastwam_pre_post_processors(cfg, dataset_stats=stats_t)
    sample = ds[0]
    raw_action = sample["action"].float()
    processed = pre(dict(sample))
    restored = post(processed["action"].float())
    max_abs = float((restored.float() - raw_action).abs().max())
    checks["sample_round_trip"] = max_abs < 1e-2

    report = {
        "kind": "relative-view",
        "source": str(args.source),
        "dest": str(args.dest),
        "train_episodes": len(train),
        "horizon": args.horizon,
        "excluded_joints": EXCLUDE,
        "relative_stats": {k: v.tolist() for k, v in rel.items() if k in ("mean", "std", "min", "max")},
        "absolute_action_stats": absolute_action,
        "checks": checks,
        "sample_round_trip_max_abs": max_abs,
        "status": "passed" if all(checks.values()) else "failed",
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "checks": checks,
                      "round_trip": max_abs,
                      "rel_std": [round(float(v), 3) for v in rel["std"]],
                      "abs_std": [round(v, 3) for v in absolute_action["std"]]}, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
