"""Car-side gates that only the deployed agent can show, all three found by the local rehearsal.

`tools/ros2_collection/local_car_rehearsal.py` runs this same agent code against synthetic topics,
and every one of these blocked a real teleop session rather than mis-reporting one:

- the topic health table only knew what the node had *received*, so a required topic the node
  itself publishes (the teleop state) read as permanently missing: start_session refused, and the
  running health poll marked the session `required_stream_missing` so no teleop session could
  reach READY;
- `start_episode` listed the modes allowed to open an episode before `leader_arm` existed, so the
  recording subtask was refused in exactly the mode the teleop session must run in;
- the control tick returned as soon as it saw no chassis publisher, which is the locked-chassis
  phase this whole stage is built around: arm and gripper commands were accepted, recorded as
  applied and answered by the follow loop, and not one trajectory was ever published.

Only the node is bypassed (`object.__new__`); the code under test is the deployed one.
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

from lelab_mobile_agent.contract import load_profile  # noqa: E402

try:
    from lelab_mobile_agent.agent import ApiError, MobileAgent
    AGENT_IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    MobileAgent = None
    AGENT_IMPORT_ERROR = exc

TELEOP_PROFILE = REPO / "configs" / "ros2_collection" / "hardware-teleop-v1.json"
CAMERA = "/ascamera/camera_publisher/rgb0/image"
CAMERA_INFO = "/ascamera/camera_publisher/rgb0/camera_info"
FEEDBACK = "/controller_manager/joint_states"
TELEOP_STATE = "/lelab/recording/teleop"


def base_agent(**overrides):
    if MobileAgent is None:
        raise AGENT_IMPORT_ERROR
    profile = load_profile(TELEOP_PROFILE)
    node = object.__new__(MobileAgent)
    node.profile = profile
    node.read_only = False
    node.lock = threading.RLock()
    node.control_token, node.control_expires = "tok", time.monotonic() + 30
    node.control_mode = "leader_arm"
    node.estop_latched = False
    node.get_name = lambda: "lelab_mobile_agent"
    node.get_logger = lambda: types.SimpleNamespace(
        warning=lambda *a: None, info=lambda *a: None, error=lambda *a: None)
    node._sim_ns = lambda: 123
    node._emit = lambda kind, **payload: {"kind": kind, **payload}
    node.events, node.rejections = deque(maxlen=100), deque(maxlen=100)
    for key, value in overrides.items():
        setattr(node, key, value)
    return node


class TopicHealthTests(unittest.TestCase):
    """A topic this node publishes is traffic, not a hole in the recording."""

    def agent(self, rx=None, tx=None):
        return base_agent(last_rx=dict(rx or {}), last_tx=dict(tx or {}))

    def test_a_self_published_required_topic_counts_as_fresh(self):
        now = time.monotonic()
        node = self.agent(rx={CAMERA: now, CAMERA_INFO: now, FEEDBACK: now},
                          tx={TELEOP_STATE: now})
        self.assertEqual(node.topic_health(missing_only=True), [], node.topic_health())
        self.assertIn(TELEOP_STATE, node._topic_ages(now))
        self.assertLess(node._topic_ages(now)[TELEOP_STATE], 0.5)

    def test_the_receive_only_table_is_what_used_to_block_the_session(self):
        # Mutation guard: if _topic_ages stops consulting last_tx this verdict flips back to the
        # exact 409 the rehearsal hit, so the fix cannot rot into an untested branch.
        now = time.monotonic()
        node = self.agent(rx={CAMERA: now, CAMERA_INFO: now, FEEDBACK: now}, tx={})
        self.assertEqual(node.topic_health(missing_only=True), [TELEOP_STATE])

    def test_a_topic_seen_by_neither_side_stays_missing(self):
        node = self.agent(rx={CAMERA: time.monotonic() - 30.0})
        missing = node.topic_health(missing_only=True)
        self.assertIn(FEEDBACK, missing)
        self.assertIn(TELEOP_STATE, missing)

    def test_the_newest_of_receive_and_publish_wins(self):
        # A five-second-old reading must not be reported as fresh because the node also published
        # the topic just now, and vice versa: the newer of the two is the traffic age.
        now = time.monotonic()
        node = self.agent(rx={CAMERA: now, CAMERA_INFO: now, FEEDBACK: now - 5.0},
                          tx={FEEDBACK: now})
        self.assertEqual(node.topic_health(missing_only=True), [TELEOP_STATE])
        self.assertLess(node._topic_ages(now)[FEEDBACK], 0.5)


class EpisodeModeTests(unittest.TestCase):
    """Recording is the teleop session's own subtask, so leader_arm must be able to open one."""

    def episode_agent(self, mode: str):
        started: list[tuple] = []
        return base_agent(
            cube=None, container=None, box=None, task_name="place_in_tray", joints={},
            runtime={"clock_domain": "monotonic"}, clock_domain="monotonic",
            camera_profile="wrist_original", episode_id=None, episode_source=None,
            episode_started_wall=0.0, episode_initial=None, episode_max_lift=0.0,
            episode_contacts=0, episode_phases=[], last_arm_targets=[], last_gripper_target=0.0,
            last_action=[0.0] * 9, last_action_seq=0, command_seq=0,
            _action_vector=lambda: [0.0] * 9,
            control_mode=mode,
            sessions=types.SimpleNamespace(current_episode=None, active=True,
                                           start_episode=lambda *a: started.append(a)),
            started=started), started

    def test_leader_arm_mode_may_open_an_episode(self):
        node, started = self.episode_agent("leader_arm")
        out = node.start_episode("tok", source="leader_arm", task="teleop grasp")
        self.assertEqual(len(started), 1, "the recorder must be told about the episode")
        self.assertEqual(out["source"], "leader_arm")
        self.assertEqual(started[0][2], "leader_arm", "the manifest keeps the teleop provenance")

    def test_every_mode_the_gateway_can_hold_still_opens_an_episode(self):
        # The four modes are the ones set_mode accepts; leader_arm was simply missing from this list.
        for mode in ("idle", "keyboard", "scripted_expert", "leader_arm"):
            node, started = self.episode_agent(mode)
            node.start_episode("tok", source="leader_arm", task="teleop grasp")
            self.assertEqual(len(started), 1, mode)

    def test_a_mode_outside_that_list_still_cannot_record(self):
        node, started = self.episode_agent("policy_inference")
        with self.assertRaises(ApiError) as ctx:
            node.start_episode("tok", source="leader_arm")
        self.assertEqual((ctx.exception.status, ctx.exception.reason), (409, "mode"))
        self.assertEqual(ctx.exception.detail, "policy_inference")
        self.assertEqual(started, [])


