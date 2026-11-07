"""Hardware-free tests for the teleop session coordinator."""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "app_ros_mobile"))

from lelab.mobile_teleop import CalibrationStore  # noqa: E402
from lelab.mobile_teleop_coordinator import (  # noqa: E402
    BridgeError,
    CoordinatorConfig,
    MobileTeleopCoordinator,
    UPPER_LEASE_TASK,
)

ARM = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]


class FakeLease:
    def __init__(self):
        self.acquired = 0
        self.released = []
        self.active = True
        self.owner = None

    def acquire(self, task, *, owner, role="operator", ttl_seconds=None, note=None):
        self.acquired += 1
        self.owner = owner
        outer = self

        class Handle:
            token = "upper-token"

        return Handle()

    def status(self):
        return {"state": "held" if self.active else "idle", "task": "teleoperation",
                "owner": self.owner, "expires_in": 30.0 if self.active else 0.0}

    def renew(self, token=None, **kw):
        return {"ok": True}

    def release(self, token=None, reason=None):
        self.released.append(reason)
        self.active = False
        return {"ok": True}


class FakeLeader:
    def __init__(self, positions_deg=None, gripper=40.0, connected=True, healthy=True):
        self.deg = positions_deg if positions_deg is not None else {
            "shoulder_pan": 10.0, "shoulder_lift": 20.0, "elbow_flex": 30.0,
            "wrist_flex": 40.0, "wrist_roll": 50.0}
        self.gripper = gripper
        self.connected = connected
        self.healthy = healthy
        self.closed = False

    def snapshot(self, now=None):
        import math
        return {"connected": self.connected, "healthy": self.healthy,
                "reason": "ok" if self.healthy else "leader_unreachable",
                "positions_deg": dict(self.deg),
                "positions_rad": {k: v * math.pi / 180.0 for k, v in self.deg.items()},
                "gripper_percent": self.gripper, "missing_joints": [], "age_s": 0.01}

    def close(self):
        self.closed = True
        return {"closed": True}


class FakeGateway:
    """Stands in for the car HTTP gateway and records every call it receives.

    shape="nested" reproduces the field layout the deployed agent reports, where the follower
    feedback and teleop settings live inside the control block; that is the shape the real car
    sends and the reason the gate must not read them only at the top level.
    """

    def __init__(self, control_active=True, feedback_available=True, mode="leader_arm",
                fail_paths=(), shape="flat"):
        self.calls: list[tuple[str, str, str | None]] = []
        self.payloads: dict[str, list[dict]] = {}
        self.control_active = control_active
        self.feedback_available = feedback_available
        self.mode = mode
        self.fail_paths = set(fail_paths)
        self.shape = shape
        self.token = "car-token"

    def __call__(self, path, method="GET", payload=None, token=None):
        self.calls.append((path, method, token))
        self.payloads.setdefault(path, []).append(payload or {})
        if path in self.fail_paths:
            raise BridgeError(502, f"tunnel down on {path}")
        if path == "/status":
            control = {"active": self.control_active, "client_id": "operator-a",
                       "mode": self.mode, "leader_arm_available": True}
            feedback = {"available": self.feedback_available,
                        "reason": "ok" if self.feedback_available else "no_feedback_yet",
                        "positions_rad": {j: 0.0 for j in ARM},
                        "age_s": 0.02 if self.feedback_available else None,
                        "missing_joints": [] if self.feedback_available else ARM,
                        "source": ["/controller_manager/joint_states"]}
            teleop = {"enabled": True, "chassis_locked": True,
                      "exclusivity": {"clean": True, "foreign": {}, "unknown": [], "reason": None},
                      "hold_policy": "vendor servos hold the last commanded position in position mode; "
                                     "torque is never disabled as a substitute for stopping",
                      "stream": {"expected": True, "live": self.feedback_available,
                               "ttl_s": 0.6, "age_s": 0.03,
                               "remaining_s": 0.4 if self.feedback_available else -0.1,
                               "clock_domain": "monotonic"}}
            if self.shape == "nested":
                control = {**control, "follower_feedback": feedback, "teleop": teleop}
                return {"read_only": False, "control": control}
            return {"read_only": False, "control": control,
                    "follower_feedback": feedback, "teleop": teleop}
        if path == "/control/acquire":
            return {"token": self.token}
        if path == "/control/arm":
            return {"applied": payload["arm"]["positions"], "clipped": []}
        if path == "/control/gripper":
            return {"applied": [payload["gripper"]["position"]], "closed": []}
        if path == "/control/estop":
            return {"estop": True}
        if path.startswith("/recording/"):
            return {"session_id": "ros_session", "episode_id": "ep_1"}
        return {"ok": True}


