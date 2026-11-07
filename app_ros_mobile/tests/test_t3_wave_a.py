"""T3 Wave A independent offline tests (upper computer, no ROS/hardware).

All objects here are explicit fixtures.  These tests prove logical contracts only:
no serial port, camera, gamepad, service, container or actuator is touched, and a
passing test is NOT hardware or physical-stop acceptance evidence.
"""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from lelab import mobile_teleop_coordinator as coordinator
from lelab.hybrid_calibration import JOINTS, KINDS, validate_bundle
from lelab.hybrid_channel import (ChannelPermits, DeadlineQueue, HybridChannelRouter,
                                  HybridProtocolError, HybridSessionError, SequenceRegistry,
                                  TrustedFeedbackGate, command_v1)
from lelab.hybrid_facade import HybridPublicFacade, validate_asset_strict
from lelab.hybrid_fixtures import (build_fixture_facade, calibration_assets_fixture,
                                   calibration_document_fixture, gamepad_sample,
                                   trusted_feedback_fixture)
from lelab.hybrid_gamepad import GamepadIntent
from lelab.hybrid_session import HybridSession

ROOT = Path(__file__).resolve().parents[2]
ARM_TARGET = {"joint_names": ["joint1", "joint2", "joint3", "joint4", "joint5"],
              "positions_rad": [0.0] * 5}
BASE_TARGET = {"vx_m_s": 0.1, "vy_m_s": 0.0, "wz_rad_s": 0.0}


def test_public_api_is_frozen_and_default_is_fail_closed():
    facade = HybridPublicFacade()
    for name in ("status", "acquire_session", "release_session", "set_arm_state",
                 "ingest_gamepad", "preview", "capabilities", "calibration",
                 "recording", "calibration_capabilities", "calibration_readiness",
                 "validate_calibration", "validate_calibration_bundle"):
        assert callable(getattr(facade, name)), name
    status = facade.status()
    for field in ("state", "session_id", "session_epoch", "profile", "leases", "channels",
                  "devices", "calibrations", "feedback", "video", "recording",
                  "last_error", "missing_for_following", "outputs_enabled"):
        assert field in status, field
    assert status["outputs_enabled"] is False
    assert status["state"] == "disconnected"
    assert facade.capabilities()["available"] is False
    assert facade.preview()["outputs_enabled"] is False
    with pytest.raises((RuntimeError, ValueError), match="adapters_unavailable"):
        facade.acquire_session({"owner": "operator"})
    json.dumps(status, allow_nan=False)


def test_t4_route_methods_present_on_core_hybrid_session():
    """T4 hardwires `from .hybrid_session import HybridSession` in ros_mobile.py."""
    core = HybridSession()
    assert callable(core.capabilities)
    assert callable(core.calibration)
    assert callable(core.recording)
    assert core.capabilities()["available"] is False
    assert core.calibration("validate", "base", {"payload": {}})["valid"] is False
    # No session and no store bound: these are deployment gaps, answered as 503.
    with pytest.raises(Exception) as no_session:
        core.recording("start_session", {})
    assert no_session.value.status == 503
    with pytest.raises(Exception) as no_store:
        core.calibration("save", "base", {"payload": {}, "identity": "x"})
    assert no_store.value.status == 503
    # Ordering is enforced before phase validity: no phase may be set for an
    # unstarted recording, and a started one rejects unknown phase names.
    with pytest.raises(ValueError, match="recording_session_not_started"):
        core.recording("set_phase", {"phase": "nonsense"})


def test_coordinator_factory_is_fail_closed_and_has_no_rclpy_dependency():
    facade = coordinator.get_hybrid_session_facade()
    assert isinstance(facade, HybridPublicFacade)
    assert facade.status()["outputs_enabled"] is False
    assert "rclpy" not in sys.modules
    adapters = coordinator.hybrid_lease_adapters()
    assert set(adapters) == {"upper", "car"}
    for adapter in adapters.values():
        for method in ("acquire", "valid", "release"):
            assert callable(getattr(adapter, method))
    assert callable(adapters["car"].stop)


