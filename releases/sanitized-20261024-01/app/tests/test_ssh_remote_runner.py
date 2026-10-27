"""Tests for the SSH training runner (all remote calls are faked)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lelab.jobs import TrainingMetrics
from lelab.remote_sync import TrainServerConfig
from lelab.runners.ssh_remote import RemoteTrainingError, SshRemoteJobRunner
from lelab.train import TrainingRequest

TRAIN_ENV = {
    "LELAB_TRAIN_HOST": "192.0.2.10",
    "LELAB_TRAIN_PORT": "22",
    "LELAB_TRAIN_USER": "trainer",
    "LELAB_TRAIN_ROOT": "~/Desktop/lelab_server",
}


class FakeSsh:
    """Scripted SSH double: matches a substring of the argv command."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.responses: list[tuple[str, int, str]] = []

    def add(self, needle: str, return_code: int, output: str) -> None:
        self.responses.append((needle, return_code, output))

    def __call__(self, argv: list[str], input_text: str | None = None) -> tuple[int, str]:
        self.calls.append((argv, input_text))
        command = argv[-1] if argv else ""
        haystack = f"{command}\n{input_text or ''}"
        for needle, return_code, output in reversed(self.responses):
            if needle in haystack:
                return return_code, output
        return 0, ""

    @property
    def scripts(self) -> list[str]:
        return [call[1] or "" for call in self.calls]

    @property
    def commands(self) -> list[str]:
        return [call[0][-1] for call in self.calls]


@pytest.fixture()
def config() -> TrainServerConfig:
    return TrainServerConfig.from_env(TRAIN_ENV)


@pytest.fixture()
def fake_ssh() -> FakeSsh:
    return FakeSsh()


def _runner(config, fake_ssh, tmp_path: Path, *, log_name: str = "log.jsonl") -> SshRemoteJobRunner:
    return SshRemoteJobRunner(
        config,
        "act-20260101-000000",
        TrainingMetrics(),
        log_file_path=tmp_path / log_name,
        ssh=fake_ssh,
        poll_interval=0.05,
    )


def test_remote_layout_is_under_the_configured_root(config, fake_ssh, tmp_path: Path) -> None:
    runner = _runner(config, fake_ssh, tmp_path)
    assert runner.remote_dir == "~/Desktop/lelab_server/jobs/act-20260101-000000"
    assert runner.remote_output_dir.endswith("/jobs/act-20260101-000000/run")
    assert runner.dataset_dir("local/pick") == "~/Desktop/lelab_server/datasets/local/pick"


def test_start_refuses_an_unsynchronized_dataset(config, fake_ssh, tmp_path: Path) -> None:
    # The readiness probe exits 3 when `.ready` is absent (a transport failure
    # is a different, louder branch — see the next test).
    fake_ssh.add(".ready", 3, "")
    runner = _runner(config, fake_ssh, tmp_path)

    with pytest.raises(RemoteTrainingError) as excinfo:
        runner.start("act-20260101-000000", TrainingRequest(dataset_repo_id="local/pick"), "/tmp/out")

    assert "not synchronized" in str(excinfo.value)
    # Only the readiness probe ran; nothing was launched.
    assert len(fake_ssh.calls) == 1


def test_start_reports_ssh_failure_separately(config, fake_ssh, tmp_path: Path) -> None:
    """A dead SSH link must not be reported as 'dataset not synchronized'."""
    fake_ssh.add(".ready", 255, "ssh: connect to host 192.0.2.10 port 22: Connection timed out")
    runner = _runner(config, fake_ssh, tmp_path)

    with pytest.raises(RemoteTrainingError) as excinfo:
        runner.start("act-20260101-000000", TrainingRequest(dataset_repo_id="local/pick"), "/tmp/out")

    message = str(excinfo.value)
    assert "not synchronized" not in message
    assert "ssh exit 255" in message
    assert len(fake_ssh.calls) == 1


