"""Wave C car-side integration: real phase route, recorder timeline and event facts.

All hardware, ROS topics and adapters here are fixtures.  A loopback HTTP server
is started only for the duration of each test so the production AgentHandler
route is exercised rather than a copied dispatcher.
"""
from __future__ import annotations

import copy
import http.client
import json
import sys
import threading
import time
import types
from collections import deque
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ros2_ws" / "src" / "lelab_mobile_agent"))

from lelab_mobile_agent import recorder as recorder_module  # noqa: E402
from lelab_mobile_agent import recording_contract as contract  # noqa: E402
from lelab_mobile_agent.agent import AgentHTTPServer, MobileAgent  # noqa: E402
from lelab_mobile_agent.contract import BaseCommand  # noqa: E402

SESSIONS = ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint")
SIM_BUNDLE = "simulation:hybrid_manual_sim_v1"
PROFILE_PATHS = (
    ROOT / "configs/ros2_collection/hardware-hybrid-manual-v1.json",
    ROOT / "ros2_ws/src/lelab_mobile_agent/config/hardware-hybrid-manual-v1.json",
)
SIM_PROFILE_PATHS = (
    ROOT / "configs/ros2_collection/hybrid_manual_sim_v1.json",
    ROOT / "ros2_ws/src/lelab_mobile_agent/config/hybrid_manual_sim_v1.json",
)


class FakeBagProcess:
    pid = 2 ** 31 - 1
    returncode = 0

    def __init__(self):
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def wait(self, timeout=None):
        self.stopped = True
        return 0


class SilentLog:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class FakeDriver:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.capabilities_value = {
            "outputs_enabled": False,
            "joint_topic": "/fixture/servo_readback",
            "base_topic": "/fixture/base_estimate",
        }

    def capabilities(self):
        return self.capabilities_value

    def read_snapshot(self):
        return self.snapshot


def v1_command(channel, seq, *, epoch=909, bundle=SIM_BUNDLE):
    target = {
        "base": {"vx_m_s": 0.1, "vy_m_s": 0.0, "wz_rad_s": 0.0},
        "arm": {
            "joint_names": list(SESSIONS[:5]),
            "positions_rad": [0.1, 0.2, 0.3, 0.4, 0.5],
        },
        "gripper": {"joint_name": "r_joint", "position_rad": -0.2},
    }[channel]
    envelope = {
        "protocol_version": 1,
        "session_id": "hybrid-sim-session",
        "session_epoch": epoch,
        "channel": channel,
        "source": "gamepad" if channel == "base" else "so101_leader",
        "seq": seq,
        "sample_seq": seq + 100,
        "ttl_ms": 200,
        "calibration_bundle_hash": bundle,
        "target": target,
    }
    if channel != "base":
        envelope["clutch"] = True
    return envelope


def adapter_snapshot(source_kind=contract.TRUSTED_JOINT_SOURCE, valid=True):
    samples = {
        name: {
            "name": name,
            "source_kind": source_kind,
            "valid": valid,
            "error": None,
            "calibration_hash": "sha256:fixture-calibration",
        }
        for name in SESSIONS
    }
    return {
        "available": valid and source_kind == contract.TRUSTED_JOINT_SOURCE,
        "stale_after_s": 0.25,
        "scan_span_s": 0.04,
        "joint_samples": samples,
        "base_estimate": {
            "source_kind": "wheel_odometry_estimate",
            "valid": True,
            "calibration_hash": "sha256:fixture-calibration",
            "covariance": [0.01, 0.01, 0.02],
        },
    }


def partial_agent(recorder):
    node = object.__new__(MobileAgent)
    node.read_only = False
    node.lock = threading.RLock()
    node.control_token = "fixture-control"
    node.control_expires = time.monotonic() + 60.0
    node.control_epoch = 4242
    node.sessions = recorder
    node.session_id = "car-session"
    node.recording_session_epoch = 4242
    node.recording_clock_epoch = None
    node.requested_session_id = "upper-session"
    node.requested_calibration_bundle_hash = "sha256:fixture-calibration"
    node.episode_id = "episode-http"
    node.clock_domain = "hardware_system_time"
    node.runtime = {"clock_domain": "hardware_system_time"}
    node.profile = {"profile": "phase_route_fixture"}
    node.hybrid_gateway = None
    node.hybrid_no_output = False
    node.output_transport = "legacy"
    node.frame_outputs_enabled = False
    node.licence_bridge = None
    node._sim_ns = lambda: 200
    node.emitted = []
    node._emit = lambda kind, **payload: node.emitted.append({"kind": kind, **payload})
    return node


class CapturingSessions:
    active = False
    session_id = None
    current_episode = None

    def __init__(self):
        self.metadata = None
        self.started = None

    def start_session(self, session_id, task, metadata, time_ns):
        self.started = (session_id, task, time_ns)
        self.metadata = metadata
        return {
            "session_id": session_id,
            "path": f"/offline/{session_id}",
            "topics": (
                list(metadata.get("recording_topics") or [])
                + list(metadata.get("recording_extra_topics") or [])
            ),
            "clock_domain": metadata["clock_domain"],
        }