def test_dual_layer_lease_rollback_on_car_failure_and_on_cancel():
    facade = build_fixture_facade()
    log = facade.simulated_context["log"]
    facade.car.fail = True
    with pytest.raises(RuntimeError, match="busy"):
        facade.acquire_session({"owner": "operator"})
    assert log == ["upper:acquire", "car:acquire", "upper:release"]
    assert facade.status()["session_id"] is None
    assert facade.router is None

    cancel = build_fixture_facade()
    clog = cancel.simulated_context["log"]
    cancel.observer.snapshot = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        cancel.acquire_session({"owner": "operator"})
    assert clog == ["upper:acquire", "car:acquire", "car:stop", "car:release", "upper:release"]
    assert cancel.status()["session_id"] is None


def test_stop_precedes_release_and_new_epoch_never_auto_follows():
    facade = build_fixture_facade()
    log = facade.simulated_context["log"]
    first = facade.acquire_session({"owner": "operator"})
    for action in ("preview", "align", "arm", "engage"):
        facade.set_arm_state(action)
    assert facade.status()["channels"]["arm"]["state"] == "following"
    assert facade.status()["outputs_enabled"] is False
    facade.release_session("operator")
    assert log[-3:] == ["car:stop", "car:release", "upper:release"]
    second = facade.acquire_session({"owner": "operator"})
    assert first["session_epoch"] != second["session_epoch"]
    assert second["channels"]["arm"]["state"] == "idle"
    assert second["channels"]["arm"]["permitted"] is False
    assert second["channels_meta"]["pending"] == {}


def test_channel_permits_are_independent_and_not_a_global_mode_switch():
    permits = ChannelPermits()
    permits.begin_epoch("e1")
    permits.grant("e1", "base")
    assert permits.allowed("e1") == {"base"}
    permits.grant("e1", "arm", "gripper")
    assert permits.allowed("e1") == {"base", "arm", "gripper"}
    assert not permits.permitted("base", "old-epoch")
    with pytest.raises(HybridProtocolError) as mismatch:
        permits.grant("e2", "base")
    assert mismatch.value.reason == "epoch_mismatch"
    permits.revoke("base")
    assert permits.allowed("e1") == {"arm", "gripper"}
    permits.begin_epoch("e2")
    assert permits.allowed("e2") == set()


def test_sequence_registry_is_keyed_by_epoch_channel_source():
    registry = SequenceRegistry()
    assert registry.check("e1", "base", "gamepad", 0)
    registry.advance("e1", "base", "gamepad", 0)
    assert not registry.check("e1", "base", "gamepad", 0)
    # A different channel or source has its own independent sequence.
    assert registry.check("e1", "arm", "so101_leader", 0)
    assert registry.check("e2", "base", "gamepad", 0)
    registry.advance("e2", "base", "gamepad", 4)
    # clear_epoch keeps only the given epoch and drops every other slot.
    assert registry.clear_epoch("e1") == 1
    assert registry.check("e1", "base", "gamepad", 1)
    assert registry.check("e2", "base", "gamepad", 0)
    with pytest.raises(HybridProtocolError):
        registry.check("e1", "arm", "so101_leader", True)


def test_deadline_queue_purges_expired_and_clears_on_epoch_change():
    queue = DeadlineQueue()
    queue.put({"channel": "base", "seq": 1, "deadline_mono_ns": 100})
    assert queue.pop("base", 99)["seq"] == 1
    queue.put({"channel": "arm", "seq": 2, "deadline_mono_ns": 500})
    assert queue.purge_expired(500) == 1
    assert queue.pop("arm", 500) is None

    clock = {"now": 1_000_000_000}
    router = HybridChannelRouter(session_id="s", session_epoch="e1", bundle_hash="h",
                                 max_ttl_ms=200, clock_ns=lambda: clock["now"])
    router.grant("base", "arm", "gripper")
    command = command_v1(session_id="s", session_epoch="e1", channel="base", source="gamepad",
                         seq=1, sample_seq=1, ttl_ms=100, calibration_bundle_hash="h",
                         target=BASE_TARGET)
    router.submit(command, now_ns=clock["now"])
    assert router.snapshot(clock["now"])["pending"]["base"]["remaining_ms"] == 100
    rolled = router.begin_epoch("e2")
    assert rolled["queue_dropped"] == 1 and rolled["seq_slots_dropped"] >= 1
    assert router.snapshot(clock["now"])["pending"] == {}
    assert router.snapshot(clock["now"])["outputs_enabled"] is False


