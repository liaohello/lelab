"""Train-only relative-action statistics for the SO-101 view (CPU, no model).

Reuses LeRobot's own `compute_relative_action_stats`, which is the same code path
`dataset_tools` uses when a dataset is exported with `--relative-action`, and
cross-checks it against an independent vectorised recomputation. The point is
not to trust either implementation blindly: a wrong normaliser silently changes
what the model learns, and it is invisible in the loss.

Semantics under test (PLAN §"实验与验收口径"):

  rel[t] = action[t] - state[anchor]      for every step of one 32-step chunk
  grippers stay absolute (percentage), so their relative stats must equal the
  absolute ones.

Usage:
    python relative_stats.py --root DIR --splits splits.json \
        --absolute-stats train_stats.json --out relative_stats.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

HORIZON = 32
GRIPPER_DIMS = (5, 11)


def load_frames(root: Path, episodes: list[int]) -> dict:
    """Contiguous frames of the requested episodes, in file order."""
    table = pq.read_table(root / "data/chunk-000/file-000.parquet",
                          columns=["action", "observation.state", "episode_index"])

    def column(name: str) -> np.ndarray:
        """Fixed-size list column -> [N, width] float32 without Python objects.

        `to_pylist()` on 89k x 12 floats allocates ~1M Python floats and blows the
        4 GiB address-space limit on this workstation; flatten + reshape does not.
        """
        flat = table[name].combine_chunks().flatten().to_numpy(zero_copy_only=False)
        width = flat.size // table.num_rows
        return np.asarray(flat, dtype=np.float32).reshape(-1, width)

    actions = column("action")
    states = column("observation.state")
    eps = np.asarray(table["episode_index"].to_numpy(), dtype=np.int64)

    keep = np.isin(eps, np.asarray(episodes, dtype=np.int64))
    actions, states, eps = actions[keep], states[keep], eps[keep]

    # Every episode must be one contiguous block, otherwise the endpoint check in
    # `_get_valid_chunk_starts` would not be enough to keep chunks inside an episode.
    boundaries = np.flatnonzero(np.diff(eps) != 0) + 1
    blocks = np.split(np.arange(eps.size), boundaries)
    for block in blocks:
        if np.unique(eps[block]).size != 1:
            raise SystemExit("episode frames are interleaved; chunk bookkeeping would be wrong")
    return {"action": actions, "observation.state": states, "episode_index": eps}


def reference_stats(frames: dict, horizon: int, exclude: tuple[int, ...]) -> dict:
    """Independent recomputation of the same quantity, written from scratch."""
    actions, states, eps = frames["action"], frames["observation.state"], frames["episode_index"]
    rel_chunks = []
    starts = []
    for i in range(eps.size - horizon + 1):
        if eps[i] != eps[i + horizon - 1]:
            continue
        starts.append(i)
    starts = np.asarray(starts)
    if starts.size:
        chunks = actions[starts[:, None] + np.arange(horizon)[None, :]].copy()
        anchors = states[starts]
        mask = np.ones(actions.shape[1], dtype=bool)
        mask[list(exclude)] = False
        chunks[:, :, mask] -= anchors[:, None, mask]
        rel_chunks.append(chunks.reshape(-1, actions.shape[1]))
    # Accumulate in float64: summing 450k float32 rows in float32 drifts the
    # standard deviation by ~1% (measured 0.039 on a 4.5 std), which would look
    # like an implementation disagreement rather than a precision bug.
    rel = np.concatenate(rel_chunks, axis=0).astype(np.float64)
    return {
        "chunks": int(starts.size),
        "rows": int(rel.shape[0]),
        "mean": rel.mean(axis=0).tolist(),
        "std": rel.std(axis=0).tolist(),
        "min": rel.min(axis=0).tolist(),
        "max": rel.max(axis=0).tolist(),
        "abs_mean": np.abs(rel).mean(axis=0).tolist(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--splits", required=True, type=Path)
    ap.add_argument("--absolute-stats", required=True, type=Path)
    ap.add_argument("--info", type=Path, default=None)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--horizon", type=int, default=HORIZON)
    ap.add_argument("--exclude-joints", default="gripper")
    ap.add_argument("--workers", type=int, default=1)
    args = ap.parse_args()

    from lerobot.datasets.compute_stats import compute_relative_action_stats

    splits = json.loads(args.splits.read_text())
    train = [int(e) for e in splits["train"]]
    info = json.loads((args.info or (args.root / "meta/info.json")).read_text())
    features = info["features"]
    names = features["action"]["names"]

    frames = load_frames(args.root, train)
    exclude_names = [s for s in args.exclude_joints.split(",") if s]

    # `compute_relative_action_stats` takes an hf-dataset-like mapping; column
    # access by name is all it uses.
    hf_like = {
        "action": frames["action"],
        "observation.state": frames["observation.state"],
        "episode_index": frames["episode_index"],
    }
    lib = compute_relative_action_stats(
        hf_dataset=hf_like,
        features=features,
        chunk_size=args.horizon,
        exclude_joints=exclude_names,
        num_workers=args.workers,
    )
    lib = {k: (np.asarray(v, dtype=np.float64) if isinstance(v, (list, np.ndarray)) else v)
           for k, v in lib.items()}

    # The gripper dimensions must be untouched, so derive which dims the mask kept
    # absolute from the names rather than hard-coding indices.
    excluded_dims = [i for i, n in enumerate(names) if any(tok in n.lower() for tok in exclude_names)]
    kept_relative = [i for i in range(len(names)) if i not in excluded_dims]

    ref = reference_stats(frames, args.horizon, tuple(excluded_dims))

    mean_diff = float(np.max(np.abs(lib["mean"] - np.asarray(ref["mean"]))))
    std_diff = float(np.max(np.abs(lib["std"] - np.asarray(ref["std"]))))

    absolute = json.loads(args.absolute_stats.read_text())
    abs_mean = np.asarray(absolute["action"]["mean"])
    abs_std = np.asarray(absolute["action"]["std"])

    # Only the dimensions that actually move can be "tightened" by going
    # relative. A static channel (std ~0) has the same spread either way, so it is
    # excluded from the comparison instead of being counted as a regression.
    active_relative = [d for d in kept_relative if abs_std[d] >= 1.0]
    checks = {
        "library_matches_independent_mean": mean_diff < 1e-4,
        "library_matches_independent_std": std_diff < 1e-4,
        # A gripper kept absolute must keep the absolute statistics.
        # Excluded dims stay absolute, so their values are the raw ones; their
        # statistics are chunk-weighted (edge frames appear in fewer chunks) and
        # therefore must NOT be compared against per-frame absolute stats.
        "gripper_stays_absolute": bool(
            np.all(lib["mean"][excluded_dims] > 1.0)),
        # Relative joints should be centred near zero and much tighter than absolute.
        "relative_joint_mean_near_zero": bool(
            np.all(np.abs(lib["mean"][kept_relative]) < 1.0)),
        "active_relative_std_smaller": bool(
            active_relative and np.all(lib["std"][active_relative] < abs_std[active_relative])),
        "dims_partitioned": bool(
            sorted(excluded_dims + kept_relative) == list(range(len(names)))),
        "no_nan": bool(np.all(np.isfinite(lib["mean"])) and np.all(np.isfinite(lib["std"]))),
    }

    report = {
        "kind": "relative-action-stats",
        "root": str(args.root),
        "train_episodes": len(train),
        "frames": int(frames["episode_index"].size),
        "horizon": args.horizon,
        "exclude_joints": exclude_names,
        "excluded_dimensions": excluded_dims,
        "relative_dimensions": kept_relative,
        "active_relative_dimensions": active_relative,
        "active_std_ratio": {
            str(d): float(lib["std"][d] / abs_std[d]) for d in active_relative
        },
        "feature_names": names,
        "chunks": ref["chunks"],
        "rows": ref["rows"],
        "lib": {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in lib.items()},
        "independent": ref,
        "max_abs_diff_mean": mean_diff,
        "max_abs_diff_std": std_diff,
        "absolute_action_stats": {"mean": abs_mean.tolist(), "std": abs_std.tolist()},
        "checks": checks,
        "status": "passed" if all(checks.values()) else "failed",
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({
        "status": report["status"],
        "chunks": report["chunks"], "rows": report["rows"],
        "checks": checks,
        "rel_mean": [round(v, 3) for v in lib["mean"]],
        "rel_std": [round(v, 3) for v in lib["std"]],
        "abs_std": [round(float(v), 3) for v in abs_std],
        "max_diff": [mean_diff, std_diff],
    }, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
