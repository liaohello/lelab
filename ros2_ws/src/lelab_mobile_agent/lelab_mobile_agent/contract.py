"""Versioned control and data contract for the LeLab mobile manipulator.

This module deliberately has no ROS dependency so the profile and validation
rules can be tested on the LeLab host as well as inside the ROS environment.
"""
from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ContractError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def load_profile(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        profile = json.load(handle)
    if profile.get("schema_version") != 1:
        raise ContractError("profile_schema", "unsupported schema_version")
    return profile


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("not_numeric", f"{name} must be numeric")
    value = float(value)
    if not math.isfinite(value):
        raise ContractError("not_finite", f"{name} must be finite")
    return value


def _clamp(value: float, low: float, high: float) -> tuple[float, bool]:
    return min(max(value, low), high), value < low or value > high


@dataclass
class BaseCommand:
    vx: float
    vy: float
    wz: float
    clipped: list[str] = field(default_factory=list)

    def as_list(self) -> list[float]:
        return [self.vx, self.vy, self.wz]


@dataclass
class JointCommand:
    positions: list[float]
    duration_s: float
    clipped: list[str] = field(default_factory=list)


class CommandGate:
    """Validate and rate-limit a single source's command stream.

    The gateway is intentionally stateful only for the monotonically
    increasing sequence number; motion state lives in the ROS node.
    """

    def __init__(self, profile: dict[str, Any], session_id: str):
        self.profile = profile
        self.session_id = session_id
        self.last_seq = -1
        self.last_seen = 0.0

    def _envelope(self, payload: dict[str, Any], mode: str) -> dict[str, Any]:
        if payload.get("session_id") != self.session_id:
            raise ContractError("session_mismatch", "control session does not match")
        seq = payload.get("seq")
        if not isinstance(seq, int) or seq < 0:
            raise ContractError("bad_seq", "seq must be a non-negative integer")
        if seq <= self.last_seq:
            raise ContractError("stale_seq", f"seq {seq} <= {self.last_seq}")
        source = payload.get("source")
        if source not in self.profile["modes"]["base"]:
            raise ContractError("bad_source", f"unknown source {source!r}")
        if payload.get("mode") != mode:
            raise ContractError("mode_mismatch", f"expected mode {mode}")
        self.last_seq = seq
        self.last_seen = time.monotonic()
        return payload

    def base(self, payload: dict[str, Any], mode: str) -> BaseCommand:
        self._envelope(payload, mode)
        values = payload.get("base")
        if not isinstance(values, dict):
            raise ContractError("bad_base", "base command missing")
        limits = self.profile["limits"]
        clipped: list[str] = []
        vx = _finite(values.get("vx"), "base.vx")
        vy = _finite(values.get("vy"), "base.vy")
        wz = _finite(values.get("wz"), "base.wz")
        vx, c = _clamp(vx, -limits["base_vx_mps"], limits["base_vx_mps"])
        clipped += ["vx"] if c else []
        vy, c = _clamp(vy, -limits["base_vy_mps"], limits["base_vy_mps"])
        clipped += ["vy"] if c else []
        wz, c = _clamp(wz, -limits["base_wz_radps"], limits["base_wz_radps"])
        clipped += ["wz"] if c else []
        return BaseCommand(vx, vy, wz, clipped)

    def arm(self, payload: dict[str, Any], mode: str) -> JointCommand:
        self._envelope(payload, mode)
        values = payload.get("arm")
        if not isinstance(values, dict):
            raise ContractError("bad_arm", "arm command missing")
        positions = values.get("positions")
        if not isinstance(positions, list) or len(positions) != 5:
            raise ContractError("bad_arm_shape", "arm needs five joint positions")
        limits = self.profile["limits"]["joint_positions_rad"]
        names = self.profile["robot"]["arm_joints"]
        clipped: list[str] = []
        normalized: list[float] = []
        for name, raw in zip(names, positions, strict=True):
            value = _finite(raw, f"arm.{name}")
            low, high = limits[name]
            value, c = _clamp(value, low, high)
            clipped += [name] if c else []
            normalized.append(value)
        duration = _finite(values.get("duration_s", 1.0), "arm.duration_s")
        duration = max(0.1, min(duration, self.profile["limits"]["max_trajectory_duration_s"]))
        return JointCommand(normalized, duration, clipped)

    def gripper(self, payload: dict[str, Any], mode: str) -> JointCommand:
        self._envelope(payload, mode)
        values = payload.get("gripper")
        if not isinstance(values, dict):
            raise ContractError("bad_gripper", "gripper command missing")
        value = _finite(values.get("position"), "gripper.position")
        low, high = self.profile["limits"]["gripper_base_rad"]
        clipped = []
        value, c = _clamp(value, low, high)
        clipped += ["gripper_base"] if c else []
        duration = _finite(values.get("duration_s", 0.6), "gripper.duration_s")
        duration = max(0.1, min(duration, 2.0))
        return JointCommand([value], duration, clipped)


def new_session_id(prefix: str = "ros") -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}"


def state_vector(
    odom_twist: tuple[float, float, float],
    joints: dict[str, float],
    profile: dict[str, Any],
) -> list[float]:
    names = profile["robot"]["arm_joints"]
    master = profile["robot"]["gripper_master_joint"]
    out = [float(odom_twist[0]), float(odom_twist[1]), float(odom_twist[2])]
    out.extend(float(joints[name]) for name in names)
    out.append(float(joints[master]))
    return out