@pytest.mark.parametrize("field,value", [
    ("session_epoch", "old"), ("source", "maintenance"), ("source", "gamepad"),
    ("calibration_bundle_hash", "old"), ("ttl_ms", 0), ("ttl_ms", 999),
    ("seq", True), ("protocol_version", 2), ("received_mono_ns", 123),
])
def test_bad_frames_do_not_consume_a_sequence_number(field, value):
    router = HybridChannelRouter(session_id="s", session_epoch="e1", bundle_hash="h",
                                 max_ttl_ms=200, clock_ns=lambda: 1_000_000_000)
    router.grant("arm")
    good = command_v1(session_id="s", session_epoch="e1", channel="arm",
                      source="so101_leader", seq=1, sample_seq=1, ttl_ms=100,
                      calibration_bundle_hash="h", target=ARM_TARGET, clutch=True)
    bad = copy.deepcopy(good)
    bad[field] = value
    with pytest.raises(HybridProtocolError):
        router.submit(bad, now_ns=1_000_000_000)
    assert router.submit(good, now_ns=1_000_000_000)["accepted"] is True
    with pytest.raises(HybridProtocolError, match="stale_seq"):
        router.submit(good, now_ns=1_000_000_000)


@pytest.mark.parametrize("target,clutch", [
    ({"joint_names": ["joint1"] * 5, "positions_rad": [0.0] * 5}, True),
    ({"joint_names": ["joint1"] * 5, "positions_rad": [0.0] * 4}, True),
    ({"joint_names": ["joint1"] * 5, "positions_rad": [float("nan")] * 5}, True),
    ({"joint_names": ["joint1"] * 5, "positions_rad": [0.0] * 5}, False),
    (None, True),
])
def test_missing_or_nonfinite_data_is_rejected(target, clutch):
    with pytest.raises(HybridProtocolError):
        command_v1(session_id="s", session_epoch="e1", channel="arm", source="so101_leader",
                   seq=1, sample_seq=1, ttl_ms=100, calibration_bundle_hash="h",
                   target=target, clutch=clutch)


def test_expired_command_is_not_dispatched_and_forward_keeps_remaining_ttl():
    clock = {"now": 1_000_000_000}
    router = HybridChannelRouter(session_id="s", session_epoch="e1", bundle_hash="h",
                                 max_ttl_ms=200, clock_ns=lambda: clock["now"])
    router.grant("arm")
    command = command_v1(session_id="s", session_epoch="e1", channel="arm",
                         source="so101_leader", seq=1, sample_seq=1, ttl_ms=100,
                         calibration_bundle_hash="h", target=ARM_TARGET, clutch=True)
    router.submit(command, now_ns=clock["now"])
    clock["now"] += 40_000_000
    assert router.dispatch("arm")["confirmation"] == "gateway_accepted"
    # A second queued command left to expire must never reach dispatch.
    command2 = command_v1(session_id="s", session_epoch="e1", channel="arm",
                          source="so101_leader", seq=2, sample_seq=2, ttl_ms=50,
                          calibration_bundle_hash="h", target=ARM_TARGET, clutch=True)
    router.submit(command2, now_ns=clock["now"])
    clock["now"] += 51_000_000
    assert router.dispatch("arm") is None
    assert router.snapshot(clock["now"])["pending"] == {}


def test_trusted_feedback_gate_refuses_cache_placeholder_synthetic_and_missing_data():
    good = trusted_feedback_fixture(now_ns=1_000_000_000, calibration_hash="h")
    assert TrustedFeedbackGate.gate(good, calibration_hash="h", now_ns=1_000_000_000,
                                    max_age_ns=100_000_000)["trusted"] is True
    for change in (
        lambda d: d.update(source_kind="command_cache"),
        lambda d: d.update(source_kind="model_placeholder"),
        lambda d: d.update(source_kind="synthetic"),
        lambda d: d["samples"].pop(),
        lambda d: d["samples"][0].update(servo_id=99),
        lambda d: d["samples"][0].update(position_rad=float("inf")),
        lambda d: d["samples"][0].update(calibration_hash="other"),
        lambda d: d["samples"][0].update(received_mono_ns=0),
        lambda d: d["samples"][0].update(raw_position=1001),
    ):
        altered = copy.deepcopy(good)
        change(altered)
        assert not TrustedFeedbackGate.gate(altered, calibration_hash="h", now_ns=1_000_000_000,
                                            max_age_ns=100_000_000)["trusted"]


