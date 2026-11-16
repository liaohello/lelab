"""Pure conversion between teleop_frame_v1 dicts and the typed DDS messages.

The bridge uses this module to publish, the car receiver to reconstruct the
exact same frame for FrameAdmission.  No rclpy import: the message object is
passed in, so the conversion is testable without ROS.
"""
from __future__ import annotations

import math
from typing import Any

from .teleop_frame_v1 import FRAME_FIELDS, FRAME_PROTOCOL_VERSION

ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"]
COMMAND_FIELDS = {"protocol_version", "session_id", "session_epoch", "channel",
                  "source", "seq", "sample_seq", "ttl_ms",
                  "calibration_bundle_hash", "target", "clutch", "clutch_label"}


class FrameCodecError(ValueError):
    pass


def _int(value: Any, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise FrameCodecError(f"{name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise FrameCodecError(f"{name} exceeds {maximum}")
    return value


def _identity(value: Any, name: str) -> Any:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise FrameCodecError(f"{name} must be a non-empty string or integer")


def _finite_list(values: Any, name: str, length: int) -> list[float]:
    if not isinstance(values, (list, tuple)) or len(values) != length:
        raise FrameCodecError(f"{name} must have exactly {length} values")
    result = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(float(value)):
            raise FrameCodecError(f"{name} must contain finite numbers")
        result.append(float(value))
    return result


def validate_frame(frame: Any) -> dict[str, Any]:
    if not isinstance(frame, dict):
        raise FrameCodecError("frame must be an object")
    if set(frame) != FRAME_FIELDS:
        missing = sorted(FRAME_FIELDS - set(frame))
        extra = sorted(set(frame) - FRAME_FIELDS)
        raise FrameCodecError(f"frame fields wrong: missing={missing} extra={extra}")
    if frame["frame_protocol_version"] != FRAME_PROTOCOL_VERSION:
        raise FrameCodecError("frame_protocol_version")
    _int(frame["frame_seq"], "frame_seq")
    _int(frame["sample_seq"], "sample_seq")
    _identity(frame["stream_epoch"], "stream_epoch")
    _identity(frame["session_id"], "session_id")
    _identity(frame["session_epoch"], "session_epoch")
    _identity(frame["calibration_bundle_hash"], "calibration_bundle_hash")
    _identity(frame["freshness_window_id"], "freshness_window_id")
    commands = frame.get("commands")
    if not isinstance(commands, dict) or set(commands) != {"arm", "gripper"}:
        raise FrameCodecError("commands must hold exactly arm and gripper")
    arm = commands["arm"]
    gripper = commands["gripper"]
    for channel, command in (("arm", arm), ("gripper", gripper)):
        if not isinstance(command, dict) or set(command) != COMMAND_FIELDS:
            raise FrameCodecError(f"{channel} command fields wrong")
        if command.get("channel") != channel:
            raise FrameCodecError(f"{channel} command channel mismatch")
        if command.get("protocol_version") != 1:
            raise FrameCodecError(f"{channel} command protocol version")
        for name in ("session_id", "session_epoch", "calibration_bundle_hash"):
            if command.get(name) != frame.get(name):
                raise FrameCodecError(f"{channel}.{name} disagrees with the envelope")
        if command.get("sample_seq") != frame.get("sample_seq"):
            raise FrameCodecError(f"{channel}.sample_seq disagrees with the envelope")
        _int(command.get("seq"), f"{channel}.seq")
        _int(command.get("ttl_ms"), f"{channel}.ttl_ms", minimum=1, maximum=200)
        if not isinstance(command.get("source"), str) or not command["source"]:
            raise FrameCodecError(f"{channel}.source")
        target = command.get("target")
        if channel == "arm":
            if not isinstance(target, dict) or target.get("joint_names") != ARM_JOINTS:
                raise FrameCodecError("arm joint order is not joint1..joint5")
            _finite_list(target.get("positions_rad"), "arm.positions_rad", 5)
            if "leader_rad" in target:
                _finite_list(target["leader_rad"], "arm.leader_rad", 5)
            if "pre_clip_rad" in target:
                _finite_list(target["pre_clip_rad"], "arm.pre_clip_rad", 5)
        else:
            if not isinstance(target, dict) or target.get("joint_name") != "r_joint":
                raise FrameCodecError("gripper joint is not r_joint")
            _finite_list([target.get("position_rad")], "gripper.position_rad", 1)
        if not isinstance(command.get("clutch"), bool):
            raise FrameCodecError(f"{channel}.clutch must be a boolean")
        if command.get("clutch_label") not in ("engaged", "released"):
            raise FrameCodecError(f"{channel}.clutch_label")
    return frame


def frame_to_message(message: Any, frame: dict[str, Any], *, sender_id: str,
                     transport_epoch: str, auth_key_id: str,
                     auth_ticket: str) -> Any:
    """Fill a TeleopFrame message from a validated frame_v1 dict."""
    validate_frame(frame)
    arm = frame["commands"]["arm"]
    gripper = frame["commands"]["gripper"]
    arm_target = arm["target"]
    gripper_target = gripper["target"]
    message.frame_protocol_version = int(frame["frame_protocol_version"])
    message.frame_seq = int(frame["frame_seq"])
    message.stream_epoch = str(frame["stream_epoch"])
    message.session_id = str(frame["session_id"])
    message.session_epoch = int(frame["session_epoch"])
    message.calibration_bundle_hash = str(frame["calibration_bundle_hash"])
    message.sample_seq = int(frame["sample_seq"])
    message.freshness_window_id = str(frame["freshness_window_id"])
    message.sender_id = str(sender_id)
    message.auth_key_id = str(auth_key_id)
    message.auth_ticket = str(auth_ticket)
    message.transport_epoch = str(transport_epoch)
    message.source = str(arm["source"])
    message.arm_seq = int(arm["seq"])
    message.arm_ttl_ms = int(arm["ttl_ms"])
    message.joint_names = list(arm_target["joint_names"])
    message.positions_rad = [float(value) for value in arm_target["positions_rad"]]
    message.has_leader_rad = "leader_rad" in arm_target
    message.leader_rad = ([float(value) for value in arm_target.get("leader_rad") or []]
                          if message.has_leader_rad else [])
    message.has_pre_clip_rad = "pre_clip_rad" in arm_target
    message.pre_clip_rad = ([float(value) for value in arm_target.get("pre_clip_rad") or []]
                            if message.has_pre_clip_rad else [])
    message.arm_clutch = bool(arm.get("clutch"))
    message.arm_clutch_label = str(arm.get("clutch_label") or "")
    message.gripper_seq = int(gripper["seq"])
    message.gripper_ttl_ms = int(gripper["ttl_ms"])
    message.gripper_joint = str(gripper_target["joint_name"])
    message.gripper_rad = float(gripper_target["position_rad"])
    message.gripper_clutch = bool(gripper.get("clutch"))
    message.gripper_clutch_label = str(gripper.get("clutch_label") or "")
    return message


def _identity_from_text(value: str) -> Any:
    """Window/generation ids are written as text; restore the car's integer form.

    The car issues its window ids and stream epochs as integers.  Keeping them as
    text would make FrameAdmission's identity comparison and window lookup miss
    (``"7" != 7``), so a decimal value is restored to int.  A non-decimal id stays
    a string and is still handled by the same identity contract.
    """
    text = str(value)
    if text and text.isascii() and text.isdigit():
        return int(text)
    return text


def teleop_message_to_frame(message: Any) -> dict[str, Any]:
    """Reconstruct the exact frame dict that FrameAdmission will validate."""
    arm_target: dict[str, Any] = {
        "joint_names": list(message.joint_names),
        "positions_rad": [float(value) for value in message.positions_rad],
    }
    if message.has_leader_rad:
        arm_target["leader_rad"] = [float(value) for value in message.leader_rad]
    if message.has_pre_clip_rad:
        arm_target["pre_clip_rad"] = [float(value) for value in message.pre_clip_rad]
    frame = {
        "frame_protocol_version": int(message.frame_protocol_version),
        "frame_seq": int(message.frame_seq),
        "stream_epoch": _identity_from_text(message.stream_epoch),
        "session_id": str(message.session_id),
        "session_epoch": int(message.session_epoch),
        "calibration_bundle_hash": str(message.calibration_bundle_hash),
        "sample_seq": int(message.sample_seq),
        "freshness_window_id": _identity_from_text(message.freshness_window_id),
        "commands": {
            "arm": {
                "protocol_version": 1,
                "session_id": str(message.session_id),
                "session_epoch": int(message.session_epoch),
                "channel": "arm",
                "source": str(message.source),
                "seq": int(message.arm_seq),
                "sample_seq": int(message.sample_seq),
                "ttl_ms": int(message.arm_ttl_ms),
                "calibration_bundle_hash": str(message.calibration_bundle_hash),
                "target": arm_target,
                "clutch": bool(message.arm_clutch),
                "clutch_label": str(message.arm_clutch_label or
                                     ("engaged" if message.arm_clutch else "released")),
            },
            "gripper": {
                "protocol_version": 1,
                "session_id": str(message.session_id),
                "session_epoch": int(message.session_epoch),
                "channel": "gripper",
                "source": str(message.source),
                "seq": int(message.gripper_seq),
                "sample_seq": int(message.sample_seq),
                "ttl_ms": int(message.gripper_ttl_ms),
                "calibration_bundle_hash": str(message.calibration_bundle_hash),
                "target": {
                    "joint_name": str(message.gripper_joint),
                    "position_rad": float(message.gripper_rad),
                },
                "clutch": bool(message.gripper_clutch),
                "clutch_label": str(message.gripper_clutch_label or
                                     ("engaged" if message.gripper_clutch else "released")),
            },
        },
    }
    return validate_frame(frame)
