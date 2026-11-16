"""The ROS status WebSocket snapshot: ages, rate, recovery, first failure, stop receipt."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "app_ros_mobile")]

import lelab.ros_mobile  # noqa: E402
from lelab.ros_status_stream import _RateTracker, collect_ros_status  # noqa: E402


class Observer:
    def __init__(self):
        self.dds_snapshot = {
            "health": {
                "owner_instance": 11, "scans_ok": 100, "scans_failed": 2,
                "consecutive_failures": 0, "stale": False,
                "max_scan_interval_s": 0.31,
                "last_recovery_phase": "quiet_wait", "last_recovery_ok": True,
                "last_recovery_quiet_s": 0.2, "last_recovery_duration_ms": 205.0,
                "last_recovery_mono_ns": 123,
                "first_failure": {"present": True, "servo_id": 5,
                                  "reason": "read_timeout"},
                "last_failure": {"present": True, "servo_id": 5,
                                 "reason": "read_timeout"},
                "consumer_failed_scans": 1,
                "consumer_last_error": "readback_payload_invalid",
            },
            "window": {"window_id": 9, "stream_epoch": 3,
                       "remaining_ms_conservative": 180.0,
                       "command_transport_ready": True},
            "receipt": {"state": "stopped", "reason": "paused_by_request",
                        "frame_seq": 12},
        }

    def cache_state(self):
        return {"feedback_source": "dds", "cache_age_s": 0.05,
                "clock_mapping": {"ready": True, "error_bound_ns": 50_000_000},
                "dds": {"counts": {"polls": 10}, "last_error": None},
                "comparison": {"max_abs_raw_diff": 0}}


class Facade:
    def __init__(self):
        self.observer = Observer()
        self._facts = {
            "feedback": {
                "valid": True, "reason": "ok", "oldest_age_s": 0.05,
                "scan_sequence": 1000, "scan_span_s": 0.024,
                "publisher_instance": 11,
                "owner_health": {"scans_ok": 100, "scans_failed": 2},
            }
        }
        self.session_id = "sess-1"
        self.session_epoch = 3
        self.state = "ready"
        self.arm_state = "following"
        self.transport = "dds_v1"
        self.estop_latched = False
        self.last_error = None
        self._last_stop = {"state": "confirmed", "reason": "operator"}


def test_the_snapshot_carries_the_fields_the_page_shows(monkeypatch):
    facade = Facade()
    monkeypatch.setattr(lelab.ros_mobile, "get_hybrid_facade", lambda: facade)
    status = collect_ros_status()
    assert status["available"] is True
    assert status["transport"] == "dds_v1"
    assert status["feedback_source"] == "dds"
    assert status["source_scan_age_s"] == 0.05
    assert status["cache_age_s"] == 0.05
    assert status["sample_rate_hz"] is None or isinstance(status["sample_rate_hz"], float)
    assert status["recovery"]["phase"] == "quiet_wait"
    assert status["first_failure"]["reason"] == "read_timeout"
    assert status["stop_receipt"]["reason"] == "paused_by_request"
    assert status["window"]["remaining_ms"] == 180.0
    assert status["consumer"]["failed_scans"] == 1
    assert status["owner_health"]["scans_failed"] == 2


def test_a_facade_failure_is_a_status_not_an_exception(monkeypatch):
    def broken():
        raise RuntimeError("no facade")

    monkeypatch.setattr(lelab.ros_mobile, "get_hybrid_facade", broken)
    status = collect_ros_status()
    assert status["available"] is False
    assert "facade_unavailable" in status["reason"]


def test_the_rate_tracker_reports_a_positive_rate():
    tracker = _RateTracker()
    assert tracker.observe(100, 10.0) is None
    assert tracker.observe(105, 10.5) == 10.0
    assert tracker.observe(110, 11.0) == 10.0
    assert tracker.observe(None, 11.5) is None
    assert tracker.observe(99, 12.0) is None  # sequence below the window start
