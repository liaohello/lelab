"""Score pre-registered SO-101 evaluation predictions (CPU, no model).

Reads a `.npz` produced by `collect_predictions.py` and compares the candidate
against trivial baselines computed here from the ground truth itself, plus the
random-head reference when one is supplied:

  * `zero`       -- predict all zeros in raw action space
  * `train_mean` -- predict the train-split mean for every step
  * `state_copy` -- hold the current pose (strong for 50 fps absolute targets)
  * `step0`      -- the *same* head before any adapter training (`--reference`)

Why three gates instead of one number: 6 of the 12 channels have a train std of
0.00-0.12 degrees (the right arm never moves in this task). Averaging a scaled
error over all 12 dimensions either divides by zero or lets a model win by being
perfect on channels that are constant anyway. So:

  1. `mae_scaled_active` -- mean |error| / train_std over the dimensions that
     actually move (std >= `--min-std`, degrees). Must improve >= 25%.
  2. `mae_raw_all` -- mean |error| in degrees over all 12 dimensions, equal
     weight. Must also improve >= 25%.
  3. static dimensions must not degrade: their raw MAE may be at most
     `--static-tolerance` times the best baseline's (default 1.5x), so accuracy
     on the moving arm cannot be bought by wrecking the rest.

The logic does no I/O beyond the npz/JSON so it can be tested on synthetic
arrays (`test_score_logic.py`).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def _masked_mean(values: np.ndarray, mask: np.ndarray, axis) -> np.ndarray:
    """Mean over `axis` ignoring masked entries; NaN if a slice is fully masked.

    `mask` marks timesteps `[N, T]`; `values` may carry a trailing action
    dimension, in which case the mask is broadcast over it.
    """
    weights = mask.astype(np.float64)
    while weights.ndim < values.ndim:
        weights = weights[..., None]
    total = (values.astype(np.float64) * weights).sum(axis=axis)
    count = weights.sum(axis=axis)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(count > 0, total / np.maximum(count, 1.0), np.nan)


def metrics(pred: np.ndarray, gt: np.ndarray, pad: np.ndarray, state: np.ndarray,
            std: np.ndarray, active: np.ndarray) -> dict:
    """All reported metrics for one candidate.

    Shapes: pred/gt `[N, T, D]`, pad `[N, T]`, state `[N, D]`, std/active `[D]`.
    Missing (padded) steps are excluded everywhere.
    """
    if pred.shape != gt.shape:
        raise ValueError(f"pred {pred.shape} != gt {gt.shape}")
    if pad.shape != gt.shape[:2]:
        raise ValueError(f"pad {pad.shape} != {gt.shape[:2]}")
    if state.shape != (gt.shape[0], gt.shape[2]):
        raise ValueError(f"state {state.shape} != {(gt.shape[0], gt.shape[2])}")
    if std.shape != (gt.shape[2],) or active.shape != std.shape:
        raise ValueError("std/active must have one entry per action dimension")

    keep = ~pad
    err = np.abs(pred - gt)
    finite = bool(np.isfinite(pred).all() and np.isfinite(gt).all())

    per_dim = _masked_mean(err, keep, axis=(0, 1))            # [D]
    safe_std = np.where(active, std, np.nan)
    per_dim_scaled = per_dim / safe_std                       # NaN outside the active set

    def mean_over(values: np.ndarray, dims: np.ndarray) -> float:
        if not dims.any():
            return float("nan")
        return float(np.nanmean(values[dims]))

    left = np.arange(6)
    right = np.arange(6, gt.shape[2])
    static = ~active

    result = {
        "finite": finite,
        "mae_raw_per_dim": per_dim.tolist(),
        "mae_scaled_active_per_dim": {
            int(d): float(per_dim_scaled[d]) for d in np.flatnonzero(active)
        },
        "mae_raw_all": mean_over(per_dim, np.ones(gt.shape[2], dtype=bool)),
        "mae_scaled_active": mean_over(per_dim_scaled, active),
        "mae_raw_active": mean_over(per_dim, active),
        "mae_raw_static": mean_over(per_dim, static),
        "mae_raw_left": mean_over(per_dim, left),
        "mae_raw_right": mean_over(per_dim, right),
        "first_step_mae_raw": (
            float(np.nanmean(_masked_mean(err[:, 0, :], keep[:, 0], axis=0)))
            if keep[:, 0].any() else float("nan")
        ),
        "horizon_mae_raw": _masked_mean(err, keep, axis=1).mean(axis=0).tolist(),
        "steps_evaluated": int(keep.sum()),
    }
    # Episode-equal weighting: average the per-episode means, not the raw frames.
    per_ep = []
    for i in range(gt.shape[0]):
        m = keep[i]
        if not m.any():
            continue
        dim_err = _masked_mean(err[i], m, axis=0)
        scaled = dim_err[active] / std[active]
        per_ep.append(float(np.nanmean(scaled)))
    result["mae_scaled_active_episode_mean"] = float(np.mean(per_ep)) if per_ep else float("nan")
    result["episodes"] = len(per_ep)
    return result


def baselines(gt: np.ndarray, pad: np.ndarray, state: np.ndarray,
              train_mean: np.ndarray, std: np.ndarray, active: np.ndarray) -> dict:
    zero = np.zeros_like(gt)
    mean = np.broadcast_to(train_mean.reshape(1, 1, -1), gt.shape).copy()
    copy = np.broadcast_to(state.reshape(state.shape[0], 1, -1), gt.shape).copy()
    return {
        "zero": metrics(zero, gt, pad, state, std, active),
        "train_mean": metrics(mean, gt, pad, state, std, active),
        "state_copy": metrics(copy, gt, pad, state, std, active),
    }


def verdict(model: dict, base: dict, min_improvement: float,
            static_tolerance: float = 1.5) -> dict:
    """Pass only if the candidate clears all three gates against *every* baseline."""
    def best(key: str) -> tuple[str, float]:
        name, values = min(base.items(), key=lambda kv: kv[1][key])
        return name, float(values[key])

    best_scaled_name, best_scaled = best("mae_scaled_active")
    best_raw_name, best_raw = best("mae_raw_all")
    _, best_static = best("mae_raw_static")

    def improve(reference: float, value: float) -> float:
        if not np.isfinite(reference) or reference <= 0 or not np.isfinite(value):
            return float("nan")
        return 1.0 - value / reference

    imp_scaled = improve(best_scaled, model["mae_scaled_active"])
    imp_raw = improve(best_raw, model["mae_raw_all"])
    if not np.isfinite(best_static) or not np.isfinite(model["mae_raw_static"]):
        static_ratio = float("nan")
    elif best_static == 0:
        # A baseline that is already exact on the still channels: any positive
        # error is infinitely worse, zero is a tie.
        static_ratio = 1.0 if model["mae_raw_static"] == 0 else float("inf")
    else:
        static_ratio = model["mae_raw_static"] / best_static

    checks = {
        "finite": bool(model["finite"]),
        "beats_best_baseline_active_scaled": bool(
            np.isfinite(imp_scaled) and imp_scaled >= min_improvement
        ),
        "beats_best_baseline_all_raw": bool(
            np.isfinite(imp_raw) and imp_raw >= min_improvement
        ),
        "static_dims_not_degraded": bool(
            np.isfinite(static_ratio) and static_ratio <= static_tolerance
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "min_improvement_required": min_improvement,
        "static_tolerance": static_tolerance,
        "best_baseline_active_scaled": best_scaled_name,
        "best_baseline_active_scaled_value": best_scaled,
        "improvement_active_scaled": imp_scaled,
        "best_baseline_all_raw": best_raw_name,
        "best_baseline_all_raw_value": best_raw,
        "improvement_all_raw": imp_raw,
        "static_ratio_vs_best": static_ratio,
    }


def load_npz(path: Path) -> dict:
    with np.load(path) as data:
        out = {k: data[k] for k in data.files}
    for key in ("pred", "gt", "pad", "state"):
        if key not in out:
            raise SystemExit(f"{path}: missing array {key!r} (has {sorted(out)})")
    out["pad"] = out["pad"].astype(bool)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preds", required=True, type=Path)
    ap.add_argument("--train-stats", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--reference", type=Path, default=None,
                    help="step0 / random-head predictions (npz) used as a baseline")
    ap.add_argument("--min-std", type=float, default=1.0,
                    help="a dimension counts as 'active' when its train std reaches this (degrees)")
    ap.add_argument("--min-improvement", type=float, default=0.25)
    ap.add_argument("--static-tolerance", type=float, default=1.5)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    data = load_npz(args.preds)
    stats = json.loads(args.train_stats.read_text())
    if stats.get("scope") != "train split only":
        raise SystemExit(f"{args.train_stats}: stats scope is {stats.get('scope')!r}, expected train-only")
    std = np.asarray(stats["action"]["std"], dtype=np.float64)
    train_mean = np.asarray(stats["action"]["mean"], dtype=np.float64)
    if std.shape != (data["gt"].shape[2],):
        raise SystemExit(f"train stats have {std.shape[0]} dims, predictions have {data['gt'].shape[2]}")
    active = std >= args.min_std

    gt, pad, state = data["gt"], data["pad"], data["state"]
    model = metrics(data["pred"], gt, pad, state, std, active)
    base = baselines(gt, pad, state, train_mean, std, active)
    report: dict = {
        "kind": "so101-dev-score",
        "label": args.label,
        "preds": str(args.preds),
        "dimensions": int(gt.shape[2]),
        "horizon": int(gt.shape[1]),
        "episode_anchors": int(gt.shape[0]),
        "active_dimensions": np.flatnonzero(active).tolist(),
        "active_dimension_std": [float(v) for v in std[active]],
        "static_dimension_std": [float(v) for v in std[~active]],
        "min_std": args.min_std,
        "candidate": model,
        "baselines": base,
    }
    combined = dict(base)
    if args.reference is not None:
        ref = load_npz(args.reference)
        if ref["gt"].shape != gt.shape:
            raise SystemExit("reference npz was collected on a different anchor set")
        combined["step0"] = metrics(ref["pred"], gt, pad, state, std, active)
        report["step0_reference"] = combined["step0"]

    result = verdict(model, combined, args.min_improvement, args.static_tolerance)
    report["verdict"] = result
    report["status"] = "passed" if result["passed"] else "not_passed"
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({
        "status": report["status"],
        "candidate": {"scaled_active": round(model["mae_scaled_active"], 4),
                      "raw_all": round(model["mae_raw_all"], 3)},
        "baselines": {k: {"scaled_active": round(v["mae_scaled_active"], 4),
                          "raw_all": round(v["mae_raw_all"], 3)}
                      for k, v in combined.items()},
        "verdict": {k: result[k] for k in
                    ("checks", "improvement_active_scaled", "improvement_all_raw",
                     "static_ratio_vs_best")},
    }, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