def simulation_agent():
    profile = json.loads(SIM_PROFILE_PATHS[0].read_text(encoding="utf-8"))
    runtime = profile["runtime"]
    sensors = profile["sensors"]
    node = object.__new__(MobileAgent)
    node.profile_path = SIM_PROFILE_PATHS[0]
    node.profile = profile
    node.runtime = runtime
    node.read_only = False
    node.actuator_outputs = True
    node.hybrid_gateway = MobileAgent._new_hybrid_gateway(node)
    node.hybrid_control_token = None
    node.hybrid_no_output = False
    node.output_transport = "legacy"
    node.frame_outputs_enabled = False
    node.licence_bridge = None
    node.lock = threading.RLock()
    node.control_token = "sim-control"
    node.control_expires = time.monotonic() + 60.0
    node.control_epoch = 909
    node.clock_domain = runtime["clock_domain"]
    node.session_id = None
    node.recording_session_epoch = None
    node.recording_clock_epoch = None
    node.requested_session_id = None
    node.requested_calibration_bundle_hash = None
    node.requested_export_schema = None
    node.requested_runtime_mode = None
    node.requested_profile = None
    node.requested_simulation = None
    node.agent_version = "test"
    node.camera_profile = runtime["camera_profile_default"]
    node.camera_extrinsics = sensors["wrist_rgb"]["profiles"][node.camera_profile]
    node.task_name = "place_in_tray"
    node.rgb_topic = sensors["wrist_rgb"]["rgb_topic"]
    node.camera_info_topic = sensors["wrist_rgb"]["camera_info_topic"]
    node.depth_topic = sensors["depth"]["topic"]
    node.lidar_topic = sensors["lidar"]["topic"]
    node.imu_topic = sensors["imu"]["topic"]
    node.teleop_cfg = profile["teleop"]
    node.follower_feedback = None
    node.preview_backend = "fixture"
    node.record_joint_topic = runtime["record_joint_state_topic"]
    node.model_sdf = ROOT / "work" / "missing-sim.sdf"
    node.base_topic = runtime["base_velocity_topic"]
    node.wheel_topic = runtime["wheel_odom_topic"]
    node.remote_source = None
    node.sessions = CapturingSessions()
    node.emitted = []
    node.last_reject = None
    node.rejections = []
    node._remote_state = lambda: None
    node.topic_health = lambda missing_only=False: [] if missing_only else {
        "ages_s": {}, "missing": [],
    }
    node._sim_ns = lambda: 1234
    def emit(kind, **payload):
        event = {"kind": kind, **payload}
        node.emitted.append(event)
        return event

    node._emit = emit
    return node


def http_json(server, method, path, body=None, token=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3.0)
    payload = None if body is None else json.dumps(body)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-LeLab-Control-Token"] = token
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def http_bytes(server, method, path):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3.0)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def preview_agent(source_age_s=0.125):
    node = object.__new__(MobileAgent)
    node.lock = threading.RLock()
    node.preview_jpeg = b"\xff\xd8fixture-jpeg\xff\xd9"
    node.preview_seq = 17
    node.preview_source_wall = time.monotonic() - source_age_s
    node.preview_wall = node.preview_source_wall
    node.preview_stamp_ns = 1_700_000_000_123_456_789
    node.image_stamp_ns = node.preview_stamp_ns
    node.preview_backend = "fixture"
    node.rgb_topic = "/fixture/camera"
    node.hybrid_no_output = False
    node.output_transport = "legacy"
    node.frame_outputs_enabled = False
    node.licence_bridge = None
    node.status = lambda: {
        "sensors": {"rgb": MobileAgent.preview_status(node)},
    }
    return node


def serve(node):
    server = AgentHTTPServer(("127.0.0.1", 0), node)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=3.0)


def test_profiles_are_identical_and_keep_hardware_fail_closed():
    assert PROFILE_PATHS[0].read_bytes() == PROFILE_PATHS[1].read_bytes()
    profile = json.loads(PROFILE_PATHS[0].read_text(encoding="utf-8"))
    assert profile["profile"] == "hardware_hybrid_manual_v1"
    assert profile["runtime"]["actuator_outputs"] is False
    assert profile["runtime"]["firmware_capability"] == "unknown"
    assert profile["teleop"]["chassis_locked"] is True
    assert profile["training_export"]["enabled"] is False
    assert set(profile["recording"]["topics"]) == {
        "/ascamera/camera_publisher/rgb0/image",
        "/ascamera/camera_publisher/rgb0/camera_info",
        "/controller_manager/joint_states",
        "/lelab/recording/teleop",
        "/lelab/events",
    }
    assert not {
        "/controller/cmd_vel", "/cmd_vel", "/servo_controller",
        "/ros_robot_controller/bus_servo/set_position",
    }.intersection(profile["recording"]["topics"])
    assert profile["recording"]["feedback_provenance_required"] is True

    offline = profile["offline_validation"]
    assert offline["profile"] == "hybrid_manual_sim_v1"
    assert offline["identity"] == "fixture:landerpi:offline-no-hardware"
    assert offline["simulated"] is True
    assert offline["data_source"] == "simulated_offline"
    assert offline["hardware_available"] is False
    assert offline["training_export"]["enabled"] is False


