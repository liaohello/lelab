"""Read-only planning for an operator-requested follower raw pose.

This module cannot publish or enable motion. Callers must obtain a new measured
snapshot for each step and use the existing session/lease and stop machinery.
"""
from __future__ import annotations

import math
from typing import Any

JOINT_IDS = {"joint1": 1, "joint2": 2, "joint3": 3, "joint4": 4,
             "joint5": 5, "r_joint": 10}
# Operator-confirmed parked pose measured with all six servos unloaded on
# 2026-11-07. This is the recovery target before teleoperation, not a generic
# mechanical midpoint.
HOME_RAW = {"joint1": 531, "joint2": 492, "joint3": 525,
            "joint4": 502, "joint5": 510, "r_joint": 457}


def raw_to_rad(raw: int, calibration: dict[str, Any]) -> float:
    """Convert one vendor raw tick through the saved follower identity."""
    if type(raw) is not int or not 0 <= raw <= 1000:
        raise ValueError("raw_out_of_range")
    try:
        direction = float(calibration["direction"])
        centre = float(calibration["centre_raw"])
        scale = float(calibration["radians_per_tick"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid_calibration") from exc
    if (not all(math.isfinite(value) for value in (direction, centre, scale))
            or direction not in (-1.0, 1.0) or scale <= 0):
        raise ValueError("invalid_calibration")
    return (float(raw) - centre) * scale * direction


def plan_home_step(feedback: dict[str, Any], asset: dict[str, Any], *,
                   bundle_hash: str, max_step_raw: int = 10,
                   tolerance_raw: int = 2,
                   targets_raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return one bounded proposal based on six fresh, identity-bound measurements.

    A proposal is not permission to move and is never a queued trajectory. Once a
    segment is executed, discard this proposal and replan from a new scan.
    """
    if not isinstance(bundle_hash, str) or not bundle_hash:
        raise ValueError("calibration_bundle_missing")
    if type(max_step_raw) is not int or not 1 <= max_step_raw <= 20:
        raise ValueError("step_out_of_range")
    if type(tolerance_raw) is not int or not 0 <= tolerance_raw <= 5:
        raise ValueError("tolerance_out_of_range")
    targets = dict(HOME_RAW) if targets_raw is None else dict(targets_raw)
    if (set(targets) != set(JOINT_IDS)
            or any(type(value) is not int or not 0 <= value <= 1000
                   for value in targets.values())):
        raise ValueError("targets_out_of_range")
    # The car status uses ``available``; the upper observation projection keeps the
    # same fact under ``input_available``. Either spelling is acceptable, but a
    # present false value must never be treated as usable.
    if (feedback.get("available") is False
            or feedback.get("input_available") is False
            or (feedback.get("available") is not True
                and feedback.get("input_available") is not True)):
        raise ValueError("feedback_unavailable")
    samples = feedback.get("joint_samples")
    conversion = asset.get("raw_to_rad")
    limits = asset.get("soft_limits_rad")
    if not isinstance(samples, dict) or not isinstance(conversion, dict) or not isinstance(limits, dict):
        raise ValueError("feedback_or_calibration_incomplete")
    current: dict[str, int] = {}
    for name, servo_id in JOINT_IDS.items():
        sample, cfg, bounds = samples.get(name), conversion.get(name), limits.get(name)
        if not isinstance(sample, dict) or not isinstance(cfg, dict):
            raise ValueError(f"missing_joint:{name}")
        age = sample.get("age_s")
        raw = sample.get("raw_position")
        if (sample.get("valid") is not True or sample.get("error") is not None
                or sample.get("source_kind") != "servo_position_readback"
                or sample.get("servo_id") != servo_id
                or sample.get("calibration_hash") != bundle_hash
                or isinstance(age, bool) or not isinstance(age, (int, float))
                or not math.isfinite(age) or not 0 <= age <= 0.5
                or isinstance(raw, bool) or not isinstance(raw, (int, float))
                or not math.isfinite(raw) or raw != int(raw) or not 0 <= raw <= 1000):
            raise ValueError(f"invalid_feedback:{name}")
        try:
            direction = float(cfg["direction"])
            centre = float(cfg["centre_raw"])
            scale = float(cfg["radians_per_tick"])
            low, high = (float(x) for x in bounds)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid_calibration:{name}") from exc
        if (not all(math.isfinite(x) for x in (direction, centre, scale, low, high))
                or direction not in (-1.0, 1.0) or scale <= 0 or low >= high):
            raise ValueError(f"invalid_calibration:{name}")
        current_rad = raw_to_rad(int(raw), cfg)
        desired_rad = raw_to_rad(targets[name], cfg)
        if not (low <= current_rad <= high and low <= desired_rad <= high):
            raise ValueError(f"position_outside_calibrated_range:{name}")
        current[name] = int(raw)
    for name, goal in targets.items():
        delta = goal - current[name]
        if abs(delta) > tolerance_raw:
            proposed = current[name] + max(-max_step_raw, min(max_step_raw, delta))
            return {"complete": False, "joint": name, "servo_id": JOINT_IDS[name],
                    "observed_raw": dict(current), "step_target_raw": proposed,
                    "final_target_raw": dict(targets), "remaining_raw": delta,
                    "max_step_raw": max_step_raw,
                    "motion_authorized": False, "requires_new_feedback": True}
    return {"complete": True, "observed_raw": current, "final_target_raw": dict(targets),
            "tolerance_raw": tolerance_raw, "motion_authorized": False}
