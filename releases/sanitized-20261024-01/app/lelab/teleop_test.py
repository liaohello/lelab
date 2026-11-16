"""Single-arm teleoperation test evidence.

A teleoperation test is the only read-only-observation replacement that can prove
a group actually follows.  Because that conclusion belongs to the operator, the
result is stored on disk together with an identity fingerprint of the two ports,
both calibration files and the effective safety limits; any change there makes
the stored pass stale instead of silently reusing it.

This module never opens a port and never writes motors.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import rig_bindings
from .safety import follower_safety_kwargs
from .utils import config

SIDES = ("left", "right")
SCHEMA_VERSION = 1
_EPSILON = 1e-6


def record_name(side: str) -> str:
    if side == "left":
        return rig_bindings.DEFAULT_LEFT_ROBOT
    if side == "right":
        return rig_bindings.DEFAULT_RIGHT_ROBOT
    raise ValueError(f"unknown side: {side!r}")


def _sha256(path: str) -> str | None:
    target = Path(path)
    if not target.is_file():
        return None
    digest = hashlib.sha256()
    digest.update(target.read_bytes())
    return digest.hexdigest()


def build_identity(side: str) -> dict[str, Any]:
    """Describe the exact configuration a passed test applies to."""

    name = record_name(side)
    record = config.get_robot_record(name)
    if record is None:
        raise ValueError(f"robot record {name!r} does not exist yet")
    diagnostics = config.robot_record_diagnostics(record)
    checks = diagnostics.get("checks", {})
    leader_path = str((checks.get("leader_calibration") or {}).get("path", ""))
    follower_path = str((checks.get("follower_calibration") or {}).get("path", ""))
    identity = {
        "side": side,
        "record": name,
        "leader_port": record.get("leader_port", ""),
        "follower_port": record.get("follower_port", ""),
        "leader_calibration": {
            "path": leader_path,
            "sha256": _sha256(leader_path),
        },
        "follower_calibration": {
            "path": follower_path,
            "sha256": _sha256(follower_path),
        },
        "safety": dict(sorted(follower_safety_kwargs().items())),
        "record_complete": bool(diagnostics.get("complete")),
        "issues": list(diagnostics.get("issues", [])),
    }
    identity["fingerprint"] = _fingerprint(identity)
    return identity


def _fingerprint(identity: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "record": identity["record"],
            "leader_port": identity["leader_port"],
            "follower_port": identity["follower_port"],
            "leader_calibration": identity["leader_calibration"],
            "follower_calibration": identity["follower_calibration"],
            "safety": identity["safety"],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def pair_from_identity(identity: dict[str, Any]) -> dict[str, str]:
    return {
        "leader_port": identity["leader_port"],
        "follower_port": identity["follower_port"],
        "leader_config": Path(identity["leader_calibration"]["path"]).name,
        "follower_config": Path(identity["follower_calibration"]["path"]).name,
    }


def resolve_pair(side: str) -> tuple[dict[str, str], dict[str, Any]]:
    """Return the four teleoperation fields and the identity they came from."""

    identity = build_identity(side)
    if not identity["record_complete"]:
        blocking = "; ".join(issue["message"] for issue in identity["issues"])
        raise ValueError(
            f"{identity['record']} 还不能用于单臂遥操作测试：{blocking or '记录不完整'}"
        )
    return pair_from_identity(identity), identity


def new_telemetry(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "identity": identity,
        "started_mono": time.monotonic(),
        "steps": 0,
        "clamped_steps": 0,
        "joints": {},
        "last_error": None,
    }


def update_step(
    telemetry: dict[str, Any],
    raw: dict[str, float],
    sent: dict[str, float],
    measured: dict[str, float],
) -> None:
    """Fold one follow step into the session telemetry."""

    telemetry["steps"] += 1
    joints: dict[str, dict[str, Any]] = telemetry["joints"]
    clamped_this_step = False
    for key, raw_value in raw.items():
        sent_value = sent.get(key)
        measured_value = measured.get(key)
        entry = joints.setdefault(
            key,
            {
                "raw": None,
                "sent": None,
                "measured": None,
                "delta": None,
                "clamp_count": 0,
                "max_clamp": 0.0,
                "max_delta": 0.0,
            },
        )
        entry["raw"] = raw_value
        if sent_value is not None:
            entry["sent"] = sent_value
            clamp = abs(float(sent_value) - float(raw_value))
            if clamp > _EPSILON:
                entry["clamp_count"] += 1
                entry["max_clamp"] = max(entry["max_clamp"], clamp)
                clamped_this_step = True
        if measured_value is not None and sent_value is not None:
            delta = abs(float(sent_value) - float(measured_value))
            entry["measured"] = measured_value
            entry["delta"] = delta
            entry["max_delta"] = max(entry["max_delta"], delta)
    if clamped_this_step:
        telemetry["clamped_steps"] += 1


def telemetry_snapshot(telemetry: dict[str, Any] | None) -> dict[str, Any] | None:
    if telemetry is None:
        return None
    joints = {}
    for key, entry in telemetry["joints"].items():
        joints[key] = {
            "raw": _round(entry["raw"]),
            "sent": _round(entry["sent"]),
            "measured": _round(entry["measured"]),
            "delta": _round(entry["delta"]),
            "clamp_count": entry["clamp_count"],
            "max_clamp": _round(entry["max_clamp"]),
            "max_delta": _round(entry["max_delta"]),
        }
    return {
        "side": telemetry["identity"]["side"],
        "record": telemetry["identity"]["record"],
        "fingerprint": telemetry["identity"]["fingerprint"],
        "elapsed_seconds": round(time.monotonic() - telemetry["started_mono"], 2),
        "steps": telemetry["steps"],
        "clamped_steps": telemetry["clamped_steps"],
        "safety": telemetry["identity"]["safety"],
        "joints": joints,
        "last_error": telemetry["last_error"],
    }


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 3)


def results_path() -> Path:
    return Path(config.TELEOP_TEST_DIR) / f"v{SCHEMA_VERSION}.json"


def load_results() -> dict[str, Any]:
    path = results_path()
    if not path.is_file():
        return {"version": SCHEMA_VERSION, "results": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": SCHEMA_VERSION, "results": {}}
    if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
        return {"version": SCHEMA_VERSION, "results": {}}
    payload.setdefault("results", {})
    return payload


def _write_results(document: dict[str, Any]) -> None:
    path = results_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def store_result(
    side: str,
    identity: dict[str, Any],
    *,
    passed: bool,
    note: str | None = None,
    telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = {
        "side": side,
        "record": identity["record"],
        "fingerprint": identity["fingerprint"],
        "passed": bool(passed),
        "note": (note or "").strip(),
        "recorded_at": datetime.now(UTC).isoformat(),
        "identity": {
            "leader_port": identity["leader_port"],
            "follower_port": identity["follower_port"],
            "leader_calibration": identity["leader_calibration"],
            "follower_calibration": identity["follower_calibration"],
            "safety": identity["safety"],
        },
    }
    if telemetry is not None:
        entry["summary"] = {
            "steps": telemetry["steps"],
            "clamped_steps": telemetry["clamped_steps"],
            "elapsed_seconds": round(time.monotonic() - telemetry["started_mono"], 2),
            "joints": {
                key: {"clamp_count": value["clamp_count"], "max_delta": _round(value["max_delta"])}
                for key, value in telemetry["joints"].items()
            },
        }
    document = load_results()
    document["results"][side] = entry
    _write_results(document)
    return entry


def _read_entry(side: str, fingerprint: str) -> dict[str, Any]:
    entry = load_results()["results"].get(side)
    if not entry:
        return {"side": side, "state": "untested", "entry": None}
    if entry["fingerprint"] != fingerprint:
        return {
            "side": side,
            "state": "stale",
            "entry": entry,
            "reason": "端口、标定文件或安全限幅已变化，需要重新确认。",
        }
    return {"side": side, "state": "passed" if entry["passed"] else "failed", "entry": entry}


def evaluate(identity: dict[str, Any]) -> dict[str, Any]:
    """Classify one group's stored test result against its current identity."""

    outcome = _read_entry(identity["side"], identity["fingerprint"])
    if outcome["state"] in ("passed", "failed"):
        outcome.pop("reason", None)
    return outcome


def readiness() -> dict[str, Any]:
    """Report every group without requiring a live session."""

    groups: dict[str, Any] = {}
    for side in SIDES:
        try:
            identity = build_identity(side)
        except ValueError as exc:
            groups[side] = {
                "side": side,
                "record": record_name(side),
                "available": False,
                "reason": str(exc),
                "test": {"side": side, "state": "untested", "entry": None},
            }
            continue
        groups[side] = {
            "side": side,
            "record": identity["record"],
            "available": identity["record_complete"],
            "reason": None
            if identity["record_complete"]
            else "; ".join(issue["message"] for issue in identity["issues"]),
            "identity": identity,
            "test": evaluate(identity),
        }
    return {"sides": groups, "result_path": str(results_path())}
