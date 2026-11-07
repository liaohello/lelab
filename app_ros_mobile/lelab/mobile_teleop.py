"""SO-101 leader → mobile follower mapping, plus versioned three-layer calibration storage.

Units are explicit everywhere: leader joints arrive in degrees (Feetech SO-101 bus with
use_degrees=True) and the gripper leader arrives as a 0-100 opening percentage; the car
follower uses radians on /controller_manager/joint_states and 0-1000 pulses on the servo bus.
Percent is never treated as an angle.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
# Vendor geometry: 240 deg of servo sweep over 1000 pulses (joint_position_controller.py).
RADIANS_PER_PULSE = (240.0 / 360.0) * (2.0 * math.pi) / 1000.0
PULSES_PER_RADIAN = 1.0 / RADIANS_PER_PULSE
DEG_TO_RAD = math.pi / 180.0
FEETECH_ENCODER_RESOLUTION = 4096


def leader_travel_ranges_rad(calibration: dict[str, Any] | None,
                             mapping: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Convert LeRobot leader raw ranges to radian travel for each follower axis."""
    if not isinstance(calibration, dict):
        raise ValueError("leader calibration missing")
    max_res = FEETECH_ENCODER_RESOLUTION - 1
    ranges: dict[str, tuple[float, float]] = {}
    for joint in mapping.get("joints", []):
        follower = joint.get("follower")
        leader = joint.get("leader")
        item = calibration.get(leader)
        if not isinstance(item, dict):
            raise ValueError(f"leader calibration missing for {leader}")
        low_raw = item.get("range_min")
        high_raw = item.get("range_max")
        if (type(low_raw) is not int or type(high_raw) is not int
                or high_raw <= low_raw):
            raise ValueError(f"invalid leader range for {leader}")
        mid = (low_raw + high_raw) / 2.0
        ranges[follower] = (
            (low_raw - mid) * 2.0 * math.pi / max_res,
            (high_raw - mid) * 2.0 * math.pi / max_res,
        )
    expected = {joint.get("follower") for joint in mapping.get("joints", [])}
    if set(ranges) != expected:
        raise ValueError("leader travel ranges incomplete")
    return ranges


def canonical_sha256(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


@dataclass
class CalibrationRecord:
    kind: str
    identity: str
    version: str
    sha256: str
    path: Path
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "identity": self.identity,
            "version": self.version,
            "sha256": self.sha256,
            "path": str(self.path),
            "device_identity": self.data.get("device_identity", {}),
            "created_utc": self.data.get("created_utc"),
        }


