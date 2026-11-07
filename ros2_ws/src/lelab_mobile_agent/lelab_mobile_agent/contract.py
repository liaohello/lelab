"""Versioned control and data contract for the LeLab mobile manipulator.

This module deliberately has no ROS dependency so the profile and validation
rules can be tested on the LeLab host as well as inside the ROS environment.
"""
from __future__ import annotations

import json
import math
import threading
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
        # When the next arm/gripper target must arrive, judged on this machine's monotonic clock.
        self.arm_hold_deadline = 0.0
        self.arm_ttl_s = float(profile.get("limits", {}).get("arm_command_ttl_s", 0.0))

    def arm_stream(self, now: float | None = None) -> dict[str, Any]:
        """Report whether the leader-arm stream is still inside its own TTL.

        Detection only: nothing here commands the arm. The hold policy itself has to be verified
        against the real servo controller before it is claimed to keep a loaded arm in place.
        """
        now = time.monotonic() if now is None else now
        live = self.arm_hold_deadline > 0.0 and now <= self.arm_hold_deadline
        return {
            "expected": self.arm_hold_deadline > 0.0,
            "live": live,
            "ttl_s": self.arm_ttl_s or None,
            "age_s": round(now - self.last_seen, 3) if self.last_seen else None,
            "remaining_s": round(self.arm_hold_deadline - now, 3) if self.arm_hold_deadline else None,
            "clock_domain": "monotonic",
        }

    def _note_arm_ttl(self, payload: dict[str, Any]) -> None:
        declared = payload.get("ttl_s")
        cap = self.arm_ttl_s
        if isinstance(declared, bool) or not isinstance(declared, (int, float)) or declared <= 0:
            effective = cap or 0.0
        else:
            # A client may shorten the window but never extend past what the profile allows.
            effective = min(float(declared), cap) if cap else float(declared)
        if effective > 0.0:
            self.arm_hold_deadline = time.monotonic() + effective

    def _envelope(self, payload: dict[str, Any], mode: str) -> dict[str, Any]:
        if payload.get("session_id") != self.session_id:
            raise ContractError("session_mismatch", "control session does not match")
        seq = payload.get("seq")
        # bool is an int subclass in Python, but `true` is never a legitimate sequence number.
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ContractError("bad_seq", "seq must be a non-negative integer")
        if seq <= self.last_seq:
            raise ContractError("stale_seq", f"seq {seq} <= {self.last_seq}")
        source = payload.get("source")
        allowed = set(self.profile["modes"].get("base", [])) | set(self.profile["modes"].get("arm_sources", []))
        if source not in allowed:
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
        self._note_arm_ttl(payload)
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
        self._note_arm_ttl(payload)
        return JointCommand([value], duration, clipped)


TELEOP_STATE_FIELDS = (
    "kind", "time_ns", "sim_time_ns", "clock_domain", "control_mode", "leader_arm_enabled",
    "clutch", "leader_positions_rad", "leader_gripper_percent", "pre_clip_target_rad",
    "sent_target_rad", "sent_gripper_rad", "calibration_version", "last_seq", "feedback",
    "ack", "last_reject", "semantics",
)


