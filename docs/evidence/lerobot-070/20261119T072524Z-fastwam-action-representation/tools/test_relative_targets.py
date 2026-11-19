"""FW-REL-01 (end-to-end): arm B's *training target* is what we think it is.

The unit tests pin the processor in isolation. This one pins the numbers a
training step would actually see: take a real 32-step window from the train
split, push it through the same preprocessor arm B uses (relative action step +
train-only normaliser), and compare against the relative target computed by hand
from the dataset.

If this drifts, arm B trains on a different quantity than the one being
measured, and nothing in the loss would reveal it.

Run: python -m pytest test_relative_targets.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import torch

DATA_ROOT = Path("/tmp/so101-lr")
RUN = Path(__file__).resolve().parent.parent
REL_STATS = RUN / "relative_stats.json"
pytestmark = pytest.mark.skipif(
    not (DATA_ROOT / "data/chunk-000/file-000.parquet").exists()
    or not REL_STATS.exists(),
    reason="local parquet copy or relative stats not present",
)

HORIZON = 32


def _window() -> tuple[np.ndarray, np.ndarray, dict]:
    import pyarrow.parquet as pq

    stats = json.loads(REL_STATS.read_text())
    names = stats["feature_names"]
    table = pq.read_table(DATA_ROOT / "data/chunk-000/file-000.parquet",
                          columns=["action", "observation.state", "episode_index"])
    flat_a = table["action"].combine_chunks().flatten().to_numpy(zero_copy_only=False)
    flat_s = table["observation.state"].combine_chunks().flatten().to_numpy(zero_copy_only=False)
    width = len(names)
    actions = np.asarray(flat_a, dtype=np.float64).reshape(-1, width)
    states = np.asarray(flat_s, dtype=np.float64).reshape(-1, width)
    eps = np.asarray(table["episode_index"].to_numpy())
    # First fully intra-episode window of the first train episode.
    start = 0
    while eps[start + HORIZON - 1] != eps[start]:
        start += 1
    return actions[start:start + HORIZON], states[start], stats


def test_relative_training_target_matches_the_hand_computation():
    from lerobot.policies.fastwam import FastWAMConfig
    from lerobot.policies.fastwam.processor_fastwam import make_fastwam_pre_post_processors

    action, state, stats = _window()
    names = stats["feature_names"]
    mean = np.asarray(stats["lib"]["mean"], dtype=np.float64)
    std = np.asarray(stats["lib"]["std"], dtype=np.float64)
    excluded = set(stats["excluded_dimensions"])

    # Hand computation in RAW units: joint dims relative to the anchor, grippers
    # absolute. Compare raw, not normalised: a static dimension has std ~0.09, so
    # a 1e-3 normalised mismatch there is only 1e-4 degrees and float32 rounding.
    expected_raw = action.copy()
    joint_dims = [d for d in range(len(names)) if d not in excluded]
    expected_raw[:, joint_dims] -= state[joint_dims]

    cfg = FastWAMConfig(
        action_dim=len(names), proprio_dim=len(names), image_size=(224, 672),
        action_horizon=HORIZON, n_action_steps=HORIZON, dtype=torch.float32, device="cpu",
        use_relative_actions=True, relative_exclude_joints=["gripper"],
        action_feature_names=list(names),
    )
    dataset_stats = {
        "observation.state": {
            "mean": torch.tensor(mean, dtype=torch.float32),
            "std": torch.tensor(std, dtype=torch.float32),
            "min": torch.tensor(mean - 3 * std, dtype=torch.float32),
            "max": torch.tensor(mean + 3 * std, dtype=torch.float32),
            "count": torch.ones(1),
        },
        "action": {
            "mean": torch.tensor(mean, dtype=torch.float32),
            "std": torch.tensor(std, dtype=torch.float32),
            "min": torch.tensor(np.asarray(stats["lib"]["min"]), dtype=torch.float32),
            "max": torch.tensor(np.asarray(stats["lib"]["max"]), dtype=torch.float32),
            "count": torch.ones(1),
        },
    }
    pre, post = make_fastwam_pre_post_processors(cfg, dataset_stats=dataset_stats)

    sample = {
        "observation.state": torch.tensor(state, dtype=torch.float32),
        "action": torch.tensor(action, dtype=torch.float32),
        "task": "pick the red cube",
        "observation.images.image": torch.rand(3, 8, 8),
    }
    processed = pre(dict(sample))
    # A single (unbatched) sample keeps the action unbatched: the batch-dim step
    # only batches observation entries, and `action - state` broadcasts.
    got = processed["action"].float().numpy()
    assert got.shape == expected_raw.shape, (got.shape, expected_raw.shape)

    got_raw = got.astype(np.float64) * std + mean
    np.testing.assert_allclose(got_raw, expected_raw, atol=1e-4)


def test_relative_target_starts_near_zero_for_joints():
    """Sanity: at t=0 the joint residual is action[0]-state[0] ~ 0.2 deg, i.e. tiny
    in normalised units, which is what makes the first step learnable at all."""
    from lerobot.policies.fastwam import FastWAMConfig
    from lerobot.policies.fastwam.processor_fastwam import make_fastwam_pre_post_processors

    action, state, stats = _window()
    names = stats["feature_names"]
    mean = np.asarray(stats["lib"]["mean"], dtype=np.float64)
    std = np.asarray(stats["lib"]["std"], dtype=np.float64)
    cfg = FastWAMConfig(
        action_dim=len(names), proprio_dim=len(names), image_size=(224, 672),
        action_horizon=HORIZON, n_action_steps=HORIZON, dtype=torch.float32, device="cpu",
        use_relative_actions=True, relative_exclude_joints=["gripper"],
        action_feature_names=list(names),
    )
    dataset_stats = {
        key: {"mean": torch.tensor(mean, dtype=torch.float32),
              "std": torch.tensor(std, dtype=torch.float32),
              "min": torch.tensor(mean - 3 * std, dtype=torch.float32),
              "max": torch.tensor(mean + 3 * std, dtype=torch.float32),
              "count": torch.ones(1)}
        for key in ("observation.state", "action")
    }
    pre, _ = make_fastwam_pre_post_processors(cfg, dataset_stats=dataset_stats)
    processed = pre({
        "observation.state": torch.tensor(state, dtype=torch.float32),
        "action": torch.tensor(action, dtype=torch.float32),
        "task": "pick",
        "observation.images.image": torch.rand(3, 8, 8),
    })
    first = processed["action"].float().numpy()[0, [0, 1, 2, 3, 4]]
    # Normalised residual at the anchor frame must be small (|action[0]-state| = 0.2 deg,
    # std 4.5-12.6 deg), while the absolute target would be ~ -4 in the same units.
    assert np.all(np.abs(first) < 0.5), first
