"""Deadline scan scheduling and bounded diagnostics for the vendor servo owner.

Pure Python 3.10, ROS-free and serial-free.  The vendor ``ros_robot_controller``
node copies this file next to ``ros_robot_controller_node.py``; the node owns the
serial bus and this module only decides *when* a scan starts and *what* was
observed.  Keeping it separate makes the schedule testable without a board.

Rules the node depends on:

* a scan is scheduled on a fixed grid.  Processing time is subtracted before the
  next wait, and a missed grid slot is dropped rather than replayed in a burst;
* every failed transaction keeps the servo id, command, reason, lock wait, total
  duration, recovery phase, last successful sequence and the scan interval;
* a recovery quiet window may not be shortened below ``QUIET_FLOOR_S`` without a
  separately validated transport reset.  The default stays at the measured 0.2 s.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

SERVO_ID_ORDER = (1, 2, 3, 4, 5, 10)
QUIET_FLOOR_S = 0.2
QUIET_MAX_S = 2.0
PERIOD_MIN_S = 0.05
PERIOD_MAX_S = 2.0


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def validate_period(period_s: float) -> float:
    period = _finite(period_s, "period_s")
    if not PERIOD_MIN_S <= period <= PERIOD_MAX_S:
        raise ValueError(
            f"period_s must be in [{PERIOD_MIN_S}, {PERIOD_MAX_S}], got {period}")
    return period


def validate_quiet(quiet_s: float) -> float:
    """Late replies need a measured quiet window; shortening it needs new evidence."""
    quiet = _finite(quiet_s, "quiet_s")
    if not QUIET_FLOOR_S <= quiet <= QUIET_MAX_S:
        raise ValueError(
            f"quiet_s must be in [{QUIET_FLOOR_S}, {QUIET_MAX_S}]; "
            "a shorter window has no validated transport reset behind it")
    return quiet


def validate_budget(budget_s: float) -> float:
    budget = _finite(budget_s, "transaction_budget_s")
    if not 0.02 <= budget <= 2.0:
        raise ValueError("transaction_budget_s must be in [0.02, 2.0]")
    return budget


class DeadlineSchedule:
    """Fixed-grid scan schedule that never catches up after a missed slot."""

    def __init__(self, period_s: float, *, clock: Callable[[], float] = time.monotonic):
        self.period_s = validate_period(period_s)
        self._clock = clock
        self.next_deadline_s = clock()
        self.last_start_s: float | None = None
        self.last_interval_s: float | None = None
        self.max_interval_s: float | None = None
        self.starts = 0
        self.skipped_slots = 0

    def begin(self, now_s: float | None = None) -> float | None:
        """Record the start of a scan and return the interval since the previous one."""
        now = self._clock() if now_s is None else float(now_s)
        interval = None if self.last_start_s is None else max(0.0, now - self.last_start_s)
        if interval is not None:
            self.last_interval_s = interval
            self.max_interval_s = interval if self.max_interval_s is None \
                else max(self.max_interval_s, interval)
        self.last_start_s = now
        self.starts += 1
        return interval

    def wait_s(self, now_s: float | None = None) -> float:
        now = self._clock() if now_s is None else float(now_s)
        return max(0.0, self.next_deadline_s - now)

    def complete(self, now_s: float | None = None) -> float:
        """Advance one grid slot, skipping every slot already in the past.

        A scan that took longer than its period drops the slots it missed.  The
        next scan starts at the next future grid point, so a slow recovery cannot
        turn into a burst of back-to-back reads.
        """
        now = self._clock() if now_s is None else float(now_s)
        self.next_deadline_s += self.period_s
        if now >= self.next_deadline_s:
            missed = int((now - self.next_deadline_s) // self.period_s) + 1
            self.skipped_slots += missed
            self.next_deadline_s += missed * self.period_s
        return self.next_deadline_s

    def state(self) -> dict[str, Any]:
        return {
            "period_s": self.period_s,
            "starts": self.starts,
            "skipped_slots": self.skipped_slots,
            "last_interval_s": self.last_interval_s,
            "max_interval_s": self.max_interval_s,
        }


class TransactionBudget:
    """A single scan's total wall-clock allowance across serialized transactions."""

    def __init__(self, started_s: float, budget_s: float,
                 *, clock: Callable[[], float] = time.monotonic):
        self.started_s = float(started_s)
        self.budget_s = validate_budget(budget_s)
        self.deadline_s = self.started_s + self.budget_s
        self._clock = clock

    def remaining_s(self, now_s: float | None = None) -> float:
        now = self._clock() if now_s is None else float(now_s)
        return max(0.0, self.deadline_s - now)

    def exhausted(self, now_s: float | None = None) -> bool:
        return self.remaining_s(now_s) <= 0.0


@dataclass
class ScanFailure:
    servo_id: int
    command: str
    reason: str
    lock_wait_ms: float
    duration_ms: float
    recovery_phase: str
    last_success_seq: int | None
    scan_interval_s: float | None
    at_mono_ns: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "servo_id": self.servo_id,
            "command": self.command,
            "reason": self.reason,
            "lock_wait_ms": round(self.lock_wait_ms, 3),
            "duration_ms": round(self.duration_ms, 3),
            "recovery_phase": self.recovery_phase,
            "last_success_seq": self.last_success_seq,
            "scan_interval_s": (None if self.scan_interval_s is None
                                else round(self.scan_interval_s, 4)),
            "at_mono_ns": self.at_mono_ns,
        }