class ControlTickTests(unittest.TestCase):
    """Locked chassis means no base publisher, not no actuator path."""

    class Pub:
        def __init__(self):
            self.messages = []

        def publish(self, msg):
            self.messages.append(msg)

    def tick_agent(self, base_pub=None, **overrides):
        node = base_agent(
            base_pub=base_pub, arm_pub=self.Pub(), gripper_pub=self.Pub(),
            heartbeat_pub=self.Pub(), arm_queue=queue.Queue(), gripper_queue=queue.Queue(),
            last_heartbeat=0.0, last_zero=0.0, base_valid_until=0.0, safety_wall=0.0,
            base_target=None, episode_id=None, gate=None)
        node.arm_queue.put("arm-1")
        node.arm_queue.put("arm-2")
        node.gripper_queue.put("grip-1")
        for key, value in overrides.items():
            setattr(node, key, value)
        return node

    def test_a_locked_chassis_still_publishes_arm_and_gripper_targets(self):
        node = self.tick_agent()
        node._control_tick()
        self.assertEqual(node.arm_pub.messages, ["arm-1", "arm-2"])
        self.assertEqual(node.gripper_pub.messages, ["grip-1"])
        self.assertTrue(node.arm_queue.empty())

    def test_the_base_stays_silent_when_the_chassis_is_locked(self):
        node = self.tick_agent()
        node._control_tick()
        self.assertIsNone(node.base_pub, "no base publisher may exist while the chassis is locked")
        self.assertTrue(node.heartbeat_pub.messages, "the watchdog heartbeat must keep running")

    def test_targets_queued_while_the_lease_was_dead_are_dropped_not_replayed(self):
        # 六: recovery must not replay expired actions. The old code left them queued and the next
        # valid tick flushed every stale pose at once.
        node = self.tick_agent(estop_latched=True)
        node._control_tick()
        self.assertEqual(node.arm_pub.messages, [])
        self.assertEqual(node.gripper_pub.messages, [])
        self.assertTrue(node.arm_queue.empty(), "expired targets must not survive the drop")

    def test_a_dropped_lease_clears_the_control_session_and_the_queue(self):
        node = self.tick_agent(control_expires=time.monotonic() - 1.0)
        node._control_tick()
        self.assertIsNone(node.control_token)
        self.assertEqual(node.control_mode, "idle")
        self.assertEqual(node.arm_pub.messages, [])
        self.assertTrue(node.arm_queue.empty())


if __name__ == "__main__":
    unittest.main()
