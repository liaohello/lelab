"""Unit tests for the mobile teleop mapping, calibration binding and follower feedback gates."""
from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import importlib.util

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "lelab_mobile_agent"))

# Load the upper-computer module directly by path: importing the lelab package would pull in
# FastAPI and LeRobot, which the mapping itself does not need.
_spec = importlib.util.spec_from_file_location(
    "mobile_teleop", str(REPO / "app_ros_mobile" / "lelab" / "mobile_teleop.py"))
mobile_teleop = importlib.util.module_from_spec(_spec)
sys.modules["mobile_teleop"] = mobile_teleop  # dataclasses need the module registered
_spec.loader.exec_module(mobile_teleop)

from lelab_mobile_agent.follower_feedback import (  # noqa: E402
    FeedbackStore,
    JointSample,
    LateReplyGuard,
    pulse_to_rad,
    rad_to_pulse,
    clamp_pulse,
    gripper_width_m_for_pulse,
)
CalibrationStore = mobile_teleop.CalibrationStore  # noqa: E402
TeleopMapper = mobile_teleop.TeleopMapper  # noqa: E402

ARM = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]
LIMITS = {j: [-2.09, 2.09] for j in ARM[:5]}
LIMITS["r_joint"] = [-2.09, 2.09]

MAPPING = {
    "joint_order": ARM,
    "filter_alpha": 1.0,
    "max_step_rad_s": 100.0,
    "joints": [
        {"leader": "joint1", "follower": "joint1", "direction": 1, "scale": 1.0, "wrap": True,
         "soft_min_rad": -1.5, "soft_max_rad": 1.5},
        {"leader": "joint2", "follower": "joint2", "direction": 1, "scale": 0.5},
        {"leader": "joint3", "follower": "joint3", "direction": -1, "scale": 1.0,
         "soft_min_rad": -0.2, "soft_max_rad": 0.2},
        {"leader": "joint4", "follower": "joint4", "direction": 1, "scale": 1.0},
        {"leader": "joint5", "follower": "joint5", "direction": 1, "scale": 1.0},
        {"leader": "r_joint", "follower": "r_joint", "direction": -1, "scale": 1.0, "wrap": True},
    ],
    "gripper": {
        "follower_joint": "r_joint_grip",
        "pulse_span_per_hundred_pct": 6.0,
        "pulse_min": 100.0,
        "pulse_max": 900.0,
        "direction": -1,
        "flipped": True,
        "initial_position_raw": 500,
        "angle_zero": 200.0,
    },
}


class FollowerUnitTests(unittest.TestCase):
    def test_pulse_to_rad_matches_vendor_formula(self):
        self.assertAlmostEqual(pulse_to_rad(500), 0.0, places=9)
        self.assertAlmostEqual(pulse_to_rad(500 + 250), math.radians(60.0), places=6)
        self.assertAlmostEqual(pulse_to_rad(250, flipped=True), math.radians(60.0), places=6)
        self.assertAlmostEqual(rad_to_pulse(math.radians(60.0)), 750.0, places=6)
        self.assertAlmostEqual(rad_to_pulse(math.radians(60.0), flipped=True), 250.0, places=6)

    def test_pulse_clamp_bounds_full_sweep(self):
        self.assertEqual(clamp_pulse(-5), 0)
        self.assertEqual(clamp_pulse(1500), 999)
        self.assertEqual(clamp_pulse(400), 400)

    def test_gripper_width_is_monotonic_near_zero(self):
        wide = gripper_width_m_for_pulse(200)
        narrow = gripper_width_m_for_pulse(200 + 300)
        self.assertGreater(wide, narrow)
        self.assertAlmostEqual(gripper_width_m_for_pulse(200), gripper_width_m_for_pulse(200), places=9)


