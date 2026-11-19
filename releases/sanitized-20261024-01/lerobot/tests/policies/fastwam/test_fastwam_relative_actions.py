"""FW-REL-01 / FW-REL-02: the relative-action contract, on CPU and without weights.

These pin the properties the B arm depends on. Every one of them is a silent
failure mode if it breaks: a wrong mask turns the grippers relative, a lost
anchor shifts the whole chunk, and a normalisation mix-up makes "zero residual"
mean something other than "hold the current pose".

The dimension names are the real ones from
`local/so101_scene1_left_red_v1` (`meta/info.json`), because the exclusion mask
is built by substring match on those names.
"""
from __future__ import annotations

import pytest
import torch

from lerobot.lerobot_types import PolicyAction, TransitionKey
from lerobot.policies.fastwam import FastWAMConfig
from lerobot.policies.fastwam.processor_fastwam import make_fastwam_pre_post_processors
from lerobot.processor.relative_action_processor import (
    AbsoluteActionsProcessorStep,
    RelativeActionsProcessorStep,
)

ACTION_NAMES = [
    "left_shoulder_pan.pos", "left_shoulder_lift.pos", "left_elbow_flex.pos",
    "left_wrist_flex.pos", "left_wrist_roll.pos", "left_gripper.pos",
    "right_shoulder_pan.pos", "right_shoulder_lift.pos", "right_elbow_flex.pos",
    "right_wrist_flex.pos", "right_wrist_roll.pos", "right_gripper.pos",
]
HORIZON = 32
DIM = 12


def _stats() -> dict:
    mean = torch.arange(DIM, dtype=torch.float32) * 3.0
    std = torch.arange(DIM, dtype=torch.float32) * 0.5 + 1.0
    return {
        "observation.state": {"mean": mean.clone(), "std": std.clone(), "min": -torch.ones(DIM),
                              "max": torch.ones(DIM), "count": torch.ones(1)},
        "action": {"mean": mean.clone(), "std": std.clone(), "min": -torch.ones(DIM),
                   "max": torch.ones(DIM), "count": torch.ones(1)},
    }


def _cfg(**overrides) -> FastWAMConfig:
    kwargs = dict(
        action_dim=DIM,
        proprio_dim=DIM,
        image_size=(224, 672),
        dtype=torch.float32,
        device="cpu",
        action_horizon=HORIZON,
        n_action_steps=HORIZON,
        use_relative_actions=True,
        relative_exclude_joints=["gripper"],
        action_feature_names=list(ACTION_NAMES),
    )
    kwargs.update(overrides)
    return FastWAMConfig(**kwargs)


def _sample(state: torch.Tensor, action: torch.Tensor) -> dict:
    return {
        "observation.state": state.clone(),
        "action": action.clone(),
        "task": "pick the red cube",
        "observation.images.image": torch.rand(3, 8, 8),
    }


def _abs_action(state: torch.Tensor, steps: int = HORIZON) -> torch.Tensor:
    """A plausible absolute chunk: starts at the state and drifts."""
    offsets = torch.linspace(0, 5.0, steps).unsqueeze(-1)
    return state.unsqueeze(0) + offsets


def test_round_trip_restores_the_absolute_action():
    cfg = _cfg()
    pre, post = make_fastwam_pre_post_processors(cfg, dataset_stats=_stats())
    state = torch.arange(DIM, dtype=torch.float32) * 2.0
    raw = _abs_action(state)

    processed = pre(_sample(state, raw))
    restored = post(PolicyAction(processed["action"].float()))

    assert torch.allclose(restored.float(), raw.float(), atol=1e-3), (
        (restored.float() - raw.float()).abs().max()
    )


def test_zero_physical_residual_maps_to_the_anchor_state():
    """A predicted *physical* zero offset must come back as the current pose.

    Note this is not the same as feeding zeros in normalised space: the normaliser
    subtracts `mean`, so normalised zero is a small non-zero physical offset.
    """
    cfg = _cfg()
    pre, post = make_fastwam_pre_post_processors(cfg, dataset_stats=_stats())
    stats = _stats()
    state = torch.full((DIM,), 7.0)
    pre(_sample(state, _abs_action(state)))          # caches the anchor

    mean = stats["action"]["mean"]
    std = stats["action"]["std"]

    # Physical zero residual on the joint dims; grippers stay absolute, so their
    # residual is "keep the observed gripper value".
    zero_residual = torch.zeros(HORIZON, DIM)
    zero_residual[:, [5, 11]] = state[[5, 11]]
    normalised = (zero_residual - mean) / std

    restored = post(PolicyAction(normalised.unsqueeze(0).float()))
    expected = state.unsqueeze(0).expand(HORIZON, DIM)
    assert torch.allclose(restored.float(), expected.float(), atol=1e-3)


