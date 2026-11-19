"""CPU regressions for `score_v2.py` (FW-EVAL-01).

These use synthetic arrays only. They pin the three things that were wrong or
unproven in round 1:

  * pairing is by `(episode, anchor)` — a reordered reference still aligns, a
    duplicate or missing key is rejected, and a label that disagrees is rejected
    instead of being silently compared;
  * aggregation is per episode — an episode with five windows cannot outvote one
    with a single window;
  * padded steps are excluded without hiding a fully-padded window.

Run: python -m pytest test_score_v2.py -q
"""
from __future__ import annotations

import numpy as np
import pytest

import score_v2 as sv


def _dataset(episodes_windows: dict[int, int], T: int = 8, D: int = 12, seed: int = 0):
    """Build npz-shaped arrays: `episodes_windows[ep] = number of anchors`."""
    rng = np.random.default_rng(seed)
    ep, anchor, gt = [], [], []
    for episode, count in episodes_windows.items():
        for k in range(count):
            ep.append(episode)
            anchor.append(k * 10)
            gt.append(rng.normal(size=(T, D)))
    gt = np.stack(gt)
    return {
        "pred": gt.copy(),
        "gt": gt,
        "pad": np.zeros(gt.shape[:2], dtype=bool),
        "state": gt[:, 0, :].copy(),
        "episode": np.asarray(ep),
        "anchor": np.asarray(anchor),
    }


def test_reordered_reference_is_aligned_not_rejected():
    base = _dataset({0: 2, 1: 1})
    shuffled = {k: v[::-1].copy() for k, v in base.items()}
    report = sv.validate_pairing(base, shuffled)
    assert report["reference_aligned"] is True
    # Same key set, different order: metrics must still be identical.
    keys = sv.keys_of(base)
    order = {k: i for i, k in enumerate(sv.keys_of(shuffled))}
    idx = np.asarray([order[k] for k in keys])
    assert np.array_equal(shuffled["gt"][idx], base["gt"])


def test_duplicate_keys_rejected():
    base = _dataset({0: 2})
    dup = {k: (np.concatenate([v, v[:1]]) if k in ("gt", "pad", "state") else
               np.concatenate([v, v[:1]]) if k == "pred" else
               np.concatenate([v, v[:1]])) for k, v in base.items()}
    with pytest.raises(SystemExit, match="duplicate"):
        sv.validate_pairing(dup, None)


def test_missing_key_rejected():
    cand = _dataset({0: 2, 1: 1})
    ref = _dataset({0: 2, 1: 1})
    drop = np.arange(len(ref["episode"])) != 0
    ref = {k: v[drop] for k, v in ref.items()}
    with pytest.raises(SystemExit, match="key mismatch"):
        sv.validate_pairing(cand, ref)


def test_label_mismatch_rejected():
    cand = _dataset({0: 1})
    ref = _dataset({0: 1})
    ref["gt"] = ref["gt"] + 1.0
    with pytest.raises(SystemExit, match="gt differs"):
        sv.validate_pairing(cand, ref)


def test_state_mismatch_rejected():
    cand = _dataset({0: 1})
    ref = _dataset({0: 1})
    ref["state"] = ref["state"] + 0.5
    with pytest.raises(SystemExit, match="state differs"):
        sv.validate_pairing(cand, ref)


def test_episode_equal_beats_window_equal_on_unbalanced_windows():
    """Episode 0 has 5 windows with a small error, episode 1 has 1 window with a
    large error. Window-averaging lets episode 0 dominate; episode-equal must
    not."""
    data = _dataset({0: 5, 1: 1})
    pred = data["gt"].copy()
    is_ep0 = data["episode"] == 0
    pred[is_ep0] += 1.0
    pred[~is_ep0] += 9.0
    keep = ~data["pad"]
    dims = sv.LEFT_ARM_JOINTS

    episode_equal = sv.group_mae(pred, data["gt"], keep, dims, (0, 8), data["episode"])["mae"]
    window_equal = sv.window_equal_mae(pred, data["gt"], keep, dims, (0, 8))

    # Mean |N(0,1)| = sqrt(2/pi) ~= 0.7979; errors are |1 + e| and |9 + e|.
    expected_episode_equal = 0.5 * (1.0 + 9.0)      # exactly two episodes, equal weight
    expected_window_equal = (5 * 1.0 + 1 * 9.0) / 6
    assert episode_equal == pytest.approx(expected_episode_equal, abs=0.05)
    assert window_equal == pytest.approx(expected_window_equal, abs=0.05)
    assert abs(episode_equal - window_equal) > 1.0


