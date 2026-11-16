"""Behaviour and static checks for the versioned vendor R0 patch (0006).

The baseline vendor tree is read-only; every check copies it into a temporary
directory and applies 0001-0005 plus 0006 there.  The SDK behaviour tests use a
fake serial port: no /dev/rrc, no device, no real sleep beyond the configured
bounded timeouts.
"""
from __future__ import annotations

import ast
import hashlib
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

BASELINE = Path("/home/liaozhengyan/Desktop/resource/lander_pi/src/driver/"
                "ros_robot_controller/ros_robot_controller")
PATCH_DIR = Path(__file__).resolve().parents[1] / "vendor_patches" / "ros"
PATCHES = ("0001-bounded-servo-read.patch", "0002-ros-uint16-state-guard.patch",
           "0003-measured-readback-topic.patch",
           "0004-torque-semantics-and-readback-contract.patch",
           "0005-readback-owner-instance.patch")
PATCHED_NODE = "09fd46a724fade105cb79e0967d54fbcdd97a917055fdddc0f7c7ab24bdfc91d"
PATCHED_SDK = "97a19b2bc336ae743bb333e2b39318785b2b7ddb02182ffc6217cca9d6ccb4c3"

pytestmark = pytest.mark.skipif(not BASELINE.is_dir(), reason="vendor reference tree absent")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def patched(tmp_path: Path) -> Path:
    target = tmp_path / "ros_robot_controller"
    target.mkdir()
    for name in ("ros_robot_controller_node.py", "ros_robot_controller_sdk.py"):
        shutil.copy2(BASELINE / name, target / name)
    for name in PATCHES:
        result = subprocess.run(
            ["patch", "--batch", "--forward", "-p1", "-i", str(PATCH_DIR / name)],
            cwd=target, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    guard = PATCH_DIR / "patch_guard_deadline.py"
    assert subprocess.run([sys.executable, str(guard), str(target), "check"],
                          capture_output=True).returncode == 0
    assert subprocess.run([sys.executable, str(guard), str(target), "apply"],
                          capture_output=True).returncode == 0
    assert digest(target / "ros_robot_controller_node.py") == PATCHED_NODE
    assert digest(target / "ros_robot_controller_sdk.py") == PATCHED_SDK
    return target


class FakePort:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.reply: bytes | None = None
        self.on_write = None

    def write(self, payload: bytes) -> int:
        self.writes.append(payload)
        if self.on_write is not None:
            self.on_write(payload)
        return len(payload)


def make_board(sdk, *, timeout: float = 0.05):
    board = sdk.Board.__new__(sdk.Board)
    board.servo_read_timeout = timeout
    board.bus_reply_lock = threading.Lock()
    board.bus_reply_active = False
    board.bus_reply_fault = False
    board.bus_reply_reports = 0
    board.port_write_lock = threading.Lock()
    board.servo_read_lock = threading.RLock()
    board.bus_servo_queue = queue.Queue(maxsize=1)
    board._read_stats = threading.local()
    board.port = FakePort()
    return board


def load_sdk(patched: Path):
    fake = types.ModuleType("serial")

    class SerialException(Exception):
        pass

    class Serial:
        def __init__(self, *args, **kwargs):
            raise AssertionError("the test never opens a serial port")

    fake.SerialException = SerialException
    fake.Serial = Serial
    saved = sys.modules.get("serial")
    sys.modules["serial"] = fake
    try:
        spec = importlib.util.spec_from_file_location(
            "vendor_patched_sdk_under_test", patched / "ros_robot_controller_sdk.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if saved is None:
            sys.modules.pop("serial", None)
        else:
            sys.modules["serial"] = saved


def test_a_position_reply_keeps_its_lock_and_duration_diagnostics(patched):
    sdk = load_sdk(patched)
    board = make_board(sdk)
    board.port.on_write = lambda _payload: board.bus_servo_queue.put_nowait(
        struct.pack("<BBbh", 1, 0x05, 0, 511))
    assert board.bus_servo_read_position(1) == [511]
    stats = board.last_read_stats
    assert stats["reason"] is None
    assert stats["servo_id"] == 1
    assert stats["command"] == "0x5"
    assert stats["lock_wait_ms"] >= 0.0
    assert stats["duration_ms"] >= 0.0


def test_a_missing_reply_is_bounded_and_named(patched):
    sdk = load_sdk(patched)
    board = make_board(sdk, timeout=0.05)
    started = time.monotonic()
    assert board.bus_servo_read_position(2) is None
    elapsed = time.monotonic() - started
    assert 0.04 <= elapsed < 0.3
    assert board.last_read_stats["reason"] == "read_timeout"
    assert board.last_read_stats["duration_ms"] >= 40.0


def test_a_reply_already_in_the_queue_latches_the_read(patched):
    sdk = load_sdk(patched)
    board = make_board(sdk)
    board.bus_servo_queue.put_nowait(struct.pack("<BBbh", 5, 0x05, 0, 400))
    assert board.bus_servo_read_position(5) is None
    assert board.last_read_stats["reason"] == "reply_latch_busy"
    assert board.bus_reply_fault is True


def test_a_write_from_another_thread_waits_boundedly_for_the_bus(patched):
    sdk = load_sdk(patched)
    board = make_board(sdk, timeout=0.03)
    board.servo_read_lock.acquire()
    errors: list[BaseException] = []

    def write():
        try:
            board.buf_write(sdk.PacketFunction.PACKET_FUNC_BUS_SERVO, [0x05, 1])
        except BaseException as error:  # noqa: BLE001 - the assertion inspects it
            errors.append(error)

    worker = threading.Thread(target=write)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=1.0)
    elapsed = time.monotonic() - started
    board.servo_read_lock.release()
    assert not worker.is_alive()
    assert errors and isinstance(errors[0], IOError)
    assert "serial bus busy" in str(errors[0])
    assert elapsed < 0.3


def test_a_write_issued_inside_a_read_is_reentrant(patched):
    sdk = load_sdk(patched)
    board = make_board(sdk)
    board.port.on_write = lambda _payload: board.bus_servo_queue.put_nowait(
        struct.pack("<BBbh", 3, 0x05, 0, 500))
    with board.servo_read_lock:
        board.buf_write(sdk.PacketFunction.PACKET_FUNC_BUS_SERVO, [0x05, 3])
    assert board.port.writes


def test_the_scan_loop_uses_a_deadline_grid_and_a_budget(patched):
    tree = ast.parse((patched / "ros_robot_controller_node.py").read_text())
    loop = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "servo_readback_loop")
    calls = {node.func.id for node in ast.walk(loop)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert {"DeadlineSchedule", "TransactionBudget"} <= calls
    iterated = {node.target.id for node in ast.walk(loop)
                if isinstance(node, ast.For) and isinstance(node.target, ast.Name)}
    assert "servo_id" in iterated
    assert "record_scan_start" in {node.func.attr for node in ast.walk(loop)
                                   if isinstance(node, ast.Call)
                                   and isinstance(node.func, ast.Attribute)}
    waits = [node for node in ast.walk(loop)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "wait" and node.args
             and isinstance(node.args[0], ast.Constant)
             and node.args[0].value in (0.1, 0.2, 0.5)]
    assert not waits, "the old fixed per-loop sleep must be gone"


def test_recovery_sleeps_before_taking_the_read_lock(patched):
    tree = ast.parse((patched / "ros_robot_controller_node.py").read_text())
    recovery = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "recover_bus_servo_reads")
    sleep_line = min(node.lineno for node in ast.walk(recovery)
                     if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute)
                     and node.func.attr == "sleep")
    acquire_lines = [node.lineno for node in ast.walk(recovery)
                     if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute)
                     and node.func.attr == "acquire"
                     and "servo_read_lock" in ast.unparse(node.func)]
    assert acquire_lines
    assert sleep_line < min(acquire_lines), \
        "the quiet window must not block position/stop writes"
    source = ast.get_source_segment((patched / "ros_robot_controller_node.py").read_text(),
                                    recovery) or ""
    assert "validate_quiet(quiet_s)" in source
    assert "bus_reply_reports" in source
