"""Upper-computer DDS feedback client and conservative clock mapping.

The bridge in the Humble container exposes a local Unix socket with the latest
typed snapshot.  This module never speaks HTTP for feedback, never treats the
DDS receipt time as a new measurement time, and never compares two hosts'
monotonic values without an explicit, bounded offset estimate.

Age is an upper bound, not a point measurement:

* the car stamps ``sample_end_mono_ns`` and ``published_mono_ns`` on its clock;
* the upper observes ``upper_receive_ns`` when it reads the local snapshot;
* ``offset = upper_receive - car_publish`` is only a lower bound on the true
  offset because the minimum observed trip is the least delayed one;
* therefore ``now - (sample_end + min_offset)`` is an upper bound on the true
  sample age, and an additional configured error floor covers scheduling and
  drift.  If the bound cannot be kept under the configured maximum, the provider
  reports unavailable instead of enabling following.
"""
from __future__ import annotations

import copy
import json
import socket
import time
from collections import deque
from typing import Any

JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint")
SERVO_IDS = {"joint1": 1, "joint2": 2, "joint3": 3,
             "joint4": 4, "joint5": 5, "r_joint": 10}
BRIDGE_SCHEMA = "lelab.upper_dds_bridge.v1"
MAX_SNAPSHOT_BYTES = 1_048_576


