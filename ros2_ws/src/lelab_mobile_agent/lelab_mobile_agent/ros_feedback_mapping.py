"""Pure conversion from the measured readback snapshot to typed ROS fields.

No rclpy import: the mapping can be tested on the upper computer and the car
publisher only adds message objects. A missing reading is carried as
``valid=False`` with an explicit error; no zero is ever presented as measured.
"""
from __future__ import annotations

import hashlib
import math
import time
from typing import Any

JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint")
SERVO_IDS = (1, 2, 3, 4, 5, 10)
FRAME_FORMAT_V2 = 2
CLOCK_DOMAIN = "car_process_monotonic"
OWNER_IDENTITY_KIND = "software_owner_instance"


def clock_epoch(boot_id: str, started_mono_ns: int) -> str:
    """Identify one monotonic epoch; a reboot or a new process changes it."""
    material = f"{boot_id}:{int(started_mono_ns)}".encode()
    return "car_monotonic:" + hashlib.sha256(material).hexdigest()[:16]


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _failure_fields(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        return {
            "present": False, "servo_id": 0, "command": "", "reason": "",
            "lock_wait_ms": 0.0, "duration_ms": 0.0, "recovery_phase": "",
            "last_success_seq": -1, "scan_interval_s": 0.0,
            "at_mono_ns": 0, "data_identity": "",
        }
    return {
        "present": True,
        "servo_id": int(document.get("servo_id") or 0),
        "command": str(document.get("command") or ""),
        "reason": str(document.get("reason") or ""),
        "lock_wait_ms": float(_finite(document.get("lock_wait_ms")) or 0.0),
        "duration_ms": float(_finite(document.get("duration_ms")) or 0.0),
        "recovery_phase": str(document.get("recovery_phase") or ""),
        "last_success_seq": int(document.get("last_success_seq")
                                if document.get("last_success_seq") is not None else -1),
        "scan_interval_s": float(_finite(document.get("scan_interval_s")) or 0.0),
        "at_mono_ns": int(document.get("at_mono_ns") or 0),
        "data_identity": str(document.get("data_identity") or ""),
    }


def measured_frame(snapshot: dict[str, Any], *, published_mono_ns: int,
                   clock_epoch_value: str, calibration_hash: str,
                   data_identity: str) -> dict[str, Any]:
    samples = snapshot.get("joint_samples") or {}
    raw: list[int] = []
    positions: list[float] = []
    valid: list[bool] = []
    errors: list[str] = []
    for name in JOINTS:
        sample = samples.get(name) if isinstance(samples, dict) else None
        if not isinstance(sample, dict):
            raw.append(0)
            positions.append(0.0)
            valid.append(False)
            errors.append("sample_missing")
            continue
        value = sample.get("raw_position")
        radian = sample.get("position_rad")
        raw_value = int(value) if _finite(value) is not None else 0
        radian_value = float(radian) if _finite(radian) is not None else 0.0
        sample_errors = []
        if sample.get("source_kind") != "servo_position_readback":
            sample_errors.append("untrusted_source")
        if sample.get("valid") is not True:
            sample_errors.append("sample_invalid")
        if sample.get("error") not in (None, ""):
            sample_errors.append(str(sample["error"]))
        if _finite(value) is None or not -1000 <= raw_value <= 1000:
            sample_errors.append("raw_out_of_range")
        if _finite(radian) is None:
            sample_errors.append("nonfinite_position")
        raw.append(raw_value)
        positions.append(radian_value)
        valid.append(not sample_errors)
        errors.append(",".join(dict.fromkeys(sample_errors)))
    span_s = _finite(snapshot.get("scan_span_s")) or 0.0
    end_s = _finite(snapshot.get("last_success_mono"))
    end_ns = int(end_s * 1_000_000_000) if end_s is not None else 0
    start_ns = max(0, end_ns - int(span_s * 1_000_000_000)) if end_ns else 0
    age_ns = max(0, int(published_mono_ns) - end_ns) if end_ns else -1
    return {
        "source": "ros_robot_controller/servo_readback",
        "data_identity": data_identity,
        "frame_format": FRAME_FORMAT_V2,
        "owner_instance": int(snapshot.get("publisher_instance") or 0),
        "scan_sequence": int(snapshot.get("scan_sequence") or 0),
        "scan_span_us": int(span_s * 1_000_000),
        "sample_start_mono_ns": start_ns,
        "sample_end_mono_ns": end_ns,
        "published_mono_ns": int(published_mono_ns),
        "sample_age_ns": age_ns,
        "clock_domain": CLOCK_DOMAIN,
        "clock_epoch": clock_epoch_value,
        "calibration_hash": calibration_hash or "",
        "joint_names": list(JOINTS),
        "servo_ids": list(SERVO_IDS),
        "raw": raw,
        "position_rad": positions,
        "valid": valid,
        "errors": errors,
    }


def driver_health(snapshot: dict[str, Any], *, now_mono_ns: int,
                  clock_epoch_value: str, data_identity: str) -> dict[str, Any]:
    owner = snapshot.get("owner_health")
    owner = owner if isinstance(owner, dict) else {}
    consumer = snapshot.get("consumer")
    consumer = consumer if isinstance(consumer, dict) else {}
    consumer_first = consumer.get("first_failure")
    consumer_first = consumer_first if isinstance(consumer_first, dict) else {}
    last_age = _finite(owner.get("last_success_age_s"))
    last_success_mono_ns = (int(now_mono_ns - last_age * 1_000_000_000)
                            if last_age is not None else 0)
    last_recovery = owner.get("last_recovery")
    last_recovery = last_recovery if isinstance(last_recovery, dict) else {}
    return {
        "source": "ros_robot_controller/servo_readback",
        "data_identity": data_identity,
        "owner_identity_kind": OWNER_IDENTITY_KIND,
        "owner_instance": int(owner.get("owner_instance") or 0),
        "scan_sequence": int(snapshot.get("scan_sequence") or 0),
        "period_s": float(_finite(owner.get("period_s")) or 0.0),
        "quiet_s": float(_finite(owner.get("quiet_s")) or 0.0),
        "transaction_budget_s": float(
            _finite(owner.get("transaction_budget_s")) or 0.0),
        "scans_started": int(owner.get("scans_started") or 0),
        "scans_ok": int(owner.get("scans_ok") or 0),
        "scans_failed": int(owner.get("scans_failed") or 0),
        "consecutive_failures": int(owner.get("consecutive_failures") or 0),
        "write_failures": int(owner.get("write_failures") or 0),
        "last_success_seq": int(owner.get("last_success_seq")
                                if owner.get("last_success_seq") is not None else -1),
        "last_success_mono_ns": last_success_mono_ns,
        "last_success_age_s": float(last_age if last_age is not None else -1.0),
        "last_scan_interval_s": float(_finite(owner.get("last_scan_interval_s")) or 0.0),
        "max_scan_interval_s": float(_finite(owner.get("max_scan_interval_s")) or 0.0),
        "stale": bool(owner.get("stale", True)) if owner else True,
        "first_failure": _failure_fields(owner.get("first_failure")),
        "last_failure": _failure_fields(owner.get("last_failure")),
        "last_recovery_phase": str(last_recovery.get("phase") or ""),
        "last_recovery_ok": bool(last_recovery.get("ok", False)),
        "last_recovery_quiet_s": float(_finite(last_recovery.get("quiet_s")) or 0.0),
        "last_recovery_duration_ms": float(
            _finite(last_recovery.get("duration_ms")) or 0.0),
        "last_recovery_mono_ns": int(last_recovery.get("at_mono_ns") or 0),
        "consumer_successful_scans": int(consumer.get("successful_scans") or 0),
        "consumer_failed_scans": int(consumer.get("failed_scans") or 0),
        "consumer_last_error": str(consumer.get("last_error") or ""),
        "consumer_first_failure_present": bool(consumer_first),
        "consumer_first_failure_reason": str(consumer_first.get("reason") or ""),
        "consumer_first_failure_mono_ns": int(consumer_first.get("at_mono_ns") or 0),
        "clock_domain": CLOCK_DOMAIN,
        "clock_epoch": clock_epoch_value,
    }


def watchdog_health(clock_epoch_value: str, data_identity: str,
                    now_mono_ns: int | None = None) -> dict[str, Any]:
    """A health document for a process that has not received a scan yet."""
    return driver_health({}, now_mono_ns=int(time.monotonic_ns() if now_mono_ns is None
                                             else now_mono_ns),
                         clock_epoch_value=clock_epoch_value,
                         data_identity=data_identity)