def test_facade_submit_intent_requires_trusted_feedback_and_never_actuates():
    facade = build_fixture_facade()
    facade.acquire_session({"owner": "operator"})
    assert facade.trusted_feedback()["trusted"] is True
    with pytest.raises(HybridProtocolError) as unpermitted:
        facade.submit_intent("arm", ARM_TARGET, sample_seq=1)
    assert unpermitted.value.reason == "channel_not_permitted"
    for action in ("preview", "align", "arm", "engage"):
        facade.set_arm_state(action)
    result = facade.submit_intent("arm", ARM_TARGET, sample_seq=1)
    assert result["confirmation"] == "gateway_accepted"
    assert result["outputs_enabled"] is False
    assert result["controller_acknowledged"] == "unavailable"
    dispatched = facade.dispatch("arm")
    assert dispatched["driver_sent"] is False and dispatched["outputs_enabled"] is False
    assert facade.simulated_context["driver"].submitted == []

    untrusted = build_fixture_facade()
    untrusted.observer.facts.pop("feedback", None)
    untrusted.acquire_session({"owner": "operator"})
    assert untrusted.trusted_feedback()["trusted"] is False
    for action in ("preview", "align", "arm", "engage"):
        untrusted.set_arm_state(action)
    with pytest.raises(HybridSessionError, match="trusted_feedback_unavailable"):
        untrusted.submit_intent("arm", ARM_TARGET)


def test_gamepad_neutral_new_press_periodic_input_and_disconnect():
    gate = GamepadIntent(0.1, 100)
    assert not gate.ingest({"seq": 1, "received_mono_ns": 1, "deadman": True,
                            "axes": [0.5, 0, 0]}, 1)["armed"]
    assert gate.ingest({"seq": 2, "received_mono_ns": 2, "deadman": False,
                        "axes": [0, 0, 0]}, 2)["neutral_seen"] is True
    assert gate.ingest({"seq": 3, "received_mono_ns": 3, "deadman": True,
                        "axes": [0, 0, 0]}, 3)["armed"] is True
    # Holding the same axis still produces a new valid periodic intent with a new seq.
    assert gate.ingest({"seq": 4, "received_mono_ns": 4, "deadman": True,
                        "axes": [0.5, 0, 0]}, 4)["intent"] == [0.5, 0, 0]
    assert gate.ingest({"seq": 5, "received_mono_ns": 5, "deadman": True,
                        "axes": [0.5, 0, 0]}, 5)["intent"] == [0.5, 0, 0]
    # Disconnect: stale snapshot drops the arm and a cached press cannot re-enable.
    assert gate.snapshot(105)["armed"] is False
    assert not gate.ingest({"seq": 6, "received_mono_ns": 106, "deadman": True,
                            "axes": [0, 0, 0]}, 106)["armed"]
    with pytest.raises(ValueError):
        gate.ingest({"seq": 5, "received_mono_ns": 107, "deadman": False,
                     "axes": [0, 0, 0]}, 107)


def test_two_inputs_concurrent_and_grasp_phase_locks_base():
    facade = build_fixture_facade()
    clock = facade.simulated_context["clock"]
    facade.acquire_session({"owner": "operator"})
    for action in ("preview", "align", "arm", "engage"):
        facade.set_arm_state(action)
    facade.set_arm_state({"action": "set_phase", "phase": "observe"})
    facade.ingest_gamepad(gamepad_sample(1, clock.now, False))
    clock.advance(1)
    facade.ingest_gamepad(gamepad_sample(2, clock.now, True))
    facade.set_arm_state("enable_base")
    clock.advance(1)
    result = facade.ingest_gamepad(gamepad_sample(3, clock.now, True, [0.5, 0, 0]))
    assert result["command_submitted"] is True
    assert result["outputs_enabled"] is False
    status = facade.status()
    assert status["channels"]["base"]["permitted"] is True
    assert status["channels"]["arm"]["permitted"] is True
    assert status["channels"]["arm"]["state"] == "following"
    assert status["outputs_enabled"] is False
    facade.set_arm_state({"action": "set_phase", "phase": "grasp"})
    assert facade.status()["channels"]["base"]["permitted"] is False
    with pytest.raises(RuntimeError, match="base_locked"):
        facade.set_arm_state("enable_base")