def test_start_writes_config_and_launches_detached(config, fake_ssh, tmp_path: Path) -> None:
    fake_ssh.add(".ready", 0, "")
    fake_ssh.add("kill -0", 0, "running 4242")
    runner = _runner(config, fake_ssh, tmp_path)
    request = TrainingRequest(dataset_repo_id="local/pick", steps=10, policy_type="act")

    runner.start("act-20260101-000000", request, "/local/output")

    assert runner.pid() == 4242
    uploaded = [call for call in fake_ssh.calls if "cat >" in call[0][-1]]
    assert len(uploaded) == 2  # train_config.json + .lelab-job.json marker
    configs = [call for call in uploaded if "train_config.json" in call[0][-1]]
    assert len(configs) == 1
    payload = json.loads(configs[0][1] or "{}")
    assert payload["job_id"] == "act-20260101-000000"
    assert payload["source"] == "jetson"
    assert payload["request"]["dataset_repo_id"] == "local/pick"
    assert payload["request"]["steps"] == 10
    assert payload["config_sha256"]

    markers = [call for call in uploaded if ".lelab-job.json" in call[0][-1]]
    assert len(markers) == 1
    marker = json.loads(markers[0][1] or "{}")
    assert marker["kind"] == "train"
    assert marker["job_id"] == "act-20260101-000000"
    assert marker["dataset"] == "local/pick"
    assert marker["instance_id"]

    launch_scripts = [script for script in fake_ssh.scripts if "setsid" in script]
    assert len(launch_scripts) == 1
    launch_call = next(call for call in fake_ssh.calls if "setsid" in (call[1] or ""))
    assert "lerobot.scripts.lerobot_train" in launch_call[0][-1]
    assert "--dataset.repo_id" in launch_call[0][-1]
    assert "local/pick" in launch_call[0][-1]


def test_start_uploads_weights_file_for_weighted_composites(
    config, fake_ssh, tmp_path: Path
) -> None:
    fake_ssh.add(".ready", 0, "")
    fake_ssh.add("kill -0", 0, "running 4242")
    runner = _runner(config, fake_ssh, tmp_path)
    request = TrainingRequest(
        dataset_repo_id="local/composite_deadbeef",
        dataset_repo_ids=["local/a", "local/b"],
        dataset_composite_hash="deadbeef",
        dataset_weights_mode="equal",
        dataset_weights=[0.5, 0.5],
        dataset_episode_weights=[0.05, 0.05, 0.1],
        dataset_weights_file="weights.json",
    )

    runner.start("act-20260101-000000", request, "/local/output")

    uploads = [call for call in fake_ssh.calls if "cat >" in call[0][-1]]
    weights_uploads = [call for call in uploads if "weights.json" in call[0][-1]]
    assert len(weights_uploads) == 1
    payload = json.loads(weights_uploads[0][1] or "{}")
    assert payload["mode"] == "equal"
    assert payload["members"] == ["local/a", "local/b"]
    assert payload["member_weights"] == [0.5, 0.5]
    assert payload["episode_weights"] == [0.05, 0.05, 0.1]

    launch_call = next(call for call in fake_ssh.calls if "setsid" in (call[1] or ""))
    assert "--dataset.sample_weights_file" in launch_call[0][-1]
    assert "weights.json" in launch_call[0][-1]
    assert "local/composite_deadbeef" in launch_call[0][-1]

    marker = next(
        json.loads(call[1] or "{}")
        for call in uploads
        if ".lelab-job.json" in call[0][-1]
    )
    assert marker["datasets"] == ["local/a", "local/b"]
    assert marker["composite_hash"] == "deadbeef"
    assert marker["weights_mode"] == "equal"


def test_weighted_start_fails_fast_when_remote_lerobot_is_old(
    config, fake_ssh, tmp_path: Path
) -> None:
    fake_ssh.add(".ready", 0, "")
    # The capability probe runs `python3 -` with the DatasetConfig source on stdin
    # and exits 3 when `sample_weights_file` is missing.
    fake_ssh.add("DatasetConfig", 3, "")
    runner = _runner(config, fake_ssh, tmp_path)
    request = TrainingRequest(
        dataset_repo_id="local/composite_deadbeef",
        dataset_repo_ids=["local/a", "local/b"],
        dataset_composite_hash="deadbeef",
        dataset_weights_mode="equal",
        dataset_weights=[0.5, 0.5],
        dataset_episode_weights=[0.1, 0.1],
        dataset_weights_file="weights.json",
    )

    with pytest.raises(RemoteTrainingError, match="不支持加权采样"):
        runner.start("act-20260101-000000", request, "/local/output")

    uploaded = [call for call in fake_ssh.calls if "cat >" in call[0][-1]]
    assert not any("weights.json" in call[0][-1] for call in uploaded)


def test_start_rejects_a_second_call(config, fake_ssh, tmp_path: Path) -> None:
    """A non-resume lerobot run refuses a pre-existing output dir.

    The remote job dir may be created, but `run/` must be left to lerobot
    (`FileExistsError: Output directory run already exists`).
    """
    fake_ssh.add(".ready", 0, "")
    fake_ssh.add("kill -0", 0, "running 7")
    runner = _runner(config, fake_ssh, tmp_path)
    runner.start("job", TrainingRequest(dataset_repo_id="local/pick"), "/out")
    runner._stop_event.set()
    if runner._thread is not None:
        runner._thread.join(timeout=2)

    prepare_scripts = [script for script in fake_ssh.scripts if "mkdir -p" in script]
    assert prepare_scripts, "expected a remote prepare script"
    assert all("run" not in script for script in prepare_scripts)

    with pytest.raises(RemoteTrainingError):
        runner.start("job", TrainingRequest(dataset_repo_id="local/pick"), "/out")


