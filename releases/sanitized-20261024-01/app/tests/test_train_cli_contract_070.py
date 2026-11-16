# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
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
"""LeLab -> LeRobot 0.7.0 CLI contract (TC-CLI-01/02/03, TC-ENV-03, TC-POL-01).

These tests do not grep the argv. They hand the generated argv to the real
`TrainPipelineConfig` parser shipped with the installed LeRobot, so a flag that
0.7.0 no longer declares fails here instead of on a training host.
"""

from __future__ import annotations

import pytest

# The config choice registry is filled by importing the policies package. The
# real entry point (`lerobot.scripts.lerobot_train`) does the same before it
# parses, so importing it here is what production does, not a test shortcut.
pytest.importorskip("lerobot.policies", reason="lerobot must be importable")

import draccus  # noqa: E402
from lerobot.configs.default import DatasetConfig  # noqa: E402
from lerobot.configs.train import TrainPipelineConfig  # noqa: E402
from lerobot.utils.constants import ACTION  # noqa: E402

from lelab.train import (  # noqa: E402
    TrainingRequest,
    build_training_command,
    unmapped_tracking_fields,
    unsupported_training_fields,
)

_FASTWAM_DIMS = {"policy_action_dim": 6, "policy_proprio_dim": 6}


def _parse(request: TrainingRequest, tmp_path) -> tuple[TrainPipelineConfig, list[str]]:
    argv = build_training_command(request, str(tmp_path))[3:]
    return draccus.parse(config_class=TrainPipelineConfig, args=argv), argv


@pytest.mark.parametrize("policy_type", ["act", "smolvla", "fastwam", "fineart_vla", "pi05"])
def test_generated_argv_parses_with_the_real_070_config(policy_type, tmp_path):
    kwargs = dict(_FASTWAM_DIMS) if policy_type == "fastwam" else {}
    config, argv = _parse(
        TrainingRequest(dataset_repo_id="lerobot/pusht", policy_type=policy_type, steps=10, **kwargs),
        tmp_path,
    )

    assert config.policy.type == policy_type
    assert config.steps == 10
    assert not [token for token in argv if token.startswith("--wandb")]


def test_tracking_maps_to_trackio_instead_of_wandb(tmp_path):
    request = TrainingRequest(
        dataset_repo_id="lerobot/pusht",
        wandb_enable=True,
        wandb_project="so101-pick",
        wandb_entity="someone",
        wandb_notes="note",
        wandb_run_id="run-1",
        wandb_mode="offline",
    )
    config, argv = _parse(request, tmp_path)

    assert config.trackio.enable is True
    assert config.trackio.project == "so101-pick"
    assert not [token for token in argv if token.startswith("--wandb")]
    # W&B-only concepts are reported, never renamed onto a Trackio field.
    assert sorted(unmapped_tracking_fields(request)) == [
        "wandb_entity",
        "wandb_mode",
        "wandb_notes",
        "wandb_run_id",
    ]
    assert config.trackio.space_id is None
    assert config.trackio.run_name is None


def test_disabled_tracking_emits_no_tracking_flags_at_all(tmp_path):
    request = TrainingRequest(dataset_repo_id="lerobot/pusht", wandb_enable=False)
    _config, argv = _parse(request, tmp_path)

    assert not [token for token in argv if "trackio" in token or "wandb" in token]


def test_optimizer_override_survives_the_policy_preset(tmp_path):
    request = TrainingRequest(dataset_repo_id="lerobot/pusht", optimizer_lr=3e-5)
    config, argv = _parse(request, tmp_path)

    assert "--optimizer_overrides" in argv
    assert config.optimizer_overrides == {"lr": pytest.approx(3e-5)}