def test_gripper_dimensions_are_not_converted():
    """`relative_exclude_joints=["gripper"]` must leave dims 5 and 11 absolute."""
    step = RelativeActionsProcessorStep(
        enabled=True, exclude_joints=["gripper"], action_names=list(ACTION_NAMES)
    )
    state = torch.zeros(1, DIM)
    action = torch.arange(HORIZON * DIM, dtype=torch.float32).reshape(1, HORIZON, DIM)
    out = step({
        TransitionKey.OBSERVATION: {"observation.state": state},
        TransitionKey.ACTION: action.clone(),
    })
    converted = out[TransitionKey.ACTION]
    joints = [0, 1, 2, 3, 4, 6, 7, 8, 9, 10]
    assert torch.allclose(converted[:, :, joints], action[:, :, joints] - state[:, None, joints])
    assert torch.equal(converted[:, :, [5, 11]], action[:, :, [5, 11]]), "gripper was made relative"


def test_anchor_is_held_while_the_chunk_is_in_flight():
    step = RelativeActionsProcessorStep(
        enabled=True, exclude_joints=["gripper"], action_names=list(ACTION_NAMES)
    )
    queue = {"n": 0}
    step.bind_action_queue(lambda: queue["n"])

    first = torch.zeros(1, DIM)
    step({TransitionKey.OBSERVATION: {"observation.state": first.clone()},
          TransitionKey.ACTION: torch.zeros(1, HORIZON, DIM)})
    anchor_a = step.get_cached_state().clone()

    queue["n"] = 5                                   # chunk still being served
    moved = torch.full((1, DIM), 50.0)
    step({TransitionKey.OBSERVATION: {"observation.state": moved.clone()},
          TransitionKey.ACTION: torch.zeros(1, HORIZON, DIM)})
    assert torch.equal(step.get_cached_state(), anchor_a), "anchor moved mid-chunk"

    queue["n"] = 0                                   # chunk drained
    step({TransitionKey.OBSERVATION: {"observation.state": moved.clone()},
          TransitionKey.ACTION: torch.zeros(1, HORIZON, DIM)})
    assert torch.equal(step.get_cached_state(), moved), "anchor did not re-anchor"


def test_reset_clears_the_anchor_and_it_is_re_taken():
    step = RelativeActionsProcessorStep(
        enabled=True, exclude_joints=["gripper"], action_names=list(ACTION_NAMES)
    )
    first = torch.ones(1, DIM)
    step({TransitionKey.OBSERVATION: {"observation.state": first.clone()},
          TransitionKey.ACTION: torch.zeros(1, HORIZON, DIM)})
    assert step.get_cached_state() is not None

    step.reset()
    assert step.get_cached_state() is None

    second = torch.full((1, DIM), 3.0)
    step({TransitionKey.OBSERVATION: {"observation.state": second.clone()},
          TransitionKey.ACTION: torch.zeros(1, HORIZON, DIM)})
    assert torch.equal(step.get_cached_state(), second)


def test_missing_action_names_fails_instead_of_converting_everything():
    cfg = _cfg(action_feature_names=None)
    with pytest.raises(ValueError, match="action_feature_names"):
        make_fastwam_pre_post_processors(cfg, dataset_stats=_stats())


def test_absolute_step_without_a_cached_anchor_fails():
    step = AbsoluteActionsProcessorStep(enabled=True, relative_step=RelativeActionsProcessorStep(
        enabled=True, exclude_joints=["gripper"], action_names=list(ACTION_NAMES)))
    with pytest.raises(RuntimeError, match="cached"):
        step({TransitionKey.ACTION: torch.zeros(1, HORIZON, DIM)})


def test_disabled_relative_actions_leave_actions_untouched():
    cfg = _cfg(use_relative_actions=False)
    pre, post = make_fastwam_pre_post_processors(cfg, dataset_stats=_stats())
    state = torch.zeros(DIM)
    raw = _abs_action(state)
    processed = pre(_sample(state, raw))
    restored = post(PolicyAction(processed["action"].float()))
    assert torch.allclose(restored.float(), raw.float(), atol=1e-3)