def teleop_state_payload(*, now_ns: int, sim_ns: int | None, clock_domain: str,
                         control_mode: str, leader_arm_enabled: bool,
                         last_teleop: dict[str, Any] | None,
                         last_arm_targets: list[float] | None,
                         last_gripper_target: float | None, last_action_seq: int,
                         feedback_ok: bool, feedback: dict[str, Any],
                         applied: list[Any], last_reject: Any) -> dict[str, Any]:
    """Build the row recorded on the teleop state topic.

    Kept ROS-free so the provenance split the acceptance depends on — reading vs mapped demand vs
    what the gateway sent vs controller acknowledgement vs what actually moved — cannot drift out of
    the recorded payload without a test noticing.
    """
    seen = last_teleop or {}
    return {
        "kind": "teleop_state",
        "time_ns": int(now_ns),
        "sim_time_ns": sim_ns,
        "clock_domain": clock_domain,
        "control_mode": control_mode,
        "leader_arm_enabled": leader_arm_enabled,
        "clutch": seen.get("clutch"),
        "leader_positions_rad": seen.get("leader_rad"),
        "leader_gripper_percent": seen.get("gripper_percent"),
        "pre_clip_target_rad": seen.get("pre_clip_rad"),
        "sent_target_rad": list(last_arm_targets or []),
        "sent_gripper_rad": last_gripper_target,
        "calibration_version": seen.get("calibration_version"),
        "last_seq": last_action_seq,
        "feedback": {"valid": feedback_ok,
                     **{key: feedback.get(key) for key in
                        ("available", "reason", "positions_rad", "age_s", "missing_joints",
                         "source", "servo_pulses")}},
        "ack": list(applied)[-3:],
        "last_reject": last_reject,
        "semantics": {
            "leader_positions_rad": "measured leader reading (upper computer)",
            "pre_clip_target_rad": "mapped target before gateway clamping",
            "sent_target_rad": "target actually sent through the gateway",
            "feedback.positions_rad": "measured follower reading",
            "ack": "controller accepted/rejected observations reported by the watchdog",
            "executed": "not directly observable; inferred only from later feedback",
        },
    }


def exclusivity_report(topology: dict[str, list[str] | None], own_node: str,
                       exempt: list[str] | None = None) -> dict[str, Any]:
    """Decide whether exclusive arm control is real, given who publishes each command topic.

    A vendor joystick or phone app steers the same servos without going through this gateway, so the
    lease alone does not prove exclusivity. Anything the gateway cannot see is treated as unsafe:
    an unreadable topology refuses enabling rather than assuming nobody else is publishing.
    """
    allowed = {own_node, *(exempt or [])}
    foreign: dict[str, list[str]] = {}
    unknown: list[str] = []
    for topic, names in topology.items():
        if names is None:
            unknown.append(topic)
            continue
        others = sorted({name for name in names if name not in allowed})
        if others:
            foreign[topic] = others
    reason = ("vendor_publisher_present" if foreign
              else "publisher_topology_unknown" if unknown else None)
    return {"clean": reason is None, "foreign": foreign, "unknown": sorted(unknown),
            "reason": reason}


def evaluation_policy(task_cfg: dict[str, Any]) -> dict[str, Any]:
    """Which ground truth is allowed to decide an episode's success.

    The physics gate reads privileged Gazebo poses. On the real car those topics never arrive, so
    applying it would label every teleop episode a failure invented from absent data; the operator's
    manual label, or "unknown", is the only honest verdict there.
    """
    if str(task_cfg.get("labels") or "") == "manual_or_unknown":
        return {"source": "manual_or_unknown", "simulator_truth_available": False}
    return {"source": "gazebo_model_truth", "simulator_truth_available": True}


