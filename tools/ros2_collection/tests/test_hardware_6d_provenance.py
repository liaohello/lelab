"""Locked-chassis hardware metadata must prove six measured joints, not a base."""
from __future__ import annotations

import copy
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ros2_ws/src/lelab_mobile_agent"))
from lelab_mobile_agent import recording_contract as contract

BUNDLE = "a" * 64


def facts():
    caps = {"joint_topic": "/lelab/ros_robot_controller/servo_readback"}
    snapshot = {
        "available": True, "stale_after_s": 0.5, "scan_span_s": 0.02,
        "joint_samples": {
            name: {"name": name, "servo_id": servo, "raw_position": 500,
                   "position_rad": 0.1, "source_kind": "servo_position_readback",
                   "sample_seq": 12, "sample_time_ns": 123456, "age_s": 0.1,
                   "valid": True, "error": None, "calibration_hash": BUNDLE}
            for name, servo in zip(contract.STATE_JOINTS, (1, 2, 3, 4, 5, 10))
        },
    }
    return caps, snapshot


def derive(caps, snapshot, expected=BUNDLE):
    return contract.feedback_provenance_from_adapter(
        caps, snapshot, schema=contract.SCHEMA_ARM_GRIPPER_6DOF,
        expected_calibration_hash=expected)


def test_measured_six_axes_do_not_require_a_fabricated_base():
    result = derive(*facts())
    assert result["trusted"] is True, result
    assert result["base_source_kind"] == "not_in_6d_schema"
    assert result["base_estimate_has_covariance"] is False
    assert result["base_topic"] is None
    assert result["calibration_hash"] == BUNDLE


def test_the_same_facts_remain_insufficient_for_default_nine_dimensions():
    result = contract.feedback_provenance_from_adapter(*facts())
    assert result["trusted"] is False
    assert "base_estimate_missing_or_untrusted" in result["reason"]
    assert "base_covariance_missing" in result["reason"]


@pytest.mark.parametrize("field,value", [
    ("source_kind", "command_cache"), ("source_kind", "synthetic"),
    ("valid", False), ("error", "timeout"), ("age_s", 0.51),
    ("age_s", -0.1), ("age_s", float("nan")),
    ("raw_position", float("inf")), ("position_rad", float("nan")),
    ("calibration_hash", "b" * 64),
])
def test_unqualified_axis_is_not_promoted_to_hardware(field, value):
    caps, snapshot = facts()
    snapshot["joint_samples"]["joint1"][field] = value
    assert derive(caps, snapshot)["trusted"] is False


@pytest.mark.parametrize("patch", [
    {"available": False}, {"stale_after_s": 1.0}, {"scan_span_s": 0.6},
])
def test_invalid_snapshot_is_rejected(patch):
    caps, snapshot = facts()
    snapshot.update(patch)
    assert derive(caps, snapshot)["trusted"] is False


@pytest.mark.parametrize("expected", [None, "unbound", "b" * 64])
def test_the_session_calibration_must_be_bound_and_match(expected):
    assert derive(*facts(), expected=expected)["trusted"] is False


def test_missing_axis_is_not_filled():
    caps, snapshot = facts()
    del snapshot["joint_samples"]["r_joint"]
    assert derive(caps, snapshot)["trusted"] is False


def test_agent_selects_schema_using_live_adapter_and_session_binding():
    from lelab_mobile_agent.agent import MobileAgent

    caps, snapshot = facts()
    agent = object.__new__(MobileAgent)
    agent.runtime = {"export_schema": contract.SCHEMA_ARM_GRIPPER_6DOF,
                     "data_source": "hardware"}
    agent.profile = {"runtime": agent.runtime}
    agent.hybrid_gateway = types.SimpleNamespace(
        driver=types.SimpleNamespace(capabilities=lambda: caps,
                                     read_snapshot=lambda: copy.deepcopy(snapshot)),
        status=lambda: {"calibration_bundle_hash": BUNDLE})
    assert agent._feedback_provenance()["hardware_measurement"] is True
    snapshot["joint_samples"]["joint2"]["source_kind"] = "command_cache"
    assert agent._feedback_provenance()["hardware_measurement"] is False


def test_dds_profile_mirrors_require_the_actual_camera_and_frame_stream():
    files = [ROOT / "configs/ros2_collection/hardware-hybrid-follow-v1-dds.json",
             ROOT / "ros2_ws/src/lelab_mobile_agent/config/hardware-hybrid-follow-v1-dds.json"]
    assert files[0].read_bytes() == files[1].read_bytes()
    profile = json.loads(files[0].read_text())
    required = set(profile["recording"]["required_topics"])
    assert required == {"/ascamera/camera_publisher/rgb0/image",
                        "/ascamera/camera_publisher/rgb0/camera_info",
                        "/lelab/recording/teleop"}
    assert "/controller_manager/joint_states" not in profile["recording"]["topics"]
    assert "/lelab/ros_robot_controller/servo_readback" in profile["recording"]["topics"]
    assert profile["runtime"]["record_joint_state_topic"] is None
