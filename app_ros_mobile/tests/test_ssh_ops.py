"""SSH helper layer used by training jobs and cloud inference."""

from __future__ import annotations

from pathlib import Path

import pytest

from lelab.remote_sync import TrainServerConfig
from lelab.runners import ssh_ops
from lelab.utils.policy_refs import PolicyRef, parse_policy_ref


def _config() -> TrainServerConfig:
    return TrainServerConfig.from_env(
        {
            "LELAB_TRAIN_HOST": "192.0.2.10",
            "LELAB_TRAIN_PORT": "22",
            "LELAB_TRAIN_USER": "trainer",
            "LELAB_TRAIN_ROOT": "~/Desktop/lelab_server",
        }
    )


def test_list_remote_checkpoints_parses_and_sorts() -> None:
    captured: dict[str, str] = {}

    def fake_ssh(argv, input_text=None, **kwargs):
        captured["script"] = input_text or ""
        captured["command"] = argv[-1]
        return 0, "50\t/home/u/jobs/x/run/checkpoints/000050/pretrained_model\n2\t/home/u/jobs/x/run/checkpoints/000002/pretrained_model\nnoise\n"

    entries = ssh_ops.list_remote_checkpoints(_config(), "~/Desktop/lelab_server/jobs/x", ssh=fake_ssh)

    assert entries == [
        (2, "/home/u/jobs/x/run/checkpoints/000002/pretrained_model"),
        (50, "/home/u/jobs/x/run/checkpoints/000050/pretrained_model"),
    ]
    # The script must look at both the job root and lerobot's run/ output.
    assert 'root="$job_dir/run/checkpoints"' in captured["script"]


def test_run_python_keeps_secrets_out_of_argv() -> None:
    seen: dict[str, object] = {}

    def fake_ssh(argv, input_text=None, **kwargs):
        seen["argv"] = argv
        seen["script"] = input_text
        return 0, "ok"

    ssh_ops.run_python(
        _config(),
        "print('hi')",
        "payload",
        python="/home/u/.venv/bin/python",
        env={"HF_TOKEN": "hf_secret", "HF_ENDPOINT": "https://hf-mirror.com"},
        ssh=fake_ssh,
    )

    assert "hf_secret" not in " ".join(seen["argv"])  # type: ignore[arg-type]
    assert "os.environ['HF_TOKEN'] = 'hf_secret'" in str(seen["script"])
    assert str(seen["argv"][-1]).startswith("/home/u/.venv/bin/python - ")  # type: ignore[index]


def test_read_remote_file_returns_none_when_missing() -> None:
    def fake_ssh(argv, input_text=None, **kwargs):
        return 3, ""

    assert ssh_ops.read_remote_file(_config(), "/nope/config.json", ssh=fake_ssh) is None


def test_stage_policy_uses_local_dir_not_hub_cache() -> None:
    """Mirror redirects stall the hub-cache blob path; local_dir works."""
    seen: dict[str, str] = {}

    def fake_ssh(argv, input_text=None, **kwargs):
        seen["script"] = input_text or ""
        return 0, "POLICY_PATH=/home/u/lelab_server/models/policies/l-e-n--act_so101\n"

    path = ssh_ops.stage_policy_on_server(_config(), parse_policy_ref("l-e-n/act_so101@root"), ssh=fake_ssh)

    assert path == "/home/u/lelab_server/models/policies/l-e-n--act_so101"
    assert "local_dir=" in seen["script"]
    # The stage root can be overridden per deployment (the Jetson sets it to
    # <train root>/models/policies).
    assert "LELAB_TRAIN_POLICY_STAGE" in seen["script"]


def test_stage_policy_copies_a_jetson_local_directory_atomically(tmp_path: Path) -> None:
    source = tmp_path / "models" / "run" / "checkpoints" / "010000" / "pretrained_model"
    source.mkdir(parents=True)
    (source / "config.json").write_text("{}", encoding="utf-8")
    calls: list[str] = []
    transfer_commands: list[list[str]] = []

    def fake_ssh(argv, input_text=None, **kwargs):
        script = input_text or ""
        calls.append(script)
        if "STAGING_PATH=" in script:
            return 0, "STAGING_PATH=/home/u/lelab_server/models/policies/.incoming/local-x\n"
        return 0, "POLICY_PATH=/home/u/lelab_server/models/policies/local-x\n"

    def fake_transfer(argv, **kwargs):
        transfer_commands.append(argv)
        return 0, "sent"

    result = ssh_ops.stage_policy_on_server(
        _config(),
        PolicyRef(kind="local", raw=str(source), path=str(source)),
        env={"LELAB_TRAIN_POLICY_STAGE": "~/Desktop/lelab_server/models/policies"},
        ssh=fake_ssh,
        transfer=fake_transfer,
    )

    assert result == "/home/u/lelab_server/models/policies/local-x"
    assert len(calls) == 2
    assert "mv -- \"$incoming\" \"$target\"" in calls[1]
    command = transfer_commands[0]
    assert command[0] == "rsync"
    assert command[-2] == f"{source}/"
    assert command[-1].endswith(":/home/u/lelab_server/models/policies/.incoming/local-x/")


def test_stage_policy_reuses_a_ready_local_stage(tmp_path: Path) -> None:
    source = tmp_path / "pretrained_model"
    source.mkdir()
    (source / "model.safetensors").write_bytes(b"weights")

    def fake_ssh(argv, input_text=None, **kwargs):
        return 0, "POLICY_PATH=/home/u/lelab_server/models/policies/local-ready\n"

    def unexpected_transfer(argv, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("a ready content-addressed stage must be reused")

    result = ssh_ops.stage_policy_on_server(
        _config(),
        PolicyRef(kind="local", raw=str(source), path=str(source)),
        ssh=fake_ssh,
        transfer=unexpected_transfer,
    )
    assert result.endswith("/local-ready")


def test_stage_policy_keeps_server_only_absolute_paths_remote() -> None:
    seen: dict[str, str] = {}

    def fake_ssh(argv, input_text=None, **kwargs):
        seen["script"] = input_text or ""
        return 0, "POLICY_PATH=/home/u/lelab_server/jobs/run/pretrained_model\n"

    result = ssh_ops.stage_policy_on_server(
        _config(), parse_policy_ref("/home/u/lelab_server/jobs/run/pretrained_model"), ssh=fake_ssh
    )
    assert result == "/home/u/lelab_server/jobs/run/pretrained_model"
    assert "policy directory not found on the training server" in seen["script"]


def test_soft_delete_moves_only_our_jobs() -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_ssh(argv, input_text=None, **kwargs):
        # argv[-1] carries the quoted arguments of `sh -s -- ...`
        calls.append((input_text or "", tuple(argv[-1].split()[-3:])))
        return 0, "act_job-20260919-120000\n"

    name = ssh_ops.soft_delete_remote_job(
        _config(), "act_job", instance_id="jetson-primary-local", ssh=fake_ssh
    )
    assert name == "act_job-20260919-120000"
    script, args = calls[0]
    assert 'trash_dir="$jobs_dir/.trash"' in script
    assert "mv -- " in script
    assert "act_job" in args and "jetson-primary-local" in args


def test_soft_delete_refuses_foreign_or_missing_jobs() -> None:
    def fake_ssh(argv, input_text=None, **kwargs):
        return 3, ""

    with pytest.raises(ssh_ops.RemoteJobNotOursError):
        ssh_ops.soft_delete_remote_job(_config(), "act_job", instance_id="me", ssh=fake_ssh)
