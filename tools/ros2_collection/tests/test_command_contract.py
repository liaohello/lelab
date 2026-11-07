"""Car-side command-envelope contract, tested without a ROS graph.

These are the rejections the upper side cannot fake its way past: an old session, a replayed or
out-of-order sequence number, an unauthorised source, and the phase-1 chassis lock. They run
against the shipped teleop profile so a profile edit that silently widens the envelope fails here.
"""
from __future__ import annotations

import math
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "lelab_mobile_agent"))

from lelab_mobile_agent.contract import (TELEOP_STATE_FIELDS, CommandGate, ContractError,  # noqa: E402
                                      load_profile, teleop_state_payload)

TELEOP_PROFILE = REPO / "configs" / "ros2_collection" / "hardware-teleop-v1.json"
SESSION = "ros_20261103T000000Z_deadbeef"


def gate(profile=None, session_id=SESSION):
    return CommandGate(profile or load_profile(TELEOP_PROFILE), session_id)


def arm_payload(seq, positions=(0.1, 0.2, 0.3, 0.4, 0.5), *, duration=0.05, **extra):
    payload = {"session_id": SESSION, "seq": seq, "source": "leader_arm", "mode": "leader_arm",
               "ttl_s": 0.05, "calibration_version": "abc123", "clutch": "engaged",
               "arm": {"positions": list(positions), "duration_s": duration}}
    return {**payload, **extra}


class EnvelopeTests(unittest.TestCase):
    def test_matching_session_and_increasing_seq_are_accepted(self):
        g = gate()
        first = g.arm(arm_payload(0), "leader_arm")
        second = g.arm(arm_payload(1), "leader_arm")
        self.assertEqual(len(first.positions), 5)
        self.assertEqual(g.last_seq, 1)
        self.assertEqual(second.positions[0], 0.1)

    def test_command_from_another_session_is_refused(self):
        """A page or process that lost the lease must not keep steering with its old session id."""
        g = gate()
        g.arm(arm_payload(0), "leader_arm")
        with self.assertRaises(ContractError) as ctx:
            g.arm(arm_payload(1, session_id="ros_some_other_session"), "leader_arm")
        self.assertEqual(ctx.exception.reason, "session_mismatch")

    def test_replayed_and_out_of_order_sequences_are_refused(self):
        """This is the 'recovery must not replay old actions' mechanism (八.6)."""
        g = gate()
        g.arm(arm_payload(5), "leader_arm")
        for seq in (5, 4, 0):
            with self.assertRaises(ContractError) as ctx:
                g.arm(arm_payload(seq), "leader_arm")
            self.assertEqual(ctx.exception.reason, "stale_seq")

    def test_a_refused_command_does_not_advance_the_sequence_watermark(self):
        """Otherwise a rejected frame could unlock a later stale replay."""
        g = gate()
        g.arm(arm_payload(7), "leader_arm")
        with self.assertRaises(ContractError):
            g.arm(arm_payload(3, session_id="other"), "leader_arm")
        self.assertEqual(g.last_seq, 7)
        with self.assertRaises(ContractError) as ctx:
            g.arm(arm_payload(4), "leader_arm")
        self.assertEqual(ctx.exception.reason, "stale_seq")

    def test_missing_or_non_integer_seq_is_refused(self):
        for bad in ("1", 1.0, True, None, -1):
            with self.subTest(seq=bad):
                with self.assertRaises(ContractError) as ctx:
                    gate().arm({**arm_payload(0), "seq": bad}, "leader_arm")
                self.assertEqual(ctx.exception.reason, "bad_seq")

    def test_source_not_in_the_profile_is_refused(self):
        g = gate()
        with self.assertRaises(ContractError) as ctx:
            g.arm(arm_payload(0, source="vendor_joystick"), "leader_arm")
        self.assertEqual(ctx.exception.reason, "bad_source")

    def test_mode_mismatch_is_refused(self):
        g = gate()
        with self.assertRaises(ContractError) as ctx:
            g.arm(arm_payload(0), "keyboard")
        self.assertEqual(ctx.exception.reason, "mode_mismatch")


