"""Follower calibration job for the LanderPi car arm.

Pure Python on purpose: no ``rclpy`` import, no serial port, no ROS message type.
The agent injects a command sink, a six-servo scan reader, the readback snapshot
source and the readback pause/resume hooks, so the whole state machine runs under
unit tests with a fake clock and a recording sink.

Two capture phases:

``manual_range``
    The operator moves joint1..joint5 by hand after an explicit per-axis torque
    release.  A background worker keeps ingesting the unique ``/dev/rrc`` owner's
    readback snapshot, so coverage never depends on how often the browser polls.

``gripper_drag``
    Only ``r_joint`` (servo id 10) receives targets.  The browser slider is a
    normalised position inside an operator-confirmed safe window; the job maps it
    to explicit raw ticks, rate limits to 10 Hz, stamps every command with a TTL
    and the current pointer generation, and requires two operator-marked
    closed/open rounds before the result can be saved.

All positions are vendor raw ticks (0..1000, 500 = 0 rad, 240 deg per 1000 ticks,
``r_joint`` flipped by its YAML ``min > max``).  Radians are never produced here:
the vendor ``JointPositionController`` remains the only unit authority, so the
browser never grows a second, private copy of the conversion.
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import statistics
import threading
import time
from collections import deque
from typing import Any, Callable

JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint")
ARM_JOINTS = JOINTS[:5]
GRIPPER_JOINT = "r_joint"
SERVO_IDS = {"joint1": 1, "joint2": 2, "joint3": 3, "joint4": 4, "joint5": 5, "r_joint": 10}

RAW_MIN, RAW_MAX = 0, 1000
OBSERVED_RAW_MIN = -1000
RAW_CENTRE = 500
# Vendor geometry (joint_position_controller.py).  Kept here only to report the
# equivalent value in evidence; no asset is ever produced from this module.
RADIANS_PER_TICK = (240.0 / 360.0) * (2.0 * math.pi) / 1000.0

MAX_TARGET_TTL_MS = 300
MIN_TARGET_TTL_MS = 20
TARGET_INTERVAL_S = 0.1          # hard ceiling: 10 Hz
MARK_SETTLE_S = 0.35             # longer than the largest accepted TTL

DEFAULT_JOB_TTL_S = 1800.0       # absolute lifetime; active calibration can take 30 min
DEFAULT_IDLE_TTL_S = 15.0        # stop when the browser stops talking to us
DEFAULT_REVIEW_TTL_S = 180.0     # read-only window after stop, for commit

MAX_SAFE_WINDOW_SPAN = 1000      # vendor command range; operator watches the mechanism
MIN_SAFE_WINDOW_SPAN = 40

ARM_MIN_SAMPLES = 30
ARM_MIN_SPAN_RAW = 80
ARM_MAX_NOISE_RAW = 6.0
GRIPPER_MIN_SAMPLES = 20
GRIPPER_MIN_SPAN_RAW = 40

MAX_JUMP_RAW = 250               # single-sample mechanical impossibility
LOG_ENTRY_CAP = 400              # per joint; extremes + periodic liveness
LOG_INTERVAL_S = 0.5


class CalibrationJobError(Exception):
    """HTTP-flavoured failure. ``status`` maps onto ``ApiError`` in the agent."""

    def __init__(self, status: int, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.status = status
        self.reason = reason
        self.detail = detail


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return int(value)


def _as_int(value: Any) -> int | None:
    """Strict integer: reject bools, floats and numeric strings."""
    return value if type(value) is int else None


def _utc(ts: float | None) -> str | None:
    if ts is None or not math.isfinite(ts):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class JointRange:
    """Accumulated coverage for one servo, fed only by accepted readback samples."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.servo_id = SERVO_IDS[name]
        self.reference_raw: int | None = None
        self.min_raw: int | None = None
        self.max_raw: int | None = None
        self.samples = 0
        self.anomalies = 0
        self.duplicates = 0
        self.stale = 0
        self.missing = 0
        self.last_raw: int | None = None
        self.last_seq: int | None = None
        self.last_time_ns: int | None = None
        self.last_mono: float | None = None
        self.first_wall: float | None = None
        self.last_wall: float | None = None
        self.log: list[dict[str, Any]] = []
        self.log_truncated = False
        self._deltas: deque[int] = deque(maxlen=40)
        self._last_log_mono: float | None = None
        self._previous_raw: int | None = None
        self._previous_previous_raw: int | None = None

    @property
    def span_raw(self) -> int:
        if self.min_raw is None or self.max_raw is None:
            return 0
        return int(self.max_raw - self.min_raw)

    @property
    def noise_raw(self) -> float | None:
        """Robust jitter floor: median absolute second difference.

        A steady hand sweep has a near-constant first difference, so its second
        difference is ~0; a flapping or mis-decoded readback is not.  Using the
        first difference instead would report real operator motion as noise.
        """
        if not self._deltas:
            return None
        return round(float(statistics.median(self._deltas)), 3)

    def reset(self, reference_raw: int | None = None) -> None:
        self.reference_raw = reference_raw
        self.min_raw = reference_raw
        self.max_raw = reference_raw
        self.samples = 1 if reference_raw is not None else 0
        self.anomalies = 0
        self.duplicates = 0
        self.stale = 0
        self.missing = 0
        self.last_raw = reference_raw
        self.last_seq = None
        self.last_time_ns = None
        self.last_mono = None
        self.first_wall = None
        self.last_wall = None
        self.log = []
        self.log_truncated = False
        self._deltas.clear()
        self._last_log_mono = None
        self._previous_raw = None
        self._previous_previous_raw = None

    def _log(self, raw: int, seq: int | None, mono: float, wall: float) -> None:
        new_extreme = (self.min_raw is None or raw < self.min_raw or raw > self.max_raw)
        due = self._last_log_mono is None or (mono - self._last_log_mono) >= LOG_INTERVAL_S
        if not new_extreme and not due:
            return
        if len(self.log) >= LOG_ENTRY_CAP:
            self.log_truncated = True
            return
        self.log.append({
            "joint": self.name,
            "seq": seq,
            "t_ms": int(round((wall - (self.first_wall or wall)) * 1000)),
            "raw": raw,
        })
        self._last_log_mono = mono

    def ingest(self, sample: dict[str, Any] | None, *, now_mono: float, now_wall: float,
               stale_after_s: float) -> str:
        if not isinstance(sample, dict):
            self.missing += 1
            return "missing"
        raw = _int_or_none(sample.get("raw_position"))
        if raw is None or not OBSERVED_RAW_MIN <= raw <= RAW_MAX:
            self.anomalies += 1
            return "out_of_range"
        source = sample.get("source_kind")
        if source not in (None, "servo_position_readback"):
            # /controller_manager/joint_states and any synthetic vector are command
            # echoes, never measurements.
            self.anomalies += 1
            return "untrusted_source"
        # An unbound rad conversion (error == "calibration_not_configured") does not
        # make the raw position less real; that is exactly what this job measures.
        error = sample.get("error")
        if sample.get("valid") is False and error != "calibration_not_configured":
            self.stale += 1
            return "invalid"
        age = sample.get("age_s")
        if not isinstance(age, (int, float)) or isinstance(age, bool) or not math.isfinite(age):
            self.stale += 1
            return "age_unknown"
        if age < 0 or age > stale_after_s:
            self.stale += 1
            return "stale"
        seq = _as_int(sample.get("sample_seq"))
        time_ns = _as_int(sample.get("sample_time_ns"))
        if seq is not None and self.last_seq is not None and seq <= self.last_seq:
            if time_ns is not None and self.last_time_ns is not None and time_ns > self.last_time_ns:
                # The readback counter went back but the timestamp advanced: a new
                # driver epoch started.  Treating it as fresh coverage would mix two
                # sample spaces, so the job faults instead.
                raise CalibrationJobError(
                    409, "readback_sequence_regressed",
                    f"{self.name}: sample_seq {seq} after {self.last_seq}")
            self.duplicates += 1
            return "duplicate"

        jumped = (self._previous_raw is not None and abs(raw - self._previous_raw) > MAX_JUMP_RAW)
        if jumped:
            # Recorded, never clamped: a physically impossible step must not extend
            # the observed range.
            self.anomalies += 1
            self._previous_previous_raw = self._previous_raw
            self._previous_raw = raw
            self.last_raw = raw
            self.last_seq = seq if seq is not None else self.last_seq
            self.last_time_ns = time_ns if time_ns is not None else self.last_time_ns
            self.last_mono = now_mono
            self.last_wall = now_wall
            self._log(raw, seq, now_mono, now_wall)
            return "jump"

        if self._previous_raw is not None:
            if self._previous_previous_raw is not None:
                # Second difference: immune to constant sweep velocity.
                self._deltas.append(
                    abs(raw - 2 * self._previous_raw + self._previous_previous_raw))
            self._previous_previous_raw = self._previous_raw
        elif self.reference_raw is not None:
            self._deltas.append(0)
        self._previous_raw = raw
        if self.reference_raw is None:
            self.reference_raw = raw
        self.min_raw = raw if self.min_raw is None else min(self.min_raw, raw)
        self.max_raw = raw if self.max_raw is None else max(self.max_raw, raw)
        self.samples += 1
        self.last_raw = raw
        self.last_seq = seq if seq is not None else self.last_seq
        self.last_time_ns = time_ns if time_ns is not None else self.last_time_ns
        self.last_mono = now_mono
        if self.first_wall is None:
            self.first_wall = now_wall
        self.last_wall = now_wall
        self._log(raw, seq, now_mono, now_wall)
        return "accepted"

    def assess(self, *, min_samples: int, min_span_raw: int, max_noise_raw: float) -> dict[str, Any]:
        """Per-joint qualification.

        Missing, duplicate, stale and anomalous samples are *counted* and never
        extend coverage; only the three coverage criteria below invalidate a joint,
        plus a small anomaly allowance (one corrupt frame must not throw away a
        five minute capture, ten must).
        """
        reasons: list[str] = []
        if self.samples < min_samples:
            reasons.append("samples_low")
        if self.span_raw < min_span_raw:
            reasons.append("span_low")
        noise = self.noise_raw
        if noise is None:
            reasons.append("noise_unknown")
        elif noise > max_noise_raw:
            reasons.append("noise_high")
        allowance = max(2, int(self.samples * 0.02))
        if self.anomalies > allowance:
            reasons.append("anomalies_exceeded")
        return {
            "valid": not reasons,
            "reasons": reasons,
            "min_span_raw": min_span_raw,
            "min_samples": min_samples,
            "max_noise_raw": max_noise_raw,
            "anomaly_allowance": allowance,
        }

    def snapshot(self, now_mono: float) -> dict[str, Any]:
        return {
            "name": self.name,
            "servo_id": self.servo_id,
            "reference_raw": self.reference_raw,
            "observed_min_raw": self.min_raw,
            "observed_max_raw": self.max_raw,
            "span_raw": self.span_raw,
            "samples": self.samples,
            "noise_raw": self.noise_raw,
            "anomalies": self.anomalies,
            "duplicates": self.duplicates,
            "stale": self.stale,
            "missing": self.missing,
            "last_raw": self.last_raw,
            "last_seq": self.last_seq,
            "age_s": None if self.last_mono is None else round(max(0.0, now_mono - self.last_mono), 3),
        }


