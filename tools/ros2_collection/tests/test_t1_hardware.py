"""T1 offline contracts: no ROS, serial device, or actuator required."""
import importlib.util
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ros2_ws/src/lelab_mobile_agent"))
from lelab_mobile_agent.follower_feedback import FeedbackStore, JointSample  # noqa: E402
from lelab_mobile_agent.hardware_adapter import HardwareAdapter, base_twist_fields, gripper_servo_target  # noqa: E402
from lelab_mobile_agent.hardware_io import ProtocolFault, ServoTransactions  # noqa: E402
from lelab_mobile_agent.ros_servo_readback import (  # noqa: E402
    ReadbackProtocolError, parse_servo_states,
)
from lelab_mobile_agent.hardware_safety import SafetySupervisor  # noqa: E402

NAMES = [f"joint{i}" for i in range(1, 6)] + ["r_joint"]
IDS = dict(zip(NAMES, [1, 2, 3, 4, 5, 10]))


def store():
    return FeedbackStore(NAMES, calibration_hash="cal-v1", servo_ids=IDS, max_scan_span_s=0.3)


def sample(name, wall=100.0, **kwargs):
    values = dict(name=name, position_rad=0.0, wall=wall, source="servo_position_readback",
                  servo_id=IDS[name], raw_position=500, sample_seq=1, valid=True, calibration_hash="cal-v1")
    values.update(kwargs)
    return JointSample(**values)


def full(s):
    for name in NAMES:
        s.ingest_sample(sample(name))


def test_trusted_six_and_scan_age_provenance():
    s = store()
    full(s)
    snap = s.snapshot(100.2)
    assert snap["available"] and len(snap["joint_samples"]) == 6
    assert snap["joint_samples"]["r_joint"]["servo_id"] == 10
    assert snap["joint_samples"]["r_joint"]["source_kind"] == "servo_position_readback"
    assert not s.snapshot(100.6)["available"]
    assert not s.snapshot(100.6)["joint_samples"]["r_joint"]["valid"]
    assert not s.snapshot(99.9)["available"]


def test_ros_readback_parser_requires_exact_ids_and_positions():
    states = [types.SimpleNamespace(present_id=[i], position=[500 + i])
              for i in [1, 2, 3, 4, 5, 10]]
    assert parse_servo_states(states) == {
        "joint1": 501, "joint2": 502, "joint3": 503,
        "joint4": 504, "joint5": 505, "r_joint": 510,
    }
    signed = [types.SimpleNamespace(present_id=list(s.present_id),
                                    position=list(s.position)) for s in states]
    signed[2].position[0] = 0xFFCF
    assert parse_servo_states(signed)["joint3"] == -49
    for change in (
        lambda rows: rows.pop(),
        lambda rows: rows[0].present_id.__setitem__(0, 9),
        lambda rows: rows[-1].position.__setitem__(0, 1001),
    ):
        altered = [types.SimpleNamespace(present_id=list(s.present_id),
                                         position=list(s.position)) for s in states]
        change(altered)
        with pytest.raises(ReadbackProtocolError):
            parse_servo_states(altered)


@pytest.mark.parametrize("kwargs", [
    {"position_rad": float("nan")}, {"raw_position": float("inf")}, {"raw_position": -1001},
    {"servo_id": 9}, {"calibration_hash": "old"}, {"source": "command_cache"},
    {"source": "model_placeholder"}, {"source": "synthetic"}, {"sample_seq": None},
])
def test_invalid_or_untrusted_readings_fail_closed(kwargs):
    s = store()
    full(s)
    params = dict(kwargs)
    params.setdefault("sample_seq", 2)
    s.ingest_sample(sample("r_joint", wall=100.1, **params))
    assert not s.snapshot(100.2)["available"]
    if kwargs.get("raw_position") == -1001:
        assert s.snapshot(100.2)["joint_samples"]["r_joint"]["raw_position"] == -1001
        assert s.snapshot(100.2)["joint_samples"]["r_joint"]["error"] == "raw_out_of_range"


