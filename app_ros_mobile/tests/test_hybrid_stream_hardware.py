"""Frame facade tests use only in-process simulated devices and a loopback server."""
from __future__ import annotations

import copy
import math
import time
import threading

import pytest

from lelab import hybrid_hardware, hybrid_stream_hardware
from lelab.hybrid_calibration import HybridCalibrationStore
from lelab.hybrid_fixtures import calibration_assets_fixture
from lelab.hybrid_home_plan import HOME_RAW
from lelab.hybrid_recording import HybridRecordingController, RecordingError
from lelab.teleop_stream import StreamServer

RADIANS_PER_TICK = math.pi / 750.0

def _car_status(raw=None, sample_seq=3, publisher_instance=4242, publisher_generation=0):
    raw = ({"joint1": 500, "joint2": 728, "joint3": 124,
            "joint4": 99, "joint5": 499, "r_joint": 150}
           if raw is None else dict(raw))
    ids = {"joint1": 1, "joint2": 2, "joint3": 3, "joint4": 4, "joint5": 5, "r_joint": 10}
    samples = {name: {"name": name, "servo_id": ids[name], "raw_position": value,
                       "source_kind": "servo_position_readback", "sample_seq": sample_seq,
                       "age_s": 0.02, "received_mono_ns": 1, "sample_time_ns": 2,
                       "valid": True, "error": None} for name, value in raw.items()}
    return {"control": {"active": True, "follower_feedback": {
                "available": True, "reason": "ok", "joint_samples": samples,
                "scan_span_s": 0.04, "stale_after_s": 0.5,
                "publisher_instance": publisher_instance,
                "publisher_generation": publisher_generation,
                "publisher_identity_kind": "software_owner_instance"}},
            "hybrid": {"driver_bound": True, "outputs_enabled": False}, "sensors": {}}


class Reader:
    def __init__(self):
        self.reads = 0

    def snapshot(self):
        self.reads += 1
        return {"connected": True, "healthy": True, "age_s": 0.001, "reads": self.reads,
                "positions_rad": {"shoulder_pan": 0.1, "shoulder_lift": -0.2,
                                  "elbow_flex": 0.3, "wrist_flex": 0.4, "wrist_roll": 0.5},
                "gripper_percent": 42.0, "source": "simulated_leader"}


class Upper:
    def __init__(self):
        self.reader = Reader()
        self.active = False

    def acquire(self, _request):
        self.active = True
        return "upper-test"

    def valid(self, token):
        return self.active and token == "upper-test"

    def release(self, _token, _reason):
        self.active = False


@pytest.fixture
def rig(tmp_path, monkeypatch):
    assets = calibration_assets_fixture(measured_at_utc="2026-11-04T00:00:00Z")
    # The gripper linkage reverses the raw pulse direction. Match the mapper's
    # gripper conversion, so an unchanged measured pulse is a continuous target.
    for name in ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"):
        assets["follower"]["raw_to_rad"][name] = {
            "zero_tick": 500,
            "centre_raw": 500,
            "direction": -1 if name == "r_joint" else 1,
            "radians_per_tick": math.pi / 750.0,
        }
    store = HybridCalibrationStore(tmp_path)
    ids = {}
    for kind in ("leader", "follower", "mapping"):
        ids[kind] = assets[kind]["identity"]
        store.save(kind, ids[kind], assets[kind])
    upper = Upper()
    state = {"active": False, "session": {}, "calls": [], "frames": [], "enabled": False,
             "raw": {"joint1": 500, "joint2": 728, "joint3": 124,
                     "joint4": 99, "joint5": 499, "r_joint": 150},
             "sample_seq": 3, "homing_sim": False, "home_transitions": [],
             "publisher_instance": 4242, "publisher_generation": 0,
             "drift_per_frame": 0}

    def http(_url, path, *, method="GET", body=None, token=None, timeout=4.0):
        del method, token, timeout
        state["calls"].append(path)
        if path == "/status":
            status = _car_status(state["raw"], state["sample_seq"],
                                 state["publisher_instance"], state["publisher_generation"])
            status["profile"] = "hardware_hybrid_manual_v1"
            status["read_only"] = False
            status["control"]["active"] = state["active"]
            status["hybrid"].update(state["session"])
            status["licence"] = {"output_transport": "frame_stream",
                                 "bridge": {"frame_process": {
                                     "age_s": 0.01, "ticks": state["sample_seq"],
                                     "outputs_enabled": state["enabled"],
                                     "homing_motion": {"enable_torque": True},
                                     "homing_torque_state": "verified",
                                     "homing_torque_error": None,
                                     "queue_depth": 0, "latched_fault": None}}}
            return status
        if path == "/control/acquire":
            state["active"] = True
            return {"token": "car-test"}
        if path == "/hybrid/begin_epoch":
            state["session"] = {k: body[k] for k in ("session_id", "session_epoch", "calibration_bundle_hash")}
            return {"driver_bound": True, "outputs_enabled": False, **state["session"]}
        if path == "/hybrid/enable_output":
            state["enabled"] = True
            return {"outputs_enabled": True, "emitter": "car_control_process"}
        if path == "/hybrid/stop":
            state["enabled"] = False
            return {"stopped": True}
        if path == "/control/release":
            state["active"] = False
            return {"ok": True}
        if path == "/control/renew":
            return {"ok": True}
        raise AssertionError(f"unexpected endpoint {path}")

    monkeypatch.setattr(hybrid_hardware, "_http_json", http)
    monkeypatch.setattr(hybrid_stream_hardware, "_http_json", http)
    window = {"window_id": 1, "window_remaining_ms": 10000.0}

    def on_frame(frame):
        state["frames"].append(frame)
        if state["homing_sim"]:
            before = dict(state["raw"])
            after = dict(before)
            arm = frame["commands"]["arm"]["target"]
            for name, value in zip(("joint1", "joint2", "joint3", "joint4", "joint5"),
                                   arm["positions_rad"], strict=True):
                direction = 1 if name != "r_joint" else -1
                target = int(round(500 + direction * float(value) / RADIANS_PER_TICK))
                after[name] = before[name] + max(-7, min(7, target - before[name]))
            grip = frame["commands"]["gripper"]["target"]["position_rad"]
            target = int(round(500 - float(grip) / RADIANS_PER_TICK))
            after["r_joint"] = before["r_joint"] + max(-7, min(7, target - before["r_joint"]))
            drift = state.get("drift_per_frame") or 0
            if isinstance(drift, list):
                drift = int(drift.pop(0)) if drift else 0
            if drift:
                drift_joint = state.get("drift_joint") or "joint2"
                after[drift_joint] = after[drift_joint] + int(drift)
            changed = [name for name in after if after[name] != before[name]]
            state["home_transitions"].append({"before": before, "after": after,
                                              "changed": changed})
            state["raw"] = after
            state["sample_seq"] += 1
        return {"type": "receipt", "state": "admitted", "frame_seq": frame["frame_seq"]}

    server = StreamServer({"on_hello": lambda *_: (True, {}), "on_frame": on_frame,
                           "on_preempt": lambda *_: {"type": "receipt", "state": "stopped"},
                           "window_provider": lambda: dict(window), "stream_epoch_provider": lambda: 1},
                          port=0, idle_timeout_s=0.05)
    server.start()
    car = hybrid_stream_hardware.FrameCarControlLease("http://sim.invalid")
    observer = hybrid_stream_hardware.StreamHardwareObserver(
        upper=upper, agent_url="http://sim.invalid", store=store,
        leader_identity=ids["leader"], follower_identity=ids["follower"],
        mapping_identity=ids["mapping"])
    car.observer = observer
    facade = hybrid_stream_hardware.FrameStreamHardwareFacade(
        upper=upper, car=car, observer=observer, calibration_store=store)
    facade.bind_driver(hybrid_stream_hardware.FrameStreamDriver(
        car, observer, "http://sim.invalid", host="127.0.0.1", port=server.port))
    yield facade, state
    if facade.session_id is not None:
        facade.release_session("test_cleanup", facade.session_id, facade.session_epoch)
    server.stop()