class ArmAndGripperTests(unittest.TestCase):
    def test_out_of_limit_positions_are_clipped_and_reported(self):
        out = gate().arm(arm_payload(0, positions=(9.9, -9.9, 0.0, 0.0, 0.0)), "leader_arm")
        self.assertEqual(out.positions, [2.09, -2.09, 0.0, 0.0, 0.0])
        self.assertEqual(out.clipped, ["joint1", "joint2"])

    def test_wrong_joint_count_and_non_finite_values_are_refused(self):
        g = gate()
        with self.assertRaises(ContractError) as ctx:
            g.arm(arm_payload(0, positions=(0.1, 0.2, 0.3)), "leader_arm")
        self.assertEqual(ctx.exception.reason, "bad_arm_shape")
        with self.assertRaises(ContractError) as ctx:
            g.arm(arm_payload(1, positions=(math.nan, 0.0, 0.0, 0.0, 0.0)), "leader_arm")
        self.assertEqual(ctx.exception.reason, "not_finite")
        with self.assertRaises(ContractError) as ctx:
            g.arm(arm_payload(2, positions=("x", 0.0, 0.0, 0.0, 0.0)), "leader_arm")
        self.assertEqual(ctx.exception.reason, "not_numeric")

    def test_trajectory_duration_is_clamped_to_the_profile_envelope(self):
        too_long = gate().arm(arm_payload(0, duration=30.0), "leader_arm")
        self.assertEqual(too_long.duration_s, 2.0)
        too_short = gate().arm(arm_payload(0, duration=0.001), "leader_arm")
        self.assertEqual(too_short.duration_s, 0.1)

    def test_gripper_is_clipped_to_the_installed_range(self):
        g = gate()
        g.arm(arm_payload(0), "leader_arm")
        cmd = g.gripper({"session_id": SESSION, "seq": 1, "source": "leader_arm",
                         "mode": "leader_arm", "ttl_s": 0.05,
                         "gripper": {"position": 5.0, "duration_s": 0.6}}, "leader_arm")
        self.assertEqual(cmd.positions, [2.09])
        self.assertEqual(cmd.clipped, ["gripper_base"])


class ArmStreamTtlTests(unittest.TestCase):
    """`arm_command_ttl_s` must mean something on the car's own monotonic clock.

    Without this the page heartbeat alone keeps the session alive and a stalled follow loop would
    look like a healthy stream; detection here is what makes "上位机停止发包" visible as such.
    """

    def test_no_arm_command_yet_is_reported_as_not_expected(self):
        self.assertEqual(gate().arm_stream()["expected"], False)
        self.assertEqual(gate().arm_stream()["live"], False)

    def test_accepted_arm_command_opens_the_window_for_the_profile_ttl(self):
        g = gate()
        g.arm(arm_payload(0, ttl_s=5.0), "leader_arm")
        stream = g.arm_stream()
        self.assertEqual(stream["ttl_s"], 0.6, "the profile cap, not the client's request")
        self.assertTrue(stream["live"])
        self.assertLessEqual(stream["remaining_s"], 0.6)

    def test_the_window_closes_on_the_monotonic_clock(self):
        g = gate()
        g.arm(arm_payload(0), "leader_arm")
        deadline = g.arm_hold_deadline
        self.assertFalse(g.arm_stream(now=deadline + 0.001)["live"])
        self.assertTrue(g.arm_stream(now=deadline - 0.001)["live"])

    def test_rejected_command_does_not_extend_the_window(self):
        g = gate()
        g.arm(arm_payload(0), "leader_arm")
        deadline = g.arm_hold_deadline
        with self.assertRaises(ContractError):
            g.arm(arm_payload(0), "leader_arm")  # stale seq
        self.assertEqual(g.arm_hold_deadline, deadline)

    def test_a_shorter_declared_ttl_is_honoured(self):
        g = gate()
        g.arm(arm_payload(0, ttl_s=0.2), "leader_arm")
        self.assertLess(g.arm_hold_deadline - time.monotonic(), 0.4)

    def test_gripper_commands_keep_the_same_window(self):
        g = gate()
        g.gripper({"session_id": SESSION, "seq": 0, "source": "leader_arm", "mode": "leader_arm",
                   "ttl_s": 0.5, "gripper": {"position": 0.3, "duration_s": 0.6}}, "leader_arm")
        self.assertTrue(g.arm_stream()["live"])