def test_missing_scan_span_old_calibration_and_generic_topic():
    s = store()
    s.ingest_joint_states(NAMES, [0.0] * 6, 100, "/controller_manager/joint_states")
    assert not s.gate(100.1)[0]
    s.clear()
    for i, name in enumerate(NAMES):
        s.ingest_sample(sample(name, wall=100 + i * .07))
    assert s.snapshot(100.36)["reason"] == "scan_span_exceeded"
    s.clear()
    s.ingest_sample(sample("joint1"))
    assert s.snapshot(100.1)["missing_joints"] == NAMES[1:]


class Sink:
    def __init__(self):
        self.sent, self.stops = [], []
    def submit(self, command):
        self.sent.append(command)
    def stop(self, reason):
        self.stops.append(reason)


def command(channel="gripper", seq=0):
    target = {"joint_name": "r_joint", "position_rad": .1}
    if channel == "base":
        target = {"vx_m_s": .1, "vy_m_s": .2, "wz_rad_s": .3}
    return dict(protocol_version=1, session_id="s", session_epoch=1, channel=channel,
                source="gamepad" if channel == "base" else "so101_leader", seq=seq, sample_seq=seq,
                ttl_ms=100, received_mono_ns=1_000_000_000, deadline_mono_ns=1_100_000_000,
                calibration_bundle_hash="cal-v1", clutch=True, target=target)


def supervisor(enabled=False):
    sink = Sink()
    now = [1_000_000_001]
    feedback = [True]
    limits = {name: (-1, 1) for name in NAMES + ["vx_m_s", "vy_m_s", "wz_rad_s"]}
    sup = SafetySupervisor(sink, lambda channel: feedback[0], calibration_hash="cal-v1", limits=limits,
                           outputs_enabled=enabled, clock_ns=lambda: now[0])
    sup.arm_session("s", 1)
    return sup, sink, now, feedback


def test_default_disabled_and_one_physical_gripper():
    sup, sink, _, _ = supervisor()
    adapter = HardwareAdapter(sup, store())
    assert not adapter.submit(command())["accepted"] and not sink.sent
    assert gripper_servo_target(command()["target"], lambda x: 500) == (10, 500)
    with pytest.raises(ValueError):
        gripper_servo_target(command()["target"], lambda x: 1001)
    with pytest.raises(ValueError):
        gripper_servo_target({"joint_name": "l_joint", "position_rad": 0}, lambda x: 0)
    assert base_twist_fields(command("base")["target"])["linear"]["y"] == .2
    assert adapter.capabilities()["base_topic"] == "/controller/cmd_vel"


def test_envelope_expiry_stale_sequence_feedback_loss_and_session_retirement():
    sup, sink, now, feedback = supervisor(True)
    c = command()
    assert sup.submit(c)["accepted"]
    c["target"]["position_rad"] = .9
    assert sink.sent[0]["target"]["position_rad"] == .1
    # A latest-wins source may skip seq 1 when seq 0 was already consumed.
    assert sup.submit(command(seq=2))["accepted"]
    # Replaying an older sequence is unsafe and retires the active session.
    assert not sup.submit(command(seq=2))["accepted"]
    assert sink.stops == ["stale_seq"]
    assert sup.submit(command(seq=1))["reason"] == "session_mismatch"
    with pytest.raises(ValueError):
        sup.arm_session("s", 1)
    sup.arm_session("s", 2)
    c = command(); c["session_epoch"] = 2
    assert sup.submit(c)["accepted"]
    feedback[0] = False
    assert sup.tick()["stop_requested"]
    feedback[0] = True
    assert sup.submit(c)["reason"] == "session_mismatch"
    sup.arm_session("s", 3)
    c["session_epoch"] = 3
    assert sup.submit(c)["accepted"]
    now[0] = 1_100_000_000
    assert sup.tick()["stop_requested"]
    assert sup.submit(c)["reason"] == "session_mismatch"
    sup.arm_session("s", 4)
    assert sup.submit(command(seq=3))["reason"] == "session_mismatch"


