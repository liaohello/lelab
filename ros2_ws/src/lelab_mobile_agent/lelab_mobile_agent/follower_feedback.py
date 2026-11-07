"""Follower measurement provenance and conservative freshness gate; no hardware I/O."""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

ENCODER_RESOLUTION = 1000
OBSERVED_RAW_MIN = -ENCODER_RESOLUTION
RADIANS_PER_ENCODER_TICK = 240 / 360 * (2 * math.pi) / 1000
ENCODER_TICKS_PER_RADIAN = 1 / RADIANS_PER_ENCODER_TICK
SOURCES = frozenset({"servo_position_readback", "command_cache", "model_placeholder", "synthetic"})


@dataclass
class JointSample:
    name: str
    position_rad: float
    wall: float
    source: str
    servo_id: int | None = None
    raw_position: float | None = None
    sample_seq: int | None = None
    sample_time_ns: int | None = None
    clock_domain: str = "host_monotonic"
    valid: bool = False
    error: str | None = None
    calibration_hash: str | None = None


@dataclass
class FeedbackStore:
    expected_joints: list[str]
    stale_after_s: float = 0.5
    calibration_hash: str | None = None
    servo_ids: dict[str, int] = field(default_factory=dict)
    max_scan_span_s: float = 0.5
    allow_synthetic: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    samples: dict[str, JointSample] = field(default_factory=dict)
    pulses: dict[int, float] = field(default_factory=dict)
    pulse_wall: float | None = None
    pulse_source: str | None = None
    ingests: int = 0
    rejects: int = 0

    def __post_init__(self) -> None:
        if (not self.expected_joints or len(set(self.expected_joints)) != len(self.expected_joints)
                or not math.isfinite(self.stale_after_s) or self.stale_after_s <= 0
                or not math.isfinite(self.max_scan_span_s) or self.max_scan_span_s <= 0):
            raise ValueError("invalid feedback configuration")
        ids = list(self.servo_ids.values())
        if (any(name not in self.expected_joints for name in self.servo_ids)
                or any(type(servo_id) is not int or not 0 <= servo_id <= 253 for servo_id in ids)
                or len(set(ids)) != len(ids)
                or ("r_joint" in self.servo_ids and self.servo_ids["r_joint"] != 10)):
            raise ValueError("invalid servo id mapping")

    def ingest_sample(self, sample: JointSample) -> bool:
        """Ingest a provenance-bearing reading; retain out-of-range raw values as invalid."""
        sample = replace(sample)
        with self.lock:
            previous = self.samples.get(sample.name)
            if (sample.name not in self.expected_joints or not math.isfinite(sample.wall)
                    or (previous is not None and (sample.wall <= previous.wall
                        or (isinstance(sample.sample_seq, int) and isinstance(previous.sample_seq, int)
                            and sample.sample_seq <= previous.sample_seq)))):
                self.rejects += 1
                return False
            error = sample.error
            if sample.source not in SOURCES:
                error = error or "unknown_source"
            if not math.isfinite(sample.position_rad):
                error = error or "nonfinite_position"
            if sample.raw_position is not None:
                if not math.isfinite(sample.raw_position):
                    error = error or "nonfinite_raw_position"
                elif not OBSERVED_RAW_MIN <= sample.raw_position <= ENCODER_RESOLUTION:
                    error = error or "raw_out_of_range"
            if sample.source == "servo_position_readback" and sample.raw_position is None:
                error = error or "raw_position_missing"
            if self.servo_ids.get(sample.name) != sample.servo_id or sample.name not in self.servo_ids:
                error = error or "servo_id_mismatch"
            if not self.calibration_hash or sample.calibration_hash != self.calibration_hash:
                error = error or "calibration_mismatch"
            if not isinstance(sample.sample_seq, int) or isinstance(sample.sample_seq, bool) or sample.sample_seq < 0:
                error = error or "sample_seq_invalid"
            sample.error = error
            sample.valid = sample.valid is True and error is None
            self.samples[sample.name] = sample
            self.ingests += 1
            return True

    def ingest_joint_states(self, names: Iterable[str], positions: Iterable[float], wall: float,
                            source: str) -> None:
        # A generic JointState topic has no proof of physical readback or calibration.
        ns, ps = list(names), list(positions)
        if len(ns) != len(ps):
            self.rejects += 1
        for name, value in zip(ns, ps):
            try:
                self.ingest_sample(JointSample(name, float(value), wall, source))
            except (TypeError, ValueError):
                self.rejects += 1

    def ingest_servo_pulses(self, id_to_pulse: dict[int, float], wall: float, source: str) -> None:
        with self.lock:
            if not math.isfinite(wall) or (self.pulse_wall is not None and wall <= self.pulse_wall):
                self.rejects += 1
                return
            self.pulses = {int(k): float(v) for k, v in id_to_pulse.items()}
            self.pulse_wall, self.pulse_source = wall, source

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        with self.lock:
            missing = [j for j in self.expected_joints if j not in self.samples]
            ages = {n: now - s.wall for n, s in self.samples.items()}
            oldest = max(ages.values()) if ages else None
            span = max((s.wall for s in self.samples.values()), default=0) - min((s.wall for s in self.samples.values()), default=0)
            allowed = {"servo_position_readback"} | ({"synthetic"} if self.allow_synthetic else set())
            reason = "ok"
            if not self.samples:
                reason = "no_feedback_yet"
            elif missing:
                reason = "joints_missing:" + ",".join(missing)
            elif not math.isfinite(now) or any(not math.isfinite(a) or a < 0 or a > self.stale_after_s for a in ages.values()):
                reason = f"feedback_stale:{oldest}"
            elif span > self.max_scan_span_s:
                reason = "scan_span_exceeded"
            elif any(not s.valid or s.source not in allowed or s.calibration_hash != self.calibration_hash for s in self.samples.values()):
                reason = "invalid_or_untrusted_feedback"
            def finite_or_none(value: Any) -> float | int | None:
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    return value
                return None
            details = {n: {"name": s.name, "servo_id": s.servo_id, "raw_position": finite_or_none(s.raw_position),
                           "position_rad": finite_or_none(s.position_rad), "source_kind": s.source, "sample_seq": s.sample_seq,
                           "received_mono_ns": int(s.wall * 1e9), "sample_time_ns": s.sample_time_ns,
                           "clock_domain": s.clock_domain, "age_s": ages[n], "valid": (s.valid and s.source in allowed and 0 <= ages[n] <= self.stale_after_s),
                           "error": s.error, "calibration_hash": s.calibration_hash}
                       for n, s in self.samples.items()}
            return {"available": reason == "ok", "reason": reason, "expected_joints": list(self.expected_joints),
                    "missing_joints": missing, "positions_rad": {n: finite_or_none(s.position_rad) for n, s in self.samples.items()},
                    "age_s": ages, "oldest_age_s": oldest, "scan_span_s": span,
                    "stale_after_s": self.stale_after_s, "source": sorted({s.source for s in self.samples.values()}) or None,
                    "joint_samples": details, "servo_pulses": dict(self.pulses),
                    "servo_pulses_age_s": None if self.pulse_wall is None else now - self.pulse_wall,
                    "servo_pulses_source": self.pulse_source, "ingests": self.ingests, "rejects": self.rejects}

    def gate(self, now: float | None = None) -> tuple[bool, str]:
        snap = self.snapshot(now)
        return bool(snap["available"]), snap["reason"]

    def clear(self, reason: str = "") -> dict[str, Any]:
        with self.lock:
            self.samples.clear()
            self.pulses.clear()
            self.pulse_wall = self.pulse_source = None
        return {"cleared": True, "reason": reason}


