"""The §5.2 pre-flight must be read-only by construction and must name what it could not see.

The on-site session is short and the car is shared, so the value of this script is that a single
pass answers "are the prerequisites met" without touching the board. These tests pin the two things
that make that claim true: the probe list contains no write, and every unavailable input becomes a
named verdict instead of a silent pass.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "tools" / "ros2_collection" / "teleop_preflight.py"
sys.path.insert(0, str(SCRIPT.parent))

import teleop_preflight as tp  # noqa: E402

AGENT_STATUS = {
    "profile": "hardware_teleop_v1", "read_only": False,
    "control": {"teleop": {"enabled": True, "state_topic": "/lelab/recording/teleop",
                           "chassis_locked": True, "hold_policy": "position hold, never torque-off",
                           "torque_control_offered": False, "exclusivity": {"clean": True},
                           "stream": {"live": True}}},
    "topics": {"ages_s": {"/controller_manager/joint_states": 0.02,
                          "/ascamera/camera_publisher/rgb0/image": 0.068}},
}
APP_STATUS = {"state": "connected", "missing_for_following": [], "calibration_version": "a|b|c"}


def remote_with(arm_info: str = "Node name: lelab_mobile_agent\n",
                feedback_info: str = "Node name: ros_robot_controller\npublisher count: 1",
                machine_type: str = "LanderPi_Mecanum",
                node_list: str = "/ros_robot_controller\n/controller_manager\n",
                deployed_sha: str | None = None) -> dict:
    return {"car_container": (0, "true 0 no"),
            "deployed_agent_source": (0, f"{deployed_sha or local_sha()}  agent.py"),
            "profile_on_car": (0, "hardware-teleop-v1.json"),
            "arm_publisher": (0, arm_info),
            "gripper_publisher": (0, "Node name: lelab_mobile_agent\n"),
            "follower_feedback_publisher": (0, feedback_info),
            "machine_type_env": (0, machine_type),
            "vendor_board_node": (0, node_list)}


def local_sha() -> str:
    source = REPO / "ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/agent.py"
    return hashlib.sha256(source.read_bytes()).hexdigest()


def blocking(checks: list[dict]) -> list[str]:
    return [c["check"] for c in checks if not c["ok"] and c["blocking"]]


def names(checks: list[dict]) -> dict[str, dict]:
    return {c["check"]: c for c in checks}


class TestProbeListIsReadOnly:
    def test_container_inspection_runs_on_host_and_ros_probes_in_container(self):
        commands = dict(tp.probe_commands())
        inspect = tp.remote_command("car_container", commands["car_container"], "MentorPi")
        topic = tp.remote_command("arm_publisher", commands["arm_publisher"], "MentorPi")
        assert inspect.startswith("docker inspect ")
        assert "docker exec" not in inspect
        assert topic.startswith("docker exec MentorPi ")
        assert "/lelab/arm/trajectory_request" in topic

    def test_no_probe_could_change_the_car(self):
        assert tp.assert_read_only(tp.probe_commands()) == [], tp.probe_commands()

    def test_the_guard_itself_is_live(self):
        """A check that never fires is not a guard, so feed it one write and watch it answer."""
        flagged = tp.assert_read_only([("docker start", "docker start MentorPi")])
        assert flagged and "docker start" in flagged[0]

    def test_render_shows_every_check_and_runs_nothing(self):
        done = subprocess.run([sys.executable, str(SCRIPT), "--render"], capture_output=True,
                              text=True, timeout=60)
        out = done.stdout
        assert done.returncode == 0, f"exit {done.returncode}: {done.stderr[:400]}"
        for name, cmd in tp.probe_commands():
            assert name in out, name
            assert cmd in out, cmd
        assert "/ros-mobile/teleop/status" in out
        assert "docker exec" in out  # read queries do run inside the container


class TestVerdicts:
    def test_missing_or_stale_feedback_and_chassis_lock_never_pass(self):
        agent = {**AGENT_STATUS, "control": {"teleop": {}},
                 "topics": {"ages_s": {"/controller_manager/joint_states": None}}}
        remote = remote_with(feedback_info="Publisher count: 0\nSubscription count: 1")
        result = names(tp.evaluate(APP_STATUS, agent, ["usb"], remote, "pi@car"))
        assert not result["chassis_locked_as_declared"]["ok"]
        assert not result["follower_feedback_stream_visible"]["ok"]
        assert not result["follower_feedback_publisher"]["ok"]

    def test_a_healthy_pass_leaves_no_blocking_gap(self):
        checks = tp.evaluate(APP_STATUS, AGENT_STATUS, ["usb-FTDI2517537-if00-port0"],
                             remote_with(), "pi@car-host")
        assert blocking(checks) == [], checks

    def test_missing_inputs_are_named_instead_of_assumed(self):
        checks = tp.evaluate(None, None, [], {"car_container": (127, "skipped: no --pi target")}, "")
        got = blocking(checks)
        for expected in ("leader_serial_on_upper_host", "car_agent_reachable", "upper_app_reachable"):
            assert expected in got, got
        assert "no leader USB identity detected" in names(checks)["leader_serial_on_upper_host"]["detail"]
        assert names(checks)["car_container_running"]["ok"] is False, "skipped remote work is not a pass"

    def test_the_accepted_read_only_profile_is_not_mistaken_for_control(self):
        agent = dict(AGENT_STATUS, profile="hardware_readonly_v1", read_only=True)
        checks = tp.evaluate(APP_STATUS, agent, ["x"], remote_with(), "pi@host")
        assert "car_profile_is_teleop" in blocking(checks)

    def test_an_agent_that_offers_torque_or_unlocks_the_chassis_fails_the_pass(self):
        broken = {**AGENT_STATUS,
                  "control": {"teleop": {**AGENT_STATUS["control"]["teleop"],
                                         "chassis_locked": False, "torque_control_offered": True}}}
        checks = tp.evaluate(APP_STATUS, broken, ["x"], remote_with(), "pi@host")
        assert {"chassis_locked_as_declared", "hold_policy_declared_without_torque_off"} <= set(blocking(checks))

    def test_no_measured_feedback_stream_is_a_gap_not_a_zero(self):
        silent = {**AGENT_STATUS, "topics": {"ages_s": {"/ascamera/camera_publisher/rgb0/image": 0.07}}}
        checks = tp.evaluate(APP_STATUS, silent, ["x"], remote_with(), "pi@host")
        assert "follower_feedback_stream_visible" in blocking(checks)

    def test_a_foreign_trajectory_publisher_is_reported_but_not_concealed(self):
        arm = "Node name: lelab_mobile_agent\nNode name: ros_robot_controller_joy\n"
        checks = tp.evaluate(APP_STATUS, AGENT_STATUS, ["x"], remote_with(arm_info=arm), "pi@host")
        check = names(checks)["no_foreign_publisher_on_/lelab/arm/trajectory_request"]
        assert check["ok"] is False and check["blocking"] is False
        assert "ros_robot_controller_joy" in check["detail"]

    def test_a_stale_deployed_copy_is_refused_loudly(self):
        """The colcon-copy mistake: the car may run different code than the source under review."""
        checks = tp.evaluate(APP_STATUS, AGENT_STATUS, ["x"], remote_with(deployed_sha="0" * 64),
                             "pi@host")
        check = names(checks)["deployed_agent_matches_source"]
        assert check["ok"] is False and local_sha() in check["detail"]

    def test_an_unset_machine_type_is_named_as_the_reason_the_board_node_cannot_start(self):
        checks = tp.evaluate(APP_STATUS, AGENT_STATUS, ["x"],
                             remote_with(machine_type="MACHINE_TYPE_UNSET"), "pi@host")
        check = names(checks)["vendor_node_machine_type_exported"]
        assert check["ok"] is False and "KeyError" in check["detail"]

    def test_a_board_node_already_in_the_graph_is_reported_as_the_uart_owner(self):
        checks = tp.evaluate(APP_STATUS, AGENT_STATUS, ["x"], remote_with(), "pi@host")
        assert names(checks)["vendor_board_node_driving_the_uart"]["ok"] is True

    def test_no_board_node_is_reported_instead_of_assumed_present(self):
        checks = tp.evaluate(APP_STATUS, AGENT_STATUS, ["x"], remote_with(node_list="/ascamera\n"),
                             "pi@host")
        check = names(checks)["vendor_board_node_driving_the_uart"]
        assert check["ok"] is False and "nothing drives" in check["detail"]
        assert check["blocking"] is False, "the accepted read-only pass never needed the vendor stack"