def profile():
    return {
        "limits": {"joint_positions_rad": {j: [-2.09, 2.09] for j in ARM}},
        "teleop": {
            "enabled": True, "leader_arm_mode": True, "chassis_locked": True, "command_hz": 20.0,
            "mapping": {
                "joint_order": ARM,
                "filter_alpha": 1.0, "max_step_rad_s": 100.0,
                "joints": [
                    {"leader": "shoulder_pan", "follower": "joint1", "direction": 1, "scale": 1.0},
                    {"leader": "shoulder_lift", "follower": "joint2", "direction": 1, "scale": 1.0},
                    {"leader": "elbow_flex", "follower": "joint3", "direction": 1, "scale": 1.0},
                    {"leader": "wrist_flex", "follower": "joint4", "direction": 1, "scale": 1.0},
                    {"leader": "wrist_roll", "follower": "r_joint", "direction": 1, "scale": 1.0},
                ],
                "unused_follower_joints": ["joint5"],
                "gripper": {"follower_joint": "r_joint", "pulse_span_per_hundred_pct": 6.0,
                            "pulse_min": 100.0, "pulse_max": 900.0, "direction": -1,
                            "flipped": True, "initial_position_raw": 500},
            },
        },
    }


def build(store_root, gateway=None, leader=None, lease=None, feedback_available=True,
          control_active=True, shape="flat"):
    store = CalibrationStore(Path(store_root))
    identities = seed_calibration(store)
    gateway = gateway or FakeGateway(feedback_available=feedback_available,
                                     control_active=control_active, shape=shape)
    coord = MobileTeleopCoordinator(
        CoordinatorConfig(agent_url="http://car.invalid", command_hz=20.0),
        profile(), store=store, leader_reader=leader or FakeLeader(), transport=gateway,
        lease_manager=lease or FakeLease())
    coord.identities = identities
    return coord, gateway, identities


LEADER = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]


