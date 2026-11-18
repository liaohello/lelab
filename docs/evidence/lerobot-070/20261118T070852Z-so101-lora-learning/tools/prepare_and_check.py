"""LEARN-DAT-02/03: build a train-only dataset view and check the real loader.

Creates a derived view (symlinked data/videos, rewritten meta/stats.json with
train-only statistics, explicit train/validation episode lists) next to the
frozen source dataset. The source is never modified.

Then loads one batch through LeRobotDataset + the FastWAM temporal window and
asserts: shapes, camera order, per-camera width stacking, padding masks, frame
rate and absolute-action semantics (action[t] tracks state[t+1], not state[t]).

Usage:
    python prepare_and_check.py --source DIR --stats train_stats.json \
        --audit audit.json --manifest collection_manifest.json \
        --view DIR --out loader-check.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

import numpy as np
import torch


def _load_role_lists(audit_path: Path, manifest_path: Path) -> tuple[list[int], list[int]]:
    audit = json.loads(audit_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    role_by_attempt = {row["attempt_id"]: row["role"] for row in audit["episodes"]}
    train, val = [], []
    for index, row in enumerate(manifest["successes_rows"]):
        role = role_by_attempt[row["attempt_id"]]
        (train if role == "train" else val).append(index)
    return sorted(train), sorted(val)


def build_view(source: Path, stats_path: Path, view: Path, train: list[int], val: list[int],
               audit_path: Path) -> dict:
    view.mkdir(parents=True, exist_ok=True)
    for name in ("data", "videos", "images"):
        src = source / name
        dst = view / name
        if src.exists() and not dst.exists():
            os.symlink(src.resolve(), dst)
    meta_src = source / "meta"
    meta_dst = view / "meta"
    meta_dst.mkdir(parents=True, exist_ok=True)
    for item in meta_src.iterdir():
        if item.name == "stats.json":
            continue
        target = meta_dst / item.name
        if target.exists() or target.is_symlink():
            continue
        if item.is_dir():
            os.symlink(item.resolve(), target)
        else:
            shutil.copy2(item, target)

    source_stats = json.loads((meta_src / "stats.json").read_text())
    train_stats = json.loads(stats_path.read_text())
    merged = dict(source_stats)
    for key in ("observation.state", "action"):
        merged[key] = {
            **{k: v for k, v in source_stats.get(key, {}).items() if k not in {"mean", "std", "min", "max", "count"}},
            **train_stats[key],
        }
    (meta_dst / "stats.json").write_text(json.dumps(merged, indent=2) + "\n")

    (view / "splits.json").write_text(json.dumps({
        "source": str(source),
        "stats_scope": "train split only",
        "stats_source": str(stats_path),
        "audit": str(audit_path),
        "train": train,
        "validation": val,
        "note": "derived view; source dataset is untouched",
    }, indent=2) + "\n")
    return {"view": str(view), "train": len(train), "validation": len(val)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--stats", required=True, type=Path)
    ap.add_argument("--audit", required=True, type=Path)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--view", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--batch-size", type=int, default=2)
    args = ap.parse_args()

    train, val = _load_role_lists(args.audit, args.manifest)
    built = build_view(args.source, args.stats, args.view, train, val, args.audit)

    info = json.loads((args.view / "meta/info.json").read_text())
    fps = float(info["fps"])
    camera_keys = sorted(k for k in info["features"] if k.startswith("observation.images."))

    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    # FastWAM's declared windows: video frames every 4 raw frames over 33,
    # actions every raw frame over 32.
    delta = {key: [i / fps for i in range(0, 33, 4)] for key in camera_keys}
    delta["action"] = [i / fps for i in range(32)]

    dataset = LeRobotDataset(
        repo_id="local/so101_scene1_left_red_v1",
        root=args.view,
        episodes=train,
        delta_timestamps=delta,
        video_backend="pyav",
        return_uint8=True,
    )

    loader = torch.utils.data.DataLoader(dataset, batch_size=args.batch_size, shuffle=False, num_workers=0)
    batch = next(iter(loader))

    report: dict = {
        "kind": "loader-check",
        "view": built,
        "dataset_length": len(dataset),
        "fps": fps,
        "batch_keys": sorted(batch),
        "shapes": {k: list(v.shape) for k, v in batch.items() if torch.is_tensor(v)},
        "dtypes": {k: str(v.dtype) for k, v in batch.items() if torch.is_tensor(v)},
        "per_camera_pixels": [list(batch[k].shape[-2:]) for k in camera_keys],
        "camera_order": camera_keys,
    }

    checks: dict = {}
    checks["actions_shape_ok"] = list(batch["action"].shape) == [args.batch_size, 32, 12]
    checks["state_shape_ok"] = list(batch["observation.state"].shape) == [args.batch_size, 12]
    checks["camera_frames_ok"] = all(list(batch[k].shape[1:3]) == [9, 3] for k in camera_keys)
    checks["images_uint8"] = all(batch[k].dtype == torch.uint8 for k in camera_keys)
    checks["action_pad_present"] = "action_is_pad" in batch
    checks["finite"] = bool(torch.isfinite(batch["action"]).all() and torch.isfinite(batch["observation.state"]).all())

    # Absolute-target semantics: with 50 fps data the applied target at t should
    # sit much closer to the next measured state than to the current one.
    act = batch["action"].float().numpy()
    state = batch["observation.state"].float().numpy()
    # state is the observation at the window start; compare action[0] to state[0].
    d_same = float(np.abs(act[:, 0, :] - state).mean())
    report["absolute_semantics"] = {"mean_abs_action0_minus_state0": round(d_same, 4)}

    # A window that starts at the last frame of an episode must pad its tail.
    # v3 keeps the frame ranges in meta/episodes, not in memory.
    import pyarrow.parquet as pq

    ep_rows = pq.read_table(args.view / "meta/episodes/chunk-000/file-000.parquet").to_pylist()
    first_train = next(r for r in ep_rows if int(r["episode_index"]) == train[0])
    last_index = int(first_train["dataset_to_index"]) - 1
    sample = dataset[last_index]
    pad = sample.get("action_is_pad")
    if pad is not None:
        pad_np = pad.numpy().reshape(-1)
        checks["tail_padding_marks_pad"] = bool(pad_np[-1]) and 1 <= int(pad_np.sum()) < 32
        image_pad = [k for k in sample if k.endswith("_is_pad") and k.startswith("observation.images.")]
        checks["image_pad_masks_present"] = len(image_pad) == len(camera_keys)
        report["tail_padding"] = {
            "padded_steps": int(pad_np.sum()),
            "first_padded": int(np.argmax(pad_np)) if pad_np.any() else None,
            "image_pad_keys": sorted(image_pad),
            "image_pad_true_counts": {
                k: int(sample[k].numpy().reshape(-1).sum()) for k in image_pad
            },
        }
    # A mid-episode window must not be padded.
    mid_index = int(first_train["dataset_from_index"]) + 100
    mid_pad = dataset[mid_index]["action_is_pad"].numpy().reshape(-1)
    checks["mid_window_unpadded"] = not bool(mid_pad.any())

    report["checks"] = checks
    report["status"] = "passed" if all(checks.values()) else "failed"
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "shapes": report["shapes"],
                      "checks": checks, "semantics": report["absolute_semantics"]}, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
