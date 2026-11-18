"""The text-context cache must be used by the *training* path, not only inference.

Measured on g128: with `text_encoder_device='cpu'`, `lerobot_train` spent
47.6 s per update, of which the model step was the whole thing because UMT5-XXL
ran a full forward on CPU for every batch. The policy already cached the prompt
for inference (`_encode_prompt_cached`) but not for training.

These tests pin the behaviour with a stub encoder — they are tiny/fake, and what
they prove is *call counting and object reuse*, not embedding quality:

  * the same prompt encodes once, and the second step gets the exact same
    tensors (identity, not just equality);
  * a different prompt re-encodes;
  * `_batch_to_training_sample` goes through the cache (so a future edit cannot
    quietly restore the per-step encode).
"""
from __future__ import annotations

import types
from types import SimpleNamespace

import torch

from lerobot.policies.fastwam.modeling_fastwam import FastWAMPolicy


class _StubModel:
    """Stands in for `FastWAM`: counts encodes and returns distinguishable tensors."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def encode_prompt(self, prompt):
        self.calls.append(prompt)
        index = float(len(self.calls))
        return (
            torch.full((1, 2, 3), index),
            torch.ones(1, 2, dtype=torch.bool),
        )


def _fake_policy(model: _StubModel) -> SimpleNamespace:
    fake = SimpleNamespace(
        model=model,
        config=SimpleNamespace(proprio_dim=None, prompt_template="{task}"),
        _cached_prompt=None,
        _cached_context=None,
        _cached_context_mask=None,
    )
    fake._encode_prompt_cached = types.MethodType(FastWAMPolicy._encode_prompt_cached, fake)
    return fake


def test_same_prompt_encodes_once_and_reuses_the_tensors():
    model = _StubModel()
    fake = _fake_policy(model)

    first_ctx, first_mask = fake._encode_prompt_cached("pick the red cube")
    second_ctx, second_mask = fake._encode_prompt_cached("pick the red cube")

    assert len(model.calls) == 1, model.calls
    assert first_ctx is second_ctx and first_mask is second_mask


def test_different_prompt_re_encodes():
    model = _StubModel()
    fake = _fake_policy(model)

    first_ctx, _ = fake._encode_prompt_cached("pick the red cube")
    other_ctx, _ = fake._encode_prompt_cached("push the blue block")

    assert len(model.calls) == 2
    assert not torch.equal(first_ctx, other_ctx)


def test_training_sample_uses_the_cache():
    """The regression this file exists for: two batches, one encode."""
    model = _StubModel()
    fake = _fake_policy(model)
    video = torch.zeros(1, 3, 9, 3, 8, 8)          # [B, C, T, H, W] already stacked
    batch = {"video": video, "task": ["pick the red cube"]}

    out_a = FastWAMPolicy._batch_to_training_sample(fake, batch)
    out_b = FastWAMPolicy._batch_to_training_sample(fake, batch)

    assert len(model.calls) == 1, model.calls
    assert out_a["context"] is out_b["context"]
    assert out_a["context"].shape == (1, 2, 3)


def test_proprio_gets_a_frame_axis_without_touching_the_cache():
    model = _StubModel()
    fake = _fake_policy(model)
    fake.config.proprio_dim = 12          # proprio is only attached when configured
    batch = {
        "video": torch.zeros(1, 3, 9, 3, 8, 8),
        "task": ["pick the red cube"],
        "observation.state": torch.zeros(1, 12),
    }
    out = FastWAMPolicy._batch_to_training_sample(fake, batch)
    assert tuple(out["proprio"].shape) == (1, 1, 12)
    assert len(model.calls) == 1
