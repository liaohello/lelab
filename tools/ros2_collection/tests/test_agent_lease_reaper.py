"""The control lease reaper and the stop latch, on the car side, without hardware.

Three behaviours the offline integration round kept getting wrong: an expired frame-mode
lease kept advertising its owner and its output permission, an explicit emergency stop
latched nothing on the car, and a stop could not be lifted once its own lease had died.
Only the rclpy node construction is bypassed, so a dropped call inside the real methods
turns red here instead of on the arm.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
import types
import unittest
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "lelab_mobile_agent"))

try:
    from lelab_mobile_agent.agent import ApiError, MobileAgent
    AGENT_IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001 - rclpy and the vendor messages live on the car side
    ApiError = MobileAgent = None
    AGENT_IMPORT_ERROR = exc


def frame_agent():
    """A MobileAgent carrying only what the tick and the stop path touch.

    No actuator publisher exists, which is the frame-mode profile: that absence used to
    skip the whole lease reaper.
    """
    node = object.__new__(MobileAgent)
    node.read_only = False
    node.lock = threading.RLock()
    node.control_token = "tok"
    node.control_expires = time.monotonic() + 30.0
    node.control_client = "hybrid_teleoperation"
    node.control_epoch = 12345
    node.control_mode = "leader_arm"
    node.frame_outputs_enabled = True
    node.profile = {}
    node.joints = {
        "joint1": 0.0, "joint2": 0.0, "joint3": 0.0,
        "joint4": 0.0, "joint5": 0.0, "gripper_base_joint": 0.0,
    }
    node.estop_latched = False
    node.estop_owner_client = None
    node.estop_requested_at = None
    node.gate = object()
    node.base_pub = node.arm_pub = node.gripper_pub = None
    node.heartbeat_pub = node.estop_pub = node.reset_pub = None
    node.event_pub = None
    node.last_zero = 0.0
    node.base_valid_until = 0.0
    node.safety_wall = 0.0
    node.expert_status = {"running": False}
    node.expert_cancel = threading.Event()
    node.arm_queue, node.gripper_queue = queue.Queue(), queue.Queue()
    node.arm_queue.put({"stale": "target from the expiring lease"})
    node.hybrid_control_token = "tok"
    node.hybrid_gateway = None
    node.licence_bridge = None
    node.events, node.rejections = deque(maxlen=100), deque(maxlen=100)
    node.session_id = node.episode_id = None
    node.clock_domain = "ros_time"
    node.get_name = lambda: "lelab_mobile_agent"
    node.get_clock = lambda: types.SimpleNamespace(
        now=lambda: types.SimpleNamespace(nanoseconds=0))
    node.get_logger = lambda: types.SimpleNamespace(
        info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None)
    node._bridge_wake = lambda *a, **k: None
    node._zero_base_now = lambda *a, **k: None
    return node


@unittest.skipIf(MobileAgent is None, f"the production agent needs rclpy: {AGENT_IMPORT_ERROR}")
class LeaseReaper(unittest.TestCase):
    def test_the_tick_reaps_an_expired_lease_with_no_legacy_publishers(self):
        node = frame_agent()
        node.control_expires = time.monotonic() - 0.01
        node._control_tick()
        self.assertIsNone(node.control_token, "the token must go with the lease")
        self.assertIsNone(node.control_client,
                          "a stale owner name next to active:false reads as continued ownership")
        self.assertIsNone(node.control_epoch)
        self.assertFalse(node.frame_outputs_enabled,
                         "the frame permission may not outlive the lease it was granted under")
        self.assertTrue(node.arm_queue.empty(), "a queued target must not survive to the next lease")

    def test_a_lease_expired_by_the_lazy_token_check_also_drops_the_identity(self):
        """``_check_token`` noticed the expiry first, so it may not do half the teardown."""
        node = frame_agent()
        node.control_expires = time.monotonic() - 0.01
        with self.assertRaises(ApiError) as raised:
            node._check_token("tok")
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.reason, "control_expired")
        self.assertIsNone(node.control_client)
        self.assertFalse(node.frame_outputs_enabled)

    def test_the_reaper_finishes_a_teardown_another_path_left_half_done(self):
        node = frame_agent()
        node.control_token = None          # some path dropped the token only
        node._control_tick()
        self.assertIsNone(node.control_client,
                          "residual identity with no live lease must be reaped, not trusted")
        self.assertFalse(node.frame_outputs_enabled)

    def test_a_live_lease_is_left_alone_by_the_same_tick(self):
        node = frame_agent()
        node._control_tick()
        self.assertEqual(node.control_client, "hybrid_teleoperation")
        self.assertTrue(node.frame_outputs_enabled)


@unittest.skipIf(MobileAgent is None, f"the production agent needs rclpy: {AGENT_IMPORT_ERROR}")
class StopLatch(unittest.TestCase):
    def test_an_explicit_stop_latches_and_remembers_the_owner(self):
        node = frame_agent()
        answer = node.estop(None, "operator pressed the red button")
        self.assertIs(answer["estop"], True)
        self.assertTrue(node.estop_latched)
        self.assertEqual(node.estop_owner_client, "hybrid_teleoperation")
        self.assertFalse(node.frame_outputs_enabled)

    def test_acquire_is_refused_while_the_stop_is_latched(self):
        node = frame_agent()
        node.estop(None, "operator")
        with self.assertRaises(ApiError) as raised:
            node.acquire("somebody-else")
        self.assertEqual(raised.exception.reason, "estop_latched")

    def test_a_stranger_cannot_lift_the_stop_after_the_lease_dies(self):
        node = frame_agent()
        node.estop(None, "operator")
        node.control_token = None          # the lease expired; nobody holds a usable token
        with self.assertRaises(ApiError) as raised:
            node.clear_estop(None, "an-unknown-client")
        self.assertEqual(raised.exception.status, 403,
                         "clearing must not become an unauthenticated back door")
        self.assertTrue(node.estop_latched)

    def test_the_prior_owner_can_lift_the_stop_and_that_grants_no_motion(self):
        node = frame_agent()
        node.estop(None, "operator")
        node.control_token = None
        answer = node.clear_estop(None, "hybrid_teleoperation")
        self.assertIs(answer["estop"], False)
        self.assertEqual(answer["authorised_by"], "prior_owner_recovery")
        self.assertIs(answer["motion_granted"], False)
        self.assertFalse(node.estop_latched)
        self.assertFalse(node.frame_outputs_enabled,
                         "lifting a stop may never re-arm the outputs by itself")
        self.assertIsNone(node.estop_owner_client, "the grant is single use")

    def test_a_live_token_still_clears_without_the_recovery_grant(self):
        node = frame_agent()
        node.estop("tok", "operator")
        answer = node.clear_estop("tok")
        self.assertEqual(answer["authorised_by"], "control_token")
        self.assertFalse(node.estop_latched)

    def test_a_recovery_acquire_can_clear_but_cannot_enable_outputs(self):
        node = frame_agent()
        node.estop(None, "web stop after the old lease disappeared")
        node.control_token = None
        acquired = node.acquire("hybrid_teleoperation", recovery=True)
        self.assertTrue(acquired["recovery"])
        self.assertTrue(node.estop_latched)
        answer = node.clear_estop(acquired["token"], "hybrid_teleoperation")
        self.assertEqual(answer["authorised_by"], "control_token")
        self.assertFalse(node.estop_latched)
        self.assertFalse(node.frame_outputs_enabled)

    def test_recovery_acquire_rejects_an_unrelated_operator(self):
        node = frame_agent()
        node.estop(None, "operator")
        node.control_token = None
        with self.assertRaises(ApiError) as raised:
            node.acquire("unrelated-operator", recovery=True)
        self.assertEqual(raised.exception.status, 403)
        self.assertTrue(node.estop_latched)
        self.assertFalse(node.frame_outputs_enabled)


if __name__ == "__main__":
    unittest.main()