def test_simulated_profile_is_separate_explicit_and_topic_aligned():
    assert SIM_PROFILE_PATHS[0].read_bytes() == SIM_PROFILE_PATHS[1].read_bytes()
    profile = json.loads(SIM_PROFILE_PATHS[0].read_text(encoding="utf-8"))
    assert profile["profile"] == "hybrid_manual_sim_v1"
    assert profile["source"] == "simulation"
    assert profile["simulation"]["simulated"] is True
    assert profile["simulation"]["data_source"] == "simulated_offline"
    assert profile["simulation"]["hardware_available"] is False
    assert profile["runtime"]["simulated"] is True
    assert profile["runtime"]["data_source"] == "simulated_offline"
    assert profile["runtime"]["hardware_available"] is False
    assert profile["training_export"]["enabled"] is True
    assert profile["training_export"]["schema"] == "mobile_9dof_sim"
    assert "servo_position_readback" not in SIM_PROFILE_PATHS[0].read_text(encoding="utf-8")

    runtime = profile["runtime"]
    sensors = profile["sensors"]
    expected_subscriptions = {
        "/clock",
        "/tf",
        runtime["joint_state_topic"],
        runtime["base_velocity_topic"],
        runtime["wheel_odom_topic"],
        sensors["wrist_rgb"]["rgb_topic"],
        sensors["wrist_rgb"]["camera_info_topic"],
        sensors["depth"]["topic"],
        sensors["lidar"]["topic"],
        sensors["imu"]["topic"],
        "/lelab/safety_state",
        "/lelab/applied_command",
        "/lelab/task/cube_pose",
        "/lelab/task/container_pose",
        "/lelab/task/box_pose",
    }
    recorded = set(profile["recording"]["topics"])
    assert recorded == expected_subscriptions
    assert set(profile["recording"]["required_topics"]).issubset(recorded)
    assert set(profile["recording"]["extra_topics"]) == {
        "/lelab/events",
        runtime["record_joint_state_topic"],
        "/lelab/recording/cube_pose",
        "/lelab/recording/container_pose",
        "/lelab/recording/box_pose",
        "/lelab/contact/left_jaw_link",
        "/lelab/contact/right_jaw_link",
        "/lelab/contact/gripper_base_link",
    }


def test_feedback_provenance_without_adapter_is_explicitly_untrusted():
    node = object.__new__(MobileAgent)
    node.hybrid_gateway = None
    provenance = MobileAgent._feedback_provenance(node)
    assert provenance["trusted"] is False
    assert provenance["available"] is False
    assert provenance["reason"] == "no_trusted_t1_driver_bound"
    assert provenance["joint_source_kind"] == "unavailable"
    assert provenance["base_source_kind"] == "unavailable"
    assert provenance["calibration_hash"] is None
    assert provenance["execution_observed"] == "unavailable"


def test_feedback_provenance_is_derived_from_adapter_facts():
    node = object.__new__(MobileAgent)
    driver = FakeDriver(adapter_snapshot())
    node.hybrid_gateway = types.SimpleNamespace(driver=driver)
    provenance = MobileAgent._feedback_provenance(node)
    assert provenance["trusted"] is True
    assert provenance["joint_source_kind"] == "servo_position_readback"
    assert provenance["base_source_kind"] == "wheel_odometry_estimate"
    assert provenance["calibration_hash"] == "sha256:fixture-calibration"
    assert provenance["base_estimate_has_covariance"] is True
    assert provenance["max_state_age_s"] == 0.25
    assert provenance["scan_span_ms"] == 40.0
    assert provenance["joint_topic"] == "/fixture/servo_readback"
    assert provenance["base_topic"] == "/fixture/base_estimate"

    cache = adapter_snapshot(source_kind="command_cache")
    cache["base_estimate"]["valid"] = False
    untrusted = contract.feedback_provenance_from_adapter(driver.capabilities_value, cache)
    assert untrusted["trusted"] is False
    assert untrusted["joint_source_kind"] == "unavailable"
    assert untrusted["base_source_kind"] == "unavailable"
    assert "base_estimate_missing_or_untrusted" in untrusted["reason"]


