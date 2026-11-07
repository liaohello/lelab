import copy
import math

import pytest

from lelab.hybrid_home_plan import HOME_RAW, JOINT_IDS, plan_home_step


def _inputs():
    raw = {"joint1": 541, "joint2": 630, "joint3": 76, "joint4": 118,
           "joint5": 907, "r_joint": 457}
    samples = {name: {"valid": True, "error": None, "source_kind": "servo_position_readback",
                      "servo_id": servo_id, "calibration_hash": "bundle",
                      "age_s": 0.1, "raw_position": raw[name]}
               for name, servo_id in JOINT_IDS.items()}
    asset = {"raw_to_rad": {name: {"centre_raw": 500,
                                    "direction": -1 if name == "r_joint" else 1,
                                    "radians_per_tick": math.pi / 750}
                            for name in JOINT_IDS},
             "soft_limits_rad": {name: [-2.1, 2.1] for name in JOINT_IDS}}
    asset["soft_limits_rad"]["r_joint"] = [0.016, 1.257]
    return {"available": True, "joint_samples": samples}, asset


def test_requested_pose_is_planned_from_live_raw_without_output():
    feedback, asset = _inputs()
    result = plan_home_step(feedback, asset, bundle_hash="bundle")
    assert result["joint"] == "joint1"
    assert result["step_target_raw"] == 531
    assert result["final_target_raw"] == HOME_RAW
    assert result["motion_authorized"] is False
    assert result["requires_new_feedback"] is True


def test_each_next_step_uses_a_new_actual_measurement():
    feedback, asset = _inputs()
    feedback["joint_samples"]["joint1"]["raw_position"] = HOME_RAW["joint1"]
    result = plan_home_step(feedback, asset, bundle_hash="bundle")
    assert result["joint"] == "joint2" and result["step_target_raw"] == 620
    feedback["joint_samples"]["joint2"]["raw_position"] = 627
    assert plan_home_step(feedback, asset, bundle_hash="bundle")["step_target_raw"] == 617


def test_live_servo_deadband_bias_within_five_ticks_is_complete():
    feedback, asset = _inputs()
    for name, target in HOME_RAW.items():
        feedback["joint_samples"][name]["raw_position"] = target
    feedback["joint_samples"]["joint1"]["raw_position"] = HOME_RAW["joint1"] + 4
    result = plan_home_step(
        feedback, asset, bundle_hash="bundle", tolerance_raw=5)
    assert result["complete"] is True


def test_single_axis_target_keeps_other_five_at_measured_raw():
    feedback, asset = _inputs()
    targets = {name: int(sample["raw_position"])
               for name, sample in feedback["joint_samples"].items()}
    targets["joint1"] = 500
    result = plan_home_step(
        feedback, asset, bundle_hash="bundle", targets_raw=targets)
    assert result["joint"] == "joint1"
    assert result["step_target_raw"] == 531
    assert result["final_target_raw"] == targets


@pytest.mark.parametrize("key,bad", [("age_s", 0.51), ("age_s", float("nan")),
    ("source_kind", "command_cache"), ("servo_id", 1), ("calibration_hash", "other"),
    ("raw_position", float("inf")), ("valid", False)])
def test_refuses_stale_or_untrusted_feedback(key, bad):
    feedback, asset = _inputs()
    feedback["joint_samples"]["joint2"][key] = bad
    with pytest.raises(ValueError, match="invalid_feedback:joint2"):
        plan_home_step(feedback, asset, bundle_hash="bundle")


def test_rejects_outside_limits_and_does_not_confuse_gripper_units():
    feedback, asset = _inputs()
    asset["soft_limits_rad"]["r_joint"] = [0.2, 0.5]
    with pytest.raises(ValueError, match="position_outside_calibrated_range:r_joint"):
        plan_home_step(feedback, asset, bundle_hash="bundle")
    asset["soft_limits_rad"]["r_joint"] = [0.016, 1.257]
    for name in JOINT_IDS:
        feedback["joint_samples"][name]["raw_position"] = HOME_RAW[name]
    result = plan_home_step(feedback, asset, bundle_hash="bundle")
    assert result["complete"] is True and result["motion_authorized"] is False


def test_plan_does_not_modify_feedback_or_saved_calibration():
    feedback, asset = _inputs()
    before = copy.deepcopy((feedback, asset))
    plan_home_step(feedback, asset, bundle_hash="bundle")
    assert (feedback, asset) == before