def manual_success_label(value: Any) -> bool | None:
    """Coerce whatever a client sent into True, False or None (no label)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "success", "1", "ok"}:
            return True
        if text in {"false", "failure", "failed", "0"}:
            return False
    return None


def manual_evaluation(task_cfg: dict[str, Any], *, task: str, reason: str,
                      camera_profile: str | None = None,
                      manual_success: bool | None = None, note: str = "") -> dict[str, Any]:
    """Episode verdict for a profile that declares no simulator ground truth."""
    return {
        "success": manual_success if manual_success is not None else "unknown",
        "labels": str(task_cfg.get("labels") or "manual_or_unknown"),
        "task": task,
        "failure_reasons": [],
        "reason": reason,
        "manual_label": manual_success,
        "note": note or None,
        "camera_profile": camera_profile,
        "privileged_truth_used_for_scripted_expert_only": False,
        "evaluation_policy": evaluation_policy(task_cfg),
        "timestamp_wall": time.time(),
    }


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


# Exact v1 wire envelope. Received/deadline stamps are assigned locally, so any
# field outside this set is a malformed or forged frame rather than extra data.
HYBRID_ENVELOPE_FIELDS = frozenset({
    "protocol_version", "session_id", "session_epoch", "channel", "source", "seq",
    "sample_seq", "ttl_ms", "calibration_bundle_hash", "target", "clutch", "clutch_label",
})


class HybridCommandGate:
    """Protocol v1 admission and local deadline queue; this object does not actuate.

    `receive` is the local ingress boundary. `forward` sends only remaining TTL;
    `pop` rechecks the original local deadline immediately before dispatch.
    """
    SOURCES = {"base": "gamepad", "arm": "so101_leader", "gripper": "so101_leader"}

    def __init__(self, session_id: str, session_epoch: int, bundle_hash: str,
                 max_ttl_ms: int, clock_ns=time.monotonic_ns):
        if (not all(isinstance(s, str) and s for s in (session_id, bundle_hash)) or
            type(session_epoch) is not int or not 0 <= session_epoch < 2 ** 53):
            raise ValueError("session and calibration identity required")
        if isinstance(max_ttl_ms, bool) or not isinstance(max_ttl_ms, int) or max_ttl_ms <= 0:
            raise ValueError("invalid TTL cap")
        self.session_id, self.epoch, self.bundle_hash = session_id, session_epoch, bundle_hash
        self.max_ttl_ms, self.clock_ns = max_ttl_ms, clock_ns
        self.last_seq: dict[tuple[str, str], int] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self.retired = False

    def clear(self) -> None:
        with self._lock:
            self.pending.clear()

    def receive(self, command: dict[str, Any], permitted: set[str]) -> dict[str, Any]:
        with self._lock:
            return self._receive(command, permitted)

    def _receive(self, command: dict[str, Any], permitted: set[str]) -> dict[str, Any]:
        if self.retired:
            raise ContractError("session_retired")
        if not isinstance(command, dict):
            raise ContractError("invalid_envelope")
        c = dict(command)
        if "received_mono_ns" in c or "deadline_mono_ns" in c:
            raise ContractError("local_clock_fields_forbidden")
        unexpected = sorted(set(c) - HYBRID_ENVELOPE_FIELDS)
        if unexpected:
            raise ContractError("unexpected_envelope_field")
        if (type(c.get("protocol_version")) is not int or c["protocol_version"] != 1 or
            c.get("session_id") != self.session_id or type(c.get("session_epoch")) is not int or
            c["session_epoch"] != self.epoch):
            raise ContractError("session_mismatch")
        channel, source = c.get("channel"), c.get("source")
        if not isinstance(channel, str) or channel not in self.SOURCES or source != self.SOURCES[channel]:
            raise ContractError("bad_channel_source")
        if channel not in permitted:
            raise ContractError("channel_not_permitted")
        if c.get("calibration_bundle_hash") != self.bundle_hash:
            raise ContractError("calibration_mismatch")
        seq, sample_seq, ttl = c.get("seq"), c.get("sample_seq"), c.get("ttl_ms")
        for value in (seq, sample_seq, ttl):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError("invalid_envelope")
        if not 0 < ttl <= self.max_ttl_ms:
            raise ContractError("invalid_ttl")
        key = (channel, source)
        # Sequence numbers are strictly increasing, not contiguous.  The
        # deadline queue is deliberately latest-wins, so dropping an older
        # target must not look like a transport sequence fault.  Only a replay
        # or a backwards sequence is unsafe and retires the session.
        if key in self.last_seq and seq <= self.last_seq[key]:
            self.pending.clear()
            self.retired = True
            raise ContractError("stale_seq")
        target = c.get("target")
        if not isinstance(target, dict):
            raise ContractError("invalid_target")
        if channel == "base":
            if set(target) != {"vx_m_s", "vy_m_s", "wz_rad_s"}:
                raise ContractError("invalid_base_target")
            for name in ("vx_m_s", "vy_m_s", "wz_rad_s"):
                _finite(target.get(name), name)
        elif channel == "arm":
            if (c.get("clutch") is not True or set(target) != {"joint_names", "positions_rad"} or
                target.get("joint_names") != ["joint1", "joint2", "joint3", "joint4", "joint5"]):
                raise ContractError("invalid_arm_target")
            positions = target.get("positions_rad")
            if not isinstance(positions, list) or len(positions) != 5:
                raise ContractError("invalid_arm_target")
            for value in positions:
                _finite(value, "position_rad")
        else:
            if (c.get("clutch") is not True or set(target) != {"joint_name", "position_rad"} or
                target.get("joint_name") != "r_joint"):
                raise ContractError("invalid_gripper_target")
            _finite(target.get("position_rad"), "position_rad")
        now = self.clock_ns()
        # Copy mutable nested data to prevent callers changing a validated target afterwards.
        try:
            c = json.loads(json.dumps(c, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ContractError("invalid_json_envelope") from exc
        c["received_mono_ns"] = now
        c["deadline_mono_ns"] = now + ttl * 1_000_000
        self.last_seq[key] = seq
        # Replacing a still-pending target is the intended drop/merge policy.
        self.pending[channel] = c
        return json.loads(json.dumps(c))

    def pop(self, channel: str, permitted: set[str]) -> dict[str, Any] | None:
        with self._lock:
            c = self.pending.pop(channel, None)
            if c is None:
                return None
            if channel not in permitted or self.clock_ns() >= c["deadline_mono_ns"]:
                self.pending.clear()
                self.retired = True
                return None
            return c

    def forward(self, command: dict[str, Any]) -> dict[str, Any]:
        remaining = (command["deadline_mono_ns"] - self.clock_ns()) // 1_000_000
        if remaining <= 0:
            raise ContractError("expired")
        result = {k: v for k, v in command.items() if k not in ("received_mono_ns", "deadline_mono_ns")}
        result["ttl_ms"] = min(result["ttl_ms"], remaining)
        return result


def hybrid_feedback_gate(samples: list[dict[str, Any]], calibration_hash: str,
                         now_ns: int, max_age_ns: int, max_scan_span_ns: int | None = None) -> dict[str, Any]:
    """Gate six individually timestamped servo readbacks; do not repair bad readings."""
    names = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]
    ids = [1, 2, 3, 4, 5, 10]
    errors = []
    if max_scan_span_ns is None:
        max_scan_span_ns = max_age_ns
    if (not isinstance(samples, list) or len(samples) != 6 or
        not isinstance(calibration_hash, str) or not calibration_hash or type(now_ns) is not int or now_ns < 0 or
        isinstance(max_age_ns, bool) or not isinstance(max_age_ns, int) or max_age_ns <= 0 or
        isinstance(max_scan_span_ns, bool) or not isinstance(max_scan_span_ns, int) or max_scan_span_ns <= 0):
        return {"valid": False, "reasons": ["feedback_incomplete"], "scan_span_ns": None}
    by_name = {s["name"]: s for s in samples if isinstance(s, dict) and isinstance(s.get("name"), str)}
    if set(by_name) != set(names):
        return {"valid": False, "reasons": ["feedback_joint_identity_mismatch"], "scan_span_ns": None}
    times = []
    for name, servo_id in zip(names, ids):
        s = by_name[name]
        if (s.get("source_kind") != "servo_position_readback" or type(s.get("servo_id")) is not int or
            s.get("servo_id") != servo_id):
            errors.append(f"{name}:untrusted_source_or_id")
        if s.get("valid") is not True or s.get("error") not in (None, "") or s.get("calibration_hash") != calibration_hash:
            errors.append(f"{name}:invalid_or_calibration_mismatch")
        try:
            _finite(s.get("position_rad"), name)
            raw = _finite(s.get("raw_position"), name + ".raw")
            if raw < -1000 or raw > 1000:
                errors.append(f"{name}:raw_out_of_range")
        except ContractError:
            errors.append(f"{name}:nonfinite_or_missing")
        if not isinstance(s.get("clock_domain"), str) or not s["clock_domain"]:
            errors.append(f"{name}:clock_domain_missing")
        stamp, seq = s.get("received_mono_ns"), s.get("sample_seq")
        if (isinstance(stamp, bool) or not isinstance(stamp, int) or stamp > now_ns or now_ns - stamp >= max_age_ns or
            isinstance(seq, bool) or not isinstance(seq, int) or seq < 0):
            errors.append(f"{name}:stale_or_invalid_timestamp")
        else:
            times.append(stamp)
    span = max(times) - min(times) if len(times) == 6 else None
    if span is not None and span > max_scan_span_ns:
        errors.append("scan_span_exceeded")
    return {"valid": not errors, "reasons": errors, "scan_span_ns": span}