class FeedbackStoreTests(unittest.TestCase):
    def store(self):
        return FeedbackStore(
            expected_joints=ARM, stale_after_s=0.5, calibration_hash="cal-v1",
            servo_ids={"joint1": 1, "joint2": 2, "joint3": 3, "joint4": 4,
                       "joint5": 5, "r_joint": 10},
        )

    def ingest_trusted(self, store, wall: float) -> None:
        for servo_id, name in enumerate(ARM, start=1):
            store.ingest_sample(JointSample(
                name, 0.0, wall, "servo_position_readback",
                servo_id=10 if name == "r_joint" else servo_id,
                raw_position=500, sample_seq=1, valid=True,
                calibration_hash="cal-v1",
            ))

    def test_empty_store_blocks_following(self):
        store = self.store()
        ok, reason = store.gate(100.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_feedback_yet")

    def test_partial_feedback_reports_missing_joints(self):
        store = self.store()
        store.ingest_joint_states(ARM[:3], [0.1, 0.2, 0.3], 100.0, "controller_manager/joint_states")
        ok, reason = store.gate(100.1)
        self.assertFalse(ok)
        self.assertEqual(reason.split(":")[1].split(","), ["joint4", "joint5", "r_joint"])

    def test_full_feedback_then_expiry(self):
        store = self.store()
        self.ingest_trusted(store, 100.0)
        self.assertEqual(store.gate(100.2), (True, "ok"))
        snap = store.snapshot(100.2)
        self.assertEqual(snap["source"], ["servo_position_readback"])
        self.assertAlmostEqual(snap["oldest_age_s"], 0.2, places=3)
        stale_ok, stale_reason = store.gate(100.8)
        self.assertFalse(stale_ok)
        self.assertTrue(stale_reason.startswith("feedback_stale"))

    def test_out_of_order_and_unexpected_samples_are_rejected(self):
        store = self.store()
        store.ingest_joint_states(["joint1"], [0.5], 100.0, "src")
        store.ingest_joint_states(["joint1"], [0.9], 99.0, "src")  # older wall clock → rejected
        snap = store.snapshot(100.1)
        self.assertEqual(snap["positions_rad"]["joint1"], 0.5)
        self.assertGreaterEqual(snap["rejects"], 1)
        store.ingest_joint_states(["not_a_joint"], [1.0], 101.0, "src")
        self.assertNotIn("not_a_joint", store.snapshot(101.0)["positions_rad"])

    def test_clear_drops_state_so_following_never_resumes_on_stale_values(self):
        store = self.store()
        self.ingest_trusted(store, 100.0)
        self.assertTrue(store.gate(100.0)[0])
        store.clear("reconnect")
        ok, reason = store.gate(100.5)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_feedback_yet")

    def test_late_reply_guard_serializes_and_drops_stale_replies(self):
        guard = LateReplyGuard()
        gen1 = guard.begin()
        self.assertEqual(gen1, 1)
        self.assertIsNone(guard.begin(), "second concurrent request must be refused")
        accepted, _ = guard.finish(gen1, {"pulse": [1, 2]})
        self.assertTrue(accepted)
        gen2 = guard.begin()
        self.assertEqual(gen2, 2)
        accepted, _ = guard.finish(gen1, "very late")
        self.assertFalse(accepted, "a reply from an older generation must be dropped")
        self.assertIsNone(guard.begin(), "a dropped late reply must not free the in-flight slot")
        guard.abandon(gen2)
        self.assertIsNone(guard.begin(), "abandon must poison until a verified transport reset")
        guard.reset_after_transport_reset()
        self.assertEqual(guard.begin(), 3)


class TeleopMapperTests(unittest.TestCase):
    def mapper(self):
        return TeleopMapper(MAPPING, LIMITS)

    def test_relative_follow_law_direction_and_scale(self):
        m = self.mapper()
        out = m.joint_target("joint2", leader_rad=1.0, leader_ref_rad=0.4, follower_ref_rad=0.1, now=10.0)
        self.assertAlmostEqual(out["target_rad"], 0.1 + 0.5 * 0.6, places=6)
        self.assertFalse(out["clipped"])

    def test_normalized_delta_uses_calibrated_travel_ranges(self):
        travel = {
            "joint1": (-math.pi, math.pi),
            "joint2": (-math.pi, math.pi),
            "joint3": (-math.pi, math.pi),
            "joint4": (-math.pi, math.pi),
            "joint5": (-math.pi, math.pi),
        }
        m = TeleopMapper(MAPPING, LIMITS, leader_travel_rad=travel)
        out = m.joint_target(
            "joint4", leader_rad=0.5, leader_ref_rad=0.0,
            follower_ref_rad=0.0, now=10.0)
        self.assertEqual(out["normalization"], "leader_range_to_follower_range")
        self.assertAlmostEqual(out["leader_normalized_delta"], 0.5 / (2 * math.pi), places=6)
        self.assertAlmostEqual(
            out["pre_clip_rad"], (0.5 / (2 * math.pi)) * (2 * 2.09), places=6)

    def test_reversed_direction_joint(self):
        m = self.mapper()
        out = m.joint_target("joint3", leader_rad=0.5, leader_ref_rad=0.0, follower_ref_rad=0.0, now=10.0)
        self.assertAlmostEqual(out["pre_clip_rad"], -0.5, places=6)
        self.assertTrue(out["clipped"], "soft limit of 0.2 rad must clip the -0.5 rad demand")
        self.assertAlmostEqual(out["target_rad"], -0.2, places=6)

    def test_wrap_uses_shortest_path(self):
        m = self.mapper()
        out = m.joint_target("joint1", leader_rad=-3.0, leader_ref_rad=3.0, follower_ref_rad=0.0, now=10.0)
        self.assertAlmostEqual(out["leader_delta_rad"], 2 * math.pi - 6.0, places=3,
                               msg="-3.0 vs +3.0 rad must wrap to the short path, not 6 rad")
        self.assertLess(abs(out["target_rad"]), 0.5)

    def test_rate_limit_blocks_jumps(self):
        mapping = dict(MAPPING)
        mapping["max_step_rad_s"] = 0.5
        m = TeleopMapper(mapping, LIMITS)
        first = m.joint_target("joint4", 0.0, 0.0, 0.0, now=10.0)
        self.assertAlmostEqual(first["target_rad"], 0.0, places=6)
        jumped = m.joint_target("joint4", 5.0, 0.0, 0.0, now=10.1)
        self.assertLessEqual(abs(jumped["target_rad"]), 0.05 + 1e-9,
                             "0.1 s at 0.5 rad/s may not move more than 0.05 rad")

    def test_frame_shares_one_elapsed_time_across_all_axes(self):
        mapping = dict(MAPPING)
        mapping["max_step_rad_s"] = 0.5
        m = TeleopMapper(mapping, LIMITS)
        m.align({name: 0.0 for name in LIMITS}, now=0.0)
        targets = m.frame([
            {"follower": "joint1", "leader_rad": 1.0,
             "leader_ref_rad": 0.0, "follower_ref_rad": 0.0},
            {"follower": "joint2", "leader_rad": -1.0,
             "leader_ref_rad": 0.0, "follower_ref_rad": 0.0},
        ], now=1.0)
        self.assertEqual([item["joint"] for item in targets], ["joint1", "joint2"])
        self.assertAlmostEqual(targets[0]["target_rad"], 0.5, places=6)
        self.assertAlmostEqual(targets[1]["target_rad"], -0.5, places=6)

    def test_alignment_seeds_filter_and_rate_state_from_follower(self):
        mapping = dict(MAPPING)
        mapping["max_step_rad_s"] = 0.5
        m = TeleopMapper(mapping, LIMITS)
        m.align({"joint4": 0.4}, now=1.0, gripper_pulse=320.0)
        first = m.joint_target("joint4", 0.4, 0.0, 0.0, now=1.0)
        self.assertAlmostEqual(first["target_rad"], 0.4, places=6)
        blocked = m.joint_target("joint4", 1.4, 0.0, 0.0, now=1.1)
        self.assertAlmostEqual(blocked["target_rad"], 0.45, places=6,
                               msg="the first post-alignment step may not jump")

    def test_reset_forces_realignment(self):
        m = self.mapper()
        m.joint_target("joint4", 0.3, 0.0, 0.0, now=10.0)
        m.reset("calibration_change")
        out = m.joint_target("joint4", 0.3, 0.0, 0.0, now=10.5)
        self.assertAlmostEqual(out["target_rad"], 0.3, places=6,
                               msg="after reset the filter and rate memory must be empty")

    def test_deadband_suppresses_subthreshold_encoder_jitter(self):
        mapping = dict(MAPPING)
        m = TeleopMapper(mapping, LIMITS)
        m.align({"joint4": 0.4}, now=1.0)
        m.deadband_rad = 0.01
        held = m.joint_target("joint4", 0.405, 0.0, 0.0, now=1.1)
        self.assertAlmostEqual(held["target_rad"], 0.4, places=6)

    def test_unknown_joint_raises(self):
        with self.assertRaises(KeyError):
            self.mapper().joint_target("joint99", 0.0, 0.0, 0.0, now=1.0)

    def test_gripper_percent_to_pulse_to_radians_not_degrees_of_percent(self):
        m = self.mapper()
        out = m.gripper_target(percent=100.0, percent_ref=0.0, pulse_ref=500.0)
        self.assertEqual(out["pre_clip_pulse"], 500.0 - 100 * 6.0)
        self.assertEqual(out["target_pulse"], 100.0, "pulse_min 100 must clip")
        self.assertTrue(out["clipped"])
        self.assertAlmostEqual(out["target_rad"], (500 - 100) * ((240 / 360) * 2 * math.pi / 1000), places=6)
        self.assertEqual(out["pulse_unit"], "pulse")
        self.assertGreater(out["jaw_width_mm_estimate"], 0)

    def test_gripper_ema_smooths_pulse_jumps(self):
        mapping = dict(MAPPING)
        mapping["filter_alpha"] = 0.5
        m = TeleopMapper(mapping, LIMITS)
        m.align({}, now=1.0, gripper_pulse=500.0)
        out = m.gripper_target(percent=100.0, percent_ref=0.0, pulse_ref=500.0, now=1.1)
        self.assertEqual(out["pre_clip_pulse"], -100.0)
        self.assertEqual(out["target_pulse"], 300.0)

    def test_absolute_gripper_maps_zero_to_the_closed_endpoint(self):
        m = TeleopMapper(
            MAPPING, LIMITS, gripper_mapping_mode="absolute_normalized")
        closed = m.gripper_target(
            percent=0.0, percent_ref=50.0, pulse_ref=500.0)
        opened = m.gripper_target(
            percent=100.0, percent_ref=50.0, pulse_ref=500.0)
        self.assertEqual(closed["mapping_mode"], "absolute_normalized")
        self.assertEqual(closed["pre_clip_pulse"], 900.0)
        self.assertEqual(closed["target_pulse"], 900.0)
        self.assertEqual(opened["target_pulse"], 300.0)

    def test_absolute_gripper_uses_ticks_per_percent_without_a_second_divide(self):
        mapping = dict(MAPPING)
        mapping["gripper"] = {
            "pulse_span_per_hundred_pct": 2.96,
            "pulse_min": 200.0,
            "pulse_max": 496.0,
            "direction": -1,
            "flipped": True,
            "initial_position_raw": 500.0,
            "angle_zero": 200.0,
        }
        m = TeleopMapper(
            mapping, LIMITS, gripper_mapping_mode="absolute_normalized")
        out = m.gripper_target(percent=57.4, percent_ref=0.568, pulse_ref=492.0)
        self.assertAlmostEqual(out["pre_clip_pulse"], 496.0 - 57.4 * 2.96, places=6)
        self.assertAlmostEqual(out["target_pulse"], 326.096, places=6)

    def test_gripper_ignores_out_of_range_percent_safely(self):
        m = self.mapper()
        over = m.gripper_target(percent=140.0, percent_ref=0.0, pulse_ref=500.0)
        self.assertEqual(over["leader_percent"], 100.0)
        under = m.gripper_target(percent=-30.0, percent_ref=50.0, pulse_ref=500.0)
        self.assertEqual(under["leader_percent"], 0.0)


class CalibrationStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="mobile_cal_"))
        self.store = CalibrationStore(self.dir)
        self.leader = {"joint_order": ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"],
                       "device_identity": {"serial": "5B91045575", "model": "SO-101 leader"},
                       "reference_deg": [0, 0, 0, 0, 0, 0],
                       "gripper_range_percent": [0, 100]}
        self.follower = {"joint_order": ARM,
                         "device_identity": {"car": "raspberrypi", "servo_ids": {"joint1": 1, "r_joint": 10}},
                         "joint_order_units": {"rad": "controller_manager/joint_states",
                                               "pulse": "GetBusServoState.response.pulse"},
                         "zero_pulse": 500, "flipped_joints": ["r_joint"],
                         "soft_limits_rad": {j: [-1.5, 1.5] for j in ARM}}
        self.mapping = {"joint_order": ARM,
                        "bound_identities": {"leader": "leader_A", "follower": "car_A"},
                        "joints": MAPPING["joints"], "gripper": MAPPING["gripper"],
                        "model_versions": {"description_sha256": "abc"}}

    def test_save_load_roundtrip_with_version_and_hash(self):
        record = self.store.save("leader", "leader_A", self.leader)
        loaded = self.store.load("leader", "leader_A")
        self.assertEqual(loaded.sha256, record.sha256)
        self.assertEqual(loaded.version, record.version)
        self.assertTrue(record.sha256.startswith(loaded.data["content_sha256"]))
        self.assertEqual(loaded.data["device_identity"]["serial"], "5B91045575")

    def test_edit_is_reapplied_over_a_loaded_document(self):
        """The wizard loads a layer, edits it and posts it back - derived fields must not poison the hash."""
        self.store.save("mapping", "map_A", self.mapping)
        loaded = self.store.load("mapping", "map_A")
        edited = dict(loaded.data)
        edited["filter_alpha"] = 0.7
        again = self.store.save("mapping", "map_A", edited)
        reloaded = self.store.load("mapping", "map_A")
        self.assertEqual(reloaded.data["filter_alpha"], 0.7)
        self.assertEqual(reloaded.sha256, again.sha256)
        self.assertEqual(reloaded.data["kind"], "mapping")
        self.assertEqual(loaded.data["schema_version"], reloaded.data["schema_version"])

    def test_tampered_file_is_rejected(self):
        record = self.store.save("follower", "car_A", self.follower)
        path = record.path
        data = json.loads(path.read_text())
        data["soft_limits_rad"]["joint1"] = [-3.14, 3.14]
        path.write_text(json.dumps(data, indent=2))
        with self.assertRaises(ValueError):
            self.store.load("follower", "car_A")

    def test_readiness_ok_when_layers_agree(self):
        self.store.save("leader", "leader_A", self.leader)
        self.store.save("follower", "car_A", self.follower)
        self.store.save("mapping", "map_A", self.mapping)
        ready = self.store.readiness("leader_A", "car_A", "map_A")
        self.assertTrue(ready["ready"], ready)
        self.assertEqual(len(ready["calibration_version"].split("|")), 3)

    def test_readiness_blocks_swapped_leader_device(self):
        self.store.save("leader", "leader_B", self.leader)
        self.store.save("follower", "car_A", self.follower)
        self.store.save("mapping", "map_A", self.mapping)
        ready = self.store.readiness("leader_B", "car_A", "map_A")
        self.assertFalse(ready["ready"])
        self.assertIn("mapping binds leader='leader_A'", " ".join(ready["reasons"]))

    def test_readiness_blocks_joint_order_mismatch(self):
        follower = dict(self.follower)
        follower["joint_order"] = list(reversed(ARM))
        self.store.save("leader", "leader_A", self.leader)
        self.store.save("follower", "car_A", follower)
        self.store.save("mapping", "map_A", self.mapping)
        ready = self.store.readiness("leader_A", "car_A", "map_A")
        self.assertFalse(ready["ready"])
        self.assertIn("joint_order", " ".join(ready["reasons"]))

    def test_missing_layer_is_reported_not_silently_defaulted(self):
        self.store.save("leader", "leader_A", self.leader)
        ready = self.store.readiness("leader_A", "car_A", "map_A")
        self.assertFalse(ready["ready"])
        self.assertIn("follower calibration missing", ready["reasons"][0])

    def test_atomic_write_leaves_no_temp_files(self):
        self.store.save("leader", "leader_A", self.leader)
        leftovers = list((self.dir / "leader").glob(".cal-*"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