def test_recording_phases_emit_no_motion_and_stop_precedes_wrap_up():
    facade = build_fixture_facade()
    log = facade.simulated_context["log"]
    facade.acquire_session({"owner": "operator"})
    facade.recording("start_session", {})
    facade.recording("start_episode", {"episode_id": "e1"})
    before = len(log)
    observe = facade.recording("set_phase", {"phase": "observe"})
    assert observe["motion_commands_sent"] == 0
    assert len(log) == before  # phase change has no motion or stop side effect
    grasp = facade.recording("set_phase", {"phase": "grasp"})
    assert grasp["motion_commands_sent"] == 0
    assert facade.status()["channels"]["base"]["permitted"] is False
    ended = facade.recording("end_episode", {})
    assert ended["stopped_first"] is True
    assert log[-2:] == ["driver:stop", "car:stop"]
    assert len(facade.status()["recording"]["phases"]) == 2
    assert facade.recording("stop_session", {})["recording"]["status"] == "stopped"


def test_reconnect_and_calibration_change_park_without_auto_follow():
    facade = build_fixture_facade()
    facade.acquire_session({"owner": "operator"})
    for action in ("preview", "align", "arm", "engage"):
        facade.set_arm_state(action)
    out = facade.set_arm_state("reconnect")
    assert out["state"] == "fault"
    assert out["channels"]["arm"]["state"] == "idle"
    assert out["channels_meta"]["pending"] == {}
    assert out["outputs_enabled"] is False


def test_calibration_strict_policy_rejects_examples_mismatch_and_over_age():
    assets = calibration_assets_fixture()
    now_ts = datetime.fromisoformat("2026-11-04T00:00:00+00:00").timestamp()
    assert validate_asset_strict("base", assets["base"], now_ts=now_ts) == []
    example = json.loads((ROOT / "docs/evidence/parallel-20261104/T3/calibration-schema-example.json").read_text())
    assert not validate_bundle(example["bundle"], example["assets"])["ready"]

    wrong_role = copy.deepcopy(assets["base"])
    wrong_role["role"] = "so101_leader"
    assert "role_mismatch" in validate_asset_strict("base", wrong_role)
    wrong_version = copy.deepcopy(assets["base"])
    wrong_version["schema_version"] = 2
    assert "schema_or_kind_mismatch" in validate_asset_strict("base", wrong_version)
    missing_joint = copy.deepcopy(assets["follower"])
    missing_joint["limits_rad"].pop("r_joint")
    assert "follower_limits_invalid" in validate_asset_strict("follower", missing_joint)
    nonfinite = copy.deepcopy(assets["base"])
    nonfinite["wheel_geometry"]["wheel_radius_m"] = float("nan")
    assert "nonfinite_or_non_json_value" in validate_asset_strict("base", nonfinite)
    simulated = copy.deepcopy(assets["base"])
    simulated["simulated"] = True
    assert "simulated_or_example_asset" in validate_asset_strict("base", simulated)
    stale = copy.deepcopy(assets["base"])
    stale["measured_at_utc"] = "2020-01-01T00:00:00Z"
    assert "calibration_over_age" in validate_asset_strict("base", stale)
    missing_time = copy.deepcopy(assets["base"])
    missing_time.pop("measured_at_utc")
    assert "measurement_time_missing_or_invalid" in validate_asset_strict("base", missing_time)


def test_calibration_bundle_fixture_is_complete_and_hash_bound():
    document = calibration_document_fixture()
    assert set(document["assets"]) == set(KINDS)
    assert document["joint_order"] == JOINTS
    result = validate_bundle(document["bundle"], document["assets"])
    assert result["ready"] is True, result["reasons"]
    assert result["bundle_hash"]
    tampered = copy.deepcopy(document["assets"])
    tampered["follower"]["limits_rad"]["joint1"] = [-9.0, 9.0]
    assert not validate_bundle(document["bundle"], tampered)["ready"]


def test_capabilities_report_unknown_camera_and_firmware_explicitly():
    facade = build_fixture_facade()
    caps = facade.capabilities()
    assert caps["available"] is False and caps["outputs_enabled"] is False
    assert caps["camera_capability"] == "unknown"
    assert caps["firmware_capability"] == "unknown"
    assert caps["firmware_watchdog_verified"] is False
    assert caps["hardware_stop_effect"] == "unverified"
    assert caps["controller_acknowledged"] == "unavailable"
    assert "command_cache" in caps["rejected_feedback_sources"]
    json.dumps(caps, allow_nan=False)
