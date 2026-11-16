# Copyright 2026 The LeLab authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""Append-only, cursor-addressable training metric events.

The training worker emits structured single-line JSON events prefixed with
``@@LELAB_METRIC@@`` on stdout. Local and SSH runners already persist every
stdout line to the job's ``log.jsonl``, so these events survive page reloads,
disconnects and LeLab restarts without a second transport.

This module turns those lines (and, for older jobs, the human-readable
``step:... loss:...`` lines) into a persistent sidecar at
``<job_dir>/metrics/events.jsonl``:

* every event gets a monotonically increasing ``event_id`` used as the API
  cursor, so a client can incrementally fetch exactly what it missed;
* the sidecar is append-only and re-readable; a truncated/corrupt trailing
  line is skipped instead of discarding history;
* re-ingesting the same raw line is idempotent (dedup key), which matters when
  the tailer replays ``log.jsonl`` after a restart;
* legacy text metrics are only used when the job has no structured worker
  events, so the same step is never represented twice by two sources.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

EVENT_PREFIX = "@@LELAB_METRIC@@"
SCHEMA_VERSION = 1
_TQDM_STEP_RE = re.compile(r"Training:\s*\d+%[^|]*\|[^|]*\|\s*(\d+)/(\d+)")

EventSource = Literal["worker", "legacy_log"]


class MetricEvent(BaseModel):
    """One persisted monitoring event."""

    event_id: int
    run_id: str | None = None
    attempt_id: str | None = None
    timestamp: float
    kind: str
    step: int | None = None
    phase: str | None = None
    source: EventSource = "worker"
    data: dict[str, Any] = Field(default_factory=dict)


class MetricEventPage(BaseModel):
    events: list[MetricEvent]
    next_cursor: int
    has_more: bool
    run_id: str | None = None


def _decode_marker(message: str) -> dict[str, Any] | None:
    marker = message.find(EVENT_PREFIX)
    if marker < 0:
        return None
    raw = message[marker + len(EVENT_PREFIX) :].strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _legacy_metrics(message: str) -> dict[str, Any] | None:
    """Parse a legacy ``step:... loss:... lr:... grdn:...`` line.

    Only the values carried by that line are returned; the step is the metric's
    own step, never the latest tqdm progress.
    """
    if "step:" not in message or "loss:" not in message:
        return None

    def _value(key: str) -> float | None:
        if key not in message:
            return None
        try:
            token = message.split(key, 1)[1].split()[0].replace(",", "")
            return float(token)
        except (ValueError, IndexError):
            return None

    step_token = message.split("step:", 1)[1].split()[0].replace(",", "")
    try:
        step = int(step_token)
    except ValueError:
        # Current LeRobot logs abbreviate large steps as `11K`. When tqdm is
        # on the same line it carries the exact step; use that instead of
        # plotting the rounded abbreviation as if it were precise.
        progress = _TQDM_STEP_RE.search(message)
        if not step_token.endswith("K") or progress is None:
            return None
        step = int(progress.group(1))
    loss = _value("loss:")
    if step is None or loss is None:
        return None
    return {
        "step": step,
        "loss": loss,
        "lr": _value("lr:"),
        "grad_norm": _value("grdn:"),
    }


def _dedup_key(attempt_id: str | None, payload: dict[str, Any], raw: str) -> str:
    stable = {
        "attempt": attempt_id,
        "kind": payload.get("kind"),
        "step": payload.get("step"),
        "phase": payload.get("phase"),
        "state": payload.get("state"),
        "raw": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode("utf-8")).hexdigest()


class _LogLine(BaseModel):
    timestamp: float
    message: str


