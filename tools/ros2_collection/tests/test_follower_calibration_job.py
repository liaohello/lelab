"""Offline tests for the car-side follower calibration job.

No ROS, no serial port, no hardware: the job receives a recording sink, a fake
six-servo scan and a controllable clock.  These tests are the gate for the frozen
HTTP contract, so they cover clamping, direction, repeatability, missing/stale/
duplicate feedback, out-of-window marks, idle and absolute timeout, cancel and
token expiry, plus the sink assertion that only servo id 10 is ever commanded.
"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ros2_ws/src/lelab_mobile_agent"))

from lelab_mobile_agent.follower_calibration_job import (  # noqa: E402
    ARM_JOINTS, JOINTS, SERVO_IDS, CalibrationJobError, FollowerCalibrationJob)

WINDOW = (300, 600)


class _Sink:
    def __init__(self) -> None:
        self.published: list[tuple[int, int, float]] = []
        self.torque: list[tuple[list[int], bool]] = []
        self.fail_publish = False

    def publish_target(self, servo_id: int, raw: int, duration_s: float) -> None:
        if self.fail_publish:
            raise RuntimeError("bus_write_failed")
        self.published.append((int(servo_id), int(raw), float(duration_s)))

    def set_torque(self, servo_ids: list[int], enabled: bool) -> None:
        self.torque.append(([int(item) for item in servo_ids], bool(enabled)))


class Rig:
    """Deterministic stand-in for the agent glue (sink + scan + snapshot)."""

    def __init__(self, *, window: tuple[int, int] = WINDOW, idle_ttl_s: float = 3600.0,
                 job_ttl_s: float = 3600.0) -> None:
        self.now = 1000.0
        self.wall = 1_700_000_000.0
        self.arm = {name: 500 for name in ARM_JOINTS}
        self.gripper = 450
        self.age = 0.02
        self.hold_seq = False
        self.seq = 0
        self.time_ns = 0
        self.scan_error: Exception | None = None
        self.sample_source = "servo_position_readback"
        self.paused = False
        self.pause_calls = 0
        self.resume_calls = 0
        self.sink = _Sink()
        self.job = FollowerCalibrationJob(
            sink=self.sink,
            read_scan=self.read_scan,
            readback_snapshot=self.snapshot,
            pause_readback=self.pause,
            resume_readback=self.resume,
            device_identity=lambda: {
                "kind": "landerpi_bus_servo_follower",
                "board_serial": "TESTBOARD01",
                "board_serial_source": "environment",
                "controller_owner": "ros_robot_controller",
                "servo_ids": dict(SERVO_IDS),
            },
            clock=lambda: self.wall,
            monotonic=lambda: self.now,
            idle_ttl_s=idle_ttl_s,
            job_ttl_s=job_ttl_s,
            agent_version="test",
            profile="hardware_hybrid_manual_v1",
        )
        self.window = window

    # -- plumbing ---------------------------------------------------------
    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall += seconds

    def pause(self) -> None:
        self.paused = True
        self.pause_calls += 1

    def resume(self) -> None:
        self.paused = False
        self.resume_calls += 1

    def read_scan(self) -> dict[str, int]:
        if self.scan_error is not None:
            raise self.scan_error
        values = dict(self.arm)
        values["r_joint"] = self.gripper
        return values

    def snapshot(self) -> dict:
        if not self.hold_seq:
            self.seq += 1
            self.time_ns += 200_000_000
        samples = {}
        for name in JOINTS:
            samples[name] = {
                "name": name,
                "servo_id": SERVO_IDS[name],
                "raw_position": self.gripper if name == "r_joint" else self.arm[name],
                "sample_seq": self.seq,
                "sample_time_ns": self.time_ns,
                "age_s": self.age,
                "valid": True,
                "error": None,
                "source_kind": self.sample_source,
            }
        return {"reason": "ok", "joint_samples": samples,
                "age_s": {name: self.age for name in JOINTS}}

    def body(self, **extra) -> dict:
        return {"token": self.job.token, "job_id": self.job.job_id,
                "generation": self.job.generation, **extra}

    # -- flows ------------------------------------------------------------
    def start(self) -> dict:
        return self.job.start({
            "safe_window_raw": list(self.window),
            "operator_confirmed_safe_window": True,
            "support_confirmed": True,
            "operator": "unit-test",
        })

    def sweep_arms(self, *, low: int = 420, high: int = 560, step: float = 0.2,
                   stride: int = 4) -> None:
        """Smooth ramp down and back: constant velocity, so second differences are 0."""
        values = list(range(low, high + 1, stride)) + list(range(high - stride, low - 1, -stride))
        for raw in values:
            for name in ARM_JOINTS:
                self.arm[name] = raw
            self.advance(step)
            self.job.tick()
            # The page polls status while the operator moves the arm; that heartbeat
            # is what keeps a long manual capture alive.
            self.job.status(self.body())

    def enter_drag(self) -> dict:
        return self.job.gripper_target(self.body(
            target_permille=500, client_seq=0, ttl_ms=200,
            pointer_active=True, pointer_generation=None,
            safe_min_raw=self.window[0], safe_max_raw=self.window[1]))

    def drag(self, *, seq: int, permille: float, feedback: int | None = None,
             settle: float = 0.12) -> dict:
        # Respect the 10 Hz ceiling the way a browser pointermove stream must.
        self.advance(0.11)
        result = self.job.gripper_target(self.body(
            target_permille=permille, client_seq=seq, ttl_ms=200,
            pointer_active=True, pointer_generation=self.job.calibrator.pointer_generation,
            safe_min_raw=self.window[0], safe_max_raw=self.window[1]))
        if result.get("accepted"):
            self.advance(settle)
            self.job.tick()
            if feedback is not None:
                self.gripper = feedback
            self.advance(0.1)
            self.job.tick()
        return result

    def stabilise(self, raw: int, *, samples: int = 5, step: float = 0.25) -> None:
        """Hold the gripper still long enough for the 1 s stability window to fill."""
        self.gripper = raw
        for _ in range(samples):
            self.advance(step)
            self.job.tick()

    def mark(self, end: str) -> dict:
        return self.job.gripper_mark(self.body(end=end))


class StartTests(unittest.TestCase):
    def test_start_returns_job_identity_and_requires_confirmations(self) -> None:
        rig = Rig()
        with self.assertRaises(CalibrationJobError) as caught:
            rig.job.start({"safe_window_raw": [300, 600], "support_confirmed": True})
        self.assertEqual(caught.exception.reason, "safe_window_confirmation_required")
        with self.assertRaises(CalibrationJobError) as caught:
            rig.job.start({"safe_window_raw": [300, 600],
                           "operator_confirmed_safe_window": True})
        self.assertEqual(caught.exception.reason, "support_confirmation_required")
        started = rig.start()
        self.assertRegex(started["job_id"], r"^[0-9a-f]{16}$")
        self.assertTrue(started["token"])
        self.assertEqual(started["phase"], "manual_range")
        self.assertEqual(started["baseline_raw"]["r_joint"], 450)
        self.assertFalse(started["outputs_enabled"])

    def test_default_calibration_job_window_allows_a_full_manual_session(self) -> None:
        rig = Rig(job_ttl_s=1800.0)
        self.assertEqual(rig.start()["expires_in_s"], 1800.0)

    def test_signed_vendor_position_is_retained_as_observed_evidence(self) -> None:
        rig = Rig()
        rig.arm["joint3"] = -49
        started = rig.start()
        self.assertEqual(started["baseline_raw"]["joint3"], -49)
        rig.advance(0.2)
        rig.job.tick()
        self.assertEqual(
            rig.job.status(rig.body())["joints"]["joint3"]["last_raw"], -49)

    def test_safe_window_is_bounded_so_no_unrestricted_endstop_search(self) -> None:
        rig = Rig()
        cases = (
            ([600, 300], "invalid_safe_window"),
            ([300, 320], "safe_window_span_invalid"),
            ([300, 600.0], "invalid_safe_window"),
            (["300", 600], "invalid_safe_window"),
            (300, "invalid_safe_window"),
        )
        for window, reason in cases:
            with self.subTest(window=window):
                with self.assertRaises(CalibrationJobError) as caught:
                    rig.job.start({"safe_window_raw": window,
                                   "operator_confirmed_safe_window": True,
                                   "support_confirmed": True})
                self.assertEqual(caught.exception.reason, reason)

    def test_full_vendor_software_window_is_allowed_for_observed_calibration(self) -> None:
        rig = Rig(window=(0, 1000))
        result = rig.start()
        self.assertEqual(result["safe_window_raw"], [0, 1000])

    def test_missing_joint_blocks_start(self) -> None:
        rig = Rig()
        original = rig.snapshot

        def without_gripper() -> dict:
            snap = original()
            snap["joint_samples"].pop("r_joint")
            return snap

        rig.job.readback_snapshot = without_gripper
        with self.assertRaises(CalibrationJobError) as caught:
            rig.start()
        self.assertEqual(caught.exception.reason, "follower_feedback_incomplete")
        self.assertIn("r_joint", caught.exception.detail)

    def test_second_start_is_refused_while_a_job_is_active(self) -> None:
        rig = Rig()
        rig.start()
        with self.assertRaises(CalibrationJobError) as caught:
            rig.start()
        self.assertEqual(caught.exception.reason, "follower_calibration_active")

    def test_operator_entered_arm_ranges_are_recorded_without_fake_samples(self) -> None:
        rig = Rig()
        rig.start()
        ranges = {
            "joint1": {"min_raw": 10, "max_raw": 998},
            "joint2": {"min_raw": 10, "max_raw": 961},
            "joint3": {"min_raw": 0, "max_raw": 990},
            "joint4": {"min_raw": 100, "max_raw": 980},
            "joint5": {"min_raw": 80, "max_raw": 910},
        }
        result = rig.job.manual_ranges(rig.body(ranges=ranges))
        self.assertTrue(result["arm_valid"])
        self.assertEqual(result["manual_arm_ranges"], ranges)
        evidence = rig.job.evidence()
        self.assertTrue(evidence["arm_ranges"]["operator_entered"])
        self.assertEqual(evidence["arm_ranges"]["method"], "operator_manual_entry")
        for name in ARM_JOINTS:
            self.assertEqual(evidence["joints"][name]["method"], "operator_manual_entry")
            self.assertEqual(evidence["joints"][name]["samples"], 0)
            self.assertTrue(evidence["joints"][name]["valid"])

    def test_full_manual_entry_completes_without_gripper_drag(self) -> None:
        rig = Rig(window=(0, 1000))
        rig.start()
        ranges = {
            "joint1": {"min_raw": 10, "max_raw": 998},
            "joint2": {"min_raw": 10, "max_raw": 961},
            "joint3": {"min_raw": 0, "max_raw": 990},
            "joint4": {"min_raw": 100, "max_raw": 980},
            "joint5": {"min_raw": 80, "max_raw": 910},
        }
        result = rig.job.manual_ranges(rig.body(
            ranges=ranges,
            gripper={"closed_raw": 504, "open_raw": 192},
        ))
        self.assertEqual(result["phase"], FollowerCalibrationJob.REVIEW)
        self.assertFalse(result["gripper"]["valid"] is False)
        self.assertEqual(result["gripper"]["method"], "operator_manual_entry")
        self.assertEqual(result["gripper"]["observed_closed_raw"], 504)
        self.assertEqual(result["gripper"]["observed_open_raw"], 192)
        self.assertEqual(rig.sink.published, [])
        rig.advance(0.2)
        rig.job.tick()
        evidence = rig.job.evidence()
        self.assertEqual(evidence["method"], "operator_manual_full_entry")
        self.assertTrue(evidence["quality"]["complete"], evidence["quality"]["reasons"])
        self.assertTrue(evidence["gripper_ranges"]["operator_entered"])

    def test_full_manual_entry_keeps_both_gripper_ends_inside_confirmed_window(self) -> None:
        rig = Rig(window=(300, 600))
        rig.start()
        ranges = {
            "joint1": {"min_raw": 10, "max_raw": 998},
            "joint2": {"min_raw": 10, "max_raw": 961},
            "joint3": {"min_raw": 0, "max_raw": 990},
            "joint4": {"min_raw": 100, "max_raw": 980},
            "joint5": {"min_raw": 80, "max_raw": 910},
        }
        with self.assertRaises(CalibrationJobError) as caught:
            rig.job.manual_ranges(rig.body(
                ranges=ranges, gripper={"closed_raw": 504, "open_raw": 192}))
        self.assertEqual(caught.exception.reason, "manual_gripper_outside_window")

    def test_operator_entered_arm_ranges_reject_missing_or_too_small_ranges(self) -> None:
        rig = Rig()
        rig.start()
        valid = {
            "joint1": {"min_raw": 10, "max_raw": 998},
            "joint2": {"min_raw": 10, "max_raw": 961},
            "joint3": {"min_raw": 0, "max_raw": 990},
            "joint4": {"min_raw": 100, "max_raw": 980},
            "joint5": {"min_raw": 80, "max_raw": 910},
        }
        with self.assertRaises(CalibrationJobError) as caught:
            rig.job.manual_ranges(rig.body(ranges={"joint1": valid["joint1"]}))
        self.assertEqual(caught.exception.reason, "invalid_manual_ranges")
        broken = {**valid, "joint2": {"min_raw": 500, "max_raw": 550}}
        with self.assertRaises(CalibrationJobError) as caught:
            rig.job.manual_ranges(rig.body(ranges=broken))
        self.assertEqual(caught.exception.reason, "manual_range_span_too_small")


class MappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = Rig()
        self.rig.start()
        self.rig.sweep_arms()
        self.rig.enter_drag()
        self.calibrator = self.rig.job.calibrator

    def test_slider_maps_onto_the_declared_window_and_clamps(self) -> None:
        self.assertEqual(self.calibrator.permille_to_raw(0), (300, False))
        self.assertEqual(self.calibrator.permille_to_raw(1000), (600, False))
        self.assertEqual(self.calibrator.permille_to_raw(500), (450, False))
        self.assertEqual(self.calibrator.permille_to_raw(-50), (300, True))
        self.assertEqual(self.calibrator.permille_to_raw(1500), (600, True))
        values = [self.calibrator.permille_to_raw(p)[0] for p in (0, 250, 500, 750, 1000)]
        self.assertEqual(values, [300, 375, 450, 525, 600])
        self.assertEqual(values, sorted(values), "raw must follow the slider direction")

    def test_reversed_declared_window_is_rejected_rather_than_reinterpreted(self) -> None:
        with self.assertRaises(CalibrationJobError):
            Rig(window=(600, 300)).start()

    def test_target_outside_the_confirmed_window_is_refused(self) -> None:
        with self.assertRaises(CalibrationJobError) as caught:
            self.rig.job.gripper_target(self.rig.body(
                target_permille=500, client_seq=99, ttl_ms=200, pointer_active=True,
                pointer_generation=self.calibrator.pointer_generation,
                safe_min_raw=100, safe_max_raw=900))
        self.assertEqual(caught.exception.reason, "safe_window_mismatch")

    def test_invalid_numeric_targets_are_refused(self) -> None:
        for permille, ttl, reason in (
            ("450", 200, "invalid_target_permille"),
            (float("nan"), 200, "invalid_target_permille"),
            (450, 5000, "invalid_ttl"),
            (450, 0, "invalid_ttl"),
        ):
            with self.subTest(permille=permille, ttl=ttl):
                with self.assertRaises(CalibrationJobError) as caught:
                    self.rig.job.gripper_target(self.rig.body(
                        target_permille=permille, client_seq=50, ttl_ms=ttl,
                        pointer_active=True,
                        pointer_generation=self.calibrator.pointer_generation,
                        safe_min_raw=WINDOW[0], safe_max_raw=WINDOW[1]))
                self.assertEqual(caught.exception.reason, reason)


class TargetFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = Rig()
        self.rig.start()
        self.rig.sweep_arms()
        self.rig.enter_drag()
        self.rig.advance(0.11)

    def test_rate_limit_drops_targets_without_publishing(self) -> None:
        rig = self.rig
        first = rig.job.gripper_target(rig.body(
            target_permille=500, client_seq=1, ttl_ms=200, pointer_active=True,
            pointer_generation=rig.job.calibrator.pointer_generation,
            safe_min_raw=WINDOW[0], safe_max_raw=WINDOW[1]))
        self.assertTrue(first["accepted"])
        # Two pointermove events in the same instant must not both reach the bus.
        second = rig.job.gripper_target(rig.body(
            target_permille=520, client_seq=2, ttl_ms=200, pointer_active=True,
            pointer_generation=rig.job.calibrator.pointer_generation,
            safe_min_raw=WINDOW[0], safe_max_raw=WINDOW[1]))
        self.assertFalse(second["accepted"])
        self.assertEqual(second["reason"], "rate_limited")
        self.assertGreater(second["retry_after_ms"], 0)
        rig.advance(0.12)
        rig.job.tick()
        self.assertEqual(len(rig.sink.published), 1)

    def test_pointer_up_is_refused(self) -> None:
        rig = self.rig
        with self.assertRaises(CalibrationJobError) as caught:
            rig.job.gripper_target(rig.body(
                target_permille=520, client_seq=5, ttl_ms=200, pointer_active=False,
                pointer_generation=rig.job.calibrator.pointer_generation))
        self.assertEqual(caught.exception.reason, "pointer_not_active")

    def test_duplicate_client_seq_replay_is_refused(self) -> None:
        rig = self.rig
        rig.drag(seq=1, permille=400)
        with self.assertRaises(CalibrationJobError) as caught:
            rig.job.gripper_target(rig.body(
                target_permille=420, client_seq=1, ttl_ms=200, pointer_active=True,
                pointer_generation=rig.job.calibrator.pointer_generation,
                safe_min_raw=WINDOW[0], safe_max_raw=WINDOW[1]))
        self.assertEqual(caught.exception.reason, "client_seq_not_increasing")

    def test_stale_pointer_generation_is_refused(self) -> None:
        rig = self.rig
        generation = rig.job.calibrator.pointer_generation
        rig.drag(seq=1, permille=400)
        with self.assertRaises(CalibrationJobError) as caught:
            rig.job.gripper_target(rig.body(
                target_permille=420, client_seq=9, ttl_ms=200, pointer_active=True,
                pointer_generation=generation + 7,
                safe_min_raw=WINDOW[0], safe_max_raw=WINDOW[1]))
        self.assertEqual(caught.exception.reason, "pointer_generation_mismatch")

    def test_only_servo_id_10_is_ever_commanded(self) -> None:
        rig = self.rig
        for seq in range(1, 6):
            permille = 300 + seq * 40
            target = rig.job.calibrator.permille_to_raw(permille)[0]
            rig.drag(seq=seq, permille=permille, feedback=target - 2)
        self.assertTrue(rig.sink.published)
        self.assertEqual({servo_id for servo_id, _, _ in rig.sink.published}, {10})
        for _, raw, duration in rig.sink.published:
            self.assertGreaterEqual(raw, WINDOW[0])
            self.assertLessEqual(raw, WINDOW[1])
            self.assertGreater(duration, 0)
        self.assertEqual(rig.sink.torque, [], "the drag phase must not touch torque")
        self.assertTrue(rig.paused, "the periodic scan pauses so the drag owns the bus")

    def test_expired_queued_target_is_dropped_instead_of_sent_late(self) -> None:
        rig = self.rig
        rig.advance(0.11)
        rig.job.gripper_target(rig.body(
            target_permille=500, client_seq=3, ttl_ms=40, pointer_active=True,
            pointer_generation=rig.job.calibrator.pointer_generation,
            safe_min_raw=WINDOW[0], safe_max_raw=WINDOW[1]))
        rig.advance(0.2)
        rig.job.tick()
        self.assertEqual(rig.sink.published, [])
        self.assertEqual(rig.job.calibrator.commands_dropped, 1)

    def test_command_budget_is_finite(self) -> None:
        rig = self.rig
        rig.job.calibrator.max_drag_commands = 3
        for seq in range(1, 4):
            rig.drag(seq=seq, permille=300 + seq * 50, feedback=350 + seq * 50)
        self.assertEqual(len(rig.sink.published), 3)
        with self.assertRaises(CalibrationJobError) as caught:
            rig.drag(seq=9, permille=700, feedback=600)
        self.assertEqual(caught.exception.reason, "drag_budget_exhausted")


class SamplingTests(unittest.TestCase):
    def test_duplicate_polling_does_not_increase_coverage(self) -> None:
        rig = Rig()
        rig.start()
        rig.arm["joint1"] = 500
        rig.hold_seq = True
        for _ in range(5):
            rig.advance(0.2)
            rig.job.tick()
        joint = rig.job.sampler.ranges["joint1"]
        samples_after_first = joint.samples
        for _ in range(10):
            rig.advance(0.2)
            rig.job.tick()
        self.assertEqual(joint.samples, samples_after_first)
        self.assertGreater(joint.duplicates, 0)

    def test_stale_feedback_is_not_counted_as_coverage(self) -> None:
        rig = Rig()
        rig.start()
        rig.age = 5.0
        rig.arm["joint1"] = 400
        rig.advance(0.2)
        rig.job.tick()
        joint = rig.job.sampler.ranges["joint1"]
        self.assertEqual(joint.min_raw, 500)
        self.assertEqual(joint.max_raw, 500)
        self.assertGreater(joint.stale, 0)

    def test_untrusted_source_never_extends_range(self) -> None:
        rig = Rig()
        rig.start()
        rig.sample_source = "joint_states"
        rig.arm["joint2"] = 300
        rig.advance(0.2)
        rig.job.tick()
        joint = rig.job.sampler.ranges["joint2"]
        self.assertEqual(joint.min_raw, 500)
        self.assertGreater(joint.anomalies, 0)

    def test_sequence_regression_faults_instead_of_extending_range(self) -> None:
        rig = Rig()
        rig.start()
        rig.advance(0.2)
        rig.job.tick()
        rig.seq = 1
        rig.time_ns += 10_000_000_000
        rig.arm["joint2"] = 300
        rig.advance(0.2)
        rig.job.tick()
        self.assertEqual(rig.job.phase, FollowerCalibrationJob.FAULT)
        self.assertEqual(rig.job.stop_reason, "readback_sequence_regressed")

    def test_noisy_readback_is_rejected(self) -> None:
        rig = Rig()
        rig.start()
        random.seed(7)
        for _ in range(60):
            for name in ARM_JOINTS:
                rig.arm[name] = 500 + random.randint(-25, 25)
            rig.advance(0.2)
            rig.job.tick()
        joint = rig.job.sampler.ranges["joint1"]
        self.assertGreater(joint.noise_raw, 6)
        self.assertIn("noise_high", rig.job.sampler.assess("joint1")["reasons"])

    def test_smooth_sweep_passes_noise_and_span(self) -> None:
        rig = Rig()
        rig.start()
        rig.sweep_arms()
        joint = rig.job.sampler.ranges["joint1"]
        self.assertGreaterEqual(joint.span_raw, 80)
        self.assertLessEqual(joint.noise_raw, 6)
        assessment = rig.job.sampler.assess("joint1")
        self.assertTrue(assessment["valid"], assessment["reasons"])


class TorqueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = Rig()
        self.rig.start()

    def test_per_axis_torque_release_requires_support_and_excludes_the_gripper(self) -> None:
        rig = self.rig
        with self.assertRaises(CalibrationJobError) as caught:
            rig.job.torque(rig.body(enabled=False, joints=["joint1"]))
        self.assertEqual(caught.exception.reason, "support_confirmation_required")
        rig.job.torque(rig.body(enabled=False, joints=["joint1", "joint2"],
                                support_confirmed=True))
        self.assertEqual(rig.sink.torque[-1], ([1, 2], False))
        self.assertEqual(rig.job.released_joints, {"joint1", "joint2"})
        rig.job.torque(rig.body(enabled=True, joints=["joint1"]))
        self.assertEqual(rig.sink.torque[-1], ([1], True))
        self.assertEqual(rig.job.released_joints, {"joint2"})
        with self.assertRaises(CalibrationJobError) as caught:
            rig.job.torque(rig.body(enabled=False, joints=["r_joint"], support_confirmed=True))
        self.assertEqual(caught.exception.reason, "invalid_torque_joints")

    def test_gripper_drag_is_refused_while_arm_torque_is_released(self) -> None:
        rig = self.rig
        rig.job.torque(rig.body(enabled=False, joints=["joint3"], support_confirmed=True))
        with self.assertRaises(CalibrationJobError) as caught:
            rig.enter_drag()
        self.assertEqual(caught.exception.reason, "arm_torque_released")

    def test_gripper_drag_can_run_before_arm_ranges(self) -> None:
        result = self.rig.enter_drag()
        self.assertEqual(result["phase"], FollowerCalibrationJob.DRAG)
        self.assertFalse(self.rig.job.sampler.arm_valid()[0])


class MarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = Rig()
        self.rig.start()
        self.rig.sweep_arms()
        self.rig.enter_drag()

    def test_two_rounds_are_required_before_the_result_is_valid(self) -> None:
        rig = self.rig
        rig.drag(seq=1, permille=100, feedback=330)
        rig.stabilise(330)
        rig.advance(0.4)
        closed = rig.mark("closed")
        self.assertEqual(closed["marked"]["raw"], 330)
        self.assertFalse(closed["gripper"]["valid"])
        self.assertIn("repeat_rounds_missing", closed["gripper"]["reasons"])

        with self.assertRaises(CalibrationJobError) as caught:
            rig.mark("closed")
        self.assertEqual(caught.exception.reason, "duplicate_mark")

        rig.drag(seq=2, permille=900, feedback=570)
        rig.stabilise(570)
        rig.advance(0.4)
        opened = rig.mark("open")
        self.assertEqual(opened["gripper"]["rounds_completed"], 1)
        self.assertFalse(opened["gripper"]["valid"])

    def test_complete_two_round_calibration_reports_repeatability(self) -> None:
        rig = self.rig
        for seq, permille, feedback, end in (
                (1, 100, 330, "closed"), (2, 900, 572, "open"),
                (3, 120, 335, "closed"), (4, 880, 568, "open")):
            rig.drag(seq=seq, permille=permille, feedback=feedback)
            rig.stabilise(feedback)
            rig.advance(0.4)
            rig.mark(end)
        summary = rig.job.calibrator.summary(rig.now)
        self.assertEqual(summary["rounds_completed"], 2)
        self.assertEqual(summary["repeatability_raw"], 5)
        self.assertEqual(summary["direction"], 1)
        self.assertTrue(summary["valid"], summary["reasons"])
        self.assertEqual(summary["observed_closed_raw"], 332)
        self.assertEqual(summary["observed_open_raw"], 570)
        self.assertLess(summary["usable_closed_raw"], summary["usable_open_raw"])

    def test_direction_that_flips_between_rounds_is_invalid(self) -> None:
        rig = self.rig
        for seq, permille, feedback, end in (
                (1, 100, 330, "closed"), (2, 900, 570, "open"),
                (3, 100, 330, "open"), (4, 900, 570, "closed")):
            rig.drag(seq=seq, permille=permille, feedback=feedback)
            rig.stabilise(feedback)
            rig.advance(0.4)
            rig.mark(end)
        summary = rig.job.calibrator.summary(rig.now)
        self.assertIn("direction_inconsistent", summary["reasons"])
        self.assertFalse(summary["valid"])

    def test_mark_requires_settled_feedback(self) -> None:
        rig = self.rig
        rig.drag(seq=1, permille=100, feedback=330)
        with self.assertRaises(CalibrationJobError) as caught:
            rig.mark("closed")
        self.assertEqual(caught.exception.reason, "gripper_settle_pending")

    def test_stale_feedback_blocks_the_mark(self) -> None:
        rig = self.rig
        rig.drag(seq=1, permille=100, feedback=330)
        rig.stabilise(330)
        # No feedback for longer than the freshness window: the worker stopped
        # receiving servo id 10, so an operator click cannot be trusted.
        rig.advance(0.7)
        with self.assertRaises(CalibrationJobError) as caught:
            rig.mark("closed")
        self.assertEqual(caught.exception.reason, "gripper_feedback_stale")

    def test_mark_error_over_tolerance_is_refused(self) -> None:
        rig = self.rig
        rig.drag(seq=1, permille=100, feedback=330)
        rig.stabilise(330)
        rig.gripper = 356
        for _ in range(3):
            rig.advance(0.1)
            rig.job.tick()
        rig.advance(0.4)
        with self.assertRaises(CalibrationJobError) as caught:
            rig.mark("closed")
        self.assertIn(caught.exception.reason,
                      {"gripper_mark_error_exceeded", "gripper_not_stable"})

    def test_mark_outside_the_safe_window_is_refused(self) -> None:
        rig = self.rig
        rig.drag(seq=1, permille=0, feedback=290)
        rig.stabilise(290)
        rig.advance(0.4)
        with self.assertRaises(CalibrationJobError) as caught:
            rig.mark("closed")
        self.assertEqual(caught.exception.reason, "gripper_mark_outside_window")

    def test_mark_without_any_drag_is_refused(self) -> None:
        with self.assertRaises(CalibrationJobError) as caught:
            self.rig.job.gripper_mark(self.rig.body(end="closed"))
        self.assertEqual(caught.exception.reason, "gripper_no_target_yet")

    def test_invalid_mark_end_is_refused(self) -> None:
        with self.assertRaises(CalibrationJobError) as caught:
            self.rig.mark("half")
        self.assertEqual(caught.exception.reason, "invalid_mark_end")


class FaultTests(unittest.TestCase):
    def test_large_slider_step_is_not_faulted_after_pointer_release(self) -> None:
        rig = Rig()
        rig.start()
        rig.enter_drag()
        rig.advance(0.11)
        result = rig.job.gripper_target(rig.body(
            target_permille=1000, client_seq=1, ttl_ms=200,
            pointer_active=True,
            pointer_generation=rig.job.calibrator.pointer_generation,
            safe_min_raw=300, safe_max_raw=600))
        self.assertTrue(result["accepted"])
        for raw in (460, 490, 520, 550, 580):
            rig.gripper = raw
            rig.advance(0.2)
            rig.job.tick()
            self.assertEqual(rig.job.phase, FollowerCalibrationJob.DRAG)
        # The target TTL expired long ago; holding stable for an observed mark
        # must not be confused with a live tracking fault.
        for _ in range(8):
            rig.gripper = 580
            rig.advance(0.2)
            rig.job.tick()
        self.assertEqual(rig.job.phase, FollowerCalibrationJob.DRAG)
        self.assertIsNone(rig.job.stop_reason)

    def test_manual_arm_takeover_leaves_gripper_mode_without_faulting(self) -> None:
        rig = Rig()
        rig.start()
        rig.sweep_arms()
        rig.enter_drag()
        rig.drag(seq=1, permille=400, feedback=420)
        before = len(rig.sink.published)
        rig.arm["joint3"] = 540
        rig.advance(0.1)
        rig.job.tick()
        self.assertEqual(rig.job.phase, FollowerCalibrationJob.MANUAL)
        self.assertFalse(rig.job.readback_paused)
        self.assertIn("manual_takeover", rig.job.last_tick_error)
        rig.advance(0.1)
        rig.job.tick()
        self.assertEqual(len(rig.sink.published), before)

    def test_lost_feedback_faults_and_resumes_periodic_readback(self) -> None:
        rig = Rig()
        rig.start()
        rig.sweep_arms()
        rig.enter_drag()
        rig.drag(seq=1, permille=400, feedback=420)
        rig.scan_error = RuntimeError("bus poisoned")
        rig.advance(0.1)
        rig.job.tick()
        self.assertEqual(rig.job.phase, FollowerCalibrationJob.DRAG)
        rig.advance(0.1)
        rig.job.tick()
        self.assertEqual(rig.job.phase, FollowerCalibrationJob.DRAG)
        rig.advance(0.1)
        rig.job.tick()
        self.assertEqual(rig.job.phase, FollowerCalibrationJob.FAULT)
        self.assertEqual(rig.job.stop_reason, "gripper_feedback_lost")
        self.assertFalse(rig.job.readback_paused)
        self.assertGreaterEqual(rig.resume_calls, 1)

    def test_transient_feedback_loss_recovers_without_faulting(self) -> None:
        rig = Rig()
        rig.start()
        rig.enter_drag()
        rig.drag(seq=1, permille=400, feedback=420)
        rig.scan_error = RuntimeError("one bad read")
        rig.advance(0.1)
        rig.job.tick()
        self.assertEqual(rig.job.phase, FollowerCalibrationJob.DRAG)
        rig.scan_error = None
        rig.advance(0.1)
        rig.job.tick()
        self.assertEqual(rig.job.phase, FollowerCalibrationJob.DRAG)
        self.assertEqual(rig.job.drag_scan_failures, 0)
        self.assertIsNone(rig.job.last_tick_error)

    def test_publish_failure_faults_the_job(self) -> None:
        rig = Rig()
        rig.start()
        rig.sweep_arms()
        rig.enter_drag()
        rig.sink.fail_publish = True
        rig.drag(seq=1, permille=400, feedback=420)
        self.assertEqual(rig.job.phase, FollowerCalibrationJob.FAULT)
        self.assertEqual(rig.job.stop_reason, "gripper_command_failed")


class TimeoutAndTokenTests(unittest.TestCase):
    def test_browser_disconnect_stops_the_drag(self) -> None:
        rig = Rig(idle_ttl_s=2.0)
        rig.start()
        rig.sweep_arms()
        rig.enter_drag()
        rig.drag(seq=1, permille=400, feedback=420)
        rig.now += 2.5
        rig.job.tick()
        self.assertEqual(rig.job.phase, FollowerCalibrationJob.STOPPED)
        self.assertEqual(rig.job.stop_reason, "client_timeout")
        self.assertFalse(rig.job.readback_paused)

    def test_browser_disconnect_restores_released_torque(self) -> None:
        rig = Rig(idle_ttl_s=2.0)
        rig.start()
        rig.job.torque(rig.body(enabled=False, joints=["joint4"], support_confirmed=True))
        rig.now += 2.5
        rig.job.tick()
        self.assertEqual(rig.job.phase, FollowerCalibrationJob.STOPPED)
        self.assertEqual(rig.sink.torque[-1], ([4], True))
        self.assertEqual(rig.job.released_joints, set())

    def test_stop_cancels_commands_and_restores_released_torque(self) -> None:
        rig = Rig()
        rig.start()
        rig.sweep_arms()
        rig.job.torque(rig.body(enabled=False, joints=["joint1"], support_confirmed=True))
        rig.job.stop(rig.body(reason="operator_cancel"))
        self.assertEqual(rig.job.phase, FollowerCalibrationJob.STOPPED)
        self.assertEqual(rig.sink.torque[-1], ([1], True))
        self.assertEqual(rig.job.released_joints, set())
        with self.assertRaises(CalibrationJobError) as caught:
            rig.enter_drag()
        self.assertEqual(caught.exception.reason, "follower_calibration_phase")
        with self.assertRaises(CalibrationJobError) as caught:
            rig.job.torque(rig.body(enabled=False, joints=["joint1"], support_confirmed=True))
        self.assertEqual(caught.exception.reason, "follower_calibration_phase")

    def test_token_expiry_is_enforced_server_side(self) -> None:
        rig = Rig(job_ttl_s=5.0)
        rig.start()
        rig.now += 6.0
        with self.assertRaises(CalibrationJobError) as caught:
            rig.job.status(rig.body())
        self.assertEqual(caught.exception.reason, "follower_calibration_expired")
        self.assertEqual(rig.job.phase, FollowerCalibrationJob.STOPPED)

    def test_foreign_token_job_and_generation_are_refused(self) -> None:
        rig = Rig()
        rig.start()
        for body, reason in (
            ({"token": "nope", "job_id": rig.job.job_id},
             "follower_calibration_token_invalid"),
            ({"token": rig.job.token, "job_id": "deadbeefdeadbeef"},
             "follower_calibration_job_mismatch"),
            ({"token": rig.job.token, "generation": rig.job.generation + 4},
             "follower_calibration_generation_mismatch"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(CalibrationJobError) as caught:
                    rig.job.status(body)
                self.assertEqual(caught.exception.reason, reason)


class EvidenceTests(unittest.TestCase):
    def test_evidence_is_complete_only_after_both_phases(self) -> None:
        rig = Rig()
        rig.start()
        partial = rig.job.evidence()
        self.assertFalse(partial["quality"]["complete"])
        self.assertIn("job_not_finished", partial["quality"]["reasons"])
        self.assertEqual(partial["source_kind"], "servo_position_readback")
        self.assertEqual(partial["method"], "operator_manual_plus_gripper_drag")

        rig.sweep_arms()
        rig.enter_drag()
        for seq, permille, feedback, end in (
                (1, 100, 330, "closed"), (2, 900, 570, "open"),
                (3, 110, 333, "closed"), (4, 890, 567, "open")):
            rig.drag(seq=seq, permille=permille, feedback=feedback)
            rig.stabilise(feedback)
            rig.advance(0.4)
            rig.mark(end)
        rig.job.gripper_stop(rig.body())
        rig.job.stop(rig.body(reason="done"))
        rig.advance(0.2)
        rig.job.tick()

        evidence = rig.job.evidence()
        self.assertTrue(evidence["quality"]["complete"], evidence["quality"]["reasons"])
        self.assertEqual(evidence["schema_version"], 2)
        self.assertRegex(evidence["sample_log_sha256"], r"^[0-9a-f]{64}$")
        self.assertGreater(evidence["sample_log_entries"], 0)
        self.assertEqual(evidence["device_identity"]["board_serial"], "TESTBOARD01")
        self.assertEqual(evidence["gripper"]["rounds_completed"], 2)
        for name in ARM_JOINTS:
            self.assertTrue(evidence["joints"][name]["valid"], name)
            self.assertGreaterEqual(evidence["joints"][name]["span_raw"], 80)
        self.assertEqual(rig.job.evidence()["sample_log_sha256"],
                         evidence["sample_log_sha256"])

    def test_incomplete_capture_is_not_complete(self) -> None:
        rig = Rig()
        rig.start()
        rig.job.stop(rig.body(reason="too_early"))
        evidence = rig.job.evidence()
        self.assertFalse(evidence["quality"]["complete"])
        self.assertTrue(any(reason.startswith("arm_ranges_incomplete")
                            for reason in evidence["quality"]["reasons"]))

    def test_evidence_never_claims_mechanical_limits(self) -> None:
        rig = Rig()
        rig.start()
        rig.sweep_arms()
        rig.enter_drag()
        rig.drag(seq=1, permille=100, feedback=330)
        rig.stabilise(330)
        rig.advance(0.4)
        rig.mark("closed")
        evidence = rig.job.evidence()
        self.assertNotIn("mechanical_limits", repr(evidence))
        self.assertEqual(evidence["gripper"]["method"], "operator_drag_safe_window")
        self.assertEqual(evidence["joints"]["joint1"]["method"], "manual_range_capture")

    def test_gripper_stop_returns_to_read_only_review(self) -> None:
        rig = Rig()
        rig.start()
        rig.sweep_arms()
        rig.enter_drag()
        rig.drag(seq=1, permille=400, feedback=420)
        result = rig.job.gripper_stop(rig.body())
        self.assertTrue(result["stopped"])
        self.assertEqual(rig.job.phase, FollowerCalibrationJob.REVIEW)
        self.assertFalse(rig.job.readback_paused)
        with self.assertRaises(CalibrationJobError) as caught:
            rig.job.gripper_stop(rig.body())
        self.assertEqual(caught.exception.reason, "gripper_drag_not_active")

    def test_gripper_stop_returns_to_manual_when_arm_ranges_are_incomplete(self) -> None:
        rig = Rig()
        rig.start()
        rig.enter_drag()
        rig.job.gripper_stop(rig.body())
        self.assertEqual(rig.job.phase, FollowerCalibrationJob.MANUAL)
        self.assertFalse(rig.job.readback_paused)
        rig.sweep_arms()
        self.assertTrue(rig.job.sampler.arm_valid()[0])


class AgentWiringTests(unittest.TestCase):
    """The agent is a ROS node and cannot be imported here, so the wiring is asserted
    against its source: the frozen routes exist, the synchronous scan routes are gone,
    and the job really is advanced by a background worker."""

    AGENT = ROOT / "ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/agent.py"

    def setUp(self) -> None:
        self.source = self.AGENT.read_text(encoding="utf-8")

    def test_only_the_frozen_routes_exist(self) -> None:
        for path in ("/calibration/follower/start", "/calibration/follower/status",
                     "/calibration/follower/torque",
                     "/calibration/follower/gripper/target",
                     "/calibration/follower/gripper/mark",
                     "/calibration/follower/gripper/stop",
                     "/calibration/follower/stop"):
            self.assertIn(f'self.path == "{path}"', self.source, path)

    def test_synchronous_scan_routes_are_removed(self) -> None:
        self.assertNotIn("/calibration/follower/step", self.source)
        self.assertNotIn("/calibration/follower/auto-gripper", self.source)
        self.assertNotIn("follower_calibration_auto_gripper", self.source)
        self.assertNotIn("follower_calibration_step", self.source)

    def test_worker_thread_advances_the_job(self) -> None:
        self.assertIn("FollowerCalibrationJob(", self.source)
        self.assertIn("_calibration_worker_loop", self.source)
        self.assertIn("job.tick()", self.source)
        self.assertIn("shutdown_calibration()", self.source)

    def test_sink_can_only_reach_servo_id_10(self) -> None:
        self.assertIn("calibration may only command id 10", self.source)


if __name__ == "__main__":
    unittest.main()