def test_existing_session_drives_complete_frames_and_release_stops(rig):
    facade, state = rig
    acquired = facade.acquire_session({"owner": "offline-test"})
    identity = {"session_id": acquired["session_id"], "session_epoch": acquired["session_epoch"]}
    facade.preview(identity)
    for action in ("align", "arm", "engage"):
        facade.set_arm_state({"action": action, **identity})
    deadline = time.monotonic() + 2
    while len(state["frames"]) < 2 and time.monotonic() < deadline:
        time.sleep(0.03)
    assert len(state["frames"]) >= 2, facade.status()
    assert all(set(frame["commands"]) == {"arm", "gripper"} for frame in state["frames"])
    assert all(frame["session_id"] == identity["session_id"] for frame in state["frames"])
    assert "/hybrid/apply" not in state["calls"]
    facade.set_arm_state({"action": "release_clutch", **identity})
    count = len(state["frames"])
    time.sleep(0.25)
    assert len(state["frames"]) == count
    assert state["enabled"] is False
    facade.release_session("test_finished", **identity)
    assert not state["active"]


def test_homing_walks_nonzero_axes_to_requested_raw_one_at_a_time(rig):
    facade, state = rig
    state["raw"].update({"joint1": 520, "joint2": 480, "joint3": 500,
                         "joint4": 500, "joint5": 500, "r_joint": 440})
    state["sample_seq"] = 20
    acquired = facade.acquire_session({"owner": "homing-test"})
    identity = {"session_id": acquired["session_id"],
                "session_epoch": acquired["session_epoch"],
                "calibration_bundle_hash": acquired["calibration_bundle_hash"]}
    state["homing_sim"] = True
    facade.start_home({**identity, "action": "start"})
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        task = facade.status().get("homing") or {}
        if task.get("state") in {"complete", "fault"}:
            break
        time.sleep(0.05)
    task = facade.status()
    assert task["homing"]["state"] == "complete", {
        "homing": task["homing"], "last_error": task.get("last_error"),
        "raw": state["raw"], "transitions": state["home_transitions"][:4]}
    assert all(abs(state["raw"][name] - target) <= 5
               for name, target in HOME_RAW.items())
    assert all(abs(task["homing"]["observed_raw"][name] - target) <= 5
               for name, target in HOME_RAW.items())
    assert task["homing"]["outputs_enabled"] is False
    assert task["outputs_enabled"] is False
    assert task["state"] == "ready"
    assert state["home_transitions"]
    assert all(len(item["changed"]) == 1 for item in state["home_transitions"])
    facade.release_session("homing_test_done",
                           session_id=identity["session_id"],
                           session_epoch=identity["session_epoch"])


