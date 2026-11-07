"""T3 offline contracts: fixtures are not hardware or physical stop evidence."""
import copy
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "app_ros_mobile"), str(ROOT / "ros2_ws/src/lelab_mobile_agent")]
from lelab.hybrid_calibration import JOINTS, KINDS, ROLES, HybridCalibrationStore, validate_bundle
from lelab.hybrid_gamepad import GamepadIntent
from lelab.hybrid_session import HybridSession
from lelab.mobile_teleop import canonical_sha256
from lelab_mobile_agent.contract import ContractError, HybridCommandGate, hybrid_feedback_gate


class Clock:
    now = 1_000_000_000
    def __call__(self):
        return self.now


class Lease:
    def __init__(self, log, name):
        self.log, self.name, self.ok = log, name, True
    def acquire(self, request):
        self.log.append(self.name + ":acquire")
        if not self.ok:
            raise RuntimeError("busy")
        return self.name + "-token"
    def valid(self, token):
        return self.ok and token == self.name + "-token"
    def renew(self, token):
        self.log.append(self.name + ":renew")
        if not self.valid(token):
            raise RuntimeError("expired")
    def release(self, token, reason):
        self.log.append(self.name + ":release")
    def stop(self, reason):
        self.log.append(self.name + ":stop")


class Observer:
    def __init__(self):
        self.bundle_hash = "measured-bundle-hash"
        self.facts = {"leader_connected": True, "leader_healthy": True, "feedback_valid": True,
                      "exclusive_control": True, "alignment_valid": True,
                      "alignment_reference": {"leader_rad": [0.0] * 5, "follower_rad": [0.0] * 5}}
    def calibration_readiness(self):
        return {"ready": True, "bundle_hash": self.bundle_hash, "reasons": []}
    def snapshot(self):
        return self.facts


def make_session():
    log, clock, observer = [], Clock(), Observer()
    upper, car = Lease(log, "upper"), Lease(log, "car")
    return HybridSession(upper=upper, car=car, observer=observer, clock_ns=clock,
                         gamepad_deadzone=.1, gamepad_timeout_ns=300_000_000), upper, car, observer, clock, log


def sample(seq, time_ns, deadman, axes=None):
    return {"seq": seq, "received_mono_ns": time_ns, "deadman": deadman, "axes": axes or [0, 0, 0]}


def test_default_closed_and_status_json():
    h = HybridSession()
    assert h.status()["outputs_enabled"] is False
    assert not h.calibration_readiness()["ready"]
    with pytest.raises(RuntimeError, match="adapters_unavailable"):
        h.acquire_session({"owner": "operator"})
    json.dumps(h.status(), allow_nan=False)


def test_acquire_failure_rolls_back_and_cancel():
    h, _, car, _, _, log = make_session()
    car.ok = False
    with pytest.raises(RuntimeError, match="busy"):
        h.acquire_session({"owner": "operator"})
    assert log == ["upper:acquire", "car:acquire", "upper:release"]
    assert h.status()["session_id"] is None


def test_stop_precedes_release_and_no_resume_on_new_session():
    h, _, _, _, _, log = make_session()
    first = h.acquire_session({"owner": "operator"})
    for action in ("preview", "align", "arm", "engage"):
        h.set_arm_state(action)
    assert h.status()["channels"]["arm"]["state"] == "following"
    assert h.status()["outputs_enabled"] is False
    h.release_session("operator")
    assert log[-3:] == ["car:stop", "car:release", "upper:release"]
    second = h.acquire_session({"owner": "operator"})
    assert first["session_epoch"] != second["session_epoch"]
    assert second["channels"]["arm"]["state"] == "idle"


def test_dual_intent_grasp_lock_and_fault():
    h, _, _, observer, clock, log = make_session()
    h.acquire_session({"owner": "operator"})
    h.set_arm_state({"action": "set_phase", "phase": "observe"})
    for action in ("preview", "align", "arm", "engage"):
        h.set_arm_state(action)
    h.ingest_gamepad(sample(1, clock.now, False))
    clock.now += 1
    h.ingest_gamepad(sample(2, clock.now, True))
    h.set_arm_state("enable_base")
    clock.now += 1
    assert h.ingest_gamepad(sample(3, clock.now, True, [0.5, 0, 0]))["intent"] == [0.5, 0, 0]
    assert h.status()["channels"]["arm"]["state"] == "following"
    assert h.status()["channels"]["base"]["permitted"]
    h.set_arm_state({"action": "set_phase", "phase": "grasp"})
    assert not h.status()["channels"]["base"]["permitted"]
    observer.bundle_hash = "changed"
    assert h.status()["state"] == "fault"
    assert "car:stop" in log
    assert h.status()["channels"]["arm"]["state"] == "idle"