@pytest.mark.parametrize("mutate", [
    {"source": "gamepad"}, {"session_epoch": 0}, {"calibration_bundle_hash": "old"},
    {"clutch": False}, {"deadline_mono_ns": 1_200_000_000}, {"ttl_ms": 0},
    {"target": {"joint_name": "mimic", "position_rad": 0}},
    {"target": {"joint_name": "r_joint", "position_rad": float("nan")}},
])
def test_bad_envelopes_never_reach_sink(mutate):
    sup, sink, _, _ = supervisor(True)
    c = command()
    c.update(mutate)
    assert not sup.submit(c)["accepted"]
    assert not sink.sent


def test_transactions_success_and_fault_latches():
    txn = None
    def send(cmd, sid):
        txn.receive(struct.pack("<BBbh", sid, cmd, 0, 543))
    txn = ServoTransactions(send, timeout_s=.03)
    assert txn.read(10, 5, "<BBbh") == (543,)
    txn.receive(struct.pack("<BBbh", 10, 5, 0, 123))
    with pytest.raises(ProtocolFault, match="latched"):
        txn.read(10, 5, "<BBbh")


@pytest.mark.parametrize("frame,reason", [
    (b"short", "wrong_id_or_subcommand"), (b"x", "wrong_length"),
    (struct.pack("<BBbh", 9, 5, 0, 500), "wrong_id_or_subcommand"),
    (struct.pack("<BBbh", 10, 6, 0, 500), "wrong_id_or_subcommand"),
    (struct.pack("<BBbh", 10, 5, -1, 500), "controller_error"),
])
def test_transaction_bad_frames_fail_closed(frame, reason):
    txn = None
    def send(cmd, sid):
        txn.receive(frame)
    txn = ServoTransactions(send, timeout_s=.03)
    with pytest.raises(ProtocolFault, match=reason):
        txn.read(10, 5, "<BBbh")
    assert txn.poisoned


def test_transaction_timeout_cancel_concurrency_and_late_reply():
    txn = ServoTransactions(lambda *_: None, timeout_s=.03)
    start = time.monotonic()
    with pytest.raises(ProtocolFault, match="timeout"):
        txn.read(10, 5, "<BBbh")
    assert time.monotonic() - start < .3
    txn.receive(struct.pack("<BBbh", 10, 5, 0, 500))
    with pytest.raises(ProtocolFault, match="latched"):
        txn.read(10, 5, "<BBbh")
    cancel = threading.Event(); cancel.set()
    txn2 = ServoTransactions(lambda *_: None, timeout_s=.03)
    with pytest.raises(ProtocolFault, match="cancelled"):
        txn2.read(10, 5, "<BBbh", cancel=cancel)
    assert txn2.poisoned
    entered = threading.Event()
    txn3 = ServoTransactions(lambda *_: entered.set(), timeout_s=.03)
    def first():
        with pytest.raises(ProtocolFault):
            txn3.read(1, 5, "<BBbh")
    t = threading.Thread(target=first); t.start(); assert entered.wait(.2)
    with pytest.raises(ProtocolFault):
        txn3.read(2, 5, "<BBbh")
    t.join(.3); assert not t.is_alive()