def seed_calibration(store):
    leader = {"joint_order": LEADER, "device_identity": {"serial": "5B91045575"},
              "motors": {j: {"id": i + 1, "drive_mode": 0, "homing_offset": 0,
                             "range_min": -200.0, "range_max": 200.0}
                         for i, j in enumerate(LEADER)},
              "gripper": {"id": 6, "range_min": 0.0, "range_max": 100.0}}
    follower = {"joint_order": ARM, "device_identity": {"car": "raspberrypi"},
                "zero_pulse": 500, "flipped_joints": ["r_joint"]}
    mapping = {"joint_order": ARM,
               "bound_identities": {"leader": "leader_A", "follower": "car_A"},
               "joints": [{"leader": l, "follower": f, "direction": 1, "scale": 1.0}
                          for l, f in zip(LEADER, ["joint1", "joint2", "joint3", "joint4", "r_joint"])],
               "gripper": {"follower_joint": "r_joint", "pulse_span_per_hundred_pct": 6.0}}
    store.save("leader", "leader_A", leader)
    store.save("follower", "car_A", follower)
    store.save("mapping", "map_A", mapping)
    return {"leader": "leader_A", "follower": "car_A", "mapping": "map_A"}


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="teleop_coord_")

    def acquire(self, coord):
        return coord.acquire("operator-a", "leader_A", "car_A", "map_A")

    def test_acquire_takes_exactly_one_upper_lease_and_switches_car_mode(self):
        coord, gateway, _ = build(self.dir)
        lease = coord.leases
        out = self.acquire(coord)
        self.assertEqual(lease.acquired, 1)
        self.assertEqual(out["state"], "calibrated")
        self.assertIn("/control/acquire", [c[0] for c in gateway.calls])
        self.assertIn("/control/mode", [c[0] for c in gateway.calls])
        self.assertEqual(gateway.payloads["/control/mode"][0]["mode"], "leader_arm")

    def test_acquire_rolls_back_upper_lease_when_car_refuses(self):
        coord, gateway, _ = build(self.dir)
        gateway.fail_paths = {"/control/acquire"}
        with self.assertRaises(BridgeError):
            self.acquire(coord)
        self.assertIn("car-acquire-failed", coord.leases.released)
        self.assertIsNone(coord.token)

    def test_acquire_mode_failure_releases_the_already_acquired_car_lease(self):
        coord, gateway, _ = build(self.dir)
        gateway.fail_paths = {"/control/mode"}
        with self.assertRaises(BridgeError):
            self.acquire(coord)
        self.assertIn(("/control/release", "POST", "car-token"), gateway.calls)
        self.assertIn("car-acquire-failed", coord.leases.released)

    def test_failed_car_rollback_is_reported_and_upper_lease_still_released(self):
        coord, gateway, _ = build(self.dir)
        gateway.fail_paths = {"/control/mode", "/control/release"}
        with self.assertRaises(BridgeError) as ctx:
            self.acquire(coord)
        self.assertIn("car lease release failed", ctx.exception.detail)
        self.assertIn("wait for car lease expiry", ctx.exception.detail)
        self.assertIn("car-acquire-failed", coord.leases.released)

    def test_release_stops_stream_before_waiting_for_remote_release(self):
        coord, gateway, _ = build(self.dir)
        self.engage(coord)
        original_transport = coord._transport

        def transport(path, method="GET", payload=None, token=None):
            if path == "/control/release":
                self.assertIsNone(coord.follow_thread)
            return original_transport(path, method, payload, token)

        coord._transport = transport
        coord.follow_thread = threading.current_thread()
        coord.release("user")

    def test_acquire_refuses_when_calibration_layers_missing(self):
        coord, gateway, identities = build(self.dir)
        coord.identities = {"leader": "nope", "follower": "car_A", "mapping": "map_A"}
        with self.assertRaises(BridgeError) as ctx:
            coord.acquire("operator-a", "nope", "car_A", "map_A")
        self.assertIn("calibration not ready", ctx.exception.detail)
        self.assertEqual(coord.leases.acquired, 0, "no lease may be taken before calibration checks")

    def test_preview_sends_nothing_to_actuators(self):
        coord, gateway, _ = build(self.dir)
        self.acquire(coord)
        before = len([c for c in gateway.calls if c[1] == "POST"])
        out = coord.preview_step()
        self.assertFalse(out["outputs_actuators"])
        self.assertEqual(len([c for c in gateway.calls if c[1] == "POST"]), before,
                         "preview must not add any POST to the gateway")
        self.assertIn("targets", out)
        self.assertTrue(out["targets"])
        self.assertEqual(sorted(out["feedback"]["positions_rad"]), sorted(ARM))

    def test_following_requires_fresh_follower_feedback(self):
        coord, gateway, _ = build(self.dir, feedback_available=False)
        self.acquire(coord)
        coord.set_state("enter_preview")
        with self.assertRaises(BridgeError) as ctx:
            coord.align()
        self.assertIn("align", ctx.exception.detail)
        coord2, gateway2, _ = build(self.dir)
        self.acquire(coord2)
        coord2.set_state("enter_preview")
        coord2.align()
        coord2.set_state("arm")
        coord2.token = None  # simulate the car lease dropping
        with self.assertRaises(BridgeError) as ctx:
            coord2.set_state("engage")
        self.assertIn("car_lease_invalid", ctx.exception.detail)

    def test_send_once_carries_session_seq_source_ttl_and_calibration(self):
        coord, gateway, _ = build(self.dir)
        self.acquire(coord)
        coord.set_state("enter_preview")
        coord.align()
        coord.set_state("arm")
        coord.set_state("engage")
        coord.send_once()
        arm = gateway.payloads["/control/arm"][0]
        self.assertEqual(arm["source"], "leader_arm")
        self.assertEqual(arm["mode"], "leader_arm")
        self.assertEqual(arm["session_id"], "car-token")
        self.assertEqual(arm["calibration_version"], coord.calibration_version)
        self.assertEqual(len(arm["arm"]["positions"]), 5)
        self.assertEqual(len(arm["pre_clip_rad"]), 5)
        self.assertTrue(arm["leader_rad"])
        self.assertGreater(arm["ttl_s"], 0.0)
        gripper = gateway.payloads["/control/gripper"][0]
        self.assertGreater(gripper["seq"], arm["seq"], "gripper must use a later seq than the arm")
        self.stop(coord)

    def test_gateway_failure_demotes_and_clears_targets(self):
        coord, gateway, _ = build(self.dir)
        self.acquire(coord)
        coord.set_state("enter_preview")
        coord.align()
        coord.set_state("arm")
        coord.set_state("engage")
        self.assertEqual(coord.machine.state, "following")
        gateway.fail_paths = {"/control/arm"}
        coord.send_once()
        self.assertEqual(coord.machine.state, "paused")
        self.assertFalse(coord.machine.aligned, "reconnect must require a fresh alignment")
        self.assertIsNone(coord.machine.alignment)
        gateway.fail_paths = set()
        with self.assertRaises(BridgeError) as ctx:
            coord.set_state("engage")
        self.assertIn("realign_required_after_release", ctx.exception.detail)

    def engage(self, coord):
        self.acquire(coord)
        coord.set_state("enter_preview")
        coord.align()
        coord.set_state("arm")
        coord.set_state("engage")
        self.assertEqual(coord.machine.state, "following")
        coord.stop_follow_loop()   # park the thread the engage started; the tests drive cycles here

    def test_a_mid_stream_send_failure_parks_the_thread_it_runs_in(self):
        # The follow loop parks itself through demote_to_paused -> stop_follow_loop, which used to
        # join its own thread. The RuntimeError aborted that first pass before the reason was ever
        # written, so on the real car the page showed the loop's wrapper error instead of
        # "arm_send_failed" and nominally stayed in `following`.
        coord, gateway, _ = build(self.dir)
        self.engage(coord)
        coord.follow_thread = threading.current_thread()
        gateway.fail_paths = {"/control/arm"}
        coord.send_once()
        self.assertIsNone(coord.follow_thread, "a paused session must not keep polling the car")
        self.assertEqual(coord.machine.state, "paused")
        self.assertTrue(str(coord.last_error).startswith("arm_send_failed:"), coord.last_error)

    def test_a_lost_gate_mid_follow_names_the_gate_and_never_resumes_by_itself(self):
        # 主臂拔线/读失败 is the vector that used to fail silently: the gate closed, send_once
        # returned None, and nothing said why or stopped the thread from polling at command_hz.
        leader = FakeLeader()
        coord, gateway, _ = build(self.dir, leader=leader)
        self.engage(coord)
        coord.follow_thread = threading.current_thread()
        sent_before = len(gateway.payloads.get("/control/arm", []))
        leader.healthy = False
        self.assertIsNone(coord.send_once())
        self.assertEqual(coord.machine.state, "paused")
        self.assertIn("leader_unhealthy", str(coord.last_error))
        self.assertIsNone(coord.machine.alignment, "the stale reference must not survive the drop")
        self.assertIsNone(coord.follow_thread)
        self.assertEqual(len(gateway.payloads.get("/control/arm", [])), sent_before,
                         "no target may reach the car after the leader reading failed")
        leader.healthy = True
        with self.assertRaises(BridgeError) as ctx:
            coord.set_state("engage")
        self.assertIn("realign_required_after_release", ctx.exception.detail)

    def test_an_older_follow_loop_generation_sends_nothing(self):
        # A loop that parked itself returns without joining, so it can still be sleeping when the
        # operator re-engages and a second loop starts. Two threads driving the arm at once would
        # double the command rate, so only the newest generation may send.
        coord, gateway, _ = build(self.dir)
        self.engage(coord)
        coord.stop_follow_loop()
        coord._stop.clear()                              # what a re-engage does
        coord._loop_generation += 1
        calls_before = len(gateway.calls)
        watchdog = threading.Timer(1.0, coord.stop_follow_loop)   # bound the assertion below
        watchdog.daemon = True
        watchdog.start()
        coord._follow_loop(coord._loop_generation - 1)   # the stale loop wakes up
        watchdog.cancel()
        self.assertEqual([c[0] for c in gateway.calls[calls_before:]], [],
                         "a superseded follow loop must not touch the car again")

    def test_an_upper_lease_that_expires_mid_follow_exits_the_stream(self):
        # 六 说"任一失效则退出跟随"，租约过期这一路必须在跟随中被发现，而不是只在 acquire 时挡一次。
        lease = FakeLease()
        coord, gateway, _ = build(self.dir, lease=lease)
        self.engage(coord)
        coord.follow_thread = threading.current_thread()
        sent_before = len(gateway.payloads.get("/control/arm", []))
        lease.active = False                      # 看门狗把租约判过期
        self.assertIsNone(coord.send_once())
        self.assertEqual(coord.machine.state, "paused")
        self.assertIn("upper_lease_invalid", str(coord.last_error))
        self.assertIsNone(coord.machine.alignment)
        self.assertIsNone(coord.follow_thread, "an expired lease must also stop the send stream")
        self.assertEqual(len(gateway.payloads.get("/control/arm", [])), sent_before,
                         "an expired upper lease must not keep driving the arm")

    def test_emergency_stop_reaches_the_car_before_reporting(self):
        coord, gateway, _ = build(self.dir)
        self.acquire(coord)
        coord.set_state("enter_preview")
        coord.align()
        coord.set_state("arm")
        coord.set_state("engage")
        coord.recording("start_session", {})
        coord.recording("start_episode", {})
        out = coord.emergency_stop("test")
        self.assertEqual(out["state"], "estop")
        self.assertTrue(out["sent_estop"])
        self.assertEqual(gateway.payloads["/control/estop"][0]["reason"], "test")
        self.assertIsNone(coord.follow_thread)
        self.assertEqual([c[0] for c in gateway.calls if c[0].startswith("/recording/")],
                         ["/recording/start_session", "/recording/start_episode"],
                         "an estop must not wait for or trigger bag finalisation")

    def test_recording_joins_the_existing_session_without_a_second_lease(self):
        coord, gateway, _ = build(self.dir)
        lease = coord.leases
        self.acquire(coord)
        out = coord.recording("start_session", {"task": "teleop"})
        self.assertEqual(out["session_id"], "ros_session")
        self.assertEqual(lease.acquired, 1, "recording must not take its own lease")
        self.assertIn("/recording/start_session", [c[0] for c in gateway.calls])
        self.assertEqual([c[2] for c in gateway.calls if c[0] == "/recording/start_session"],
                         ["car-token"])

    def test_the_manual_episode_label_reaches_the_car_untouched(self):
        """需求七: the operator's verdict must be recorded on the car, not only shown in the browser."""
        coord, gateway, _ = build(self.dir)
        self.acquire(coord)
        coord.recording("end_episode", {"success": False, "note": "dropped the cube"})
        self.assertEqual(gateway.payloads["/recording/end_episode"][0],
                         {"success": False, "note": "dropped the cube"})

    def test_ending_an_episode_parks_the_stream_even_without_a_pause_click(self):
        # 七 的"结束本集后默认暂停跟随"必须是协调器的保证，而不是页面先替用户点一次暂停：
        # 少发或漏发那次 pause 调用，就会留下"没有开集却仍在跟随"的手臂。
        coord, gateway, _ = build(self.dir)
        self.engage(coord)
        coord.recording("start_session", {})
        coord.recording("start_episode", {})
        coord.follow_thread = threading.current_thread()
        coord.recording("end_episode", {"success": True})
        self.assertEqual(coord.machine.state, "paused")
        self.assertFalse(coord.machine.aligned, "the next engagement needs a fresh reference")
        self.assertIsNone(coord.follow_thread, "parking must stop the send stream")
        self.assertIsNone(coord.recording_episode)
        with self.assertRaises(BridgeError) as ctx:
            coord.set_state("engage")
        self.assertIn("realign_required_after_release", ctx.exception.detail)

    def test_ending_episode_stops_following_before_recorder_io_even_if_it_fails(self):
        coord, gateway, _ = build(self.dir)
        self.engage(coord)
        coord.recording("start_session", {})
        coord.recording("start_episode", {})
        original_transport = coord._transport

        def failing_end(path, method, payload, token):
            if path == "/recording/end_episode":
                self.assertEqual(coord.machine.state, "paused")
                self.assertIsNone(coord.follow_thread)
                raise BridgeError(503, "recorder unavailable")
            return original_transport(path, method, payload, token)

        coord._transport = failing_end
        with self.assertRaises(BridgeError):
            coord.recording("end_episode", {})
        self.assertEqual(coord.machine.state, "paused")

    def test_recording_refuses_without_a_teleop_session(self):
        coord, gateway, _ = build(self.dir)
        with self.assertRaises(BridgeError) as ctx:
            coord.recording("start_session", {})
        self.assertIn("teleop session is not acquired", ctx.exception.detail)

    def test_mapper_is_rebuilt_from_the_stored_mapping_not_the_profile(self):
        """A stale profile copy must never steer the arm once a mapping layer exists."""
        coord, gateway, identities = build(self.dir)
        store = coord.store
        stored = store.load("mapping", identities["mapping"]).data
        stored["joints"][0].update({"scale": 2.0, "direction": -1})
        follower = store.load("follower", identities["follower"]).data
        follower["limits_rad"] = {j: [-1.0, 1.0] for j in ARM}
        follower["soft_limits_rad"] = {"joint1": [-0.5, 0.5]}
        store.save("mapping", identities["mapping"], stored)
        store.save("follower", identities["follower"], follower)
        self.acquire(coord)
        self.assertEqual(coord.mapper.joints["joint1"]["scale"], 2.0,
                         "profile scale 1.0 must lose to the stored mapping")
        self.assertEqual(coord.mapper.joints["joint1"]["soft_min_rad"], -0.5,
                         "the follower layer owns the safe envelope the mapping may only narrow")
        self.assertEqual(coord.limits["joint1"], [-1.0, 1.0], "profile limits must lose")

    def test_acquire_refuses_when_a_mapped_joint_has_no_limits(self):
        coord, gateway, identities = build(self.dir)
        store = coord.store
        follower = store.load("follower", identities["follower"]).data
        follower["limits_rad"] = {"joint1": [-1.0, 1.0]}
        store.save("follower", identities["follower"], follower)
        coord.profile["limits"]["joint_positions_rad"] = {}
        with self.assertRaises(BridgeError) as ctx:
            self.acquire(coord)
        self.assertIn("no limits_rad", ctx.exception.detail)
        self.assertEqual(coord.leases.acquired, 0, "no lease may be taken for an unbounded mapping")

    def test_acquire_refuses_a_mapping_without_a_gripper_section(self):
        coord, gateway, identities = build(self.dir)
        store = coord.store
        stored = store.load("mapping", identities["mapping"]).data
        stored.pop("gripper")
        store.save("mapping", identities["mapping"], stored)
        with self.assertRaises(BridgeError) as ctx:
            self.acquire(coord)
        self.assertIn("no gripper section", ctx.exception.detail)
        self.assertEqual(coord.leases.acquired, 0)

    def test_acquire_refuses_without_a_live_leader_and_takes_no_lease(self):
        """No half-open session: both leases stay free when the leader cannot be read."""
        coord, gateway, _ = build(self.dir)
        coord.leader = FakeLeader(connected=False, healthy=False)
        with self.assertRaises(BridgeError) as ctx:
            self.acquire(coord)
        self.assertIn("leader not ready", ctx.exception.detail)
        self.assertEqual(coord.leases.acquired, 0)
        self.assertIsNone(coord.token)
        self.assertNotIn("/control/acquire", [c[0] for c in gateway.calls])

    def test_acquire_refuses_when_no_leader_reader_is_configured(self):
        coord, gateway, _ = build(self.dir)
        coord.leader = None
        with self.assertRaises(BridgeError) as ctx:
            self.acquire(coord)
        self.assertIn("leader reader is not configured", ctx.exception.detail)
        self.assertEqual(coord.leases.acquired, 0)

    def test_release_rolls_back_both_sides(self):
        coord, gateway, _ = build(self.dir)
        self.acquire(coord)
        out = coord.release("user")
        self.assertTrue(out["released"])
        self.assertEqual(coord.token, None)
        self.assertIsNone(coord.upper_token)
        self.assertIn("/control/release", [c[0] for c in gateway.calls])
        self.assertEqual(coord.leases.released[-1], "user")
        self.assertEqual(coord.machine.state, "disconnected")

    def test_send_once_reads_the_car_status_once_per_control_cycle(self):
        """At command_hz, extra /status reads per cycle are what makes the tunnel time out."""
        coord, gateway, _ = build(self.dir)
        self.acquire(coord)
        coord.set_state("enter_preview")
        coord.align()
        coord.set_state("arm")
        coord.set_state("engage")
        # engage starts the real 20 Hz loop; park it so the count below is this explicit cycle's
        # reads and not this cycle plus whatever the thread happened to poll in between.
        coord.stop_follow_loop()
        before = len([c for c in gateway.calls if c[0] == "/status"])
        coord.send_once()
        reads = len([c for c in gateway.calls if c[0] == "/status"]) - before
        self.assertEqual(reads, 1, f"one control cycle must read the car status once, got {reads}")

    def test_tunnel_down_makes_every_gate_fail(self):
        coord, gateway, _ = build(self.dir)
        self.acquire(coord)
        coord.set_state("enter_preview")
        coord.align()
        coord.set_state("arm")
        gateway.fail_paths = {"/status"}
        gate = coord._gate()
        self.assertEqual(sorted(gate.missing("following")),
                         ["car_lease_invalid", "control_not_exclusive",
                          "follower_feedback_missing_or_stale"])
        self.assertEqual(coord.machine.state, "armed", "gate checks alone must not move the machine")
        with self.assertRaises(BridgeError):
            coord.set_state("engage")

    def stop(self, coord):
        coord.stop_follow_loop()


