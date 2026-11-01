"""Centralized SO101 rig definitions for single-arm and bimanual sessions.

Recording, teleoperation and remote inference all build their LeRobot configs
here so the four-device identity checks, calibration bindings and local safety
limits cannot drift apart.  Bimanual arms are assembled as native
``BiSOFollower``/``BiSOLeader`` wrappers; the wrapper honours each child's
calibration id (see the local LeRobot customization) instead of deriving a new
``<pair>_left`` name that would miss an already calibrated file.

Synchronization wording matters: the native wrappers read the left bus and then
the right bus on the control thread.  That is *software-synchronized sequential
I/O*, not hardware-level simultaneous sampling.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .safety import follower_safety_kwargs
from .utils.config import (
    FOLLOWER_CONFIG_PATH,
    LEADER_CONFIG_PATH,
    setup_calibration_files,
)

SIDE_JOINTS = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
)
LEFT_JOIN_NAMES = tuple(f"left_{joint}.pos" for joint in SIDE_JOINTS)
RIGHT_JOIN_NAMES = tuple(f"right_{joint}.pos" for joint in SIDE_JOINTS)
JOINT_NAMES = LEFT_JOIN_NAMES + RIGHT_JOIN_NAMES

MOTOR_TO_URDF = {
    "shoulder_pan": "Rotation",
    "shoulder_lift": "Pitch",
    "elbow_flex": "Elbow",
    "wrist_flex": "Wrist_Pitch",
    "wrist_roll": "Wrist_Roll",
    "gripper": "Jaw",
}

BIMANUAL_ROBOT_TYPE = "bi_so_follower"
# Dataset action semantics.  Bimanual sessions store the command returned by
# BiSOFollower.send_action() (after max_position_deviation, calibrated-range and
# max_relative_target clamps); single-arm sessions keep the historical
# teleop-processor output.  The raw demonstration target is always written to
# the sidecar diagnostics log.
ACTION_SEMANTICS_SENT = "sent_target_after_local_safety_clamps"
ACTION_SEMANTICS_RAW = "teleop_processor_output"
SYNC_SEMANTICS = "software_synchronized_sequential_bus_io"


class RigConfigError(ValueError):
    """A user-correctable rig/calibration/camera configuration error."""


def normalize_port(port: str) -> str:
    text = port.strip()
    if not text:
        raise ValueError("Device port cannot be empty")
    return os.path.realpath(text) if os.path.isabs(text) else text.casefold()


def _normalize_calibration_name(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("Select a non-empty local calibration filename")
    if "/" in text or "\\" in text or ".." in text:
        raise ValueError("Select a local calibration filename, not a path")
    if not text.lower().endswith(".json"):
        text = f"{text}.json"
    return text


class ArmPair(BaseModel):
    """One already configured leader/follower pair."""

    leader_port: str = Field(min_length=1)
    follower_port: str = Field(min_length=1)
    leader_config: str = Field(min_length=1)
    follower_config: str = Field(min_length=1)

    @field_validator("leader_port", "follower_port")
    @classmethod
    def _port(cls, value: str) -> str:
        return value.strip()

    @field_validator("leader_config", "follower_config")
    @classmethod
    def _calibration(cls, value: str) -> str:
        return _normalize_calibration_name(value)

    @model_validator(mode="after")
    def _distinct_within_pair(self) -> ArmPair:
        if normalize_port(self.leader_port) == normalize_port(self.follower_port):
            raise ValueError("Leader and follower must use different serial devices")
        return self


class BimanualRig(BaseModel):
    left: ArmPair
    right: ArmPair

    @model_validator(mode="after")
    def _distinct_devices(self) -> BimanualRig:
        ports = [
            self.left.leader_port,
            self.left.follower_port,
            self.right.leader_port,
            self.right.follower_port,
        ]
        if len({normalize_port(port) for port in ports}) != 4:
            raise ValueError("Bimanual recording requires four distinct serial devices")
        for role in ("leader", "follower"):
            left_name = getattr(self.left, f"{role}_config").casefold()
            right_name = getattr(self.right, f"{role}_config").casefold()
            if left_name == right_name:
                raise ValueError(
                    f"Left and right {role} arms need independent calibration files"
                )
        return self


def validate_cameras(cameras: dict[str, dict], *, require_one: bool = True) -> list[dict[str, Any]]:
    """Validate unique names and unique physical capture devices.

    A camera index/path may only be configured once per session so the preview
    and the recording loop share one owner per device.
    """

    if not isinstance(cameras, dict):
        raise RigConfigError("cameras must be a mapping of camera name to configuration")
    if require_one and not cameras:
        raise RigConfigError("Configure at least one camera for bimanual recording")

    seen_names: set[str] = set()
    seen_devices: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for raw_name, raw_config in cameras.items():
        name = str(raw_name).strip()
        if not name:
            raise RigConfigError("Camera names cannot be empty")
        if name in seen_names:
            raise RigConfigError(f"Duplicate camera name: {name}")
        if "/" in name or "\\" in name or ".." in name:
            raise RigConfigError(f"Camera name is not a safe dataset key: {name!r}")
        if not isinstance(raw_config, dict):
            raise RigConfigError(f"Camera {name} configuration must be an object")
        if raw_config.get("type") != "opencv":
            raise RigConfigError(f"Camera {name} must use type 'opencv'")
        if "camera_index" not in raw_config:
            raise RigConfigError(f"Camera {name} is missing camera_index")
        identity = str(raw_config.get("device_id") or raw_config["camera_index"])
        if identity in seen_devices:
            raise RigConfigError(
                f"Cameras {seen_devices[identity]} and {name} point at the same capture device"
            )
        seen_names.add(name)
        seen_devices[identity] = name
        out.append({"name": name, **raw_config})
    return out


def _calibration_binding(role: str, side: str, config_name: str) -> dict[str, Any]:
    directory = Path(LEADER_CONFIG_PATH if role == "leader" else FOLLOWER_CONFIG_PATH)
    path = directory / config_name
    if not path.is_file():
        raise RigConfigError(
            f"Missing {side} {role} calibration file: {path}. Calibrate that arm first."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RigConfigError(f"Cannot read {side} {role} calibration {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RigConfigError(f"Calibration {path} is not a motor mapping")
    missing = [joint for joint in SIDE_JOINTS if joint not in payload]
    if missing:
        raise RigConfigError(f"Calibration {path} is missing motor entries: {', '.join(missing)}")
    if any(not isinstance(payload[joint], dict) for joint in SIDE_JOINTS):
        raise RigConfigError(f"Calibration {path} has invalid motor entries")
    return {
        "role": role,
        "side": side,
        "file": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "modified_ns": path.stat().st_mtime_ns,
    }


def calibration_manifest(rig: BimanualRig) -> dict[str, dict[str, Any]]:
    """Read and fingerprint all four per-arm calibration files (no copying)."""

    manifest: dict[str, dict[str, Any]] = {}
    for side in ("left", "right"):
        pair = getattr(rig, side)
        for role in ("leader", "follower"):
            config_name = getattr(pair, f"{role}_config")
            manifest[f"{side}_{role}"] = _calibration_binding(role, side, config_name)
    for role in ("leader", "follower"):
        left = manifest[f"left_{role}"]["sha256"]
        right = manifest[f"right_{role}"]["sha256"]
        if left == right:
            raise RigConfigError(
                f"Left and right {role} arms resolve to identical calibration content; "
                "bind each physical arm's own file"
            )
    return manifest


def describe_rig(rig: BimanualRig) -> dict[str, Any]:
    """Return the validated four-device binding without touching hardware."""

    manifest = calibration_manifest(rig)
    return {
        "mode": "bimanual",
        "sync": SYNC_SEMANTICS,
        "joint_names": list(JOINT_NAMES),
        "devices": {
            f"{side}_{role}": {
                "side": side,
                "role": role,
                "port": getattr(getattr(rig, side), f"{role}_port"),
                "calibration": manifest[f"{side}_{role}"],
            }
            for side in ("left", "right")
            for role in ("leader", "follower")
        },
    }


def build_single_configs(
    *,
    leader_port: str,
    follower_port: str,
    leader_config: str,
    follower_config: str,
    camera_configs: dict,
    setup_calibration_files_fn=setup_calibration_files,
    follower_config_cls=None,
    leader_config_cls=None,
):
    """Build the historical single-arm follower/leader config pair."""

    from lerobot.robots.so_follower import SO101FollowerConfig
    from lerobot.teleoperators.so_leader import SO101LeaderConfig

    follower_config_cls = follower_config_cls or SO101FollowerConfig
    leader_config_cls = leader_config_cls or SO101LeaderConfig
    leader_name, follower_name = setup_calibration_files_fn(leader_config, follower_config)
    robot_config = follower_config_cls(
        port=follower_port,
        id=follower_name,
        cameras=camera_configs,
        **follower_safety_kwargs(),
    )
    teleop_config = leader_config_cls(port=leader_port, id=leader_name)
    def optional_binding(role: str, name: str) -> dict[str, Any]:
        try:
            return _calibration_binding(role, "single", name)
        except RigConfigError as exc:
            # Preserve the historical single-arm path: setup_calibration_files
            # already validated the file, and diagnostics are best-effort here.
            return {"role": role, "side": "single", "file": name, "error": str(exc)}

    metadata = {
        "mode": "single",
        "sync": None,
        "action_semantics": ACTION_SEMANTICS_RAW,
        "joint_names": list(SIDE_JOINTS),
        "devices": {
            "leader": {"port": leader_port, "calibration": optional_binding("leader", leader_name)},
            "follower": {
                "port": follower_port,
                "calibration": optional_binding("follower", follower_name),
            },
        },
    }
    return robot_config, teleop_config, metadata


def build_bimanual_configs(
    rig: BimanualRig,
    camera_configs: dict,
    setup_calibration_files_fn=setup_calibration_files,
):
    """Assemble native BiSOFollower/BiSOLeader configs with child identities.

    Each child retains the id of its selected calibration file.  The local
    LeRobot wrapper prefers an explicit child id over its derived
    ``<pair>_left/right`` name, so existing per-arm JSON files are reused
    without copying or renaming.
    """

    from lerobot.robots.bi_so_follower import BiSOFollowerConfig
    from lerobot.robots.so_follower import SO101FollowerConfig
    from lerobot.teleoperators.bi_so_leader import BiSOLeaderConfig
    from lerobot.teleoperators.so_leader import SO101LeaderConfig

    # Validate the four bindings before setup_calibration_files can create
    # any directory or copy.
    manifest = calibration_manifest(rig)

    left_leader, left_follower = setup_calibration_files_fn(
        rig.left.leader_config, rig.left.follower_config
    )
    right_leader, right_follower = setup_calibration_files_fn(
        rig.right.leader_config, rig.right.follower_config
    )

    safety = follower_safety_kwargs()
    left_follower_config = SO101FollowerConfig(
        port=rig.left.follower_port,
        id=left_follower,
        calibration_dir=Path(FOLLOWER_CONFIG_PATH),
        cameras={},
        **safety,
    )
    right_follower_config = SO101FollowerConfig(
        port=rig.right.follower_port,
        id=right_follower,
        calibration_dir=Path(FOLLOWER_CONFIG_PATH),
        cameras={},
        **safety,
    )
    left_leader_config = SO101LeaderConfig(
        port=rig.left.leader_port,
        id=left_leader,
        calibration_dir=Path(LEADER_CONFIG_PATH),
    )
    right_leader_config = SO101LeaderConfig(
        port=rig.right.leader_port,
        id=right_leader,
        calibration_dir=Path(LEADER_CONFIG_PATH),
    )

    robot_config = BiSOFollowerConfig(
        id="lelab_bimanual",
        left_arm_config=left_follower_config,
        right_arm_config=right_follower_config,
        cameras=camera_configs,
    )
    teleop_config = BiSOLeaderConfig(
        id="lelab_bimanual",
        left_arm_config=left_leader_config,
        right_arm_config=right_leader_config,
    )
    metadata = {
        "mode": "bimanual",
        "sync": SYNC_SEMANTICS,
        "action_semantics": ACTION_SEMANTICS_SENT,
        "joint_names": list(JOINT_NAMES),
        "safety": dict(safety),
        "devices": {
            f"{side}_{role}": {
                "side": side,
                "role": role,
                "port": getattr(getattr(rig, side), f"{role}_port"),
                "calibration": manifest[f"{side}_{role}"],
            }
            for side in ("left", "right")
            for role in ("leader", "follower")
        },
    }
    return robot_config, teleop_config, metadata


def assert_bimanual_robot_contract(robot, camera_names: list[str]) -> None:
    """Fail fast if the assembled robot does not expose the declared 12+images contract."""

    if getattr(robot, "name", None) != BIMANUAL_ROBOT_TYPE:
        raise RigConfigError(
            f"Expected a {BIMANUAL_ROBOT_TYPE} wrapper, got {getattr(robot, 'name', None)!r}"
        )
    action_names = list(robot.action_features)
    if action_names != list(JOINT_NAMES):
        raise RigConfigError(
            "Bimanual action features must be the fixed 12 joints in left-then-right order; "
            f"got {action_names}"
        )
    observation = robot.observation_features
    missing = [name for name in JOINT_NAMES if name not in observation]
    if missing:
        raise RigConfigError(f"Bimanual observation is missing joints: {missing}")
    extra_pos = sorted(
        name for name in observation if name.endswith(".pos") and name not in JOINT_NAMES
    )
    if extra_pos:
        raise RigConfigError(f"Unexpected joint features in bimanual observation: {extra_pos}")
    actual_cameras = sorted(name for name in observation if not name.endswith(".pos"))
    if actual_cameras != sorted(camera_names):
        raise RigConfigError(
            "Bimanual camera features do not match the configured cameras: "
            f"features={actual_cameras}, configured={sorted(camera_names)}"
        )


def bimanual_snapshot_from_observation(observation: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Split one BiSO observation into left/right URDF maps and missing-key errors."""

    import math

    arms: dict[str, Any] = {}
    errors: list[str] = []
    for side in ("left", "right"):
        degrees: dict[str, float] = {}
        joints: dict[str, float] = {}
        for motor, urdf_name in MOTOR_TO_URDF.items():
            key = f"{side}_{motor}.pos"
            if key not in observation:
                errors.append(f"missing {key}")
                continue
            try:
                value = float(observation[key])
            except (TypeError, ValueError):
                errors.append(f"non-numeric {key}")
                continue
            degrees[motor] = value
            joints[urdf_name] = value * math.pi / 180.0
        arms[side] = {"joints_rad": joints, "joints_deg": degrees}
    return arms, errors
