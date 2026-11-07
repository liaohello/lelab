"""T3 Wave A: pure, ROS-free channel coordination for hybrid manual teleoperation.

This module never opens a serial port, camera or gamepad, never starts a service and
never drives an actuator.  It owns only the *logical* admission boundary:

  * one session owner, three independent channel permits (base / arm / gripper);
  * sequence numbers keyed by (session_epoch, channel, source);
  * a latest-wins deadline queue that is purged on expiry, epoch change, stop and
    rollback;
  * a trusted-feedback gate that refuses vendor command caches / placeholders /
    synthetic samples as "measured".

Everything returned is JSON-serialisable.  Python 3.10 compatible (car side).
"""
from __future__ import annotations

import copy
import math
import time
from typing import Any

PROTOCOL_VERSION = 1
CHANNELS = ("base", "arm", "gripper")
CHANNEL_SOURCES = {
    "base": "gamepad",
    "arm": "so101_leader",
    "gripper": "so101_leader",
}
ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"]
FEEDBACK_JOINTS = ARM_JOINTS + ["r_joint"]
FEEDBACK_SERVO_IDS = {"joint1": 1, "joint2": 2, "joint3": 3, "joint4": 4, "joint5": 5, "r_joint": 10}
TRUSTED_SOURCE = "servo_position_readback"
UNTRUSTED_SOURCES = ("command_cache", "model_placeholder", "synthetic", "vendor_joint_states")
CONFIRMATION_LEVELS = ("requested", "gateway_accepted", "driver_sent",
                       "controller_acknowledged", "execution_observed")
# Exact wire envelope for v1. Received/deadline stamps are assigned locally, so a
# frame that carries them (or any unknown field) is a malformed frame, not data.
ENVELOPE_FIELDS = frozenset({
    "protocol_version", "session_id", "session_epoch", "channel", "source", "seq",
    "sample_seq", "ttl_ms", "calibration_bundle_hash", "target", "clutch", "clutch_label",
})
PHASES = ("drive", "observe", "grasp", "transport", "release")


class HybridSessionError(ValueError, RuntimeError):
    """User-actionable refusal.

    ValueError makes the existing /ros-mobile route guard answer 409 with a readable
    detail; RuntimeError keeps the earlier T3 contract tests valid.
    """


