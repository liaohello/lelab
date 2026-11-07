"""Contract tests for car_servo_torque.py — the guard must never let a write slip through.

Nothing here touches hardware: every case stops before the SDK is imported. What is being
pinned is the part that matters on a real arm:

  * torque changes require an explicit --i-authorize-servo-writes,
  * --unload and --load cannot be combined,
  * a second process holding the control-board UART blocks the tool outright (the vendor SDK's
    blocking read has no timeout, so two writers on one bus is how a node dies),
  * the only bus commands in the source are 0x0B / 0x0C / 0x0D, and no blanket kill exists.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "tools" / "ros2_collection" / "car_servo_torque.py"
BLANKET_KILL = re.compile(r"(?m)^\s*(?:sudo\s+)?(?:pkill|killall)\b|[;&|]\s*(?:pkill|killall)\b")


def run(*args, timeout=30):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          timeout=timeout, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"})


class TorqueToolContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text(encoding="utf-8")

    def test_render_needs_no_ids_and_no_hardware(self):
        out = run("--render")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("car_servo_torque.py", out.stdout)

    def test_unload_without_authorization_is_refused(self):
        out = run("--ids", "1", "--unload")
        self.assertEqual(out.returncode, 2)
        self.assertIn("REFUSED", out.stderr)
        self.assertIn("--i-authorize-servo-writes", out.stderr)

    def test_load_without_authorization_is_refused(self):
        out = run("--ids", "1", "--load")
        self.assertEqual(out.returncode, 2)
        self.assertIn("REFUSED", out.stderr)

    def test_unload_and_load_together_are_refused(self):
        out = run("--ids", "1", "--unload", "--load", "--i-authorize-servo-writes")
        self.assertEqual(out.returncode, 2)
        self.assertIn("mutually exclusive", out.stderr)

    def test_missing_ids_is_refused(self):
        out = run("--unload", "--i-authorize-servo-writes")
        self.assertEqual(out.returncode, 2)
        self.assertIn("--ids is required", out.stderr)

    def test_a_second_holder_of_the_uart_blocks_the_tool(self):
        """Stand-in for the vendor node: any other process holding the device is enough."""
        handle, path = tempfile.mkstemp(prefix="fake_rrc_")
        os.close(handle)
        holder = subprocess.Popen(
            [sys.executable, "-c", "import sys,time;f=open(sys.argv[1]);time.sleep(20)", path])
        try:
            time.sleep(0.4)  # let the child open the descriptor
            out = run("--device", path, "--ids", "1")
        finally:
            holder.kill()
            holder.wait(timeout=5)
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        self.assertIn("REFUSED", out.stderr)
        self.assertIn(str(holder.pid), out.stderr)

    def test_only_torque_write_commands_appear_in_the_source(self):
        for command in ("CMD_UNLOAD = 0x0B", "CMD_LOAD = 0x0C", "CMD_TORQUE_STATE = 0x0D"):
            self.assertIn(command, self.text)
        # 0x30 (set angle limit) is deliberately absent: widening limits to "make it move"
        # is exactly the shortcut this tool must not offer.
        self.assertNotIn("set_angle_limit", self.text)
        self.assertNotIn("bus_servo_save", self.text)

    def test_no_blanket_kill(self):
        self.assertIsNone(BLANKET_KILL.search(self.text))


if __name__ == "__main__":
    unittest.main()
