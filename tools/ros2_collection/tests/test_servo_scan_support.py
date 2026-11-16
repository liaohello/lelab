"""Pure tests for the vendor owner's deadline schedule and diagnostics.

The module under test is loaded from the versioned patch directory, so these
tests fail if the shipped patch copy drifts from what the offline checks expect.
No ROS, no serial port and no real sleep.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SUPPORT = (Path(__file__).resolve().parents[1]
           / "vendor_patches" / "ros" / "servo_scan_support.py")


def _load():
    spec = importlib.util.spec_from_file_location("servo_scan_support_under_test", SUPPORT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = _load()


def test_a_scan_is_scheduled_on_the_grid_without_a_fixed_post_scan_sleep():
    schedule = module.DeadlineSchedule(0.2)
    schedule.next_deadline_s = 0.0
    assert schedule.wait_s(0.0) == pytest.approx(0.0)
    schedule.begin(0.0)
    # 50 ms of work is subtracted from the wait: no extra 200 ms sleep.
    assert schedule.complete(0.05) == pytest.approx(0.2)
    assert schedule.wait_s(0.05) == pytest.approx(0.15)
    assert schedule.skipped_slots == 0


def test_a_missed_slot_is_dropped_not_replayed_in_a_burst():
    schedule = module.DeadlineSchedule(0.2)
    schedule.next_deadline_s = 0.0
    schedule.begin(0.0)
    # A 350 ms scan missed the 200 ms slot; the next start is the 400 ms grid point.
    assert schedule.complete(0.35) == pytest.approx(0.4)
    assert schedule.skipped_slots == 1
    assert schedule.wait_s(0.35) == pytest.approx(0.05)


def test_a_long_recovery_drops_every_slot_it_crossed():
    schedule = module.DeadlineSchedule(0.2)
    schedule.next_deadline_s = 0.0
    schedule.begin(0.0)
    assert schedule.complete(0.65) == pytest.approx(0.8)
    assert schedule.skipped_slots == 3
    assert schedule.wait_s(0.65) == pytest.approx(0.15)


def test_interval_metrics_are_measured_between_scan_starts():
    schedule = module.DeadlineSchedule(0.5)
    schedule.next_deadline_s = 0.0
    assert schedule.begin(10.0) is None
    assert schedule.begin(10.52) == pytest.approx(0.52)
    assert schedule.begin(11.01) == pytest.approx(0.49)
    assert schedule.max_interval_s == pytest.approx(0.52)
    assert schedule.state()["starts"] == 3


@pytest.mark.parametrize("quiet", [0.0, 0.05, 0.19, 2.01])
def test_the_quiet_window_cannot_be_shortened_without_new_evidence(quiet):
    with pytest.raises(ValueError):
        module.validate_quiet(quiet)


def test_the_measured_default_quiet_window_is_accepted():
    assert module.validate_quiet(0.2) == pytest.approx(0.2)
    assert module.validate_quiet(0.4) == pytest.approx(0.4)


def test_transaction_budget_is_a_hard_remaining_time():
    budget = module.TransactionBudget(10.0, 0.12)
    assert budget.remaining_s(10.03) == pytest.approx(0.09)
    assert not budget.exhausted(10.11)
    assert budget.exhausted(10.12)
    assert budget.remaining_s(10.5) == 0.0


def test_health_keeps_the_first_failure_of_an_outage_and_the_last_one():
    health = module.new_scan_health(owner_instance=77, period_s=0.2, quiet_s=0.2,
                                    budget_s=0.18, stale_after_s=1.0)
    health.record_scan_start(interval_s=None)
    health.record_success(seq=4, completed_mono_s=100.0, span_us=24_000)
    first = module.ScanFailure(
        servo_id=5, command="bus_servo_read_position", reason="read_timeout",
        lock_wait_ms=1.2, duration_ms=101.0, recovery_phase="quiet_wait",
        last_success_seq=4, scan_interval_s=0.21, at_mono_ns=101_000_000_000)
    second = module.ScanFailure(
        servo_id=10, command="bus_servo_read_position", reason="read_timeout",
        lock_wait_ms=0.8, duration_ms=100.5, recovery_phase="quiet_wait",
        last_success_seq=4, scan_interval_s=0.42, at_mono_ns=102_000_000_000)
    health.record_scan_start(interval_s=0.21)
    health.record_failure(first)
    health.record_scan_start(interval_s=0.42)
    health.record_failure(second)
    assert health.first_failure is first
    assert health.last_failure is second
    assert health.consecutive_failures == 2
    assert health.last_success_seq == 4
    assert health.max_scan_interval_s == pytest.approx(0.42)
    health.record_recovery(phase="quiet_wait", ok=False, quiet_s=0.2,
                           duration_ms=205.0, at_mono_s=102.2)
    snapshot = health.snapshot(now_s=102.3)
    assert snapshot["first_failure"]["servo_id"] == 5
    assert snapshot["last_failure"]["servo_id"] == 10
    assert snapshot["last_failure"]["recovery_phase"] == "quiet_wait"
    assert snapshot["last_failure"]["last_success_seq"] == 4
    assert snapshot["last_recovery"]["ok"] is False
    assert snapshot["last_success_age_s"] == pytest.approx(2.3)
    assert snapshot["stale"] is True
    json.dumps(snapshot)


def test_a_success_clears_only_the_current_outage_and_keeps_the_session_first():
    health = module.new_scan_health(owner_instance=9, period_s=0.2, quiet_s=0.2,
                                    budget_s=0.18)
    failure = module.ScanFailure(
        servo_id=1, command="bus_servo_read_position", reason="bad_reply",
        lock_wait_ms=0.0, duration_ms=100.0, recovery_phase="quiet_wait",
        last_success_seq=None, scan_interval_s=None, at_mono_ns=1)
    health.record_failure(failure)
    health.record_success(seq=0, completed_mono_s=1.1, span_us=22_000)
    health.record_failure(failure)
    assert health.first_failure is failure
    assert health.first_failure_session is failure
    assert health.consecutive_failures == 1


def test_failure_history_is_bounded():
    health = module.new_scan_health(owner_instance=1, period_s=0.2, quiet_s=0.2,
                                    budget_s=0.18)
    for index in range(20):
        health.record_failure(module.ScanFailure(
            servo_id=1, command="bus_servo_read_position", reason=f"r{index}",
            lock_wait_ms=0.0, duration_ms=100.0, recovery_phase="quiet_wait",
            last_success_seq=None, scan_interval_s=None, at_mono_ns=index))
    assert len(health.failure_history) == health.history_limit
    assert health.failure_history[-1]["reason"] == "r19"
