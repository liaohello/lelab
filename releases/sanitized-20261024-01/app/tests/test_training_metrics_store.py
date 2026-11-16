"""Tests for the append-only metric event store."""

from __future__ import annotations

import json
from pathlib import Path

from lelab.training_metrics import EVENT_PREFIX, JobMetricsStore, store_for


def _marker(payload: dict) -> str:
    return EVENT_PREFIX + " " + json.dumps(payload, separators=(",", ":"))


def _train(step: int, loss: float, attempt: str = "a1", **extra) -> str:
    return _marker(
        {
            "v": 1,
            "kind": "train",
            "run_id": "run",
            "attempt_id": attempt,
            "timestamp": 100.0 + step,
            "pid": 1,
            "step": step,
            "phase": "train",
            "loss": loss,
            "lr": 1e-4,
            "grad_norm": 2.5,
            **extra,
        }
    )


def test_structured_events_keep_their_own_step_and_attempt(tmp_path: Path) -> None:
    store = JobMetricsStore(tmp_path)
    store.ingest_line("run", "a1", _train(5, 1.0), 105.0)
    store.ingest_line("run", "a1", _train(10, 0.5), 110.0)
    store.ingest_line(
        "run",
        "a2",
        _train(10, 0.4, attempt="a2"),
        111.0,
    )
    events = store.all_events()
    assert [(event.step, event.attempt_id, event.data["loss"]) for event in events] == [
        (5, "a1", 1.0),
        (10, "a1", 0.5),
        (10, "a2", 0.4),
    ]
    # The same step in a new attempt is preserved, not overwritten.
    assert len({event.event_id for event in events}) == 3


def test_worker_marker_suppresses_legacy_text_lines(tmp_path: Path) -> None:
    store = JobMetricsStore(tmp_path)
    store.ingest_line("run", "a1", _marker({"kind": "resolved_config", "optimizer": {}}), 1.0)
    legacy = "INFO step:5 smpl:10 loss:1.234 grdn:2.0 lr:0.0001"
    store.ingest_line("run", "a1", legacy, 2.0)
    store.ingest_line("run", "a1", _train(5, 1.2), 3.0)
    events = store.all_events()
    assert [event.source for event in events] == ["worker", "worker"]
    assert events[-1].data["loss"] == 1.2


def test_legacy_log_fallback_uses_line_step(tmp_path: Path) -> None:
    store = JobMetricsStore(tmp_path)
    store.ingest_line("run", "a1", "Training:  10%|# | 5/50 [00:01<00:09]", 1.0)
    store.ingest_line("run", "a1", "INFO step:5 smpl:10 loss:1.234 grdn:2.0 lr:0.0001", 2.0)
    store.ingest_line("run", "a1", "Training:  20%|## | 10/50 [00:02<00:08]", 3.0)
    events = store.all_events()
    assert len(events) == 1
    event = events[0]
    assert event.source == "legacy_log"
    assert event.step == 5
    assert event.data == {"loss": 1.234, "lr": 0.0001, "grad_norm": 2.0}


def test_sidecar_reload_is_idempotent(tmp_path: Path) -> None:
    store = JobMetricsStore(tmp_path)
    store.ingest_line("run", "a1", _train(1, 1.0), 1.0)
    store.ingest_line("run", "a1", _train(1, 1.0), 1.0)  # same raw line twice
    assert len(store.all_events()) == 1
    reopened = JobMetricsStore(tmp_path)
    assert len(reopened.all_events()) == 1
    reopened.ingest_line("run", "a1", _train(1, 1.0), 1.0)
    assert len(reopened.all_events()) == 1
    reopened.ingest_line("run", "a1", _train(2, 0.9), 2.0)
    assert [event.step for event in reopened.all_events()] == [1, 2]


def test_corrupt_trailing_line_keeps_earlier_history(tmp_path: Path) -> None:
    store = JobMetricsStore(tmp_path)
    store.ingest_line("run", "a1", _train(1, 1.0), 1.0)
    with store.path.open("a", encoding="utf-8") as stream:
        stream.write('{"event_id": 999, "broken": ')
    reopened = JobMetricsStore(tmp_path)
    assert [event.step for event in reopened.all_events()] == [1]


def test_cursor_pagination(tmp_path: Path) -> None:
    store = JobMetricsStore(tmp_path)
    for step in range(1, 8):
        store.ingest_line("run", "a1", _train(step, 1.0 / step), float(step))
    first = store.events(after=0, limit=3)
    assert [event.step for event in first.events] == [1, 2, 3]
    assert first.has_more is True
    second = store.events(after=first.next_cursor, limit=10)
    assert [event.step for event in second.events] == [4, 5, 6, 7]
    assert second.has_more is False


def test_kind_filter_and_latest_values(tmp_path: Path) -> None:
    store = JobMetricsStore(tmp_path)
    store.ingest_line("run", "a1", _train(1, 1.0), 1.0)
    store.ingest_line(
        "run",
        "a1",
        _marker(
            {
                "v": 1,
                "kind": "validation",
                "run_id": "run",
                "attempt_id": "a1",
                "timestamp": 2.0,
                "step": 2,
                "phase": "validation",
                "loss": 0.25,
            }
        ),
        2.0,
    )
    validation = store.events(after=0, limit=10, kinds=["validation"])
    assert [event.step for event in validation.events] == [2]
    latest = store.latest_values()
    assert latest["validation"]["loss"] == 0.25
    assert latest["train"]["loss"] == 1.0


def test_log_scan_rebuilds_from_log_jsonl(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    lines = [
        {"timestamp": 1.0, "message": _marker({"kind": "resolved_config", "steps": 10})},
        {"timestamp": 2.0, "message": _train(3, 0.75)},
        {"timestamp": 3.0, "message": "INFO step:3 loss:0.75 lr:0.0001"},
        {"timestamp": 4.0, "message": "Training: 10%|# | 3/10 [00:01<00:02]"},
    ]
    log_path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    store = store_for(tmp_path)
    store.ensure_loaded(log_path)
    events = store.all_events()
    assert [event.kind for event in events] == ["resolved_config", "train"]
    assert events[-1].step == 3


def test_legacy_job_without_markers_scans_text(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    lines = [
        {"timestamp": 1.0, "message": "Training: 10%|# | 3/10 [00:01<00:02]"},
        {"timestamp": 2.0, "message": "INFO step:3 smpl:6 loss:0.75 grdn:1.5 lr:0.0001"},
        {"timestamp": 3.0, "message": "INFO step:6 smpl:12 loss:0.50 grdn:1.2 lr:0.0001"},
    ]
    log_path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    store = JobMetricsStore(tmp_path)
    store.ensure_loaded(log_path)
    events = store.all_events()
    assert [event.step for event in events] == [3, 6]
    assert all(event.source == "legacy_log" for event in events)
