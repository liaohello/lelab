#!/usr/bin/env python

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""LoRA defaults for FastWAM: the target regex must hit the action expert only.

`wrap_with_peft` refuses to build a config when a policy defines no default
`target_modules`, so without these defaults `--policy.use_peft=true` cannot even
start. The regex and the `modules_to_save` list are the contract; these tests pin
them against parameter names taken from `lerobot/fastwam_base`, and pin the
optimizer rule that must include `modules_to_save` weights living outside the DiT.
"""

import re

import pytest
import torch
from torch import nn

from lerobot.policies.fastwam.modeling_fastwam import FastWAMPolicy

# Live module paths. `MoT` re-parents each expert block into
# `mot.layers[i].blocks[<expert>]`, so the checkpoint spelling
# `mot.mixtures.action.blocks.<i>` is not a module that exists at runtime and
# targeting it raises NoMatchingPeftModuleError.
ACTION_MODULES = [
    "model.mot.layers.0.blocks.action.self_attn.q",
    "model.mot.layers.0.blocks.action.self_attn.v",
    "model.mot.layers.11.blocks.action.cross_attn.q",
    "model.mot.layers.11.blocks.action.cross_attn.v",
]
NON_TARGET_MODULES = [
    "model.mot.layers.0.blocks.video.self_attn.q",
    "model.mot.layers.0.blocks.action.self_attn.k",
    "model.mot.layers.0.blocks.action.self_attn.o",
    "model.mot.layers.0.blocks.action.ffn.0",
    "model.mot.layers.0.blocks.action.norm3",
    # The safetensors spelling: a real key in the file, but no such live module.
    "model.mot.mixtures.action.blocks.0.self_attn.q",
]


def _defaults() -> dict:
    # `_get_default_peft_targets` reads no instance state; calling it unbound keeps
    # this test free of a 12 GB checkpoint and of transformers/diffusers downloads.
    return FastWAMPolicy._get_default_peft_targets(None)


def test_default_targets_cover_the_action_expert():
    pattern = re.compile(_defaults()["target_modules"])
    for name in ACTION_MODULES:
        assert pattern.fullmatch(name), f"LoRA would not adapt {name}"
    for name in NON_TARGET_MODULES:
        assert not pattern.fullmatch(name), f"LoRA would waste rank on {name}"


def test_checkpoint_spelling_is_not_a_target():
    """The file says `mot.mixtures.action.blocks.N`; the live tree says
    `mot.layers.N.blocks.action`. Targeting the former matches nothing."""
    pattern = re.compile(_defaults()["target_modules"])
    assert not pattern.fullmatch("model.mot.mixtures.action.blocks.0.self_attn.q")


def test_video_expert_is_never_a_target():
    """Adapting the frozen ~5B video expert would erase the memory saving."""
    pattern = re.compile(_defaults()["target_modules"])
    assert not any(pattern.fullmatch(n) for n in NON_TARGET_MODULES if ".blocks.video." in n)


def test_modules_to_save_are_the_layers_absent_from_the_base_checkpoint():
    saved = _defaults()["modules_to_save"]
    assert saved == [
        "model.mot.mixtures.action.action_encoder",
        "model.mot.mixtures.action.head",
        "model.proprio_encoder",
    ]
    # These are exactly the names the loader reports as missing for fastwam_base;
    # they must be trained in full, otherwise they stay at their random init.
    assert all(name.startswith(("model.mot.", "model.proprio")) for name in saved)


class _FakePolicy(nn.Module):
    """Minimal graph: DiT-like block plus a `modules_to_save`-like head outside it."""

    def __init__(self) -> None:
        super().__init__()
        self.dit = nn.Linear(4, 4)
        self.head = nn.Linear(4, 2)
        self.frozen = nn.Linear(2, 2)
        self.frozen.requires_grad_(False)


def test_optim_params_include_trainables_outside_the_dit():
    """PEFT saves the action head/encoder outside `model.dit`; dropping them would
    leave those tensors frozen at their random init while the loss still depends on
    them."""
    fake = _FakePolicy()
    selected = FastWAMPolicy.get_optim_params(fake)
    ids = {id(p) for p in selected}
    assert id(fake.dit.weight) in ids
    assert id(fake.head.weight) in ids, "trainable head outside the DiT was dropped"
    assert id(fake.frozen.weight) not in ids, "frozen parameter must not reach the optimizer"
    assert len(selected) == 4  # dit.weight, dit.bias, head.weight, head.bias


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