class LateReplyGuard:
    """Serialize requests; a timeout poisons the channel until explicit transport reset.

    Local tokens only manage ownership; they never identify wire replies.
    """
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.generation = 0
        self.inflight = False
        self.poisoned = False

    def begin(self) -> int | None:
        with self.lock:
            if self.inflight or self.poisoned:
                return None
            self.inflight = True
            self.generation += 1
            return self.generation

    def finish(self, generation: int, payload: Any) -> tuple[bool, Any]:
        with self.lock:
            current = self.inflight and generation == self.generation and not self.poisoned
            if current:
                self.inflight = False
            return current, payload

    def abandon(self, generation: int) -> None:
        with self.lock:
            if self.inflight and generation == self.generation:
                self.inflight = False
                self.poisoned = True

    def reset_after_transport_reset(self) -> None:
        with self.lock:
            if self.inflight:
                raise RuntimeError("request in progress")
            self.poisoned = False


def pulse_to_rad(raw: float, initial_position_raw: int = 500, flipped: bool = False) -> float:
    if flipped:
        return (initial_position_raw - raw) * RADIANS_PER_ENCODER_TICK
    return (raw - initial_position_raw) * RADIANS_PER_ENCODER_TICK


def rad_to_pulse(angle_rad: float, initial_position_raw: int = 500, flipped: bool = False) -> float:
    ticks = angle_rad * ENCODER_TICKS_PER_RADIAN
    return initial_position_raw - ticks if flipped else initial_position_raw + ticks


def clamp_pulse(raw: float, min_pulse: float = 0, max_pulse: float = ENCODER_RESOLUTION - 1) -> float:
    return max(min_pulse, min(max_pulse, raw))


def gripper_width_m_for_pulse(pulse: float, angle_zero: float = 200.0,
                              link_bc: float = 0.024, link_ec: float = 0.020,
                              link_hb: float = 0.030, link_jc: float = 0.018,
                              lcd_plus_ecd: float = 0.0) -> float:
    """Jaw width from the gripper servo pulse, mirroring the vendor linkage geometry.

    lander_pi utils compute width as 2*KE with KE = HB + cos(theta)*BC - cos(LCD+ECD)*EC, where
    theta = radians((angle - angle_zero) / 1000 * 180). The percentage is never treated as radians.
    """
    import math

    angle_deg = (pulse - angle_zero) / ENCODER_RESOLUTION * 180.0
    theta = math.radians(angle_deg)
    ke = link_hb + math.cos(theta) * link_bc - math.cos(lcd_plus_ecd) * link_ec
    return 2.0 * ke