class JobMetricsStore:
    """Per-job metric event store. Thread-safe; one instance per job directory."""

    def __init__(self, job_dir: Path) -> None:
        self.job_dir = Path(job_dir)
        self.path = self.job_dir / "metrics" / "events.jsonl"
        #: Worker-written structured events (survive a LeLab restart even if
        #: the stdout tail lost its place).
        self.worker_events_path = self.job_dir / "metrics" / "worker-events.jsonl"
        self._lock = threading.RLock()
        self._events: list[MetricEvent] | None = None
        self._keys: set[str] = set()
        self._next_id = 1
        #: True once this run has emitted any structured worker event. From
        #: then on the human-readable lines are presentation only, so the same
        #: step is never recorded twice from two sources.
        self._worker_mode = False
        self._log_scanned = False
        self._worker_file_scanned = False

    # -- loading / persistence -------------------------------------------------

    def _load_sidecar(self) -> None:
        self._events = []
        self._keys = set()
        self._next_id = 1
        self._worker_mode = False
        if not self.path.is_file():
            return
        with self.path.open(encoding="utf-8", errors="replace") as stream:
            for raw in stream:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = MetricEvent.model_validate_json(raw)
                except Exception:
                    # A partially written last line (crash mid-append) is skipped;
                    # earlier history stays intact.
                    continue
                self._events.append(event)
                if event.source == "worker":
                    self._worker_mode = True
                self._next_id = max(self._next_id, event.event_id + 1)
        for event in self._events:
            payload = {
                "kind": event.kind,
                "step": event.step,
                "phase": event.phase,
                "state": event.data.get("state"),
            }
            raw = json.dumps(event.data, sort_keys=True, default=str)
            self._keys.add(_dedup_key(event.attempt_id, payload, raw))

    def _append(self, event: MetricEvent) -> bool:
        """Append one event to the sidecar. Caller holds the lock."""
        assert self._events is not None
        self._events.append(event)
        self._next_id = event.event_id + 1
        if event.source == "worker":
            self._worker_mode = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(event.model_dump_json() + "\n")
                stream.flush()
        except OSError as exc:  # best effort: the in-memory view stays consistent
            logger.warning("Could not persist metric event to %s: %s", self.path, exc)
        return True

    def ensure_loaded(self, log_path: Path | None = None) -> None:
        with self._lock:
            if self._events is None:
                self._load_sidecar()
            if not self._worker_file_scanned:
                self._worker_file_scanned = True
                if self.worker_events_path.is_file():
                    try:
                        with self.worker_events_path.open(
                            encoding="utf-8", errors="replace"
                        ) as stream:
                            for raw in stream:
                                raw = raw.strip()
                                if not raw:
                                    continue
                                if EVENT_PREFIX in raw:
                                    self.ingest_line(None, None, raw, 0.0)
                    except OSError as exc:
                        logger.warning("Could not read %s: %s", self.worker_events_path, exc)
            if self._log_scanned or log_path is None or not log_path.is_file():
                return
            self._log_scanned = True
            self._scan_log(log_path)

    def _scan_log(self, log_path: Path) -> None:
        """Ingest persisted log lines once, deduplicating against the sidecar."""
        legacy_lines: list[_LogLine] = []
        try:
            with log_path.open(encoding="utf-8", errors="replace") as stream:
                for raw in stream:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        line = _LogLine.model_validate_json(raw)
                    except Exception:
                        continue
                    if EVENT_PREFIX in line.message:
                        self.ingest_line(None, None, line.message, line.timestamp)
                    else:
                        legacy_lines.append(line)
        except OSError as exc:
            logger.warning("Could not scan %s: %s", log_path, exc)
            return
        if self._worker_mode:
            return
        for line in legacy_lines:
            self.ingest_line(None, None, line.message, line.timestamp, allow_legacy=True)

    # -- ingestion -------------------------------------------------------------

    def ingest_line(
        self,
        run_id: str | None,
        attempt_id: str | None,
        message: str,
        timestamp: float,
        *,
        allow_legacy: bool = True,
    ) -> MetricEvent | None:
        """Turn one log line into an event (marker first, legacy text fallback)."""
        with self._lock:
            if self._events is None:
                self._load_sidecar()
            payload = _decode_marker(message)
            if payload is not None:
                kind = str(payload.get("kind") or "unknown")
                data = {key: value for key, value in payload.items() if key not in {"kind", "step", "phase"}}
                step = payload.get("step")
                phase = payload.get("phase")
                try:
                    step_value = int(step) if step is not None else None
                except (TypeError, ValueError):
                    step_value = None
                event_attempt = payload.get("attempt_id") or attempt_id
                event_run = payload.get("run_id") or run_id
                key = _dedup_key(
                    event_attempt,
                    {
                        "kind": kind,
                        "step": step_value,
                        "phase": phase,
                        "state": data.get("state"),
                    },
                    json.dumps(data, sort_keys=True, default=str),
                )
                if key in self._keys:
                    return None
                self._keys.add(key)
                event = MetricEvent(
                    event_id=self._next_id,
                    run_id=event_run,
                    attempt_id=event_attempt,
                    timestamp=float(payload.get("timestamp") or timestamp),
                    kind=kind,
                    step=step_value,
                    phase=str(phase) if phase is not None else None,
                    source="worker",
                    data=data,
                )
                self._append(event)
                return event

            if not allow_legacy or self._worker_mode:
                return None
            metrics = _legacy_metrics(message)
            if metrics is None:
                return None
            step_value = int(metrics.pop("step"))
            key = _dedup_key(
                attempt_id,
                {"kind": "train", "step": step_value, "phase": "train", "state": None},
                json.dumps(metrics, sort_keys=True, default=str),
            )
            if key in self._keys:
                return None
            self._keys.add(key)
            event = MetricEvent(
                event_id=self._next_id,
                run_id=run_id,
                attempt_id=attempt_id,
                timestamp=timestamp,
                kind="train",
                step=step_value,
                phase="train",
                source="legacy_log",
                data=metrics,
            )
            self._append(event)
            return event

    # -- queries ---------------------------------------------------------------

    def events(
        self,
        *,
        after: int = 0,
        limit: int = 500,
        kinds: Iterable[str] | None = None,
        attempt: str | None = None,
    ) -> MetricEventPage:
        with self._lock:
            if self._events is None:
                self._load_sidecar()
            wanted = set(kinds) if kinds else None
            selected: list[MetricEvent] = []
            has_more = False
            for event in self._events:
                if event.event_id <= after:
                    continue
                if wanted is not None and event.kind not in wanted:
                    continue
                if attempt is not None and event.attempt_id != attempt:
                    continue
                if len(selected) >= limit:
                    has_more = True
                    break
                selected.append(event)
            next_cursor = selected[-1].event_id if selected else after
            run_id = self._events[0].run_id if self._events else None
        return MetricEventPage(
            events=selected, next_cursor=next_cursor, has_more=has_more, run_id=run_id
        )

    def all_events(self) -> list[MetricEvent]:
        with self._lock:
            if self._events is None:
                self._load_sidecar()
            return list(self._events)

    def latest(self, kind: str, attempt: str | None = None) -> MetricEvent | None:
        with self._lock:
            if self._events is None:
                self._load_sidecar()
            for event in reversed(self._events):
                if event.kind != kind:
                    continue
                if attempt is not None and event.attempt_id != attempt:
                    continue
                return event
        return None

    def latest_pause_state(self, attempt: str | None = None) -> dict[str, Any] | None:
        event = self.latest("pause", attempt=attempt)
        return event.data if event is not None else None

    def resolved_config(self, attempt: str | None = None) -> dict[str, Any] | None:
        event = self.latest("resolved_config", attempt=attempt)
        return event.data if event is not None else None

    def attempts(self) -> list[str]:
        with self._lock:
            if self._events is None:
                self._load_sidecar()
            seen: list[str] = []
            for event in self._events:
                if event.attempt_id and event.attempt_id not in seen:
                    seen.append(event.attempt_id)
            return seen

    def latest_values(self) -> dict[str, Any]:
        """Latest train/validation/weight-norm values for quick display."""
        with self._lock:
            if self._events is None:
                self._load_sidecar()
            values: dict[str, Any] = {}
            for kind in ("train", "validation", "weight_norm", "pause", "attempt_started"):
                for event in reversed(self._events):
                    if event.kind == kind:
                        values[kind] = {"step": event.step, **event.data}
                        break
        return values


_STORES: dict[str, JobMetricsStore] = {}
_STORES_LOCK = threading.Lock()


def store_for(job_dir: Path) -> JobMetricsStore:
    key = str(Path(job_dir).resolve())
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = JobMetricsStore(Path(job_dir))
            _STORES[key] = store
        return store


def clear_store(job_dir: Path) -> None:
    with _STORES_LOCK, contextlib.suppress(KeyError):
        del _STORES[str(Path(job_dir).resolve())]


__all__ = [
    "EVENT_PREFIX",
    "MetricEvent",
    "MetricEventPage",
    "JobMetricsStore",
    "store_for",
    "clear_store",
]
