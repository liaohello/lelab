"""T4 car-side recording contract: phases, events, command status and labels.

Pure Python 3.10 / ROS-Humble compatible (no rclpy import, no third-party
dependency).  The upper host mirrors the same constants in
``lelab/hybrid_export_schema.py`` and a T4 parity test compares the two with
golden vectors, so a field drift between the car and the exporter is caught
offline instead of on the car.

Nothing here touches hardware.  A phase boundary is an append-only event on
the car clock; it never publishes a motion command and never opens an episode.
"""
from __future__ import annotations

import math
import uuid
from typing import Any

PROTOCOL_VERSION = 1

PHASES: tuple[str, ...] = ("drive", "observe", "grasp", "transport", "release")
MANUAL_RESULTS: tuple[str, ...] = ("success", "failure", "unknown")

EVENT_KINDS: tuple[str, ...] = (
    "session_start", "episode_start", "phase_started", "phase_ended",
    "episode_end", "session_stop", "command_status", "fault",
    "calibration_snapshot", "clock_jump", "recording_error", "estop",
    "control_paused", "hybrid_sample",
)

# Ordered.  A write to the UART ("driver_sent") is NOT mechanical execution.
CONFIRMATION_LEVELS: tuple[str, ...] = (
    "requested", "gateway_accepted", "driver_sent",
    "controller_acknowledged", "execution_observed",
)

CHANNELS: tuple[str, ...] = ("base", "arm", "gripper")
COMMAND_SOURCES: tuple[str, ...] = ("gamepad", "so101_leader", "maintenance")
TRUSTED_JOINT_SOURCE = "servo_position_readback"
TRUSTED_BASE_SOURCES: tuple[str, ...] = ("wheel_odometry_estimate", "imu_fused_estimate")
FORBIDDEN_STATE_SOURCES: tuple[str, ...] = (
    "command_cache", "command_integrated_estimate", "model_placeholder", "synthetic",
)

SCHEMA_ARM_GRIPPER_6DOF = "arm_gripper_6dof_phase1"
SCHEMA_MOBILE_HYBRID_9D = "mobile_hybrid_manual_9d_v1"
SCHEMA_NAMES: tuple[str, ...] = (SCHEMA_ARM_GRIPPER_6DOF, SCHEMA_MOBILE_HYBRID_9D)

STATE_JOINTS: tuple[str, ...] = ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint")
ARM_JOINTS: tuple[str, ...] = STATE_JOINTS[:5]

# 9D action: 3 base velocity COMMANDS + 5 final arm targets + 1 gripper target.
ACTION_9D_FIELDS: tuple[str, ...] = (
    "base_vx_cmd", "base_vy_cmd", "base_wz_cmd",
    "joint1_target", "joint2_target", "joint3_target", "joint4_target", "joint5_target",
    "r_joint_target",
)
# 9D state: VERIFIED base estimate + six measured joints.  Never zero-filled.
STATE_9D_FIELDS: tuple[str, ...] = (
    "base_vx_est", "base_vy_est", "base_wz_est",
    "joint1_measured", "joint2_measured", "joint3_measured", "joint4_measured",
    "joint5_measured", "r_joint_measured",
)
ACTION_6D_FIELDS: tuple[str, ...] = (
    "joint1_target", "joint2_target", "joint3_target", "joint4_target", "joint5_target",
    "r_joint_target",
)
STATE_6D_FIELDS: tuple[str, ...] = (
    "joint1_measured", "joint2_measured", "joint3_measured", "joint4_measured",
    "joint5_measured", "r_joint_measured",
)

# Golden contract payload.  The upper host mirrors this dict; see
# app_ros_mobile/tests/test_t4_contract_parity.py.
CONTRACT: dict[str, Any] = {
    "protocol_version": PROTOCOL_VERSION,
    "phases": list(PHASES),
    "manual_results": list(MANUAL_RESULTS),
    "event_kinds": list(EVENT_KINDS),
    "confirmation_levels": list(CONFIRMATION_LEVELS),
    "channels": list(CHANNELS),
    "command_sources": list(COMMAND_SOURCES),
    "trusted_joint_source": TRUSTED_JOINT_SOURCE,
    "trusted_base_sources": list(TRUSTED_BASE_SOURCES),
    "forbidden_state_sources": list(FORBIDDEN_STATE_SOURCES),
    "schema_names": list(SCHEMA_NAMES),
    "state_joints": list(STATE_JOINTS),
    "arm_joints": list(ARM_JOINTS),
    "action_9d_fields": list(ACTION_9D_FIELDS),
    "state_9d_fields": list(STATE_9D_FIELDS),
    "action_6d_fields": list(ACTION_6D_FIELDS),
    "state_6d_fields": list(STATE_6D_FIELDS),
}