def test_padding_is_excluded_and_counted():
    data = _dataset({0: 1})
    data["pad"][0, 4:] = True                     # half the window is padding
    pred = data["gt"].copy()
    pred[0, 4:] += 1000.0                         # garbage where it should be ignored
    m = sv.group_mae(pred, data["gt"], ~data["pad"], sv.GROUPS["left_arm_joints"], (0, 8),
                     data["episode"])
    assert m["steps"] == 4
    assert m["mae"] < 1e-9


def test_fully_padded_window_is_skipped_not_zeroed():
    data = _dataset({0: 1, 1: 1})
    data["pad"][0, :] = True
    pred = data["gt"].copy()
    pred[0] += 500.0
    m = sv.group_mae(pred, data["gt"], ~data["pad"], sv.GROUPS["left_arm_joints"], (0, 8),
                     data["episode"])
    assert m["episodes"] == 1
    assert m["mae"] < 1e-9


def test_segments_slice_the_right_steps():
    data = _dataset({0: 1}, T=32)
    pred = data["gt"].copy()
    pred[0, 0] += 10.0                            # only the first step is wrong
    keep = ~data["pad"]
    eps = data["episode"]
    first = sv.group_mae(pred, data["gt"], keep, sv.LEFT_ARM_JOINTS, sv.SEGMENTS["first"], eps)
    late = sv.group_mae(pred, data["gt"], keep, sv.LEFT_ARM_JOINTS, sv.SEGMENTS["late"], eps)
    full = sv.group_mae(pred, data["gt"], keep, sv.LEFT_ARM_JOINTS, sv.SEGMENTS["full"], eps)
    assert first["steps"] == 1 and first["mae"] == pytest.approx(10.0, abs=1e-6)
    assert late["steps"] == 16 and late["mae"] < 1e-9
    assert full["steps"] == 32 and full["mae"] == pytest.approx(10.0 / 32, abs=1e-6)


def test_groups_keep_units_apart():
    data = _dataset({0: 1})
    pred = data["gt"].copy()
    pred[0] += 3.0                                # equal error in every dimension
    keep = ~data["pad"]
    eps = data["episode"]
    joints = sv.group_mae(pred, data["gt"], keep, sv.LEFT_ARM_JOINTS, (0, 8), eps)
    grip = sv.group_mae(pred, data["gt"], keep, sv.LEFT_GRIPPER, (0, 8), eps)
    right = sv.group_mae(pred, data["gt"], keep, sv.RIGHT_ARM_JOINTS, (0, 8), eps)
    assert joints["mae"] == pytest.approx(3.0, abs=1e-6)
    assert grip["mae"] == pytest.approx(3.0, abs=1e-6)
    assert right["mae"] == pytest.approx(3.0, abs=1e-6)
    # ... and the group definitions really are disjoint and cover all 12 dims.
    covered = set().union(*(set(d) for d in sv.GROUPS.values()))
    assert covered == set(range(12))


def test_window_labels_follow_the_frozen_thresholds():
    gt = np.zeros((3, 32, 12))
    gt[0, :, 0] = 0.0                             # static
    gt[1, :, 0] = np.linspace(0, 6.0, 32)         # joint-dynamic (>= 5 deg)
    gt[1, :, 5] = np.linspace(0, 1.0, 32)         # gripper static (< 5 pct)
    gt[2, :, 0] = np.linspace(0, 1.0, 32)         # joint static
    gt[2, :, 5] = np.linspace(0, 9.0, 32)         # gripper-dynamic (>= 5 pct)
    windows = {"thresholds": {"left_joint_displacement_deg": 5.0,
                              "left_gripper_displacement_pct": 5.0}}
    labels = sv.label_windows([(0, 0), (1, 0), (2, 0)], gt, windows)
    assert labels["joint_dynamic"] == 1
    assert labels["gripper_dynamic"] == 1
    assert labels["windows"][1]["joint_dynamic"] is True
    assert labels["windows"][1]["gripper_dynamic"] is False
    assert labels["windows"][2]["joint_dynamic"] is False
    assert labels["windows"][2]["gripper_dynamic"] is True
