"""Tests for multi-dataset training requests and composite resolution."""

from __future__ import annotations

import pytest


def _arg_value(cmd: list[str], flag: str) -> str:
    assert flag in cmd, f"{flag} missing from {cmd}"
    return cmd[cmd.index(flag) + 1]


def test_single_dataset_request_keeps_legacy_shape() -> None:
    from lelab.train import TrainingRequest, build_training_command

    request = TrainingRequest(dataset_repo_id="local/one")

    assert request.dataset_ids == ["local/one"]
    cmd = build_training_command(request, "/tmp/out")
    assert _arg_value(cmd, "--dataset.repo_id") == "local/one"
    assert "--dataset.sample_weights_file" not in cmd


def test_multi_dataset_request_needs_a_composite() -> None:
    from lelab.train import TrainingRequest, build_training_command

    request = TrainingRequest(
        dataset_repo_id="local/a", dataset_repo_ids=["local/a", "local/b"]
    )

    assert request.dataset_ids == ["local/a", "local/b"]
    assert request.dataset_repo_id == "local/a"
    with pytest.raises(ValueError, match="composite"):
        build_training_command(request, "/tmp/out")


def test_multi_dataset_request_uses_the_composite_repo_id() -> None:
    from lelab.train import TrainingRequest, build_training_command

    request = TrainingRequest(
        dataset_repo_id="local/a",
        dataset_repo_ids=["local/a", "local/b"],
        dataset_composite_hash="deadbeef",
    )

    resolved = request.model_copy(update={"dataset_repo_id": "local/composite_deadbeef"})
    cmd = build_training_command(resolved, "/tmp/out")

    assert _arg_value(cmd, "--dataset.repo_id") == "local/composite_deadbeef"


def test_duplicate_members_are_rejected() -> None:
    from pydantic import ValidationError

    from lelab.train import TrainingRequest

    with pytest.raises(ValidationError):
        TrainingRequest(dataset_repo_id="local/a", dataset_repo_ids=["local/a", "local/a"])


def test_equal_weights_need_at_least_two_datasets() -> None:
    from pydantic import ValidationError

    from lelab.train import TrainingRequest

    with pytest.raises(ValidationError):
        TrainingRequest(dataset_repo_id="local/a", dataset_weights_mode="equal")


def test_custom_weights_must_match_member_count() -> None:
    from pydantic import ValidationError

    from lelab.train import TrainingRequest

    with pytest.raises(ValidationError):
        TrainingRequest(
            dataset_repo_id="local/a",
            dataset_repo_ids=["local/a", "local/b"],
            dataset_weights_mode="custom",
            dataset_weights=[1.0],
        )
    with pytest.raises(ValidationError):
        TrainingRequest(
            dataset_repo_id="local/a",
            dataset_repo_ids=["local/a", "local/b"],
            dataset_weights_mode="custom",
            dataset_weights=[1.0, 0.0],
        )


def test_weight_modes_emit_the_sampler_file_flag() -> None:
    from lelab.train import TrainingRequest, build_training_command

    request = TrainingRequest(
        dataset_repo_id="local/a",
        dataset_repo_ids=["local/a", "local/b"],
        dataset_composite_hash="deadbeef",
        dataset_weights_mode="equal",
        dataset_weights_file="weights.json",
    ).model_copy(update={"dataset_repo_id": "local/composite_deadbeef"})

    cmd = build_training_command(request, "/tmp/out")

    assert _arg_value(cmd, "--dataset.sample_weights_file") == "weights.json"


def test_weight_modes_require_a_weights_file() -> None:
    from lelab.train import TrainingRequest, build_training_command

    request = TrainingRequest(
        dataset_repo_id="local/a",
        dataset_repo_ids=["local/a", "local/b"],
        dataset_composite_hash="deadbeef",
        dataset_weights_mode="equal",
    ).model_copy(update={"dataset_repo_id": "local/composite_deadbeef"})

    with pytest.raises(ValueError, match="sample_weights_file"):
        build_training_command(request, "/tmp/out")


