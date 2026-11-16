"""Observation-domain configuration never expands unsigned position commands."""
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ros2_ws/src/lelab_mobile_agent"))
from lelab_mobile_agent import servo_feedback_limits as limits
from test_ros_servo_output import (Clock, Node, Position, Positions, Publisher, RAW,
                                   Readback, RosServoOutputDriver, rad_to_raw,
                                   RADIANS_PER_ENCODER_TICK)


def ranges():
    return {name: [-50 if name == "joint3" else 0, 1000] for name in limits.JOINTS}


def test_versioned_atomic_save_keeps_history_and_rejects_stale_edits(tmp_path):
    path = tmp_path / "limits.json"
    first = limits.load(path, "car")
    edited = ranges()
    edited["joint1"] = [10, 990]
    saved = limits.save(path, "car", edited, first["version"])
    assert limits.load(path, "car") == saved
    assert saved["version"] != first["version"]
    assert len(list((tmp_path / "limits.versions").glob("*.json"))) == 2
    with pytest.raises(ValueError, match="version_conflict"):
        limits.save(path, "car", edited, first["version"])
    assert limits.load(path, "car") == saved
    with pytest.raises(ValueError, match="identity_or_hash"):
        limits.load(path, "other_device_profile")


def test_legacy_profile_only_file_is_migrated_to_a_device_bound_version(tmp_path):
    path = tmp_path / "limits.json"
    legacy = limits._legacy_document(ranges(), "car")
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = limits.load(path, "car")
    migrated = limits.save(path, "car", ranges(), loaded["version"])

    assert loaded["binding_status"] == "legacy_profile_only_upgrade_on_save"
    assert migrated["binding_status"] == "device_bound"
    assert migrated["version"] != legacy["version"]
    persisted = json.loads(path.read_text())
    assert persisted["device_identity"]["joint_order"] == list(limits.JOINTS)
    assert persisted["device_identity"]["servo_ids"] == limits.SERVO_IDS
    assert limits.load(path, "car")["version"] == migrated["version"]


@pytest.mark.parametrize("joint,pair", [
    ("joint3", [-51, 1000]), ("joint1", [-1, 1000]),
    ("joint3", [-50, 1001]), ("joint3", [True, 1000]),
    ("joint3", [50, 50]), ("joint3", [float("nan"), 1000]),
])
def test_invalid_configuration_rejected(joint, pair):
    values = ranges(); values[joint] = pair
    with pytest.raises(ValueError): limits.document(values, "car")


def test_driver_accepts_configured_negative_readback_but_never_sends_negative_hold(tmp_path):
    path = tmp_path / "limits.json"
    saved = limits.save(path, "car", ranges(), limits.load(path, "car")["version"])
    publisher = Publisher()
    driver = RosServoOutputDriver(
        Node(), publisher, servos_position_type=Positions, servo_position_type=Position,
        readback=Readback({**RAW, "joint3": -50}),
        limits_rad={name: (-2.5, 2.5) for name in RAW}, clock=Clock(),
        feedback_limits_path=path, feedback_limits_profile="car")
    driver.enable()
    assert driver.outputs_enabled
    assert driver.capabilities()["feedback_raw_limits"]["version"] == saved["version"]
    assert not publisher.messages
    result = driver.stop("test")
    assert result["hold_published"] is False
    assert not publisher.messages
    with pytest.raises(ValueError, match="pulse range"):
        rad_to_raw("joint3", (-50 - 500) * RADIANS_PER_ENCODER_TICK)


def test_agent_configuration_requires_token_and_stopped_unbound_emitter(tmp_path):
    from lelab_mobile_agent.agent import MobileAgent, ApiError
    agent = object.__new__(MobileAgent)
    agent.lock = threading.RLock()
    agent.read_only = False
    agent.frame_transport = True
    agent.hybrid_control_token = None
    agent.frame_outputs_enabled = False
    agent.sessions = SimpleNamespace(active=False)
    agent.control_mode = "idle"
    agent.profile = {"profile": "car"}
    agent.control_status_path = str(tmp_path / "control.json")
    Path(agent.control_status_path).write_text(json.dumps({"outputs_enabled": False}))
    agent._check_token = lambda token: None if token == "owner" else (_ for _ in ()).throw(ApiError(403, "token", "bad"))
    agent._emit = lambda *a, **kw: None
    body = {"ranges": ranges(), "expected_version": agent.feedback_limits_settings()["version"]}
    with pytest.raises(ApiError): agent.save_feedback_limits("stranger", body)
    agent.hybrid_control_token = "active_session"
    with pytest.raises(ApiError, match="release"): agent.save_feedback_limits("owner", body)
    agent.hybrid_control_token = None
    Path(agent.control_status_path).write_text(json.dumps({"outputs_enabled": True}))
    with pytest.raises(ApiError): agent.save_feedback_limits("owner", body)
    Path(agent.control_status_path).write_text(json.dumps({"outputs_enabled": False}))
    saved = agent.save_feedback_limits("owner", body)
    assert saved["saved"] and saved["motion_granted"] is False
    assert agent.feedback_limits_settings()["ranges"]["joint3"] == [-50, 1000]


def test_recording_requires_the_driver_to_have_loaded_the_saved_device_bound_version(tmp_path):
    from lelab_mobile_agent.agent import MobileAgent, ApiError
    path = tmp_path / "feedback-raw-limits.json"
    saved = limits.save(path, "car", ranges(), limits.load(path, "car")["version"])
    control_path = tmp_path / "control.json"
    agent = object.__new__(MobileAgent)
    agent.frame_transport = True
    agent.read_only = False
    agent.profile = {"profile": "car"}
    agent.control_status_path = str(control_path)

    control_path.write_text(json.dumps({"control": {
        "feedback_raw_limits": saved,
    }}), encoding="utf-8")
    assert agent._active_feedback_limits()["version"] == saved["version"]
    assert agent._assert_feedback_limits_recording_ready()["sha256"] == saved["sha256"]

    older = {**saved, "version": "old-version"}
    control_path.write_text(json.dumps({"control": {
        "feedback_raw_limits": older,
    }}), encoding="utf-8")
    with pytest.raises(ApiError) as caught:
        agent._assert_feedback_limits_recording_ready()
    assert caught.value.reason == "feedback_limits_reenable_required"
