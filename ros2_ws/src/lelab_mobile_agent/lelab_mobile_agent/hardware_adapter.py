"""Explicit hardware-facing adapter; creates no ROS nodes, ports or motion by itself."""
from __future__ import annotations

import math
from typing import Any

from .hardware_safety import SafetySupervisor


class HardwareAdapter:
    def __init__(self, supervisor: SafetySupervisor, feedback: Any):
        self.supervisor, self.feedback = supervisor, feedback

    def capabilities(self) -> dict[str, Any]:
        return {"outputs_enabled": self.supervisor.outputs_enabled,
                "base_topic": "/controller/cmd_vel", "base_units": ["m/s", "m/s", "rad/s"],
                "arm_joint_names": [f"joint{i}" for i in range(1, 6)],
                "gripper_joint_name": "r_joint", "gripper_servo_id": 10,
                "hardware_stop_effect": "unverified", "controller_acknowledged": "unavailable"}

    def read_snapshot(self) -> dict[str, Any]:
        return self.feedback.snapshot()

    def submit(self, command: dict[str, Any]) -> dict[str, Any]:
        return self.supervisor.submit(command)

    def stop(self, reason: str) -> dict[str, Any]:
        return self.supervisor.stop(reason)


def base_twist_fields(target: dict[str, float]) -> dict[str, dict[str, float]]:
    """Pure SI mapping; caller must pass a supervisor-validated target."""
    return {"linear": {"x": target["vx_m_s"], "y": target["vy_m_s"]},
            "angular": {"z": target["wz_rad_s"]}}


def gripper_servo_target(target: dict[str, Any], converter: Any) -> tuple[int, Any]:
    """Return one physical actuator only; converter must apply calibrated hard limits."""
    if set(target) != {"joint_name", "position_rad"} or target["joint_name"] != "r_joint":
        raise ValueError("expected physical r_joint only")
    angle = target["position_rad"]
    if isinstance(angle, bool) or not isinstance(angle, (int, float)) or not math.isfinite(angle):
        raise ValueError("invalid r_joint position")
    raw = converter(angle)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or not 0 <= raw <= 1000:
        raise ValueError("converted servo target outside controller range")
    return 10, raw


# --- T1 Wave A: pure hardware-free boundaries (nothing is published here) ---

SERVO_READBACK_JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint")
SERVO_READBACK_IDS = {"joint1": 1, "joint2": 2, "joint3": 3,
                      "joint4": 4, "joint5": 5, "r_joint": 10}

# Six joints are scanned sequentially by the single UART owner. The vendor
# 50 Hz publisher only re-publishes its command cache; it is NOT a readback
# capability, so max_readback_hz stays unknown until it is measured on the bus.
READBACK_SCAN_DEFAULTS = {
    "joints": list(SERVO_READBACK_JOINTS),
    "servo_ids": dict(SERVO_READBACK_IDS),
    "scan_is_sequential": True,
    "per_joint_timeout_s": 0.1,
    "max_scan_span_s": 0.5,
    "scan_period_s": None,
    "max_readback_hz": None,
    "vendor_cache_publish_hz": 50.0,
    "vendor_cache_source_kind": "command_cache",
    "vendor_cache_is_readback": False,
    "measured_on_hardware": False,
}

REAL_BASE_TOPIC = "/controller/cmd_vel"
SIMULATION_BASE_TOPIC = "/lelab/base/cmd_vel"
BASE_SI_FIELDS = ("vx_m_s", "vy_m_s", "wz_rad_s")


def validate_si_twist(target: Any) -> dict[str, Any]:
    """Pure unit/keys boundary. No kinematics is applied here; ROS mecanum owns that."""
    import math

    if not isinstance(target, dict) or set(target) != set(BASE_SI_FIELDS):
        return {"valid": False, "reason": "invalid_base_target_fields"}
    for key in BASE_SI_FIELDS:
        value = target[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return {"valid": False, "reason": "nonfinite_or_non_numeric:" + key}
    return {"valid": True, "reason": "ok", "units": {"vx_m_s": "m/s", "vy_m_s": "m/s",
                                                    "wz_rad_s": "rad/s"}}


def base_route_boundary(profile: str, *, calibration_verified: bool,
                        machine_type: str | None = None) -> dict[str, Any]:
    """Fail-closed routing boundary for real chassis output; pure logic, no I/O.

    Legacy read-only / locked-chassis profiles keep their identity and never gain
    a chassis route here. Kinematics (SI twist -> four wheel RPS) happens once in
    the vendor ROS controller, never in this adapter.
    """
    reason = "ok"
    enabled = True
    if profile != "hardware_hybrid_manual_v1":
        reason, enabled = "profile_not_hybrid_manual", False
    elif not calibration_verified:
        reason, enabled = "base_calibration_unverified", False
    elif not machine_type:
        reason, enabled = "machine_type_unknown", False
    return {"profile": profile, "topic": REAL_BASE_TOPIC,
            "simulation_only_topic": SIMULATION_BASE_TOPIC,
            "units": {"vx_m_s": "m/s", "vy_m_s": "m/s", "wz_rad_s": "rad/s"},
            "kinematics_owner": "vendor_ros/controller/mecanum",
            "wheel_rps_conversion_in_adapter": False,
            "calibration_verified": bool(calibration_verified),
            "machine_type": machine_type, "outputs_enabled": enabled, "reason": reason}