def test_three_channel_command_events_have_the_same_identity_and_ttl_shape():
    profile = {"limits": {"base_command_ttl_s": 0.3, "arm_command_ttl_s": 0.6}}
    payloads = {
        "base": {
            "session_id": "command-session", "session_epoch": 77,
            "source": "gamepad", "seq": 10,
        },
        "arm": {
            "session_id": "command-session", "session_epoch": 77,
            "source": "so101_leader", "seq": 4, "ttl_ms": 150,
        },
        "gripper": {
            "session_id": "command-session", "session_epoch": 77,
            "source": "so101_leader", "seq": 9, "ttl_s": 0.2,
        },
    }
    fields = {
        channel: contract.command_event_fields(
            channel=channel, payload=payload, profile=profile, control_epoch=77,
            calibration_bundle_hash="sha256:fixture-calibration",
        )
        for channel, payload in payloads.items()
    }
    common = {
        "protocol_version", "component", "session_id", "session_epoch", "source",
        "seq", "sample_seq", "ttl_ms", "ttl_source",
        "calibration_bundle_hash", "calibration_source", "confirmation",
    }
    assert all(set(value) == common for value in fields.values())
    assert fields["base"]["ttl_ms"] == pytest.approx(300.0)
    assert fields["arm"]["ttl_ms"] == pytest.approx(150.0)
    assert fields["gripper"]["ttl_ms"] == pytest.approx(200.0)
    assert all(value["session_epoch"] == 77 for value in fields.values())
    assert all(value["calibration_bundle_hash"] == "sha256:fixture-calibration"
               for value in fields.values())
    for value in fields.values():
        confirmation = value["confirmation"]
        assert confirmation["gateway_accepted"] is True
        assert confirmation["driver_sent"] == "unavailable"
        assert confirmation["controller_acknowledged"] == "unavailable"
        assert confirmation["execution_observed"] == "unavailable"
        assert confirmation["highest_confirmed"] == "gateway_accepted"


def test_command_event_rejects_a_foreign_epoch_and_missing_ttl():
    base = {
        "session_id": "command-session", "session_epoch": 88,
        "source": "gamepad", "seq": 1, "ttl_ms": 100,
    }
    with pytest.raises(contract.ContractError, match="!= lease epoch"):
        contract.command_event_fields(
            channel="base", payload=base, profile={"limits": {}}, control_epoch=77)
    base.pop("ttl_ms")
    base["session_epoch"] = 77
    with pytest.raises(contract.ContractError, match="no effective ttl_ms"):
        contract.command_event_fields(
            channel="base", payload=base, profile={"limits": {}}, control_epoch=77)
    base["ttl_ms"] = 0
    with pytest.raises(contract.ContractError, match="positive finite number"):
        contract.command_event_fields(
            channel="base", payload=base, profile={"limits": {}}, control_epoch=77)


def test_agent_base_command_event_uses_uniform_audit_fields():
    node = object.__new__(MobileAgent)
    node.read_only = False
    node.lock = threading.RLock()
    node.control_token = "fixture-control"
    node.control_expires = time.monotonic() + 60.0
    node.control_epoch = 77
    node.control_mode = "keyboard"
    node.chassis_locked = False
    node.profile = {"limits": {"base_command_ttl_s": 0.3}}
    node.hybrid_gateway = None
    node.gate = types.SimpleNamespace(
        base=lambda payload, mode: BaseCommand(0.1, 0.0, 0.0, []))
    node.base_target = None
    node.base_valid_until = 0.0
    node.last_base_wall = 0.0
    node.last_action = [0.0] * 9
    node.last_action_source = ""
    node.last_action_seq = -1
    node.command_seq = -1
    node.last_action_sim_ns = 0
    node._emit = lambda kind, **payload: {"kind": kind, **payload}
    node._sim_ns = lambda: 500
    node._action_vector = lambda: list(node.last_action)
    out = node.base_command("fixture-control", {
        "session_id": "fixture-control", "session_epoch": 77,
        "source": "keyboard", "mode": "keyboard", "seq": 0,
        "base": {"vx": 0.1, "vy": 0.0, "wz": 0.0},
    })
    event = out["event"]
    assert event["component"] == "base"
    assert event["session_epoch"] == 77
    assert event["ttl_ms"] == pytest.approx(300.0)
    assert event["target"] == pytest.approx([0.1, 0.0, 0.0])
    assert event["confirmation"]["execution_observed"] == "unavailable"


