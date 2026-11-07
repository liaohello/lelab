"""Offline contract tests for the read-only car servo probe (no car, no hardware)."""
from __future__ import annotations

import queue
import struct
import sys
import threading
import types
import unittest

import car_servo_readout as probe

# The vendor SDK's own write commands. If any of these ever shows up in the read table, the
# tool would be writing to servos while claiming to identify them.
WRITE_COMMANDS = {0x01, 0x03, 0x11, 0x21, 0x23, 0x31, 0x35, 0x39}


class FakeBoard:
    """Stands in for the vendor Board: records what was sent and feeds the reply queue."""

    def __init__(self, frames=()):
        self.servo_read_lock = threading.Lock()
        self.bus_servo_queue = queue.Queue(maxsize=8)
        self.sent = []
        self._pending = list(frames)
        self.lock_held_during_send = None

    def buf_write(self, func, data):
        self.sent.append((func, list(data)))
        self.lock_held_during_send = self.servo_read_lock.locked()
        for frame in self._pending:
            self.bus_servo_queue.put(bytes(frame))
        self._pending = []


def sdk_stub() -> None:
    module = types.ModuleType("ros_robot_controller")
    sdk = types.ModuleType("ros_robot_controller.ros_robot_controller_sdk")

    class PacketFunction(int):
        PACKET_FUNC_BUS_SERVO = 5

    sdk.PacketFunction = PacketFunction
    module.ros_robot_controller_sdk = sdk
    sys.modules.setdefault("ros_robot_controller", module)
    sys.modules["ros_robot_controller.ros_robot_controller_sdk"] = sdk


class ReadOnlyContractTests(unittest.TestCase):
    def setUp(self):
        sdk_stub()

    def test_the_command_table_contains_no_writes(self):
        self.assertFalse(WRITE_COMMANDS & {cmd for cmd, _ in probe.READ_ONLY_COMMANDS.values()},
                         "every byte in READ_ONLY_COMMANDS must be a board read command")

    def test_a_zero_status_is_a_successful_read(self):
        # The vendor helper returns the payload only when the status byte is 0.
        frame = struct.pack("<BBbB", 3, 0x12, 0, 3)
        board = FakeBoard([frame])

        result = probe.read_servo(board, 3, "id", timeout_s=0.5, retries=0)

        self.assertEqual(result["value"], [3])
        self.assertEqual(result["attempts"], [])
        self.assertTrue(board.lock_held_during_send, "reads must be serialized")

    def test_a_nonzero_status_is_a_servo_error_and_not_a_value(self):
        frame = struct.pack("<BBbB", 6, 0x12, -1, 101)
        board = FakeBoard([frame])

        result = probe.read_servo(board, 6, "id", timeout_s=0.5, retries=0)

        self.assertIsNone(result["value"])
        self.assertEqual(result["attempts"], ["servo_error(-1)"])

    def test_the_echo_of_the_request_is_skipped_and_the_real_reply_is_still_used(self):
        # The board answers twice into one single-slot queue: first the echo of what we sent
        # (2 bytes), then the servo's reply. Draining before the next send would discard it.
        echo = struct.pack("<BB", 0x12, 4)
        reply = struct.pack("<BBbh", 4, 0x05, 0, 512)
        board = FakeBoard([echo, reply])

        result = probe.read_servo(board, 4, "position", timeout_s=0.5, retries=0)

        self.assertEqual(result["value"], [512])
        self.assertEqual(len(result["attempts"]), 1)
        self.assertTrue(result["attempts"][0].startswith("echo_or_short(2B"), result["attempts"])
        self.assertEqual(len(board.sent), 1, "one request must be enough once echoes are skipped")

    def test_a_reply_for_another_servo_is_not_accepted(self):
        board = FakeBoard([struct.pack("<BBbB", 5, 0x12, 0, 5),
                           struct.pack("<BBbB", 2, 0x12, 0, 2)])

        result = probe.read_servo(board, 2, "id", timeout_s=0.2, retries=0)

        self.assertEqual(result["value"], [2])
        self.assertEqual(result["attempts"], ["stale_reply(id=5,cmd=0x12)"])

    def test_a_dead_bus_returns_within_the_deadline(self):
        board = FakeBoard([])

        result = probe.read_servo(board, 1, "position", timeout_s=0.2, retries=1)

        self.assertIsNone(result["value"])
        self.assertIn("timeout", result["attempts"])


class GuardTests(unittest.TestCase):
    def setUp(self):
        sdk_stub()

    def test_unknown_field_names_are_rejected(self):
        with self.assertRaises(KeyError):
            probe.READ_ONLY_COMMANDS["set_position"]

    def test_a_held_port_refuses_before_any_import_or_send(self):
        calls = []
        original = probe.port_holders
        probe.port_holders = lambda device="": [["4242", "python3 ros_robot_controller"]]
        try:
            argv = sys.argv
            sys.argv = ["car_servo_readout.py"]
            self.assertEqual(probe.main(), 3)
        finally:
            probe.port_holders = original
            sys.argv = argv
        self.assertEqual(calls, [])

    def test_render_does_not_touch_the_car(self):
        argv = sys.argv
        sys.argv = ["car_servo_readout.py", "--render"]
        try:
            self.assertEqual(probe.main(), 0)
        finally:
            sys.argv = argv


if __name__ == "__main__":
    unittest.main()
