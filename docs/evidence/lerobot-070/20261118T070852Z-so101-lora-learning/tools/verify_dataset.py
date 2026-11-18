"""LEARN-DAT-01/02/03: read-only verification of the SO-101 left-red dataset.

Checks completeness, the frozen 100/20 split, per-episode frame accounting,
camera layout, action semantics (absolute joint targets), FPS/time monotonicity
and train-only normalisation statistics. It never writes to the dataset.

Usage:
    python verify_dataset.py --dataset /path/to/dataset --audit /path/to/audit.json \
        --out inventory.json [--stats-out train_stats.json] [--decode-episode 20303]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import pyarrow.parquet as pq


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, type=Path)
    ap.add_argument("--audit", required=True, type=Path)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--stats-out", type=Path)
    ap.add_argument("--decode-episode", type=int, default=None)
    args = ap.parse_args()

    ds = args.dataset
    info = json.loads((ds / "meta/info.json").read_text())
    tasks = pq.read_table(ds / "meta/tasks.parquet").to_pylist()
    ep_meta = pq.read_table(ds / "meta/episodes/chunk-000/file-000.parquet").to_pylist()
    audit = json.loads(args.audit.read_text())
    manifest = json.loads(args.manifest.read_text())

    # Episode i is the i-th success row in collection order. Map through the
    # attempt_id, because the audit list order and the parquet order are not
    # guaranteed to agree (the first version of this script assumed they did and
    # produced a train split whose frame count was 208 off the declared 74,131).
    role_by_attempt = {row["attempt_id"]: row["role"] for row in audit["episodes"]}
    attempt_order = [row["attempt_id"] for row in manifest["successes_rows"]]
    if len(attempt_order) != len(ep_meta):
        raise SystemExit(f"manifest rows {len(attempt_order)} != episodes {len(ep_meta)}")
    if set(attempt_order) != set(role_by_attempt):
        raise SystemExit("manifest attempt ids and audit attempt ids differ")
    roles_by_index = [role_by_attempt[a] for a in attempt_order]
    train_roles = [r for r in roles_by_index if r == "train"]
    val_roles = [r for r in roles_by_index if r == "validation"]

    report: dict = {
        "kind": "so101-dataset-inventory",
        "dataset": str(ds),
        "codebase_version": info.get("codebase_version"),
        "robot_type": info.get("robot_type"),
        "fps": info.get("fps"),
        "total_episodes": info.get("total_episodes"),
        "total_frames": info.get("total_frames"),
        "features": {
            k: {"shape": v.get("shape"), "dtype": v.get("dtype"), "names": v.get("names")}
            for k, v in info["features"].items()
        },
        "tasks": tasks,
        "split": {
            "train": len(train_roles),
            "validation": len(val_roles),
            "family_crosses": audit["split"]["family_crosses"],
            "families": audit["split"]["families"],
            "validation_families": audit["split"]["validation_families"],
            "source": "audit_formal_v1.json (frozen before any normalisation statistic)",
        },
        "norm_scope_declared": audit.get("norm_scope"),
        "norm_stats_declared": audit.get("norm_stats"),
    }

    # --- completeness: parquet + three videos per episode -------------------
    missing: list[str] = []
    frames_seen = 0
    per_episode = []
    data_files = set()
    video_files = {k: set() for k in info["features"] if k.startswith("observation.images")}
    for row in ep_meta:
        ep = row["episode_index"]
        data_rel = info["data_path"].format(
            chunk_index=row["data/chunk_index"], file_index=row["data/file_index"]
        )
        data_files.add(data_rel)
        if not (ds / data_rel).is_file():
            missing.append(data_rel)
        for key in video_files:
            chunk = row[f"videos/{key}/chunk_index"]
            file_index = row[f"videos/{key}/file_index"]
            rel = info["video_path"].format(video_key=key, chunk_index=chunk, file_index=file_index)
            video_files[key].add(rel)
            if not (ds / rel).is_file():
                missing.append(rel)
        frames_seen += int(row["length"])
        per_episode.append({
            "episode_index": ep,
            "length": int(row["length"]),
            "tasks": row["tasks"],
            "from_index": int(row["dataset_from_index"]),
            "to_index": int(row["dataset_to_index"]),
        })

    report["files"] = {
        "data_parquet": len(data_files),
        "video_files": {k: len(v) for k, v in video_files.items()},
        "missing": missing[:20],
        "missing_count": len(missing),
        "sum_episode_lengths": frames_seen,
        "declared_total_frames": info.get("total_frames"),
        "frames_match": frames_seen == info.get("total_frames"),
    }

    # --- split integrity -----------------------------------------------------
    ids = [row["episode_index"] for row in ep_meta]
    leak = len(ids) != len(set(ids))
    report["split"]["no_duplicate_episode_index"] = not leak
    report["split"]["frames_train"] = sum(
        e["length"] for e in per_episode if roles_by_index[int(e["episode_index"])] == "train"
    )
    report["split"]["frames_validation"] = sum(
        e["length"] for e in per_episode if roles_by_index[int(e["episode_index"])] == "validation"
    )

    # --- content hashes ------------------------------------------------------
    report["hashes"] = {
        "meta/info.json": sha256_file(ds / "meta/info.json"),
        "meta/stats.json": sha256_file(ds / "meta/stats.json"),
        "meta/tasks.parquet": sha256_file(ds / "meta/tasks.parquet"),
        "meta/episodes/chunk-000/file-000.parquet": sha256_file(
            ds / "meta/episodes/chunk-000/file-000.parquet"
        ),
    }

    # --- action semantics & finite checks on a sample of episodes -----------
    import numpy as np

    sample_rows = [ep_meta[0], ep_meta[len(ep_meta) // 2], ep_meta[-1]]
    semantics = []
    for row in sample_rows:
        rel = info["data_path"].format(
            chunk_index=row["data/chunk_index"], file_index=row["data/file_index"]
        )
        table = pq.read_table(ds / rel, columns=["observation.state", "action", "timestamp", "episode_index"])
        ep_id = int(row["episode_index"])
        keep = np.asarray(table["episode_index"].to_pylist()) == ep_id
        state = np.asarray(table["observation.state"].to_pylist(), dtype=np.float32)[keep]
        action = np.asarray(table["action"].to_pylist(), dtype=np.float32)[keep]
        ts = np.asarray(table["timestamp"].to_pylist(), dtype=np.float64).reshape(-1)[keep]
        dt = np.diff(ts)
        semantics.append({
            "episode_index": int(row["episode_index"]),
            "rows": int(state.shape[0]),
            "state_finite": bool(np.isfinite(state).all()),
            "action_finite": bool(np.isfinite(action).all()),
            "state_min": state.min(axis=0).round(3).tolist(),
            "state_max": state.max(axis=0).round(3).tolist(),
            "action_min": action.min(axis=0).round(3).tolist(),
            "action_max": action.max(axis=0).round(3).tolist(),
            "dt_min": float(dt.min()) if dt.size else None,
            "dt_max": float(dt.max()) if dt.size else None,
            "expected_dt": 1.0 / float(info["fps"]),
            "dt_ok": bool(dt.size and abs(dt.min() - 1 / info["fps"]) < 1e-4
                          and abs(dt.max() - 1 / info["fps"]) < 1e-4),
        })
    report["sample_semantics"] = semantics

    # --- optional frame decode ----------------------------------------------
    if args.decode_episode is not None:
        import av

        row = next(r for r in ep_meta if r["episode_index"] == args.decode_episode)
        decoded = {}
        for key in video_files:
            rel = info["video_path"].format(
                video_key=key,
                chunk_index=row[f"videos/{key}/chunk_index"],
                file_index=row[f"videos/{key}/file_index"],
            )
            start = float(row[f"videos/{key}/from_timestamp"])
            stop = float(row[f"videos/{key}/to_timestamp"])
            with av.open(str(ds / rel)) as container:
                stream = container.streams.video[0]
                # The file holds several episodes: seek into this episode's window
                # instead of decoding frame 0 of the file.
                seek_pts = int(start / float(stream.time_base))
                container.seek(seek_pts, stream=stream, backward=True)
                first = None
                for frame in container.decode(stream):
                    if frame.pts is not None and float(frame.pts) * float(stream.time_base) >= start - 1e-3:
                        first = frame
                        break
                decoded[key] = {
                    "shape": [first.height, first.width, 3] if first is not None else None,
                    "window_seconds": round(stop - start, 4),
                    "expected_frames_in_window": int(round((stop - start) * info["fps"])),
                    "average_rate": float(stream.average_rate) if stream.average_rate else None,
                    "rate_ok": abs(float(stream.average_rate) - info["fps"]) < 1e-3
                    if stream.average_rate else False,
                }
        report["decoded_episode"] = {"episode_index": args.decode_episode, "cameras": decoded}

    # --- train-only statistics ----------------------------------------------
    if args.stats_out:
        train_indices = {
            int(e["episode_index"]) for e in per_episode
            if roles_by_index[int(e["episode_index"])] == "train"
        }
        # One read for the whole split: the dataset packs every episode into a
        # single parquet, so a per-episode loop re-reads 89k rows 100 times and
        # (worse) silently multiplies the frame count when the mask is forgotten.
        state_parts, action_parts = [], []
        for rel in sorted(data_files):
            table = pq.read_table(
                ds / rel, columns=["observation.state", "action", "episode_index"]
            )
            ep_ids = np.asarray(table["episode_index"].to_pylist())
            keep = np.isin(ep_ids, np.fromiter(train_indices, dtype=np.int64))
            if keep.any():
                state_parts.append(
                    np.asarray(table["observation.state"].to_pylist(), dtype=np.float32)[keep]
                )
                action_parts.append(
                    np.asarray(table["action"].to_pylist(), dtype=np.float32)[keep]
                )
        state = np.concatenate(state_parts, axis=0)
        action = np.concatenate(action_parts, axis=0)

        def summarise(arr: np.ndarray) -> dict:
            return {
                "mean": arr.mean(axis=0).tolist(),
                "std": arr.std(axis=0).tolist(),
                "min": arr.min(axis=0).tolist(),
                "max": arr.max(axis=0).tolist(),
                "count": [int(arr.shape[0])],
            }

        payload = {
            "scope": "train split only",
            "episodes": len(train_indices),
            "frames": int(state.shape[0]),
            "source_episode_indices": sorted(train_indices),
            "observation.state": summarise(state),
            "action": summarise(action),
            "camera_keys": sorted(k for k in info["features"] if k.startswith("observation.images")),
        }
        args.stats_out.write_text(json.dumps(payload, indent=2) + "\n")
        declared = audit.get("norm_stats") or {}
        if declared:
            decl_state = declared.get("state", {}).get("mean")
            report["train_stats_cross_check"] = {
                "declared_episodes": declared.get("episodes"),
                "declared_frames": declared.get("frames"),
                "state_mean_max_abs_diff": max(
                    abs(a - b) for a, b in zip(payload["observation.state"]["mean"], decl_state)
                ) if decl_state else None,
                "frame_count_matches": declared.get("frames") == payload["frames"],
            }

    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({
        "episodes": report["total_episodes"],
        "frames": report["total_frames"],
        "missing": report["files"]["missing_count"],
        "frames_match": report["files"]["frames_match"],
        "split": [report["split"]["train"], report["split"]["validation"]],
        "dt_ok": [s["dt_ok"] for s in semantics],
        "stats": report.get("train_stats_cross_check"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