def test_metadata_attaches_adapter_provenance_and_keeps_export_disabled(tmp_path):
    node = object.__new__(MobileAgent)
    node.profile_path = PROFILE_PATHS[0]
    node.profile = json.loads(PROFILE_PATHS[0].read_text(encoding="utf-8"))
    node.read_only = False
    node.runtime = node.profile["runtime"]
    node.clock_domain = "hardware_system_time"
    node.actuator_outputs = False
    node.session_id = "car-session"
    node.recording_session_epoch = 4242
    node.recording_clock_epoch = "clock-fixture"
    node.requested_session_id = "upper-session"
    node.requested_calibration_bundle_hash = "sha256:fixture-calibration"
    node.hybrid_gateway = types.SimpleNamespace(driver=FakeDriver(adapter_snapshot()))
    node.agent_version = "test"
    node.camera_profile = "fixture-camera"
    node.camera_extrinsics = {}
    node.task_name = "manual_hybrid"
    node.rgb_topic = "/fixture/camera"
    node.camera_info_topic = "/fixture/camera_info"
    node.teleop_cfg = {"enabled": False}
    node.follower_feedback = None
    node.preview_backend = "fixture"
    node.record_joint_topic = "/fixture/joint_states"
    node.model_sdf = tmp_path / "missing.sdf"
    node.base_topic = "/fixture/base"
    node.wheel_topic = "/fixture/wheel"
    node.remote_source = None
    node._remote_state = lambda: None
    with patch("lelab_mobile_agent.agent.subprocess.check_output", side_effect=OSError):
        metadata = node._metadata()
    assert metadata["training_export"] is False
    assert metadata["simulated"] is False
    assert metadata["hardware_available"] == "unknown"
    assert metadata["data_source"] == "hardware"
    assert metadata["session_epoch"] == 4242
    assert metadata["clock_epoch"] == "clock-fixture"
    assert metadata["feedback_provenance"]["trusted"] is True
    assert metadata["feedback_provenance"]["joint_source_kind"] == "servo_position_readback"


def test_simulation_start_session_persists_controller_identity_and_synthetic_provenance():
    node = simulation_agent()
    server, thread = serve(node)
    try:
        body = {
            "task": "manual hybrid simulation",
            "source": "manual_hybrid",
            "session_id": "upper-sim-session",
            "session_epoch": 909,
            "calibration_bundle_hash": "simulation:leader-follower-fixture",
            "export_schema": "mobile_9dof_sim",
            "runtime_mode": "simulated_hybrid_manual",
            "profile": "hybrid_manual_sim_v1",
            "simulation": {
                "simulated": True,
                "identity": "controller:offline-simulation-fixture",
            },
        }
        wrong_schema = dict(body, export_schema="mobile_hybrid_manual_9d_v1")
        status, out = http_json(
            server, "POST", "/recording/start_session", wrong_schema,
            token="sim-control")
        assert status == 409
        assert out["error"] == "export_schema_mismatch"

        status, out = http_json(
            server, "POST", "/recording/start_session", body, token="sim-control")
        assert status == 200, out
        assert out["clock_domain"] == "ros_sim_time"
    finally:
        stop_server(server, thread)

    metadata = node.sessions.metadata
    assert metadata["profile"] == "hybrid_manual_sim_v1"
    assert metadata["requested_profile"] == "hybrid_manual_sim_v1"
    assert metadata["runtime_mode"] == "simulated_hybrid_manual"
    assert metadata["agent_runtime_mode"] == "hybrid_manual_sim"
    assert metadata["export_schema"] == "mobile_9dof_sim"
    assert metadata["simulated"] is True
    assert metadata["source"] == "simulation"
    assert metadata["data_source"] == "simulated_offline"
    assert metadata["hardware_available"] is False
    assert metadata["training_export"] is True
    assert metadata["controller_session"] == {
        "export_schema": "mobile_9dof_sim",
        "runtime_mode": "simulated_hybrid_manual",
        "profile": "hybrid_manual_sim_v1",
        "simulation": {
            "simulated": True,
            "identity": "controller:offline-simulation-fixture",
        },
    }
    provenance = metadata["feedback_provenance"]
    assert provenance["trusted"] is False
    assert provenance["available"] is True
    assert provenance["simulated"] is True
    assert provenance["hardware_measurement"] is False
    assert provenance["joint_source_kind"] == "synthetic"
    assert provenance["base_source_kind"] == "simulation_odometry_estimate"
    assert provenance["calibration_hash"] == "simulation:hybrid_manual_sim_v1"
    assert provenance["reason"] == "explicit_synthetic_simulation_feedback"
    assert metadata["recording_topics"][0] == "/clock"
    assert "/lelab/events" in metadata["recording_extra_topics"]


def test_simulation_command_event_uses_sim_identity_not_a_servo_claim():
    node = simulation_agent()
    fields = node._command_audit_fields("base", {
        "session_id": "sim-control",
        "session_epoch": 909,
        "source": "gamepad",
        "seq": 1,
        "ttl_ms": 120,
    })
    assert fields["ttl_ms"] == pytest.approx(120.0)
    assert fields["session_id"] == "sim-control"
    assert fields["session_epoch"] == 909
    assert fields["source"] == "gamepad"
    assert fields["seq"] == 1
    assert fields["calibration_bundle_hash"] == "simulation:hybrid_manual_sim_v1"
    assert fields["confirmation"]["controller_acknowledged"] == "unavailable"
    assert fields["confirmation"]["execution_observed"] == "unavailable"


