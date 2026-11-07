"""The deliverable schema examples must actually load, agree on joint order and drive the mapper."""
from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "app_ros_mobile"))

from lelab.mobile_teleop import RADIANS_PER_PULSE, CalibrationStore, TeleopMapper  # noqa: E402

EXAMPLES = REPO / "configs" / "mobile_teleop"
FOLLOWER_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]


def load(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class ExampleCalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.leader = load("leader.example.json")
        cls.follower = load("follower.example.json")
        cls.mapping = load("mapping.example.json")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="teleop_examples_")
        self.store = CalibrationStore(Path(self.dir))
        self.identities = (self.leader["identity_hint"], self.follower["identity_hint"],
                           self.mapping["identity_hint"])
        self.store.save("leader", self.identities[0], self.leader)
        self.store.save("follower", self.identities[1], self.follower)
        self.store.save("mapping", self.identities[2], self.mapping)

    def test_examples_are_marked_as_not_measured(self):
        self.assertTrue(self.leader.get("_example_not_measured"),
                        "a shipped example must never look like a real calibration")
        blob = json.dumps([self.follower, self.mapping], ensure_ascii=False)
        self.assertIn("example_not_measured", blob)

    def test_the_layers_agree_structurally_but_an_unedited_example_is_not_ready(self):
        """The examples must line up on identity and joint order, yet still be refused by the gate.

        This file used to assert readiness("ready" is True) on the shipped templates while another
        test in the same file insisted an example must never look like a real calibration. The gate
        now refuses unedited markers, so both halves are stated here instead of contradicting.
        """
        ready = self.store.readiness(*self.identities)
        structural = [r for r in ready["reasons"] if "shipped example values" not in r]
        self.assertEqual(structural, [], "the example layers must agree on identity and joint order")
        self.assertFalse(ready["ready"], "loading the example and saving it unchanged must not enable following")
        self.assertEqual(self.mapping["joint_order"], FOLLOWER_JOINTS)
        self.assertEqual(self.follower["joint_order"], self.mapping["joint_order"])
        self.assertEqual([j["leader"] for j in self.mapping["joints"]], self.leader["joint_order"])
        self.assertEqual(len(ready["calibration_version"].split("|")), 3)

    def test_follower_hardware_limits_match_the_vendor_pulse_conversion(self):
        for joint in FOLLOWER_JOINTS:
            low, high = self.follower["limits_rad"][joint]
            self.assertAlmostEqual(low, -500 * RADIANS_PER_PULSE, places=6)
            self.assertAlmostEqual(high, 500 * RADIANS_PER_PULSE, places=6)
            slow, shigh = self.follower["soft_limits_rad"][joint]
            self.assertGreaterEqual(slow, low, "soft limits must sit inside the hardware envelope")
            self.assertLessEqual(shigh, high)

    def test_only_real_actuators_are_independent_control_quantities(self):
        self.assertEqual(sorted(self.follower["independent_control_joints"]), sorted(FOLLOWER_JOINTS))
        self.assertNotIn("gripper_base_joint", self.follower["independent_control_joints"])
        self.assertNotIn("w_joint", self.follower["joint_order"])
        mapped = [j["follower"] for j in self.mapping["joints"]]
        mapped.append(self.mapping["gripper"]["follower_joint"])
        self.assertEqual(sorted(set(mapped)), sorted(FOLLOWER_JOINTS))

    def test_joint_targets_respect_direction_scale_and_soft_limits(self):
        mapper = TeleopMapper(self.mapping, self.follower["limits_rad"])
        out = mapper.joint_target("joint2", leader_rad=math.radians(20), leader_ref_rad=0.0,
                                  follower_ref_rad=0.0, now=1.0)
        self.assertEqual(out["direction"], -1.0)
        self.assertAlmostEqual(out["pre_clip_rad"], -0.9 * math.radians(20), places=6)
        self.assertFalse(out["clipped"])
        far = mapper.joint_target("joint2", leader_rad=math.radians(200), leader_ref_rad=0.0,
                                  follower_ref_rad=0.0, now=2.0)
        self.assertTrue(far["clipped"], "a large leader move must be clamped, not sent")
        self.assertLessEqual(abs(far["target_rad"]), 1.6 + 1e-6)

    def test_wrap_is_enabled_only_for_the_continuously_turning_leader_joint(self):
        wrapped = {j["follower"] for j in self.mapping["joints"] if j.get("wrap")}
        self.assertEqual(wrapped, {"joint5"},
                         "only wrist_roll crosses +-pi on the leader; the limited joints must clamp instead")

    def test_continuous_leader_joint_delta_wraps_to_the_shortest_path(self):
        mapper = TeleopMapper(self.mapping, self.follower["limits_rad"])
        out = mapper.joint_target("joint5", leader_rad=math.pi - 0.1, leader_ref_rad=-math.pi + 0.1,
                                  follower_ref_rad=0.0, now=1.0)
        self.assertAlmostEqual(out["leader_delta_rad"], -0.2, places=6)
        self.assertLess(abs(out["pre_clip_rad"]), 0.25, "wrap must prevent a ~2pi jump")

    def test_limited_leader_joint_over_wraps_instead_of_reversing(self):
        # shoulder_pan is a limited servo, so a 6.6 rad demand must saturate toward the commanded
        # direction. With wrap on it took the "shortest path" and drove the base the other way.
        mapper = TeleopMapper(self.mapping, self.follower["limits_rad"])
        out = mapper.joint_target("joint1", leader_rad=3.3, leader_ref_rad=-3.3,
                                  follower_ref_rad=0.0, now=1.0)
        self.assertGreater(out["pre_clip_rad"], 0.0, "must stay in the commanded direction")
        self.assertTrue(out["clipped"])
        self.assertLessEqual(out["target_rad"], 2.0 + 1e-9)

    def test_gripper_percent_is_converted_through_pulses_never_as_an_angle(self):
        mapper = TeleopMapper(self.mapping, self.follower["limits_rad"])
        closed = mapper.gripper_target(percent=0.0, percent_ref=0.0, pulse_ref=700.0, now=1.0)
        opened = mapper.gripper_target(percent=100.0, percent_ref=0.0, pulse_ref=700.0, now=1.1)
        self.assertEqual(closed["target_pulse"], 700.0)
        self.assertEqual(opened["target_pulse"], 200.0, "direction -1 opens the jaw as percent grows")
        self.assertFalse(opened["clipped"])
        # r_joint is the flipped servo: pulse 200 -> +rad, pulse 700 -> -rad around the 500 zero
        self.assertAlmostEqual(opened["target_rad"], (500 - 200) * RADIANS_PER_PULSE, places=5)
        self.assertAlmostEqual(closed["target_rad"], (500 - 700) * RADIANS_PER_PULSE, places=5)
        self.assertGreater(opened["jaw_width_mm_estimate"], closed["jaw_width_mm_estimate"])
        self.assertGreater(closed["jaw_width_mm_estimate"], 0.0,
                           "the example pulse range must stay inside the geometrically valid jaw travel")
        beyond = mapper.gripper_target(percent=100.0, percent_ref=0.0, pulse_ref=100.0, now=1.2)
        self.assertEqual(beyond["target_pulse"], 200.0, "pulse_min must hold the jaws in geometry")
        self.assertTrue(beyond["clipped"])

    def test_leader_percent_range_is_declared_as_percent_not_degrees(self):
        units = self.leader["units"]
        self.assertIn("degrees", units["joints"])
        self.assertIn("percent", units["gripper"])
        self.assertIn("percent", self.mapping["gripper"]["leader_semantic"])


if __name__ == "__main__":
    unittest.main()
