"""Exclusive arm control is refused unless the graph proves nobody else steers the servos.

需求六: a vendor joystick or phone app drives the same servos without passing through this gateway,
so an HTTP control lease proves nothing. The deployed ``set_mode`` is imported for real and only the
node construction is bypassed, which catches the wiring being dropped, not just the helper.
"""
from __future__ import annotations

import re
import sys
import threading
import time
import types
import unittest
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "lelab_mobile_agent"))

from lelab_mobile_agent.contract import exclusivity_report, load_profile  # noqa: E402

try:  # the agent needs rclpy plus the vendor message packages; skip rather than fail elsewhere
    from lelab_mobile_agent.agent import ApiError, MobileAgent
    AGENT_IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    ApiError = MobileAgent = None
    AGENT_IMPORT_ERROR = exc

TELEOP_PROFILE = REPO / "configs" / "ros2_collection" / "hardware-teleop-v1.json"
PACKAGE_PROFILE = REPO / "ros2_ws" / "src" / "lelab_mobile_agent" / "config" / "hardware-teleop-v1.json"
OWN = "lelab_mobile_agent"
ARM = "/lelab/arm/trajectory_request"
GRIPPER = "/lelab/gripper/trajectory_request"


class ReportTests(unittest.TestCase):
    def test_only_the_gateway_publishes(self):
        report = exclusivity_report({ARM: [OWN], GRIPPER: [OWN]}, OWN)
        self.assertEqual(report, {"clean": True, "foreign": {}, "unknown": [], "reason": None})

    def test_no_publisher_yet_is_clean(self):
        self.assertTrue(exclusivity_report({ARM: []}, OWN)["clean"])

    def test_foreign_node_is_named_per_topic(self):
        report = exclusivity_report({ARM: [OWN, "joy_to_motor"], GRIPPER: ["app_server"]}, OWN)
        self.assertFalse(report["clean"])
        self.assertEqual(report["reason"], "vendor_publisher_present")
        self.assertEqual(report["foreign"], {ARM: ["joy_to_motor"], GRIPPER: ["app_server"]})

    def test_duplicate_publishers_are_collapsed(self):
        self.assertEqual(exclusivity_report({ARM: ["joy", "joy"]}, OWN)["foreign"], {ARM: ["joy"]})

    def test_exempt_names_are_not_foreign(self):
        self.assertTrue(exclusivity_report({ARM: ["relay"]}, OWN, ["relay"])["clean"])

    def test_unreadable_topology_fails_closed(self):
        report = exclusivity_report({ARM: None, GRIPPER: [OWN]}, OWN)
        self.assertFalse(report["clean"], "an unknown graph must not read as exclusive")
        self.assertEqual(report["reason"], "publisher_topology_unknown")
        self.assertEqual(report["unknown"], [ARM])

    def test_a_visible_intruder_outranks_an_unknown_topic(self):
        self.assertEqual(exclusivity_report({ARM: ["joy"], GRIPPER: None}, OWN)["reason"],
                         "vendor_publisher_present")


def fake_agent(publishers, *, exempt=None, base_topic=None, profile=None):
    """A MobileAgent instance with just the attributes set_mode() touches."""
    if MobileAgent is None:
        raise AGENT_IMPORT_ERROR
    teleop = dict((profile or load_profile(TELEOP_PROFILE))["teleop"])
    if exempt is not None:
        teleop["exclusive_exempt_nodes"] = exempt
    node = object.__new__(MobileAgent)
    node.read_only = False
    node.lock = threading.RLock()
    node.control_token, node.control_expires = "tok", time.monotonic() + 30
    node.control_mode, node.leader_arm_enabled = "idle", True
    node.expert_status, node.expert_cancel = {"running": False}, threading.Event()
    node.follower_feedback = object()  # configured; freshness is a separate gate
    node.teleop_cfg, node.base_topic = teleop, base_topic
    node.base_pub = node.event_pub = None
    node.last_teleop = {"stale": "target carried over from the previous session"}
    node.last_exclusivity = None
    node.session_id = node.episode_id = None
    node.clock_domain = "ros_time"
    node.events, node.rejections = deque(maxlen=100), deque(maxlen=100)
    node.get_name = lambda: OWN
    node.get_logger = lambda: types.SimpleNamespace(warning=lambda *a: None)
    node.get_clock = lambda: types.SimpleNamespace(now=lambda: types.SimpleNamespace(nanoseconds=0))

    def info(topic):
        if topic not in publishers:
            raise RuntimeError("graph is not ready")
        return [types.SimpleNamespace(node_name=name) for name in publishers[topic]]

    node.get_publishers_info_by_topic = info
    return node


