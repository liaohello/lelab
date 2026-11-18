"""Tiny/fake regression tests for `score_predictions.py`.

These are NOT model results. They exist so that a scoring bug cannot turn a
random head into a "pass", which is the failure mode this round must rule out:

  * a candidate equal to the hold-pose baseline must not pass;
  * a candidate that is perfect on the six nearly-static channels while merely
    predicting the train mean on the six active channels must not pass;
  * a candidate with a NaN must not pass;
  * dimension/shape mismatches must raise instead of being broadcast;
  * a fully padded timestep must read NaN, not a 0 error.

Run:  python -m pytest test_score_logic.py -q
"""
from __future__ import annotations

import numpy as np
import pytest

import score_predictions as sp

T, D, N = 32, 12, 4
ACTIVE = np.array([i < 6 for i in range(D)])
STD = np.where(ACTIVE, 10.0, 0.05)          # degrees: left arm moves, right arm is still
MEAN = np.where(ACTIVE, 5.0, 0.0)


def _fixture():
    t = np.arange(T, dtype=np.float64)
    gt = np.zeros((N, T, D))
    for i in range(N):
        for d in range(6):
            gt[i, :, d] = MEAN[d] + STD[d] * 0.5 * np.sin(2 * np.pi * (t + 3 * i) / T + d)
    state = gt[:, 0, :].copy()
    pad = np.zeros((N, T), dtype=bool)
    pad[0, -2:] = True                       # tail padding must be excluded
    return gt, pad, state


def test_perfect_prediction_passes_and_excludes_padded_steps():
    gt, pad, state = _fixture()
    model = sp.metrics(gt.copy(), gt, pad, state, STD, ACTIVE)
    result = sp.verdict(model, sp.baselines(gt, pad, state, MEAN, STD, ACTIVE), 0.25)
    assert result["passed"], result
    assert result["improvement_active_scaled"] > 0.99
    assert model["steps_evaluated"] == N * T - 2
    assert model["mae_raw_all"] == pytest.approx(0.0)


def test_hold_pose_candidate_cannot_pass():
    gt, pad, state = _fixture()
    copy = np.broadcast_to(state[:, None, :], gt.shape).copy()
    model = sp.metrics(copy, gt, pad, state, STD, ACTIVE)
    base = sp.baselines(gt, pad, state, MEAN, STD, ACTIVE)
    result = sp.verdict(model, base, 0.25)
    assert not result["passed"]
    # The candidate *is* the state_copy baseline, so it cannot improve on the best.
    assert result["improvement_active_scaled"] <= 0.0
    assert model["mae_scaled_active"] == base["state_copy"]["mae_scaled_active"]


def test_static_channels_only_do_not_pass_the_active_gate():
    """Exact on the six still channels, train mean on the six moving ones."""
    gt, pad, state = _fixture()
    pred = np.broadcast_to(MEAN.reshape(1, 1, -1), gt.shape).copy()
    pred[:, :, 6:] = gt[:, :, 6:]
    model = sp.metrics(pred, gt, pad, state, STD, ACTIVE)
    result = sp.verdict(model, sp.baselines(gt, pad, state, MEAN, STD, ACTIVE), 0.25)
    assert not result["passed"], result
    assert not result["checks"]["beats_best_baseline_active_scaled"]


def test_verdict_gates_are_independent():
    """Injected metrics: wins the scaled gate, loses the raw one."""
    model = {"finite": True, "mae_scaled_active": 0.2, "mae_raw_all": 9.0, "mae_raw_static": 0.1}
    base = {k: {"finite": True, "mae_scaled_active": 0.5, "mae_raw_all": 1.0, "mae_raw_static": 0.1}
            for k in ("zero", "train_mean", "state_copy")}
    result = sp.verdict(model, base, 0.25)
    assert result["checks"]["beats_best_baseline_active_scaled"] is True
    assert result["checks"]["beats_best_baseline_all_raw"] is False
    assert not result["passed"]


def test_wrecking_static_dims_is_rejected():
    model = {"finite": True, "mae_scaled_active": 0.1, "mae_raw_all": 0.1, "mae_raw_static": 5.0}
    base = {k: {"finite": True, "mae_scaled_active": 0.5, "mae_raw_all": 1.0, "mae_raw_static": 0.2}
            for k in ("zero", "train_mean", "state_copy")}
    result = sp.verdict(model, base, 0.25)
    assert result["checks"]["static_dims_not_degraded"] is False
    assert not result["passed"]


def test_nan_candidate_fails_instead_of_scoring_low():
    gt, pad, state = _fixture()
    pred = gt.copy()
    pred[1, 4, 2] = np.nan
    model = sp.metrics(pred, gt, pad, state, STD, ACTIVE)
    result = sp.verdict(model, sp.baselines(gt, pad, state, MEAN, STD, ACTIVE), 0.25)
    assert model["finite"] is False
    assert not result["passed"]


def test_shape_mismatch_raises():
    gt, pad, state = _fixture()
    with pytest.raises(ValueError):
        sp.metrics(gt[:, :-1], gt, pad, state, STD, ACTIVE)
    with pytest.raises(ValueError):
        sp.metrics(gt, gt, pad[:, :-1], state, STD, ACTIVE)
    with pytest.raises(ValueError):
        sp.metrics(gt, gt, pad, state, STD[:-1], ACTIVE)


def test_fully_padded_slice_is_nan_not_zero():
    gt, pad, state = _fixture()
    pad[:, 0] = True
    model = sp.metrics(gt.copy(), gt, pad, state, STD, ACTIVE)
    assert np.isnan(model["first_step_mae_raw"])
    assert np.isfinite(model["mae_scaled_active"])


def test_zero_std_dimension_is_excluded_from_the_scaled_metric():
    """dim 6/10 in the real data have std exactly 0; scaling must not divide by it."""
    gt, pad, state = _fixture()
    std = STD.copy()
    std[6] = 0.0
    std[10] = 0.0
    model = sp.metrics(gt.copy(), gt, pad, state, std, std >= 1.0)
    assert np.isfinite(model["mae_scaled_active"])
    assert 6 not in model["mae_scaled_active_per_dim"]
    assert 10 not in model["mae_scaled_active_per_dim"]