@dataclass
class ScanHealth:
    """Bounded, wire-safe health for one software owner instance."""

    owner_instance: int
    period_s: float
    quiet_s: float
    budget_s: float
    stale_after_s: float = 1.0
    history_limit: int = 8
    scans_started: int = 0
    scans_ok: int = 0
    scans_failed: int = 0
    consecutive_failures: int = 0
    last_success_seq: int | None = None
    last_success_mono_s: float | None = None
    last_success_span_us: int | None = None
    last_scan_interval_s: float | None = None
    max_scan_interval_s: float | None = None
    first_failure: ScanFailure | None = None
    first_failure_session: ScanFailure | None = None
    last_failure: ScanFailure | None = None
    last_recovery: dict[str, Any] | None = None
    recovery_attempts: int = 0
    write_failures: int = 0
    last_write_failure: dict[str, Any] | None = None
    failure_history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.period_s = validate_period(self.period_s)
        self.quiet_s = validate_quiet(self.quiet_s)
        self.budget_s = validate_budget(self.budget_s)
        self.stale_after_s = _finite(self.stale_after_s, "stale_after_s")
        if self.stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive")
        if isinstance(self.owner_instance, bool) or not isinstance(self.owner_instance, int) \
                or self.owner_instance < 1:
            raise ValueError("owner_instance must be a positive integer")
        self.history_limit = max(1, int(self.history_limit))

    def record_scan_start(self, *, interval_s: float | None) -> None:
        self.scans_started += 1
        self.last_scan_interval_s = interval_s
        if interval_s is not None:
            self.max_scan_interval_s = interval_s if self.max_scan_interval_s is None \
                else max(self.max_scan_interval_s, interval_s)

    def record_success(self, *, seq: int, completed_mono_s: float, span_us: int) -> None:
        self.scans_ok += 1
        self.consecutive_failures = 0
        self.first_failure = None
        self.last_success_seq = int(seq)
        self.last_success_mono_s = float(completed_mono_s)
        self.last_success_span_us = int(span_us)

    def record_failure(self, failure: ScanFailure) -> None:
        self.scans_failed += 1
        self.consecutive_failures += 1
        self.last_failure = failure
        if self.first_failure is None:
            self.first_failure = failure
        if self.first_failure_session is None:
            self.first_failure_session = failure
        self.failure_history.append(failure.as_dict())
        if len(self.failure_history) > self.history_limit:
            del self.failure_history[:-self.history_limit]

    def record_recovery(self, *, phase: str, ok: bool, quiet_s: float,
                        duration_ms: float, at_mono_s: float) -> None:
        self.recovery_attempts += 1
        self.last_recovery = {
            "phase": phase,
            "ok": bool(ok),
            "quiet_s": round(validate_quiet(quiet_s), 3),
            "duration_ms": round(duration_ms, 3),
            "at_mono_ns": int(at_mono_s * 1_000_000_000),
        }

    def record_write_failure(self, reason: str) -> None:
        """A bounded write lock timeout is a dropped target, not a scan failure."""
        self.write_failures += 1
        self.last_write_failure = {
            "reason": str(reason)[:160],
            "at_mono_ns": int(time.monotonic() * 1_000_000_000),
        }

    def snapshot(self, now_s: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now_s is None else float(now_s)
        age_s = (None if self.last_success_mono_s is None
                 else max(0.0, now - self.last_success_mono_s))
        return {
            "schema": "lelab.servo_driver_health.v1",
            "owner_instance": self.owner_instance,
            "owner_identity_kind": "software_owner_instance",
            "period_s": self.period_s,
            "transaction_budget_s": self.budget_s,
            "quiet_s": self.quiet_s,
            "stale_after_s": self.stale_after_s,
            "scans_started": self.scans_started,
            "scans_ok": self.scans_ok,
            "scans_failed": self.scans_failed,
            "consecutive_failures": self.consecutive_failures,
            "last_success_seq": self.last_success_seq,
            "last_success_age_s": age_s,
            "last_success_span_us": self.last_success_span_us,
            "last_scan_interval_s": self.last_scan_interval_s,
            "max_scan_interval_s": self.max_scan_interval_s,
            "stale": bool(age_s is None or age_s > self.stale_after_s),
            "first_failure": None if self.first_failure is None else self.first_failure.as_dict(),
            "first_failure_session": (None if self.first_failure_session is None
                                      else self.first_failure_session.as_dict()),
            "last_failure": None if self.last_failure is None else self.last_failure.as_dict(),
            "last_recovery": None if self.last_recovery is None else dict(self.last_recovery),
            "recovery_attempts": self.recovery_attempts,
            "write_failures": self.write_failures,
            "last_write_failure": (None if self.last_write_failure is None
                                   else dict(self.last_write_failure)),
            "failure_history": list(self.failure_history),
            "consumer_note": ("owner counters describe the scan thread; a consumer's "
                              "failed_scans counts invalid received frames, not missed scans"),
        }


def new_scan_health(*, owner_instance: int, period_s: float, quiet_s: float,
                    budget_s: float, stale_after_s: float = 1.0) -> ScanHealth:
    return ScanHealth(owner_instance=owner_instance, period_s=period_s, quiet_s=quiet_s,
                      budget_s=budget_s, stale_after_s=stale_after_s)