def test_hybrid_gateway_routes_are_token_bound_and_never_output():
    node = simulation_agent()
    server, thread = serve(node)
    begin = {
        "session_id": "hybrid-sim-session",
        "session_epoch": 909,
        "calibration_bundle_hash": SIM_BUNDLE,
        "channels": ["base", "arm", "gripper"],
    }
    try:
        status, out = http_json(
            server, "POST", "/hybrid/begin_epoch", begin, token="wrong-token")
        assert status == 403
        assert out["error"] == "control_token"

        status, out = http_json(
            server, "POST", "/hybrid/begin_epoch",
            {**begin, "unexpected": True}, token="sim-control")
        assert status == 400
        assert out["error"] == "hybrid_begin_fields"

        status, out = http_json(
            server, "POST", "/hybrid/begin_epoch", begin, token="sim-control")
        assert status == 200, out
        assert out["session_id"] == "hybrid-sim-session"
        assert out["session_epoch"] == 909
        assert out["channels"] == ["arm", "base", "gripper"]
        assert out["driver_bound"] is False
        assert out["outputs_enabled"] is False
        assert out["simulated"] is True
        assert node.hybrid_gateway.driver is None
        assert node.hybrid_gateway.outputs_enabled is False

        status, out = http_json(
            server, "POST", "/hybrid/stop",
            {"reason": "operator", "session_epoch": 908},
            token="sim-control")
        assert status == 409
        assert out["error"] == "hybrid_identity_mismatch"

        status, out = http_json(
            server, "POST", "/hybrid/stop", {"reason": "operator"},
            token="sim-control")
        assert status == 200, out
        assert out["stopped"] is True
        assert out["driver_stopped"] is False
        assert out["outputs_enabled"] is False
        assert out["hardware_effect"] == "unverified"
        assert out["simulated"] is True
    finally:
        stop_server(server, thread)

    hybrid_status = node._hybrid_status()
    assert hybrid_status["simulated"] is True
    assert hybrid_status["data_source"] == "simulated_offline"
    assert hybrid_status["hardware_available"] is False
    assert hybrid_status["driver_bound"] is False
    assert hybrid_status["outputs_enabled"] is False
    assert hybrid_status["controller_acknowledged"] == "unavailable"
    assert hybrid_status["execution_observed"] == "unavailable"


def test_hybrid_command_routes_preserve_envelopes_and_record_all_channels():
    node = simulation_agent()
    # Explicit simulation may record and audit commands with no actuator publishers.
    node.actuator_outputs = False
    node.base_pub = node.arm_pub = node.gripper_pub = None
    server, thread = serve(node)
    begin = {
        "session_id": "hybrid-sim-session",
        "session_epoch": 909,
        "calibration_bundle_hash": SIM_BUNDLE,
        "channels": ["base", "arm", "gripper"],
    }
    try:
        status, out = http_json(
            server, "POST", "/hybrid/begin_epoch", begin, token="sim-control")
        assert status == 200, out
        for channel in ("base", "arm", "gripper"):
            envelope = v1_command(channel, 1)
            original = copy.deepcopy(envelope)
            status, out = http_json(
                server, "POST", "/hybrid/command", envelope, token="sim-control")
            assert status == 200, out
            assert envelope == original
            # Submitting is not sending: the gateway has accepted the envelope and the
            # dispatch is still pending, so the answer must say so rather than claim a
            # transmission.  Acknowledgement and execution stay unavailable either way.
            assert out["driver_sent"] == "pending_dispatch"
            assert out["controller_acknowledged"] == "unavailable"
            assert out["execution_observed"] == "unavailable"
            assert out["outputs_enabled"] is False
            event = out["event"]
            assert event["kind"] == "command"
            assert event["component"] == channel
            assert event["session_id"] == "hybrid-sim-session"
            assert event["session_epoch"] == 909
            assert event["source"] == original["source"]
            assert event["seq"] == 1
            assert event["ttl_ms"] == pytest.approx(200.0)
            assert event["calibration_bundle_hash"] == SIM_BUNDLE
            assert event["gateway_target"] == original["target"]
            assert event["gateway_envelope"] == original
            assert event["confirmation"]["driver_sent"] == "unavailable"
            assert event["confirmation"]["controller_acknowledged"] == "unavailable"
            assert event["confirmation"]["execution_observed"] == "unavailable"

            status, dispatched = http_json(
                server, "POST", "/hybrid/dispatch", {"channel": channel},
                token="sim-control")
            assert status == 200, dispatched
            assert dispatched["dispatched"] is True
            assert dispatched["driver_sent"] is False
            assert dispatched["outputs_enabled"] is False
            assert dispatched["controller_acknowledged"] == "unavailable"
            assert dispatched["execution_observed"] == "unavailable"

        status, out = http_json(
            server, "POST", "/hybrid/command",
            v1_command("base", 2), token="wrong-token")
        assert status == 403
        assert out["error"] == "control_token"

        status, out = http_json(
            server, "POST", "/hybrid/dispatch", {"channel": "base"},
            token="sim-control")
        assert status == 200
        assert out["dispatched"] is False
        assert out["driver_sent"] is False
        assert out["reason"] == "no_pending_command"
    finally:
        stop_server(server, thread)


