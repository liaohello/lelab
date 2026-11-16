"""Clock mapping and DDS feedback conversion, without ROS or a running bridge."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "app_ros_mobile")]

from lelab.hybrid_dds_feedback import (  # noqa: E402
    BRIDGE_SCHEMA, ClockMapping, compare_sources, feedback_from_snapshot,
)

JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]
SERVO_IDS = [1, 2, 3, 4, 5, 10]


def bridge_snapshot(*, published_car_ns=500_000_000, sample_end_car_ns=490_000_000,
                    epoch="car_monotonic:abc", calibration="bundle-1",
                    raw=None, valid=None, errors=None, receipt_mono_ns=None):
    raw = raw or [500, 510, 520, 530, 540, 550]
    valid = valid or [True] * 6
    errors = errors or [""] * 6
    return {
        "schema": BRIDGE_SCHEMA,
        "ok": True,
        "bridge": {"pid": 1, "clock_epoch": "upper-1", "data_identity": "simulated_offline"},
        "feedback": {
            "source": "ros_robot_controller/servo_readback",
            "data_identity": "simulated_offline",
            "frame_format": 2,
            "owner_instance": 11,
            "scan_sequence": 42,
            "scan_span_us": 24_000,
            "sample_start_mono_ns": sample_end_car_ns - 24_000_000,
            "sample_end_mono_ns": sample_end_car_ns,
            "published_mono_ns": published_car_ns,
            "sample_age_ns": published_car_ns - sample_end_car_ns,
            "clock_domain": "car_process_monotonic",
            "clock_epoch": epoch,
            "calibration_hash": calibration,
            "joint_names": JOINTS,
            "servo_ids": SERVO_IDS,
            "raw": raw,
            "position_rad": [0.01, 0.02, 0.03, 0.04, 0.05, 0.06],
            "valid": valid,
            "errors": errors,
            "receipt_mono_ns": receipt_mono_ns or 1_000_000_000,
            "receipt_age_ns": 1_000_000,
        },
        "counts": {"feedback": 1, "health": 1},
    }


class Clock:
    def __init__(self, value=1_000_000_000):
        self.value = value

    def __call__(self):
        return self.value


def test_age_is_an_upper_bound_with_the_configured_error_floor():
    clock = Clock()
    mapping = ClockMapping(error_floor_ns=50_000_000, clock_ns=clock)
    snapshot = bridge_snapshot()
    feedback = feedback_from_snapshot(snapshot, mapping,
                                      expected_calibration_hash="bundle-1")
    assert feedback["available"] is True
    assert feedback["reason"] == "ok"
    assert feedback["age_source"] == "car_publish_age_plus_bounded_transport"
    # car publish age 10 ms + default transport bound 150 ms; scan span 24 ms added.
    assert feedback["age_bound_s"] == pytest.approx(0.16, abs=0.005)
    assert feedback["oldest_age_s"] == pytest.approx(0.184, abs=0.005)
    for name in JOINTS:
        assert feedback["joint_samples"][name]["age_s"] >= 0.18


def test_the_transport_bound_is_explicit_and_configurable():
    mapping = ClockMapping(clock_ns=Clock())
    feedback = feedback_from_snapshot(bridge_snapshot(), mapping,
                                      expected_calibration_hash="bundle-1",
                                      transport_bound_s=0.05)
    assert feedback["transport_bound_s"] == 0.05
    assert feedback["age_bound_s"] == pytest.approx(0.06, abs=0.005)
    assert feedback["available"] is True


def test_a_calibration_mismatch_disables_the_provider():
    mapping = ClockMapping()
    feedback = feedback_from_snapshot(bridge_snapshot(calibration="other"),
                                      mapping, expected_calibration_hash="bundle-1")
    assert feedback["available"] is False
    assert "calibration_mismatch" in feedback["reason"]


def test_a_stale_measurement_is_not_revived_by_a_receipt_timestamp():
    clock = Clock()
    mapping = ClockMapping(error_floor_ns=10_000_000, clock_ns=clock)
    # The frame was measured 2 seconds before it was published.
    snapshot = bridge_snapshot(sample_end_car_ns=100_000_000,
                               published_car_ns=2_100_000_000,
                               receipt_mono_ns=1_000_000_000)
    clock.value = 2_100_000_000
    feedback = feedback_from_snapshot(snapshot, mapping,
                                      expected_calibration_hash="bundle-1")
    assert feedback["available"] is False
    assert "feedback_stale" in feedback["reason"]
    assert feedback["age_bound_s"] > 2.0


def test_a_clock_epoch_change_resets_the_offset_estimate():
    clock = Clock()
    mapping = ClockMapping(clock_ns=clock)
    feedback_from_snapshot(bridge_snapshot(), mapping,
                           expected_calibration_hash="bundle-1")
    first = mapping.state()
    clock.value += 1_000_000_000
    feedback_from_snapshot(bridge_snapshot(epoch="car_monotonic:def"), mapping,
                           expected_calibration_hash="bundle-1")
    second = mapping.state()
    assert first["epoch"] == "car_monotonic:abc"
    assert second["epoch"] == "car_monotonic:def"
    assert second["observations"] == 1
    assert second["reset_reason"].startswith("clock_epoch_changed")


def test_one_delayed_dds_sample_does_not_poison_the_clock_bound():
    clock = Clock()
    mapping = ClockMapping(error_floor_ns=50_000_000, clock_ns=clock)
    base_car = 10_000_000_000
    base_upper = 20_000_000_000
    for index in range(20):
        car = base_car + index * 1_000_000
        mapping.observe(
            car_epoch="car_monotonic:abc",
            car_publish_ns=car,
            upper_receive_ns=base_upper + index * 1_000_000 + 1_000_000,
        )
    mapping.observe(
        car_epoch="car_monotonic:abc",
        car_publish_ns=base_car + 21_000_000,
        upper_receive_ns=base_upper + 3_024_000_000,
    )

    state = mapping.state()

    assert state["offset_max_ns"] - state["offset_min_ns"] >= 2_000_000_000
    assert state["offset_lower_decile_ns"] == state["offset_min_ns"]
    assert state["error_bound_ns"] == 50_000_000
    assert state["ready"] is True


def test_a_missing_car_publish_age_disables_the_provider():
    snapshot = bridge_snapshot()
    del snapshot["feedback"]["sample_age_ns"]
    feedback = feedback_from_snapshot(snapshot, ClockMapping(),
                                      expected_calibration_hash="bundle-1")
    assert feedback["available"] is False
    assert "dds_age_unbounded" in feedback["reason"]
    assert feedback["age_bound_s"] is None


def test_missing_feedback_is_reported_instead_of_filled():
    feedback = feedback_from_snapshot({}, ClockMapping(),
                                      expected_calibration_hash="bundle-1")
    assert feedback["available"] is False
    assert feedback["reason"] == "dds_feedback_missing"
    assert feedback["joint_samples"] == {}


def test_compare_reports_raw_differences_and_identity():
    http = {"available": True, "publisher_instance": 11, "scan_sequence": 42,
            "calibration_hash": "bundle-1",
            "joint_samples": {name: {"raw_position": 500 + index}
                              for index, name in enumerate(JOINTS)}}
    dds = {"available": True, "publisher_instance": 11, "scan_sequence": 42,
           "calibration_hash": "bundle-1",
           "joint_samples": {name: {"raw_position": 501 + index}
                             for index, name in enumerate(JOINTS)}}
    result = compare_sources(http, dds, {"counts": {"feedback": 3}})
    assert result["max_abs_raw_diff"] == 1
    assert result["http_publisher_instance"] == result["dds_publisher_instance"]
    assert result["bridge_counts"] == {"feedback": 3}
