"""Versioned observation limits, separate from the unsigned actuator command domain."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint")
BOUNDS = {name: [-50 if name == "joint3" else 0, 1000] for name in JOINTS}
SERVO_IDS = {"joint1": 1, "joint2": 2, "joint3": 3,
             "joint4": 4, "joint5": 5, "r_joint": 10}
COMMAND_RAW_DOMAIN = [0, 1000]


def validate_ranges(ranges):
    if not isinstance(ranges, dict) or set(ranges) != set(JOINTS):
        raise ValueError("feedback_ranges_require_six_joints")
    result = {}
    for name in JOINTS:
        pair = ranges[name]
        if (not isinstance(pair, list) or len(pair) != 2
                or any(isinstance(x, bool) or not isinstance(x, (float, int))
                       or not math.isfinite(x) for x in pair)
                or not BOUNDS[name][0] <= pair[0] < pair[1] <= BOUNDS[name][1]):
            raise ValueError(f"feedback_range_invalid:{name}:bounds={BOUNDS[name]}")
        result[name] = list(pair)
    return result


def _read_host_serial():
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                if key.strip().lower() == "serial":
                    return value.strip() or None
    except OSError:
        return None
    return None


def device_identity(profile):
    board_serial = os.environ.get("LELAB_BOARD_SERIAL", "").strip() or None
    return {
        "kind": "landerpi_bus_servo_follower",
        "control_board": "landerpi_mentorpi",
        "controller_owner": "ros_robot_controller",
        "joint_order": list(JOINTS),
        "servo_ids": dict(SERVO_IDS),
        "board_serial": board_serial,
        "board_serial_source": "environment" if board_serial else "unavailable",
        "host_serial": _read_host_serial(),
    }


def document(ranges, profile, identity=None):
    identity = dict(identity or device_identity(profile))
    if (identity.get("kind") != "landerpi_bus_servo_follower"
            or identity.get("control_board") != "landerpi_mentorpi"
            or identity.get("controller_owner") != "ros_robot_controller"
            or identity.get("joint_order") != list(JOINTS)
            or identity.get("servo_ids") != SERVO_IDS):
        raise ValueError("feedback_limits_device_identity_invalid")
    unsigned = {"schema_version": 1, "kind": "servo_observation_limits",
                "profile": profile, "joint_order": list(JOINTS),
                "device_identity": identity,
                "ranges": validate_ranges(ranges)}
    digest = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                                       allow_nan=False).encode()).hexdigest()
    return {**unsigned, "sha256": digest, "version": digest[:12],
            "editable_bounds": BOUNDS,
            "command_raw_domain": list(COMMAND_RAW_DOMAIN)}


def _legacy_document(ranges, profile):
    unsigned = {"schema_version": 1, "kind": "servo_observation_limits",
                "profile": profile, "joint_order": list(JOINTS),
                "ranges": validate_ranges(ranges)}
    digest = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                                       allow_nan=False).encode()).hexdigest()
    return {**unsigned, "sha256": digest, "version": digest[:12],
            "editable_bounds": BOUNDS,
            "command_raw_domain": list(COMMAND_RAW_DOMAIN)}


def load(path, profile, identity=None):
    if path is None or not Path(path).exists():
        return document(BOUNDS, profile, identity)
    raw = json.loads(Path(path).read_text())
    expected = document(raw.get("ranges"), profile, identity)
    if raw == expected:
        return {**expected, "binding_status": "device_bound"}
    # Read the pre-identity schema so the first validated save can migrate an
    # existing installation without silently rebinding it to another device.
    if "device_identity" not in raw and raw == _legacy_document(
            raw.get("ranges"), profile):
        return {
            **raw,
            "binding_status": "legacy_profile_only_upgrade_on_save",
        }
    raise ValueError("feedback_limits_identity_or_hash_mismatch")


def save(path, profile, ranges, expected_version, identity=None):
    path = Path(path)
    current = load(path, profile, identity)
    if expected_version != current["version"]:
        raise ValueError("feedback_limits_version_conflict")
    updated = document(ranges, profile, identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep immutable previous/current versions; replacing the active pointer is atomic.
    history = path.parent / (path.stem + ".versions")
    history.mkdir(exist_ok=True)
    for item in (current, updated):
        item = {k: v for k, v in item.items() if k != "binding_status"}
        target = history / (item["sha256"] + ".json")
        if not target.exists():
            with target.open("x") as handle:
                json.dump(item, handle, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(updated, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return {**updated, "binding_status": "device_bound"}