class DeployedStatusShapeTests(unittest.TestCase):
    """The real agent nests follower_feedback and teleop inside `control`.

    The rest of this file historically used a flat fake, which is why reading those fields only at
    the top level passed every test yet left every gate closed on the car.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="teleop_coord_nested_")

    def acquire(self, coord):
        return coord.acquire("operator-a", "leader_A", "car_A", "map_A")

    def test_gate_reports_leases_and_feedback_valid_from_the_nested_block(self):
        coord, _, _ = build(self.dir, shape="nested")
        self.acquire(coord)
        gate = coord._gate()
        self.assertTrue(gate.car_lease_valid)
        self.assertTrue(gate.exclusive_control)
        self.assertTrue(gate.upper_lease_valid)
        self.assertTrue(gate.follower_feedback_valid)
        self.assertTrue(gate.chassis_locked)
        self.assertEqual(gate.missing("following"), [])

    def test_preview_reads_follower_feedback_from_the_nested_block(self):
        coord, gateway, _ = build(self.dir, shape="nested")
        self.acquire(coord)
        out = coord.preview_step()
        self.assertFalse(out["outputs_actuators"])
        self.assertEqual(sorted(out["feedback"]["positions_rad"]), sorted(ARM))
        self.assertNotIn("follower_feedback_missing_or_stale", out["gate_missing"])
        self.assertEqual(len([c for c in gateway.calls if c[1] == "POST"
                             and c[0] in ("/control/arm", "/control/gripper")]), 0)

    def test_preview_surfaces_the_cars_own_arm_stream_window(self):
        """The operator must see 'the car stopped receiving targets', not only 'my page is sending'."""
        coord, _, _ = build(self.dir, shape="nested")
        self.acquire(coord)
        self.assertEqual(coord.preview_step()["arm_stream"]["live"], True)
        coord2, _, _ = build(self.dir, shape="nested", feedback_available=False)
        self.acquire(coord2)
        expired = coord2.preview_step()["arm_stream"]
        self.assertFalse(expired["live"])
        self.assertLess(expired["remaining_s"], 0.0)

    def test_align_arm_engage_sequence_opens_on_the_nested_block(self):
        coord, _, _ = build(self.dir, shape="nested")
        self.acquire(coord)
        coord.set_state("enter_preview")
        coord.align()
        coord.set_state("arm")
        coord.set_state("engage")
        self.assertEqual(coord.machine.state, "following")

    def test_stale_nested_feedback_still_blocks_following(self):
        coord, _, _ = build(self.dir, shape="nested", feedback_available=False)
        self.acquire(coord)
        coord.set_state("enter_preview")
        with self.assertRaises(BridgeError) as ctx:
            coord.align()
        self.assertIn("follower_feedback_missing_or_stale", ctx.exception.detail)

    def test_status_carries_the_cars_own_exclusivity_verdict(self):
        """The page shows what the car concluded about vendor publishers, not what we assume."""
        coord, _, _ = build(self.dir, shape="nested")
        self.acquire(coord)
        coord._gate()
        self.assertTrue(coord.status()["car_exclusivity"]["clean"])

    def test_status_carries_the_cars_declared_hold_policy(self):
        """需求五: what "stopping" means must come from the car, not from our assumption."""
        coord, _, _ = build(self.dir, shape="nested")
        self.acquire(coord)
        coord._gate()
        self.assertIn("torque is never disabled", coord.status()["car_hold_policy"])

    def test_status_reports_no_exclusivity_check_before_the_car_has_observed_one(self):
        coord, _, _ = build(self.dir, shape="nested")
        self.assertIsNone(coord.status()["car_exclusivity"], "nothing observed from the car yet")


class ExclusivityRefusalTests(unittest.TestCase):
    """需求六: enable is refused unless exclusive control is real, and the refusal must roll back."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="teleop_coord_exclusive_")

    def test_a_car_refusal_releases_the_upper_lease_and_keeps_the_reason(self):
        coord, gateway, _ = build(self.dir, shape="nested")
        refuse = BridgeError(409, "vendor_publisher_present: exclusive arm control unavailable: "
                                  "{'/lelab/arm/trajectory_request': ['joy_to_motor']}")

        def transport(path, method="GET", payload=None, token=None):
            if path == "/control/mode":
                raise refuse
            return gateway(path, method, payload, token)

        coord._transport = transport
        with self.assertRaises(BridgeError) as ctx:
            coord.acquire("operator-a", "leader_A", "car_A", "map_A")
        self.assertIs(ctx.exception, refuse, "the car's own reason must reach the operator")
        self.assertIn("car-acquire-failed", coord.leases.released)
        self.assertIsNone(coord.token)
        self.assertIsNone(coord.upper_token)
        self.assertEqual(coord.machine.state, "disconnected")