def test_gamepad_requires_neutral_new_press_and_timeout():
    g = GamepadIntent(.1, 100)
    assert not g.ingest(sample(1, 1, True), 1)["armed"]
    assert not g.ingest(sample(2, 2, False), 2)["armed"]
    assert g.ingest(sample(3, 3, True), 3)["armed"]
    assert not g.snapshot(103)["armed"]
    assert not g.ingest(sample(4, 104, True), 104)["armed"]
    with pytest.raises(ValueError):
        g.ingest(sample(5, 103, True), 105)


def command(channel, seq=0):
    target = {"base": {"vx_m_s": .1, "vy_m_s": 0, "wz_rad_s": 0},
              "arm": {"joint_names": JOINTS[:5], "positions_rad": [0.] * 5},
              "gripper": {"joint_name": "r_joint", "position_rad": 0.}}
    return {"protocol_version": 1, "session_id": "s", "session_epoch": 7, "channel": channel,
            "source": "gamepad" if channel == "base" else "so101_leader", "seq": seq, "sample_seq": 1,
            "ttl_ms": 100, "calibration_bundle_hash": "hash", "clutch": channel != "base", "target": target[channel]}


def test_protocol_independent_sequence_deadline_and_no_mutation():
    clock = Clock()
    gate = HybridCommandGate("s", 7, "hash", 200, clock)
    allowed = {"base", "arm", "gripper"}
    outgoing = gate.receive(command("arm"), allowed)
    outgoing["target"]["positions_rad"][0] = 99
    gate.receive(command("base"), allowed)
    gate.receive(command("gripper"), allowed)
    clock.now += 40_000_000
    original = gate.pop("arm", allowed)
    assert original["target"]["positions_rad"][0] == 0
    assert gate.forward(original)["ttl_ms"] == 60
    assert "deadline_mono_ns" not in gate.forward(original)
    clock.now += 60_000_000
    assert gate.pop("base", allowed) is None
    with pytest.raises(ContractError, match="expired"):
        gate.forward(original)
    gate.clear()
    assert gate.pop("gripper", allowed) is None


@pytest.mark.parametrize("field,value", [("session_epoch", 6), ("source", "maintenance"),
                                          ("calibration_bundle_hash", "old"), ("ttl_ms", 0),
                                          ("seq", True), ("deadline_mono_ns", 900)])
def test_bad_envelope_rejected_without_consuming_seq(field, value):
    gate = HybridCommandGate("s", 7, "hash", 200, Clock())
    bad = command("arm")
    bad[field] = value
    with pytest.raises(ContractError):
        gate.receive(bad, {"arm"})
    gate.receive(command("arm"), {"arm"})


def test_feedback_rejects_cache_stale_missing_and_mismatch():
    now = 1000
    samples = [{"name": name, "servo_id": ident, "source_kind": "servo_position_readback", "valid": True,
                "raw_position": 500, "position_rad": 0., "calibration_hash": "h", "sample_seq": 1,
                "received_mono_ns": now - i, "clock_domain": "monotonic"} for i, (name, ident) in enumerate(zip(JOINTS, [1, 2, 3, 4, 5, 10]))]
    assert hybrid_feedback_gate(samples, "h", now, 100)["valid"]
    for change in (lambda x: x.pop(), lambda x: x[0].update(source_kind="command_cache"),
                   lambda x: x[0].update(received_mono_ns=0), lambda x: x[0].update(position_rad=float("nan")),
                   lambda x: x[0].update(servo_id=10)):
        altered = copy.deepcopy(samples)
        change(altered)
        assert not hybrid_feedback_gate(altered, "h", now, 100)["valid"]


def test_example_and_bundle_mismatch_rejected():
    data = json.loads((ROOT / "docs/evidence/parallel-20261104/T3/calibration-schema-example.json").read_text())
    result = validate_bundle(data["bundle"], data["assets"])
    assert not result["ready"]
    assert any("unmeasured_example" in r for r in result["reasons"])


