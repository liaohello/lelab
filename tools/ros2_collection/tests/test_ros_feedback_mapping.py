"""Typed feedback mapping: axis order, units, calibration binding and provenance."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "ros2_ws/src/lelab_mobile_agent")]

from lelab_mobile_agent.ros_feedback_mapping import (  # noqa: E402
    JOINTS, SERVO_IDS, clock_epoch, driver_health, measured_frame,
)


def snapshot(*, valid=None, raw=None, errors=None, instance=11, seq=7):
    valid = valid if valid is not None else [True] * 6
    raw = raw if raw is not None else [500, 510, 520, 530, 540, 550]
    errors = errors if errors is not None else [None] * 6
    samples = {}
    for index, name in enumerate(JOINTS):
        samples[name] = {
            "name": name, "servo_id": SERVO_IDS[index],
            "raw_position": raw[index], "position_rad": 0.01 * (index + 1),
            "source_kind": "servo_position_readback",
            "valid": valid[index], "error": errors[index],
            "sample_seq": seq, "calibration_hash": "bundle-1",
            "age_s": 0.05,
        }
    return {
        "joint_samples": samples,
        "scan_span_s": 0.024,
        "last_success_mono": 1000.0,
        "scan_sequence": seq,
        "publisher_instance": instance,
        "owner_health": {
            "owner_instance": instance, "period_s": 0.2, "quiet_s": 0.2,
            "transaction_budget_s": 0.3, "scans_started": 10, "scans_ok": 9,
            "scans_failed": 1, "consecutive_failures": 1, "write_failures": 0,
            "last_success_seq": seq, "last_success_age_s": 0.05,
            "last_scan_interval_s": 0.21, "max_scan_interval_s": 0.42,
            "stale": False,
            "first_failure": {"servo_id": 5, "command": "bus_servo_read_position",
                              "reason": "read_timeout", "lock_wait_ms": 1.2,
                              "duration_ms": 101.0, "recovery_phase": "quiet_wait",
                              "last_success_seq": seq - 1, "scan_interval_s": 0.21,
                              "at_mono_ns": 1000},
            "last_failure": {"servo_id": 5, "command": "bus_servo_read_position",
                             "reason": "read_timeout", "lock_wait_ms": 1.2,
                             "duration_ms": 101.0, "recovery_phase": "quiet_wait",
                             "last_success_seq": seq - 1, "scan_interval_s": 0.21,
                             "at_mono_ns": 1000},
            "last_recovery": {"phase": "quiet_wait", "ok": True, "quiet_s": 0.2,
                              "duration_ms": 205.0, "at_mono_ns": 1001},
        },
        "consumer": {"successful_scans": 9, "failed_scans": 2,
                     "last_error": "readback_payload_invalid",
                     "first_failure": {"reason": "readback_payload_invalid",
                                       "at_mono_ns": 900}},
    }


def test_measured_frame_keeps_axis_order_units_and_identity():
    fields = measured_frame(snapshot(), published_mono_ns=1_000_050_000_000,
                            clock_epoch_value="car_monotonic:abc",
                            calibration_hash="bundle-1",
                            data_identity="simulated_offline")
    assert fields["joint_names"] == list(JOINTS)
    assert fields["servo_ids"] == list(SERVO_IDS)
    assert fields["raw"] == [500, 510, 520, 530, 540, 550]
    assert fields["position_rad"] == [0.01, 0.02, 0.03, 0.04, 0.05, 0.06]
    assert fields["valid"] == [True] * 6
    assert fields["owner_instance"] == 11
    assert fields["scan_sequence"] == 7
    assert fields["scan_span_us"] == 24_000
    assert fields["calibration_hash"] == "bundle-1"
    assert fields["clock_domain"] == "car_process_monotonic"
    assert fields["clock_epoch"] == "car_monotonic:abc"
    assert fields["sample_age_ns"] == 50_000_000
    assert fields["source"] == "ros_robot_controller/servo_readback"
    json.dumps(fields)


def test_a_missing_or_invalid_axis_is_never_a_zero_measurement():
    bad = snapshot(valid=[True, False, True, True, True, True],
                   errors=[None, "sample_invalid", None, None, None, None])
    fields = measured_frame(bad, published_mono_ns=1_000_050_000_000,
                            clock_epoch_value="e", calibration_hash="bundle-1",
                            data_identity="hardware")
    assert fields["valid"][1] is False
    assert "sample_invalid" in fields["errors"][1]
    assert all(fields["valid"][index] for index in (0, 2, 3, 4, 5))
    missing = json.loads(json.dumps(bad))
    del missing["joint_samples"]["joint3"]
    fields = measured_frame(missing, published_mono_ns=1_000_050_000_000,
                            clock_epoch_value="e", calibration_hash="bundle-1",
                            data_identity="hardware")
    assert fields["valid"][2] is False
    assert fields["errors"][2] == "sample_missing"


def test_driver_health_separates_owner_and_consumer_failures():
    fields = driver_health(snapshot(), now_mono_ns=1_000_200_000_000,
                           clock_epoch_value="e", data_identity="simulated_offline")
    assert fields["owner_instance"] == 11
    assert fields["scans_ok"] == 9 and fields["scans_failed"] == 1
    assert fields["first_failure"]["present"] is True
    assert fields["first_failure"]["servo_id"] == 5
    assert fields["last_recovery_phase"] == "quiet_wait"
    assert fields["consumer_successful_scans"] == 9
    assert fields["consumer_failed_scans"] == 2
    assert fields["consumer_first_failure_present"] is True
    assert fields["consumer_first_failure_reason"] == "readback_payload_invalid"
    assert fields["last_success_age_s"] == 0.05
    json.dumps(fields)


def test_clock_epoch_changes_with_the_process_start():
    first = clock_epoch("boot-1", 100)
    assert first.startswith("car_monotonic:")
    assert first != clock_epoch("boot-1", 101)
    assert first != clock_epoch("boot-2", 100)
    assert len(first.split(":")[1]) == 16
