"""The standalone control process: what it emits, what it records and what it refuses.

These are the rules the frame transport exists to enforce, tested against the production
runtime class with only the ROS node, the servo driver and the readback faked.  SIMULATED
ONLY: no rclpy graph, no serial port, no hardware, no motion.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "ros2_ws" / "src" / "lelab_mobile_agent"), str(ROOT / "app_ros_mobile")]

HAS_RCLPY = os.environ.get("LELAB_FORCE_NO_RCLPY") != "1"
IMPORT_ERROR = ""
if HAS_RCLPY:
    try:
        from lelab_mobile_agent.car_control_process import CarControlRuntime  # noqa: E402
        from lelab_mobile_agent.licence_ipc import (LicenceError, LicenceMirror,  # noqa: E402
                                                    licence_record,
                                                    recording_context_record)  # noqa: E402
        from lelab.hybrid_channel import command_v1  # noqa: E402
        from lelab_mobile_agent.teleop_frame_v1 import frame_v1  # noqa: E402
        from lelab_mobile_agent.teleop_stream import token_digest  # noqa: E402
    except Exception as error:  # noqa: BLE001
        HAS_RCLPY = False
        IMPORT_ERROR = f"{type(error).__name__}: {str(error)[:200]}"

BUNDLE = "c" * 64
TOKEN = "a-real-lease-token-from-the-agent"
JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"]
SCHEMA = 1


class FakeTime:
    def __init__(self):
        self.nanoseconds = int(time.time() * 1e9)


class FakeClock:
    def now(self):
        return FakeTime()


class FakeNode:
    def create_timer(self, *_args, **_kwargs):
        return None

    def get_clock(self):
        return FakeClock()


class FakeReadback:
    def read_snapshot(self):
        return {"available": True,
                "positions_rad": {name: 0.0 for name in JOINTS + ["r_joint"]},
                "servo_pulses": [500, 500, 500, 500, 500, 500],
                "oldest_age_s": 0.05, "scan_span_s": 0.2, "last_error": None}


class FakeDriver:
    """Only what the runtime asks of it: enable, check, submit, stop, and the flags."""

    def __init__(self):
        self.outputs_enabled = False
        self.publish_count = 0
        self.stop_count = 0
        self.submitted = []
        self.torque_invalidations = 0

    def enable(self):
        self.outputs_enabled = True

    def invalidate_torque_verification(self):
        self.torque_invalidations += 1

    def check_only(self, name, value):
        if value != value:                      # NaN
            raise RuntimeError(f"nonfinite_{name}_position")
        return float(value)

    def submit_frame(self, targets):
        if not self.outputs_enabled:
            raise RuntimeError("outputs_disabled")
        self.submitted.append(list(targets))
        self.publish_count += 1

    def stop(self, reason):
        self.outputs_enabled = False
        self.stop_count += 1
        return {"stopped": True, "reason": reason, "hold_published": True,
                "driver_sent": True}


class RecordingPublisher:
    def __init__(self):
        self.rows = []

    def publish(self, message):
        self.rows.append(json.loads(message.data))


def test_profile():
    return {
        "robot": {"arm_joints": list(JOINTS), "gripper_joint": "r_joint"},
        "limits": {"joint_positions_rad": {name: [-3.0, 3.0] for name in JOINTS + ["r_joint"]}},
        "teleop": {"max_step_rad_s": 3.0, "output_watchdog_s": 1.0, "output_duration_s": 0.15},
        "follower_feedback": {"stale_after_s": 0.5, "max_scan_span_s": 0.5,
                              "scan_period_s": 0.2},
    }


@unittest.skipUnless(HAS_RCLPY, IMPORT_ERROR or "needs the Humble runtime")
class EntrypointLifecycle(unittest.TestCase):
    def test_main_builds_one_runtime_and_stops_it(self):
        from lelab_mobile_agent import car_control_process as module

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile.json"
            profile.write_text(json.dumps(test_profile()), encoding="utf-8")
            node, licence, server = mock.Mock(), mock.Mock(), mock.Mock()
            server.port = 18121
            with mock.patch.object(sys, "argv", ["car_control_process", "--profile", str(profile)]), \
                 mock.patch.dict(sys.modules, {"serial": None}), \
                 mock.patch.object(module, "build", return_value=(node, object(), licence, server)) as build, \
                 mock.patch.object(module.rclpy, "init"), \
                 mock.patch.object(module.rclpy, "spin", side_effect=KeyboardInterrupt), \
                 mock.patch.object(module.signal, "signal"):
                # The production process deliberately refuses any imported serial module.
                del sys.modules["serial"]
                self.assertEqual(module.main(), 0)
            self.assertEqual(build.call_count, 1)
            server.start.assert_called_once()
            server.stop.assert_called_once()
            licence.stop.assert_called_once()
            node.destroy_node.assert_called_once()

    def test_invalid_control_rate_starts_no_ros_runtime(self):
        from lelab_mobile_agent import car_control_process as module

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile.json"
            profile.write_text(json.dumps(test_profile()), encoding="utf-8")
            with mock.patch.object(sys, "argv", ["car_control_process", "--profile", str(profile),
                                               "--control-hz", "21"]), \
                 mock.patch.dict(sys.modules, {"serial": None}), \
                 mock.patch.object(module, "build") as build, \
                 mock.patch.object(module.rclpy, "init") as init:
                del sys.modules["serial"]
                self.assertEqual(module.main(), 2)
            build.assert_not_called()
            init.assert_not_called()


@unittest.skipUnless(HAS_RCLPY, IMPORT_ERROR or "needs the Humble runtime")
@unittest.skipUnless(HAS_RCLPY, IMPORT_ERROR or "needs Humble")
class HardwareTrialConfiguration(unittest.TestCase):
    def test_real_trial_refuses_wrong_identity_wiring_or_unlocked_axis(self):
        import argparse
        import copy
        from lelab_mobile_agent.car_control_process import validate_hardware_trial
        path = ROOT / "configs/ros2_collection/hardware-hybrid-arm-trial-v1.json"
        profile = json.loads(path.read_text())
        args = argparse.Namespace(
            data_identity="hardware", stream_host="127.0.0.1",
            output_topic="/ros_robot_controller/bus_servo/set_position",
            readback_topic="/lelab/ros_robot_controller/servo_readback",
            bundle_hash=profile["safety"]["calibration_bundle_hash"])
        validate_hardware_trial(profile, args)
        for field, bad in (("data_identity", "simulated_offline"),
                           ("stream_host", "0.0.0.0"),
                           ("output_topic", "/other"),
                           ("bundle_hash", "wrong")):
            altered = copy.copy(args)
            setattr(altered, field, bad)
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_hardware_trial(profile, altered)
        altered = copy.deepcopy(profile)
        altered["teleop"]["trial_motion"]["allowed_joints"] = ["joint1", "joint2"]
        with self.assertRaises(ValueError):
            validate_hardware_trial(altered, args)

    def test_second_axis_profile_cannot_enable_any_other_axis(self):
        import argparse
        import copy
        from lelab_mobile_agent.car_control_process import validate_hardware_trial
        profile = json.loads((ROOT / "configs/ros2_collection/hardware-hybrid-arm2-trial-v1.json").read_text())
        args = argparse.Namespace(data_identity="hardware", stream_host="127.0.0.1",
            output_topic="/ros_robot_controller/bus_servo/set_position",
            readback_topic="/lelab/ros_robot_controller/servo_readback",
            bundle_hash=profile["safety"]["calibration_bundle_hash"])
        validate_hardware_trial(profile, args)
        for axes in (["joint1"], ["joint1", "joint2"], [], ["r_joint"]):
            bad = copy.deepcopy(profile)
            bad["teleop"]["trial_motion"]["allowed_joints"] = axes
            with self.subTest(axes=axes), self.assertRaises(ValueError):
                validate_hardware_trial(bad, args)

    def test_remaining_joint_profiles_are_bound_to_exactly_one_named_axis(self):
        import argparse
        import copy
        from lelab_mobile_agent.car_control_process import validate_hardware_trial
        for axis in (3, 4, 5):
            profile = json.loads((ROOT / f"configs/ros2_collection/hardware-hybrid-arm{axis}-trial-v1.json").read_text())
            args = argparse.Namespace(data_identity="hardware", stream_host="127.0.0.1",
                output_topic="/ros_robot_controller/bus_servo/set_position",
                readback_topic="/lelab/ros_robot_controller/servo_readback",
                bundle_hash=profile["safety"]["calibration_bundle_hash"])
            validate_hardware_trial(profile, args)
            bad = copy.deepcopy(profile)
            bad["teleop"]["trial_motion"]["allowed_joints"] = ["joint1"]
            with self.subTest(axis=axis), self.assertRaises(ValueError):
                validate_hardware_trial(bad, args)

    def test_homing_profile_requires_exact_raw_pose_and_staged_bounds(self):
        import argparse
        import copy
        from lelab_mobile_agent.car_control_process import validate_hardware_trial
        profile = json.loads(
            (ROOT / "configs/ros2_collection/hardware-hybrid-homing-v1.json").read_text())
        args = argparse.Namespace(
            data_identity="hardware", stream_host="127.0.0.1",
            output_topic="/ros_robot_controller/bus_servo/set_position",
            readback_topic="/lelab/ros_robot_controller/servo_readback",
            state_topic="/ros_robot_controller/bus_servo/set_state",
            state_service="/ros_robot_controller/bus_servo/get_state",
            bundle_hash=profile["safety"]["calibration_bundle_hash"])
        validate_hardware_trial(profile, args)
        for mutate in (
                lambda p: p["teleop"]["homing_motion"]["targets_raw"].update(r_joint=349),
                lambda p: p["teleop"]["homing_motion"].update(max_step_raw=21),
                lambda p: p["teleop"]["homing_motion"].update(tolerance_raw=3),
                lambda p: p["teleop"].update(chassis_locked=False)):
            bad = copy.deepcopy(profile)
            mutate(bad)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                validate_hardware_trial(bad, args)
        for field, bad in (("state_topic", "/lelab_offline/ros_robot_controller/bus_servo/set_state"),
                           ("state_service", "/other/get_state")):
            altered = copy.copy(args)
            setattr(altered, field, bad)
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_hardware_trial(profile, altered)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        # The mirror is real, and its socket is never opened: apply() is how the agent's
        # publisher would feed it, so the tests exercise the same state machine.
        self.mirror = LicenceMirror(base / "licence.sock")
        self.driver = FakeDriver()
        self.readback = FakeReadback()
        self.recording = RecordingPublisher()
        self.runtime = CarControlRuntime(
            FakeNode(), profile=test_profile(), control_hz=10.0, window_ms=250,
            licence=self.mirror, readback=self.readback, driver=self.driver,
            data_identity="simulated_offline", status_path=base / "status.json",
            bundle_hash=BUNDLE, max_ttl_ms=200, recording_publisher=self.recording)

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------------
    def licence(self, generation=1, outputs=True, session_id="sess-1", session_epoch=77,
                token=TOKEN):
        self.mirror.apply(licence_record(
            control_generation=generation, session_id=session_id,
            session_epoch=session_epoch, calibration_bundle_hash=BUNDLE,
            permits={"arm": True, "gripper": True}, outputs_enabled=outputs,
            token_sha256=token_digest(token), ttl_ms=300))

    def context(self, seq=1, episode="ep-1", phase="observe", clock_epoch="clock-abc",
                session_id="sess-9", session_epoch=99):
        self.mirror.apply(recording_context_record(
            session_id=session_id, session_epoch=session_epoch, episode_id=episode,
            phase=phase, clock_epoch=clock_epoch, calibration_bundle_hash=BUNDLE,
            context_seq=seq, ttl_ms=1000))

    def clear(self, generation):
        self.mirror.apply({"schema_version": SCHEMA, "type": "clear_estop",
                           "control_generation": generation,
                           "reason": "operator_cleared_estop"})

    def hello(self, token=TOKEN, session_id="sess-1", session_epoch=77):
        return self.runtime.on_hello(session_id, session_epoch, token)

    def frame(self, frame_seq=1, sample_seq=None, positions=(0.1, 0.2, 0.3, 0.4, 0.5),
              gripper=0.02):
        window = self.runtime.windows.issue(duration_ms=self.runtime.window_ms)
        sample_seq = frame_seq if sample_seq is None else sample_seq
        common = dict(session_id="sess-1", session_epoch=77, source="so101_leader",
                      sample_seq=sample_seq, ttl_ms=150, calibration_bundle_hash=BUNDLE,
                      clutch=True)
        # The upper side fills the leader and pre-clip values next to the target it
        # actually sends; the row has to keep all three so a clip can be traced back.
        arm = command_v1(channel="arm", seq=frame_seq,
                         target={"joint_names": JOINTS, "positions_rad": list(positions),
                                 "leader_rad": [value + 0.01 for value in positions],
                                 "pre_clip_rad": [value + 0.02 for value in positions]},
                         **common)
        gripper_command = command_v1(channel="gripper", seq=frame_seq,
                                     target={"joint_name": "r_joint",
                                             "position_rad": gripper}, **common)
        return frame_v1(session_id="sess-1", session_epoch=77, calibration_bundle_hash=BUNDLE,
                        frame_seq=frame_seq, stream_epoch=window["stream_epoch"],
                        sample_seq=sample_seq, freshness_window_id=window["window_id"],
                        commands={"arm": arm, "gripper": gripper_command})

    def emit_one(self, frame_seq=1):
        """Admit a frame and run one tick, the way the stream and the timer would."""
        receipt = self.runtime._on_frame_locked(self.frame(frame_seq=frame_seq))
        self.runtime._tick_locked()
        return receipt


class ContextedRows(Fixture):
    def test_a_recorded_row_names_the_episode_the_agent_published(self):
        self.licence()
        self.context(episode="ep-7", phase="grasp", clock_epoch="epoch-xyz")
        self.assertEqual(self.hello()[0], True)
        receipt = self.emit_one()
        self.assertEqual(receipt["state"], "admitted", receipt)
        row = self.recording.rows[-1]
        self.assertEqual(row["episode_id"], "ep-7")
        self.assertEqual(row["phase"], "grasp")
        self.assertEqual(row["clock_epoch"], "epoch-xyz")
        self.assertEqual(row["recording_session_id"], "sess-9")
        self.assertEqual(row["recording_context"], "agent")
        self.assertEqual(row["data_identity"], "simulated_offline")
        self.assertEqual(self.runtime.counters["contexted_rows"], 1)

    def test_a_row_without_a_context_says_so_instead_of_inventing_one(self):
        self.licence()
        self.hello()
        self.emit_one()
        row = self.recording.rows[-1]
        self.assertIsNone(row["episode_id"])
        self.assertIsNone(row["phase"])
        self.assertEqual(row["recording_context"], "missing")
        self.assertEqual(self.runtime.counters["uncontexted_rows"], 0 + 1)
        self.assertEqual(self.runtime.counters["contexted_rows"], 0)

    def test_the_expired_context_stops_being_stamped(self):
        self.licence()
        self.context(seq=1)
        self.hello()
        self.emit_one(frame_seq=1)
        self.assertEqual(self.recording.rows[-1]["episode_id"], "ep-1")
        # A clock advanced past the context TTL: the mirror drops it, and the next row has
        # to admit it has no episode rather than reuse the one it saw a moment ago.
        self.mirror._context["deadline_ns"] = self.mirror._clock() - 1
        self.emit_one(frame_seq=2)
        self.assertIsNone(self.recording.rows[-1]["episode_id"])
        self.assertEqual(self.recording.rows[-1]["recording_context"], "missing")

    def test_the_two_clocks_travel_together_and_are_never_one_number(self):
        self.licence()
        self.context()
        self.hello()
        self.emit_one()
        row = self.recording.rows[-1]
        # Monotonic nanoseconds since boot and wall-clock nanoseconds differ by orders of
        # magnitude, so a comparison between the two would be obviously wrong on the page.
        self.assertLess(row["control_mono_ns"], row["stamp_ns"])
        self.assertEqual(row["control_clock_domain"], "car_process_monotonic")
        # The recorded stamp is declared in the domain the recorder and the bag use, which
        # is what makes a row joinable to a camera frame or a phase event.
        self.assertEqual(row["clock_domain"], "car_system_time")

    def test_a_frame_keeps_its_leader_preclip_sent_target_and_readback_apart(self):
        self.licence()
        self.context()
        self.hello()
        self.emit_one(frame_seq=42)
        row = self.recording.rows[-1]
        self.assertEqual(row["frame_seq"], 42)
        self.assertEqual(row["sample_seq"], 42)
        self.assertEqual(len(row["leader_rad"]), 5)
        self.assertEqual(len(row["pre_clip_rad"]), 5)
        # The three values must stay distinguishable, or the row cannot show what was
        # actually clipped on the way to the sent target.
        self.assertNotEqual(row["pre_clip_rad"][0], row["action_rad"]["joint1"])
        self.assertNotEqual(row["leader_rad"][0], row["action_rad"]["joint1"])
        self.assertNotEqual(row["state_rad"]["joint1"], row["action_rad"]["joint1"])
        self.assertEqual(len(row["action_rad"]), 6)
        self.assertEqual(len(row["state_rad"]), 6)
        self.assertEqual(row["state_source"], "servo_position_readback")
        self.assertEqual(row["confirmation"], "driver_sent")
        self.assertEqual(row["controller_acknowledged"], "unavailable")
        self.assertEqual(row["execution_observed"], "unavailable")
        self.assertEqual(row["hardware_effect"], "unverified")

    def test_a_refused_frame_records_no_row_at_all(self):
        self.licence(outputs=False)
        self.context()
        self.hello()
        receipt = self.runtime._on_frame_locked(self.frame(frame_seq=3))
        self.runtime._tick_locked()
        self.assertEqual(receipt["state"], "refused")
        self.assertIn("licence_outputs_disabled", str(receipt.get("detail", "")))
        self.assertEqual(self.recording.rows, [])


class LatchRules(Fixture):
    def test_an_emergency_stop_disables_output_and_refuses_the_next_frame(self):
        self.licence()
        self.hello()
        self.emit_one(frame_seq=1)
        self.assertTrue(self.driver.outputs_enabled)
        receipt = self.runtime._preempt_locked("estop", "rehearsal")
        self.assertTrue(receipt["hold_published"])
        self.assertFalse(self.driver.outputs_enabled)
        refused = self.runtime._on_frame_locked(self.frame(frame_seq=5))
        self.assertEqual(refused["state"], "refused")
        self.assertIn("fault_latched", refused["reason"])

    def test_a_newer_licence_generation_alone_never_lifts_the_latch(self):
        self.licence(generation=1)
        self.hello()
        self.emit_one(frame_seq=1)
        self.runtime._preempt_locked("estop", "rehearsal")
        emitted = self.runtime.counters["frames_emitted"]
        for generation in (2, 3, 4):
            self.licence(generation=generation)
            for _ in range(3):
                self.runtime._tick_locked()
        self.assertEqual(self.runtime.latched_fault, "estop")
        self.assertFalse(self.driver.outputs_enabled)
        self.assertEqual(self.runtime.counters["frames_emitted"], emitted,
                         "renewing the licence must not resume motion")

    def test_a_reconnect_does_not_lift_the_latch(self):
        self.licence(generation=1)
        self.hello()
        self.emit_one(frame_seq=1)
        self.runtime._preempt_locked("estop", "rehearsal")
        ok, reason = self.hello()
        self.assertFalse(ok)
        self.assertIn("fault_latched", str(reason))
        self.assertEqual(self.runtime.latched_fault, "estop")

    def test_an_explicit_clear_lifts_the_latch_but_grants_nothing(self):
        self.licence(generation=1)
        self.hello()
        self.emit_one(frame_seq=1)
        self.runtime._preempt_locked("estop", "rehearsal")
        self.clear(generation=2)
        self.runtime._tick_locked()
        self.assertIsNone(self.runtime.latched_fault)
        self.assertTrue(self.runtime.reengage_required)
        self.assertFalse(self.driver.outputs_enabled)
        # The clearance consumed the frame that was buffered before the stop.
        self.assertEqual(self.runtime.counters["frames_emitted"], 1)
        self.emit_one(frame_seq=2)
        self.assertEqual(self.runtime.counters["frames_emitted"], 1,
                         "no emission until the client re-aligns with a fresh hello")
        self.assertFalse(self.hello()[0], "the old enabled permission must be withdrawn")
        self.licence(generation=3, outputs=False)
        self.assertTrue(self.hello()[0])
        self.licence(generation=4, outputs=True)
        self.emit_one(frame_seq=3)
        self.assertEqual(self.runtime.counters["frames_emitted"], 2)

    def test_watchdog_stop_does_not_reenable_with_a_live_permission(self):
        self.licence()
        self.assertTrue(self.hello()[0])
        self.emit_one(frame_seq=1)
        self.driver.outputs_enabled = False
        self.driver.last_error = "command_timeout"
        for _ in range(3):
            self.runtime._tick_locked()
        self.assertFalse(self.driver.outputs_enabled)
        self.assertTrue(self.runtime.reengage_required)
        self.assertEqual(self.runtime.counters["frames_emitted"], 1)
        self.assertFalse(self.hello()[0])
        self.assertEqual(self.emit_one(frame_seq=2)["reason"], "reengage_required")
        self.licence(generation=2, outputs=False)
        self.assertTrue(self.hello()[0])
        self.licence(generation=3, outputs=True)
        self.runtime._tick_locked()
        self.assertEqual(self.runtime.counters["frames_emitted"], 1)
        self.emit_one(frame_seq=3)
        self.assertEqual(self.runtime.counters["frames_emitted"], 2)

    def test_emitter_refusal_parks_the_driver_and_discards_pending_targets(self):
        self.licence()
        self.hello()
        with mock.patch.object(self.driver, "submit_frame", side_effect=RuntimeError("bad_target")):
            self.emit_one(frame_seq=1)
        self.assertTrue(self.runtime.reengage_required)
        self.assertFalse(self.driver.outputs_enabled)
        self.assertEqual(self.driver.stop_count, 1)
        self.assertEqual(self.runtime.last_refusal["reason"], "bad_target")
        self.assertEqual(self.emit_one(frame_seq=2)["reason"], "reengage_required")
        self.assertEqual(self.driver.publish_count, 0)

    def test_a_clearance_from_before_the_stop_cannot_lift_it(self):
        self.licence(generation=5)
        self.hello()
        self.emit_one(frame_seq=1)
        self.runtime._preempt_locked("estop", "rehearsal")
        self.clear(generation=9)
        with self.assertRaises(LicenceError):
            self.clear(generation=4)
        self.runtime._tick_locked()
        self.assertIsNone(self.runtime.latched_fault)
        # The stop is lifted, but the queue and reference are gone and outputs stay off.
        self.assertTrue(self.runtime.reengage_required)
        self.assertFalse(self.driver.outputs_enabled)


class LicenceLoss(Fixture):
    def test_losing_the_licence_mid_follow_stops_emission(self):
        self.licence()
        self.hello()
        self.emit_one(frame_seq=1)
        self.assertTrue(self.driver.outputs_enabled)
        self.mirror.apply({"schema_version": SCHEMA, "type": "revoke",
                           "control_generation": 2, "reason": "car_lease_released"})
        self.runtime._tick_locked()
        self.assertFalse(self.driver.outputs_enabled)
        self.assertEqual(self.runtime.counters["licences_lost"], 1)
        self.assertTrue(self.runtime.reengage_required)
        self.licence(generation=3, outputs=True)
        self.runtime._tick_locked()
        self.assertFalse(self.driver.outputs_enabled)
        self.assertFalse(self.hello()[0])
        self.assertEqual(self.runtime.counters["frames_emitted"], 1)

    def test_a_licence_that_expires_stops_emission_without_any_revoke_message(self):
        self.licence()
        self.hello()
        self.emit_one(frame_seq=1)
        self.assertTrue(self.driver.outputs_enabled)
        self.mirror._current["deadline_ns"] = self.mirror._clock() - 1
        self.runtime._tick_locked()
        self.assertFalse(self.driver.outputs_enabled)
        self.assertEqual(self.mirror.state()["revoked_reason"], "heartbeat_expired")

    def test_output_permission_comes_from_the_licence_not_from_the_hello(self):
        self.licence(outputs=False)
        ok, _ = self.hello()
        self.assertTrue(ok)
        receipt = self.emit_one(frame_seq=2)
        self.assertFalse(self.driver.outputs_enabled)
        self.assertEqual(self.runtime.counters["frames_emitted"], 0)
        self.assertEqual(receipt["state"], "refused")

    def test_a_channel_the_licence_does_not_permit_is_refused(self):
        self.mirror.apply(licence_record(
            control_generation=1, session_id="sess-1", session_epoch=77,
            calibration_bundle_hash=BUNDLE, permits={"arm": True, "gripper": False},
            outputs_enabled=True, token_sha256=token_digest(TOKEN), ttl_ms=300))
        self.hello()
        receipt = self.runtime._on_frame_locked(self.frame(frame_seq=7))
        self.assertEqual(receipt["state"], "refused")
        self.assertIn("gripper", str(receipt.get("detail", "")))
        self.assertEqual(self.driver.publish_count, 0,
                         "a frame refused on one channel may not emit the other")

    def test_a_token_that_is_not_the_agents_is_refused(self):
        self.licence()
        ok, reason = self.hello(token="someone-elses-token")
        self.assertFalse(ok)
        self.assertIn("lease_token", str(reason))

    def test_a_session_the_licence_does_not_name_is_refused(self):
        self.licence(session_id="sess-1", session_epoch=77)
        ok, reason = self.hello(session_id="sess-OTHER", session_epoch=77)
        self.assertFalse(ok)
        self.assertEqual(reason, "epoch_or_bundle_not_licensed")


class FeedbackOwnerRevocation(Fixture):
    """A feedback publisher restart revokes output at once and never auto-resumes."""

    def test_owner_change_stops_output_invalidates_torque_and_requires_reengage(self):
        self.licence()
        self.hello()
        self.emit_one(frame_seq=1)
        self.assertTrue(self.driver.outputs_enabled)
        self.assertEqual(self.driver.publish_count, 1)

        self.runtime._on_feedback_owner_changed(777, 888, 1)

        self.assertFalse(self.driver.outputs_enabled)
        self.assertEqual(self.driver.torque_invalidations, 1)
        self.assertTrue(self.runtime.reengage_required)
        self.assertEqual(self.runtime.counters["feedback_owner_changes"], 1)
        self.assertIn("feedback_owner_changed", str(self.runtime.last_refusal["reason"]))
        # A still-live licence cannot restart motion after the owner changed.
        self.runtime._tick_locked()
        self.assertFalse(self.driver.outputs_enabled)
        receipt = self.runtime._on_frame_locked(self.frame(frame_seq=2))
        self.assertEqual(receipt["state"], "refused")
        self.assertEqual(receipt["reason"], "reengage_required")
        self.assertEqual(self.driver.publish_count, 1)

    def test_owner_change_with_output_already_disabled_still_blocks_resume(self):
        self.licence(outputs=False)
        self.hello()
        self.runtime._tick_locked()
        self.assertFalse(self.driver.outputs_enabled)
        self.runtime._on_feedback_owner_changed(None, 888, 1)
        self.assertTrue(self.runtime.reengage_required)
        # Even once the licence grants output again, the emitter waits for a re-hello.
        self.licence(outputs=True, generation=2)
        self.runtime._tick_locked()
        self.assertFalse(self.driver.outputs_enabled)


if __name__ == "__main__":
    unittest.main()