def test_log_lines_update_metrics_and_persist(config, fake_ssh, tmp_path: Path) -> None:
    fake_ssh.add(".ready", 0, "")
    fake_ssh.add("kill -0", 0, "running 99")
    runner = _runner(config, fake_ssh, tmp_path)
    runner.start("job", TrainingRequest(dataset_repo_id="local/pick"), "/out")

    fake_ssh.add(
        "tail -c",
        0,
        "Training:  10%|█         | 100/1000 [00:20<03:00,  5.0step/s]\n"
        "INFO step:100 smpl:800 loss:0.512 grdn:1.2 lr:1e-4\n"
        "wandb: View run at https://wandb.ai/team/proj/runs/abc123\n",
    )
    runner._drain_remote_log()

    lines = runner.stream_log_lines()
    messages = [line.message for line in lines]
    assert any("loss:0.512" in message for message in messages)
    assert runner._metrics.current_step == 100
    assert runner._metrics.total_steps == 1000
    assert runner._metrics.current_loss == pytest.approx(0.512)
    assert runner.wandb_run_url() == "https://wandb.ai/team/proj/runs/abc123"

    persisted = (tmp_path / "log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(persisted) == 3


def test_partial_lines_are_joined_across_polls(config, fake_ssh, tmp_path: Path) -> None:
    runner = _runner(config, fake_ssh, tmp_path)

    fake_ssh.add("tail -c", 0, "INFO step:5 smpl:40 los")
    runner._drain_remote_log()
    assert runner.stream_log_lines() == []

    fake_ssh.responses.clear()
    fake_ssh.add("tail -c", 0, "s:0.25 grdn:0.9 lr:1e-4\n")
    runner._drain_remote_log()

    lines = runner.stream_log_lines()
    assert len(lines) == 1
    assert "loss:0.25" in lines[0].message


def test_is_running_reads_the_exit_file(config, fake_ssh, tmp_path: Path) -> None:
    runner = _runner(config, fake_ssh, tmp_path)
    fake_ssh.add("kill -0", 0, "exit 4242 0")
    fake_ssh.add("tail -c", 0, "done\n")

    assert runner.is_running() is False
    assert runner.returncode() == 0


def test_is_running_reports_a_failed_exit_code(config, fake_ssh, tmp_path: Path) -> None:
    runner = _runner(config, fake_ssh, tmp_path)
    fake_ssh.add("kill -0", 0, "exit 4242 2")
    fake_ssh.add("tail -c", 0, "")

    assert runner.is_running() is False
    assert runner.returncode() == 2


def test_stop_sends_term_to_the_process_group(config, fake_ssh, tmp_path: Path) -> None:
    runner = _runner(config, fake_ssh, tmp_path)
    fake_ssh.add("kill -TERM", 0, "")
    fake_ssh.add("kill -0", 0, "exit 4242 143")

    runner.stop()

    assert any("kill -TERM" in script for script in fake_ssh.scripts)
    assert runner.returncode() == 143


def test_reattach_skips_the_existing_log(config, fake_ssh, tmp_path: Path) -> None:
    runner = _runner(config, fake_ssh, tmp_path)
    fake_ssh.add("wc -c", 0, "1234\n")
    fake_ssh.add("kill -0", 0, "running 55")

    runner.reattach(remote_dir="~/Desktop/lelab_server/jobs/old-job")

    assert runner.remote_dir == "~/Desktop/lelab_server/jobs/old-job"
    assert runner._log_offset == 1234
    assert runner.pid() == 55
    fake_ssh.responses.clear()
    fake_ssh.add("tail -c", 0, "new line\n")
    runner._drain_remote_log()
    assert [line.message for line in runner.stream_log_lines()] == ["new line"]


def test_reattach_marks_a_finished_job(config, fake_ssh, tmp_path: Path) -> None:
    runner = _runner(config, fake_ssh, tmp_path)
    fake_ssh.add("wc -c", 0, "10\n")
    fake_ssh.add("kill -0", 0, "exit 55 0")

    runner.reattach(remote_dir="~/Desktop/lelab_server/jobs/old-job")

    assert runner.is_running() is False
    assert runner.returncode() == 0
