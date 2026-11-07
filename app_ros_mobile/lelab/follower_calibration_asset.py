"""Server-built follower calibration asset (schema v2).

The browser may start a car-side job, drag the gripper slider and read the
snapshot.  It may not assert that a calibration was measured, which board it came
from, which servo ids were used, or how long it took: those facts are copied from
the car's evidence document, and this module refuses evidence that does not add up.

``measured`` is therefore only ever written here, after:

* the evidence digest over the sample log matches the log that arrived,
* every arm joint has a qualifying manual range capture,
* the gripper has two closed/open rounds with an acceptable repeatability,
* feedback was fresh when the job finished,
* the device identity matches the controller this project actually drives.

Schema v1 follower files stay readable; nothing in this module writes v1.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from .mobile_teleop import RADIANS_PER_PULSE, canonical_sha256

FOLLOWER_SCHEMA_VERSION = 2
EVIDENCE_SCHEMA_VERSION = 2
EVIDENCE_KIND = "follower_calibration_evidence"
ASSET_KIND = "follower"
ASSET_ROLE = "landerpi_follower"
ASSET_METHOD = "operator_manual_plus_gripper_drag"
OPERATOR_ENTRY_ASSET_METHOD = "operator_entered_ranges_plus_gripper_drag"
OPERATOR_FULL_ENTRY_ASSET_METHOD = "operator_manual_full_entry"
ARM_METHOD_CAPTURE = "manual_range_capture"
ARM_METHOD_ENTRY = "operator_manual_entry"
ARM_METHODS = {ARM_METHOD_CAPTURE, ARM_METHOD_ENTRY}
GRIPPER_METHOD_DRAG = "operator_drag_safe_window"
GRIPPER_METHOD_ENTRY = "operator_manual_entry"
LIMITS_SOURCE_CAPTURE = "observed_range_not_mechanical"
LIMITS_SOURCE_ENTRY = "operator_entry_not_mechanical"

JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]
ARM_JOINTS = JOINTS[:5]
GRIPPER_JOINT = "r_joint"
SERVO_IDS = {"joint1": 1, "joint2": 2, "joint3": 3,
             "joint4": 4, "joint5": 5, "r_joint": 10}
CONTROLLER_OWNER = "ros_robot_controller"
TRUSTED_SOURCE = "servo_position_readback"

MIN_ARM_SPAN_RAW = 80
MIN_ARM_SAMPLES = 30
MIN_ROUNDS = 2
MAX_REPEATABILITY_RAW = 15
MAX_EVIDENCE_AGE_S = 1800.0
MIN_USABLE_SPAN_RAW = 20
RADIANS_PER_TICK_VALUE = float(RADIANS_PER_PULSE)


def _number(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _parse_utc(value: Any) -> _dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def sample_log_digest(sample_log: Any) -> str | None:
    if not isinstance(sample_log, list):
        return None
    try:
        blob = json.dumps(sample_log, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(blob).hexdigest()


def default_follower_identity() -> str:
    return os.environ.get("LELAB_HYBRID_FOLLOWER_ID", "landerpi-follower-v1")


def validate_evidence(evidence: Any, *, now_ts: float | None = None,
                      max_age_s: float = MAX_EVIDENCE_AGE_S) -> list[str]:
    """Reasons the evidence cannot become a follower asset. Empty means accepted."""
    if not isinstance(evidence, dict):
        return ["evidence_missing"]
    reasons: list[str] = []

    if (type(evidence.get("schema_version")) is not int
            or evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION
            or evidence.get("kind") != EVIDENCE_KIND):
        reasons.append("evidence_schema_mismatch")
    if evidence.get("source_kind") != TRUSTED_SOURCE:
        # /controller_manager/joint_states and every synthetic vector are echoes.
        reasons.append("evidence_source_untrusted")
    if evidence.get("method") not in {
            ASSET_METHOD, OPERATOR_ENTRY_ASSET_METHOD, OPERATOR_FULL_ENTRY_ASSET_METHOD}:
        reasons.append("evidence_method_unexpected")

    job_id = evidence.get("job_id")
    if not (isinstance(job_id, str) and len(job_id) == 16
            and all(char in "0123456789abcdef" for char in job_id)):
        reasons.append("evidence_job_id_invalid")
    if type(evidence.get("generation")) is not int or evidence["generation"] < 1:
        reasons.append("evidence_generation_invalid")

    identity = evidence.get("device_identity")
    if not isinstance(identity, dict):
        reasons.append("evidence_device_identity_missing")
    else:
        if evidence.get("simulated") is True or identity.get("simulated") is True:
            # A fixture can drive the same state machine; it can never become a
            # measured asset.
            reasons.append("evidence_simulated")
        if identity.get("controller_owner") != CONTROLLER_OWNER:
            reasons.append("evidence_controller_owner_mismatch")
        if identity.get("servo_ids") != SERVO_IDS:
            reasons.append("evidence_servo_ids_mismatch")
        if identity.get("joint_order") != JOINTS:
            reasons.append("evidence_joint_order_mismatch")
        if not _text(identity.get("control_board")):
            reasons.append("evidence_control_board_missing")
        if identity.get("board_serial") is not None and not _text(identity.get("board_serial")):
            reasons.append("evidence_board_serial_invalid")

    quality = evidence.get("quality")
    if not isinstance(quality, dict):
        reasons.append("evidence_quality_missing")
    else:
        if quality.get("complete") is not True:
            reasons.append("evidence_incomplete")
        if quality.get("feedback_fresh") is not True:
            reasons.append("evidence_feedback_stale")

    started = _parse_utc(evidence.get("started_at_utc"))
    completed = _parse_utc(evidence.get("completed_at_utc"))
    if started is None or completed is None:
        reasons.append("evidence_time_missing_or_invalid")
    else:
        if completed < started:
            reasons.append("evidence_time_reversed")
        now = _dt.datetime.now(_dt.timezone.utc).timestamp() if now_ts is None else float(now_ts)
        if not math.isfinite(now):
            reasons.append("invalid_now")
        elif now - completed.timestamp() > max_age_s:
            reasons.append("evidence_expired")
        elif completed.timestamp() > now + 120:
            reasons.append("evidence_from_the_future")

    joints = evidence.get("joints")
    if not isinstance(joints, dict) or set(joints) != set(JOINTS):
        reasons.append("evidence_joints_incomplete")
    else:
        for name in ARM_JOINTS:
            entry = joints.get(name) or {}
            method = entry.get("method")
            if method not in ARM_METHODS:
                reasons.append(f"{name}:method_mismatch")
            if entry.get("valid") is not True:
                reasons.append(f"{name}:invalid")
            reference = entry.get("reference_raw")
            low = entry.get("observed_min_raw")
            high = entry.get("observed_max_raw")
            if not all(type(value) is int and -1000 <= value <= 1000
                       for value in (reference, low, high)):
                reasons.append(f"{name}:range_missing")
                continue
            if high <= low:
                reasons.append(f"{name}:range_reversed")
            if int(entry.get("span_raw", 0)) < MIN_ARM_SPAN_RAW:
                reasons.append(f"{name}:span_too_small")
            if method == ARM_METHOD_CAPTURE and int(entry.get("samples", 0)) < MIN_ARM_SAMPLES:
                reasons.append(f"{name}:samples_too_few")
            if method == ARM_METHOD_CAPTURE and not _number(entry.get("noise_raw")):
                reasons.append(f"{name}:noise_unknown")

    gripper = evidence.get("gripper")
    if not isinstance(gripper, dict):
        reasons.append("evidence_gripper_missing")
    else:
        gripper_method = gripper.get("method")
        if gripper_method not in {GRIPPER_METHOD_DRAG, GRIPPER_METHOD_ENTRY}:
            reasons.append("gripper:method_mismatch")
        if gripper.get("valid") is not True:
            reasons.append("gripper:invalid")
        rounds = gripper.get("rounds_completed")
        if gripper_method == GRIPPER_METHOD_DRAG and (type(rounds) is not int or rounds < MIN_ROUNDS):
            reasons.append("gripper:repeatability_rounds_missing")
        repeatability = gripper.get("repeatability_raw")
        if gripper_method == GRIPPER_METHOD_DRAG and type(repeatability) is not int:
            reasons.append("gripper:repeatability_missing")
        elif gripper_method == GRIPPER_METHOD_DRAG and repeatability > MAX_REPEATABILITY_RAW:
            reasons.append("gripper:repeatability_exceeded")
        window = gripper.get("safe_window_raw")
        if (not isinstance(window, list) or len(window) != 2
                or not all(type(value) is int for value in window) or window[0] >= window[1]):
            reasons.append("gripper:safe_window_invalid")
        closed, opened = gripper.get("observed_closed_raw"), gripper.get("observed_open_raw")
        if type(closed) is not int or type(opened) is not int:
            reasons.append("gripper:observed_ends_missing")
        elif closed == opened:
            reasons.append("gripper:observed_ends_equal")
        marks = gripper.get("marks")
        if (gripper_method == GRIPPER_METHOD_DRAG
                and (not isinstance(marks, list) or len(marks) < 2 * MIN_ROUNDS)):
            reasons.append("gripper:marks_incomplete")
        for axis in ("usable_closed_raw", "usable_open_raw"):
            if type(gripper.get(axis)) is not int:
                reasons.append(f"gripper:{axis}_missing")
        if (type(gripper.get("usable_closed_raw")) is int
                and type(gripper.get("usable_open_raw")) is int
                and abs(gripper["usable_open_raw"] - gripper["usable_closed_raw"])
                < MIN_USABLE_SPAN_RAW):
            reasons.append("gripper:usable_span_too_small")

    units = evidence.get("units")
    if not isinstance(units, dict) or not _text(units.get("gripper_flip")):
        reasons.append("evidence_units_missing")
    if gripper.get("mechanical_limits") is not None:
        # Observed safe windows are not mechanical hard stops; claiming one would
        # silently widen every later limit.
        reasons.append("gripper:mechanical_limits_not_proven")

    digest = sample_log_digest(evidence.get("sample_log"))
    if digest is None or digest != evidence.get("sample_log_sha256"):
        reasons.append("sample_log_digest_mismatch")
    if int(evidence.get("sample_log_entries") or 0) <= 0:
        reasons.append("sample_log_empty")
    return sorted(set(reasons))


def _radians(raw: int, direction: int) -> float:
    return round((raw - 500) * RADIANS_PER_TICK_VALUE * direction, 6)


def _ordered(low: float, high: float) -> list[float]:
    return [min(low, high), max(low, high)]


def build_follower_asset(evidence: dict[str, Any], *, identity: str | None = None,
                         now_ts: float | None = None) -> dict[str, Any]:
    """Build the v2 asset. Raises ValueError with the same reasons as validate."""
    reasons = validate_evidence(evidence, now_ts=now_ts)
    if reasons:
        raise ValueError(", ".join(reasons))
    identity = identity or default_follower_identity()
    if not _text(identity):
        raise ValueError("follower_identity_required")

    joints: dict[str, Any] = {}
    limits: dict[str, list[float]] = {}
    soft_limits: dict[str, list[float]] = {}
    raw_to_rad: dict[str, Any] = {}
    for name in JOINTS:
        entry = evidence["joints"][name]
        direction = 1
        if name == GRIPPER_JOINT:
            # The vendor YAML declares min=1000/max=0 for id 10; the flip is a
            # property of the mechanism, not a guess about which end is "closed".
            direction = -1
            low_raw = int(evidence["gripper"]["usable_closed_raw"])
            high_raw = int(evidence["gripper"]["usable_open_raw"])
            method = "operator_drag_safe_window"
        else:
            low_raw = int(entry["observed_min_raw"])
            high_raw = int(entry["observed_max_raw"])
            method = entry["method"]
        limits[name] = _ordered(_radians(low_raw, direction), _radians(high_raw, direction))
        soft_limits[name] = limits[name]
        raw_to_rad[name] = {
            # `zero_tick` is the field the existing observer reads; `centre_raw` is the
            # explicit alias so a future reader never has to guess the unit.
            "zero_tick": 500,
            "centre_raw": 500,
            "radians_per_tick": RADIANS_PER_TICK_VALUE,
            "direction": direction,
            "source": "vendor_joint_position_controller",
        }
        joints[name] = {
            "method": method,
            "servo_id": SERVO_IDS[name],
            "reference_raw": entry.get("reference_raw"),
            "observed_min_raw": entry.get("observed_min_raw"),
            "observed_max_raw": entry.get("observed_max_raw"),
            "span_raw": entry.get("span_raw"),
            "samples": entry.get("samples"),
            "noise_raw": entry.get("noise_raw"),
            "anomalies": entry.get("anomalies"),
            "duplicates": entry.get("duplicates"),
            "stale": entry.get("stale"),
            "last_seq": entry.get("last_seq"),
            "evidence_sha256": canonical_sha256({
                "joint": name,
                "reference_raw": entry.get("reference_raw"),
                "observed_min_raw": entry.get("observed_min_raw"),
                "observed_max_raw": entry.get("observed_max_raw"),
                "samples": entry.get("samples"),
                "sample_log_entries": entry.get("sample_log_entries"),
            }),
            "valid": True,
        }

    gripper_evidence = evidence["gripper"]
    gripper = {
        "method": gripper_evidence["method"],
        "safe_window_raw": list(gripper_evidence["safe_window_raw"]),
        "observed_closed_raw": int(gripper_evidence["observed_closed_raw"]),
        "observed_open_raw": int(gripper_evidence["observed_open_raw"]),
        "usable_closed_raw": int(gripper_evidence["usable_closed_raw"]),
        "usable_open_raw": int(gripper_evidence["usable_open_raw"]),
        "direction": int(gripper_evidence.get("direction") or
                         (1 if gripper_evidence["observed_open_raw"] > gripper_evidence["observed_closed_raw"]
                          else -1)),
        "repeatability_raw": (int(gripper_evidence["repeatability_raw"])
                              if gripper_evidence.get("repeatability_raw") is not None
                              else None),
        "repeatability_tolerance_raw": int(gripper_evidence.get("repeatability_tolerance_raw")
                                           or MAX_REPEATABILITY_RAW),
        "rounds_completed": int(gripper_evidence.get("rounds_completed") or 0),
        "marks": [dict(mark) for mark in (gripper_evidence.get("marks") or [])],
        "commands_sent": gripper_evidence.get("commands_sent"),
        "commands_dropped": gripper_evidence.get("commands_dropped"),
        "mechanical_limits": None,
    }

    return {
        "schema_version": FOLLOWER_SCHEMA_VERSION,
        "kind": ASSET_KIND,
        "role": ASSET_ROLE,
        "identity": identity,
        "measured": True,
        "measured_at_utc": evidence["completed_at_utc"],
        "method": (
            OPERATOR_FULL_ENTRY_ASSET_METHOD
            if (evidence.get("arm_ranges", {}).get("operator_entered") is True
                and evidence.get("gripper_ranges", {}).get("operator_entered") is True)
            else (OPERATOR_ENTRY_ASSET_METHOD
                  if evidence.get("arm_ranges", {}).get("operator_entered") is True
                  else ASSET_METHOD)
        ),
        "source_kind": TRUSTED_SOURCE,
        "joint_order": list(JOINTS),
        "device_identity": {
            "kind": "landerpi_bus_servo_follower",
            "control_board": evidence["device_identity"]["control_board"],
            "controller_owner": evidence["device_identity"]["controller_owner"],
            "servo_ids": dict(evidence["device_identity"]["servo_ids"]),
            "board_serial": evidence["device_identity"].get("board_serial"),
            "board_serial_source": evidence["device_identity"].get("board_serial_source"),
            "host_serial": evidence["device_identity"].get("host_serial"),
            "agent_version": evidence.get("agent", {}).get("version"),
            "profile": evidence.get("agent", {}).get("profile"),
        },
        "units": {
            "position": "vendor_raw_tick",
            "centre_raw": 500,
            "radians_per_tick": RADIANS_PER_TICK_VALUE,
            "gripper_flip": evidence["units"]["gripper_flip"],
            "conversion_owner": "vendor_joint_position_controller",
        },
        "limits_rad": limits,
        "soft_limits_rad": soft_limits,
        "limits_source": (LIMITS_SOURCE_ENTRY
                          if evidence.get("arm_ranges", {}).get("operator_entered") is True
                          else LIMITS_SOURCE_CAPTURE),
        "raw_to_rad": raw_to_rad,
        "joints": joints,
        "gripper": gripper,
        "evidence": {
            "job_id": evidence["job_id"],
            "generation": evidence["generation"],
            "started_at_utc": evidence["started_at_utc"],
            "completed_at_utc": evidence["completed_at_utc"],
            "operator": evidence.get("operator"),
            "sample_log_sha256": evidence["sample_log_sha256"],
            "sample_log_entries": evidence.get("sample_log_entries"),
            "safe_window_raw": list(evidence["safe_window_raw"]),
            "support_confirmed": evidence.get("support_confirmed"),
            "operator_confirmed_safe_window": evidence.get("operator_confirmed_safe_window"),
            "stop_reason": evidence.get("stop_reason"),
        },
        "provenance": [
            ("operator-entered arm endpoints; not automatic range capture"
             if evidence.get("arm_ranges", {}).get("operator_entered") is True
             else "operator manual range capture through the unique /dev/rrc owner"),
            ("gripper endpoints entered by the operator; not automatic repeatability"
             if evidence.get("gripper_ranges", {}).get("operator_entered") is True
             else "gripper dragged inside an operator-confirmed safe window"),
            "measured flag, device identity and timestamps written by the server",
        ],
        "simulated": False,
    }


def validate_follower_asset_v2(asset: Any) -> list[str]:
    """Schema gate for a stored v2 follower asset (used before saving and on load)."""
    if not isinstance(asset, dict):
        return ["asset_must_be_object"]
    reasons: list[str] = []
    if (type(asset.get("schema_version")) is not int
            or asset.get("schema_version") != FOLLOWER_SCHEMA_VERSION
            or asset.get("kind") != ASSET_KIND):
        reasons.append("schema_or_kind_mismatch")
    if asset.get("role") != ASSET_ROLE:
        reasons.append("role_mismatch")
    if asset.get("measured") is not True:
        reasons.append("unmeasured_example")
    if asset.get("method") not in {
            ASSET_METHOD, OPERATOR_ENTRY_ASSET_METHOD, OPERATOR_FULL_ENTRY_ASSET_METHOD}:
        reasons.append("method_mismatch")
    if asset.get("source_kind") != TRUSTED_SOURCE:
        reasons.append("source_kind_mismatch")
    if asset.get("joint_order") != JOINTS:
        reasons.append("follower_joint_order_mismatch")
    if asset.get("limits_source") not in {LIMITS_SOURCE_CAPTURE, LIMITS_SOURCE_ENTRY}:
        reasons.append("limits_source_not_declared")
    if asset.get("simulated") is True:
        reasons.append("simulated_or_example_asset")

    device = asset.get("device_identity")
    if (not isinstance(device, dict) or device.get("controller_owner") != CONTROLLER_OWNER
            or device.get("servo_ids") != SERVO_IDS):
        reasons.append("device_binding_invalid")

    limits = asset.get("limits_rad")
    if (not isinstance(limits, dict) or set(limits) != set(JOINTS)
            or not all(isinstance(pair, list) and len(pair) == 2
                       and all(_number(value) for value in pair) and pair[0] < pair[1]
                       for pair in limits.values())):
        reasons.append("follower_limits_invalid")

    joints = asset.get("joints")
    if not isinstance(joints, dict) or set(joints) != set(JOINTS):
        reasons.append("follower_joints_incomplete")
    else:
        for name in ARM_JOINTS:
            entry = joints.get(name) or {}
            if entry.get("method") not in ARM_METHODS or entry.get("valid") is not True:
                reasons.append(f"{name}:not_measured")
            if not _text(entry.get("evidence_sha256")):
                reasons.append(f"{name}:evidence_missing")

    gripper = asset.get("gripper")
    if not isinstance(gripper, dict):
        reasons.append("gripper_missing")
    else:
        gripper_method = gripper.get("method")
        if gripper_method not in {GRIPPER_METHOD_DRAG, GRIPPER_METHOD_ENTRY}:
            reasons.append("gripper:method_mismatch")
        if gripper.get("direction") not in (-1, 1) or isinstance(gripper.get("direction"), bool):
            reasons.append("gripper:direction_invalid")
        for axis in ("observed_closed_raw", "observed_open_raw",
                     "usable_closed_raw", "usable_open_raw"):
            if type(gripper.get(axis)) is not int:
                reasons.append(f"gripper:{axis}_invalid")
        if (gripper_method == GRIPPER_METHOD_DRAG
                and type(gripper.get("repeatability_raw")) is not int):
            reasons.append("gripper:repeatability_missing")
        if (gripper_method == GRIPPER_METHOD_DRAG
                and (type(gripper.get("rounds_completed")) is not int
                     or gripper["rounds_completed"] < MIN_ROUNDS)):
            reasons.append("gripper:repeat_rounds_missing")
        if gripper.get("mechanical_limits") is not None:
            reasons.append("gripper:mechanical_limits_not_proven")

    evidence = asset.get("evidence")
    if (not isinstance(evidence, dict) or not _text(evidence.get("sample_log_sha256"))
            or len(evidence["sample_log_sha256"]) != 64):
        reasons.append("evidence_digest_missing")
    return sorted(set(reasons))


# --------------------------------------------------------------------- mapping
def mapping_invalidation_path(root: Path, identity: str) -> Path:
    safe = identity.replace("/", "_").replace("..", "_")
    return Path(root) / "mapping" / f"{safe}.invalidated.json"


def read_mapping_invalidation(root: Path, identity: str) -> dict[str, Any] | None:
    path = mapping_invalidation_path(root, identity)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"reason": "unreadable", "path": str(path)}
    return payload if isinstance(payload, dict) else {"reason": "unreadable", "path": str(path)}


def invalidate_mappings_for_follower(root: Path, *, follower_identity: str,
                                     follower_version: str) -> list[dict[str, Any]]:
    """Mark every mapping that binds this follower as stale, without deleting it.

    The old file stays on disk for audit, but ``HybridCalibrationStore.load`` refuses
    it until the operator re-pairs, which is what "旧 C 层 mapping 必须失效" means for
    a workspace that must not destroy evidence.
    """
    root = Path(root)
    directory = root / "mapping"
    invalidated: list[dict[str, Any]] = []
    if not directory.is_dir():
        return invalidated
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".invalidated.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        bound = payload.get("bound_identities")
        if not isinstance(bound, dict) or bound.get("follower") != follower_identity:
            continue
        identity = str(payload.get("identity") or path.stem)
        marker = {
            "schema_version": 1,
            "kind": "mapping_invalidation",
            "identity": identity,
            "reason": "follower_calibration_replaced",
            "follower_identity": follower_identity,
            "follower_version": follower_version,
            "invalidated_at_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "action_required": "re_pair_leader_and_follower_before_preview",
        }
        target = mapping_invalidation_path(root, identity)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = target.with_suffix(".tmp")
        handle.write_text(json.dumps(marker, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(handle, target)
        invalidated.append(marker)
    return invalidated