@unittest.skipIf(MobileAgent is None, f"agent module unavailable: {AGENT_IMPORT_ERROR}")
class ModeSwitchTests(unittest.TestCase):
    def test_a_clean_graph_enables_leader_arm(self):
        node = fake_agent({ARM: [OWN], GRIPPER: [OWN]})
        self.assertEqual(node.set_mode("tok", "leader_arm"), {"mode": "leader_arm"})
        self.assertEqual(node.control_mode, "leader_arm")
        self.assertTrue(node.last_exclusivity["clean"])

    def test_a_configured_but_stale_target_clears_on_the_mode_change(self):
        node = fake_agent({ARM: [OWN], GRIPPER: [OWN]})
        node.set_mode("tok", "leader_arm")
        self.assertIsNone(node.last_teleop, "switching mode must not keep the old target live")

    def test_vendor_publisher_blocks_enable_before_any_motion(self):
        node = fake_agent({ARM: [OWN, "joy_to_motor"], GRIPPER: [OWN]})
        with self.assertRaises(ApiError) as ctx:
            node.set_mode("tok", "leader_arm")
        self.assertEqual((ctx.exception.status, ctx.exception.reason), (409, "vendor_publisher_present"))
        self.assertEqual(node.control_mode, "idle", "refusing must leave the mode untouched")
        self.assertEqual(node.last_exclusivity["foreign"], {ARM: ["joy_to_motor"]})
        self.assertEqual(node.last_reject["reason"], "vendor_publisher_present")
        self.assertEqual([e["kind"] for e in node.events], ["command_rejected"])

    def test_unreadable_graph_refuses_enable(self):
        node = fake_agent({})  # every enumeration raises
        with self.assertRaises(ApiError) as ctx:
            node.set_mode("tok", "leader_arm")
        self.assertEqual(ctx.exception.reason, "publisher_topology_unknown")
        self.assertEqual(node.control_mode, "idle")

    def test_exempt_node_is_allowed_and_recorded_as_clean(self):
        node = fake_agent({ARM: [OWN, "gripper_relay"], GRIPPER: [OWN]}, exempt=["gripper_relay"])
        node.set_mode("tok", "leader_arm")
        self.assertTrue(node.last_exclusivity["clean"])

    def test_a_refusal_does_not_enable_and_can_be_retried_after_the_intruder_stops(self):
        node = fake_agent({ARM: [OWN, "joy_to_motor"], GRIPPER: [OWN]})
        with self.assertRaises(ApiError):
            node.set_mode("tok", "leader_arm")
        node.get_publishers_info_by_topic = lambda topic: [
            types.SimpleNamespace(node_name=name) for name in ({ARM: [OWN], GRIPPER: [OWN]}[topic])]
        node.set_mode("tok", "leader_arm")
        self.assertEqual(node.control_mode, "leader_arm")

    def test_keyboard_mode_is_not_guarded_by_the_arm_exclusivity_check(self):
        node = fake_agent({})
        node.set_mode("tok", "keyboard")
        self.assertIsNone(node.last_exclusivity, "only leader_arm claims exclusive servo motion")

    def test_guard_topics_cover_both_commanded_trajectories(self):
        node = fake_agent({ARM: [OWN], GRIPPER: [OWN]})
        self.assertEqual(node._guard_topics(), [ARM, GRIPPER])
        self.assertEqual(fake_agent({ARM: [OWN], GRIPPER: [OWN]}, base_topic="/cmd_vel")
                         ._guard_topics(), [ARM, GRIPPER, "/cmd_vel"])


