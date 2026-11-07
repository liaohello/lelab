"""T1 Wave A independent acceptance: patch provenance, bounded serial faults and gaps.

Complements (does not replace) test_t1_hardware.py from the parallel T1 session.
All checks are offline: no SSH, no serial port, no camera, no actuator, no service.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import json
import queue
import shutil
import struct
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ros2_ws" / "src" / "lelab_mobile_agent"))

from lelab_mobile_agent import hardware_adapter as ha  # noqa: E402
from lelab_mobile_agent.follower_feedback import FeedbackStore, JointSample, SOURCES  # noqa: E402
from lelab_mobile_agent.hardware_safety import SafetySupervisor  # noqa: E402

REF = Path("/home/liaozhengyan/Desktop/resource/lander_pi/src/driver/ros_robot_controller/ros_robot_controller")
ROS_PATCH_DIR = ROOT / "tools" / "ros2_collection" / "vendor_patches" / "ros"
GUARD = ROS_PATCH_DIR / "patch_guard.py"
CONFIG = ROS_PATCH_DIR / "readback_config.example.json"
FILES = ("ros_robot_controller_sdk.py", "ros_robot_controller_node.py")
NAMES = list(ha.SERVO_READBACK_JOINTS)
IDS = dict(ha.SERVO_READBACK_IDS)


def guard_module():
    spec = importlib.util.spec_from_file_location("t1_patch_guard_acceptance", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stage(tmp_path):
    for name in FILES:
        shutil.copyfile(REF / name, tmp_path / name)


def run_guard(tmp_path, action):
    return subprocess.run([sys.executable, str(GUARD), str(tmp_path), action],
                          capture_output=True, text=True)


def patched_sdk(tmp_path, monkeypatch):
    stage(tmp_path)
    assert run_guard(tmp_path, "apply").returncode == 0
    fake_serial = types.ModuleType("serial")
    fake_serial.SerialException = OSError
    monkeypatch.setitem(sys.modules, "serial", fake_serial)
    spec = importlib.util.spec_from_file_location("t1_patched_sdk_acceptance", tmp_path / FILES[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def board(module, timeout=0.02):
    b = module.Board.__new__(module.Board)
    b.servo_read_lock = threading.Lock()
    b.bus_reply_lock = threading.Lock()
    b.port_write_lock = threading.Lock()
    b.bus_reply_active = False
    b.bus_reply_fault = False
    b.servo_read_timeout = timeout
    b.bus_servo_queue = queue.Queue(maxsize=1)
    return b


# --- provenance / patch lifecycle -------------------------------------------

def test_reference_tree_is_unmodified_and_matches_pinned_baseline():
    g = guard_module()
    for name in FILES:
        path = REF / name
        assert path.is_file() and not path.is_symlink()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == g.BASE[name]
    assert not list(REF.glob("*.orig")) and not list(REF.glob("*.rej"))
    assert not list(REF.glob("*.patch"))


def test_guard_apply_and_reverse_roundtrip(tmp_path):
    stage(tmp_path)
    assert run_guard(tmp_path, "check").returncode == 0
    assert run_guard(tmp_path, "apply").returncode == 0
    g = guard_module()
    for name in FILES:
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == g.PATCHED[name]
    assert run_guard(tmp_path, "check-reverse").returncode == 0
    assert run_guard(tmp_path, "reverse").returncode == 0
    for name in FILES:
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == g.BASE[name]


def test_guard_refuses_wrong_baseline_without_modifying_file(tmp_path):
    stage(tmp_path)
    path = tmp_path / FILES[0]
    path.write_bytes(path.read_bytes() + b"\n# drifted baseline\n")
    drifted = path.read_bytes()
    result = run_guard(tmp_path, "apply")
    assert result.returncode == 2
    assert "wrong baseline" in result.stderr
    assert path.read_bytes() == drifted


def test_guard_refuses_symlinked_target(tmp_path):
    stage(tmp_path)
    path = tmp_path / FILES[0]
    real = tmp_path / "baseline_copy.py"
    shutil.move(str(path), str(real))
    path.symlink_to(real)
    assert run_guard(tmp_path, "check").returncode == 2


def test_patched_node_rejects_non_uint16_states_before_ros_assignment(tmp_path):
    stage(tmp_path)
    assert run_guard(tmp_path, "apply").returncode == 0
    tree = ast.parse((tmp_path / FILES[1]).read_text(encoding="utf-8"))
    helpers = {
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    helper = next(node for node in helpers if node.name == "_set_uint16_state")
    position_helper = next(node for node in helpers
                           if node.name == "_set_position_state")
    namespace = {}
    exec(compile(ast.Module(body=[helper, position_helper], type_ignores=[]),
                 str(tmp_path / FILES[1]), "exec"),
         namespace)
    setter = namespace["_set_uint16_state"]
    position_setter = namespace["_set_position_state"]

    class Data:
        position = ()

    data = Data()
    assert setter(data, "position", (229,)) is True
    assert data.position == (229,)
    data.position = ()
    assert setter(data, "position", (-1,)) is False
    assert data.position == ()
    assert setter(data, "position", (True,)) is False
    assert setter(data, "position", None) is False
    assert position_setter(data, (-49,)) is True
    assert data.position == [0xFFCF]
    assert position_setter(data, (500,)) is True
    assert data.position == [500]


# --- simulated serial faults on the patched single-owner SDK -----------------

def test_patched_sdk_simulated_serial_faults(tmp_path, monkeypatch):
    module = patched_sdk(tmp_path, monkeypatch)

    good = board(module)
    good.buf_write = lambda _, data: good.packet_report_serial_servo(
        struct.pack("<BBbh", data[1], data[0], 0, 512))
    assert good.bus_servo_read_position(10) == [512]
    assert not good.bus_reply_fault

    timed_out = board(module)
    timed_out.buf_write = lambda *_: None
    assert timed_out.bus_servo_read_position(10) is None
    assert timed_out.bus_reply_fault

    # A late identical reply must never be mistaken for a later identical request.
    timed_out.packet_report_serial_servo(struct.pack("<BBbh", 10, 5, 0, 777))
    assert timed_out.bus_servo_queue.empty()
    assert timed_out.bus_reply_fault
    assert timed_out.bus_servo_read_position(10) is None

    short = board(module)
    short.buf_write = lambda *_: short.packet_report_serial_servo(b"abc")
    assert short.bus_servo_read_position(10) is None and short.bus_reply_fault

    wrong_id = board(module)
    wrong_id.buf_write = lambda _, data: wrong_id.packet_report_serial_servo(
        struct.pack("<BBbh", 9, data[0], 0, 500))
    assert wrong_id.bus_servo_read_position(10) is None and wrong_id.bus_reply_fault

    error_status = board(module)
    error_status.buf_write = lambda _, data: error_status.packet_report_serial_servo(
        struct.pack("<BBbh", data[1], data[0], -1, 500))
    assert error_status.bus_servo_read_position(10) is None and error_status.bus_reply_fault

    duplicate = board(module)

    def double(_, data):
        frame = struct.pack("<BBbh", data[1], data[0], 0, 500)
        duplicate.packet_report_serial_servo(frame)
        duplicate.packet_report_serial_servo(frame)

    duplicate.buf_write = double
    assert duplicate.bus_servo_read_position(10) is None and duplicate.bus_reply_fault

    broadcast = board(module)
    broadcast.buf_write = lambda *_: None
    assert broadcast.bus_servo_read_id(254) is None


# --- six-joint readback throughput configuration -----------------------------

def test_readback_config_never_claims_cache_rate_is_readback():
    cfg = json.loads(CONFIG.read_text())
    rb = cfg["readback"]
    assert rb["joints"] == NAMES
    assert rb["servo_ids"] == IDS and rb["servo_ids"]["r_joint"] == 10
    assert rb["scan_is_sequential"] is True
    assert rb["max_readback_hz"] is None and rb["scan_period_s"] is None
    assert rb["measured_on_hardware"] is False
    assert cfg["vendor_cache"]["is_readback_capability"] is False
    assert cfg["vendor_cache"]["source_kind"] == "command_cache"
    assert cfg["vendor_cache"]["publish_hz"] == 50
    assert ha.READBACK_SCAN_DEFAULTS["max_readback_hz"] is None
    assert ha.READBACK_SCAN_DEFAULTS["vendor_cache_is_readback"] is False
    assert ha.READBACK_SCAN_DEFAULTS["vendor_cache_publish_hz"] == cfg["vendor_cache"]["publish_hz"]


# --- real chassis topic / unit boundary --------------------------------------

@pytest.mark.parametrize("profile,calibrated,machine,reason", [
    ("hardware_readonly_v1", True, "mecanum", "profile_not_hybrid_manual"),
    ("hardware_teleop_v1", True, "mecanum", "profile_not_hybrid_manual"),
    ("hardware_hybrid_manual_v1", False, "mecanum", "base_calibration_unverified"),
    ("hardware_hybrid_manual_v1", True, None, "machine_type_unknown"),
])
def test_base_route_fails_closed_for_legacy_or_unverified(profile, calibrated, machine, reason):
    result = ha.base_route_boundary(profile, calibration_verified=calibrated, machine_type=machine)
    assert result["outputs_enabled"] is False
    assert result["reason"] == reason


def test_base_route_units_and_single_kinematics_owner():
    result = ha.base_route_boundary("hardware_hybrid_manual_v1",
                                    calibration_verified=True, machine_type="mecanum")
    assert result["outputs_enabled"] is True
    assert result["topic"] == ha.REAL_BASE_TOPIC == "/controller/cmd_vel"
    assert result["simulation_only_topic"] != result["topic"]
    assert result["wheel_rps_conversion_in_adapter"] is False
    assert result["kinematics_owner"] == "vendor_ros/controller/mecanum"
    assert result["units"] == {"vx_m_s": "m/s", "vy_m_s": "m/s", "wz_rad_s": "rad/s"}


@pytest.mark.parametrize("target", [
    {"vx_m_s": float("nan"), "vy_m_s": 0, "wz_rad_s": 0},
    {"vx_m_s": 0, "vy_m_s": float("inf"), "wz_rad_s": 0},
    {"vx_m_s": True, "vy_m_s": 0, "wz_rad_s": 0},
    {"vx_m_s": 0, "vy_m_s": 0},
    {"vx_m_s": 0, "vy_m_s": 0, "wz_rad_s": 0, "extra": 1},
    {"vx_m_s": "0", "vy_m_s": 0, "wz_rad_s": 0},
])
def test_invalid_si_twist_is_rejected_before_routing(target):
    assert ha.validate_si_twist(target)["valid"] is False


def test_valid_si_twist_carries_units():
    result = ha.validate_si_twist({"vx_m_s": 0.1, "vy_m_s": 0.2, "wz_rad_s": 0.3})
    assert result["valid"] is True and result["units"]["wz_rad_s"] == "rad/s"
    assert ha.base_twist_fields({"vx_m_s": 0.1, "vy_m_s": 0.2, "wz_rad_s": 0.3}) == {
        "linear": {"x": 0.1, "y": 0.2}, "angular": {"z": 0.3}}


# --- public surface, default no output, confirmation levels ------------------

class Sink:
    def __init__(self):
        self.sent, self.stops = [], []

    def submit(self, command):
        self.sent.append(command)

    def stop(self, reason):
        self.stops.append(reason)


def command(seq=0):
    return dict(protocol_version=1, session_id="s", session_epoch=1, channel="gripper",
                source="so101_leader", seq=seq, sample_seq=seq, ttl_ms=100,
                received_mono_ns=1_000_000_000, deadline_mono_ns=1_100_000_000,
                calibration_bundle_hash="cal-v1", clutch=True,
                target={"joint_name": "r_joint", "position_rad": 0.1})


def adapter(enabled=False, feedback_ok=True):
    sink = Sink()
    limits = {name: (-1, 1) for name in NAMES}
    sup = SafetySupervisor(sink, lambda channel: feedback_ok, calibration_hash="cal-v1",
                           limits=limits, outputs_enabled=enabled,
                           clock_ns=lambda: 1_000_000_001)
    sup.arm_session("s", 1)
    feedback = FeedbackStore(NAMES, calibration_hash="cal-v1", servo_ids=IDS)
    return ha.HardwareAdapter(sup, feedback), sup, sink


def test_public_surface_signatures_and_default_no_output():
    a, _, sink = adapter()
    for method, params in (("capabilities", ["self"]), ("read_snapshot", ["self"]),
                           ("submit", ["self", "command"]), ("stop", ["self", "reason"])):
        assert list(inspect.signature(getattr(type(a), method)).parameters) == params
    caps = a.capabilities()
    assert caps["outputs_enabled"] is False
    assert caps["hardware_stop_effect"] == "unverified"
    assert caps["controller_acknowledged"] == "unavailable"
    assert caps["gripper_joint_name"] == "r_joint" and caps["gripper_servo_id"] == 10
    assert caps["base_topic"] == "/controller/cmd_vel"
    assert a.submit(command())["accepted"] is False
    assert sink.sent == []
    assert a.stop("release")["hardware_effect"] == "unverified"
    snap = a.read_snapshot()
    assert snap["available"] is False and snap["reason"] == "no_feedback_yet"
    assert snap["expected_joints"] == NAMES


def test_confirmation_levels_never_claim_hardware_execution():
    a, _, sink = adapter(enabled=True)
    result = a.submit(command())
    assert result["accepted"] is True
    assert result["level"] == "gateway_accepted"
    assert result["controller_acknowledged"] == "unavailable"
    assert result["execution_observed"] == "unavailable"
    assert "driver_sent" not in result
    assert len(sink.sent) == 1


def test_missing_feedback_stops_without_fabricated_zero_hold():
    a, sup, sink = adapter(enabled=True)
    assert a.submit(command())["accepted"] is True
    sup.feedback_ok = lambda channel: False
    assert sup.tick()["stop_requested"] is True
    assert len(sink.sent) == 1
    assert sink.sent[0]["target"]["position_rad"] == 0.1
    assert sink.stops == ["feedback_unavailable"]
    assert sup.stop("release")["hardware_effect"] == "unverified"


# --- provenance, raw bounds, single physical gripper -------------------------

def sample(name, **kwargs):
    values = dict(name=name, position_rad=0.0, wall=100.0, source="servo_position_readback",
                  servo_id=IDS[name], raw_position=500, sample_seq=1, valid=True,
                  calibration_hash="cal-v1")
    values.update(kwargs)
    return JointSample(**values)


def test_source_kinds_are_exactly_four_and_cache_is_never_measured():
    assert SOURCES == frozenset({"servo_position_readback", "command_cache",
                                 "model_placeholder", "synthetic"})
    for untrusted in ("command_cache", "model_placeholder"):
        s = FeedbackStore(NAMES, calibration_hash="cal-v1", servo_ids=IDS)
        for name in NAMES:
            s.ingest_sample(sample(name))
        s.ingest_sample(sample("r_joint", wall=100.1, sample_seq=2, source=untrusted))
        assert s.snapshot(100.2)["available"] is False


def test_out_of_range_raw_is_retained_not_clamped():
    s = FeedbackStore(NAMES, calibration_hash="cal-v1", servo_ids=IDS)
    for name in NAMES:
        s.ingest_sample(sample(name))
    s.ingest_sample(sample("r_joint", wall=100.1, sample_seq=2, raw_position=1500))
    snap = s.snapshot(100.2)
    assert snap["joint_samples"]["r_joint"]["raw_position"] == 1500
    assert snap["joint_samples"]["r_joint"]["error"] == "raw_out_of_range"
    assert snap["available"] is False


def test_synthetic_is_allowed_only_in_explicit_rehearsal():
    strict = FeedbackStore(NAMES, calibration_hash="cal-v1", servo_ids=IDS)
    for name in NAMES:
        strict.ingest_sample(sample(name, source="synthetic"))
    assert strict.snapshot(100.1)["available"] is False
    rehearsal = FeedbackStore(NAMES, calibration_hash="cal-v1", servo_ids=IDS,
                              allow_synthetic=True)
    for name in NAMES:
        rehearsal.ingest_sample(sample(name, source="synthetic"))
    assert rehearsal.snapshot(100.1)["available"] is True


def test_five_mimic_joints_are_never_actuators():
    for name in ("gripper_base_joint", "gripper_mimic_1", "mimic", "l_joint", "joint6"):
        with pytest.raises(ValueError):
            ha.gripper_servo_target({"joint_name": name, "position_rad": 0}, lambda x: x)
    assert len(NAMES) == 6 and "r_joint" in NAMES
    assert not any("mimic" in n for n in NAMES)
    assert ha.gripper_servo_target({"joint_name": "r_joint", "position_rad": 0.1},
                                   lambda x: 500) == (10, 500)
