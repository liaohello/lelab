"""Deploy-script contract: profile choice, exact pid, no blanket kills, preserved recordings.

Nothing here touches the Pi: --render builds the payload locally and prints the wrapper, and the
remote-facing modes are only read as text.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "tools" / "ros2_collection" / "deploy_readonly_agent.sh"
BLANKET_KILL = re.compile(r"(?m)^\s*(?:sudo\s+)?(?:pkill|killall)\b|[;&|]\s*(?:pkill|killall)\b")


def run(*args, env=None):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin", **(env or {})})


class DeployScriptContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text(encoding="utf-8")

    def test_shell_is_valid_and_never_kills_by_name(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode, 0)
        self.assertIsNone(BLANKET_KILL.search(self.text),
                          "only the pid this script recorded for its own agent may be signalled")

    def test_the_readonly_profile_is_the_default(self):
        rendered = run("--render").stdout
        self.assertIn("profile_path:=/home/ubuntu/lelab_car/config/hardware-readonly-v1.json", rendered)

    def test_the_teleop_profile_is_an_explicit_choice(self):
        rendered = run("--render", env={"PROFILE_NAME": "hardware-teleop-v1"}).stdout
        self.assertIn("config/hardware-teleop-v1.json", rendered)

    def test_an_unknown_or_path_shaped_profile_is_refused_before_any_ssh(self):
        for name in ("hardware-nonexistent-v1", "../ros2_ws/src"):
            out = run("--render", env={"PROFILE_NAME": name})
            self.assertEqual(out.returncode, 2, f"{name}: {out.stdout}{out.stderr}")
            self.assertIn("REFUSED", out.stderr)

    def test_the_recorded_pid_is_the_agent_itself(self):
        rendered = run("--render").stdout
        self.assertRegex(rendered, r"echo \$\$ > .*/readonly_agent\.pid")
        self.assertIn("exec python3 -m lelab_mobile_agent.agent", rendered,
                      "without exec the pid file would point at the wrapping shell")

    def test_stop_and_rollback_keep_the_recorded_sessions(self):
        """Both commands must be aimed at the recorded pid / the shipped code, never at recordings."""
        stop_cmd = next(line for line in self.text[self.text.index("stop() {"):].splitlines()
                        if line.strip().startswith("remote "))
        self.assertIn("readonly_agent.pid", stop_cmd)
        self.assertNotIn("sessions", stop_cmd)
        rollback_cmd = next(line for line in self.text[self.text.index("rollback() {"):].splitlines()
                            if "rm -rf" in line)
        self.assertNotIn("sessions", rollback_cmd, f"rollback must delete code only: {rollback_cmd}")
        self.assertIn("lelab_mobile_agent", rollback_cmd)

    def test_deploy_chowns_only_the_agent_payload(self):
        """Root-owned stack PID files under car_arm_stack must never be chowned recursively."""
        apply_block = self.text[self.text.index("apply() {"):self.text.index("stop() {")]
        chown_lines = [line for line in apply_block.splitlines() if "chown" in line]
        self.assertEqual(len(chown_lines), 1, chown_lines)
        line = chown_lines[0]
        self.assertIn("$REMOTE_DIR/lelab_mobile_agent", line)
        self.assertIn("$REMOTE_DIR/config", line)
        self.assertNotIn("chown -R '$REMOTE_UID:$REMOTE_GID' '$REMOTE_DIR'", line)


if __name__ == "__main__":
    unittest.main()