class ContractError(ValueError):
    """A malformed / expired / untrusted recording event. Never silently repaired."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("missing_or_non_numeric", f"{field} is not numeric")
    out = float(value)
    if not math.isfinite(out):
        raise ContractError("non_finite", f"{field} is not finite")
    return out


def new_event_id() -> str:
    return str(uuid.uuid4())


def build_event(kind: str, *, session_id: str, time_ns: int, clock_domain: str,
                session_epoch: str, clock_epoch: str, episode_id: str | None = None,
                payload: dict[str, Any] | None = None, event_id: str | None = None,
                source: str = "manual") -> dict[str, Any]:
    """One canonical event record (plan section 3.4). JSON-serializable."""
    if kind not in EVENT_KINDS:
        raise ContractError("unknown_event_kind", kind)
    if not isinstance(session_id, str) or not session_id:
        raise ContractError("missing_session_id", "session_id must be a non-empty string")
    if not isinstance(session_epoch, str) or not session_epoch:
        raise ContractError("missing_session_epoch", "session_epoch must be a non-empty string")
    if not isinstance(clock_epoch, str) or not clock_epoch:
        raise ContractError("missing_clock_epoch", "clock_epoch must be a non-empty string")
    if not isinstance(clock_domain, str) or not clock_domain:
        raise ContractError("missing_clock_domain", "clock_domain must be a non-empty string")
    if isinstance(time_ns, bool) or not isinstance(time_ns, int) or time_ns < 0:
        raise ContractError("invalid_time_ns", "time_ns must be a non-negative integer")
    return {
        "event_id": event_id or new_event_id(),
        "kind": kind,
        "protocol_version": PROTOCOL_VERSION,
        "session_id": session_id,
        "session_epoch": session_epoch,
        "episode_id": episode_id,
        "time_ns": time_ns,
        "clock_domain": clock_domain,
        "clock_epoch": clock_epoch,
        "source": source,
        "payload": dict(payload or {}),
    }


def validate_event(event: dict[str, Any], *, session_id: str, session_epoch: str,
                   clock_domain: str, clock_epoch: str, now_time_ns: int,
                   max_age_ns: int) -> dict[str, Any]:
    """Reject stale sessions, old epochs, expired TTL, wrong frames and missing data.

    ``now_time_ns`` and ``max_age_ns`` are on the *same* car clock as the event;
    no cross-host monotonic value is compared here (plan section 3.1).
    """
    if not isinstance(event, dict):
        raise ContractError("malformed_frame", "event is not an object")
    required = ("event_id", "kind", "session_id", "session_epoch", "episode_id",
                "time_ns", "clock_domain", "clock_epoch", "payload")
    for field in required:
        if field not in event:
            raise ContractError("missing_field", f"{field} is missing")
    if event.get("protocol_version") not in (None, PROTOCOL_VERSION):
        raise ContractError("protocol_version", f"unsupported {event['protocol_version']}")
    if event["kind"] not in EVENT_KINDS:
        raise ContractError("unknown_event_kind", str(event["kind"]))
    if not isinstance(event["event_id"], str) or not event["event_id"]:
        raise ContractError("missing_event_id", "event_id must be a non-empty string")
    if not isinstance(event["payload"], dict):
        raise ContractError("malformed_frame", "payload must be an object")
    if event["session_id"] != session_id:
        raise ContractError("wrong_session", "event belongs to another session")
    if event["session_epoch"] != session_epoch:
        raise ContractError("stale_session_epoch", "event belongs to an old session epoch")
    if event["clock_domain"] != clock_domain:
        raise ContractError("wrong_clock_domain", "event clock domain differs")
    if event["clock_epoch"] != clock_epoch:
        raise ContractError("cross_clock_epoch", "no interpolation across a clock epoch")
    if isinstance(event["time_ns"], bool) or not isinstance(event["time_ns"], int):
        raise ContractError("invalid_time_ns", "time_ns must be an integer")
    age = now_time_ns - event["time_ns"]
    if age < 0:
        raise ContractError("future_event", "event time is in the future; no future state")
    if age > max_age_ns:
        raise ContractError("expired", f"event age {age}ns exceeds {max_age_ns}ns")
    ttl_ms = event["payload"].get("ttl_ms")
    if ttl_ms is not None:
        _finite(ttl_ms, "ttl_ms")
        if ttl_ms <= 0:
            raise ContractError("expired", "ttl_ms is not positive")
        if age > int(ttl_ms) * 1_000_000:
            raise ContractError("expired", "event exceeded its ttl_ms")
    source = event.get("source")
    if source is not None and source not in ("manual", "car_gateway", "safety", "operator"):
        raise ContractError("untrusted_source", f"unknown event source {source!r}")
    return event


def validate_phase(phase: str, *, time_ns: int, episode_started_ns: int,
                   previous_started_ns: int | None, previous_phase: str | None,
                   episode_ended_ns: int | None = None) -> None:
    """Phase boundary rules: monotonic, same episode, no motion side effects."""
    if phase not in PHASES:
        raise ContractError("unknown_phase", f"{phase!r} is not a declared phase")
    if isinstance(time_ns, bool) or not isinstance(time_ns, int):
        raise ContractError("invalid_time_ns", "phase time must be an integer")
    if time_ns < episode_started_ns:
        raise ContractError("phase_time_precedes_episode", "boundary predates episode_start")
    if episode_ended_ns is not None and time_ns > episode_ended_ns:
        raise ContractError("phase_after_episode", "boundary is after episode_end")
    if previous_started_ns is not None and time_ns <= previous_started_ns:
        raise ContractError("phase_time_not_increasing", "boundaries must strictly increase")
    if previous_phase == phase:
        raise ContractError("phase_already_active", "re-stating the active phase is ambiguous")


def normalize_manual_result(value: Any) -> str:
    """Manual success/failure/unknown only; never a simulator ground truth."""
    if value is None or value == "":
        return "unknown"
    if value is True or value == "success":
        return "success"
    if value is False or value in ("failure", "failed", "false"):
        return "failure"
    if value == "unknown":
        return "unknown"
    raise ContractError("invalid_manual_result", f"unreadable manual result {value!r}")


def normalize_command_status(status: dict[str, Any] | None, *,
                             channel: str | None = None,
                             source: str | None = None) -> dict[str, Any]:
    """Confirmation levels with an explicit ``unavailable`` for a missing ACK.

    ``driver_sent`` is a UART write, NOT execution.  Claiming
    ``execution_observed`` requires measured feedback evidence; a driver write
    or an action-accepted message can never stand in for it.
    """
    if status is not None and not isinstance(status, dict):
        raise ContractError("malformed_frame", "confirmation must be an object")
    raw = dict(status or {})
    if channel is not None and channel not in CHANNELS:
        raise ContractError("unknown_channel", f"{channel!r}")
    if source is not None and source not in COMMAND_SOURCES:
        raise ContractError("untrusted_source", f"command source {source!r}")
    out: dict[str, Any] = {"channel": channel, "source": source}
    for level in CONFIRMATION_LEVELS:
        value = raw.get(level)
        if value is None or value is False:
            out[level] = "unavailable"
        else:
            out[level] = value
    # An explicit "unavailable" from the car is preserved, never upgraded.
    for level in CONFIRMATION_LEVELS:
        if raw.get(level) == "unavailable":
            out[level] = "unavailable"
    if out["execution_observed"] not in ("unavailable", None, False):
        evidence = raw.get("execution_evidence")
        if not isinstance(evidence, dict) or evidence.get("source_kind") != TRUSTED_JOINT_SOURCE \
                or evidence.get("valid") is not True:
            raise ContractError(
                "unproven_execution",
                "execution_observed needs measured servo readback; driver_sent is not executed")
    if out["driver_sent"] not in ("unavailable", None, False) \
            and out["controller_acknowledged"] == "unavailable":
        out["ack_note"] = "driver_sent does not imply controller_acknowledged or execution_observed"
    out["highest_confirmed"] = next(
        (level for level in reversed(CONFIRMATION_LEVELS)
         if out[level] not in ("unavailable", None, False)),
        "unavailable",
    )
    return out


def command_event_fields(*, channel: str, payload: dict[str, Any],
                         profile: dict[str, Any] | None = None,
                         control_epoch: int | None = None,
                         calibration_bundle_hash: str | None = None) -> dict[str, Any]:
    """Build the common audit fields for a base/arm/gripper command event.

    The car has no proof that publishing a ROS command reached a controller or
    produced motion, so those levels stay ``unavailable``.  ``ttl_ms`` is the
    effective lifetime used by this agent, taken from the accepted envelope or
    the configured command limit rather than invented by the exporter.
    """
    if channel not in CHANNELS:
        raise ContractError("unknown_channel", str(channel))
    if not isinstance(payload, dict):
        raise ContractError("malformed_command", "command payload must be an object")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ContractError("missing_session_id", "command has no session_id")
    declared_epoch = payload.get("session_epoch")
    if declared_epoch is None:
        session_epoch = control_epoch
    else:
        if type(declared_epoch) is not int or not 0 <= declared_epoch < 2 ** 53:
            raise ContractError("invalid_session_epoch",
                                "session_epoch must be an integer in [0, 2**53)")
        if control_epoch is not None and declared_epoch != control_epoch:
            raise ContractError("session_epoch_mismatch",
                                f"command epoch {declared_epoch} != lease epoch {control_epoch}")
        session_epoch = declared_epoch
    if session_epoch is not None and (
            type(session_epoch) is not int or not 0 <= session_epoch < 2 ** 53):
        raise ContractError("invalid_session_epoch",
                            "session_epoch must be an integer in [0, 2**53)")

    seq = payload.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ContractError("invalid_seq", "command seq must be a non-negative integer")
    source = payload.get("source")
    if not isinstance(source, str) or not source:
        raise ContractError("missing_source", "command source must be a non-empty string")
    sample_seq = payload.get("sample_seq")
    if sample_seq is not None and (
            isinstance(sample_seq, bool) or not isinstance(sample_seq, int) or sample_seq < 0):
        raise ContractError("invalid_sample_seq",
                            "sample_seq must be a non-negative integer when present")

    limits = (profile or {}).get("limits") or {}
    cap_s = {"base": limits.get("base_command_ttl_s"),
             "arm": limits.get("arm_command_ttl_s"),
             "gripper": limits.get("arm_command_ttl_s")}[channel]
    cap_ms = (float(cap_s) * 1000.0
              if isinstance(cap_s, (int, float)) and not isinstance(cap_s, bool) and cap_s > 0
              else None)
    declared_ms = payload.get("ttl_ms")
    declared_s = payload.get("ttl_s")
    if declared_ms is not None:
        if (isinstance(declared_ms, bool)
                or not isinstance(declared_ms, (int, float))
                or not math.isfinite(float(declared_ms))
                or declared_ms <= 0):
            raise ContractError("invalid_ttl",
                                "command.ttl_ms must be a positive finite number")
        ttl_ms = float(declared_ms)
        ttl_source = "command.ttl_ms"
    elif declared_s is not None:
        if (isinstance(declared_s, bool)
                or not isinstance(declared_s, (int, float))
                or not math.isfinite(float(declared_s))
                or declared_s <= 0):
            raise ContractError("invalid_ttl",
                                "command.ttl_s must be a positive finite number")
        ttl_ms = float(declared_s) * 1000.0
        if cap_ms is not None:
            ttl_ms = min(ttl_ms, cap_ms)
        ttl_source = "command.ttl_s_legacy"
    elif cap_ms is not None:
        ttl_ms = cap_ms
        ttl_source = "profile.limits.effective_command_ttl"
    else:
        raise ContractError("missing_ttl_ms",
                            f"{channel} command has no effective ttl_ms")

    if "calibration_bundle_hash" in payload:
        calibration = payload.get("calibration_bundle_hash")
        calibration_source = "command.calibration_bundle_hash"
    elif "calibration_version" in payload:
        calibration = payload.get("calibration_version")
        calibration_source = "command.calibration_version"
    else:
        calibration = calibration_bundle_hash
        calibration_source = "agent.adapter_fact" if calibration is not None else "unavailable"
    if calibration is not None and (
            not isinstance(calibration, str) or not calibration or calibration == "unbound"):
        raise ContractError("invalid_calibration",
                            "calibration identity must be a non-empty bound string")

    confirmation = normalize_command_status({
        "requested": True,
        "gateway_accepted": True,
        "driver_sent": "unavailable",
        "controller_acknowledged": "unavailable",
        "execution_observed": "unavailable",
    }, channel=channel)
    confirmation["source"] = source
    return {
        "protocol_version": PROTOCOL_VERSION,
        "component": channel,
        "session_id": session_id,
        "session_epoch": session_epoch,
        "source": source,
        "seq": seq,
        "sample_seq": sample_seq,
        "ttl_ms": ttl_ms,
        "ttl_source": ttl_source,
        "calibration_bundle_hash": calibration,
        "calibration_source": calibration_source,
        "confirmation": confirmation,
    }


def feedback_provenance_from_adapter(capabilities: Any, snapshot: Any) -> dict[str, Any]:
    """Derive session provenance from live adapter facts, fail closed when absent.

    A profile label or topic name cannot prove that a value is measured.  This
    function only marks joints/base as trusted when the adapter explicitly
    reports the trusted source, validity, unchanged calibration identity and
    freshness metadata needed by the 9D exporter.
    """
    caps = capabilities if isinstance(capabilities, dict) else {}
    snap = snapshot if isinstance(snapshot, dict) else {}
    reasons: list[str] = []
    if not caps:
        reasons.append("adapter_capabilities_missing")
    if not snap:
        reasons.append("adapter_snapshot_missing")

    joint_samples = snap.get("joint_samples")
    hashes: set[str] = set()
    joint_sources: set[str] = set()
    joint_fact_ok = isinstance(joint_samples, dict) and set(joint_samples) == set(STATE_JOINTS)
    if not joint_fact_ok:
        reasons.append("joint_facts_incomplete")
    else:
        for name in STATE_JOINTS:
            sample = joint_samples.get(name)
            if not isinstance(sample, dict):
                joint_fact_ok = False
                break
            joint_sources.add(str(sample.get("source_kind")))
            if sample.get("source_kind") != TRUSTED_JOINT_SOURCE:
                joint_fact_ok = False
            if sample.get("valid") is not True or sample.get("error") not in (None, "", False):
                joint_fact_ok = False
            calibration_hash = sample.get("calibration_hash")
            if isinstance(calibration_hash, str) and calibration_hash:
                hashes.add(calibration_hash)
            else:
                joint_fact_ok = False
        if joint_sources != {TRUSTED_JOINT_SOURCE}:
            joint_fact_ok = False
        if len(hashes) != 1:
            joint_fact_ok = False
    if not joint_fact_ok and "joint_facts_incomplete" not in reasons:
        reasons.append("joint_facts_not_trusted")

    calibration_hash = next(iter(hashes)) if len(hashes) == 1 else None
    base = snap.get("base_estimate")
    if base is None:
        base = snap.get("base")
    base_ok = isinstance(base, dict)
    if base_ok:
        base_ok = (base.get("source_kind") in TRUSTED_BASE_SOURCES
                   and base.get("valid") is True
                   and calibration_hash is not None
                   and base.get("calibration_hash") == calibration_hash)
    if not base_ok:
        reasons.append("base_estimate_missing_or_untrusted")

    covariance = base.get("covariance") if isinstance(base, dict) else None
    covariance_ok = (isinstance(covariance, (list, tuple)) and len(covariance) == 3
                     and all(not isinstance(v, bool)
                             and isinstance(v, (int, float))
                             and math.isfinite(float(v)) for v in covariance))
    if not covariance_ok:
        reasons.append("base_covariance_missing")

    stale_after_s = snap.get("stale_after_s")
    max_age_ok = (isinstance(stale_after_s, (int, float))
                  and not isinstance(stale_after_s, bool)
                  and math.isfinite(float(stale_after_s))
                  and 0 < float(stale_after_s) <= 0.5)
    if not max_age_ok:
        reasons.append("max_state_age_unavailable_or_above_0.5s")
    scan_span_s = snap.get("scan_span_s")
    scan_span_ok = (isinstance(scan_span_s, (int, float))
                    and not isinstance(scan_span_s, bool)
                    and math.isfinite(float(scan_span_s))
                    and scan_span_s >= 0)
    if isinstance(joint_samples, dict) and not scan_span_ok:
        reasons.append("scan_span_unavailable")

    trusted = not reasons
    return {
        "trusted": trusted,
        "available": trusted,
        "reason": "ok" if trusted else ",".join(dict.fromkeys(reasons)),
        "joint_source_kind": TRUSTED_JOINT_SOURCE if joint_fact_ok else "unavailable",
        "base_source_kind": base.get("source_kind") if base_ok else "unavailable",
        "calibration_hash": calibration_hash,
        "base_estimate_has_covariance": covariance_ok,
        "max_state_age_s": float(stale_after_s) if max_age_ok else None,
        "joint_topic": caps.get("joint_topic") or caps.get("joint_states_topic"),
        "base_topic": caps.get("base_topic"),
        "scan_span_ms": round(float(scan_span_s) * 1000.0, 3) if scan_span_ok else None,
        "fact_source": "adapter.capabilities+adapter.read_snapshot",
        "execution_observed": "unavailable",
    }


def validate_joint_sample(sample: dict[str, Any], *, expected_name: str,
                          calibration_hash: str, max_age_s: float) -> dict[str, Any]:
    """One measured joint.  command_cache / placeholder / synthetic never pass."""
    if not isinstance(sample, dict):
        raise ContractError("malformed_frame", "joint sample must be an object")
    for field in ("name", "servo_id", "raw_position", "position_rad", "source_kind",
                  "sample_seq", "sample_time_ns", "age_s", "valid", "calibration_hash"):
        if field not in sample:
            raise ContractError("missing_field", f"joint sample lacks {field}")
    if sample["name"] != expected_name:
        raise ContractError("wrong_joint_order", f"expected {expected_name}, got {sample['name']!r}")
    if sample["source_kind"] != TRUSTED_JOINT_SOURCE:
        raise ContractError("untrusted_joint_source",
                            f"{sample['source_kind']!r} is not measured feedback")
    if sample["valid"] is not True or sample.get("error") not in (None, "", False):
        raise ContractError("invalid_joint_sample", "joint is flagged invalid or has an error")
    if sample["calibration_hash"] != calibration_hash:
        raise ContractError("calibration_mismatch", "joint calibration hash differs from the session")
    raw = _finite(sample["raw_position"], "raw_position")
    if not -1000 <= raw <= 1000:
        # Preserve out-of-range raw values and reject; never clamp to a "normal" value.
        raise ContractError("raw_position_out_of_range", f"raw_position={raw} outside -1000..1000")
    position = _finite(sample["position_rad"], "position_rad")
    age = _finite(sample["age_s"], "age_s")
    if age < 0 or age > max_age_s:
        raise ContractError("expired", f"joint age {age}s exceeds {max_age_s}s")
    return {"position_rad": position, "raw_position": raw,
            "source_kind": sample["source_kind"], "age_s": age,
            "sample_seq": sample["sample_seq"]}


def validate_base_estimate(sample: dict[str, Any], *, calibration_hash: str,
                           max_age_s: float) -> dict[str, Any]:
    """Verified wheel/IMU estimate only.  Command integration is NOT a measured state."""
    if not isinstance(sample, dict):
        raise ContractError("malformed_frame", "base estimate must be an object")
    for field in ("vx_m_s", "vy_m_s", "wz_rad_s", "source_kind", "sample_time_ns",
                  "age_s", "covariance", "valid", "calibration_hash"):
        if field not in sample:
            raise ContractError("missing_field", f"base estimate lacks {field}")
    if sample["source_kind"] not in TRUSTED_BASE_SOURCES:
        raise ContractError("untrusted_base_estimate",
                            f"{sample['source_kind']!r} is not a verified base estimate "
                            "(command integration is not a training state)")
    if sample["valid"] is not True:
        raise ContractError("invalid_base_estimate", "base estimate is flagged invalid")
    if sample["calibration_hash"] != calibration_hash:
        raise ContractError("calibration_mismatch", "base calibration hash differs from the session")
    if not isinstance(sample["covariance"], (list, tuple)) or len(sample["covariance"]) != 3 \
            or any(not isinstance(v, (int, float)) for v in sample["covariance"]):
        raise ContractError("missing_covariance", "base estimate needs a 3-element covariance")
    age = _finite(sample["age_s"], "age_s")
    if age < 0 or age > max_age_s:
        raise ContractError("expired", f"base estimate age {age}s exceeds {max_age_s}s")
    return {
        "vx_m_s": _finite(sample["vx_m_s"], "vx_m_s"),
        "vy_m_s": _finite(sample["vy_m_s"], "vy_m_s"),
        "wz_rad_s": _finite(sample["wz_rad_s"], "wz_rad_s"),
        "source_kind": sample["source_kind"], "age_s": age,
        "estimator_version": sample.get("estimator_version") or "unavailable",
    }
