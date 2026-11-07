"""Safety contract for the car-side minimal startup entry (static + dryrun, never touches hardware)."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "tools" / "ros2_collection" / "start_car_arm_stack.sh"

# Command-position use only: the script is allowed to say in prose that it never does this.
BLANKET_KILL = re.compile(r"(?m)^\s*(?:sudo\s+)?(?:pkill|killall)\b|[;&|]\s*(?:pkill|killall)\b")


def audit_source() -> str:
    """The Python the script pipes into the container for its UART/co-control audit."""
    text = SCRIPT.read_text(encoding="utf-8")
    marker = "BUS_AUDIT_PY='\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n'\n", start)
    return text[start:end]


def run(mode, extra=(), env=None):
    full = {"LELAB_STACK_DRYRUN": "1", "PATH": "/usr/bin:/bin"}
    full.update(env or {})
    return subprocess.run(["bash", str(SCRIPT), mode, *extra], capture_output=True, text=True,
                          env=full, timeout=30)


class StartupEntryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text(encoding="utf-8")

    def test_script_is_shell_valid(self):
        checked = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_no_blanket_process_killing(self):
        self.assertIsNone(BLANKET_KILL.search(self.text),
                          "only the process groups this script recorded may be signalled")

    def test_apply_refuses_without_board_write_authorization(self):
        out = run("--apply")
        self.assertEqual(out.returncode, 78, out.stdout + out.stderr)
        self.assertIn("REFUSED", out.stdout)
        self.assertNotIn("setsid", out.stdout, "no node may be launched without the flag")

    def test_authorized_apply_is_still_dryrun_in_this_suite(self):
        out = run("--apply", ["--i-authorize-board-writes"])
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("[dryrun]", out.stdout)
        self.assertIn("wrappers built locally", out.stdout)

    def test_it_never_launches_the_full_vendor_bringup(self):
        self.assertNotIn("ros2 launch", self.text,
                         "the minimal entry starts nodes directly; vendor launch files also run init_pose/odom/ekf")
        for forbidden in ("init_pose", "odom_publisher", "ekf_node"):
            # allowed only as read-only audit needles and in the rejection comments
            self.assertLessEqual(self.text.count(forbidden), 3,
                                 f"{forbidden} must not become part of the started stack")

    def test_wrappers_record_their_own_pid_and_gate_on_preconditions(self):
        rendered = run("--render").stdout
        self.assertIn("echo $$ > '/home/ubuntu/lelab_car/car_arm_stack/ros_robot_controller.pid'", rendered)
        self.assertIn("echo $$ > '/home/ubuntu/lelab_car/car_arm_stack/controller_manager.pid'", rendered)
        self.assertIn("exec ros2 run ros_robot_controller ros_robot_controller", rendered)
        self.assertIn('exec ros2 run servo_controller servo_controller --ros-args --params-file "$SERVO_PARAMS"',
                      rendered)
        self.assertIn("missing SERVO_PARAMS", rendered)
        self.assertIn("MACHINE_TYPE is unset", rendered)
        self.assertIn("set +u", rendered, "the ROS setup scripts need unset variables to be tolerated")

    def test_start_order_waits_for_init_finish_with_a_finite_timeout(self):
        out = run("--apply", ["--i-authorize-board-writes"]).stdout
        rrc_start = out.index("nohup setsid '/home/ubuntu/lelab_car/car_arm_stack/ros_robot_controller.sh'")
        gate = out.index("/ros_robot_controller/init_finish")
        cm_start = out.index("nohup setsid '/home/ubuntu/lelab_car/car_arm_stack/controller_manager.sh'")
        self.assertLess(rrc_start, gate, "the board node must be up before the gate is queried")
        self.assertLess(gate, cm_start,
                        "controller_manager blocks forever in wait_for_service() without the gate")
        self.assertIn("wait_for_service(timeout_sec=", out)

    def test_stop_only_signals_recorded_session_ids(self):
        out = run("--stop").stdout
        self.assertIn("pgid=", out)
        self.assertIn("is not one of our nodes", out, "a reused pid must be skipped, never killed")
        self.assertIn("cat /proc/$pid/task/$pid/children", out)

    def test_status_reports_measured_feedback_and_honours_timeouts(self):
        out = run("--status").stdout
        for needle in ("timeout_sec=", "GetBusServoState", "/controller_manager/joint_states",
                       "feedback_is_measured", "no real follower feedback"):
            self.assertIn(needle, out)

    def test_check_is_read_only_and_audits_uart_ownership(self):
        out = run("--check").stdout
        self.assertIn("uart_device=", out)
        self.assertIn("co_control_risk=", out)
        self.assertIn("params.env", out)
        self.assertNotIn("nohup setsid", out, "--check must not start anything")


class BusAuditTests(unittest.TestCase):
    """The UART/co-control audit has to actually answer, on data shaped like the car's /proc.

    It runs the shipped Python against a fake /proc tree: the earlier version raised
    TypeError on the first process it looked at, and the fixed one then reported its own
    command line as a running vendor node. Both are invisible to a test that only greps
    for labels in the script text.
    """

    def _proc(self, root: Path, pid: str, cmd: bytes, ppid: str) -> Path:
        directory = root / pid
        directory.mkdir(parents=True)
        (directory / "cmdline").write_bytes(cmd)
        (directory / "status").write_text(f"Name:\tx\nPPid:\t{ppid}\n")
        return directory

    def _run(self, root: Path, device: str, self_pid: str = "7000") -> str:
        env = {**os.environ, "PROC_ROOT": str(root), "AUDIT_SELF_PID": self_pid}
        done = subprocess.run([sys.executable, "-", device], input=audit_source(),
                              capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout

    def test_it_ignores_its_own_wrapper_but_names_a_holding_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, base = Path(tmp) / "proc", Path(tmp) / "dev"
            base.mkdir()
            device = base / "rrc"
            device.write_bytes(b"")

            # The audit process (7000) and its parent: the docker-exec wrapper whose command
            # line contains the whole heredoc, i.e. every needle.
            self._proc(root, "7000", b"python3\0-\0/dev/rrc\0", "6000")
            self._proc(root, "6000",
                       b"bash\0-lc\0for f in /opt/ros; source ros_robot_controller controller_manager "
                       b"init_pose odom_publisher; python3 - /dev/rrc\0", "1")
            node = self._proc(root, "1234",
                              b"python3\0/home/ubuntu/ros2_ws/install/ros_robot_controller/lib/"
                              b"python3.10/site-packages/ros_robot_controller/ros_robot_controller_node.py\0",
                              "1")
            (node / "fd").mkdir()
            os.symlink(str(device), str(node / "fd" / "3"))

            out = self._run(root, str(device))

        self.assertIn("present=True", out)
        self.assertIn("'1234'", out.split("holders=")[1].split("vendor_nodes_running")[0])
        self.assertIn("1234", out.split("vendor_nodes_running=")[1])
        self.assertNotIn("6000", out.split("vendor_nodes_running=")[1].split("co_control_risk")[0])

    def test_a_third_party_controller_is_reported_as_co_control_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proc"
            device = Path(tmp) / "rrc"
            device.write_bytes(b"")
            self._proc(root, "7000", b"python3\0-\0/dev/rrc\0", "1")
            self._proc(root, "2345",
                       b"python3\0/opt/ros/humble/bin/ros2\0run\0joystick\0joystick_node\0", "1")

            out = self._run(root, str(device))

        self.assertIn("co_control_risk=True", out)
        self.assertIn("2345", out)

    def test_no_vendor_nodes_and_no_holder_is_a_clean_bus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proc"
            device = Path(tmp) / "rrc"
            device.write_bytes(b"")
            self._proc(root, "7000", b"python3\0-\0/dev/rrc\0", "1")
            self._proc(root, "9999", b"/usr/bin/dbus-daemon\0--system\0", "1")

            out = self._run(root, str(device))

        self.assertIn("holders=none", out)
        self.assertIn("vendor_nodes_running=none", out)
        self.assertIn("co_control_risk=False", out)


if __name__ == "__main__":
    unittest.main()