def measured_assets():
    assets = {kind: {"schema_version": 1, "kind": kind, "role": ROLES[kind],
                     "identity": kind + "-fixture", "measured": True} for kind in KINDS}
    assets["base"]["wheel_geometry"] = {"wheel_radius_m": .03, "track_width_m": .2, "wheelbase_m": .2}
    assets["follower"].update(joint_order=JOINTS, limits_rad={j: [-1., 1.] for j in JOINTS})
    leader_order = ["l1", "l2", "l3", "l4", "l5", "gripper"]
    assets["leader"].update(joint_order=leader_order, device_identity={"serial": "fixture", "usb_by_id": "/dev/serial/by-id/fixture"})
    assets["mapping"].update(joint_order=JOINTS, bound_identities={"leader": "leader-fixture", "follower": "follower-fixture"},
                             joints=[{"follower": j, "leader": l, "direction": 1, "scale": 1.} for j, l in zip(JOINTS, leader_order)],
                             gripper={"follower": "r_joint", "direction": 1, "scale": 1.})
    assets["mapping"]["joints"] = assets["mapping"]["joints"][:5]
    assets["gamepad"].update(deadman_button=0, axes={name: {"index": index, "min": -1, "center": 0, "max": 1, "deadzone": .1}
                                                    for index, name in enumerate(("vx", "vy", "wz"))})
    assets["camera"].update(intrinsics={"width": 640, "height": 400, "matrix": [100, 0, 320, 0, 100, 200, 0, 0, 1]},
                            extrinsics={"parent_frame": "link4", "child_frame": "camera", "translation_m": [0, 0, 0], "quaternion_xyzw": [0, 0, 0, 1]})
    for asset in assets.values():
        digest = canonical_sha256(asset)
        asset.update(content_sha256=digest, version=digest[:12])
    return assets


def make_bundle(assets):
    return {"schema_version": 1, "kind": "rig_bundle", "assets": {k: {"identity": v["identity"], "sha256": canonical_sha256(v)} for k, v in assets.items()}}


def test_valid_calibration_bundle_and_rejections():
    assets = measured_assets()
    bundle = make_bundle(assets)
    assert validate_bundle(bundle, assets)["ready"]
    for kind, change in (
        ("base", lambda a: a["wheel_geometry"].update(wheel_radius_m=float("nan"))),
        ("follower", lambda a: a["limits_rad"].pop("r_joint")),
        ("leader", lambda a: a.update(role="landerpi_follower")),
        ("mapping", lambda a: a["bound_identities"].update(leader="wrong")),
        ("gamepad", lambda a: a["axes"]["vx"].update(center=1)),
        ("camera", lambda a: a["extrinsics"].update(quaternion_xyzw=[0, 0, 0, 0])),
    ):
        altered = copy.deepcopy(assets)
        change(altered[kind])
        assert not validate_bundle(bundle, altered)["ready"]
    assert not validate_bundle(bundle, {**assets, "mapping": None})["ready"]


def test_estop_latches_across_release_and_requires_explicit_clear():
    h, _, _, _, _, _ = make_session()
    h.acquire_session({"owner": "operator"})
    h.set_arm_state("estop")
    h.release_session("operator")
    with pytest.raises(RuntimeError, match="estop_latched"):
        h.acquire_session({"owner": "operator"})
    assert h.set_arm_state("clear_estop")["state"] == "disconnected"
    assert h.acquire_session({"owner": "operator"})["channels"]["arm"]["state"] == "idle"


def test_observer_failure_rolls_back_in_order_and_retains_no_session():
    h, _, _, observer, _, log = make_session()
    observer.facts["feedback_valid"] = False
    with pytest.raises(RuntimeError, match="observed_prerequisites_missing"):
        h.acquire_session({"owner": "operator"})
    assert log == ["upper:acquire", "car:acquire", "car:stop", "car:release", "upper:release"]
    assert h.status()["session_id"] is None


def test_expired_lease_fault_and_status_reports_not_held():
    h, upper, _, _, _, log = make_session()
    h.acquire_session({"owner": "operator"})
    upper.ok = False
    status = h.status()
    assert status["state"] == "fault"
    assert status["leases"]["upper"]["held"] is False
    assert status["channels"]["arm"]["state"] == "idle"
    assert "car:stop" in log