class UpperExitTests(unittest.TestCase):
    """需求六/七: the upper host leaving must stop commanding, close the bag and drop both leases."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="teleop_coord_exit_")

    def live_session(self, **kwargs):
        coord, gateway, _ = build(self.dir, **kwargs)
        coord.acquire("operator-a", "leader_A", "car_A", "map_A")
        coord.recording("start_session", {})
        coord.recording("start_episode", {})
        return coord, gateway

    def test_exit_closes_the_bag_before_releasing_the_car_lease(self):
        import threading

        coord, gateway = self.live_session()
        coord.follow_thread = threading.Thread(target=lambda: None, daemon=True)
        coord.follow_thread.start()
        gateway.calls.clear()
        out = coord.shutdown(reason="server-exit")

        paths = [call[0] for call in gateway.calls]
        self.assertEqual(paths.count("/control/arm"), 0, "exiting must not send another target")
        self.assertLess(paths.index("/recording/end_episode"), paths.index("/control/release"),
                        "the follow stream and the bag close before the car lease is let go")
        self.assertLess(paths.index("/recording/stop_session"), paths.index("/control/release"))
        self.assertIsNone(coord.follow_thread)
        self.assertIsNone(coord.token)
        self.assertIsNone(coord.upper_token)
        self.assertFalse(coord.is_live())
        self.assertTrue(out["released"])

    def test_a_dead_tunnel_still_drops_the_local_session(self):
        coord, gateway = self.live_session()
        gateway.fail_paths = {"/recording/end_episode", "/recording/stop_session", "/control/release"}
        out = coord.shutdown(reason="server-exit")
        self.assertIn("recording_close_error", out, "the operator must learn we did not close the bag")
        self.assertTrue(out["released"])
        self.assertIsNone(coord.token, "the car lease expires on its own; the local session must not linger")

    def test_is_live_only_reports_a_real_session(self):
        coord, _, _ = build(self.dir)
        self.assertFalse(coord.is_live(), "an untouched coordinator is not a held lease")
        coord.upper_token = "upper"
        self.assertTrue(coord.is_live())


class LeaseContentionTests(unittest.TestCase):
    """需求六: calibration, desktop teleop, car teleop and policy control are mutually exclusive.

    All four ask the same single-slot upper lease; these tests pin both halves of that claim —
    the car is never touched while someone else holds it, and the real manager really has one slot.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="teleop_coord_contend_")

    def test_an_active_policy_session_blocks_the_car_before_any_gateway_traffic(self):
        from lelab.hardware_lease import HardwareLeaseError

        class HeldByPolicy:
            def acquire(self, task, *, owner, role="operator", ttl_seconds=None, note=None):
                raise HardwareLeaseError("inference is holding the hardware lease (owner: policy-A)",
                                         status_code=409)

            def release(self, token=None, reason=None):
                raise AssertionError("we must not release a lease we never obtained")

        coord, gateway, _ = build(self.dir, lease=HeldByPolicy())
        with self.assertRaises(BridgeError) as ctx:
            coord.acquire("operator-a", "leader_A", "car_A", "map_A")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("inference", ctx.exception.detail, "the page must show who is in front")
        self.assertEqual(gateway.calls, [], "a busy upper lease means no traffic to the car at all")

    def test_the_real_lease_manager_gives_the_car_session_a_single_slot(self):
        from lelab.hardware_lease import HardwareLeaseError, HardwareLeaseManager

        manager = HardwareLeaseManager(clock=lambda: 1000.0)
        policy = manager.acquire("inference", owner="policy-A")
        with self.assertRaises(HardwareLeaseError) as ctx:
            manager.acquire(UPPER_LEASE_TASK, owner="operator-1")
        self.assertIn("inference", str(ctx.exception), "the refusal must name the current holder")
        manager.release(token=policy.token, reason="policy-stopped")
        self.assertTrue(manager.acquire(UPPER_LEASE_TASK, owner="operator-1").token,
                        "the same single slot becomes available once policy control releases")