class ClockMapping:
    """Conservative car-monotonic -> upper-monotonic offset with an error bound."""

    def __init__(self, *, max_error_ns: int = 250_000_000,
                 error_floor_ns: int = 50_000_000, drift_ppm: float = 200.0,
                 history: int = 256, clock_ns=time.monotonic_ns) -> None:
        if max_error_ns <= 0 or error_floor_ns < 0 or not 0 <= drift_ppm <= 5000:
            raise ValueError("invalid clock mapping configuration")
        self.max_error_ns = int(max_error_ns)
        self.error_floor_ns = int(error_floor_ns)
        self.drift_ppm = float(drift_ppm)
        self.clock_ns = clock_ns
        self.history = max(8, int(history))
        self._observations: deque[tuple[int, int]] = deque(maxlen=self.history)
        self.epoch: str | None = None
        self.offset_min_ns: int | None = None
        self.offset_max_ns: int | None = None
        self.first_observation_ns: int | None = None
        self.last_observation_ns: int | None = None

    def reset(self, reason: str = "") -> None:
        self._observations.clear()
        self.epoch = None
        self.offset_min_ns = self.offset_max_ns = None
        self.first_observation_ns = self.last_observation_ns = None
        self.reset_reason = reason

    def observe(self, *, car_epoch: str, car_publish_ns: int,
                upper_receive_ns: int) -> None:
        if car_epoch != self.epoch:
            self.reset(f"clock_epoch_changed:{self.epoch}->{car_epoch}")
            self.epoch = car_epoch
        offset = int(upper_receive_ns) - int(car_publish_ns)
        self._observations.append((int(car_publish_ns), int(upper_receive_ns)))
        self.offset_min_ns = offset if self.offset_min_ns is None \
            else min(self.offset_min_ns, offset)
        self.offset_max_ns = offset if self.offset_max_ns is None \
            else max(self.offset_max_ns, offset)
        if self.first_observation_ns is None:
            self.first_observation_ns = int(upper_receive_ns)
        self.last_observation_ns = int(upper_receive_ns)

    def error_bound_ns(self, now_ns: int | None = None) -> int | None:
        if self.offset_min_ns is None:
            return None
        now = self.clock_ns() if now_ns is None else int(now_ns)
        offsets = sorted(upper - car for car, upper in self._observations)
        # A delayed DDS delivery increases upper_receive - car_publish, but it does
        # not move the lower bound. Bound the estimate with the lower decile so one
        # scheduler pause or stale queued sample cannot poison every later frame.
        lower_decile = offsets[min(len(offsets) - 1, len(offsets) // 10)]
        jitter = max(0, lower_decile - self.offset_min_ns)
        elapsed = max(0, now - (self.first_observation_ns or now))
        drift = int(elapsed * self.drift_ppm / 1_000_000.0)
        return max(self.error_floor_ns, jitter, drift)

    def ready(self, now_ns: int | None = None) -> bool:
        bound = self.error_bound_ns(now_ns)
        return bound is not None and bound <= self.max_error_ns

    def age_upper_bound_ns(self, *, car_sample_ns: int,
                           now_ns: int | None = None) -> int | None:
        bound = self.error_bound_ns(now_ns)
        if bound is None or self.offset_min_ns is None:
            return None
        now = self.clock_ns() if now_ns is None else int(now_ns)
        return max(0, now - (int(car_sample_ns) + self.offset_min_ns) + bound)

    def state(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "observations": len(self._observations),
            "offset_min_ns": self.offset_min_ns,
            "offset_max_ns": self.offset_max_ns,
            "offset_lower_decile_ns": (
                sorted(upper - car for car, upper in self._observations)[
                    min(len(self._observations) - 1, len(self._observations) // 10)]
                if self._observations else None),
            "error_bound_ns": self.error_bound_ns(),
            "max_error_ns": self.max_error_ns,
            "error_floor_ns": self.error_floor_ns,
            "drift_ppm": self.drift_ppm,
            "ready": self.ready(),
            "reset_reason": getattr(self, "reset_reason", None),
        }


class DdsFeedbackClient:
    """Bounded local-IPC client for the upper Humble bridge snapshot."""

    def __init__(self, socket_path: str, *, timeout_s: float = 0.15,
                 clock_ns=time.monotonic_ns) -> None:
        self.socket_path = str(socket_path)
        self.timeout_s = max(0.01, float(timeout_s))
        self.clock_ns = clock_ns
        self._socket: socket.socket | None = None
        self._buffer = b""
        self.counts = {"reads": 0, "ok": 0, "errors": 0, "reconnects": 0}
        self.last_error: str | None = None
        self.last_snapshot_mono_ns: int | None = None

    def close(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout_s)
        sock.connect(self.socket_path)
        self._socket = sock
        self._buffer = b""
        self.counts["reconnects"] += 1
        return sock

    def read(self) -> dict[str, Any] | None:
        self.counts["reads"] += 1
        try:
            sock = self._socket or self._connect()
        except OSError as error:
            self.counts["errors"] += 1
            self.last_error = f"connect:{type(error).__name__}:{error}"
            self._socket = None
            return None
        try:
            sock.sendall(b'{"op":"snapshot"}\n')
            deadline = time.monotonic() + self.timeout_s
            while b"\n" not in self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("snapshot_timeout")
                sock.settimeout(remaining)
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("bridge_closed")
                self._buffer += chunk
                if len(self._buffer) > MAX_SNAPSHOT_BYTES:
                    raise ValueError("snapshot_too_large")
            line, self._buffer = self._buffer.split(b"\n", 1)
            payload = json.loads(line)
            if not isinstance(payload, dict) or payload.get("schema") != BRIDGE_SCHEMA \
                    or payload.get("ok") is not True:
                raise ValueError("snapshot_schema")
        except (OSError, ValueError, TimeoutError, ConnectionError) as error:
            self.counts["errors"] += 1
            self.last_error = f"{type(error).__name__}:{error}"
            self.close()
            return None
        self.counts["ok"] += 1
        self.last_error = None
        self.last_snapshot_mono_ns = self.clock_ns()
        return payload

    def state(self) -> dict[str, Any]:
        return {"socket": self.socket_path, "counts": dict(self.counts),
                "last_error": self.last_error,
                "connected": self._socket is not None,
                "last_snapshot_age_s": (
                    None if self.last_snapshot_mono_ns is None
                    else round((self.clock_ns() - self.last_snapshot_mono_ns) / 1e9, 3))}


def _sample_map(frame: dict[str, Any], *, age_s: float, scan_span_s: float,
                calibration_hash: str) -> dict[str, Any]:
    names = list(frame.get("joint_names") or [])
    raw = list(frame.get("raw") or [])
    positions = list(frame.get("position_rad") or [])
    valid = list(frame.get("valid") or [])
    errors = list(frame.get("errors") or [])
    samples: dict[str, Any] = {}
    for index, expected in enumerate(JOINTS):
        if index >= len(names) or names[index] != expected:
            samples[expected] = {
                "name": expected, "servo_id": SERVO_IDS[expected],
                "raw_position": None, "position_rad": None,
                "source_kind": "servo_position_readback", "valid": False,
                "error": "joint_order_or_missing", "sample_seq": None,
                "age_s": age_s, "sample_time_ns": None,
                "clock_domain": frame.get("clock_domain"),
                "calibration_hash": calibration_hash,
            }
            continue
        sample_error = None if (index < len(valid) and valid[index] is True
                                and not (errors[index] if index < len(errors) else "")) \
            else (errors[index] if index < len(errors) and errors[index]
                  else "axis_invalid")
        samples[expected] = {
            "name": expected, "servo_id": SERVO_IDS[expected],
            "raw_position": (int(raw[index]) if index < len(raw) else None),
            "position_rad": (float(positions[index]) if index < len(positions) else None),
            "source_kind": "servo_position_readback",
            "valid": sample_error is None,
            "error": sample_error,
            "sample_seq": int(frame.get("scan_sequence") or 0),
            # Conservative: every axis is assumed to be as old as the end of the
            # scan plus the full scan span, not only the last axis.
            "age_s": age_s + scan_span_s,
            "sample_time_ns": None,
            "source_sample_mono_ns": int(frame.get("sample_end_mono_ns") or 0),
            "clock_domain": "upper_clock_mapping_upper_bound",
            "calibration_hash": calibration_hash,
        }
    return samples


def feedback_from_snapshot(snapshot: dict[str, Any], mapping: ClockMapping, *,
                           expected_calibration_hash: str,
                           stale_after_s: float = 1.0,
                           transport_bound_s: float = 0.15) -> dict[str, Any]:
    """Convert a bridge snapshot into the car-status ``follower_feedback`` shape.

    The age is a conservative upper bound built only from quantities that do not
    require comparing two hosts' monotonic clocks:

    * ``sample_age_ns`` is the car's own ``published - sample_end`` age at publish;
    * ``transport_bound_s`` is a configured upper bound on the one-way DDS trip;
    * ``now - receipt_mono_ns`` is the local residence on this (upper) host, where
      both timestamps share one monotonic clock.

    The min-offset ``ClockMapping`` is kept for diagnostics only; it is not used to
    gate freshness, because a minimum observed offset is not a proven lower bound
    on the true clock offset and could understate age.
    """
    now = mapping.clock_ns()
    frame = snapshot.get("feedback") if isinstance(snapshot.get("feedback"), dict) else None
    bridge = snapshot.get("bridge") if isinstance(snapshot.get("bridge"), dict) else {}
    base = {
        "source": "dds_bridge",
        "source_kind": "servo_position_readback",
        "stale_after_s": float(stale_after_s),
        "transport_bound_s": float(transport_bound_s),
        "bridge": {"clock_epoch": bridge.get("clock_epoch"),
                   "pid": bridge.get("pid"),
                   "data_identity": bridge.get("data_identity")},
    }
    if frame is None:
        base.update({"available": False, "reason": "dds_feedback_missing",
                     "joint_samples": {}, "positions_rad": {}, "servo_pulses": {},
                     "scan_span_s": None, "oldest_age_s": None})
        return base
    if frame.get("clock_epoch") != mapping.epoch and mapping.epoch is None:
        mapping.epoch = frame.get("clock_epoch")
    mapping.observe(car_epoch=str(frame.get("clock_epoch")),
                    car_publish_ns=int(frame.get("published_mono_ns") or 0),
                    upper_receive_ns=now)
    sample_age_ns = frame.get("sample_age_ns")
    receipt_mono_ns = frame.get("receipt_mono_ns")
    age_ready = (isinstance(sample_age_ns, int) and not isinstance(sample_age_ns, bool)
                 and sample_age_ns >= 0
                 and isinstance(receipt_mono_ns, int) and not isinstance(receipt_mono_ns, bool)
                 and transport_bound_s > 0)
    if age_ready:
        local_elapsed_ns = max(0, now - receipt_mono_ns)
        age_ns = sample_age_ns + int(transport_bound_s * 1e9) + local_elapsed_ns
    else:
        age_ns = None
    scan_span_s = max(0.0, float(frame.get("scan_span_us") or 0) / 1_000_000.0)
    calibration_hash = str(frame.get("calibration_hash") or "")
    calibration_ok = bool(expected_calibration_hash) \
        and calibration_hash == expected_calibration_hash
    samples = _sample_map(frame, age_s=(age_ns or 0) / 1e9,
                          scan_span_s=scan_span_s,
                          calibration_hash=calibration_hash)
    reasons = []
    if not age_ready:
        reasons.append("dds_age_unbounded")
    if not calibration_ok:
        reasons.append("calibration_mismatch")
    if age_ns is not None and age_ns / 1e9 > stale_after_s:
        reasons.append("feedback_stale")
    if any(not sample["valid"] for sample in samples.values()):
        reasons.append("axis_invalid")
    available = not reasons
    base.update({
        "available": available,
        "reason": "ok" if available else ",".join(dict.fromkeys(reasons)),
        "joint_samples": samples,
        "positions_rad": {name: sample["position_rad"] for name, sample in samples.items()},
        "servo_pulses": {name: sample["raw_position"] for name, sample in samples.items()},
        "scan_span_s": scan_span_s,
        "oldest_age_s": (None if age_ns is None else age_ns / 1e9 + scan_span_s),
        "scan_sequence": int(frame.get("scan_sequence") or 0),
        "publisher_instance": int(frame.get("owner_instance") or 0),
        # MeasuredServoFrame carries no generation field; owner_instance is a random id
        # per readback process and already distinguishes restarts. Homing refuses
        # feedback without an integer generation, so expose a stable 0 here.
        "publisher_generation": 0,
        "publisher_identity_kind": "software_owner_instance",
        "calibration_hash": calibration_hash,
        "age_source": "car_publish_age_plus_bounded_transport",
        "age_bound_s": None if age_ns is None else age_ns / 1e9,
        "clock_mapping": mapping.state(),
        "data_identity": frame.get("data_identity"),
        "bridge_receipt_age_s": (float(frame.get("receipt_age_ns") or 0) / 1e9),
    })
    return base


def compare_sources(http_feedback: dict[str, Any], dds_feedback: dict[str, Any],
                    snapshot: dict[str, Any]) -> dict[str, Any]:
    """Side-by-side comparison for the HTTP/DDS observation period."""
    http_samples = (http_feedback.get("joint_samples") or {})
    dds_samples = (dds_feedback.get("joint_samples") or {})
    diffs = {}
    for name in JOINTS:
        left = (http_samples.get(name) or {}).get("raw_position")
        right = (dds_samples.get(name) or {}).get("raw_position")
        diffs[name] = (None if not isinstance(left, (int, float))
                       or not isinstance(right, (int, float)) else int(right) - int(left))
    return {
        "http_available": http_feedback.get("available"),
        "dds_available": dds_feedback.get("available"),
        "raw_diff_dds_minus_http": diffs,
        "max_abs_raw_diff": max((abs(value) for value in diffs.values()
                                 if value is not None), default=None),
        "http_publisher_instance": http_feedback.get("publisher_instance"),
        "dds_publisher_instance": dds_feedback.get("publisher_instance"),
        "http_scan_sequence": http_feedback.get("scan_sequence"),
        "dds_scan_sequence": dds_feedback.get("scan_sequence"),
        "http_calibration_hash": http_feedback.get("calibration_hash"),
        "dds_calibration_hash": dds_feedback.get("calibration_hash"),
        "bridge_counts": (snapshot.get("counts") if isinstance(snapshot, dict) else None),
    }


def copy_feedback(document: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(document)