def test_emergency_stop_does_not_wait_for_slow_status_observation():
    h, _, _, observer, _, _ = make_session()
    h.acquire_session({"owner": "operator"})
    entered = threading.Event()
    release = threading.Event()

    def slow_snapshot():
        entered.set()
        release.wait(2.0)
        return dict(observer.facts)

    observer.snapshot = slow_snapshot
    status_thread = threading.Thread(target=h.status)
    status_thread.start()
    assert entered.wait(1.0)
    started = time.monotonic()
    result = h.emergency_stop("operator")
    elapsed = time.monotonic() - started
    release.set()
    status_thread.join(timeout=2.0)

    assert not status_thread.is_alive()
    assert result["estop"] is True
    assert result["state"] == "estop"
    assert elapsed < 0.5


def test_one_transient_feedback_miss_is_retried_without_fault(monkeypatch):
    h, _, _, observer, _, log = make_session()
    h.acquire_session({"owner": "operator"})
    good = dict(observer.facts)
    transient = {**good, "feedback_valid": False}
    snapshots = iter([transient, good])
    observer.snapshot = lambda: next(snapshots)
    monkeypatch.setattr("lelab.hybrid_session.time.sleep", lambda _delay: None)

    status = h.status()

    assert status["state"] == "ready"
    assert status["last_error"] is None
    assert "car:stop" not in log


def test_persistent_feedback_loss_faults_and_preserves_primary_reason():
    h, _, car, observer, _, log = make_session()
    h.acquire_session({"owner": "operator"})
    observer.facts["feedback_valid"] = False

    def stop_failed(_reason):
        raise RuntimeError("car stopped accepting the token")

    car.stop = stop_failed
    status = h.status()

    assert status["state"] == "fault"
    assert status["last_error"].startswith(
        "feedback_valid_missing;stop_failed:RuntimeError")
    assert "car:stop" not in log


def test_gamepad_timeout_preserves_sequence_and_requires_fresh_press():
    g = GamepadIntent(.1, 100)
    g.ingest(sample(10, 1, False), 1)
    assert g.ingest(sample(11, 2, True), 2)["armed"]
    assert not g.snapshot(102)["armed"]
    with pytest.raises(ValueError):
        g.ingest(sample(10, 103, False), 103)
    assert not g.ingest(sample(12, 104, True), 104)["armed"]


def test_cancel_during_acquisition_rolls_back_both():
    h, _, car, observer, _, log = make_session()
    def cancel():
        raise KeyboardInterrupt()
    observer.snapshot = cancel
    with pytest.raises(KeyboardInterrupt):
        h.acquire_session({"owner": "operator"})
    assert log == ["upper:acquire", "car:acquire", "car:stop", "car:release", "upper:release"]
    assert h.status()["session_id"] is None


def test_heartbeat_failure_stops_and_latches_fault():
    h, upper, _, _, _, log = make_session()
    h.acquire_session({"owner": "operator"})
    h.heartbeat()
    upper.ok = False
    with pytest.raises(RuntimeError, match="heartbeat_failed"):
        h.heartbeat()
    assert h.status()["state"] == "fault"
    assert log[-1] == "car:stop"


def test_calibration_store_roundtrip_and_write_during_session_denied(tmp_path):
    store = HybridCalibrationStore(tmp_path)
    asset = measured_assets()["base"]
    record = store.save_validated("base", "base-fixture", asset)
    assert store.load("base", "base-fixture").sha256 == record.sha256
    h, _, _, _, _, _ = make_session()
    h.calibration_store = store
    h.acquire_session({"owner": "operator"})
    with pytest.raises(RuntimeError, match="release_session_before"):
        h.save_calibration("base", "base-fixture", asset)
    h.release_session("done")
    invalid = copy.deepcopy(asset)
    invalid["measured"] = False
    with pytest.raises(ValueError, match="unmeasured_example"):
        h.save_calibration("base", "base-fixture", invalid)
    assert store.load("base", "base-fixture").sha256 == record.sha256