class HybridProtocolError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HybridProtocolError("not_numeric", f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise HybridProtocolError("not_finite", f"{name} must be finite")
    return number


def _seq(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HybridProtocolError("invalid_envelope", f"{name} must be a non-negative integer")
    return value


def _identity(value: Any, name: str) -> str | int:
    """Session / epoch / bundle identity: opaque string or integer, never bool/float/None."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise HybridProtocolError("invalid_envelope", f"{name} must be a non-empty string or integer")


def validate_target(channel: str, target: Any, clutch: Any) -> dict[str, Any]:
    """Validate a v1 target without clamping, inventing or repairing anything."""
    if not isinstance(target, dict):
        raise HybridProtocolError("invalid_target", "target must be an object")
    if channel == "base":
        for name in ("vx_m_s", "vy_m_s", "wz_rad_s"):
            _finite(target.get(name), name)
        if set(target) != {"vx_m_s", "vy_m_s", "wz_rad_s"}:
            raise HybridProtocolError("invalid_target", "base target has unexpected fields")
    elif channel == "arm":
        if clutch is not True and clutch != "engaged":
            raise HybridProtocolError("invalid_arm_target", "arm requires an engaged clutch")
        if target.get("joint_names") != ARM_JOINTS:
            raise HybridProtocolError("invalid_arm_target", "arm joint_names mismatch")
        positions = target.get("positions_rad")
        if not isinstance(positions, list) or len(positions) != len(ARM_JOINTS):
            raise HybridProtocolError("invalid_arm_target", "arm needs five positions_rad")
        for value in positions:
            _finite(value, "position_rad")
    elif channel == "gripper":
        if clutch is not True and clutch != "engaged":
            raise HybridProtocolError("invalid_gripper_target", "gripper requires an engaged clutch")
        if target.get("joint_name") != "r_joint":
            raise HybridProtocolError("invalid_gripper_target", "gripper must be the physical r_joint")
        _finite(target.get("position_rad"), "position_rad")
        if set(target) != {"joint_name", "position_rad"}:
            raise HybridProtocolError("invalid_gripper_target", "one physical actuator only")
    else:
        raise HybridProtocolError("bad_channel", f"unknown channel {channel!r}")
    return copy.deepcopy(target)


def command_v1(*, session_id: str, session_epoch: str, channel: str, source: str,
               seq: int, sample_seq: int, ttl_ms: int, calibration_bundle_hash: str,
               target: dict[str, Any], clutch: bool | None = None) -> dict[str, Any]:
    """Build a protocol-v1 logical command.  Empty / non-finite targets are refused."""
    if channel not in CHANNELS or CHANNEL_SOURCES[channel] != source:
        raise HybridProtocolError("bad_channel_source", f"{channel!r} does not accept {source!r}")
    for value, name in ((session_id, "session_id"), (session_epoch, "session_epoch"),
                        (calibration_bundle_hash, "calibration_bundle_hash")):
        _identity(value, name)
    _seq(seq, "seq")
    _seq(sample_seq, "sample_seq")
    _seq(ttl_ms, "ttl_ms")
    if ttl_ms <= 0:
        raise HybridProtocolError("invalid_ttl", "ttl_ms must be positive")
    command: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "session_id": session_id,
        "session_epoch": session_epoch,
        "channel": channel,
        "source": source,
        "seq": seq,
        "sample_seq": sample_seq,
        "ttl_ms": ttl_ms,
        "calibration_bundle_hash": calibration_bundle_hash,
        "target": validate_target(channel, target, clutch),
    }
    if channel != "base":
        # Boolean matches the car-side HybridCommandGate admission contract; the
        # semantic string is kept alongside it for audit/readability.
        command["clutch"] = clutch is True or clutch == "engaged"
        command["clutch_label"] = "engaged" if command["clutch"] else "released"
    return command


class ChannelPermits:
    """Per-channel authorisation; there is deliberately no global mode switch."""

    def __init__(self) -> None:
        self._epoch: str | int | None = None
        self._granted: dict[str, bool] = {name: False for name in CHANNELS}

    def begin_epoch(self, epoch: str | int) -> None:
        self._epoch = _identity(epoch, "session_epoch")
        self.revoke(*CHANNELS)

    def grant(self, epoch: str, *channels: str) -> None:
        if epoch != self._epoch:
            raise HybridProtocolError("epoch_mismatch", "permits belong to another session epoch")
        for channel in channels:
            if channel not in CHANNELS:
                raise HybridProtocolError("bad_channel", f"unknown channel {channel!r}")
            self._granted[channel] = True

    def revoke(self, *channels: str) -> None:
        for channel in channels or CHANNELS:
            if channel in self._granted:
                self._granted[channel] = False

    def permitted(self, channel: str, epoch: str) -> bool:
        return epoch == self._epoch and self._granted.get(channel, False)

    def allowed(self, epoch: str) -> set[str]:
        return {name for name in CHANNELS if self.permitted(name, epoch)}

    def clear(self) -> None:
        self._epoch = None
        self.revoke(*CHANNELS)

    def snapshot(self, epoch: str | None = None) -> dict[str, Any]:
        active = epoch if epoch is not None else self._epoch
        return {"epoch": self._epoch,
                "granted": {name: (self.permitted(name, active) if active else False) for name in CHANNELS}}


class SequenceRegistry:
    """Monotonic sequence per (session_epoch, channel, source)."""

    def __init__(self) -> None:
        self._last: dict[tuple[str, str, str], int] = {}

    @staticmethod
    def key(epoch: str, channel: str, source: str) -> tuple[str, str, str]:
        return (epoch, channel, source)

    def check(self, epoch: str, channel: str, source: str, seq: Any) -> bool:
        _seq(seq, "seq")
        return seq > self._last.get(self.key(epoch, channel, source), -1)

    def advance(self, epoch: str, channel: str, source: str, seq: int) -> None:
        self._last[self.key(epoch, channel, source)] = seq

    def clear_epoch(self, keep_epoch: str | None = None) -> int:
        """Drop old-session state; returns how many sequence slots were discarded."""
        before = len(self._last)
        self._last = {key: value for key, value in self._last.items()
                      if keep_epoch is not None and key[0] == keep_epoch}
        return before - len(self._last)

    def snapshot(self) -> dict[str, Any]:
        return {"slots": len(self._last),
                "last_seq": {"|".join(str(part) for part in key): value
                             for key, value in sorted(self._last.items(),
                                                      key=lambda item: tuple(str(x) for x in item[0]))}}


class DeadlineQueue:
    """One latest intent per channel, each with its original local deadline."""

    def __init__(self) -> None:
        self._pending: dict[str, dict[str, Any]] = {}

    def put(self, command: dict[str, Any]) -> None:
        deadline = command.get("deadline_mono_ns")
        if not isinstance(deadline, int) or isinstance(deadline, bool):
            raise HybridProtocolError("invalid_deadline")
        self._pending[command["channel"]] = copy.deepcopy(command)

    def purge_expired(self, now_ns: int) -> int:
        expired = [channel for channel, command in self._pending.items()
                   if now_ns >= command["deadline_mono_ns"]]
        for channel in expired:
            self._pending.pop(channel, None)
        return len(expired)

    def pop(self, channel: str, now_ns: int) -> dict[str, Any] | None:
        self.purge_expired(now_ns)
        command = self._pending.pop(channel, None)
        if command is None or now_ns >= command["deadline_mono_ns"]:
            return None
        return command

    def clear(self) -> int:
        count = len(self._pending)
        self._pending.clear()
        return count

    def snapshot(self, now_ns: int) -> dict[str, Any]:
        return {channel: {"seq": command["seq"],
                          "remaining_ms": max(0, (command["deadline_mono_ns"] - now_ns) // 1_000_000)}
                for channel, command in self._pending.items()}


class TrustedFeedbackGate:
    """Hardware mode only accepts a T1 servo readback. Never repairs bad readings."""

    @staticmethod
    def gate(snapshot: Any, *, calibration_hash: str, now_ns: int,
             max_age_ns: int) -> dict[str, Any]:
        reasons: list[str] = []
        samples = snapshot.get("samples") if isinstance(snapshot, dict) else snapshot
        if isinstance(snapshot, dict) and samples is None:
            joint_samples = snapshot.get("joint_samples")
            if isinstance(joint_samples, dict):
                samples = list(joint_samples.values())
        if isinstance(snapshot, dict):
            kind = snapshot.get("source_kind")
            declared_sources = {
                item.get("source_kind") for item in samples or []
                if isinstance(item, dict)
            } if isinstance(samples, list) else set()
            # A T1 FeedbackStore snapshot has a source on each joint rather than
            # duplicating it in the envelope. Neither shape may silently turn an
            # unknown source into a trusted sample.
            if kind is not None and kind != TRUSTED_SOURCE:
                reasons.append(f"untrusted_source:{kind!r}")
            elif kind is None and declared_sources and declared_sources != {TRUSTED_SOURCE}:
                reasons.append(f"untrusted_source:{sorted(str(v) for v in declared_sources)}")
            if snapshot.get("clock_domain") in (None, "", "unknown"):
                reasons.append("clock_domain_unknown")
        if not isinstance(samples, list) or len(samples) != len(FEEDBACK_JOINTS):
            return {"valid": False, "trusted": False, "reasons": reasons + ["feedback_incomplete"],
                    "source_kind": None, "scan_span_ns": None, "joints": {}}
        if max_age_ns <= 0:
            return {"valid": False, "trusted": False, "reasons": reasons + ["invalid_age_limit"],
                    "source_kind": None, "scan_span_ns": None, "joints": {}}
        by_name = {sample.get("name"): sample for sample in samples if isinstance(sample, dict)}
        if set(by_name) != set(FEEDBACK_JOINTS) or len(by_name) != len(FEEDBACK_JOINTS):
            return {"valid": False, "trusted": False,
                    "reasons": reasons + ["feedback_joint_identity_mismatch"],
                    "source_kind": None, "scan_span_ns": None, "joints": {}}
        stamps: list[int] = []
        joints: dict[str, Any] = {}
        for name in FEEDBACK_JOINTS:
            sample = by_name[name]
            joint_reasons: list[str] = []
            kind = sample.get("source_kind")
            if kind != TRUSTED_SOURCE or kind in UNTRUSTED_SOURCES:
                joint_reasons.append("untrusted_source")
            if sample.get("servo_id") != FEEDBACK_SERVO_IDS[name]:
                joint_reasons.append("servo_id_mismatch")
            if sample.get("valid") is not True or sample.get("error") not in (None, ""):
                joint_reasons.append("sample_invalid")
            if sample.get("calibration_hash") != calibration_hash:
                joint_reasons.append("calibration_mismatch")
            try:
                _finite(sample.get("position_rad"), name)
                raw = _finite(sample.get("raw_position"), name + ".raw_position")
                if not -1000 <= raw <= 1000:
                    joint_reasons.append("raw_out_of_range")
            except HybridProtocolError as exc:
                joint_reasons.append(exc.reason)
            stamp, sample_seq = sample.get("received_mono_ns"), sample.get("sample_seq")
            try:
                _seq(sample_seq, "sample_seq")
            except HybridProtocolError as exc:
                joint_reasons.append(exc.reason)
            age_ns = sample.get("age_ns")
            if isinstance(age_ns, bool) or not isinstance(age_ns, int) or age_ns < 0:
                age_ns = None
            age_s = sample.get("age_s")
            if age_ns is None and isinstance(age_s, (int, float)) and not isinstance(age_s, bool):
                age_ns = int(float(age_s) * 1_000_000_000)
            stamp_age = now_ns - stamp if isinstance(stamp, int) and not isinstance(stamp, bool) else None
            effective_age = max(
                value for value in (age_ns, stamp_age) if value is not None
            ) if any(value is not None for value in (age_ns, stamp_age)) else None
            if (isinstance(stamp, bool) or not isinstance(stamp, int) or stamp > now_ns or
                    effective_age is None or effective_age >= max_age_ns):
                joint_reasons.append("stale_or_invalid_timestamp")
            else:
                stamps.append(stamp)
                joints[name] = {"age_ns": effective_age,
                                "position_rad": sample.get("position_rad"),
                                "raw_position": sample.get("raw_position"),
                                "source_kind": kind or sample.get("source_kind")}
            reasons.extend(f"{name}:{reason}" for reason in joint_reasons)
        return {"valid": not reasons, "trusted": not reasons, "reasons": reasons,
                "source_kind": TRUSTED_SOURCE if not reasons else None,
                "scan_span_ns": max(stamps) - min(stamps) if len(stamps) == len(FEEDBACK_JOINTS) else None,
                "joints": joints,
                "note": "six sequential readbacks, not a simultaneous sample; oldest joint age gates the set"}


class HybridChannelRouter:
    """Admission + deadline + dispatch boundary. No output unless explicitly enabled.

    `driver` is the T1 fixture/interface: capabilities(), read_snapshot(), submit(command),
    stop(reason).  It is injected, never constructed here; with driver=None nothing can
    ever be actuated.
    """

    def __init__(self, *, session_id: str, session_epoch: str, bundle_hash: str,
                 max_ttl_ms: int = 200, clock_ns=time.monotonic_ns, driver: Any | None = None,
                 outputs_enabled: bool = False):
        for value, name in ((session_id, "session_id"), (session_epoch, "session_epoch"),
                            (bundle_hash, "calibration_bundle_hash")):
            _identity(value, name)
        if isinstance(max_ttl_ms, bool) or not isinstance(max_ttl_ms, int) or max_ttl_ms <= 0:
            raise HybridProtocolError("invalid_ttl")
        self.session_id, self.epoch, self.bundle_hash = session_id, session_epoch, bundle_hash
        self.max_ttl_ms, self.clock_ns, self.driver = max_ttl_ms, clock_ns, driver
        self.outputs_enabled = bool(outputs_enabled) and driver is not None
        self.permits = ChannelPermits()
        self.sequences = SequenceRegistry()
        self.queue = DeadlineQueue()
        self.feedback_gate = TrustedFeedbackGate()
        self.last_error: str | None = None
        self.events: list[dict[str, Any]] = []
        self.permits.begin_epoch(session_epoch)

    def _audit(self, event: str, detail: str = "") -> None:
        self.events.append({"event": event, "detail": detail, "epoch": self.epoch})

    def grant(self, *channels: str) -> dict[str, Any]:
        self.permits.grant(self.epoch, *channels)
        self._audit("grant", ",".join(channels))
        return {"granted": sorted(self.permits.allowed(self.epoch))}

    def revoke(self, *channels: str) -> dict[str, Any]:
        self.permits.revoke(*channels)
        self._audit("revoke", ",".join(channels or CHANNELS))
        return {"granted": sorted(self.permits.allowed(self.epoch))}

    def enable_outputs(self) -> dict[str, Any]:
        if self.driver is None:
            raise HybridProtocolError("driver_unavailable")
        self.outputs_enabled = True
        self._audit("outputs_enabled")
        return {"outputs_enabled": True}

    def begin_epoch(self, epoch: str | int, bundle_hash: str | int | None = None) -> dict[str, Any]:
        """New session / reconnect / calibration change: discard all old intents."""
        _identity(epoch, "session_epoch")
        if bundle_hash is not None:
            _identity(bundle_hash, "calibration_bundle_hash")
        dropped = self.queue.clear()
        discarded = self.sequences.clear_epoch(None)
        self.epoch = epoch
        if bundle_hash is not None:
            self.bundle_hash = bundle_hash
        self.permits.begin_epoch(epoch)
        self.outputs_enabled = False
        self._audit("begin_epoch", f"queue_dropped={dropped},seq_slots_dropped={discarded}")
        return {"epoch": epoch, "queue_dropped": dropped, "seq_slots_dropped": discarded,
                "outputs_enabled": False}

    def submit(self, command: dict[str, Any], now_ns: int | None = None) -> dict[str, Any]:
        """Validate first; a rejected frame must not consume a sequence number."""
        if not isinstance(command, dict):
            raise HybridProtocolError("invalid_command")
        unexpected = sorted(set(command) - ENVELOPE_FIELDS)
        if unexpected:
            # A remote timestamp or any extra field is a malformed/forged frame.
            raise HybridProtocolError("unexpected_field", f"unexpected envelope fields: {unexpected}")
        now = self.clock_ns() if now_ns is None else now_ns
        if command.get("protocol_version") != PROTOCOL_VERSION:
            raise HybridProtocolError("protocol_version_mismatch")
        if command.get("session_id") != self.session_id or command.get("session_epoch") != self.epoch:
            raise HybridProtocolError("session_mismatch", "old session, wrong epoch or replayed frame")
        if command.get("calibration_bundle_hash") != self.bundle_hash:
            raise HybridProtocolError("calibration_mismatch")
        channel, source = command.get("channel"), command.get("source")
        if channel not in CHANNELS or CHANNEL_SOURCES[channel] != source:
            raise HybridProtocolError("bad_channel_source")
        if source == "maintenance":
            raise HybridProtocolError("maintenance_source_forbidden")
        if not self.permits.permitted(channel, self.epoch):
            raise HybridProtocolError("channel_not_permitted", f"{channel} is not granted")
        ttl = _seq(command.get("ttl_ms"), "ttl_ms")
        if ttl <= 0 or ttl > self.max_ttl_ms:
            raise HybridProtocolError("invalid_ttl")
        seq, sample_seq = command.get("seq"), command.get("sample_seq")
        if not self.sequences.check(self.epoch, channel, source, seq):
            raise HybridProtocolError("stale_seq")
        _seq(sample_seq, "sample_seq")
        target = validate_target(channel, command.get("target"), command.get("clutch"))
        # Deadline is local monotonic only; forwarding must never reset the full TTL.
        stored = copy.deepcopy(command)
        stored["target"] = target
        stored["received_mono_ns"] = now
        stored["deadline_mono_ns"] = now + ttl * 1_000_000
        self.sequences.advance(self.epoch, channel, source, seq)
        self.queue.put(stored)
        self._audit("accepted", f"{channel}/{source} seq={seq} ttl_ms={ttl}")
        return {"accepted": True, "confirmation": "gateway_accepted", "channel": channel,
                "seq": seq, "deadline_mono_ns": stored["deadline_mono_ns"],
                "outputs_enabled": self.outputs_enabled}

    def dispatch(self, channel: str, now_ns: int | None = None) -> dict[str, Any] | None:
        now = self.clock_ns() if now_ns is None else now_ns
        command = self.queue.pop(channel, now)
        if command is None:
            return None
        if not self.permits.permitted(channel, self.epoch):
            self._audit("dispatch_rejected", "permit revoked before dispatch")
            return None
        if not self.outputs_enabled or self.driver is None:
            return {"command": command, "confirmation": "gateway_accepted",
                    "driver_sent": False, "controller_acknowledged": "unavailable",
                    "execution_observed": False, "outputs_enabled": False,
                    "reason": "no_output_wave_a"}
        try:
            result = self.driver.submit(command)
        except BaseException as exc:  # cancellation must roll back too
            self.rollback(f"driver_submit_failed:{type(exc).__name__}")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return {"command": command, "confirmation": "gateway_accepted",
                    "driver_sent": False, "outputs_enabled": False,
                    "error": type(exc).__name__,
                    "error_detail": str(exc)}
        self._audit("driver_sent", f"{channel} seq={command['seq']}")
        return {"command": command, "confirmation": "driver_sent", "driver_sent": True,
                "controller_acknowledged": "unavailable", "execution_observed": False,
                "outputs_enabled": True, "driver_result": result}

    def trusted_feedback(self, snapshot: Any, *, now_ns: int | None = None,
                         max_age_ns: int = 150_000_000) -> dict[str, Any]:
        return self.feedback_gate.gate(snapshot, calibration_hash=self.bundle_hash,
                                       now_ns=self.clock_ns() if now_ns is None else now_ns,
                                       max_age_ns=max_age_ns)

    def stop(self, reason: str) -> dict[str, Any]:
        """Stop first: clear intents and permits before any network/recording wrap-up."""
        dropped = self.queue.clear()
        self.permits.revoke(*CHANNELS)
        self.outputs_enabled = False
        driver_stopped = False
        if self.driver is not None:
            try:
                self.driver.stop(str(reason))
                driver_stopped = True
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"driver_stop_failed:{type(exc).__name__}"
        self._audit("stop", f"reason={reason},queue_dropped={dropped}")
        return {"stopped": True, "reason": str(reason), "queue_dropped": dropped,
                "outputs_enabled": False, "driver_stopped": driver_stopped,
                "last_error": self.last_error}

    def rollback(self, reason: str) -> dict[str, Any]:
        self.last_error = str(reason)
        result = self.stop(reason)
        self.sequences.clear_epoch(None)
        self._audit("rollback", reason)
        return {**result, "rolled_back": True}

    def snapshot(self, now_ns: int | None = None) -> dict[str, Any]:
        now = self.clock_ns() if now_ns is None else now_ns
        return {"session_id": self.session_id, "session_epoch": self.epoch,
                "calibration_bundle_hash": self.bundle_hash,
                "permits": self.permits.snapshot(self.epoch),
                "sequences": self.sequences.snapshot(),
                "pending": self.queue.snapshot(now),
                "outputs_enabled": self.outputs_enabled, "last_error": self.last_error,
                "trusted_feedback_source": TRUSTED_SOURCE,
                "confirmation_levels": list(CONFIRMATION_LEVELS)}