def test_homing_cancel_stops_without_waiting_for_a_stall(rig):
    facade, state = rig
    acquired = facade.acquire_session({"owner": "homing-cancel-test"})
    identity = {"session_id": acquired["session_id"],
                "session_epoch": acquired["session_epoch"],
                "calibration_bundle_hash": acquired["calibration_bundle_hash"]}
    # The fake feedback deliberately does not advance, so this can only finish
    # through the explicit cancel path.
    state["homing_sim"] = False
    facade.start_home({**identity, "action": "start"})
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not state["frames"]:
        time.sleep(0.02)
    assert state["frames"]
    result = facade.cancel_home({**identity, "action": "cancel"})
    assert result["homing"]["state"] == "cancelled"
    assert result["outputs_enabled"] is False
    facade.release_session("homing_cancel_done",
                           session_id=identity["session_id"],
                           session_epoch=identity["session_epoch"])


def test_single_axis_homing_moves_only_joint1(rig):
    facade, state = rig
    state["raw"].update({**HOME_RAW, "joint1": HOME_RAW["joint1"] + 20})
    expected = dict(state["raw"])
    expected["joint1"] = HOME_RAW["joint1"]
    state["sample_seq"] = 30
    acquired = facade.acquire_session({"owner": "single-axis-homing-test"})
    identity = {"session_id": acquired["session_id"],
                "session_epoch": acquired["session_epoch"],
                "calibration_bundle_hash": acquired["calibration_bundle_hash"]}
    state["homing_sim"] = True
    facade.start_home({**identity, "action": "start", "only_joint": "joint1"})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = facade.status().get("homing") or {}
        if task.get("state") in {"complete", "fault"}:
            break
        time.sleep(0.05)
    status = facade.status()
    task = status["homing"]
    assert task["state"] == "complete", {
        "homing": task, "last_error": status.get("last_error"), "raw": state["raw"]}
    assert task["scope"] == "single_axis" and task["only_joint"] == "joint1"
    assert abs(state["raw"]["joint1"] - HOME_RAW["joint1"]) <= 5
    assert all(state["raw"][name] == expected[name]
               for name in ("joint2", "joint3", "joint4", "joint5", "r_joint"))
    assert state["home_transitions"]
    assert all(item["changed"] == ["joint1"] for item in state["home_transitions"])
    facade.release_session("single_axis_homing_done",
                           session_id=identity["session_id"],
                           session_epoch=identity["session_epoch"])


def test_factory_requires_explicit_supported_transport(monkeypatch):
    monkeypatch.setenv("LELAB_HYBRID_TRANSPORT", "unknown")
    with pytest.raises(hybrid_hardware.HardwareCompositionError, match="unknown_hybrid_transport"):
        hybrid_hardware.build_hardware_facade("http://127.0.0.1:1")


def test_frame_lease_rejects_legacy_agent(monkeypatch):
    monkeypatch.setattr(hybrid_stream_hardware, "_http_json", lambda *_a, **_k: {"licence": {}})
    lease = hybrid_stream_hardware.FrameCarControlLease("http://127.0.0.1:1")
    with pytest.raises(hybrid_hardware.HardwareCompositionError, match="frame_transport_not_available"):
        lease.acquire({"owner": "offline-test"})


def test_status_polling_does_not_advance_mapper_before_engage(rig):
    facade, _ = rig
    acquired = facade.acquire_session({"owner": "offline-test"})
    identity = {"session_id": acquired["session_id"], "session_epoch": acquired["session_epoch"]}
    facade.preview(identity)
    facade.set_arm_state({"action": "align", **identity})
    initial = facade.observer.mapper._last_wall
    estimate = facade.status().get("hardware_preview")
    assert estimate and estimate["available"] is True
    assert estimate["preview_mapper"] == "independent_alignment_snapshot"
    assert estimate["motion_commands_sent"] == 0
    for _ in range(4):
        facade.status()
        time.sleep(0.03)
    assert facade.observer.mapper._last_wall == initial


def test_trial_cap_tightens_runtime_mapping_without_rewriting_calibration(rig):
    facade, _ = rig
    observer = facade.observer
    original = observer.store.load("mapping", observer.identities["mapping"]).data
    original_limit = original.get("max_step_rad_s", 1.0)
    observer.trial_max_step_rad_s = 0.10
    observer.filter_alpha_override = 0.35
    observer.deadband_rad_override = 0.004
    acquired = facade.acquire_session({"owner": "offline-test"})
    identity = {"session_id": acquired["session_id"], "session_epoch": acquired["session_epoch"]}
    facade.preview(identity)
    facade.set_arm_state({"action": "align", **identity})
    assert observer.mapper.max_step_rad_s == min(original_limit, 0.10)
    assert observer.mapper.filter_alpha == min(
        original.get("filter_alpha", 1.0), 0.35)
    assert observer.mapper.deadband_rad >= 0.004
    assert observer._display_mapper.max_step_rad_s == observer.mapper.max_step_rad_s
    assert observer.store.load("mapping", observer.identities["mapping"]).data == original