class ProfileGuardTests(unittest.TestCase):
    def test_both_teleop_profiles_declare_the_exclusivity_guard(self):
        for path in (TELEOP_PROFILE, PACKAGE_PROFILE):
            teleop = load_profile(path)["teleop"]
            self.assertTrue(teleop.get("exclusive_control"), f"{path.name} does not require exclusivity")
            self.assertEqual(teleop.get("exclusive_guard_topics"), [ARM, GRIPPER], path.name)

    def test_both_profiles_state_a_real_hold_policy_that_is_not_torque_off(self):
        """需求五: "stop" must mean what this controller really does, never "torque is disabled"."""
        for path in (TELEOP_PROFILE, PACKAGE_PROFILE):
            policy = load_profile(path)["teleop"].get("hold_policy") or ""
            self.assertIn("torque is never disabled", policy, path.name)
            self.assertIn("last commanded position", policy, path.name)

    def test_the_deployed_agent_never_writes_torque_state(self):
        """Teleop commands positions only; the one torque writer stays calibration-scoped.

        This guard was written as a blanket ban, before the follower calibration job
        existed, and calibration legitimately cycles torque. Dropping the assertion would
        hide the original hazard - a teleop path releasing a loaded arm - so the rule is
        scoped instead: no torque call outside the named calibration API, and that API
        must refuse a frame-stream session before it touches the bus.
        """
        pkg = REPO / "ros2_ws" / "src" / "lelab_mobile_agent" / "lelab_mobile_agent"
        names = ("agent.py", "follower_feedback.py", "watchdog.py", "contract.py")
        joined = "".join((pkg / name).read_text(encoding="utf-8") for name in names)
        calibration_api = {"set_torque", "_calibration_set_torque",
                           "follower_calibration_torque"}
        offenders = [call for call in
                     re.findall(r"([A-Za-z_][A-Za-z0-9_]*torque[A-Za-z0-9_]*)\s*\(", joined)
                     if call not in calibration_api]
        for key in ('"enable_torque"', '"torque_setting"'):
            if key in joined:
                offenders.append(key)
        self.assertEqual(offenders, [], "no non-calibration car-side path may toggle torque")

        writer = (pkg / "agent.py").read_text(encoding="utf-8")
        start = writer.index("def _calibration_set_torque")
        body = writer[start:writer.index("def ", start + 10)]
        self.assertIn("_refuse_frame_stream_action", body.splitlines()[1],
                      "the calibration torque writer must refuse a frame-stream session first")
        self.assertIn("bus_servo/set_state", body,
                      "the torque writer is the bus-state message, not a position command")

    def test_the_frame_stream_path_still_refuses_torque(self) -> None:
        """The refusal is an behaviour gate, so it is executed, not just read as text."""
        if MobileAgent is None:
            self.skipTest(f"the production agent needs rclpy: {AGENT_IMPORT_ERROR}")
        source = (REPO / "ros2_ws" / "src" / "lelab_mobile_agent" / "lelab_mobile_agent"
                  / "agent.py").read_text(encoding="utf-8")
        refuse = source.index("def _refuse_frame_stream_action")
        body = source[refuse:source.index("def ", refuse + 10)]
        self.assertIn("frame_stream", body, "the refusal must key off the frame transport")

    def test_the_deployed_status_block_declares_hold_and_torque_claims(self):
        """The page can only show the car's own words if the shipped status() emits them.

        status() needs a live node, so the block is read from the deployed source: a dropped key
        must fail here instead of silently leaving the operator with our assumption.
        """
        src = (REPO / "ros2_ws" / "src" / "lelab_mobile_agent" / "lelab_mobile_agent" / "agent.py").read_text(
            encoding="utf-8")
        block = src.split('"teleop": {')[1].split('"safety":')[0]
        for needle in ('"hold_policy": self.teleop_cfg.get("hold_policy")',
                       '"torque_control_offered": False',
                       '"exclusivity_required": True',
                       '"exclusivity": self.last_exclusivity'):
            self.assertIn(needle, block, f"car status no longer reports {needle}")

    def test_the_readonly_profile_is_not_given_the_guard_or_a_new_mode(self):
        readonly = load_profile(REPO / "configs" / "ros2_collection" / "hardware-readonly-v1.json")
        self.assertNotIn("leader_arm", (readonly.get("modes") or {}).get("arm_sources", []))
        self.assertNotIn("exclusive_control", readonly.get("teleop") or {})


if __name__ == "__main__":
    unittest.main()