def test_resume_takes_precedence_over_preset_overrides():
    """A resumed run must not carry preset overrides: the checkpoint owns them.

    The library-level guard is exercised where draccus provides a parser context
    (resume validation reads `config_path` from the parser); here we assert the
    app-side contract that decides what the trainer ever sees.
    """
    argv = build_training_command(
        TrainingRequest(dataset_repo_id="lerobot/pusht", resume=True, optimizer_lr=3e-5),
        "/tmp/out",
    )

    assert "--optimizer_overrides" not in argv
    assert argv[argv.index("--optimizer.lr") + 1] == "3e-05"


def test_optimizer_override_rejects_unknown_field():
    from lerobot.policies.act import ACTConfig

    config = TrainPipelineConfig(
        dataset=DatasetConfig(repo_id="lerobot/pusht"),
        policy=ACTConfig(),
        use_policy_training_preset=True,
        optimizer_overrides={"betas": 1},
    )
    with pytest.raises(ValueError, match="only supports lr/weight_decay/grad_clip_norm"):
        config.validate()


def test_fastwam_requires_explicit_dimensions():
    with pytest.raises(ValueError, match="fastwam requires explicit dimensions"):
        build_training_command(
            TrainingRequest(dataset_repo_id="lerobot/pusht", policy_type="fastwam"), "/tmp/out"
        )


def test_policy_dimensions_do_not_leak_to_other_policies():
    with pytest.raises(ValueError, match="only apply to fastwam"):
        build_training_command(
            TrainingRequest(dataset_repo_id="lerobot/pusht", policy_type="act", **_FASTWAM_DIMS),
            "/tmp/out",
        )


def test_fastwam_dimensions_reach_the_parsed_config(tmp_path):
    request = TrainingRequest(
        dataset_repo_id="lerobot/pusht",
        policy_type="fastwam",
        policy_action_dim=12,
        policy_proprio_dim=12,
    )
    config, _argv = _parse(request, tmp_path)

    assert config.policy.action_dim == 12
    assert config.policy.proprio_dim == 12
    # 12 channels are not silently replaced by the 7-action default.
    assert config.policy.action_dim != 7


def test_fastwam_rejects_a_dataset_width_that_differs_from_its_own_declaration():
    from lerobot.configs.types import FeatureType, PolicyFeature
    from lerobot.policies.fastwam.configuration_fastwam import FastWAMConfig

    config = FastWAMConfig(action_dim=12, proprio_dim=12)
    # `action_feature` is derived from output_features, so install a 6-wide
    # declaration to model "config says 12 but the bound feature says 6".
    config.output_features = {ACTION: PolicyFeature(type=FeatureType.ACTION, shape=(6,))}

    with pytest.raises(ValueError, match="FastWAM action feature shape must be"):
        config.validate_features()


@pytest.mark.parametrize(
    ("policy_type", "package", "target"),
    [
        ("fastwam", "diffusers", "lerobot[fastwam]"),
        ("fineart_vla", "transformers", "lerobot[fineart_vla]"),
        ("smolvla", "transformers", "lerobot[smolvla]"),
    ],
)
def test_policy_extra_map_covers_the_new_policies(policy_type, package, target):
    from lelab.utils.system import POLICY_EXTRAS

    assert POLICY_EXTRAS[policy_type] == (package, target)


@pytest.mark.parametrize("policy_type", ["fastwam", "fineart_vla"])
def test_language_conditioned_policies_require_a_task(policy_type):
    from lelab.jobs import _LANGUAGE_CONDITIONED_POLICY_TYPES

    assert policy_type in _LANGUAGE_CONDITIONED_POLICY_TYPES


def test_monitor_only_flags_are_reported_when_the_library_cannot_take_them():
    request = TrainingRequest(
        dataset_repo_id="lerobot/pusht",
        weight_norm_freq=50,
        action_loss_groups={"arm": [0, 1, 2, 3, 4], "gripper": [5]},
    )
    # 0.7.0 dropped both fields; they must be reported rather than emitted.
    assert unsupported_training_fields(request) == ["weight_norm_freq", "action_loss_groups"]
    argv = build_training_command(request, "/tmp/out")
    assert "--weight_norm_freq" not in argv
    assert "--action_loss_groups" not in argv
