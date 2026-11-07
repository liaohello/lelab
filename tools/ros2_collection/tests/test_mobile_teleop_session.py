"""State machine tests: every gate must be met before following, and no path may resume motion."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "mobile_teleop_session", str(REPO / "app_ros_mobile" / "lelab" / "mobile_teleop_session.py"))
session_module = importlib.util.module_from_spec(_spec)
sys.modules["mobile_teleop_session"] = session_module
_spec.loader.exec_module(session_module)

Gate = session_module.Gate
TeleopStateMachine = session_module.TeleopStateMachine

REFERENCE = {
    "leader_rad": {"joint1": 0.10, "joint2": 0.20, "joint3": -0.30, "joint4": 0.40,
                   "joint5": 0.05, "r_joint": 0.0},
    "follower_rad": {"joint1": 0.05, "joint2": 0.10, "joint3": -0.15, "joint4": 0.20,
                     "joint5": 0.02, "r_joint": 0.0},
    "gripper": {"leader_percent": 40.0, "pulse": 500.0},
    "limits_rad": {j: [-2.09, 2.09] for j in
                   ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]},
}


def full_gate(**overrides):
    kwargs = dict(leader_connected=True, leader_healthy=True, calibration_ready=True,
                  calibration_version="v1|v2|v3", upper_lease_valid=True, car_lease_valid=True,
                  follower_feedback_valid=True, exclusive_control=True, chassis_locked=True)
    kwargs.update(overrides)
    return Gate(**kwargs)


class TeleopStateMachineTests(unittest.TestCase):
    def build(self):
        cleared = []
        machine = TeleopStateMachine(on_clear_targets=lambda reason: cleared.append(reason))
        return machine, cleared

    def walk_to(self, machine, target, gate=None):
        gate = gate or full_gate()
        steps = [
            ("leader_found", gate),
            ("calibration_ready", gate),
            ("enter_preview", gate),
            ("align", gate),
            ("arm", gate),
            ("engage", gate),
        ]
        reached = "disconnected"
        for event, g in steps:
            detail = {"reference": REFERENCE} if event == "align" else {}
            snap = machine.event(event, g, detail)
            reached = snap["state"]
            if reached == target:
                return snap
        return snap

    def test_happy_path_reaches_following(self):
        machine, _ = self.build()
        snap = self.walk_to(machine, "following")
        self.assertEqual(snap["state"], "following", snap)
        self.assertTrue(snap["clutch_engaged"])
        self.assertTrue(snap["aligned"])

    def test_engage_is_denied_without_car_lease(self):
        machine, _ = self.build()
        gate = full_gate(car_lease_valid=False)
        snap = self.walk_to(machine, "following", gate)
        self.assertEqual(snap["state"], "preview", "car lease is required before align")
        self.assertIn("car_lease_invalid", snap["deny_reasons"])

    def test_engage_is_denied_with_stale_feedback(self):
        machine, _ = self.build()
        gate = full_gate(follower_feedback_valid=False)
        snap = self.walk_to(machine, "following", gate)
        self.assertNotEqual(snap["state"], "following")
        self.assertIn("follower_feedback_missing_or_stale", snap["deny_reasons"])

    def test_preview_requires_leader_and_lease_but_not_car_lease(self):
        machine, _ = self.build()
        gate = full_gate(car_lease_valid=False)
        snap = self.walk_to(machine, "preview", gate)
        self.assertEqual(snap["state"], "preview")

    def test_engage_denied_when_control_not_exclusive(self):
        machine, _ = self.build()
        gate = full_gate(exclusive_control=False)
        snap = self.walk_to(machine, "following", gate)
        self.assertIn("control_not_exclusive", snap["deny_reasons"])

    def test_chassis_must_stay_locked_to_follow(self):
        machine, _ = self.build()
        gate = full_gate(chassis_locked=False)
        snap = self.walk_to(machine, "following", gate)
        self.assertEqual(snap["state"], "armed")
        self.assertIn("chassis_must_stay_locked_in_phase_1", snap["deny_reasons"])

    def test_release_stops_following_and_forces_new_reference(self):
        machine, cleared = self.build()
        self.walk_to(machine, "following")
        snap = machine.event("release", full_gate())
        self.assertEqual(snap["state"], "paused")
        self.assertFalse(snap["clutch_engaged"])
        self.assertFalse(snap["aligned"])
        self.assertIn("clutch_released", cleared)
        re_engage = machine.event("engage", full_gate())
        self.assertNotEqual(re_engage["state"], "following")
        self.assertIn("realign_required_after_release", re_engage["deny_reasons"])

    def test_estop_drops_targets_and_recovery_never_resumes_motion(self):
        machine, cleared = self.build()
        self.walk_to(machine, "following")
        snap = machine.event("estop", full_gate())
        self.assertEqual(snap["state"], "estop")
        self.assertFalse(snap["clutch_engaged"])
        self.assertIn("estop", cleared)
        recovered = machine.event("estop_cleared", full_gate())
        self.assertEqual(recovered["state"], "preview", "recovery must require re-alignment")
        self.assertFalse(recovered["aligned"])
        engage = machine.event("engage", full_gate())
        self.assertNotEqual(engage["state"], "following")

    def test_leader_loss_resets_to_disconnected(self):
        machine, cleared = self.build()
        self.walk_to(machine, "following")
        snap = machine.event("leader_lost", full_gate(leader_connected=False, leader_healthy=False))
        self.assertEqual(snap["state"], "disconnected")
        self.assertIn("leader_lost", cleared)

    def test_calibration_change_invalidates_alignment(self):
        machine, cleared = self.build()
        self.walk_to(machine, "following")
        snap = machine.event("calibration_changed", full_gate())
        self.assertEqual(snap["state"], "calibrated")
        self.assertFalse(snap["aligned"])
        self.assertIn("calibration_changed", cleared)

    def test_guard_following_drops_when_lease_expires_mid_stream(self):
        machine, cleared = self.build()
        self.walk_to(machine, "following")
        ok, reasons = machine.guard_following(full_gate(upper_lease_valid=False))
        self.assertFalse(ok)
        self.assertIn("upper_lease_invalid", reasons)
        self.assertEqual(machine.state, "paused")
        self.assertIn("gate_lost:upper_lease_invalid", cleared)

    def test_align_rejects_reference_outside_limits(self):
        machine, _ = self.build()
        bad = dict(REFERENCE)
        bad["follower_rad"] = {**REFERENCE["follower_rad"], "joint2": 3.5}
        self.walk_to(machine, "preview")
        snap = machine.event("align", full_gate(), {"reference": bad})
        self.assertIn("alignment_reference_out_of_limits", snap["deny_reasons"])
        self.assertEqual(snap["state"], "preview")

    def test_align_requires_both_reference_sides(self):
        machine, _ = self.build()
        self.walk_to(machine, "preview")
        snap = machine.event("align", full_gate(), {"reference": {"follower_rad": {"joint1": 0.0}}})
        self.assertIn("alignment_reference_incomplete", snap["deny_reasons"])

    def test_align_records_calibration_version(self):
        machine, _ = self.build()
        gate = full_gate(calibration_version="aaa|bbb|ccc")
        self.walk_to(machine, "aligned", gate)
        self.assertEqual(machine.alignment["calibration_version"], "aaa|bbb|ccc")

    def test_unknown_event_is_denied(self):
        machine, _ = self.build()
        snap = machine.event("launch_missiles", full_gate())
        self.assertIn("unknown_event:launch_missiles", snap["deny_reasons"])

    def test_forward_only_transitions(self):
        machine, _ = self.build()
        gate = full_gate()
        snap = machine.event("arm", gate)
        self.assertEqual(snap["state"], "disconnected")
        self.assertIn("bad_transition:disconnected->armed", snap["deny_reasons"])

    def test_a_paused_session_can_reach_preview_again(self):
        # 七 makes ending an episode park following, so paused needs a legal way back; without one
        # the second episode of a session could never start, which the local rehearsal hit directly.
        machine, _ = self.build()
        gate = full_gate()
        self.walk_to(machine, "following", gate)
        machine.event("pause", gate)
        self.assertEqual(machine.state, "paused")
        denied = machine.event("engage", gate)
        self.assertIn("realign_required_after_release", denied["deny_reasons"],
                      "coming back may not skip establishing a new reference")
        snap = machine.event("enter_preview", gate)
        self.assertEqual(snap["state"], "preview", snap["deny_reasons"])

    def test_the_second_episode_engages_on_a_fresh_reference(self):
        machine, _ = self.build()
        gate = full_gate()
        self.walk_to(machine, "following", gate)
        machine.event("pause", gate)
        machine.event("enter_preview", gate)
        moved = {**REFERENCE, "follower_rad": {**REFERENCE["follower_rad"], "joint1": 0.30}}
        for event, detail in (("align", {"reference": moved}), ("arm", {}), ("engage", {})):
            snap = machine.event(event, gate, detail)
            self.assertEqual(snap["deny_reasons"], [], event)
        self.assertEqual(snap["state"], "following")
        self.assertEqual(snap["alignment"]["follower_rad"]["joint1"], 0.30,
                         "the re-engaged session must run from the new reference, not the parked one")


if __name__ == "__main__":
    unittest.main()