class CalibrationStore:
    """Atomic, hashed, identity-bound storage for the three calibration layers."""

    KINDS = ("leader", "follower", "mapping")
    SUPPORTED_VERSIONS = (SCHEMA_VERSION,)

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, kind: str, identity: str) -> Path:
        if kind not in self.KINDS:
            raise ValueError(f"unknown calibration kind {kind!r}")
        safe = identity.replace("/", "_").replace("..", "_")
        return self.root / kind / f"{safe}.json"

    def save(self, kind: str, identity: str, payload: dict[str, Any],
             *, schema_version: int | None = None) -> CalibrationRecord:
        if not identity:
            raise ValueError("calibration identity is required")
        version = SCHEMA_VERSION if schema_version is None else int(schema_version)
        if version not in self.SUPPORTED_VERSIONS:
            raise ValueError(f"schema_version {version} is not supported by this store")
        # Derived fields are recomputed on every save, so a document that was loaded, edited and
        # saved again must not carry its previous hash, version or timestamp into the new digest.
        derived = {"content_sha256", "version", "kind", "identity", "created_utc", "schema_version"}
        body = {k: v for k, v in payload.items() if k not in derived}
        body["schema_version"] = version
        body["kind"] = kind
        body["identity"] = identity
        body["created_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        digest = canonical_sha256(body)
        body["content_sha256"] = digest
        body["version"] = digest[:12]
        path = self._path(kind, identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".cal-", suffix=".json",
                                             delete=False, encoding="utf-8")
        try:
            json.dump(body, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, path)
        return CalibrationRecord(kind, identity, body["version"], digest, path, body)

    def load(self, kind: str, identity: str) -> CalibrationRecord:
        path = self._path(kind, identity)
        if not path.is_file():
            raise FileNotFoundError(f"{kind} calibration missing for identity {identity!r}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") not in self.SUPPORTED_VERSIONS:
            raise ValueError(f"{kind} calibration schema_version unsupported: {data.get('schema_version')}")
        stored = data.get("content_sha256")
        unsigned = {k: v for k, v in data.items() if k not in {"content_sha256", "version"}}
        digest = canonical_sha256(unsigned)
        if stored != digest:
            raise ValueError(f"{kind} calibration hash mismatch (file tampered or truncated)")
        return CalibrationRecord(kind, identity, data.get("version", digest[:12]), digest, path, data)

    def readiness(self, leader_identity: str, follower_identity: str, mapping_identity: str) -> dict[str, Any]:
        """Report whether all three layers exist, hash-match and agree on joint order."""
        out: dict[str, Any] = {"ready": False, "reasons": [], "layers": {}}
        try:
            layers = {
                "leader": self.load("leader", leader_identity),
                "follower": self.load("follower", follower_identity),
                "mapping": self.load("mapping", mapping_identity),
            }
        except (FileNotFoundError, ValueError) as exc:
            return {"ready": False, "reasons": [str(exc)], "layers": {}}
        for name, record in layers.items():
            out["layers"][name] = record.as_dict()
        mapping = layers["mapping"].data
        for side in ("leader", "follower"):
            bound = mapping.get("bound_identities", {}).get(side)
            actual = layers[side].identity
            if bound and bound != actual:
                out["reasons"].append(f"mapping binds {side}={bound!r} but loaded {actual!r}")
        joint_order = mapping.get("joint_order", [])
        follower_order = layers["follower"].data.get("joint_order", [])
        if follower_order and joint_order != follower_order:
            out["reasons"].append("mapping joint_order differs from follower joint_order")
        leader_order = layers["leader"].data.get("joint_order", [])
        mapping_leader_order = [j.get("leader") for j in mapping.get("joints", [])]
        leader_arm_order = [name for name in leader_order if name != "gripper"]
        if leader_arm_order and mapping_leader_order and \
                mapping_leader_order != leader_arm_order:
            out["reasons"].append("mapping leader joints differ from leader calibration joint_order")
        for name in ("leader", "follower", "mapping"):
            paths = self.unmeasured_paths(layers[name].data)
            if paths:
                shown = ", ".join(paths[:4]) + (f" (+{len(paths) - 4} more)" if len(paths) > 4 else "")
                out["reasons"].append(f"{name} layer still holds shipped example values at {shown}")
        # A leader calibration that is absent or partial is enforced where the serial is actually
        # opened (build_bus refuses), because readiness() describes cross-layer agreement only.
        out["ready"] = not out["reasons"]
        out["calibration_version"] = "|".join(layers[k].version for k in ("leader", "follower", "mapping"))
        return out

    # The delivered templates are what the page's "载入模板" puts in the editor, so "saved" must not
    # be read as "measured": without this guard a layer loaded and saved unchanged would pass
    # readiness and following would engage on placeholder offsets, directions and soft limits.
    UNMEASURED_PREFIXES = ("example_not_measured", "derived_not_measured")
    UNMEASURED_FLAG = "_example_not_measured"

    @classmethod
    def unmeasured_paths(cls, payload: Any, prefix: str = "") -> list[str]:
        """JSON paths of this layer that still carry the shipped not-measured markers."""
        hits: list[str] = []
        if isinstance(payload, dict):
            if payload.get(cls.UNMEASURED_FLAG):
                hits.append(prefix or "<root>")
            for key, value in payload.items():
                hits += cls.unmeasured_paths(value, f"{prefix}.{key}" if prefix else str(key))
        elif isinstance(payload, list):
            for index, value in enumerate(payload):
                hits += cls.unmeasured_paths(value, f"{prefix}[{index}]")
        elif isinstance(payload, str) and payload.strip().startswith(cls.UNMEASURED_PREFIXES):
            hits.append(prefix or "<root>")
        return hits


class TeleopMapper:
    """Joint-space only mapping with relative (differential) follow law and rate limits."""

    def __init__(self, mapping: dict[str, Any], follower_limits_rad: dict[str, list[float]],
                 leader_travel_rad: dict[str, tuple[float, float]] | None = None,
                 gripper_mapping_mode: str = "relative"):
        self.joints = {j["follower"]: j for j in mapping.get("joints", [])}
        self.gripper = mapping.get("gripper", {})
        self.filter_alpha = float(mapping.get("filter_alpha", 0.4))
        self.deadband_rad = float(mapping.get("deadband_rad", 0.0))
        self.max_step_rad_s = float(mapping.get("max_step_rad_s", 1.0))
        self.follower_limits = follower_limits_rad
        self.leader_travel_rad = dict(leader_travel_rad or {})
        if gripper_mapping_mode not in ("relative", "absolute_normalized"):
            raise ValueError("invalid gripper mapping mode")
        self.gripper_mapping_mode = gripper_mapping_mode
        self._filtered: dict[str, float] = {}
        self._last_target: dict[str, float] = {}
        self._last_wall: float | None = None
        self._last_gripper_pulse: float | None = None

    def reset(self, reason: str = "") -> dict[str, Any]:
        self._filtered.clear()
        self._last_target.clear()
        self._last_wall = None
        self._last_gripper_pulse: float | None = None
        return {"reset": True, "reason": reason}

    def align(self, follower_rad: dict[str, float], *, now: float | None = None,
              gripper_pulse: float | None = None) -> dict[str, Any]:
        """Seed filtering/rate state from the measured follower without commanding a jump."""
        self._filtered.clear()
        self._last_target.clear()
        for name, value in follower_rad.items():
            if name in self.joints and isinstance(value, (int, float)) and math.isfinite(value):
                self._filtered[name] = float(value)
                self._last_target[name] = float(value)
        self._last_wall = time.monotonic() if now is None else float(now)
        self._last_gripper_pulse = (
            float(gripper_pulse)
            if isinstance(gripper_pulse, (int, float)) and math.isfinite(gripper_pulse)
            else None
        )
        return {"aligned": True, "joints": sorted(self._last_target),
                "gripper_pulse": self._last_gripper_pulse}

    @staticmethod
    def _wrap(delta: float) -> float:
        """Shortest-path angle difference for continuous joints."""
        return (delta + math.pi) % (2.0 * math.pi) - math.pi

    def joint_target(self, follower: str, leader_rad: float, leader_ref_rad: float,
                     follower_ref_rad: float, now: float) -> dict[str, Any]:
        now = float(now)
        dt = 0.0 if self._last_wall is None else max(0.0, now - self._last_wall)
        self._last_wall = now
        return self._joint_target(follower, leader_rad, leader_ref_rad,
                                  follower_ref_rad, dt)

    def frame(self, requests: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
        """Compute one complete arm frame with one shared elapsed time."""
        now = float(now)
        dt = 0.0 if self._last_wall is None else max(0.0, now - self._last_wall)
        self._last_wall = now
        return [
            self._joint_target(
                str(request["follower"]),
                float(request["leader_rad"]),
                float(request["leader_ref_rad"]),
                float(request["follower_ref_rad"]),
                dt,
            )
            for request in requests
        ]

    def _joint_target(self, follower: str, leader_rad: float, leader_ref_rad: float,
                      follower_ref_rad: float, dt: float) -> dict[str, Any]:
        cfg = self.joints.get(follower)
        if cfg is None:
            raise KeyError(f"joint {follower!r} is not in the mapping")
        direction = 1.0 if cfg.get("direction", 1) >= 0 else -1.0
        scale = float(cfg.get("scale", 1.0))
        wrap = bool(cfg.get("wrap", False))
        delta = leader_rad - leader_ref_rad
        if wrap:
            delta = self._wrap(delta)
        low, high = self.follower_limits[follower]
        soft_low = max(low, float(cfg.get("soft_min_rad", low)))
        soft_high = min(high, float(cfg.get("soft_max_rad", high)))
        normalization = None
        leader_range = self.leader_travel_rad.get(follower)
        if leader_range is not None:
            leader_span = leader_range[1] - leader_range[0]
            follower_span = soft_high - soft_low
            if leader_span <= 0 or follower_span <= 0:
                raise ValueError(f"invalid calibration travel for {follower}")
            normalization = delta / leader_span
            raw = follower_ref_rad + direction * scale * normalization * follower_span
        else:
            raw = follower_ref_rad + direction * scale * delta
        clipped = min(soft_high, max(soft_low, raw))
        alpha = self.filter_alpha
        previous = self._filtered.get(follower)
        filtered = clipped if previous is None else alpha * clipped + (1 - alpha) * previous
        if (previous is not None and self.deadband_rad > 0
                and abs(filtered - previous) <= self.deadband_rad):
            filtered = previous
        limited = filtered
        if self._last_target:
            step_limit = self.max_step_rad_s * dt if dt > 0 else 0.0
            last = self._last_target.get(follower, filtered)
            limited = min(filtered, last + step_limit) if filtered > last else max(filtered, last - step_limit)
        self._filtered[follower] = limited
        self._last_target[follower] = limited
        return {
            "joint": follower,
            "pre_clip_rad": round(raw, 6),
            "target_rad": round(limited, 6),
            "clipped": abs(raw - clipped) > 1e-9,
            "soft_limits_rad": [soft_low, soft_high],
            "hardware_limits_rad": [low, high],
            "direction": direction,
            "scale": scale,
            "wrapped": wrap,
            "leader_delta_rad": round(delta, 6),
            "normalization": ("leader_range_to_follower_range"
                              if normalization is not None else "none"),
            "leader_normalized_delta": (round(normalization, 6)
                                        if normalization is not None else None),
            "leader_travel_rad": (list(leader_range)
                                  if leader_range is not None else None),
            "follower_travel_rad": [soft_low, soft_high],
        }

    def gripper_target(self, percent: float, percent_ref: float, pulse_ref: float,
                       now: float | None = None) -> dict[str, Any]:
        """Map the leader 0-100 opening to the calibrated follower endpoints.

        A gripper is an endpoint device, unlike the arm's relative incremental axes:
        0% must converge to the calibrated closed endpoint even when the leader was
        already near closed when the session was aligned. The returned target still
        passes through the car-side step limiter, so the physical transition is ramped.
        """
        cfg = self.gripper
        if not cfg:
            raise KeyError("mapping has no gripper section")
        # The calibrated field is ticks per percentage point (2.96 ticks/% in the
        # current rig); multiplying by 100 would make 57.4% move only 1.7 ticks.
        span = float(cfg.get("pulse_span_per_hundred_pct", 6.0))
        percent = max(0.0, min(100.0, float(percent)))
        percent_ref = max(0.0, min(100.0, float(percent_ref)))
        direction = 1.0 if cfg.get("direction", 1) >= 0 else -1.0
        pulse_min = float(cfg.get("pulse_min", 0.0))
        pulse_max = float(cfg.get("pulse_max", 999.0))
        absolute = self.gripper_mapping_mode == "absolute_normalized"
        if absolute:
            pulse_at_zero = pulse_max if direction < 0 else pulse_min
            pre_clip = pulse_at_zero + direction * percent * span
        else:
            pre_clip = pulse_ref + direction * (percent - percent_ref) * span
        pulse = min(pulse_max, max(pulse_min, pre_clip))
        previous_pulse = self._last_gripper_pulse
        # Absolute gripper mapping must expose the true endpoint target in the UI.
        # The car-side step limiter is the single smoothing boundary, so do not
        # stack a second EMA here (which made 57% look like a closed target).
        filtered_pulse = (pulse if absolute or previous_pulse is None else
                          self.filter_alpha * pulse +
                          (1 - self.filter_alpha) * previous_pulse)
        filtered_pulse = min(pulse_max, max(pulse_min, filtered_pulse))
        flipped = bool(cfg.get("flipped", True))
        initial = float(cfg.get("initial_position_raw", 500))
        rad = ((initial - filtered_pulse) if flipped
               else (filtered_pulse - initial)) * RADIANS_PER_PULSE
        soft = cfg.get("soft_max_rad")
        clipped_rad = rad if soft is None else min(float(soft), rad)
        self._last_gripper_pulse = filtered_pulse
        width_mm = None
        if cfg.get("report_width_mm", True):
            width_mm = round(2000.0 * _jaw_width_m(pulse, float(cfg.get("angle_zero", 200.0)),
                                                   float(cfg.get("link_bc", 0.024)),
                                                   float(cfg.get("link_hb", 0.030)),
                                                   float(cfg.get("link_ec", 0.020)),
                                                   float(cfg.get("lcd_plus_ecd", 0.0))), 3)
        return {
            "joint": cfg.get("follower_joint", "r_joint"),
            "leader_percent": round(float(percent), 3),
            "leader_percent_ref": round(float(percent_ref), 3),
            "mapping_mode": "absolute_normalized" if absolute else "relative",
            "pre_clip_pulse": round(pre_clip, 3),
            "target_pulse": round(filtered_pulse, 3),
            "clipped": abs(pre_clip - pulse) > 1e-9,
            "target_rad": round(clipped_rad, 6),
            "pulse_unit": "pulse",
            "radians_per_pulse": RADIANS_PER_PULSE,
            "flipped": flipped,
            "jaw_width_mm_estimate": width_mm,
            "note": "0-100 percent is converted through pulses and the linkage geometry, never reinterpreted as radians",
        }


def _jaw_width_m(pulse: float, angle_zero: float, link_bc: float, link_hb: float,
                 link_ec: float, lcd_plus_ecd: float) -> float:
    theta = math.radians((pulse - angle_zero) / 1000.0 * 180.0)
    ke = link_hb + math.cos(theta) * link_bc - math.cos(lcd_plus_ecd) * link_ec
    return ke


def default_store_root() -> Path:
    return Path(os.environ.get(
        "LELAB_MOBILE_TELEOP_HOME",
        str(Path.home() / ".cache" / "lelab" / "mobile_teleop"),
    ))