def test_hybrid_command_rejects_stale_identity_without_consuming_sequence():
    node = simulation_agent()
    server, thread = serve(node)
    begin = {
        "session_id": "hybrid-sim-session",
        "session_epoch": 909,
        "calibration_bundle_hash": SIM_BUNDLE,
        "channels": ["base"],
    }
    try:
        status, out = http_json(
            server, "POST", "/hybrid/begin_epoch", begin, token="sim-control")
        assert status == 200, out

        for bad in (
                v1_command("base", 1, epoch=908),
                v1_command("base", 1, bundle="old-bundle")):
            status, out = http_json(
                server, "POST", "/hybrid/command", bad, token="sim-control")
            assert status == 400
            assert out["error"] in {"session_mismatch", "calibration_mismatch"}

        status, out = http_json(
            server, "POST", "/hybrid/command", v1_command("base", 1),
            token="sim-control")
        assert status == 200, out
        status, out = http_json(
            server, "POST", "/hybrid/command", v1_command("base", 1),
            token="sim-control")
        assert status == 400
        assert out["error"] == "stale_seq"
    finally:
        stop_server(server, thread)


def test_preview_jpeg_headers_and_status_share_the_same_source_frame():
    node = preview_agent(source_age_s=0.125)
    server, thread = serve(node)
    try:
        status, headers, body = http_bytes(server, "GET", "/preview.jpg")
        assert status == 200
        assert body == node.preview_jpeg
        assert headers["X-LeLab-Frame-Seq"] == "17"
        assert headers["X-LeLab-Source-Stamp-Ns"] == str(node.preview_stamp_ns)
        assert float(headers["X-LeLab-Source-Age-Ms"]) >= 100.0
        assert float(headers["X-LeLab-Source-Age-S"]) >= 0.1
        assert headers["X-LeLab-Source-Topic"] == "/fixture/camera"
        assert "X-LeLab-Frame-Seq" in headers["Access-Control-Expose-Headers"]

        # A newer raw camera callback must not rewrite the source identity of the
        # JPEG already committed, nor create a new preview sequence by itself.
        node.image_stamp_ns = 1_800_000_000_000_000_000
        status, public_status = http_json(server, "GET", "/status")
        assert status == 200
        rgb = public_status["sensors"]["rgb"]
        assert rgb["preview_seq"] == int(headers["X-LeLab-Frame-Seq"])
        assert rgb["stamp_ns"] == int(headers["X-LeLab-Source-Stamp-Ns"])
        assert rgb["latest_image_stamp_ns"] == node.image_stamp_ns
    finally:
        stop_server(server, thread)


def test_preview_jpeg_can_serve_the_exact_status_frame_from_bounded_cache():
    node = preview_agent(source_age_s=0.125)
    node.preview_frames = deque(maxlen=32)
    node.preview_frames.append(
        (node.preview_seq, node.preview_stamp_ns, time.monotonic(), node.preview_jpeg))
    server, thread = serve(node)
    try:
        status, headers, body = http_bytes(
            server, "GET", f"/preview.jpg?frame_seq={node.preview_seq}")
        assert status == 200
        assert body == node.preview_jpeg
        assert headers["X-LeLab-Frame-Seq"] == str(node.preview_seq)
        assert headers["X-LeLab-Source-Stamp-Ns"] == str(node.preview_stamp_ns)

        status, _, body = http_bytes(server, "GET", "/preview.jpg?frame_seq=16")
        assert status == 409
        assert json.loads(body)["error"] == "frame_not_available"
    finally:
        stop_server(server, thread)


def test_preview_jpeg_without_a_frame_still_returns_503():
    node = preview_agent()
    node.preview_jpeg = None
    node.preview_seq = 0
    node.preview_stamp_ns = None
    node.preview_source_wall = 0.0
    node.preview_wall = 0.0
    server, thread = serve(node)
    try:
        status, headers, body = http_bytes(server, "GET", "/preview.jpg")
        assert status == 503
        assert "X-LeLab-Frame-Seq" not in headers
        assert json.loads(body)["error"] == "preview_unavailable"
    finally:
        stop_server(server, thread)


def test_mjpeg_parts_carry_the_same_source_identity_without_blocking_http():
    node = preview_agent(source_age_s=0.125)
    server, thread = serve(node)
    stream = http.client.HTTPConnection(*server.server_address, timeout=3.0)
    try:
        stream.request("GET", "/preview.mjpg")
        response = stream.getresponse()
        assert response.status == 200

        started = time.monotonic()
        status, public_status = http_json(server, "GET", "/status")
        assert status == 200
        assert time.monotonic() - started < 1.0
        assert public_status["sensors"]["rgb"]["preview_seq"] == 17

        boundary = response.fp.readline()
        while boundary in (b"\r\n", b""):
            boundary = response.fp.readline()
        assert boundary == b"--frame\r\n"
        frame_headers = {}
        while True:
            line = response.fp.readline()
            if line == b"\r\n":
                break
            name, value = line.decode("ascii").rstrip("\r\n").split(": ", 1)
            frame_headers[name] = value
        assert frame_headers["X-LeLab-Frame-Seq"] == "17"
        assert frame_headers["X-LeLab-Source-Stamp-Ns"] == str(node.preview_stamp_ns)
        assert float(frame_headers["X-LeLab-Source-Age-Ms"]) >= 100.0
        body = response.fp.read(int(frame_headers["Content-Length"]))
        assert body == node.preview_jpeg
    finally:
        stream.close()
        stop_server(server, thread)