class LeaderCalibrationBindingTests(unittest.TestCase):
    """Opening the leader serial has to refuse a calibration that belongs to the other arm.

    Layer A can be complete - all six motors, all five fields - and still be the *left* arm's
    numbers while the operator plugged the *right* one in. The readings would look smooth and
    plausible. The motor ids the bus polls are the only thing in layer A that says which arm this
    actually is, so that is what the open path checks.
    """

    LEROBOT_CALIB = {name: {"id": index + 1, "drive_mode": 0, "homing_offset": 0,
                           "range_min": 0, "range_max": 4095}
                     for index, name in enumerate(LEADER + ["gripper"])}

    class StubBus:
        def connect(self):
            pass

        def disconnect(self, disable_torque=True):
            pass

        def sync_read(self, model, ids, num_retry=0):
            return {name: 0.0 for name in ids}

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="coord-leader-bind-")

    def coord(self):
        import lelab.mobile_teleop_coordinator as coord_module

        store = CalibrationStore(Path(self.dir))
        identities = seed_calibration(store)
        store.save("leader", identities["leader"],
                   {"joint_order": LEADER, "device_identity": {"serial": "5B91045575"},
                    "lerobot_calibration": dict(self.LEROBOT_CALIB)})
        coord = MobileTeleopCoordinator(
            CoordinatorConfig(agent_url="http://car.invalid", command_hz=20.0),
            profile(), store=store, transport=FakeGateway(), lease_manager=FakeLease())
        coord.identities = identities
        return coord, coord_module

    def test_another_arm_s_ids_are_named_and_no_reader_is_created(self):
        import lelab.mobile_teleop_coordinator as coord_module

        coord, _ = self.coord()
        other_arm = {**self.LEROBOT_CALIB,
                     "shoulder_pan": {**self.LEROBOT_CALIB["shoulder_pan"], "id": 7}}
        coord.store.save("leader", coord.identities["leader"],
                         {"joint_order": LEADER, "device_identity": {"serial": "5B91045575"},
                          "lerobot_calibration": other_arm})
        with unittest.mock.patch.object(coord_module, "build_bus",
                                        side_effect=AssertionError("must not build a bus")):
            with self.assertRaises(BridgeError) as ctx:
                coord.open_leader("/dev/ttyACM0", leader_identity=coord.identities["leader"])
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("leader_calibration_does_not_match_this_arm", ctx.exception.detail)
        self.assertIn("shoulder_pan", ctx.exception.detail)
        self.assertIn("bus_id=1", ctx.exception.detail)
        self.assertIsNone(coord.leader, "a refused open must leave no half-built reader")

    def test_a_missing_layer_a_calibration_is_refused_before_any_serial_traffic(self):
        import lelab.mobile_teleop_coordinator as coord_module

        coord, _ = self.coord()
        coord.store.save("leader", coord.identities["leader"],
                         {"joint_order": LEADER, "device_identity": {"serial": "5B91045575"}})
        with unittest.mock.patch.object(coord_module, "build_bus",
                                        side_effect=AssertionError("must not build a bus")):
            with self.assertRaises(BridgeError) as ctx:
                coord.open_leader("/dev/ttyACM0", leader_identity=coord.identities["leader"])
        self.assertIn("leader_calibration_missing", ctx.exception.detail)
        self.assertEqual(coord.leader, None)

    def test_the_matching_calibration_is_handed_to_the_bus_unchanged(self):
        """Reuse means the exact numbers the SO-101 wizard produced, not a re-derived copy."""
        import lelab.mobile_teleop_coordinator as coord_module

        coord, _ = self.coord()
        identity = coord.identities["leader"]
        record = coord.store.load("leader", identity).data
        record["device_identity"] = {
            "serial": "5B90149652",
            "usb_by_id": "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149652-if00",
        }
        coord.store.save("leader", identity, record)
        seen: list = []

        def factory(config, calibration):
            seen.append((config, calibration))
            return self.StubBus()

        with unittest.mock.patch.object(coord_module, "build_bus", side_effect=factory):
            coord.open_leader("/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149652-if00",
                              leader_identity=coord.identities["leader"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], self.LEROBOT_CALIB)
        self.assertTrue(coord.leader.connected)
        coord.close_leader()

    def test_different_usb_serial_is_refused_before_the_bus_is_opened(self):
        coord, coord_module = self.coord()
        identity = coord.identities["leader"]
        record = coord.store.load("leader", identity).data
        record["device_identity"]["usb_by_id"] = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B91045575-if00"
        coord.store.save("leader", identity, record)
        with unittest.mock.patch.object(coord_module, "build_bus", side_effect=AssertionError("bus opened")):
            with self.assertRaises(BridgeError) as ctx:
                coord.open_leader("/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149652-if00",
                                  leader_identity=identity)
        self.assertIn("leader_device_identity_mismatch", ctx.exception.detail)

    def test_changing_calibration_after_open_requires_reopening_before_acquire(self):
        coord, coord_module = self.coord()
        identity = coord.identities["leader"]
        port = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B91045575-if00"
        record = coord.store.load("leader", identity).data
        record["device_identity"]["usb_by_id"] = port
        coord.store.save("leader", identity, record)
        with unittest.mock.patch.object(coord_module, "build_bus", return_value=self.StubBus()):
            coord.open_leader(port, leader_identity=identity)
        record["note"] = "calibration changed"
        coord.store.save("leader", identity, record)
        with self.assertRaises(BridgeError) as ctx:
            coord.acquire("operator-a", identity, "car_A", "map_A")
        self.assertIn("close and reopen", ctx.exception.detail)
        self.assertEqual(coord.leases.acquired, 0)
        coord.close_leader()


if __name__ == "__main__":
    unittest.main()