class TeleopRecordingTests(unittest.TestCase):
    """The recorded teleop row is what separates 'published' from 'accepted' from 'moved'."""

    def payload(self, **over):
        args = dict(now_ns=1730000000000000000, sim_ns=None, clock_domain="wall",
                    control_mode="leader_arm", leader_arm_enabled=True,
                    last_teleop={"leader_rad": {"joint1": 0.5}, "gripper_percent": 25.0,
                                 "pre_clip_rad": [3.0, 0.1, 0.1, 0.1, 0.1],
                                 "calibration_version": "abc123", "clutch": "engaged"},
                    last_arm_targets=[2.09, 0.1, 0.1, 0.1, 0.1], last_gripper_target=0.4,
                    last_action_seq=7, feedback_ok=True,
                    feedback={"available": True, "reason": None,
                              "positions_rad": {"joint1": 2.05}, "age_s": 0.02,
                              "missing_joints": [], "source": ["/controller_manager/joint_states"],
                              "servo_pulses": {"joint1": 998}},
                    applied=[True, False, True, True], last_reject=None)
        return teleop_state_payload(**{**args, **over})

    def test_every_documented_field_is_present(self):
        payload = self.payload()
        self.assertEqual(sorted(payload), sorted(TELEOP_STATE_FIELDS))

    def test_reading_pre_clamp_and_sent_are_three_separate_records(self):
        payload = self.payload()
        self.assertEqual(payload["leader_positions_rad"], {"joint1": 0.5})
        self.assertEqual(payload["pre_clip_target_rad"], [3.0, 0.1, 0.1, 0.1, 0.1])
        self.assertEqual(payload["sent_target_rad"], [2.09, 0.1, 0.1, 0.1, 0.1])
        self.assertEqual(payload["semantics"]["executed"],
                         "not directly observable; inferred only from later feedback")
        self.assertNotIn("executed_rad", payload, "no field may claim measured execution")

    def test_no_command_yet_yields_missing_not_zero_filled(self):
        payload = self.payload(last_teleop=None, last_arm_targets=None, last_gripper_target=None)
        self.assertIsNone(payload["leader_positions_rad"])
        self.assertIsNone(payload["pre_clip_target_rad"])
        self.assertIsNone(payload["calibration_version"])
        self.assertEqual(payload["sent_target_rad"], [])
        self.assertIsNone(payload["sent_gripper_rad"])

    def test_feedback_provenance_and_ack_window_are_carried(self):
        payload = self.payload()
        self.assertTrue(payload["feedback"]["valid"])
        self.assertEqual(payload["feedback"]["source"], ["/controller_manager/joint_states"])
        self.assertEqual(payload["feedback"]["servo_pulses"], {"joint1": 998})
        self.assertEqual(payload["ack"], [False, True, True], "only the last three observations")

    def test_the_readonly_profile_is_not_widened_for_teleoperation(self):
        """需求七: the new capability must not relax the accepted read-only profile."""
        readonly = load_profile(REPO / "configs" / "ros2_collection" / "hardware-readonly-v1.json")
        self.assertNotIn("leader_arm", (readonly.get("modes") or {}).get("arm_sources", []))
        self.assertEqual(readonly.get("runtime", {}).get("mode"), "hardware_readonly")
        self.assertTrue(readonly.get("read_only", False) or readonly.get("profile", "")
                        .startswith("hardware_readonly"))


class ProfileRecordingContractTests(unittest.TestCase):
    """Whatever the exporter consumes must be in the recorded topic set, in both profile copies."""

    def test_shipped_teleop_profiles_agree_between_repo_and_package(self):
        a = load_profile(REPO / "configs" / "ros2_collection" / "hardware-teleop-v1.json")
        b = load_profile(REPO / "ros2_ws" / "src" / "lelab_mobile_agent" / "config"
                         / "hardware-teleop-v1.json")
        for key in ("recording", "teleop", "limits", "modes"):
            self.assertEqual(a[key], b[key], f"the two teleop profiles disagree on {key}")

    def test_every_export_input_is_recorded(self):
        profile = load_profile(TELEOP_PROFILE)
        recorded = set(profile["recording"]["topics"])
        needed = {
            "/lelab/arm/trajectory_request",     # arm action source
            "/lelab/gripper/trajectory_request",  # gripper action source
            "/controller_manager/joint_states",   # measured follower state
            "/lelab/events",                      # episode + gateway accept/reject
            "/ascamera/camera_publisher/rgb0/image",
            "/ascamera/camera_publisher/rgb0/camera_info",
            profile["teleop"]["state_topic"],      # leader reading / pre-clamp / sent target
        }
        self.assertEqual(sorted(needed - recorded), [], "exporter needs topics the bag will not hold")
        self.assertTrue(set(profile["recording"]["required_topics"]) <= recorded,
                        "required_topics must be a subset of what is actually recorded")


class ChassisLockTests(unittest.TestCase):
    def test_phase_one_profile_admits_no_base_source(self):
        """底盘锁定 is a profile fact, not a page detail: nothing may queue a velocity."""
        profile = load_profile(TELEOP_PROFILE)
        self.assertEqual(profile["modes"]["base"], [])
        self.assertEqual(
            [profile["limits"][k] for k in ("base_vx_mps", "base_vy_mps", "base_wz_radps")],
            [0.0, 0.0, 0.0])
        g = gate(profile)
        with self.assertRaises(ContractError) as ctx:
            g.base({"session_id": SESSION, "seq": 0, "source": "keyboard", "mode": "keyboard",
                    "base": {"vx": 0.1, "vy": 0.0, "wz": 0.0}}, "keyboard")
        self.assertEqual(ctx.exception.reason, "bad_source")


if __name__ == "__main__":
    unittest.main()