def test_feedback_raw_out_of_bounds_is_rejected_without_modification():
    samples = [{"name": name, "servo_id": ident, "source_kind": "servo_position_readback", "valid": True,
                "raw_position": 500, "position_rad": 0., "calibration_hash": "h", "sample_seq": 1,
                "received_mono_ns": 100, "clock_domain": "monotonic"} for name, ident in zip(JOINTS, [1, 2, 3, 4, 5, 10])]
    samples[0]["raw_position"] = 1001
    result = hybrid_feedback_gate(samples, "h", 110, 100)
    assert not result["valid"] and "joint1:raw_out_of_range" in result["reasons"]
    assert samples[0]["raw_position"] == 1001


def test_gap_is_latest_wins_but_a_replay_retires_and_clears_all_channels():
    gate = HybridCommandGate("s", 7, "hash", 200, Clock())
    gate.receive(command("arm"), {"arm", "base"})
    gate.receive(command("base"), {"arm", "base"})
    gate.receive(command("arm", 2), {"arm", "base"})
    with pytest.raises(ContractError, match="stale_seq"):
        gate.receive(command("arm", 2), {"arm", "base"})
    assert gate.pop("base", {"base"}) is None
    with pytest.raises(ContractError, match="session_retired"):
        gate.receive(command("arm", 1), {"arm"})


def test_t1_supervisor_rejects_output_with_shared_envelope():
    from lelab_mobile_agent.hardware_safety import SafetySupervisor

    clock = Clock()
    gate = HybridCommandGate("s", 7, "hash", 200, clock)
    class Sink:
        def submit(self, _command):
            raise AssertionError("output occurred")
        def stop(self, _reason):
            raise AssertionError("stop sink called despite no output")
    limits = {name: (-1., 1.) for name in ["vx_m_s", "vy_m_s", "wz_rad_s", *JOINTS]}
    supervisor = SafetySupervisor(Sink(), lambda _channel: True, calibration_hash="hash", limits=limits,
                                  outputs_enabled=False, clock_ns=clock)
    supervisor.arm_session("s", 7)
    for channel in ("base", "arm", "gripper"):
        gate.receive(command(channel), {channel})
        result = supervisor.submit(gate.pop(channel, {channel}))
        assert result == {"accepted": False, "reason": "outputs_disabled"}


def test_pending_overrun_replaces_the_older_target_without_retiring():
    gate = HybridCommandGate("s", 7, "hash", 200, Clock())
    gate.receive(command("arm"), {"arm"})
    replacement = command("arm", 2)
    replacement["target"]["positions_rad"] = [0.1] * 5
    gate.receive(replacement, {"arm"})
    assert gate.pop("arm", {"arm"})["target"]["positions_rad"] == [0.1] * 5
    assert gate.retired is False


@pytest.mark.parametrize("field,value", [("protocol_version", True), ("session_epoch", True), ("channel", [])])
def test_invalid_protocol_types_are_rejected(field, value):
    gate = HybridCommandGate("s", 7, "hash", 200, Clock())
    bad = command("arm")
    bad[field] = value
    with pytest.raises(ContractError):
        gate.receive(bad, {"arm"})


def test_t1_feedback_snapshot_passes_only_fresh_trusted_gate():
    from lelab_mobile_agent.follower_feedback import FeedbackStore, JointSample

    ids = dict(zip(JOINTS, [1, 2, 3, 4, 5, 10]))
    store = FeedbackStore(JOINTS, calibration_hash="h", servo_ids=ids, stale_after_s=.5)
    for name, servo_id in ids.items():
        assert store.ingest_sample(JointSample(name, 0., 1., "servo_position_readback", servo_id=servo_id,
                                               raw_position=500., sample_seq=1, valid=True, calibration_hash="h"))
    snapshot = store.snapshot(now=1.1)
    assert snapshot["available"]
    samples = list(snapshot["joint_samples"].values())
    assert hybrid_feedback_gate(samples, "h", 1_100_000_000, 500_000_000)["valid"]
    assert not hybrid_feedback_gate(samples, "h", 1_500_000_000, 500_000_000)["valid"]
    samples[0]["source_kind"] = "command_cache"
    assert not hybrid_feedback_gate(samples, "h", 1_100_000_000, 500_000_000)["valid"]


