"""Pause/resume state machine tests with a fake runner and a real verified checkpoint."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from lelab import jobs as jobs_module
from lelab.jobs import (
    JobAttempt,
    JobRecord,
    JobRegistry,
    PauseNotSupportedError,
    ResumeNotPossibleError,
    pause_resume_capabilities,
    verify_local_pause_checkpoint,
)
from lelab.train import TrainingRequest

from lerobot.utils.lelab_monitor import build_checkpoint_manifest, write_json_atomic


def _make_checkpoint(root: Path, step: int = 4) -> Path:
    checkpoint = root / "run" / "checkpoints" / f"{step:06d}"
    files = {
        "pretrained_model/model.safetensors": b"weights",
        "pretrained_model/config.json": b'{"type":"act"}',
        "pretrained_model/train_config.json": b'{"steps":10}',
        "training_state/training_step.json": b'{"step":4}',
        "training_state/rng_state.safetensors": b"rng",
        "training_state/optimizer_state.safetensors": b"optimizer",
        "training_state/scheduler_state.json": b'{"last_epoch":4}',
    }
    for name, payload in files.items():
        path = checkpoint / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    manifest = build_checkpoint_manifest(
        checkpoint, step=step, cfg=SimpleNamespace(steps=10), extra={"dataset_repo_id": "a/b"}
    )
    assert manifest["complete"] is True
    write_json_atomic(checkpoint / "lelab_pause.json", manifest)
    return checkpoint


class FakeRunner:
    supports_pause = True
    stop_requested = False

    def __init__(self) -> None:
        self.running = True
        self.rc = None
        self.pause_calls = 0
        self.pause_state: dict | None = None
        self.raise_on_pause = False

    def request_pause(self) -> None:
        self.pause_calls += 1
        if self.raise_on_pause:
            raise RuntimeError("marker write failed")

    def read_pause_state(self) -> dict | None:
        return self.pause_state

    def is_running(self) -> bool:
        return self.running

    def returncode(self):
        return self.rc

    def stream_log_lines(self):
        return []

    def wandb_run_url(self):
        return None

    def stop(self) -> None:
        self.running = False
        self.rc = -15


class FakeLaunchRunner(FakeRunner):
    launched: list[dict] = []

    def __init__(self, metrics, log_file_path=None, **kwargs) -> None:
        super().__init__()
        self.metrics = metrics
        self.kwargs = kwargs
        self.process_pid = 4321
        self.remote_dir = None

    def pid(self):
        return self.process_pid

    def start(self, job_id, config, output_dir) -> None:
        type(self).launched.append(
            {
                "job_id": job_id,
                "config": config.model_dump(mode="json"),
                "output_dir": output_dir,
                "kwargs": self.kwargs,
            }
        )


def _registry(tmp_path: Path) -> JobRegistry:
    registry = JobRegistry(tmp_path / "root")
    registry.shutdown()
    if registry._watchdog_thread is not None:
        registry._watchdog_thread.join(timeout=2)
    return registry


def _record(tmp_path: Path, *, runner: str = "local", capable: bool = True) -> JobRecord:
    return JobRecord(
        id="job1",
        name="ACT · a/b",
        state="running",
        config=TrainingRequest(dataset_repo_id="a/b", steps=10),
        output_dir=str(tmp_path / "root" / "job1" / "run"),
        started_at=time.time(),
        runner=runner,
        attempts=[JobAttempt(id="a1", started_at=time.time(), state="running")],
        pause_capable=capable,
        process_pid=1234,
    )


def _install(registry: JobRegistry, record: JobRecord, runner: FakeRunner) -> None:
    Path(record.output_dir).parent.mkdir(parents=True, exist_ok=True)
    registry._records[record.id] = record
    registry._runners[record.id] = runner
    registry._persist(record, force=True)


def test_pause_then_saved_checkpoint_then_paused(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    record = _record(tmp_path)
    runner = FakeRunner()
    _install(registry, record, runner)

    paused_request = registry.pause(record.id)
    assert paused_request.state == "pause_requested"
    assert runner.pause_calls == 1

    runner.pause_state = {"state": "saving"}
    registry._tick()
    assert registry.get(record.id).pause_status == "saving"

    checkpoint = _make_checkpoint(tmp_path)
    ok, reason, _manifest = verify_local_pause_checkpoint(checkpoint)
    assert ok, reason

    runner.pause_state = {"state": "saved", "checkpoint": str(checkpoint)}
    runner.running = False
    runner.rc = 75
    registry._tick()

    paused = registry.get(record.id)
    assert paused.state == "paused"
    assert paused.pause_checkpoint == str(checkpoint)
    assert paused.pause_step == 4
    assert paused.current_attempt().state == "paused"
    assert pause_resume_capabilities(paused)["can_resume"] is True


def test_pause_save_failure_reverts_to_running(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    record = _record(tmp_path)
    runner = FakeRunner()
    _install(registry, record, runner)

    registry.pause(record.id)
    runner.pause_state = {"state": "failed", "error": "disk full"}
    registry._tick()
    current = registry.get(record.id)
    assert current.state == "running"
    assert current.pause_status == "failed"
    assert "disk full" in (current.pause_error or "")


def test_pause_request_transport_failure(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    record = _record(tmp_path)
    runner = FakeRunner()
    runner.raise_on_pause = True
    _install(registry, record, runner)

    with pytest.raises(PauseNotSupportedError):
        registry.pause(record.id)
    current = registry.get(record.id)
    assert current.state == "running"
    assert current.pause_status == "failed"
    assert "marker write failed" in (current.pause_error or "")


def test_pause_unsupported_for_cloud_and_legacy_jobs(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    cloud = _record(tmp_path, runner="hf_cloud", capable=False)
    cloud.id = "cloud"
    _install(registry, cloud, FakeRunner())
    with pytest.raises(PauseNotSupportedError):
        registry.pause("cloud")

    legacy = _record(tmp_path, capable=False)
    legacy.id = "legacy"
    _install(registry, legacy, FakeRunner())
    with pytest.raises(PauseNotSupportedError):
        registry.pause("legacy")


def test_resume_creates_new_attempt_with_checkpoint_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    checkpoint = _make_checkpoint(tmp_path)
    record = _record(tmp_path)
    record.state = "paused"
    record.pause_checkpoint = str(checkpoint)
    record.pause_step = 4
    record.pause_status = "saved"
    _install(registry, record, FakeRunner())

    FakeLaunchRunner.launched.clear()
    monkeypatch.setattr(jobs_module, "LocalJobRunner", FakeLaunchRunner)
    resumed = registry.resume(record.id)

    assert resumed.state == "running"
    assert len(resumed.attempts) == 2
    assert resumed.attempts[-1].resumed_from_step == 4
    assert resumed.attempts[-1].resume_checkpoint == str(checkpoint)
    assert resumed.process_pid == 4321
    launched = FakeLaunchRunner.launched[-1]
    assert launched["output_dir"] == record.output_dir
    assert launched["config"]["resume"] is True
    assert launched["config"]["resume_config_path"] == str(checkpoint / "pretrained_model")
    assert launched["config"]["steps"] == 10
    assert launched["kwargs"]["attempt_id"] == resumed.attempts[-1].id


def test_resume_rejects_missing_or_corrupted_checkpoint(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    record = _record(tmp_path)
    record.state = "paused"
    record.pause_checkpoint = str(tmp_path / "run" / "checkpoints" / "000004")
    _install(registry, record, FakeRunner())
    with pytest.raises(ResumeNotPossibleError):
        registry.resume(record.id)
    assert registry.get(record.id).state == "paused"

    checkpoint = _make_checkpoint(tmp_path)
    (checkpoint / "pretrained_model" / "model.safetensors").write_bytes(b"tampered")
    record.pause_checkpoint = str(checkpoint)
    with pytest.raises(ResumeNotPossibleError):
        registry.resume(record.id)
    assert "校验失败" in (registry.get(record.id).pause_error or "")


def test_stop_while_paused_preserves_checkpoint(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    checkpoint = _make_checkpoint(tmp_path)
    record = _record(tmp_path)
    record.state = "paused"
    record.pause_checkpoint = str(checkpoint)
    _install(registry, record, FakeRunner())
    stopped = registry.stop(record.id)
    assert stopped.state == "interrupted"
    assert stopped.pause_checkpoint == str(checkpoint)


def test_metrics_history_and_monitor_endpoints(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    record = _record(tmp_path)
    _install(registry, record, FakeRunner())
    from lelab.training_metrics import store_for

    store = store_for(Path(record.output_dir).parent)
    store.ingest_line(
        record.id,
        "a1",
        'INFO step:3 smpl:6 loss:0.5 grdn:1.5 lr:0.0001',
        3.0,
    )
    history = registry.read_metrics_history(record.id)
    assert [(point.step, point.loss, point.grad_norm) for point in history] == [(3, 0.5, 1.5)]
    info = registry.monitor_info(record.id)
    assert info["job_id"] == record.id
    assert info["capabilities"]["pause_resume"] is True
    assert info["metric_event_count"] == 1


def test_load_from_disk_recovers_paused_checkpoint(tmp_path: Path) -> None:
    root = tmp_path / "root"
    job_dir = root / "job1"
    job_dir.mkdir(parents=True)
    checkpoint = _make_checkpoint(tmp_path)
    record = _record(tmp_path)
    record.state = "pause_requested"
    record.process_pid = 2**31 - 1  # definitely not alive
    (job_dir / "job.json").write_text(record.model_dump_json(), encoding="utf-8")
    control = job_dir / "control"
    control.mkdir()
    (control / "pause.state.json").write_text(
        json.dumps({"state": "saved", "checkpoint": str(checkpoint)}), encoding="utf-8"
    )

    registry = _registry(tmp_path)
    restored = registry.get("job1")
    assert restored.state == "paused"
    assert restored.pause_checkpoint == str(checkpoint)
    assert restored.pause_step == 4
    assert restored.current_attempt().state == "paused"


def test_load_from_disk_infers_completed_run_from_persisted_events(tmp_path: Path) -> None:
    from lelab.training_metrics import EVENT_PREFIX

    root = tmp_path / "root"
    job_dir = root / "job1"
    (job_dir / "metrics").mkdir(parents=True)
    record = _record(tmp_path)
    record.state = "running"
    record.config = TrainingRequest(dataset_repo_id="a/b", steps=3)
    record.process_pid = 2**31 - 1
    (job_dir / "job.json").write_text(record.model_dump_json(), encoding="utf-8")
    events = []
    for step in (1, 2, 3):
        payload = {
            "kind": "train",
            "run_id": "job1",
            "attempt_id": "a1",
            "timestamp": float(step),
            "step": step,
            "phase": "train",
            "loss": 1.0 / step,
            "lr": 1e-4,
            "grad_norm": 1.0,
        }
        events.append(EVENT_PREFIX + " " + json.dumps(payload))
    (job_dir / "metrics" / "worker-events.jsonl").write_text(
        "\n".join(events) + "\n", encoding="utf-8"
    )
    (job_dir / "stdout.log").write_text("... End of training\n", encoding="utf-8")

    registry = _registry(tmp_path)
    restored = registry.get("job1")
    assert restored.state == "done"
    assert restored.metrics.current_step == 3
    assert restored.metrics.total_steps == 3


def test_job_record_round_trips_attempts(tmp_path: Path) -> None:
    record = _record(tmp_path)
    payload = json.loads(record.model_dump_json())
    assert payload["attempts"][0]["id"] == "a1"
    assert payload["pause_capable"] is True
    restored = JobRecord.model_validate(payload)
    assert restored.current_attempt_id() == "a1"