def test_real_http_phase_route_appends_to_session_recorder_timeline(tmp_path):
    recorder = recorder_module.SessionRecorder(
        tmp_path,
        {
            "profile": "phase_route_fixture",
            "runtime": {"clock_domain": "hardware_system_time"},
            "recording": {
                "topics": ["/lelab/events"],
                "required_topics": [],
                "min_free_bytes": 1,
                "raw_storage": "sqlite3",
            },
        },
        SilentLog(),
    )
    with patch.object(recorder_module.subprocess, "Popen", return_value=FakeBagProcess()), \
            patch.object(recorder, "_startup_check", return_value=True), \
            patch.object(recorder, "_bag_info", return_value=(
                    True, "Duration: 1.0s\nMessages: 2\nFiles: x")):
        recorder.start_session("car-session", "manual_hybrid", {
            "source": "hardware",
            "session_epoch": 4242,
            "clock_epoch": None,
            "feedback_provenance": {"trusted": False},
        }, 100)
        recorder.start_episode("episode-http", "manual_hybrid", "manual_hybrid", 100, {
            "time_ns": 100, "clock_domain": "hardware_system_time",
        })
        node = partial_agent(recorder)
        server, thread = serve(node)
        try:
            body = {
                "phase": "observe",
                "episode_id": "episode-http",
                "session_id": "upper-session",
                "session_epoch": 4242,
                "clock_epoch": "clock-fixture",
                "calibration_bundle_hash": "sha256:fixture-calibration",
            }
            status, out = http_json(
                server, "POST", "/recording/set_phase", body, token="fixture-control")
            assert status == 200
            assert out["payload"] == {"phase": "observe"}
            assert out["time_ns"] == 200
            assert out["session_epoch"] == 4242
            assert out["clock_epoch"] == "clock-fixture"

            status, out = http_json(
                server, "POST", "/recording/set_phase",
                {**body, "episode_id": "another-episode"},
                token="fixture-control")
            assert status == 409
            assert out["error"] == "episode_mismatch"

            status, out = http_json(
                server, "POST", "/recording/set_phase",
                {**body, "clock_epoch": "different-clock"},
                token="fixture-control")
            assert status == 409
            assert out["error"] == "clock_epoch_mismatch"

            status, out = http_json(
                server, "POST", "/recording/set_phase",
                {**body, "session_id": "foreign-session"},
                token="fixture-control")
            assert status == 409
            assert out["error"] == "session_mismatch"

            status, out = http_json(
                server, "POST", "/recording/set_phase",
                {**body, "calibration_bundle_hash": "sha256:foreign-calibration"},
                token="fixture-control")
            assert status == 409
            assert out["error"] == "calibration_mismatch"

            status, out = http_json(
                server, "POST", "/recording/set_phase", body, token="wrong-token")
            assert status == 403
            assert out["error"] == "control_token"
        finally:
            stop_server(server, thread)

        phase_events = [
            json.loads(line)
            for line in (tmp_path / "sessions/car-session/events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        phase_events = [event for event in phase_events
                        if event["kind"] in {"phase_started", "phase_ended"}]
        assert len(phase_events) == 1
        assert phase_events[0]["kind"] == "phase_started"
        assert phase_events[0]["session_id"] == "car-session"
        assert phase_events[0]["session_epoch"] == 4242
        assert phase_events[0]["clock_epoch"] == "clock-fixture"
        assert phase_events[0]["time_ns"] == 200
        assert phase_events[0]["payload"] == {"phase": "observe"}
        assert recorder.episodes["episode-http"].phases == [
            {"phase": "observe", "started_time_ns": 200, "ended_time_ns": None}
        ]
        assert node.emitted[-1]["kind"] == "phase_started"
        assert not any(event["kind"].startswith("command") for event in node.emitted)

        recorder.finish_episode(300, {"success": "unknown"})
        recorder.stop_session(reason="html_route_test", time_ns=300)
    manifest = json.loads(
        (tmp_path / "sessions/car-session/manifest.json").read_text(encoding="utf-8"))
    assert manifest["session_epoch"] == 4242
    assert manifest["clock_epoch"] == "clock-fixture"
    assert manifest["episodes"][0]["phases"] == [
        {"phase": "observe", "started_time_ns": 200, "ended_time_ns": 300}
    ]