def test_gamepad_has_no_implicit_calibration_or_timeout():
    h, upper, car, observer, clock, _ = make_session()
    h = HybridSession(upper=upper, car=car, observer=observer, clock_ns=clock)
    h.acquire_session({"owner": "operator"})
    assert h.status()["channels"]["base"]["gamepad"]["reason"] == "gamepad_unconfigured"
    with pytest.raises(RuntimeError, match="gamepad_unconfigured"):
        h.ingest_gamepad(sample(1, clock.now, False))


def test_acquisition_binds_server_generated_same_identity_to_both_leases():
    h, upper, car, _, _, _ = make_session()
    seen = []
    original_upper, original_car = upper.acquire, car.acquire
    def capture_upper(request):
        seen.append(dict(request))
        return original_upper(request)
    def capture_car(request):
        seen.append(dict(request))
        return original_car(request)
    upper.acquire, car.acquire = capture_upper, capture_car
    result = h.acquire_session({"owner": "operator", "session_id": "forged", "session_epoch": 7})
    assert seen[0] == seen[1]
    assert seen[0]["session_id"] == result["session_id"] != "forged"
    assert seen[0]["session_epoch"] == result["session_epoch"]
    assert 0 <= result["session_epoch"] < 2 ** 52
    assert seen[0]["calibration_bundle_hash"] == "measured-bundle-hash"


def test_heartbeat_and_operator_presence_timing_is_on_the_record():
    h, upper, _, _, _, _ = make_session()
    h.acquire_session({"owner": "operator"})
    h.heartbeat()
    h.heartbeat()

    diagnostics = h.status()["diagnostics"]
    assert diagnostics["heartbeats"] == 2
    assert diagnostics["heartbeat_failures"] == 0
    assert diagnostics["last_heartbeat_outcome"] == "ok"
    assert diagnostics["operator_presence_age_s"] is not None
    assert diagnostics["observation_cache"] is None, "an observer that cannot say must not be guessed"


def test_a_failed_heartbeat_is_counted_and_labelled():
    h, upper, _, _, _, _ = make_session()
    h.acquire_session({"owner": "operator"})
    upper.ok = False
    with pytest.raises(RuntimeError, match="heartbeat_failed"):
        h.heartbeat()

    status = h.status()
    assert status["diagnostics"]["heartbeat_failures"] == 1
    assert status["diagnostics"]["last_heartbeat_outcome"] == "failed"
    kinds = [entry["kind"] for entry in status["diagnostics"]["state_changes"]]
    assert "state_change" in kinds and "stop_failed" not in kinds


def test_a_refused_heartbeat_still_marks_operator_presence():
    h, _, _, _, _, _ = make_session()
    with pytest.raises(RuntimeError, match="session_not_ready"):
        h.heartbeat()
    assert h.status()["diagnostics"]["last_heartbeat_outcome"] == "refused_not_ready"


def test_stop_failure_keeps_the_cause_that_decides_lease_or_transport():
    """A type name alone cannot separate a refused token from a timed-out request.

    That ambiguity is why a lost lease could not be pinned down after the fact.
    """
    h, _, car, observer, _, _ = make_session()
    h.acquire_session({"owner": "operator"})
    observer.facts["feedback_valid"] = False

    def stop_failed(_reason):
        raise RuntimeError("car_http_403: control_token: missing or invalid control token")

    car.stop = stop_failed
    status = h.status()

    assert "stop_failed:RuntimeError:car_http_403" in status["last_error"]
    notes = [entry for entry in status["diagnostics"]["state_changes"]
             if entry["kind"] == "stop_failed"]
    assert len(notes) == 1
    assert "car_http_403" in notes[-1]["detail"]


def test_state_changes_record_the_reason_that_produced_them():
    h, _, _, observer, _, _ = make_session()
    h.acquire_session({"owner": "operator"})
    observer.facts["exclusive_control"] = False

    status = h.status()
    entry = status["diagnostics"]["state_changes"][-1]
    assert entry["previous"] == "ready"
    assert entry["state"] == "fault"
    assert "exclusive_control_missing" in entry["reason"]


def test_an_adapter_without_a_lease_report_still_shares_only_what_it_knows():
    h, _, car, _, _, _ = make_session()
    h.acquire_session({"owner": "operator"})
    status = h.status()
    assert status["leases"]["car"] == {"held": True}
    car.ok = False
    status = h.status()
    assert status["leases"]["car"]["held"] is False
    assert "lease_lost:car=not_held" in status["last_error"]