def test_engage_rebases_after_leader_reposition_and_rejects_stale_identity(rig):
    facade, state = rig
    acquired = facade.acquire_session({"owner": "offline-test"})
    identity = {"session_id": acquired["session_id"], "session_epoch": acquired["session_epoch"]}
    facade.preview(identity)
    facade.set_arm_state({"action": "align", **identity})
    old_reference = dict(facade.observer.reference["leader_rad"])
    with pytest.raises(Exception):
        facade.set_arm_state({"action": "align", "session_id": "stale", "session_epoch": 0})
    assert facade.observer.reference["leader_rad"] == old_reference
    original = facade.upper.reader.snapshot

    def repositioned():
        sample = original()
        sample["positions_rad"]["shoulder_pan"] += 0.3
        return sample

    facade.upper.reader.snapshot = repositioned
    facade.set_arm_state({"action": "arm", **identity})
    facade.set_arm_state({"action": "engage", **identity})
    assert facade.observer.reference["leader_rad"]["joint1"] == pytest.approx(
        old_reference["joint1"] + 0.3)
    deadline = time.monotonic() + 2
    while not state["frames"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert state["frames"]
    facade.set_arm_state({"action": "release_clutch", **identity})


def test_emergency_preempts_sender_and_old_frames(rig):
    facade, state = rig
    acquired = facade.acquire_session({"owner": "offline-test"})
    identity = {"session_id": acquired["session_id"], "session_epoch": acquired["session_epoch"]}
    facade.preview(identity)
    for action in ("align", "arm", "engage"):
        facade.set_arm_state({"action": action, **identity})
    deadline = time.monotonic() + 2
    while not state["frames"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert state["frames"]
    facade.emergency_stop("offline-test")
    count = len(state["frames"])
    time.sleep(0.25)
    assert len(state["frames"]) == count
    assert facade.state == "estop"


def test_a_clear_needs_the_emitter_to_report_its_own_stop_lifted(rig, monkeypatch):
    """Confirmation is two-layer, and an absent report is unknown, not clear.

    The agent answering "no stop" only proves the permission authority agreed. The
    control process keeps a latch of its own and refuses to admit or emit until it has
    seen a clearance newer than what it latched on, so a half-applied stop must never be
    reported to the operator as a cleared one.
    """
    facade, _state = rig
    ok = {"estop": False, "authorised_by": "control_token", "clear_estop_sent": True,
          "announcements": "all"}
    monkeypatch.setattr(facade, "_car_answer",
                        lambda path, body, token, *a, **k: dict(ok))
    half = {"agent_estop_latched": False, "process_latched_fault": "estop",
            "process_witness": "fresh", "clear_still_queued": False,
            "process_outputs_enabled": False, "process_age_s": 0.01, "queue_depth": 0}
    monkeypatch.setattr(facade, "_car_estop_state", lambda: dict(half))
    started = time.monotonic()
    assert facade._car_clear_estop()["car_clear"] == "pending_or_failed"
    assert time.monotonic() - started < 3.0, "the wait for the emitter is bounded"
    blind = dict(half, process_latched_fault=None, process_witness="absent",
                 clear_still_queued=True)
    monkeypatch.setattr(facade, "_car_estop_state", lambda: dict(blind))
    assert facade._car_clear_estop()["car_clear"] == "pending_or_failed"
    fresh = dict(blind, process_witness="fresh", clear_still_queued=False)
    monkeypatch.setattr(facade, "_car_estop_state", lambda: dict(fresh))
    assert facade._car_clear_estop()["car_clear"] == "confirmed"


def test_unbound_clear_estop_reaches_the_car_latch(rig, monkeypatch):
    facade, _state = rig
    facade.estop_latched = True
    facade.state = "estop"
    seen = {}
    monkeypatch.setattr(
        facade, "_car_clear_estop",
        lambda owner=None: seen.update(owner=owner) or {
            "car_clear": "confirmed", "agent_estop_latched": False,
            "process_latched_fault": None})
    out = facade.clear_estop_unbound(owner="prior-browser")
    assert out["state"] == "disconnected"
    assert out["car_clear"]["car_clear"] == "confirmed"
    assert seen["owner"] == "prior-browser"


def test_clear_estop_uses_the_client_recorded_by_the_car(rig, monkeypatch):
    facade, _state = rig
    calls = []
    confirmed = {
        "agent_estop_latched": False,
        "process_latched_fault": None,
        "process_witness": "fresh",
        "clear_still_queued": False,
        "process_outputs_enabled": False,
        "process_age_s": 0.01,
        "queue_depth": 0,
    }

    def car_answer(path, body, token, *args, **kwargs):
        calls.append((body["client_id"], token))
        if body["client_id"] == "browser-id":
            return {"error": "car_http_403:control_token: missing or invalid control token"}
        return {"estop": False, "authorised_by": "prior_owner_recovery",
                "clear_estop_sent": True, "announcements": "all"}

    monkeypatch.setattr(facade, "_car_answer", car_answer)
    monkeypatch.setattr(facade, "_car_estop_state", lambda: dict(confirmed))
    monkeypatch.setattr(facade, "_car_estop_owner_hint",
                        lambda: "recorded-car-client")
    out = facade._car_clear_estop("browser-id")
    assert out["car_clear"] == "confirmed"
    assert out["authorised_by"] == "prior_owner_recovery"
    assert ("recorded-car-client", None) in calls


def test_clear_estop_uses_a_temporary_token_when_no_owner_is_recorded(rig, monkeypatch):
    facade, _state = rig
    calls = []
    confirmed = {
        "agent_estop_latched": False,
        "process_latched_fault": None,
        "process_witness": "fresh",
        "clear_still_queued": False,
        "process_outputs_enabled": False,
        "process_age_s": 0.01,
        "queue_depth": 0,
    }

    def car_answer(path, body, token, *args, **kwargs):
        calls.append((path, dict(body), token))
        if path == "/control/clear_estop":
            if token != "temporary-token":
                return {"error": "car_http_403:control_token: missing or invalid control token"}
            return {"estop": False, "authorised_by": "control_token",
                    "clear_estop_sent": True, "announcements": "all"}
        if path == "/control/acquire":
            return {"token": "temporary-token"}
        if path == "/control/release":
            return {"ok": True}
        raise AssertionError(path)

    monkeypatch.setattr(facade, "_car_answer", car_answer)
    monkeypatch.setattr(facade, "_car_estop_owner_hint", lambda: None)
    monkeypatch.setattr(facade, "_car_estop_state", lambda: dict(confirmed))
    out = facade._car_clear_estop("browser-id")
    assert out["car_clear"] == "confirmed"
    assert any(path == "/control/acquire" for path, _body, _token in calls)
    assert any(path == "/control/release" and token == "temporary-token"
               for path, _body, token in calls)


def test_wrong_session_cannot_stop_worker_or_observer(rig):
    facade, state = rig
    acquired = facade.acquire_session({"owner": "offline-test"})
    identity = {"session_id": acquired["session_id"], "session_epoch": acquired["session_epoch"]}
    facade.preview(identity)
    for action in ("align", "arm", "engage"):
        facade.set_arm_state({"action": action, **identity})
    with pytest.raises(Exception):
        facade.release_session("foreign", "wrong-session", identity["session_epoch"])
    assert not facade._follow_stop.is_set()
    assert facade.observer._poll_thread is not None
    assert state["active"]


def test_stalled_leader_does_not_replay_old_sample(rig):
    facade, state = rig
    acquired = facade.acquire_session({"owner": "offline-test"})
    identity = {"session_id": acquired["session_id"], "session_epoch": acquired["session_epoch"]}
    facade.preview(identity)
    for action in ("align", "arm", "engage"):
        facade.set_arm_state({"action": action, **identity})
    deadline = time.monotonic() + 2
    while not state["frames"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert state["frames"]
    reader = facade.upper.reader
    frozen = reader.snapshot()
    reader.snapshot = lambda: dict(frozen)
    deadline = time.monotonic() + 2
    while facade.state == "ready" and time.monotonic() < deadline:
        time.sleep(0.03)
    assert facade.state == "fault"
    time.sleep(0.2)
    count = len(state["frames"])
    time.sleep(0.25)
    assert len(state["frames"]) == count


def test_reengage_requires_fresh_epoch_binding(rig):
    facade, state = rig
    acquired = facade.acquire_session({"owner": "offline-test"})
    identity = {"session_id": acquired["session_id"], "session_epoch": acquired["session_epoch"]}
    for cycle in range(2):
        facade.preview(identity)
        for action in ("align", "arm", "engage"):
            facade.set_arm_state({"action": action, **identity})
        deadline = time.monotonic() + 1
        before = len(state["frames"])
        while len(state["frames"]) == before and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(state["frames"]) > before
        facade.set_arm_state({"action": "release_clutch", **identity})
    assert state["calls"].count("/hybrid/begin_epoch") == 2


def test_offline_6d_requires_simulated_identity_and_records_car_id():
    class RecordingFacade:
        simulated = False
        def status(self):
            return {"state": "ready", "session_id": "control", "session_epoch": 1,
                    "simulated": self.simulated, "calibration_bundle_hash": "a" * 64}
    class RecordingCar:
        def post(self, _path, body):
            assert body["export_schema"] == "arm_gripper_6dof_phase1"
            return {"session_id": "recording", "clock_domain": "car_ros"}
    facade = RecordingFacade()
    controller = HybridRecordingController(facade, RecordingCar(), offline_6d=True)
    with pytest.raises(RecordingError, match="offline_6d_identity_mismatch"):
        controller.start_session({"profile": "hardware_hybrid_manual_v1_offline6d"})
    facade.simulated = True
    result = controller.start_session({"profile": "hardware_hybrid_manual_v1_offline6d"})
    assert result["session_id"] == "control"
    assert result["car_recording_id"] == "recording"


def test_phase_barrier_confirms_quiescence_and_applied_context(monkeypatch):
    facade = object.__new__(hybrid_stream_hardware.FrameStreamHardwareFacade)
    facade.car = type("Car", (), {"agent_url": "http://sim.invalid"})()
    facade.session_id, facade.session_epoch, facade.bundle_hash = "control", 7, "bundle"
    states = iter([
        {"age_s": 0.01, "ticks": 1, "outputs_enabled": True, "queue_depth": 1},
        {"age_s": 0.01, "ticks": 2, "outputs_enabled": False, "queue_depth": 0},
        {"age_s": 0.01, "ticks": 3, "outputs_enabled": False, "queue_depth": 0,
         "context": {"session_id": "recording", "session_epoch": 7, "episode_id": "ep",
                     "phase": "grasp", "clock_epoch": "clock", "context_seq": 2,
                     "calibration_bundle_hash": "bundle"}},
    ])
    def status(*_args, **_kwargs):
        return {"control": {"active": True}, "hybrid": {"session_id": "control",
            "session_epoch": 7, "calibration_bundle_hash": "bundle"},
            "licence": {"bridge": {"frame_process": next(states)}}}
    calls = []
    monkeypatch.setattr(hybrid_stream_hardware, "_http_json", status)
    monkeypatch.setattr(facade, "set_arm_state", lambda event: calls.append(event))
    facade.prepare_recording_phase()
    facade.confirm_recording_phase(recording_id="recording", episode_id="ep",
                                   phase="grasp", clock_epoch="clock")
    assert calls == ["pause_all"]


def test_heartbeat_releases_facade_lock_before_recording_status(rig, monkeypatch):
    facade, _ = rig
    facade.acquire_session({"owner": "offline-test"})
    class Recording:
        def __init__(self):
            self.lock = threading.Lock()
        def status(self):
            with self.lock:
                return {"status": "active"}
    recording = Recording()
    facade.bind_recording_controller(recording, replace=True)
    renewed = threading.Event()
    monkeypatch.setattr(facade.car, "renew", lambda _token: renewed.set())
    monkeypatch.setattr(facade.upper, "renew", lambda _token: None, raising=False)
    recording.lock.acquire()
    worker = threading.Thread(target=facade.heartbeat, daemon=True)
    try:
        worker.start()
        assert renewed.wait(2)
        time.sleep(0.1)
        assert facade.lock.acquire(timeout=1), "heartbeat kept facade lock while waiting for recorder"
        facade.lock.release()
    finally:
        recording.lock.release()
        worker.join(timeout=2)
    assert not worker.is_alive()


def _frozen_cache(facade, monkeypatch):
    """Stop the poller from refreshing, so the cache is old but the lease is not."""
    monkeypatch.setattr(facade.observer, "_poll", lambda: None)
    time.sleep(facade.car.CACHE_MAX_AGE_S + 0.1)


def test_a_stale_observation_cache_probes_the_car_instead_of_blaming_the_lease(rig, monkeypatch):
    """The cache going old is the poller being slow, not the car taking the lease back.

    This distinction is what a lost lease is judged on: a slow car read used to be
    reported as ``lease_lost`` and then expired a healthy session for real, because the
    operator heartbeat stops the moment the facade leaves ``ready``.
    """
    facade, state = rig
    facade.acquire_session({"owner": "offline-test"})
    _frozen_cache(facade, monkeypatch)
    before = state["calls"].count("/status")

    assert facade.car.valid(facade.car_token) is True, facade.car.last_reason
    assert facade.car.last_reason == "car_lease_held"
    assert facade.car.last_probe_source == "probe"
    assert state["calls"].count("/status") == before + 1


def test_a_stale_cache_probes_at_most_once_per_freshness_window(rig, monkeypatch):
    facade, state = rig
    facade.acquire_session({"owner": "offline-test"})
    _frozen_cache(facade, monkeypatch)
    before = state["calls"].count("/status")

    for _ in range(3):
        assert facade.car.valid(facade.car_token) is True
    assert state["calls"].count("/status") == before + 1, "a slow car must not be hammered"


def test_an_unreachable_car_still_parks_the_session(rig, monkeypatch):
    """Fail closed, but say it is the read that failed rather than inventing a lost lease."""
    facade, _state = rig
    facade.acquire_session({"owner": "offline-test"})
    _frozen_cache(facade, monkeypatch)

    def unreachable(_url, path, **_kwargs):
        if path == "/status":
            raise hybrid_hardware.HardwareCompositionError("car_unreachable:timed out")
        raise AssertionError(f"unexpected endpoint {path}")

    monkeypatch.setattr(hybrid_stream_hardware, "_http_json", unreachable)
    monkeypatch.setattr(hybrid_hardware, "_http_json", unreachable)
    assert facade.car.valid(facade.car_token) is False, facade.car.last_reason
    assert facade.car.last_reason.startswith("car_status_read_failed:car_unreachable")


def test_a_retracted_car_lease_is_named_in_the_facade_error(rig):
    facade, state = rig
    acquired = facade.acquire_session({"owner": "offline-test"})
    identity = {"session_id": acquired["session_id"], "session_epoch": acquired["session_epoch"]}
    facade.preview(identity)
    state["active"] = False
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        cached = facade.observer.car_snapshot()[0] or {}
        if (cached.get("control") or {}).get("active") is False:
            break
        time.sleep(0.02)

    status = facade.status()
    assert status["state"] == "fault", status["last_error"]
    assert "lease_lost:car=car_control_inactive" in status["last_error"]
    assert status["leases"]["car"]["held"] is False
    assert status["leases"]["car"]["reason"] == "car_control_inactive"
    assert status["leases"]["car"]["expires_in_s"] > 0.0, "the local TTL was still live"


def test_the_upper_lease_reports_its_own_reason(rig):
    facade, _state = rig
    acquired = facade.acquire_session({"owner": "offline-test"})
    identity = {"session_id": acquired["session_id"], "session_epoch": acquired["session_epoch"]}
    facade.preview(identity)
    facade.upper.active = False

    status = facade.status()
    assert "lease_lost:upper=not_held" in status["last_error"]
    assert status["leases"]["upper"]["held"] is False


def test_status_carries_heartbeat_and_cache_timing(rig, monkeypatch):
    facade, _state = rig
    facade.acquire_session({"owner": "offline-test"})
    monkeypatch.setattr(facade.upper, "renew", lambda _token: None, raising=False)
    facade.heartbeat()

    diagnostics = facade.status()["diagnostics"]
    assert diagnostics["heartbeats"] == 1
    assert diagnostics["last_heartbeat_outcome"] == "ok"
    assert diagnostics["operator_presence_age_s"] < 1.0
    assert diagnostics["observation_cache"]["running"] is True
    assert diagnostics["observation_cache"]["cache_age_s"] <= 0.6
    assert [entry["kind"] for entry in diagnostics["state_changes"]][-1] == "state_change"


def test_follow_refreshes_a_late_observer_cache_before_rejecting_it(rig, monkeypatch):
    facade, _state = rig
    facade.acquire_session({"owner": "fresh-cache-test"})
    observer = facade.observer
    original_poll = observer._poll
    calls = []

    def counted_poll(*, timeout_s=None, total_timeout_s=None):
        calls.append((timeout_s, total_timeout_s))
        return original_poll(timeout_s=timeout_s, total_timeout_s=total_timeout_s)

    monkeypatch.setattr(observer, "_poll", counted_poll)
    with observer._cache_lock:
        observer._cache_started_at = time.monotonic() - 0.30
    facts = observer.fresh_snapshot(max_cache_age_s=0.20, timeout_s=0.35)
    assert calls == [(0.35, 0.35)]
    assert facts["feedback_valid"] is True
    assert observer.cache_state()["cache_age_s"] < 0.20


def test_cache_refresh_total_budget_includes_poll_lock_wait(rig):
    facade, _state = rig
    facade.acquire_session({"owner": "cache-budget-test"})
    observer = facade.observer
    with observer._cache_lock:
        observer._cache_started_at = time.monotonic() - 0.30
    observer._poll_lock.acquire()
    try:
        started = time.monotonic()
        _facts = observer.fresh_snapshot(max_cache_age_s=0.20, timeout_s=0.05)
        elapsed = time.monotonic() - started
    finally:
        observer._poll_lock.release()
    assert elapsed < 0.20
    assert "observer_poll_lock_timeout" in observer.cache_state()["last_error"]


def test_one_missed_feedback_scan_is_recovered_without_sending_stale_data(rig):
    facade, _state = rig
    facade.acquire_session({"owner": "feedback-retry-test"})
    fresh = facade.observer.snapshot()
    stale = copy.deepcopy(fresh)
    now_ns = time.monotonic_ns()
    for sample in stale["feedback"]["samples"]:
        sample["age_s"] = 1.10
        sample["age_ns"] = 1_100_000_000
        sample["received_mono_ns"] = now_ns - 1_100_000_000
        sample["valid"] = False
        sample["error"] = "sample_invalid,sample_stale,feedback_stale:1.04"
    stale["feedback"]["valid"] = False
    stale_gate = facade._feedback_gate_for(
        stale, facade.bundle_hash, now_ns)
    assert facade._transient_feedback_gap(stale, stale_gate) is True
    recovered, recovered_gate = facade._recover_transient_feedback(
        stale, stale_gate, threading.Event())
    assert recovered_gate["trusted"] is True
    assert recovered["feedback"]["valid"] is True


def test_persistent_feedback_gap_still_faults_after_the_retry_window(rig, monkeypatch):
    facade, _state = rig
    facade.acquire_session({"owner": "feedback-retry-timeout-test"})
    fresh = facade.observer.snapshot()
    stale = copy.deepcopy(fresh)
    now_ns = time.monotonic_ns()
    for sample in stale["feedback"]["samples"]:
        sample["age_s"] = 1.10
        sample["age_ns"] = 1_100_000_000
        sample["received_mono_ns"] = now_ns - 1_100_000_000
        sample["valid"] = False
        sample["error"] = "sample_invalid,sample_stale,feedback_stale:1.04"
    stale["feedback"]["valid"] = False
    stale_gate = facade._feedback_gate_for(
        stale, facade.bundle_hash, now_ns)
    monkeypatch.setattr(
        facade.observer, "fresh_snapshot",
        lambda **_kwargs: copy.deepcopy(stale))
    started = time.monotonic()
    _facts, final_gate = facade._recover_transient_feedback(
        stale, stale_gate, threading.Event())
    elapsed = time.monotonic() - started
    assert final_gate["trusted"] is False
    assert 0.20 <= elapsed < 0.60


def test_a_fault_stops_once_and_preserves_the_primary_cause(rig, monkeypatch):
    facade, state = rig
    facade.acquire_session({"owner": "single-stop-test"})
    stop_calls = []

    def failed_stop(reason):
        stop_calls.append(reason)
        raise hybrid_hardware.HardwareCompositionError(
            "car_http_403:control_token: missing or invalid control token")

    monkeypatch.setattr(facade.car, "stop", failed_stop)
    primary = (
        "frame_follow_failed:HardwareCompositionError:follow_feedback_untrusted:"
        "joint1:stale_or_invalid_timestamp")
    detail = {"feedback": {"samples": [{"name": "joint1", "age_s": 0.6}]}}
    facade._follow_fail(primary, detail)
    state["active"] = False
    first = facade.status()
    second = facade.status()
    assert len(stop_calls) == 1
    assert first["primary_error"] == primary
    assert second["primary_error"] == primary
    assert second["last_error"] == primary
    assert second["fault_detail"] == detail
    assert second["last_stop"]["state"] == "failed"
    assert "stop_failed" in second["last_stop"]["error"]


def _episode_barrier_controller(order, fail_prepare=False):
    class Facade:
        simulated = True

        def status(self):
            return {"state": "ready", "session_id": "control", "session_epoch": 7,
                    "simulated": True, "profile": "hardware_hybrid_manual_v1_offline6d",
                    "calibration_bundle_hash": "bundle"}

        def prepare_recording_phase(self):
            order.append("prepare")
            if fail_prepare:
                raise hybrid_stream_hardware.HardwareCompositionError("phase_control_stop_unconfirmed")

    class Car:
        def post(self, path, body):
            order.append(path)
            if path == "/recording/start_session":
                return {"session_id": "recording", "clock_domain": "car_system_time"}
            return {"episode_id": body["episode_id"],
                    "initial": {"time_ns": 1_000, "clock_domain": "car_system_time"}}

    controller = HybridRecordingController(Facade(), Car(), offline_6d=True, background=False)
    controller.start_session({"profile": "hardware_hybrid_manual_v1_offline6d"})
    return controller


def test_episode_start_quiesces_before_the_car_opens_the_window():
    """A frame in the window without the mirrored episode context makes the session unexportable.

    The web closure hit exactly this: following was still live when the episode opened,
    so the first rows carried no agent context and the restricted export refused them.
    """
    order = []
    controller = _episode_barrier_controller(order)
    order.clear()

    controller.start_episode({"episode_id": "ep_a"})

    assert order == ["prepare", "/recording/start_episode"]
    assert controller.status()["episode_id"] == "ep_a"


def test_an_unconfirmed_quiescence_refuses_the_episode_without_opening_it():
    order = []
    controller = _episode_barrier_controller(order, fail_prepare=True)
    order.clear()

    with pytest.raises(RecordingError, match="episode_stop_unconfirmed"):
        controller.start_episode({"episode_id": "ep_b"})

    assert order == ["prepare"]
    status = controller.status()
    assert status["episode_id"] is None
    assert status["last_error"] == "episode_stop_unconfirmed"


def _start_single_axis_home(facade, state, only_joint="joint1"):
    state["raw"].update({**HOME_RAW, only_joint: HOME_RAW[only_joint] + 20})
    state["sample_seq"] = 30
    acquired = facade.acquire_session({"owner": "homing-fault-test"})
    identity = {"session_id": acquired["session_id"],
                "session_epoch": acquired["session_epoch"],
                "calibration_bundle_hash": acquired["calibration_bundle_hash"]}
    facade.start_home({**identity, "action": "start", "only_joint": only_joint})
    return identity


def _wait_home_terminal(facade, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = facade.status().get("homing") or {}
        if task.get("state") in {"complete", "fault", "cancelled", "stopped"}:
            break
        time.sleep(0.05)
    return facade.status()


def test_homing_faults_when_the_feedback_owner_changes(rig):
    facade, state = rig
    state["homing_sim"] = True
    identity = _start_single_axis_home(facade, state)
    deadline = time.monotonic() + 3
    while not state["frames"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert state["frames"]
    state["publisher_instance"] = 9999
    state["publisher_generation"] = 1
    status = _wait_home_terminal(facade)
    task = status["homing"]
    assert task["state"] == "fault", task
    assert "homing_feedback_owner_changed" in str(task["last_error"]), task
    assert task["outputs_enabled"] is False
    assert status["outputs_enabled"] is False
    # No motion resumes on its own after the owner change.
    count = len(state["frames"])
    time.sleep(0.4)
    assert len(state["frames"]) == count
    facade.release_session("owner_change_done",
                           session_id=identity["session_id"],
                           session_epoch=identity["session_epoch"])


def test_homing_refuses_a_publisher_without_an_instance(rig):
    facade, state = rig
    state["publisher_instance"] = None
    acquired = facade.acquire_session({"owner": "no-owner-test"})
    identity = {"session_id": acquired["session_id"],
                "session_epoch": acquired["session_epoch"],
                "calibration_bundle_hash": acquired["calibration_bundle_hash"]}
    with pytest.raises(Exception, match="homing_feedback_publisher_missing"):
        facade.start_home({**identity, "action": "start", "only_joint": "joint1"})
    facade.release_session("no_owner_done",
                           session_id=identity["session_id"],
                           session_epoch=identity["session_epoch"])


def test_single_axis_homing_stops_on_unselected_axis_drift(rig):
    facade, state = rig
    state["homing_sim"] = True
    state["drift_per_frame"] = 15
    state["drift_joint"] = "joint2"
    identity = _start_single_axis_home(facade, state)
    status = _wait_home_terminal(facade)
    task = status["homing"]
    assert task["state"] == "fault", task
    assert "homing_unselected_drift:joint2" in str(task["last_error"]), task
    assert task["outputs_enabled"] is False
    facade.release_session("drift_done",
                           session_id=identity["session_id"],
                           session_epoch=identity["session_epoch"])


def test_single_axis_homing_detects_reverse_motion(rig):
    facade, state = rig
    state["homing_sim"] = True
    state["drift_per_frame"] = -30
    state["drift_joint"] = "joint1"
    identity = _start_single_axis_home(facade, state)
    status = _wait_home_terminal(facade)
    task = status["homing"]
    assert task["state"] == "fault", task
    assert "homing_reversed:joint1" in str(task["last_error"]), task
    facade.release_session("reverse_done",
                           session_id=identity["session_id"],
                           session_epoch=identity["session_epoch"])


def test_single_axis_homing_detects_oscillation(rig):
    facade, state = rig
    state["homing_sim"] = True
    # Each frame first moves 7 ticks toward the goal, then the drift cancels it:
    # scan-time positions alternate 531/527 around the start pose, so the axis
    # reverses direction without ever counting as progress past the start.
    state["drift_per_frame"] = [11, 3] * 30
    state["drift_joint"] = "joint1"
    identity = _start_single_axis_home(facade, state)
    status = _wait_home_terminal(facade)
    task = status["homing"]
    assert task["state"] == "fault", {
        "task": task, "transitions": state["home_transitions"][:8]}
    assert "homing_oscillating:joint1" in str(task["last_error"]), {
        "task": task, "transitions": state["home_transitions"][:8],
        "frame_targets": [f["commands"]["arm"]["target"] for f in state["frames"][-3:]]}
    facade.release_session("oscillation_done",
                           session_id=identity["session_id"],
                           session_epoch=identity["session_epoch"])