class FollowerRangeSampler:
    """Six independent range accumulators sharing one sequential scan."""

    def __init__(self, *, stale_after_s: float = 0.5,
                 arm_min_samples: int = ARM_MIN_SAMPLES,
                 arm_min_span_raw: int = ARM_MIN_SPAN_RAW,
                 gripper_min_samples: int = GRIPPER_MIN_SAMPLES,
                 gripper_min_span_raw: int = GRIPPER_MIN_SPAN_RAW,
                 max_noise_raw: float = ARM_MAX_NOISE_RAW) -> None:
        self.stale_after_s = stale_after_s
        self.arm_min_samples = arm_min_samples
        self.arm_min_span_raw = arm_min_span_raw
        self.gripper_min_samples = gripper_min_samples
        self.gripper_min_span_raw = gripper_min_span_raw
        self.max_noise_raw = max_noise_raw
        self.ranges = {name: JointRange(name) for name in JOINTS}
        self.rounds = 0
        self.last_reason: str | None = "no_readback_yet"

    def reset(self, baseline: dict[str, int] | None = None) -> None:
        baseline = baseline or {}
        for name, accumulator in self.ranges.items():
            accumulator.reset(baseline.get(name))
        self.rounds = 0
        self.last_reason = "no_readback_yet"

    def ingest(self, snapshot: dict[str, Any], *, now_mono: float,
               now_wall: float) -> dict[str, Any]:
        samples = snapshot.get("joint_samples") if isinstance(snapshot, dict) else None
        samples = samples if isinstance(samples, dict) else {}
        outcome: dict[str, str] = {}
        for name, accumulator in self.ranges.items():
            outcome[name] = accumulator.ingest(
                samples.get(name), now_mono=now_mono, now_wall=now_wall,
                stale_after_s=self.stale_after_s)
        self.rounds += 1
        self.last_reason = snapshot.get("reason") if isinstance(snapshot, dict) else None
        return outcome

    def assess(self, name: str) -> dict[str, Any]:
        accumulator = self.ranges[name]
        if name == GRIPPER_JOINT:
            return accumulator.assess(min_samples=self.gripper_min_samples,
                                      min_span_raw=self.gripper_min_span_raw,
                                      max_noise_raw=self.max_noise_raw)
        return accumulator.assess(min_samples=self.arm_min_samples,
                                  min_span_raw=self.arm_min_span_raw,
                                  max_noise_raw=self.max_noise_raw)

    def snapshot(self, now_mono: float) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, accumulator in self.ranges.items():
            snap = accumulator.snapshot(now_mono)
            snap.update(self.assess(name))
            out[name] = snap
        return out

    def arm_valid(self) -> tuple[bool, list[str]]:
        missing = [name for name in ARM_JOINTS if not self.assess(name)["valid"]]
        return not missing, missing

    def sample_log(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name in JOINTS:
            rows.extend(self.ranges[name].log)
        rows.sort(key=lambda row: (row["t_ms"], SERVO_IDS[row["joint"]]))
        return rows


class GripperDragCalibrator:
    """Only servo id 10, only inside a confirmed safe window, at most 10 Hz."""

    def __init__(self, *, safe_min_raw: int, safe_max_raw: int,
                 mark_tolerance_raw: float = 12.0,
                 stability_span_raw: float = 6.0,
                 stability_samples: int = 3,
                 stability_window_s: float = 1.0,
                 max_error_raw: float = 18.0,
                 max_error_streak: int = 5,
                 stall_streak_limit: int = 5,
                 stall_tolerance_raw: float = 2.0,
                 progress_timeout_s: float = 1.5,
                 progress_tolerance_raw: float = 2.0,
                 min_gap_raw: int = 20,
                 usable_margin_raw: int = 8,
                 repeatability_tolerance_raw: int = 15,
                 min_rounds: int = 2,
                 max_commands: int = 400,
                 max_drag_commands: int = 150,
                 stale_after_s: float = 0.5) -> None:
        self.safe_min_raw = int(safe_min_raw)
        self.safe_max_raw = int(safe_max_raw)
        self.mark_tolerance_raw = float(mark_tolerance_raw)
        self.stability_span_raw = float(stability_span_raw)
        self.stability_samples = int(stability_samples)
        self.stability_window_s = float(stability_window_s)
        self.max_error_raw = float(max_error_raw)
        self.max_error_streak = int(max_error_streak)
        self.stall_streak_limit = int(stall_streak_limit)
        self.stall_tolerance_raw = float(stall_tolerance_raw)
        self.progress_timeout_s = float(progress_timeout_s)
        self.progress_tolerance_raw = float(progress_tolerance_raw)
        self.min_gap_raw = int(min_gap_raw)
        self.usable_margin_raw = int(usable_margin_raw)
        self.repeatability_tolerance_raw = int(repeatability_tolerance_raw)
        self.min_rounds = int(min_rounds)
        self.max_commands = int(max_commands)
        self.max_drag_commands = int(max_drag_commands)
        self.stale_after_s = float(stale_after_s)

        self.pointer_generation = 0
        self.generation_open = False
        self.last_client_seq: int | None = None
        self.pending: dict[str, Any] | None = None
        self.published_target: int | None = None
        self.published_target_mono: float | None = None
        self.last_target_request_mono: float | None = None
        self.last_target_ttl_s: float | None = None
        self.last_accept_mono: float | None = None
        self.commands_sent = 0
        self.commands_dropped = 0
        self.rate_limited = 0
        self.marks: list[dict[str, Any]] = []
        self.fault: str | None = None
        self.stall_streak = 0
        self.error_streak = 0
        self._feedback_window: deque[tuple[float, int]] = deque(maxlen=60)
        self.last_feedback_raw: int | None = None
        self.last_feedback_mono: float | None = None
        self._feedback_at_publish: int | None = None
        self._stall_moved = False
        self._best_error: float | None = None
        self._last_progress_mono: float | None = None

    # ------------------------------------------------------------------ mapping
    def permille_to_raw(self, permille: float) -> tuple[int, bool]:
        """0..1000 permille maps linearly onto the confirmed window, clamped."""
        value = float(permille)
        clamped = min(1000.0, max(0.0, value))
        span = self.safe_max_raw - self.safe_min_raw
        raw = int(round(self.safe_min_raw + span * clamped / 1000.0))
        raw = min(self.safe_max_raw, max(self.safe_min_raw, raw))
        return raw, clamped != value

    # ------------------------------------------------------------------ targets
    def request_target(self, *, permille: float, client_seq: int, ttl_ms: float,
                       pointer_active: Any, pointer_generation: Any,
                       safe_min_raw: Any, safe_max_raw: Any,
                       now: float) -> dict[str, Any]:
        if pointer_active is not True:
            raise CalibrationJobError(409, "pointer_not_active",
                                      "targets are only accepted while the pointer is down")
        if safe_min_raw is not None or safe_max_raw is not None:
            if (_as_int(safe_min_raw) != self.safe_min_raw
                    or _as_int(safe_max_raw) != self.safe_max_raw):
                raise CalibrationJobError(
                    409, "safe_window_mismatch",
                    f"job window is [{self.safe_min_raw}, {self.safe_max_raw}]")
        if isinstance(permille, bool) or not isinstance(permille, (int, float)):
            raise CalibrationJobError(400, "invalid_target_permille", repr(permille))
        if not math.isfinite(float(permille)):
            raise CalibrationJobError(400, "invalid_target_permille", "not finite")
        if isinstance(ttl_ms, bool) or not isinstance(ttl_ms, (int, float)):
            raise CalibrationJobError(400, "invalid_ttl", repr(ttl_ms))
        if not math.isfinite(float(ttl_ms)) or not MIN_TARGET_TTL_MS <= float(ttl_ms) <= MAX_TARGET_TTL_MS:
            raise CalibrationJobError(400, "invalid_ttl",
                                      f"require {MIN_TARGET_TTL_MS}..{MAX_TARGET_TTL_MS} ms")
        if _as_int(client_seq) is None or client_seq < 0:
            raise CalibrationJobError(400, "invalid_client_seq", repr(client_seq))

        if pointer_generation is None:
            self.pointer_generation += 1
            self.generation_open = True
            self.last_client_seq = None
        else:
            if (_as_int(pointer_generation) is None
                    or pointer_generation != self.pointer_generation
                    or not self.generation_open):
                raise CalibrationJobError(
                    409, "pointer_generation_mismatch",
                    f"current generation is {self.pointer_generation}")
        if self.last_client_seq is not None and client_seq <= self.last_client_seq:
            raise CalibrationJobError(409, "client_seq_not_increasing",
                                      f"{client_seq} after {self.last_client_seq}")
        if self.commands_sent >= self.max_commands:
            raise CalibrationJobError(409, "command_budget_exhausted",
                                      f"{self.commands_sent} commands")
        if self.commands_sent >= self.max_drag_commands:
            raise CalibrationJobError(409, "drag_budget_exhausted", "restart the drag session")

        if self.last_accept_mono is not None and (now - self.last_accept_mono) < TARGET_INTERVAL_S:
            self.rate_limited += 1
            return {"accepted": False, "published": False, "reason": "rate_limited",
                    "retry_after_ms": int(round(
                        (TARGET_INTERVAL_S - (now - self.last_accept_mono)) * 1000))}

        raw, clamped = self.permille_to_raw(permille)
        self.pending = {"raw": raw, "seq": client_seq, "ttl_s": float(ttl_ms) / 1000.0,
                        "deadline": now + float(ttl_ms) / 1000.0, "requested_mono": now}
        self.last_client_seq = client_seq
        self.last_accept_mono = now
        self.last_target_request_mono = now
        self.last_target_ttl_s = float(ttl_ms) / 1000.0
        return {"accepted": True, "published": False, "target_raw": raw,
                "target_permille": round(min(1000.0, max(0.0, float(permille))), 3),
                "permille_clamped": clamped, "client_seq": client_seq,
                "pointer_generation": self.pointer_generation,
                "expires_in_ms": int(ttl_ms)}

    def consume_pending(self, now: float) -> int | None:
        """Return the raw target the worker must publish now, or None."""
        target = self.pending
        if target is None:
            return None
        if now > float(target["deadline"]):
            self.pending = None
            self.commands_dropped += 1
            return None
        self.pending = None
        return int(target["raw"])

    def note_published(self, raw: int, now: float) -> None:
        self.commands_sent += 1
        self.published_target = int(raw)
        self.published_target_mono = now
        self._feedback_at_publish = self.last_feedback_raw
        self._stall_moved = False
        self.stall_streak = 0
        self._best_error = (None if self.last_feedback_raw is None
                            else abs(float(self.last_feedback_raw) - raw))
        self._last_progress_mono = now

    # ------------------------------------------------------------------ feedback
    def ingest_feedback(self, raw: int, now: float) -> None:
        self.last_feedback_raw = int(raw)
        self.last_feedback_mono = now
        self._feedback_window.append((now, int(raw)))

    def check(self, now: float) -> str | None:
        if self.fault:
            return self.fault
        if (self.last_feedback_mono is None
                or (now - self.last_feedback_mono) > self.stale_after_s):
            return None  # reported as stale by mark(); a lost link faults in the job
        if self.published_target is None:
            return None
        feedback = self.last_feedback_raw
        if feedback is None:
            return None
        if (self.last_target_request_mono is None
                or self.last_target_ttl_s is None
                or now > self.last_target_request_mono + self.last_target_ttl_s):
            # The pointer stopped supplying targets. Hold the last command and let
            # the operator mark a stable observed endpoint; do not require it to
            # reach a target that has already expired.
            self.error_streak = 0
            self.stall_streak = 0
            self._best_error = None
            self._last_progress_mono = None
            return None
        held = now - (self.published_target_mono or now)
        if held < min(0.4, self.progress_timeout_s / 3.0):
            return None
        error = abs(feedback - self.published_target)
        if error <= self.max_error_raw:
            self.error_streak = 0
            self.stall_streak = 0
            self._best_error = error
            self._last_progress_mono = now
            return None
        if (self._best_error is None
                or error <= self._best_error - self.progress_tolerance_raw):
            self._best_error = error
            self._last_progress_mono = now
            self.stall_streak = 0
            return None
        if (self._last_progress_mono is not None
                and now - self._last_progress_mono >= self.progress_timeout_s):
            self.stall_streak += 1
            self.fault = "gripper_tracking_error"
            return self.fault
        return None

    # ------------------------------------------------------------------ marks
    def _round_index(self) -> int:
        closed = sum(1 for mark in self.marks if mark["end"] == "closed")
        opened = sum(1 for mark in self.marks if mark["end"] == "open")
        return min(closed, opened) + 1

    def mark(self, end: Any, now: float) -> dict[str, Any]:
        if end not in ("closed", "open"):
            raise CalibrationJobError(400, "invalid_mark_end", repr(end))
        if self.fault:
            raise CalibrationJobError(409, "gripper_calibration_faulted", self.fault)
        if self.last_target_request_mono is None:
            raise CalibrationJobError(409, "gripper_no_target_yet",
                                      "drag the slider before marking an end")
        if self.published_target is None:
            raise CalibrationJobError(409, "gripper_no_target_yet", "no commanded position")
        settle = now - self.last_target_request_mono
        if settle < MARK_SETTLE_S:
            raise CalibrationJobError(
                409, "gripper_settle_pending",
                f"wait {int(round((MARK_SETTLE_S - settle) * 1000))} ms after releasing")
        if (self.last_feedback_mono is None
                or (now - self.last_feedback_mono) > self.stale_after_s):
            raise CalibrationJobError(409, "gripper_feedback_stale",
                                      "no fresh servo id 10 feedback")
        window = [raw for mono, raw in self._feedback_window
                  if now - mono <= self.stability_window_s]
        if len(window) < self.stability_samples:
            raise CalibrationJobError(409, "gripper_not_stable",
                                      f"{len(window)}/{self.stability_samples} samples")
        if max(window) - min(window) > self.stability_span_raw:
            raise CalibrationJobError(409, "gripper_not_stable",
                                      f"span {max(window) - min(window)} raw while still moving")
        error = abs(self.last_feedback_raw - self.published_target)
        if error > self.mark_tolerance_raw:
            raise CalibrationJobError(409, "gripper_mark_error_exceeded",
                                      f"error {error} raw > {self.mark_tolerance_raw}")
        raw = int(self.last_feedback_raw)
        if not self.safe_min_raw <= raw <= self.safe_max_raw:
            raise CalibrationJobError(409, "gripper_mark_outside_window",
                                      f"{raw} outside [{self.safe_min_raw}, {self.safe_max_raw}]")
        round_index = self._round_index()
        for existing in self.marks:
            if existing["end"] == end and existing["round"] == round_index:
                raise CalibrationJobError(409, "duplicate_mark",
                                          f"{end} already marked in round {round_index}")
        record = {
            "end": end,
            "round": round_index,
            "raw": raw,
            "target_raw": int(self.published_target),
            "error_raw": int(error),
            "stable_span_raw": int(max(window) - min(window)),
            "feedback_age_s": round(max(0.0, now - (self.last_feedback_mono or now)), 3),
            "sample_count": len(window),
        }
        self.marks.append(record)
        return record

    def clear_fault(self) -> None:
        self.fault = None
        self.stall_streak = 0
        self.error_streak = 0
        self._best_error = None
        self._last_progress_mono = None

    # ------------------------------------------------------------------ summary
    def summary(self, now: float) -> dict[str, Any]:
        closed = [mark["raw"] for mark in self.marks if mark["end"] == "closed"]
        opened = [mark["raw"] for mark in self.marks if mark["end"] == "open"]
        rounds = min(len(closed), len(opened))
        reasons: list[str] = []
        repeatability = None
        direction = None
        usable_closed = usable_open = None

        if rounds < self.min_rounds:
            reasons.append("repeat_rounds_missing")
        if rounds >= 1:
            closed_mean = statistics.fmean(closed)
            open_mean = statistics.fmean(opened)
            separation = open_mean - closed_mean
            direction = 1 if separation > 0 else (-1 if separation < 0 else 0)
            if abs(separation) < self.min_gap_raw:
                reasons.append("endpoint_separation_low")
            if direction == 0:
                reasons.append("direction_unknown")
        if rounds >= 2:
            closed_spread = max(closed) - min(closed)
            open_spread = max(opened) - min(opened)
            repeatability = int(max(closed_spread, open_spread))
            if repeatability > self.repeatability_tolerance_raw:
                reasons.append("repeatability_exceeded")
            signs = set()
            ordered = sorted(self.marks, key=lambda mark: (mark["round"], mark["end"] == "open"))
            by_round: dict[int, dict[str, int]] = {}
            for mark in ordered:
                by_round.setdefault(mark["round"], {})[mark["end"]] = mark["raw"]
            for pair in by_round.values():
                if "closed" in pair and "open" in pair:
                    delta = pair["open"] - pair["closed"]
                    signs.add(1 if delta > 0 else (-1 if delta < 0 else 0))
            if len(signs) > 1:
                reasons.append("direction_inconsistent")
        if direction in (1, -1) and rounds >= 1:
            closed_mean = statistics.fmean(closed)
            open_mean = statistics.fmean(opened)
            usable_closed = int(round(closed_mean + direction * self.usable_margin_raw))
            usable_open = int(round(open_mean - direction * self.usable_margin_raw))
            usable_closed = min(self.safe_max_raw, max(self.safe_min_raw, usable_closed))
            usable_open = min(self.safe_max_raw, max(self.safe_min_raw, usable_open))
            if abs(usable_open - usable_closed) < max(self.min_gap_raw, 2 * self.usable_margin_raw):
                reasons.append("usable_span_low")
        if self.fault:
            reasons.append(self.fault)
        if self.commands_sent == 0:
            reasons.append("no_commands_sent")

        return {
            "method": "operator_drag_safe_window",
            "safe_window_raw": [self.safe_min_raw, self.safe_max_raw],
            "observed_closed_raw": int(round(statistics.fmean(closed))) if closed else None,
            "observed_open_raw": int(round(statistics.fmean(opened))) if opened else None,
            "usable_closed_raw": usable_closed,
            "usable_open_raw": usable_open,
            "repeatability_raw": repeatability,
            "direction": direction,
            "rounds_completed": rounds,
            "min_rounds": self.min_rounds,
            "marks": [dict(mark) for mark in self.marks],
            "commands_sent": self.commands_sent,
            "commands_dropped": self.commands_dropped,
            "rate_limited": self.rate_limited,
            "max_error_raw": self.max_error_raw,
            "mark_tolerance_raw": self.mark_tolerance_raw,
            "repeatability_tolerance_raw": self.repeatability_tolerance_raw,
            "pointer_generation": self.pointer_generation,
            "last_target_raw": self.published_target,
            "feedback_raw": self.last_feedback_raw,
            "error_raw": (None if self.last_feedback_raw is None or self.published_target is None
                          else int(self.last_feedback_raw - self.published_target)),
            "feedback_age_s": (None if self.last_feedback_mono is None
                               else round(max(0.0, now - self.last_feedback_mono), 3)),
            "settle_remaining_ms": (0 if self.last_target_request_mono is None else
                                    max(0, int(round((MARK_SETTLE_S - (now - self.last_target_request_mono)) * 1000)))),
            "fault": self.fault,
            "valid": not reasons,
            "reasons": reasons,
        }


class FollowerCalibrationJob:
    """One calibration job; the worker thread in the agent advances it by tick()."""

    IDLE = "idle"
    MANUAL = "manual_range"
    DRAG = "gripper_drag"
    REVIEW = "review"
    STOPPED = "stopped"
    FAULT = "fault"

    def __init__(self, *, sink: Any, read_scan: Callable[[], dict[str, int]],
                 readback_snapshot: Callable[[], dict[str, Any]],
                 pause_readback: Callable[[], None] | None = None,
                 resume_readback: Callable[[], None] | None = None,
                 device_identity: Callable[[], dict[str, Any]] | None = None,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 stale_after_s: float = 0.5,
                 job_ttl_s: float = DEFAULT_JOB_TTL_S,
                 idle_ttl_s: float = DEFAULT_IDLE_TTL_S,
                 review_ttl_s: float = DEFAULT_REVIEW_TTL_S,
                 agent_version: str = "unknown",
                 profile: str = "unknown") -> None:
        self.sink = sink
        self.read_scan = read_scan
        self.readback_snapshot = readback_snapshot
        self.pause_readback = pause_readback or (lambda: None)
        self.resume_readback = resume_readback or (lambda: None)
        self.device_identity = device_identity or (lambda: {})
        self.clock = clock
        self.monotonic = monotonic
        self.stale_after_s = stale_after_s
        self.job_ttl_s = job_ttl_s
        self.idle_ttl_s = idle_ttl_s
        self.review_ttl_s = review_ttl_s
        self.agent_version = agent_version
        self.profile = profile

        self.lock = threading.RLock()
        self.sampler = FollowerRangeSampler(stale_after_s=stale_after_s)
        self.calibrator: GripperDragCalibrator | None = None
        self.job_id: str | None = None
        self.token: str | None = None
        self.generation = 0
        self.phase = self.IDLE
        self.stop_reason: str | None = None
        self.faults: list[dict[str, Any]] = []
        self.safe_window_raw: tuple[int, int] | None = None
        self.started_mono: float | None = None
        self.started_wall: float | None = None
        self.stopped_wall: float | None = None
        self.last_touch_mono: float | None = None
        self.absolute_deadline = 0.0
        self.review_deadline = 0.0
        self.released_joints: set[str] = set()
        self.support_confirmed = False
        self.operator = ""
        self.readback_paused = False
        self.drag_started_wall: float | None = None
        self.non_target_reference: dict[str, int] = {}
        self.worker_ticks = 0
        self.last_tick_error: str | None = None
        self.drag_scan_failures = 0
        self.drag_scan_failure_started_mono: float | None = None
        self.manual_arm_ranges: dict[str, dict[str, int]] = {}
        self.manual_arm_ranges_at_utc: str | None = None
        self.manual_gripper_range: dict[str, int] | None = None

    # ------------------------------------------------------------------ helpers
    @property
    def active(self) -> bool:
        return self.phase in (self.MANUAL, self.DRAG)

    def _ensure_idle(self) -> None:
        if self.phase in (self.MANUAL, self.DRAG, self.REVIEW):
            raise CalibrationJobError(409, "follower_calibration_active",
                                      "stop the active calibration first")

    def _token_deadline(self) -> float:
        if self.phase in (self.STOPPED, self.FAULT):
            return self.review_deadline
        return self.absolute_deadline

    def require(self, body: dict[str, Any], *, phases: tuple[str, ...] | None = None) -> None:
        now = self.monotonic()
        if self.phase == self.IDLE or self.token is None:
            raise CalibrationJobError(409, "follower_calibration_not_active",
                                      "start a follower calibration job")
        if body.get("token") != self.token:
            raise CalibrationJobError(409, "follower_calibration_token_invalid",
                                      "token does not match the active job")
        job_id = body.get("job_id")
        if job_id is not None and job_id != self.job_id:
            raise CalibrationJobError(409, "follower_calibration_job_mismatch", str(job_id))
        generation = body.get("generation")
        if generation is not None and _as_int(generation) != self.generation:
            raise CalibrationJobError(409, "follower_calibration_generation_mismatch",
                                      f"{generation} != {self.generation}")
        if now > self._token_deadline():
            self._stop_locked("token_expired")
            raise CalibrationJobError(409, "follower_calibration_expired",
                                      "start follower calibration again")
        if phases is not None and self.phase not in phases:
            raise CalibrationJobError(409, "follower_calibration_phase",
                                      f"phase {self.phase} does not allow this action")
        self.last_touch_mono = now

    def _apply_fault(self, reason: str) -> None:
        self.faults.append({"reason": str(reason), "at_utc": _utc(self.clock())})
        self.stop_reason = str(reason)
        self._finish_locked(self.FAULT)

    def _finish_locked(self, phase: str) -> None:
        """Shared teardown: stop commanding first, then make the arm hold again."""
        if self.readback_paused:
            try:
                self.resume_readback()
            except Exception as exc:  # noqa: BLE001
                self.last_tick_error = f"resume_readback:{type(exc).__name__}"
            self.readback_paused = False
        if self.released_joints:
            try:
                self.sink.set_torque([SERVO_IDS[name] for name in sorted(self.released_joints)], True)
            except Exception as exc:  # noqa: BLE001
                self.last_tick_error = f"restore_torque:{type(exc).__name__}"
            self.released_joints.clear()
        self.phase = phase
        if self.stopped_wall is None:
            self.stopped_wall = self.clock()
        self.review_deadline = self.monotonic() + self.review_ttl_s

    def _stop_locked(self, reason: str) -> None:
        if self.phase in (self.STOPPED, self.FAULT):
            return
        self.stop_reason = str(reason)
        self._finish_locked(self.STOPPED)

    # ------------------------------------------------------------------ lifecycle
    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._ensure_idle()
            now = self.monotonic()
            window = body.get("safe_window_raw")
            if (not isinstance(window, (list, tuple)) or len(window) != 2
                    or _as_int(window[0]) is None or _as_int(window[1]) is None):
                raise CalibrationJobError(400, "invalid_safe_window",
                                          "safe_window_raw must be [min_raw, max_raw] integers")
            low, high = int(window[0]), int(window[1])
            if not (RAW_MIN <= low < high <= RAW_MAX):
                raise CalibrationJobError(400, "invalid_safe_window",
                                          f"require {RAW_MIN} <= min < max <= {RAW_MAX}")
            span = high - low
            if not MIN_SAFE_WINDOW_SPAN <= span <= MAX_SAFE_WINDOW_SPAN:
                raise CalibrationJobError(
                    400, "safe_window_span_invalid",
                    f"span {span} outside {MIN_SAFE_WINDOW_SPAN}..{MAX_SAFE_WINDOW_SPAN}")
            if body.get("operator_confirmed_safe_window") is not True:
                raise CalibrationJobError(409, "safe_window_confirmation_required",
                                          "confirm the safe window before starting")
            if body.get("support_confirmed") is not True:
                raise CalibrationJobError(409, "support_confirmation_required",
                                          "confirm the arm is supported before starting")
            snapshot = self.readback_snapshot()
            samples = snapshot.get("joint_samples") if isinstance(snapshot, dict) else {}
            samples = samples if isinstance(samples, dict) else {}
            baseline: dict[str, int] = {}
            missing: list[str] = []
            for name in JOINTS:
                raw = _int_or_none((samples.get(name) or {}).get("raw_position")) \
                    if isinstance(samples.get(name), dict) else None
                if raw is None:
                    missing.append(name)
                else:
                    baseline[name] = raw
            if missing:
                raise CalibrationJobError(503, "follower_feedback_incomplete",
                                          ",".join(missing))

            self.sampler.reset(baseline)
            self.calibrator = None
            self.job_id = secrets.token_hex(8)
            self.token = secrets.token_urlsafe(24)
            self.generation += 1
            self.phase = self.MANUAL
            self.stop_reason = None
            self.faults = []
            self.safe_window_raw = (low, high)
            self.started_mono = now
            self.started_wall = self.clock()
            self.stopped_wall = None
            self.last_touch_mono = now
            self.absolute_deadline = now + self.job_ttl_s
            self.review_deadline = 0.0
            self.released_joints = set()
            self.support_confirmed = True
            self.operator = str(body.get("operator") or "operator")[:64]
            self.readback_paused = False
            self.drag_started_wall = None
            self.non_target_reference = {}
            self.worker_ticks = 0
            self.last_tick_error = None
            self.drag_scan_failures = 0
            self.drag_scan_failure_started_mono = None
            self.manual_arm_ranges = {}
            self.manual_arm_ranges_at_utc = None
            self.manual_gripper_range = None
            return {
                "token": self.token,
                "job_id": self.job_id,
                "generation": self.generation,
                "phase": self.phase,
                "expires_in_s": round(self.job_ttl_s, 3),
                "idle_timeout_s": self.idle_ttl_s,
                "safe_window_raw": [low, high],
                "baseline_raw": baseline,
                "device_identity": dict(self.device_identity()),
                "outputs_enabled": False,
                "motion": "idle",
                "joints": self.sampler.snapshot(now),
            }

    def manual_ranges(self, body: dict[str, Any]) -> dict[str, Any]:
        """Record operator-entered endpoints without claiming automatic sampling."""
        with self.lock:
            self.require(body)
            ranges = body.get("ranges")
            if not isinstance(ranges, dict) or set(ranges) != set(ARM_JOINTS):
                raise CalibrationJobError(
                    400, "invalid_manual_ranges",
                    "ranges must contain exactly joint1..joint5")
            parsed: dict[str, dict[str, int]] = {}
            for name in ARM_JOINTS:
                item = ranges.get(name)
                if not isinstance(item, dict):
                    raise CalibrationJobError(400, "invalid_manual_ranges", name)
                low = _as_int(item.get("min_raw"))
                high = _as_int(item.get("max_raw"))
                if low is None or high is None or not RAW_MIN <= low < high <= RAW_MAX:
                    raise CalibrationJobError(
                        400, "invalid_manual_ranges",
                        f"{name}: require {RAW_MIN} <= min < max <= {RAW_MAX}")
                if high - low < ARM_MIN_SPAN_RAW:
                    raise CalibrationJobError(
                        400, "manual_range_span_too_small",
                        f"{name}: {high - low} < {ARM_MIN_SPAN_RAW}")
                parsed[name] = {"min_raw": low, "max_raw": high}
            gripper = body.get("gripper")
            parsed_gripper: dict[str, int] | None = None
            if gripper is not None:
                if not isinstance(gripper, dict):
                    raise CalibrationJobError(400, "invalid_manual_gripper", "object required")
                closed = _as_int(gripper.get("closed_raw"))
                opened = _as_int(gripper.get("open_raw"))
                if closed is None or opened is None:
                    raise CalibrationJobError(
                        400, "invalid_manual_gripper", "closed_raw/open_raw are required")
                if self.safe_window_raw is None:
                    raise CalibrationJobError(409, "safe_window_missing", "no confirmed window")
                if not all(self.safe_window_raw[0] <= value <= self.safe_window_raw[1]
                           for value in (closed, opened)):
                    raise CalibrationJobError(
                        409, "manual_gripper_outside_window",
                        f"[{closed},{opened}] outside {list(self.safe_window_raw)}")
                if abs(opened - closed) < 20:
                    raise CalibrationJobError(
                        400, "manual_gripper_span_too_small",
                        f"{abs(opened - closed)} < 20")
                parsed_gripper = {"closed_raw": closed, "open_raw": opened}
            self.manual_arm_ranges = parsed
            self.manual_arm_ranges_at_utc = _utc(self.clock())
            if parsed_gripper is not None:
                self.manual_gripper_range = parsed_gripper
            if self.phase == self.DRAG:
                if self.readback_paused:
                    self.resume_readback()
                    self.readback_paused = False
                self.phase = self.MANUAL
            if self.manual_gripper_range is not None:
                self.stop_reason = "manual_entry_complete"
                self._finish_locked(self.REVIEW)
            return self._snapshot_locked(self.monotonic())

    def _manual_gripper_summary(self) -> dict[str, Any] | None:
        entry = self.manual_gripper_range
        if entry is None:
            return None
        closed, opened = entry["closed_raw"], entry["open_raw"]
        direction = 1 if opened > closed else -1
        usable_closed = max(RAW_MIN, min(RAW_MAX, closed + direction * 8))
        usable_open = max(RAW_MIN, min(RAW_MAX, opened - direction * 8))
        return {
            "method": "operator_manual_entry",
            "safe_window_raw": list(self.safe_window_raw or [RAW_MIN, RAW_MAX]),
            "observed_closed_raw": closed,
            "observed_open_raw": opened,
            "usable_closed_raw": usable_closed,
            "usable_open_raw": usable_open,
            "repeatability_raw": None,
            "direction": direction,
            "rounds_completed": 0,
            "min_rounds": 0,
            "marks": [],
            "commands_sent": 0,
            "commands_dropped": 0,
            "rate_limited": 0,
            "max_error_raw": 18.0,
            "mark_tolerance_raw": 12.0,
            "repeatability_tolerance_raw": 15,
            "pointer_generation": 0,
            "last_target_raw": None,
            "feedback_raw": None,
            "error_raw": None,
            "feedback_age_s": None,
            "settle_remaining_ms": 0,
            "fault": None,
            "valid": abs(opened - closed) >= 20,
            "reasons": [],
        }

    def _arm_validation(self) -> tuple[bool, list[str]]:
        if set(self.manual_arm_ranges) == set(ARM_JOINTS):
            return True, []
        return self.sampler.arm_valid()

    def stop(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.require(body)
            if self.phase not in (self.STOPPED, self.FAULT):
                self._stop_locked(str(body.get("reason") or "operator"))
            return self._snapshot_locked(self.monotonic())

    # ------------------------------------------------------------------ torque
    def torque(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.require(body, phases=(self.MANUAL, self.REVIEW))
            now = self.monotonic()
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                raise CalibrationJobError(400, "invalid_torque_state", "enabled must be boolean")
            joints = body.get("joints")
            if (not isinstance(joints, list) or not joints
                    or any(name not in ARM_JOINTS for name in joints)
                    or len(set(joints)) != len(joints)):
                raise CalibrationJobError(
                    400, "invalid_torque_joints",
                    "joints must be a unique non-empty subset of joint1..joint5")
            if not enabled and body.get("support_confirmed") is not True:
                raise CalibrationJobError(409, "support_confirmation_required",
                                          "confirm the arm is supported before releasing torque")
            if not enabled and self.phase != self.MANUAL:
                raise CalibrationJobError(409, "follower_calibration_phase",
                                          "release torque only during manual range capture")
            servo_ids = [SERVO_IDS[name] for name in joints]
            self.sink.set_torque(servo_ids, enabled)
            if enabled:
                self.released_joints.difference_update(joints)
            else:
                self.released_joints.update(joints)
            return {"enabled": enabled, "joints": list(joints), "servo_ids": servo_ids,
                    "released_joints": sorted(self.released_joints),
                    "all_enabled": not self.released_joints,
                    "outputs_enabled": False}

    # ------------------------------------------------------------------ gripper
    def _enter_drag_locked(self, now: float) -> GripperDragCalibrator:
        if self.released_joints:
            raise CalibrationJobError(
                409, "arm_torque_released",
                "restore joint1..joint5 torque before moving the gripper")
        if self.safe_window_raw is None:
            raise CalibrationJobError(409, "safe_window_missing", "no confirmed window")
        if self.calibrator is None:
            self.calibrator = GripperDragCalibrator(
                safe_min_raw=self.safe_window_raw[0],
                safe_max_raw=self.safe_window_raw[1],
                stale_after_s=self.stale_after_s)
            self.calibrator.ingest_feedback(
                int(self.sampler.ranges[GRIPPER_JOINT].last_raw
                    if self.sampler.ranges[GRIPPER_JOINT].last_raw is not None
                    else self.safe_window_raw[0]), now)
        if self.phase != self.DRAG:
            self.pause_readback()
            self.readback_paused = True
            self.phase = self.DRAG
            self.drag_started_wall = self.clock()
            self.non_target_reference = {
                name: int(self.sampler.ranges[name].last_raw)
                for name in ARM_JOINTS
                if self.sampler.ranges[name].last_raw is not None
            }
        return self.calibrator

    def gripper_target(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.require(body, phases=(self.MANUAL, self.DRAG))
            now = self.monotonic()
            calibrator = self._enter_drag_locked(now)
            result = calibrator.request_target(
                permille=body.get("target_permille"),
                client_seq=body.get("client_seq"),
                ttl_ms=body.get("ttl_ms"),
                pointer_active=body.get("pointer_active"),
                pointer_generation=body.get("pointer_generation"),
                safe_min_raw=body.get("safe_min_raw"),
                safe_max_raw=body.get("safe_max_raw"),
                now=now,
            )
            result.update({
                "job_id": self.job_id,
                "phase": self.phase,
                "pointer_generation": calibrator.pointer_generation,
                "command_budget_remaining": max(0, calibrator.max_commands - calibrator.commands_sent),
                "motion": "controlled_gripper_motion",
                "outputs_enabled": False,
                "gripper": calibrator.summary(now),
            })
            return result

    def gripper_mark(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.require(body, phases=(self.DRAG,))
            now = self.monotonic()
            if self.calibrator is None:
                raise CalibrationJobError(409, "gripper_drag_not_active", "no drag session")
            record = self.calibrator.mark(body.get("end"), now)
            return {"marked": record, "job_id": self.job_id, "phase": self.phase,
                    "gripper": self.calibrator.summary(now), "outputs_enabled": False}

    def gripper_stop(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.require(body)
            if self.phase != self.DRAG:
                raise CalibrationJobError(409, "gripper_drag_not_active", self.phase)
            if self.readback_paused:
                self.resume_readback()
                self.readback_paused = False
            arm_ok, _missing = self.sampler.arm_valid()
            self.phase = self.REVIEW if arm_ok else self.MANUAL
            summary = self.calibrator.summary(self.monotonic()) if self.calibrator else {}
            return {"stopped": True, "job_id": self.job_id, "phase": self.phase,
                    "gripper": summary, "outputs_enabled": False}

    # ------------------------------------------------------------------ status
    def status(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.require(body)
            snapshot = self._snapshot_locked(self.monotonic())
            if body.get("include_evidence") is True:
                snapshot["evidence"] = self.evidence()
            return snapshot

    def _snapshot_locked(self, now: float) -> dict[str, Any]:
        gaps = {
            "temperature": "unavailable",
            "voltage": "unavailable",
            "load": "unavailable",
            "torque_readback": "unavailable",
        }
        gripper = (self._manual_gripper_summary()
                   or (self.calibrator.summary(now) if self.calibrator is not None
                       else {"method": "operator_drag_safe_window", "valid": False,
                             "reasons": ["drag_not_started"], "marks": [],
                             "rounds_completed": 0, "safe_window_raw":
                                 list(self.safe_window_raw) if self.safe_window_raw else None}))
        arm_ok, arm_missing = self._arm_validation()
        return {
            "job_id": self.job_id,
            "generation": self.generation,
            "phase": self.phase,
            "stop_reason": self.stop_reason,
            "expires_in_s": max(0.0, round(self._token_deadline() - now, 3)),
            "idle_expires_in_s": (0.0 if self.last_touch_mono is None else
                                  max(0.0, round(self.idle_ttl_s - (now - self.last_touch_mono), 3))),
            "safe_window_raw": list(self.safe_window_raw) if self.safe_window_raw else None,
            "device_identity": dict(self.device_identity()),
            "torque": {
                "released_joints": sorted(self.released_joints),
                "all_enabled": not self.released_joints,
                "gripper_releasable": False,
            },
            "joints": self.sampler.snapshot(now),
            "arm_valid": arm_ok,
            "arm_invalid_joints": arm_missing,
            "gripper": gripper,
            "diagnostics": gaps,
            "faults": list(self.faults),
            "motion": "controlled_gripper_motion" if self.phase == self.DRAG else "idle",
            "outputs_enabled": False,
            "teleop_enabled": False,
            "readback": {
                "source_kind": "servo_position_readback",
                "paused_for_drag": self.readback_paused,
                "rounds": self.sampler.rounds,
                "last_reason": self.sampler.last_reason,
            },
            "worker": {"ticks": self.worker_ticks, "last_error": self.last_tick_error},
            "manual_arm_ranges": {name: dict(value)
                                  for name, value in self.manual_arm_ranges.items()},
            "manual_arm_ranges_at_utc": self.manual_arm_ranges_at_utc,
            "manual_gripper_range": (dict(self.manual_gripper_range)
                                     if self.manual_gripper_range else None),
        }

    # ------------------------------------------------------------------ evidence
    def evidence(self) -> dict[str, Any]:
        with self.lock:
            now = self.monotonic()
            now_wall = self.clock()
            sample_log = self.sampler.sample_log()
            blob = json.dumps(sample_log, sort_keys=True, separators=(",", ":")).encode()
            joints: dict[str, Any] = {}
            for name in JOINTS:
                snap = self.sampler.ranges[name].snapshot(now)
                assess = self.sampler.assess(name)
                manual = self.manual_arm_ranges.get(name)
                low = manual["min_raw"] if manual else snap["observed_min_raw"]
                high = manual["max_raw"] if manual else snap["observed_max_raw"]
                methods = "operator_manual_entry" if manual else "manual_range_capture"
                joints[name] = {
                    "method": methods,
                    "servo_id": SERVO_IDS[name],
                    "reference_raw": snap["reference_raw"],
                    "observed_min_raw": low,
                    "observed_max_raw": high,
                    "span_raw": (high - low) if manual else snap["span_raw"],
                    "samples": snap["samples"] if not manual else 0,
                    "noise_raw": snap["noise_raw"] if not manual else None,
                    "anomalies": snap["anomalies"],
                    "duplicates": snap["duplicates"],
                    "stale": snap["stale"],
                    "missing": snap["missing"],
                    "sample_log_entries": len(self.sampler.ranges[name].log),
                    "sample_log_truncated": self.sampler.ranges[name].log_truncated,
                    "last_seq": snap["last_seq"],
                    "first_sample_utc": _utc(self.sampler.ranges[name].first_wall),
                    "last_sample_utc": _utc(self.sampler.ranges[name].last_wall),
                    "valid": True if manual else assess["valid"],
                    "reasons": [] if manual else assess["reasons"],
                    "min_span_raw": assess["min_span_raw"],
                    "min_samples": assess["min_samples"],
                }
            gripper = (self._manual_gripper_summary()
                       or (self.calibrator.summary(now) if self.calibrator is not None
                           else {"method": "operator_drag_safe_window", "valid": False,
                                 "reasons": ["drag_not_started"], "marks": []}))
            arm_ok, arm_missing = self._arm_validation()
            arm_method = ("operator_manual_entry"
                          if set(self.manual_arm_ranges) == set(ARM_JOINTS)
                          else "manual_range_capture")
            gripper_manual = self.manual_gripper_range is not None
            evidence_method = (
                "operator_manual_full_entry"
                if arm_method == "operator_manual_entry" and gripper_manual
                else ("operator_entered_ranges_plus_gripper_drag"
                      if arm_method == "operator_manual_entry"
                      else "operator_manual_plus_gripper_drag")
            )
            ages = {}
            for name in JOINTS:
                last = self.sampler.ranges[name].last_mono
                ages[name] = None if last is None else round(max(0.0, now - last), 3)
            feedback_fresh = all(
                age is not None and age <= self.stale_after_s for age in ages.values())
            reasons: list[str] = []
            if not arm_ok:
                reasons.append("arm_ranges_incomplete:" + ",".join(arm_missing))
            if not gripper.get("valid"):
                reasons.extend(f"gripper:{reason}" for reason in gripper.get("reasons", []))
            if self.phase == self.MANUAL:
                reasons.append("job_not_finished")
            if not feedback_fresh:
                reasons.append("feedback_stale")
            return {
                "schema_version": 2,
                "kind": "follower_calibration_evidence",
                "job_id": self.job_id,
                "generation": self.generation,
                "phase": self.phase,
                "agent": {"version": self.agent_version, "profile": self.profile},
                "device_identity": dict(self.device_identity()),
                "source_kind": "servo_position_readback",
                "method": evidence_method,
                "arm_ranges": {
                    "method": arm_method,
                    "operator_entered": arm_method == "operator_manual_entry",
                    "entered_at_utc": self.manual_arm_ranges_at_utc,
                },
                "gripper_ranges": {
                    "method": "operator_manual_entry" if gripper_manual
                              else "operator_drag_safe_window",
                    "operator_entered": gripper_manual,
                    "entered_at_utc": self.manual_arm_ranges_at_utc if gripper_manual else None,
                },
                "units": {"position": "vendor_raw_tick", "radians_per_tick": RADIANS_PER_TICK,
                          "centre_raw": RAW_CENTRE, "range_raw": [RAW_MIN, RAW_MAX],
                          "gripper_flip": "vendor_yaml_min_greater_than_max"},
                "started_at_utc": _utc(self.started_wall),
                "completed_at_utc": _utc(self.stopped_wall) if self.stopped_wall else _utc(now_wall),
                "operator": self.operator,
                "operator_confirmed_safe_window": self.safe_window_raw is not None,
                "support_confirmed": self.support_confirmed,
                "safe_window_raw": list(self.safe_window_raw) if self.safe_window_raw else None,
                "stop_reason": self.stop_reason,
                "faults": list(self.faults),
                "joints": joints,
                "gripper": gripper,
                "quality": {
                    "arm_joints_valid": sum(1 for name in ARM_JOINTS if joints[name]["valid"]),
                    "arm_joints_required": len(ARM_JOINTS),
                    "gripper_valid": bool(gripper.get("valid")),
                    "feedback_fresh": feedback_fresh,
                    "feedback_age_s": ages,
                    "readback_reason": self.sampler.last_reason,
                    "complete": not reasons,
                    "reasons": reasons,
                },
                "sample_log_entries": len(sample_log),
                "sample_log_sha256": hashlib.sha256(blob).hexdigest(),
                "sample_log": sample_log,
            }

    # ------------------------------------------------------------------ worker
    def tick(self) -> bool:
        """Advance the job once. Called from the agent worker thread only.

        Blocking bus I/O happens *outside* ``self.lock`` so ``stop`` and ``status``
        never queue behind a servo scan; the phase is re-checked after every wait.
        """
        with self.lock:
            self.worker_ticks += 1
            phase = self.phase
            if phase not in (self.MANUAL, self.DRAG, self.REVIEW, self.STOPPED):
                return False
            now = self.monotonic()
            if phase in (self.MANUAL, self.DRAG):
                if now > self.absolute_deadline:
                    self.stop_reason = "absolute_timeout"
                    self._finish_locked(self.STOPPED)
                    return True
                if (self.last_touch_mono is not None
                        and (now - self.last_touch_mono) > self.idle_ttl_s):
                    # The browser went away.  Stop commanding, then put the arm back
                    # under torque instead of leaving it limp and unsupervised.
                    self.stop_reason = "client_timeout"
                    self._finish_locked(self.STOPPED)
                    return True

        if phase == self.DRAG:
            return self._tick_drag()

        try:
            snapshot = self.readback_snapshot()
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                self.last_tick_error = f"snapshot:{type(exc).__name__}"
            return False
        with self.lock:
            if self.phase not in (self.MANUAL, self.REVIEW, self.STOPPED):
                return False
            try:
                self.sampler.ingest(snapshot, now_mono=self.monotonic(), now_wall=self.clock())
            except CalibrationJobError as exc:
                # A regressed sequence after the capture ended only invalidates the
                # evidence; while capturing it is a fault.
                if self.phase == self.MANUAL:
                    self._apply_fault(exc.reason)
                    return True
                self.last_tick_error = exc.reason
        return False

    def _tick_drag(self) -> bool:
        with self.lock:
            calibrator = self.calibrator
            if calibrator is None or self.phase != self.DRAG:
                return False
            now = self.monotonic()
            target = (None if self.drag_scan_failures > 0
                      else calibrator.consume_pending(now))
        if target is not None:
            try:
                self.sink.publish_target(SERVO_IDS[GRIPPER_JOINT], target, 0.12)
            except Exception as exc:  # noqa: BLE001
                with self.lock:
                    self.last_tick_error = f"publish:{type(exc).__name__}"
                    self._apply_fault("gripper_command_failed")
                return True
            with self.lock:
                if self.calibrator is not None and self.phase == self.DRAG:
                    self.calibrator.note_published(target, self.monotonic())
        try:
            scan = self.read_scan()
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                self.last_tick_error = f"scan:{type(exc).__name__}:{exc}"
                self._note_drag_scan_failure(self.monotonic())
                return self.phase == self.FAULT
        raw = _int_or_none(scan.get(GRIPPER_JOINT))
        with self.lock:
            if self.phase != self.DRAG or self.calibrator is None:
                return False
            if raw is None:
                self.last_tick_error = "scan:gripper_feedback_missing"
                self._note_drag_scan_failure(self.monotonic())
                return self.phase == self.FAULT
            self.drag_scan_failures = 0
            self.drag_scan_failure_started_mono = None
            self.last_tick_error = None
            now = self.monotonic()
            self.calibrator.ingest_feedback(raw, now)
            for name in ARM_JOINTS:
                reference = self.non_target_reference.get(name)
                actual = _int_or_none(scan.get(name))
                if reference is not None and actual is not None and abs(actual - reference) > 8:
                    # The operator physically took over an arm joint. Stop gripper
                    # commands, resume six-joint sampling, and continue the manual
                    # range phase instead of treating intentional movement as a fault.
                    if self.readback_paused:
                        self.resume_readback()
                        self.readback_paused = False
                    self.phase = self.MANUAL
                    self.last_tick_error = (
                        f"manual_takeover:{name} {reference}->{actual}")
                    self.non_target_reference = {}
                    return False
            fault = self.calibrator.check(now)
            if fault:
                self._apply_fault(fault)
            return True

    def _note_drag_scan_failure(self, now: float) -> None:
        """Stop issuing targets immediately; fault only after bounded retries."""
        self.drag_scan_failures += 1
        if self.drag_scan_failure_started_mono is None:
            self.drag_scan_failure_started_mono = now
        if (self.drag_scan_failures >= 3
                or now - self.drag_scan_failure_started_mono >= 1.5):
            self._apply_fault("gripper_feedback_lost")

    def shutdown(self) -> None:
        with self.lock:
            if self.phase in (self.MANUAL, self.DRAG):
                self._stop_locked("agent_shutdown")
