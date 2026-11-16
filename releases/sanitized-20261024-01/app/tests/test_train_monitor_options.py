"""Training request/CLI mapping for monitoring, validation and resume."""

from __future__ import annotations

import json

import pytest
from lelab.jobs import JobTarget
from lelab.train import TrainingRequest, build_training_command
from pydantic import ValidationError


def _argv(request: TrainingRequest, output_dir: str = "/tmp/run") -> list[str]:
    return build_training_command(request, output_dir, python_executable="python")


def test_optimizer_lr_is_sent_as_post_preset_override() -> None:
    cmd = _argv(
        TrainingRequest(
            dataset_repo_id="a/b",
            optimizer_lr=1e-4,
            optimizer_weight_decay=1e-10,
            optimizer_grad_clip_norm=10,
        )
    )
    assert "--optimizer.lr" not in cmd
    override_index = cmd.index("--optimizer_overrides")
    assert json.loads(cmd[override_index + 1]) == {
        "lr": 1e-4,
        "weight_decay": 1e-10,
        "grad_clip_norm": 10.0,
    }


def test_optimizer_flags_still_used_without_preset() -> None:
    cmd = _argv(
        TrainingRequest(
            dataset_repo_id="a/b",
            use_policy_training_preset=False,
            optimizer_lr=5e-5,
        )
    )
    assert "--optimizer_overrides" not in cmd
    assert cmd[cmd.index("--optimizer.lr") + 1] == "5e-05"


def test_scheduler_and_validation_mapping() -> None:
    """Scheduler *fields* ride post-preset; the type is not interchangeable."""
    cmd = _argv(
        TrainingRequest(
            dataset_repo_id="a/b",
            eval_steps=250,
            dataset_eval_episodes=[0, 1, 2],
            max_eval_samples=12,
            scheduler_warmup_steps=500,
            scheduler_decay_steps=15000,
            scheduler_peak_lr=1e-4,
            scheduler_floor_lr=1e-5,
        )
    )
    assert cmd[cmd.index("--dataset.eval_episodes") + 1] == "[0, 1, 2]"
    assert cmd[cmd.index("--eval_steps") + 1] == "250"
    assert cmd[cmd.index("--max_eval_samples") + 1] == "12"
    scheduler = json.loads(cmd[cmd.index("--scheduler_overrides") + 1])
    assert scheduler == {
        "num_warmup_steps": 500,
        "num_decay_steps": 15000,
        "peak_lr": 1e-4,
        "decay_lr": 1e-5,
    }
    # The type belongs to the policy recipe and is never smuggled through an override.
    assert "type" not in scheduler


def test_scheduler_type_conflicts_with_the_policy_preset() -> None:
    with pytest.raises(ValueError, match="cannot be combined with use_policy_training_preset"):
        _argv(
            TrainingRequest(
                dataset_repo_id="a/b",
                scheduler_type="cosine_decay_with_warmup",
            )
        )


def test_monitor_only_flags_are_reported_not_emitted() -> None:
    """0.7.0 dropped the monitor patch's instrumentation flags."""
    from lelab.train import unsupported_training_fields

    request = TrainingRequest(
        dataset_repo_id="a/b",
        weight_norm_freq=100,
        action_loss_groups={"joints": [0, 1], "grippers": [2]},
    )
    cmd = _argv(request)

    assert "--weight_norm_freq" not in cmd
    assert "--action_loss_groups" not in cmd
    assert unsupported_training_fields(request) == ["weight_norm_freq", "action_loss_groups"]


def test_resume_command_is_minimal_and_checkpoint_authoritative() -> None:
    request = TrainingRequest(
        dataset_repo_id="a/b",
        resume=True,
        resume_config_path="/job/run/checkpoints/000004/pretrained_model",
        steps=999,
        optimizer_lr=1e-3,
        scheduler_type="constant_with_warmup",
        scheduler_warmup_steps=1,
    )
    cmd = _argv(request, output_dir="/job/run")
    assert cmd[:3] == ["python", "-m", "lerobot.scripts.lerobot_train"]
    assert cmd[3:] == [
        "--resume",
        "true",
        "--config_path",
        "/job/run/checkpoints/000004/pretrained_model",
        "--output_dir",
        "/job/run",
    ]
    # No dataset/policy/steps/optimizer/scheduler flags that could override the
    # checkpoint configuration on resume.
    for forbidden in ("--dataset.repo_id", "--steps", "--optimizer.lr", "--scheduler_overrides"):
        assert forbidden not in cmd


def test_eval_requires_explicit_split() -> None:
    with pytest.raises(ValidationError):
        TrainingRequest(dataset_repo_id="a/b", eval_steps=10)
    with pytest.raises(ValidationError):
        TrainingRequest(
            dataset_repo_id="a/b",
            eval_steps=10,
            dataset_eval_split=0.2,
            dataset_eval_episodes=[0],
        )
    with pytest.raises(ValidationError):
        TrainingRequest(
            dataset_repo_id="a/b",
            dataset_episodes=[1, 2],
            dataset_eval_episodes=[2],
        )


def test_unknown_scheduler_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TrainingRequest(dataset_repo_id="a/b", scheduler_type="mystery")


def test_hf_cloud_still_omits_output_dir_and_resume_path_is_ignored() -> None:
    request = TrainingRequest(
        dataset_repo_id="a/b",
        resume=True,
        resume_config_path="/x/pretrained_model",
    )
    cmd = build_training_command(
        request,
        "/tmp/run",
        python_executable="python",
        job_target=JobTarget(runner="hf_cloud", flavor="a10g-large"),
    )
    # Cloud submits through lerobot's HF path; the local resume shortcut must not fire.
    assert "--config_path" not in cmd
    assert "--output_dir" not in cmd