def test_patch_guard_apply_reverse_and_wrong_baseline(tmp_path):
    source = Path("/home/liaozhengyan/Desktop/resource/lander_pi/src/driver/ros_robot_controller/ros_robot_controller")
    guard = ROOT / "tools/ros2_collection/vendor_patches/ros/patch_guard.py"
    for name in ("ros_robot_controller_sdk.py", "ros_robot_controller_node.py"):
        shutil.copyfile(source / name, tmp_path / name)
    def run(action):
        return subprocess.run([sys.executable, str(guard), str(tmp_path), action], capture_output=True, text=True)
    assert run("check").returncode == 0
    assert run("apply").returncode == 0
    assert run("check").returncode == 2
    assert run("check-reverse").returncode == 0
    assert run("reverse").returncode == 0
    (tmp_path / "ros_robot_controller_sdk.py").write_text("different baseline")
    result = run("apply")
    assert result.returncode == 2 and "wrong baseline" in result.stderr
    assert (tmp_path / "ros_robot_controller_sdk.py").read_text() == "different baseline"


def test_patched_sdk_simulated_responses(tmp_path, monkeypatch):
    source = Path("/home/liaozhengyan/Desktop/resource/lander_pi/src/driver/ros_robot_controller/ros_robot_controller")
    guard = ROOT / "tools/ros2_collection/vendor_patches/ros/patch_guard.py"
    followup = (ROOT / "tools/ros2_collection/vendor_patches/ros/"
                "0004-torque-semantics-and-readback-contract.patch")
    for name in ("ros_robot_controller_sdk.py", "ros_robot_controller_node.py"):
        shutil.copyfile(source / name, tmp_path / name)
    subprocess.run([sys.executable, str(guard), str(tmp_path), "apply"], check=True, capture_output=True)
    subprocess.run(["patch", "--batch", "--forward", "-p1", "-i", str(followup)],
                   cwd=tmp_path, check=True, capture_output=True)
    serial = types.ModuleType("serial"); serial.SerialException = OSError
    monkeypatch.setitem(sys.modules, "serial", serial)
    spec = importlib.util.spec_from_file_location("t1_vendor_sdk", tmp_path / "ros_robot_controller_sdk.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    def board():
        b = module.Board.__new__(module.Board)
        b.servo_read_lock = threading.Lock(); b.bus_reply_lock = threading.Lock()
        b.bus_reply_active = False; b.bus_reply_fault = False; b.servo_read_timeout = .02
        b.bus_servo_queue = queue.Queue(maxsize=1)
        return b
    b = board()
    b.buf_write = lambda _, data: b.packet_report_serial_servo(struct.pack("<BBbh", data[1], data[0], 0, 500))
    assert b.bus_servo_read_position(10) == [500]
    b.buf_write = lambda *_: None
    assert b.bus_servo_read_position(10) is None and b.bus_reply_fault
    b.packet_report_serial_servo(struct.pack("<BBbh", 10, 5, 0, 600))
    assert b.bus_servo_read_position(10) is None
    b2 = board()
    b2.buf_write = lambda *_: b2.packet_report_serial_servo(struct.pack("<BBbh", 9, 5, 0, 500))
    assert b2.bus_servo_read_position(10) is None and b2.bus_reply_fault
    writes = []
    b3 = board()
    b3.buf_write = lambda function, payload: writes.append((function, bytes(payload)))
    b3.bus_servo_enable_torque(1, True)
    b3.bus_servo_enable_torque(1, False)
    assert writes == [(5, b"\x0c\x01"), (5, b"\x0b\x01")]
    node_source = (tmp_path / "ros_robot_controller_node.py").read_text()
    assert "bus_servo_read_vin(" in node_source
    assert "bus_servo_read_torque_state(" in node_source
    assert "bus_servo_read_voltage(" not in node_source
    assert "bus_servo_read_torque(" not in node_source


def test_rejects_invalid_session_types_and_untrusted_feedback():
    sup, sink, _, feedback = supervisor(True)
    c = command(); c["session_epoch"] = True
    assert sup.submit(c)["reason"] == "session_mismatch"
    c = command(); c["channel"] = []
    assert not sup.submit(c)["accepted"]
    feedback[0] = "truthy but not valid"
    assert sup.submit(command())["reason"] == "feedback_unavailable"
    assert not sink.sent


def _worker_process(child, parent, events):
    from lelab_mobile_agent.hardware_safety import run_supervisor
    parent.close()
    class ProcessSink:
        def submit(self, envelope):
            events.put(("submit", envelope["session_epoch"], envelope["seq"], envelope["deadline_mono_ns"]))
        def stop(self, reason):
            events.put(("stop", reason))
    limits = {n: (-1, 1) for n in NAMES + ["vx_m_s", "vy_m_s", "wz_rad_s"]}
    sup = SafetySupervisor(ProcessSink(), lambda _: True, calibration_hash="cal-v1", limits=limits,
                           outputs_enabled=True)
    run_supervisor(child, sup, tick_interval_s=.01)


def test_supervisor_in_separate_process_checks_deadline_and_ipc_loss():
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    events = ctx.Queue()
    process = ctx.Process(target=_worker_process, args=(child, parent, events))
    process.start(); child.close()
    try:
        parent.send({"op": "arm_session", "session_id": "s", "session_epoch": 1})
        assert parent.poll(2) and parent.recv()["accepted"]
        now = time.monotonic_ns()
        c = command(); c.update(received_mono_ns=now, deadline_mono_ns=now + 100_000_000)
        parent.send({"op": "submit", "command": c})
        assert parent.poll(2) and parent.recv()["accepted"]
        sent = events.get(timeout=2)
        assert sent == ("submit", 1, 0, c["deadline_mono_ns"])
        stopped = events.get(timeout=2)
        assert stopped == ("stop", "expired_or_invalid_deadline")
        parent.close()
        process.join(2)
        assert not process.is_alive() and process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate(); process.join(2)
        events.close()


def test_supervisor_stops_on_ipc_eof_while_active():
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    events = ctx.Queue()
    process = ctx.Process(target=_worker_process, args=(child, parent, events))
    process.start(); child.close()
    try:
        parent.send({"op": "arm_session", "session_id": "s", "session_epoch": 1})
        assert parent.poll(2) and parent.recv()["accepted"]
        now = time.monotonic_ns()
        c = command(); c.update(ttl_ms=1000, received_mono_ns=now, deadline_mono_ns=now + 1_000_000_000)
        parent.send({"op": "submit", "command": c})
        assert parent.poll(2) and parent.recv()["accepted"]
        assert events.get(timeout=2)[0] == "submit"
        parent.close()
        assert events.get(timeout=2) == ("stop", "ipc_closed")
        process.join(2)
        assert not process.is_alive() and process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate(); process.join(2)
        events.close()


def test_synthetic_requires_opt_in_and_snapshot_is_strict_json():
    import json
    s = store()
    s.allow_synthetic = True
    for name in NAMES:
        s.ingest_sample(sample(name, source="synthetic"))
    assert s.gate(100.1)[0]
    s.ingest_sample(sample("joint1", wall=100.2, sample_seq=2, raw_position=float("inf")))
    result = s.snapshot(100.21)
    assert not result["available"]
    assert result["joint_samples"]["joint1"]["raw_position"] is None
    json.dumps(result, allow_nan=False)


def test_feedback_rejects_old_samples_and_copies_input():
    s = store()
    original = sample("joint1")
    assert s.ingest_sample(original)
    original.position_rad = 99
    assert s.snapshot(100.1)["positions_rad"]["joint1"] == 0
    assert not s.ingest_sample(sample("joint1", wall=99, sample_seq=2))
    assert not s.ingest_sample(sample("joint1", wall=101, sample_seq=1))


@pytest.mark.parametrize("ids", [
    {"joint1": 1, "joint2": 1}, {"r_joint": 9}, {"joint1": True}, {"unknown": 6},
])
def test_servo_mapping_rejects_duplicates_wrong_gripper_and_invalid_id(ids):
    with pytest.raises(ValueError, match="servo id mapping"):
        FeedbackStore(NAMES, calibration_hash="cal-v1", servo_ids=ids)