def _fake_plan(weights_mode: str = "equal"):
    from lelab.composites import CompositePlan

    return CompositePlan(
        repo_ids=("local/a", "local/b"),
        members=(
            {
                "repo_id": "local/a",
                "total_episodes": 2,
                "total_frames": 10,
                "meta_fingerprint_sha256": "a" * 64,
                "seal_manifest_sha256": "s" * 64,
            },
            {
                "repo_id": "local/b",
                "total_episodes": 3,
                "total_frames": 30,
                "meta_fingerprint_sha256": "b" * 64,
                "seal_manifest_sha256": "t" * 64,
            },
        ),
        blockers=(),
        warnings=(),
        estimated_size_bytes=1024,
        weights_mode=weights_mode,
        weights=(0.5, 0.5),
        composite_hash="deadbeefdeadbeef",
    )


def test_resolver_rejects_local_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    from lelab.jobs import JobTarget, resolve_dataset_selection
    from lelab.train import TrainingRequest

    request = TrainingRequest(
        dataset_repo_id="local/a", dataset_repo_ids=["local/a", "local/b"]
    )

    with pytest.raises(ValueError, match="训练机"):
        resolve_dataset_selection(request, JobTarget(runner="local"))


def test_resolver_returns_ready_composite_with_episode_weights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lelab import composites
    from lelab.jobs import JobTarget, resolve_dataset_selection
    from lelab.train import TrainingRequest

    plan = _fake_plan()
    monkeypatch.setattr(composites, "plan_composite", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        composites,
        "remote_status",
        lambda repo_id, **kwargs: {"state": "ready", "repo_id": repo_id},
    )

    request = TrainingRequest(
        dataset_repo_id="local/a",
        dataset_repo_ids=["local/a", "local/b"],
        dataset_weights_mode="equal",
    )
    resolved = resolve_dataset_selection(request, JobTarget(runner="ssh_remote"))

    assert resolved.dataset_repo_id == "local/composite_deadbeefdeadbeef"
    assert resolved.dataset_composite_hash == "deadbeefdeadbeef"
    assert resolved.dataset_weights == [0.5, 0.5]
    assert resolved.dataset_episode_weights == [0.05, 0.05] + [0.5 / 30] * 3
    assert resolved.dataset_ids == ["local/a", "local/b"]


def test_resolver_blocks_unbuilt_composite(monkeypatch: pytest.MonkeyPatch) -> None:
    from lelab import composites
    from lelab.jobs import JobTarget, resolve_dataset_selection
    from lelab.train import TrainingRequest

    monkeypatch.setattr(composites, "plan_composite", lambda *args, **kwargs: _fake_plan())
    monkeypatch.setattr(
        composites, "remote_status", lambda repo_id, **kwargs: {"state": "missing"}
    )

    request = TrainingRequest(
        dataset_repo_id="local/a", dataset_repo_ids=["local/a", "local/b"]
    )

    with pytest.raises(ValueError, match="尚未在训练机构建"):
        resolve_dataset_selection(request, JobTarget(runner="ssh_remote"))


def test_resolver_reports_blockers(monkeypatch: pytest.MonkeyPatch) -> None:
    from lelab import composites
    from lelab.jobs import JobTarget, resolve_dataset_selection
    from lelab.train import TrainingRequest

    blocked = _fake_plan()
    blocked = composites.CompositePlan(
        repo_ids=blocked.repo_ids,
        members=blocked.members,
        blockers=(
            {"code": "not-sealed", "message": "local/b: 尚未封存", "repo_ids": ["local/b"]},
        ),
        warnings=(),
        estimated_size_bytes=0,
    )
    monkeypatch.setattr(composites, "plan_composite", lambda *args, **kwargs: blocked)

    request = TrainingRequest(
        dataset_repo_id="local/a", dataset_repo_ids=["local/a", "local/b"]
    )

    with pytest.raises(ValueError, match="尚未封存"):
        resolve_dataset_selection(request, JobTarget(runner="ssh_remote"))


def test_job_id_marks_multi_dataset_runs() -> None:
    from lelab.jobs import _generate_job_id

    job_id = _generate_job_id("act", "local/composite_deadbeef", dataset_count=3)

    assert "_plus2_" in job_id
