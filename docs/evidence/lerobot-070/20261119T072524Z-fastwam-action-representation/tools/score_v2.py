"""Scorer v2: paired, per-episode, per-segment, unit-aware.

Differences from the round-1 scorer (`.../20261118T070852Z/tools/score_predictions.py`):

1. **Pairing.** Candidates are matched to the ground truth by `(episode, anchor)`,
   not by array position. Duplicate, missing or reordered keys are rejected, and
   `gt` / `pad` / `state` must agree exactly between the candidate and the
   reference that is compared against it.
2. **Episode-equal aggregation.** A window's error is averaged over its valid
   steps first, then episodes are averaged with equal weight. The old code
   averaged over windows, which silently weights an episode with more anchors
   more heavily.
3. **Segments.** First step, short prefix (0-3), late (16-31) and full (0-31)
   are reported separately, because a policy can score well by copying the
   current pose at t=0 and then drift.
4. **Units.** Left/right arm joints are reported in degrees, grippers in
   percentage points. The old `raw_all` mixed both into one number.
5. **Window labels.** Windows are tagged joint-dynamic / static and
   gripper-dynamic from their **ground truth** with thresholds frozen on the
   train split (`windows.json`), so the split-by-dynamism report is not
   post-hoc slicing of a headline number.

The round-1 numbers are still computed and reported under `legacy` so the old
`not_passed` verdict can be compared like for like.

Usage:
    python score_v2.py --preds preds-l5-dev.npz --reference preds-step0-dev.npz \
        --train-stats train_stats.json --windows windows.json --out score.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

# Dimension layout of `local/so101_scene1_left_red_v1` (meta/info.json names).
LEFT_ARM_JOINTS = (0, 1, 2, 3, 4)
LEFT_GRIPPER = 5
RIGHT_ARM_JOINTS = (6, 7, 8, 9, 10)
RIGHT_GRIPPER = 11
GROUPS = {
    "left_arm_joints": LEFT_ARM_JOINTS,
    "left_gripper": (LEFT_GRIPPER,),
    "right_arm_joints": RIGHT_ARM_JOINTS,
    "right_gripper": (RIGHT_GRIPPER,),
}
UNIT = {
    "left_arm_joints": "deg",
    "left_gripper": "pct",
    "right_arm_joints": "deg",
    "right_gripper": "pct",
}
ACTIVE_DIMS = LEFT_ARM_JOINTS + (LEFT_GRIPPER,)      # the arm that actually moves
STATIC_DIMS = RIGHT_ARM_JOINTS + (RIGHT_GRIPPER,)
SEGMENTS = {
    "first": (0, 1),
    "prefix": (0, 4),
    "late": (16, 32),
    "full": (0, 32),
}


# --------------------------------------------------------------------------- io
def load_npz(path: Path) -> dict:
    with np.load(path) as data:
        out = {k: data[k] for k in data.files}
    required = ("pred", "gt", "pad", "state", "episode", "anchor")
    missing = [k for k in required if k not in out]
    if missing:
        raise SystemExit(f"{path}: missing arrays {missing} (has {sorted(out)})")
    out["pad"] = out["pad"].astype(bool)
    if out["pred"].shape != out["gt"].shape:
        raise SystemExit(f"{path}: pred {out['pred'].shape} != gt {out['gt'].shape}")
    if out["pad"].shape != out["gt"].shape[:2]:
        raise SystemExit(f"{path}: pad {out['pad'].shape} != {out['gt'].shape[:2]}")
    return out


def keys_of(data: dict) -> list[tuple[int, int]]:
    return [(int(e), int(a)) for e, a in zip(data["episode"], data["anchor"])]


def validate_pairing(candidate: dict, reference: dict | None) -> dict:
    """Reject duplicate/missing/misaligned keys and inconsistent labels."""
    cand_keys = keys_of(candidate)
    if len(set(cand_keys)) != len(cand_keys):
        dupes = sorted({k for k in cand_keys if cand_keys.count(k) > 1})
        raise SystemExit(f"candidate has duplicate (episode, anchor) keys: {dupes[:5]}")
    report = {
        "windows": len(cand_keys),
        "episodes": len({e for e, _ in cand_keys}),
        "unique_keys": True,
    }
    if reference is None:
        return report

    ref_keys = keys_of(reference)
    if len(set(ref_keys)) != len(ref_keys):
        dupes = sorted({k for k in ref_keys if ref_keys.count(k) > 1})
        raise SystemExit(f"reference has duplicate (episode, anchor) keys: {dupes[:5]}")
    if set(ref_keys) != set(cand_keys):
        missing = sorted(set(cand_keys) - set(ref_keys))[:5]
        extra = sorted(set(ref_keys) - set(cand_keys))[:5]
        raise SystemExit(f"key mismatch: missing in reference {missing}, extra {extra}")

    # Align the reference to the candidate's order, then require identical labels.
    order = {k: i for i, k in enumerate(ref_keys)}
    idx = np.asarray([order[k] for k in cand_keys])
    for field in ("gt", "pad", "state"):
        ref = reference[field][idx]
        if not np.array_equal(ref, candidate[field]):
            diff = float(np.nanmax(np.abs(ref.astype(float) - candidate[field].astype(float)))) \
                if ref.dtype.kind in "fiu" else "n/a"
            raise SystemExit(f"reference {field} differs after alignment (max abs diff {diff})")
    report["reference_aligned"] = True
    report["reference_keys_equal"] = True
    return report


# ---------------------------------------------------------------------- metrics
def _as_dims(dims) -> tuple[int, ...]:
    """Accept a single dimension index or a sequence of them."""
    if isinstance(dims, (int, np.integer)):
        return (int(dims),)
    return tuple(int(d) for d in dims)


def group_mae(pred: np.ndarray, gt: np.ndarray, keep: np.ndarray,
              dims: tuple[int, ...], seg: tuple[int, int],
              episode_ids: np.ndarray) -> dict:
    """Episode-equal mean absolute error for one group over one segment.

    Windows that belong to the same episode are pooled (all their valid steps)
    and contribute **one** number, so an episode with five anchors cannot
    outvote an episode with one. This is the difference from round 1, which
    averaged over windows.
    """
    dims = _as_dims(dims)
    start, stop = seg
    pooled: dict[int, list[np.ndarray]] = {}
    steps_per_episode: dict[int, int] = {}
    for i in range(gt.shape[0]):
        sl = slice(start, min(stop, gt.shape[1]))
        mask = keep[i][sl]
        if not mask.any():
            continue
        err = np.abs(pred[i][sl][mask][:, dims] - gt[i][sl][mask][:, dims])
        ep = int(episode_ids[i])
        pooled.setdefault(ep, []).append(err)
        steps_per_episode[ep] = steps_per_episode.get(ep, 0) + int(mask.sum())
    if not pooled:
        return {"mae": float("nan"), "episodes": 0, "steps": 0}

    per_episode = np.stack([
        np.concatenate(pooled[ep], axis=0).mean(axis=0) for ep in sorted(pooled)
    ])                                            # [n_eps, len(dims)]
    episode_scalars = per_episode.mean(axis=1)
    return {
        "mae": float(episode_scalars.mean()),
        "mae_per_dim": [float(v) for v in per_episode.mean(axis=0)],
        "episodes": int(per_episode.shape[0]),
        "steps": int(sum(steps_per_episode.values())),
        "worst_episode": float(episode_scalars.max()),
        "best_episode": float(episode_scalars.min()),
    }


def window_equal_mae(pred: np.ndarray, gt: np.ndarray, keep: np.ndarray,
                     dims: tuple[int, ...], seg: tuple[int, int]) -> float:
    """The round-1 aggregation: mean over windows of their masked mean error."""
    dims = _as_dims(dims)
    start, stop = seg
    values = []
    for i in range(gt.shape[0]):
        sl = slice(start, min(stop, gt.shape[1]))
        mask = keep[i][sl]
        if not mask.any():
            continue
        values.append(float(np.abs(pred[i][sl][mask][:, dims] - gt[i][sl][mask][:, dims]).mean()))
    return float(np.mean(values)) if values else float("nan")


def all_metrics(pred: np.ndarray, gt: np.ndarray, keep: np.ndarray,
                episode_ids: np.ndarray) -> dict:
    out = {}
    for name, dims in GROUPS.items():
        out[name] = {
            seg: group_mae(pred, gt, keep, dims, sl, episode_ids)
            for seg, sl in SEGMENTS.items()
        }
    # Mixed-unit legacy numbers, kept only so round-1 can be reproduced.
    all_dims = tuple(range(gt.shape[2]))
    out["_legacy_all_dims"] = {
        seg: {"window_equal_mae": window_equal_mae(pred, gt, keep, all_dims, sl),
              "episode_equal_mae": group_mae(pred, gt, keep, all_dims, sl, episode_ids)["mae"]}
        for seg, sl in SEGMENTS.items()
    }
    return out


# ------------------------------------------------------------------ window tags
def label_windows(keys: list[tuple[int, int]], gt: np.ndarray,
                  windows: dict) -> dict:
    """Attach the frozen dynamism labels to each window, using its ground truth."""
    thr = windows["thresholds"]
    joint_thr = float(thr["left_joint_displacement_deg"])
    grip_thr = float(thr["left_gripper_displacement_pct"])
    labels = []
    for i, (ep, anchor) in enumerate(keys):
        chunk = gt[i]
        joint_disp = float(np.max(np.abs(chunk[:, LEFT_ARM_JOINTS] - chunk[0, LEFT_ARM_JOINTS])))
        grip_disp = float(np.max(np.abs(chunk[:, LEFT_GRIPPER] - chunk[0, LEFT_GRIPPER])))
        labels.append({
            "episode": ep, "anchor": anchor,
            "left_joint_displacement": joint_disp,
            "left_gripper_displacement": grip_disp,
            "joint_dynamic": joint_disp >= joint_thr,
            "gripper_dynamic": grip_disp >= grip_thr,
        })
    return {
        "thresholds": {"left_joint_displacement_deg": joint_thr,
                       "left_gripper_displacement_pct": grip_thr},
        "joint_dynamic": sum(1 for r in labels if r["joint_dynamic"]),
        "gripper_dynamic": sum(1 for r in labels if r["gripper_dynamic"]),
        "windows": labels,
    }


def subset_metrics(pred: np.ndarray, gt: np.ndarray, keep: np.ndarray,
                   selector: np.ndarray, episode_ids: np.ndarray,
                   segment: str = "full") -> dict:
    """Metrics restricted to a boolean subset of windows (e.g. dynamic ones).

    Still episode-equal: an episode contributes one number even if several of
    its windows fall in the subset.
    """
    start, stop = SEGMENTS[segment]
    idx = np.flatnonzero(selector)
    out = {}
    if idx.size == 0:
        for name in GROUPS:
            out[name] = {"mae": float("nan"), "windows": 0, "episodes": 0}
        return out
    for name, dims in GROUPS.items():
        dims = _as_dims(dims)
        pooled: dict[int, list[np.ndarray]] = {}
        for i in idx:
            sl = slice(start, min(stop, gt.shape[1]))
            mask = keep[i][sl]
            if not mask.any():
                continue
            err = np.abs(pred[i][sl][mask][:, dims] - gt[i][sl][mask][:, dims])
            pooled.setdefault(int(episode_ids[i]), []).append(err)
        if not pooled:
            out[name] = {"mae": float("nan"), "windows": 0, "episodes": 0}
            continue
        per_episode = [np.concatenate(v, axis=0).mean(axis=0) for v in pooled.values()]
        out[name] = {
            "mae": float(np.mean([v.mean() for v in per_episode])),
            "windows": int(idx.size),
            "episodes": len(per_episode),
        }
    return out


# ----------------------------------------------------------------------- verdict
def legacy_verdict(candidate: dict, baselines: dict, min_improvement: float = 0.25,
                   static_tolerance: float = 1.5) -> dict:
    """Round-1 gates, reproduced verbatim for like-for-like comparison."""
    def scaled(m: dict) -> float:
        return m["_legacy_scaled_active"]

    def raw_all(m: dict) -> float:
        return m["_legacy_all_dims"]["full"]["window_equal_mae"]

    def static(m: dict) -> float:
        return m["right_arm_joints"]["full"]["mae"]

    best_scaled = min(baselines.items(), key=lambda kv: scaled(kv[1]))
    best_raw = min(baselines.items(), key=lambda kv: raw_all(kv[1]))
    imp_scaled = 1.0 - scaled(candidate) / scaled(best_scaled[1]) if scaled(best_scaled[1]) else float("nan")
    imp_raw = 1.0 - raw_all(candidate) / raw_all(best_raw[1]) if raw_all(best_raw[1]) else float("nan")
    static_ratio = (static(candidate) / static(best_raw[1])
                    if static(best_raw[1]) else float("inf"))
    checks = {
        "beats_best_baseline_active_scaled": bool(np.isfinite(imp_scaled) and imp_scaled >= min_improvement),
        "beats_best_baseline_all_raw": bool(np.isfinite(imp_raw) and imp_raw >= min_improvement),
        "static_dims_not_degraded": bool(np.isfinite(static_ratio) and static_ratio <= static_tolerance),
    }
    return {"passed": all(checks.values()), "checks": checks,
            "improvement_active_scaled": imp_scaled, "improvement_all_raw": imp_raw,
            "static_ratio_vs_best": static_ratio,
            "best_baseline_active_scaled": best_scaled[0],
            "best_baseline_all_raw": best_raw[0],
            "min_improvement": min_improvement, "static_tolerance": static_tolerance}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preds", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--reference", type=Path, default=None)
    ap.add_argument("--train-stats", type=Path, default=None)
    ap.add_argument("--windows", type=Path, default=None)
    ap.add_argument("--min-std", type=float, default=1.0)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    cand = load_npz(args.preds)
    ref = load_npz(args.reference) if args.reference else None
    pairing = validate_pairing(cand, ref)
    keys = keys_of(cand)

    gt, keep = cand["gt"], ~cand["pad"]
    report: dict = {
        "kind": "so101-dev-score-v2",
        "label": args.label,
        "preds": str(args.preds),
        "reference": str(args.reference) if args.reference else None,
        "pairing": pairing,
        "groups": {k: list(v) for k, v in GROUPS.items()},
        "unit": UNIT,
        "segments": {k: list(v) for k, v in SEGMENTS.items()},
    }

    candidates = {"candidate": cand}
    if ref is not None:
        candidates["reference"] = ref
    if args.train_stats is not None:
        stats = json.loads(args.train_stats.read_text())
        if stats.get("scope") != "train split only":
            raise SystemExit(f"{args.train_stats}: scope {stats.get('scope')!r} is not train-only")
        mean = np.asarray(stats["action"]["mean"], dtype=np.float64)
        candidates["train_mean"] = {
            **cand,
            "pred": np.broadcast_to(mean.reshape(1, 1, -1), gt.shape).copy(),
        }
    candidates["zero"] = {**cand, "pred": np.zeros_like(gt)}
    candidates["state_copy"] = {
        **cand,
        "pred": np.broadcast_to(cand["state"][:, None, :], gt.shape).copy(),
    }

    episode_ids = cand["episode"]
    metrics = {name: all_metrics(c["pred"], gt, keep, episode_ids)
               for name, c in candidates.items()}

    # Round-1 scaling used per-dim train std over the active set; keep it so the
    # legacy verdict is reproducible.
    if args.train_stats is not None:
        std = np.asarray(stats["action"]["std"], dtype=np.float64)
        active = std >= args.min_std
        for name, c in candidates.items():
            per_dim = np.abs(c["pred"] - gt)
            masked = [per_dim[i][keep[i]] for i in range(gt.shape[0])]
            dim_mae = np.concatenate(masked, axis=0).mean(axis=0)
            metrics[name]["_legacy_scaled_active"] = float(np.nanmean(dim_mae[active] / std[active]))
            metrics[name]["_legacy_per_dim_mae"] = [float(v) for v in dim_mae]
        report["active_dimensions"] = np.flatnonzero(active).tolist()
        report["min_std"] = args.min_std
    else:
        for name in candidates:
            metrics[name]["_legacy_scaled_active"] = float("nan")

    report["metrics"] = metrics
    report["legacy_verdict"] = legacy_verdict(metrics["candidate"], metrics)

    if args.windows is not None:
        windows = json.loads(args.windows.read_text())
        labels = label_windows(keys, gt, windows)
        report["window_labels"] = {k: v for k, v in labels.items() if k != "windows"}
        joint_dyn = np.asarray([r["joint_dynamic"] for r in labels["windows"]])
        grip_dyn = np.asarray([r["gripper_dynamic"] for r in labels["windows"]])
        by_label = {}
        for name, c in candidates.items():
            by_label[name] = {
                "joint_dynamic": subset_metrics(c["pred"], gt, keep, joint_dyn, episode_ids),
                "joint_static": subset_metrics(c["pred"], gt, keep, ~joint_dyn, episode_ids),
                "gripper_dynamic": subset_metrics(c["pred"], gt, keep, grip_dyn, episode_ids),
                "gripper_static": subset_metrics(c["pred"], gt, keep, ~grip_dyn, episode_ids),
                "joint_and_gripper_dynamic": subset_metrics(c["pred"], gt, keep,
                                                            joint_dyn & grip_dyn, episode_ids),
            }
        report["by_window_label"] = by_label
        report["window_table"] = labels["windows"]

    args.out.write_text(json.dumps(report, indent=2) + "\n")

    def brief(name: str) -> dict:
        m = metrics[name]
        return {
            "first_left_joints_deg": round(m["left_arm_joints"]["first"]["mae"], 3),
            "late_left_joints_deg": round(m["left_arm_joints"]["late"]["mae"], 3),
            "full_left_joints_deg": round(m["left_arm_joints"]["full"]["mae"], 3),
            "first_left_gripper_pct": round(m["left_gripper"]["first"]["mae"], 3),
            "full_left_gripper_pct": round(m["left_gripper"]["full"]["mae"], 3),
            "full_right_joints_deg": round(m["right_arm_joints"]["full"]["mae"], 3),
            "legacy_scaled_active": round(m["_legacy_scaled_active"], 4),
        }
    print(json.dumps({
        "pairing": pairing,
        "candidate": brief("candidate"),
        "reference": brief("reference") if "reference" in metrics else None,
        "state_copy": brief("state_copy"),
        "legacy_verdict": report["legacy_verdict"]["checks"],
        "legacy_passed": report["legacy_verdict"]["passed"],
    }, indent=2))
    return 0 if report["legacy_verdict"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
