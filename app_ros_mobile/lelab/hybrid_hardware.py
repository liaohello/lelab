"""Production hardware composition for the hybrid manual facade.

Construction is side-effect free.  The upper serial port opens only in
``UpperLeaderLease.acquire()``; the car HTTP lease starts only when the facade
acquires a session.  Neither path can publish an actuator command in this phase.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .hardware_lease import HardwareLeaseError, get_hardware_lease_manager
from .hybrid_calibration import HybridCalibrationStore
from .hybrid_channel import HybridSessionError, TrustedFeedbackGate
from .hybrid_facade import DEFAULT_TTL_MS, HybridPublicFacade
from .mobile_leader_reader import (LeaderConfig, SO101LeaderReader, build_bus,
                                   calibration_id_mismatches)
from .mobile_teleop import (RADIANS_PER_PULSE, TeleopMapper, canonical_sha256,
                            leader_travel_ranges_rad)

JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint")
ARM_JOINTS = JOINTS[:5]
LEADER_JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex",
                 "wrist_roll", "gripper")
UPPER_LEASE_TASK = "hybrid_teleoperation"


class HardwareCompositionError(RuntimeError):
    pass


def _http_json(url: str, path: str, *, method: str = "GET", body: dict | None = None,
               token: str | None = None, timeout: float = 4.0) -> dict:
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url.rstrip("/") + path, data=payload, method=method,
        headers={"Content-Type": "application/json"},
    )
    if token:
        request.add_header("X-LeLab-Control-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(detail)
            detail = f"{body.get('error')}: {body.get('detail')}"
        except json.JSONDecodeError:
            pass
        raise HardwareCompositionError(f"car_http_{exc.code}:{detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HardwareCompositionError(f"car_unreachable:{exc}") from exc
    if not isinstance(parsed, dict):
        raise HardwareCompositionError("car_response_not_object")
    return parsed


class UpperLeaderLease:
    """Exclusive upper-side leader serial lease."""

    def __init__(self, *, agent_url: str, store: HybridCalibrationStore,
                 leader_identity: str, ttl_s: float = 60.0, read_hz: float = 30.0,
                 request_timeout_s: float = 4.0) -> None:
        self.agent_url = agent_url
        self.store = store
        self.leader_identity = leader_identity
        self.ttl_s = ttl_s
        self.read_hz = read_hz
        self.request_timeout_s = request_timeout_s
        self.leases = get_hardware_lease_manager()
        self.token: str | None = None
        self.reader: SO101LeaderReader | None = None
        self.lock = threading.RLock()
        self.last_reason = "unobserved"
        self.last_valid_at_mono: float | None = None

    def _open_reader(self) -> SO101LeaderReader:
        record = self.store.load("leader", self.leader_identity)
        asset = record.data
        device = asset.get("device_identity") or {}
        port = device.get("usb_by_id")
        if not isinstance(port, str) or not port.startswith("/dev/serial/by-id/"):
            raise HardwareCompositionError("leader_by_id_missing")
        if not Path(port).exists():
            raise HardwareCompositionError(f"leader_serial_missing:{port}")
        calibration = device.get("lerobot_calibration")
        if not isinstance(calibration, dict):
            raise HardwareCompositionError("leader_lerobot_calibration_missing")
        ids = {}
        for name in LEADER_JOINTS:
            entry = calibration.get(name)
            if not isinstance(entry, dict) or type(entry.get("id")) is not int:
                raise HardwareCompositionError(f"leader_calibration_id_missing:{name}")
            ids[name] = entry["id"]
        mismatches = calibration_id_mismatches(calibration, ids)
        if mismatches:
            raise HardwareCompositionError("leader_calibration_id_mismatch:" + ";".join(mismatches))
        config = LeaderConfig(port=port, ids=ids, read_hz=self.read_hz, stale_after_s=0.5)
        reader = SO101LeaderReader(config, bus_factory=lambda cfg: build_bus(cfg, calibration))
        reader.open()
        reader.start()
        state = reader.snapshot()
        if not state.get("connected"):
            reader.close()
            raise HardwareCompositionError(str(state.get("reason") or "leader_connect_failed"))
        return reader

    def acquire(self, request: dict) -> str:
        with self.lock:
            if self.token is not None:
                raise HardwareCompositionError("upper_lease_already_held")
            owner = str(request.get("owner") or "operator")
            lease = self.leases.acquire(
                UPPER_LEASE_TASK, owner=owner, role="operator", ttl_seconds=self.ttl_s)
            try:
                reader = self.reader or self._open_reader()
            except BaseException:
                self.leases.release(token=lease.token, reason="leader_open_failed")
                raise
            self.token = lease.token
            self.reader = reader
            return lease.token

    def probe(self) -> dict[str, Any]:
        """Open the configured leader read-only for device/calibration inspection."""
        with self.lock:
            if self.reader is None:
                self.reader = self._open_reader()
            time.sleep(0.15)
            state = self.reader.snapshot()
            if not state.get("healthy"):
                # A just-opened reader may not have completed its first poll yet.
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and not state.get("healthy"):
                    time.sleep(0.1)
                    state = self.reader.snapshot()
            return state

    def detected_port(self) -> str | None:
        try:
            record = self.store.load("leader", self.leader_identity)
        except (FileNotFoundError, ValueError):
            return None
        port = (record.data.get("device_identity") or {}).get("usb_by_id")
        return port if isinstance(port, str) else None

    def valid(self, token: str) -> bool:
        with self.lock:
            if token != self.token:
                self.last_reason = "upper_token_mismatch"
                return False
            state = self.leases.status()
            held = (state.get("state") == "held" and state.get("task") == UPPER_LEASE_TASK
                    and float(state.get("expires_in") or 0.0) > 0.0)
            self.last_reason = ("upper_lease_held" if held
                                else f"upper_lease_not_held:{state.get('state')}")
            if held:
                self.last_valid_at_mono = time.monotonic()
            return held

    def lease_state(self) -> dict[str, Any]:
        try:
            state = self.leases.status()
        except Exception:  # noqa: BLE001 - reporting must not break a status poll
            return {"held": self.token is not None, "reason": self.last_reason}
        return {"held": state.get("state") == "held",
                "expires_in_s": state.get("expires_in"),
                "reason": self.last_reason,
                "state": state.get("state"),
                "task": state.get("task")}

    def renew(self, token: str) -> None:
        if token != self.token or not self.leases.renew(token=token, ttl_seconds=self.ttl_s):
            raise HardwareCompositionError("upper_lease_renew_failed")

    def release(self, token: str, reason: str) -> None:
        with self.lock:
            if token != self.token:
                raise HardwareCompositionError("upper_lease_token_mismatch")
            reader, self.reader, self.token = self.reader, None, None
            if reader is not None:
                reader.close()
            self.leases.release(token=token, reason=reason)


class CarControlLease:
    """The existing HTTP control lease plus its T1 hybrid epoch."""

    def __init__(self, agent_url: str, *, ttl_s: float = 30.0,
                 timeout_s: float = 4.0,
                 expected_profile: str = "hardware_hybrid_manual_v1") -> None:
        self.agent_url = agent_url.rstrip("/")
        self.ttl_s = ttl_s
        self.timeout_s = timeout_s
        self.expected_profile = expected_profile
        self.token: str | None = None
        self.session: dict[str, Any] | None = None
        self.expires_mono = 0.0
        self.lock = threading.RLock()
        # Why the last validity read said no.  ``lease_lost`` alone cannot tell an
        # operator apart from a stalled status read, and the facade labels the two
        # the same way; see HybridSession._refresh.
        self.last_reason = "unobserved"
        self.last_valid_at_mono: float | None = None

    def _driver_available_before_epoch(self, status: dict[str, Any]) -> bool:
        hybrid = status.get("hybrid") if isinstance(status.get("hybrid"), dict) else {}
        return hybrid.get("driver_bound") is True

    def acquire(self, request: dict) -> str:
        with self.lock:
            if self.token is not None:
                raise HardwareCompositionError("car_lease_already_held")
            status = _http_json(self.agent_url, "/status", timeout=self.timeout_s)
            if status.get("profile") != self.expected_profile or status.get("read_only"):
                raise HardwareCompositionError("car_hardware_hybrid_profile_not_loaded")
            if not self._driver_available_before_epoch(status):
                raise HardwareCompositionError("car_trusted_driver_not_bound")
            acquired = _http_json(
                self.agent_url, "/control/acquire", method="POST",
                body={"client_id": str(request.get("owner") or "operator"),
                      "ttl_s": self.ttl_s}, timeout=self.timeout_s)
            # Remember who the lease was claimed as. Once it dies no token is usable,
            # and the agent only accepts a stop clearance from this same prior owner.
            self.acquired_client_id = str(request.get("owner") or "operator")
            token = acquired.get("token")
            if not isinstance(token, str) or not token:
                raise HardwareCompositionError("car_control_token_missing")
            begin = {
                "session_id": request.get("session_id"),
                "session_epoch": request.get("session_epoch"),
                "calibration_bundle_hash": request.get("calibration_bundle_hash"),
                "channels": ["arm", "gripper"],
            }
            try:
                epoch = _http_json(
                    self.agent_url, "/hybrid/begin_epoch", method="POST",
                    body=begin, token=token, timeout=self.timeout_s)
            except BaseException:
                try:
                    _http_json(self.agent_url, "/control/release", method="POST",
                               body={}, token=token, timeout=self.timeout_s)
                finally:
                    raise
            if epoch.get("driver_bound") is not True or epoch.get("outputs_enabled") is not False:
                self._release_token(token, "epoch_validation_failed")
                raise HardwareCompositionError("car_epoch_driver_or_output_state_invalid")
            self.token = token
            self.session = begin
            self.expires_mono = time.monotonic() + self.ttl_s
            return token

    def _local_state(self, token: str) -> tuple[bool, dict[str, Any], float]:
        """Check the held token and its local expiry: (ok, bound session, remaining_s)."""
        with self.lock:
            if token != self.token:
                self.last_reason = "car_token_mismatch"
                return False, {}, 0.0
            remaining = self.expires_mono - time.monotonic()
            if remaining <= 0.0:
                self.last_reason = "car_local_lease_expired"
                return False, {}, 0.0
            return True, dict(self.session or {}), remaining

    def lease_state(self) -> dict[str, Any]:
        """What this adapter itself knows: remaining local TTL and the last verdict."""
        with self.lock:
            remaining = (round(self.expires_mono - time.monotonic(), 3)
                         if self.token else None)
            return {"held": bool(self.token) and (remaining or 0.0) > 0.0,
                    "expires_in_s": remaining,
                    "reason": self.last_reason,
                    "last_confirmed_age_s": (round(time.monotonic() - self.last_valid_at_mono, 3)
                                             if self.last_valid_at_mono else None)}

    def _live_status(self, timeout_s: float | None = None) -> dict[str, Any] | None:
        try:
            return _http_json(self.agent_url, "/status",
                              timeout=self.timeout_s if timeout_s is None else timeout_s)
        except HardwareCompositionError as exc:
            self.last_reason = f"car_status_read_failed:{exc}"
            return None

    def valid(self, token: str) -> bool:
        ok, _session, _remaining = self._local_state(token)
        if not ok:
            return False
        status = self._live_status()
        if status is None:
            return False
        if (status.get("control") or {}).get("active") is not True:
            self.last_reason = "car_control_inactive"
            return False
        self.last_reason = "car_lease_held"
        self.last_valid_at_mono = time.monotonic()
        return True

    def renew(self, token: str) -> None:
        with self.lock:
            if token != self.token:
                raise HardwareCompositionError("car_lease_token_mismatch")
            _http_json(self.agent_url, "/control/renew", method="POST", body={},
                       token=token, timeout=self.timeout_s)
            self.expires_mono = time.monotonic() + self.ttl_s

    def _release_token(self, token: str, reason: str) -> None:
        try:
            _http_json(self.agent_url, "/hybrid/stop", method="POST",
                       body={"reason": reason}, token=token, timeout=self.timeout_s)
        finally:
            _http_json(self.agent_url, "/control/release", method="POST",
                       body={}, token=token, timeout=self.timeout_s)

    def release(self, token: str, reason: str) -> None:
        with self.lock:
            if token != self.token:
                raise HardwareCompositionError("car_lease_token_mismatch")
            try:
                self._release_token(token, reason)
            finally:
                self.token = None
                self.session = None
                self.expires_mono = 0.0

    def stop(self, reason: str) -> None:
        with self.lock:
            token = self.token
        if token:
            _http_json(self.agent_url, "/hybrid/stop", method="POST",
                       body={"reason": str(reason)}, token=token, timeout=self.timeout_s)


class HardwareObserver:
    """Server-side facts for leader, follower, video, calibration and exclusivity."""

    def __init__(self, *, upper: UpperLeaderLease, agent_url: str,
                 store: HybridCalibrationStore, leader_identity: str,
                 follower_identity: str, mapping_identity: str,
                 timeout_s: float = 4.0) -> None:
        self.upper = upper
        self.agent_url = agent_url.rstrip("/")
        self.store = store
        self.identities = {
            "leader": leader_identity,
            "follower": follower_identity,
            "mapping": mapping_identity,
        }
        self.timeout_s = timeout_s
        self.lock = threading.RLock()
        self.reference: dict[str, Any] | None = None
        self.mapper: TeleopMapper | None = None

    def _load_layers(self) -> dict[str, dict[str, Any]]:
        return {
            kind: self.store.load(kind, identity).data
            for kind, identity in self.identities.items()
        }

    def calibration_readiness(self) -> dict[str, Any]:
        try:
            readiness = self.store.readiness(
                self.identities["leader"],
                self.identities["follower"],
                self.identities["mapping"],
            )
            layers = self._load_layers()
        except (FileNotFoundError, ValueError) as exc:
            return {"ready": False, "reasons": [str(exc)], "bundle_hash": None,
                    "simulated": False}
        reasons = list(readiness.get("reasons") or [])
        mapping = layers["mapping"]
        bound = mapping.get("bound_identities") if isinstance(
            mapping.get("bound_identities"), dict) else {}
        for side in ("leader", "follower"):
            if bound.get(side) != self.identities[side]:
                reasons.append(
                    f"mapping binds {side}={bound.get(side)!r} but loaded "
                    f"{self.identities[side]!r}")
        versions = mapping.get("model_versions") if isinstance(
            mapping.get("model_versions"), dict) else {}
        for side, key in (("leader", "leader_calibration"),
                          ("follower", "follower_calibration")):
            expected = self.store.load(side, self.identities[side]).version
            declared = versions.get(key)
            if declared is not None and declared != expected:
                reasons.append(
                    f"mapping model_versions.{key}={declared!r} but loaded "
                    f"{expected!r}")
        if not readiness.get("ready"):
            return {"ready": False, "bundle_hash": None, "reasons": reasons,
                    "layout": "so101_three_layer", "simulated": False}
        refs = {
            kind: {"identity": self.identities[kind],
                   "sha256": canonical_sha256(asset),
                   "version": self.store.load(kind, self.identities[kind]).version}
            for kind, asset in layers.items()
        }
        bundle = {"schema_version": 1, "kind": "rig_bundle", "assets": refs,
                  "required_layers": list(refs)}
        bundle_hash = hashlib.sha256(
            json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {"ready": not reasons, "bundle_hash": bundle_hash if not reasons else None,
                "reasons": reasons,
                "layout": "so101_three_layer", "layers": refs,
                "simulated": False, "camera_capability": "observed_real",
                "firmware_capability": "unknown"}

    def capture_reference(self) -> dict[str, Any]:
        status = self.snapshot()
        leader = status.get("leader") or {}
        feedback = status.get("feedback") or {}
        positions = feedback.get("positions_rad") or {}
        if not leader.get("healthy") or not feedback.get("valid"):
            raise HardwareCompositionError("alignment_requires_live_leader_and_follower")
        try:
            follower_asset = self.store.load(
                "follower", self.identities["follower"]).data
            mapping = self.store.load("mapping", self.identities["mapping"]).data
            leader_asset = self.store.load(
                "leader", self.identities["leader"]).data
        except (FileNotFoundError, ValueError) as exc:
            raise HardwareCompositionError(f"mapping_unavailable:{exc}") from exc
        leader_rad = {}
        for cfg in mapping.get("joints", []):
            follower = cfg.get("follower")
            source = cfg.get("leader")
            if follower not in ARM_JOINTS or source not in leader.get("positions_rad", {}):
                raise HardwareCompositionError("leader_mapping_incomplete")
            leader_rad[follower] = float(leader["positions_rad"][source])
        reference = {
            "captured_mono_ns": time.monotonic_ns(),
            "leader_rad": leader_rad,
            "follower_rad": {name: positions[name] for name in JOINTS},
            "leader_gripper_percent": leader.get("gripper_percent"),
            "follower_gripper_raw": feedback.get("servo_pulses", {}).get("r_joint"),
            "leader_identity": self.identities["leader"],
            "follower_identity": self.identities["follower"],
            "mapping_identity": self.identities["mapping"],
        }
        calibration = ((leader_asset.get("device_identity") or {})
                       .get("lerobot_calibration"))
        try:
            travel = leader_travel_ranges_rad(calibration, mapping)
        except ValueError as exc:
            raise HardwareCompositionError(
                f"leader_normalization_unavailable:{exc}") from exc
        mapper = TeleopMapper(
            mapping, follower_asset.get("limits_rad") or {},
            leader_travel_rad=travel,
            gripper_mapping_mode="absolute_normalized")
        mapper.align(
            {name: positions[name] for name in ARM_JOINTS},
            now=time.monotonic(),
            gripper_pulse=reference["follower_gripper_raw"],
        )
        with self.lock:
            self.reference = reference
            self.mapper = mapper
        return reference

    def _follower_rad(self, feedback: dict[str, Any],
                      follower_asset: dict[str, Any]) -> tuple[dict[str, float], dict[str, int]]:
        raw: dict[str, int] = {}
        positions: dict[str, float] = {}
        for name in JOINTS:
            sample = (feedback.get("joint_samples") or {}).get(name) or {}
            value = sample.get("raw_position")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            raw[name] = int(value)
            transform = (follower_asset.get("raw_to_rad") or {}).get(name)
            if not isinstance(transform, dict):
                transform = follower_asset.get("raw_to_rad") or {}
            zero = float(transform.get("zero_tick", 500))
            direction = float(transform.get("direction", 1))
            positions[name] = (float(value) - zero) * RADIANS_PER_PULSE * direction
        return positions, raw

    def _preview(self, leader: dict[str, Any], follower: dict[str, float],
                 layers: dict[str, dict[str, Any]], *,
                 mapper_override: TeleopMapper | None = None) -> dict[str, Any]:
        with self.lock:
            reference = self.reference
        if not reference:
            return {"available": False, "reason": "alignment_reference_not_captured",
                    "motion_commands_sent": 0}
        if not leader.get("healthy"):
            return {"available": False, "reason": "leader_unhealthy",
                    "motion_commands_sent": 0}
        mapping = layers["mapping"]
        with self.lock:
            mapper = mapper_override if mapper_override is not None else self.mapper
        if mapper is None:
            return {"available": False, "reason": "mapper_not_aligned",
                    "motion_commands_sent": 0}
        now = time.monotonic()
        requests = []
        for cfg in mapping.get("joints", []):
            name = cfg.get("follower")
            leader_name = cfg.get("leader")
            if name not in follower or leader_name not in leader.get("positions_rad", {}):
                continue
            requests.append({
                "follower": name,
                "leader_rad": float(leader["positions_rad"][leader_name]),
                "leader_ref_rad": float(reference["leader_rad"][name]),
                "follower_ref_rad": float(reference["follower_rad"][name]),
            })
        targets = mapper.frame(requests, now)
        gripper_pct = leader.get("gripper_percent")
        gripper = None
        if isinstance(gripper_pct, (int, float)) and reference.get("follower_gripper_raw") is not None:
            gripper = mapper.gripper_target(
                float(gripper_pct),
                float(reference.get("leader_gripper_percent") or 0.0),
                float(reference["follower_gripper_raw"]),
                now,
            )
        return {
            "available": len(targets) == len(ARM_JOINTS) and gripper is not None,
            "reason": "preview_only_no_execution_output",
            "leader_rad": dict(leader.get("positions_rad") or {}),
            "follower_rad": dict(follower),
            "reference": reference,
            "targets": targets,
            "gripper": gripper,
            "motion_commands_sent": 0,
            "outputs_enabled": False,
        }

    def snapshot(self, *, car_status: dict[str, Any] | None = None,
                 network_elapsed_s: float = 0.0,
                 advance_preview: bool = True) -> dict[str, Any]:
        request_started = time.monotonic()
        if car_status is None:
            car = _http_json(self.agent_url, "/status", timeout=self.timeout_s)
            network_elapsed_s = max(0.0, time.monotonic() - request_started)
        else:
            car = car_status
            network_elapsed_s = max(0.0, float(network_elapsed_s))
        leader = self.upper.reader.snapshot() if self.upper.reader is not None else {
            "connected": False,
            "detected": bool(self.upper.detected_port()
                             and Path(self.upper.detected_port()).exists()),
            "healthy": False,
            "reason": ("leader_detected_not_opened"
                       if self.upper.detected_port()
                       and Path(self.upper.detected_port()).exists()
                       else "leader_not_detected"),
            "source": self.upper.detected_port(),
            "positions_rad": {}, "gripper_percent": None,
        }
        raw_feedback = (car.get("control") or {}).get("follower_feedback") or {}
        try:
            layers = self._load_layers()
            follower, raw_positions = self._follower_rad(raw_feedback, layers["follower"])
        except (FileNotFoundError, ValueError):
            layers = {}
            raw_positions = {
                name: int(sample["raw_position"])
                for name in JOINTS
                if isinstance((sample := (raw_feedback.get("joint_samples") or {}).get(name)), dict)
                and isinstance(sample.get("raw_position"), (int, float))
                and not isinstance(sample.get("raw_position"), bool)
            }
            follower = {
                name: (float(raw) - 500.0) * RADIANS_PER_PULSE
                for name, raw in raw_positions.items()
            }
        now = time.monotonic_ns()
        readiness = self.calibration_readiness()
        calibration_bound = bool(readiness.get("ready"))
        stale_after_raw = raw_feedback.get("stale_after_s", 1.0)
        stale_after_s = (
            float(stale_after_raw)
            if isinstance(stale_after_raw, (int, float))
            and not isinstance(stale_after_raw, bool)
            and math.isfinite(float(stale_after_raw))
            and float(stale_after_raw) > 0
            else 1.0
        )
        input_available = raw_feedback.get("available") is True
        input_reason = str(raw_feedback.get("reason") or "feedback_incomplete")
        scan_span_raw = raw_feedback.get("scan_span_s")
        scan_span_s = (
            float(scan_span_raw)
            if isinstance(scan_span_raw, (int, float))
            and not isinstance(scan_span_raw, bool)
            and math.isfinite(float(scan_span_raw))
            and float(scan_span_raw) >= 0
            else None
        )
        input_scan_ok = scan_span_s is not None and scan_span_s <= stale_after_s
        samples = []
        for name in JOINTS:
            source = (raw_feedback.get("joint_samples") or {}).get(name) or {}
            age_s = source.get("age_s")
            if not isinstance(age_s, (int, float)) or isinstance(age_s, bool):
                continue
            local_age_s = max(0.0, float(age_s)) + network_elapsed_s
            local_received = now - int(local_age_s * 1_000_000_000)
            raw_value = source.get("raw_position")
            if name not in follower or not isinstance(raw_value, (int, float)):
                continue
            sample_errors = []
            if source.get("source_kind") != "servo_position_readback":
                sample_errors.append("untrusted_source")
            if source.get("servo_id") != {
                    "joint1": 1, "joint2": 2, "joint3": 3, "joint4": 4,
                    "joint5": 5, "r_joint": 10}[name]:
                sample_errors.append("servo_id_mismatch")
            if source.get("valid") is not True:
                sample_errors.append("sample_invalid")
            if source.get("error") not in (None, ""):
                sample_errors.append(str(source.get("error")))
            if not -1000 <= float(raw_value) <= 1000:
                sample_errors.append("raw_out_of_range")
            if local_age_s > stale_after_s:
                sample_errors.append("sample_stale")
            if not input_available:
                sample_errors.append(input_reason)
            if not input_scan_ok:
                sample_errors.append("scan_span_invalid")
            if not calibration_bound:
                sample_errors.append("calibration_not_configured")
            sample_valid = not sample_errors
            samples.append({
                "name": name,
                "servo_id": source.get("servo_id"),
                "raw_position": raw_value,
                "position_rad": follower[name],
                "source_kind": source.get("source_kind"),
                "valid": sample_valid,
                "error": None if sample_valid else ",".join(dict.fromkeys(sample_errors)),
                "calibration_hash": readiness.get("bundle_hash"),
                "sample_seq": source.get("sample_seq"),
                "received_mono_ns": local_received,
                "sample_time_ns": source.get("sample_time_ns"),
                "clock_domain": "upper_receipt_monotonic",
                "age_s": local_age_s,
                "age_ns": int(local_age_s * 1_000_000_000),
            })
        feedback = {
            "valid": bool(
                calibration_bound
                and input_available
                and input_reason == "ok"
                and input_scan_ok
                and len(samples) == 6
                and all(item["valid"] for item in samples)
            ),
            "source_kind": "servo_position_readback",
            "clock_domain": "upper_receipt_monotonic",
            "samples": samples,
            "joint_samples": {item["name"]: item for item in samples},
            "positions_rad": follower,
            "servo_pulses": raw_positions,
            "scan_span_s": scan_span_s,
            "scan_span_ns": int((scan_span_s or 0.0) * 1_000_000_000),
            "stale_after_s": stale_after_s,
            "input_available": input_available,
            "input_reason": input_reason,
            "publisher_instance": raw_feedback.get("publisher_instance"),
            "publisher_generation": raw_feedback.get("publisher_generation"),
            "publisher_identity_kind": raw_feedback.get("publisher_identity_kind"),
            "network_elapsed_s": round(network_elapsed_s, 6),
            "reason": "ok" if input_available and input_reason == "ok"
            and input_scan_ok and samples else input_reason,
            "calibration_binding": ("bound" if calibration_bound
                                    else "not_configured"),
        }
        hybrid = car.get("hybrid") if isinstance(car.get("hybrid"), dict) else {}
        frame_process = ((((car.get("licence") or {}).get("bridge") or {})
                          .get("frame_process")))
        sensors = car.get("sensors") if isinstance(car.get("sensors"), dict) else {}
        rgb = sensors.get("rgb") if isinstance(sensors.get("rgb"), dict) else {}
        alignment_valid = bool(leader.get("healthy") and feedback["valid"])
        reference = self.reference or {}
        preview = self._preview(leader, follower, layers) if layers and advance_preview else {
            "available": False, "reason": ("preview_not_advanced" if layers else "calibration_layers_missing"),
            "motion_commands_sent": 0,
        }
        return {
            "leader_connected": bool(leader.get("connected")),
            "leader_healthy": bool(leader.get("healthy")),
            "feedback_valid": bool(feedback["valid"]),
            "exclusive_control": bool(hybrid.get("driver_bound") is True),
            "alignment_valid": alignment_valid,
            "alignment_reference": {
                "leader_rad": [reference.get("leader_rad", {}).get(name) for name in ARM_JOINTS],
                "follower_rad": [reference.get("follower_rad", {}).get(name) for name in JOINTS],
            } if reference else None,
            "leader": leader,
            "feedback": feedback,
            "preview": preview,
            "frame_process": frame_process,
            "trial_motion": (frame_process or {}).get("trial_motion"),
            "devices": {
                "leader": {"connected": bool(leader.get("connected")),
                           "source": leader.get("source"),
                           "simulated": False},
                "follower": {"driver_bound": hybrid.get("driver_bound") is True,
                             "source": "ros_robot_controller/servo_readback",
                             "simulated": False},
                "camera": {"source": rgb.get("topic"), "stale": rgb.get("stale"),
                           "simulated": False},
            },
            "video": {
                "available": bool(rgb.get("topic") and not rgb.get("stale")),
                "valid": bool(rgb.get("topic") and not rgb.get("stale")),
                "stale": bool(rgb.get("stale", True)),
                # A frozen picture must not be described as a fresh frame: this string is
                # what the operator reads when the camera stopped publishing.
                "reason": ("camera_unavailable" if not rgb.get("topic")
                           else ("camera_frame_stale" if rgb.get("stale", True)
                                 else "fresh_camera_frame")),
                "preview_seq": rgb.get("preview_seq"),
                "source_stamp_ns": rgb.get("source_stamp_ns"),
                "age_s": rgb.get("age_s"),
                "topic": rgb.get("topic"),
                "source": rgb.get("topic"),
            },
            "calibration_hash": readiness.get("bundle_hash"),
            "outputs_enabled": False,
            "simulated": False,
        }


class ReadbackOnlyDriver:
    """Upper T1 driver surface; never writes an actuator."""

    def __init__(self, observer: HardwareObserver) -> None:
        self.observer = observer

    def capabilities(self) -> dict[str, Any]:
        return {
            "driver": "upper_readback_only",
            "outputs_enabled": False,
            "driver_sent": "unavailable",
            "controller_acknowledged": "unavailable",
            "execution_observed": "unavailable",
            "trusted_source": "servo_position_readback",
        }

    def read_snapshot(self) -> dict[str, Any]:
        return self.observer.snapshot()

    def submit(self, command: dict) -> dict:
        del command
        raise HardwareCompositionError("readback_only_driver_has_no_output")

    def stop(self, reason: str) -> dict:
        return {"stopped": True, "reason": str(reason),
                "hardware_effect": "not_applicable_readback_only",
                "driver_sent": "unavailable"}


class CarHttpOutputDriver:
    """Forward admitted commands to the car gateway and require a driver send."""

    def __init__(self, lease: CarControlLease, observer: HardwareObserver,
                 agent_url: str, *, timeout_s: float = 4.0) -> None:
        self.lease = lease
        self.observer = observer
        self.agent_url = agent_url.rstrip("/")
        self.timeout_s = timeout_s
        self.enabled = False
        self.commands_sent = 0
        self.last_result: dict[str, Any] | None = None
        self.lock = threading.RLock()

    def capabilities(self) -> dict[str, Any]:
        with self.lock:
            return {
                "driver": "car_http_ros_servo_output",
                "output_capable": True,
                "outputs_enabled": self.enabled,
                "command_topic": "/ros_robot_controller/bus_servo/set_position",
                "hardware_effect": "unverified",
                "controller_acknowledged": "unavailable",
                "execution_observed": "unavailable",
                "commands_sent": self.commands_sent,
            }

    def read_snapshot(self) -> dict[str, Any]:
        return self.observer.snapshot()

    def enable(self, session: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self.lease.token
        if not token:
            raise HardwareCompositionError("car_control_lease_missing")
        if not isinstance(session, dict):
            raise HardwareCompositionError("session_identity_required")
        body = {
            "session_id": session.get("session_id"),
            "session_epoch": session.get("session_epoch"),
            "calibration_bundle_hash": session.get("calibration_bundle_hash"),
        }
        result = _http_json(
            self.agent_url, "/hybrid/enable_output", method="POST",
            body=body, token=token, timeout=self.timeout_s)
        if result.get("outputs_enabled") is not True:
            raise HardwareCompositionError("car_output_enable_not_confirmed")
        with self.lock:
            self.enabled = True
            self.last_result = dict(result)
        return result

    def submit(self, command: dict[str, Any]) -> dict[str, Any]:
        token = self.lease.token
        if not token:
            raise HardwareCompositionError("car_control_lease_missing")
        with self.lock:
            if not self.enabled:
                raise HardwareCompositionError("outputs_disabled")
        deadline = command.get("deadline_mono_ns")
        now_ns = time.monotonic_ns()
        if isinstance(deadline, bool) or not isinstance(deadline, int) or deadline <= now_ns:
            raise HardwareCompositionError("command_expired_before_transport")
        remaining_ms = max(1, min(200, (deadline - now_ns) // 1_000_000))
        forwarded = dict(command)
        forwarded["ttl_ms"] = int(remaining_ms)
        forwarded.pop("received_mono_ns", None)
        forwarded.pop("deadline_mono_ns", None)
        response = _http_json(
            self.agent_url, "/hybrid/apply", method="POST",
            body={"command": forwarded}, token=token, timeout=self.timeout_s)
        accepted = response.get("accepted") or {}
        dispatched = response.get("dispatch") or {}
        if accepted.get("accepted") is not True:
            raise HardwareCompositionError(
                "car_gateway_rejected_command:" + json.dumps(accepted, sort_keys=True))
        if dispatched.get("driver_sent") is not True:
            raise HardwareCompositionError(
                "car_driver_send_not_confirmed:" +
                json.dumps(dispatched, sort_keys=True))
        with self.lock:
            self.commands_sent += 1
            self.last_result = dict(dispatched)
        return dispatched

    def stop(self, reason: str) -> dict[str, Any]:
        token = self.lease.token
        with self.lock:
            self.enabled = False
        if not token:
            return {"stopped": True, "reason": str(reason),
                    "driver_stopped": False,
                    "hardware_effect": "unverified"}
        result = _http_json(
            self.agent_url, "/hybrid/stop", method="POST",
            body={"reason": str(reason)}, token=token, timeout=self.timeout_s)
        return {**result, "driver_stopped": True}


class HardwarePreviewFacade(HybridPublicFacade):
    """Expose preview capability while keeping hardware output and verification false."""

    PREVIEW_ACTIONS = (
        "preview", "align", "arm", "engage", "release_clutch", "pause_all",
        "estop", "clear_estop",
        "lock_base", "validate", "save", "start_session", "start_episode",
        "set_phase", "end_episode", "stop_session", "sync", "export",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._follow_stop = threading.Event()
        self._follow_thread: threading.Thread | None = None
        self._follow_commands = 0
        self._follow_error: str | None = None

    def _stop_follow_worker(self) -> None:
        self._follow_stop.set()
        worker = self._follow_thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1.0)
        self._follow_thread = None

    def _start_follow_worker(self) -> None:
        self._stop_follow_worker()
        self._follow_stop = threading.Event()
        self._follow_thread = threading.Thread(
            target=self._follow_worker_loop,
            args=(self._follow_stop,),
            name="lelab-follow-worker",
            daemon=True,
        )
        self._follow_error = None
        self._follow_thread.start()

    def _follow_fail(self, reason: str, detail: dict | None = None) -> None:
        self._follow_error = reason
        if self._primary_error is None:
            self._primary_error = reason
            self._primary_fault_detail = copy.deepcopy(detail)
        self.last_error = self._primary_error or reason
        self.state = "fault"
        self.arm_state = "idle"
        try:
            self._stop(reason)
        except Exception:
            pass

    def _follow_worker_loop(self, stop: threading.Event) -> None:
        next_tick = time.monotonic()
        first_frame = True
        while not stop.is_set():
            next_tick += 0.1
            try:
                with self.lock:
                    if self.state != "ready" or self.arm_state != "following":
                        break
                    cached_facts = self._facts if first_frame else None
                facts = cached_facts if isinstance(cached_facts, dict) else \
                    self.observer.snapshot()
                first_frame = False
                feedback = facts.get("feedback")
                gate = TrustedFeedbackGate.gate(
                    feedback,
                    calibration_hash=self.bundle_hash or "",
                    now_ns=self.clock_ns(),
                    max_age_ns=1_000_000_000,
                )
                with self.lock:
                    if stop.is_set() or self.arm_state != "following":
                        break
                    self._facts = facts
                    self._feedback_gate = gate
                    if gate.get("trusted") is not True:
                        raise HardwareCompositionError(
                            "follow_feedback_untrusted:" +
                            ",".join(gate.get("reasons") or []))
                preview = facts.get("preview") or {}
                targets = preview.get("targets")
                if preview.get("available") is not True or not isinstance(targets, list):
                    raise HardwareCompositionError("follow_preview_unavailable")
                by_joint = {
                    item.get("joint"): item for item in targets
                    if isinstance(item, dict)
                }
                if set(by_joint) != set(ARM_JOINTS):
                    raise HardwareCompositionError("follow_arm_targets_incomplete")
                arm_target = {
                    "joint_names": list(ARM_JOINTS),
                    "positions_rad": [
                        float(by_joint[name]["target_rad"]) for name in ARM_JOINTS
                    ],
                }
                self.submit_intent(
                    "arm", arm_target, sample_seq=self._follow_commands + 1,
                    ttl_ms=DEFAULT_TTL_MS, clutch=True, now_ns=self.clock_ns())
                arm_sent = self.dispatch("arm", now_ns=self.clock_ns())
                if arm_sent is None or arm_sent.get("driver_sent") is not True:
                    raise HardwareCompositionError(
                        "follow_arm_driver_send_failed:" +
                        json.dumps(arm_sent or {}, sort_keys=True))
                gripper = preview.get("gripper") or {}
                if not isinstance(gripper.get("target_rad"), (int, float)):
                    raise HardwareCompositionError("follow_gripper_target_missing")
                gripper_target = {
                    "joint_name": "r_joint",
                    "position_rad": float(gripper["target_rad"]),
                }
                self.submit_intent(
                    "gripper", gripper_target,
                    sample_seq=self._follow_commands + 2,
                    ttl_ms=DEFAULT_TTL_MS, clutch=True, now_ns=self.clock_ns())
                grip_sent = self.dispatch("gripper", now_ns=self.clock_ns())
                if grip_sent is None or grip_sent.get("driver_sent") is not True:
                    raise HardwareCompositionError(
                        "follow_gripper_driver_send_failed:" +
                        json.dumps(grip_sent or {}, sort_keys=True))
                self._follow_commands += 2
            except Exception as exc:  # noqa: BLE001 - any loop fault parks the session
                self._follow_fail(f"follow_worker_failed:{type(exc).__name__}:{exc}")
                break
            if stop.wait(max(0.0, next_tick - time.monotonic())):
                break

    def status(self) -> dict[str, Any]:
        out = super().status()
        out["follow_runtime"] = {
            "active": bool(
                self._follow_thread is not None and self._follow_thread.is_alive()),
            "commands_sent": self._follow_commands,
            "last_error": self._follow_error,
        }
        if self._is_simulated() or not isinstance(self.observer, HardwareObserver):
            return out
        out["calibrations"] = self.observer.calibration_readiness()
        try:
            facts = self.observer.snapshot()
        except HardwareCompositionError as exc:
            out["hardware_probe"] = {"available": False, "reason": str(exc)}
            return out
        out["devices"] = facts.get("devices", {})
        out["feedback"] = {
            **(facts.get("feedback") or {}),
            "trusted": bool((facts.get("feedback") or {}).get("valid")),
            "trusted_reasons": [],
            "source_kind": "servo_position_readback",
            "note": "six measured servo positions relayed with age; no command cache",
        }
        out["video"] = facts.get("video", out.get("video"))
        out["leader"] = facts.get("leader")
        out["hardware_preview"] = facts.get("preview")
        out["frame_process"] = facts.get("frame_process")
        out["trial_motion"] = facts.get("trial_motion")
        out["hardware_probe"] = {
            "available": True,
            "leader_connected": facts.get("leader_connected"),
            "leader_healthy": facts.get("leader_healthy"),
            "feedback_valid": facts.get("feedback_valid"),
            "exclusive_control": facts.get("exclusive_control"),
            "outputs_enabled": out.get("outputs_enabled") is True,
        }
        return out

    def set_arm_state(self, event: Any) -> dict[str, Any]:
        action = event.get("action") if isinstance(event, dict) else event
        if action in ("align", "engage") and isinstance(self.observer, HardwareObserver):
            # Validate before touching the reference: stale or out-of-order requests
            # must not alter the active mapper. Rebase at the explicit engage, too,
            # because the operator can reposition the leader after preview alignment.
            with self.lock:
                self._validate_optional_identity(event)
                self._refresh()
                expected = "preview" if action == "align" else "armed"
                if self.state != "ready" or self.arm_state != expected:
                    raise HardwareCompositionError("alignment_transition_invalid")
                self.observer.capture_reference()
                if action == "engage":
                    facts = self.observer.snapshot()
                    reference = facts.get("alignment_reference")
                    preview = facts.get("preview") or {}
                    feedback = (facts.get("feedback") or {}).get("positions_rad") or {}
                    targets = preview.get("targets") or []
                    gripper = preview.get("gripper") or {}
                    errors = []
                    if facts.get("alignment_valid") is not True or not isinstance(reference, dict):
                        errors.append("reference_invalid")
                    if preview.get("available") is not True or len(targets) != len(ARM_JOINTS):
                        errors.append("preview_incomplete")
                    for target in targets:
                        name = target.get("joint")
                        value = target.get("target_rad")
                        if (target.get("clipped") or name not in feedback or
                                not isinstance(value, (int, float)) or not math.isfinite(value) or
                                abs(value - float(feedback[name])) > 0.02):
                            errors.append(f"arm:{name}")
                    grip_value = gripper.get("target_rad")
                    gripper_absolute = gripper.get("mapping_mode") == "absolute_normalized"
                    if (gripper.get("clipped") or "r_joint" not in feedback or
                            not isinstance(grip_value, (int, float)) or not math.isfinite(grip_value) or
                            (not gripper_absolute
                             and abs(grip_value - float(feedback["r_joint"])) > 0.02)):
                        errors.append("gripper")
                    if errors:
                        raise HardwareCompositionError("engage_target_not_continuous:" + ",".join(errors))
                    self._facts = facts
                    self._alignment = dict(reference)
        if action in ("pause_all", "release_clutch", "lock_base", "estop",
                      "clear_estop", "reconnect", "fault"):
            self._stop_follow_worker()
        out = super().set_arm_state(event)
        if action == "engage" and self.arm_state == "following":
            self._start_follow_worker()
        return out

    def capabilities(self) -> dict[str, Any]:
        report = super().capabilities()
        if self._is_simulated():
            return report
        bound = {
            "upper": self.upper is not None,
            "car": self.car is not None,
            "observer": self.observer is not None,
            "driver": self.driver is not None,
        }
        adapters_ready = all(bound.values())
        calibration = (self.observer.calibration_readiness()
                       if isinstance(self.observer, HardwareObserver)
                       else {"ready": False, "reasons": ["observer_not_hardware"]})
        calibration_ready = bool(calibration.get("ready"))
        preview_ready = adapters_ready and calibration_ready
        actions = (list(self.PREVIEW_ACTIONS) if preview_ready else
                   (["validate", "save", "estop"] if adapters_ready else ["estop"]))
        report.update({
            "available": adapters_ready,
            "preview_available": preview_ready,
            "hardware_verified": False,
            "outputs_enabled": bool(
                self.router is not None and self.router.outputs_enabled),
            "actions": actions,
            "adapters_bound": bound,
            "reason": ("hardware_preview_and_readback_ready_no_output"
                       if preview_ready else
                       ("hardware_adapters_bound_calibration_not_ready"
                        if adapters_ready else "hardware_adapters_not_fully_bound")),
            "calibration_readiness": calibration,
            "control_semantics": (
                "hardware_follow_with_verified_stop_pending"
                if self.router is not None and self.router.outputs_enabled
                else "preview_readback_only"),
        })
        detail = report.get("profiles_detail")
        if isinstance(detail, dict) and isinstance(detail.get("hardware_hybrid_manual_v1"), dict):
            detail["hardware_hybrid_manual_v1"].update({
                "available": preview_ready,
                "actions": actions,
                "reason": report["reason"],
            })
        return report

    def mapping_directions(self) -> dict[str, Any]:
        if self.session_id is not None:
            raise HybridSessionError("release_session_before_mapping_change")
        if not isinstance(self.observer, HardwareObserver):
            raise HardwareCompositionError("mapping_observer_unavailable")
        record = self.calibration_store.load(
            "mapping", self.observer.identities["mapping"])
        mapping = record.data
        return {
            "identity": record.identity,
            "version": record.version,
            "sha256": record.sha256,
            "joints": [
                {
                    "follower": item.get("follower"),
                    "leader": item.get("leader"),
                    "direction": int(item.get("direction", 1)),
                }
                for item in mapping.get("joints", [])
                if isinstance(item, dict)
            ],
            "gripper": {
                "follower": (mapping.get("gripper") or {}).get("follower", "r_joint"),
                "leader": "gripper",
                "direction": int(
                    (mapping.get("gripper") or {}).get("direction", 1)),
            },
        }

    def save_mapping_directions(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.session_id is not None:
            raise HybridSessionError("release_session_before_mapping_change")
        if not isinstance(self.observer, HardwareObserver):
            raise HardwareCompositionError("mapping_observer_unavailable")
        if set(body) != {"directions"} or not isinstance(body.get("directions"), dict):
            raise HybridSessionError("mapping_directions_required")
        requested = body["directions"]
        allowed = set(JOINTS)
        if set(requested) != allowed:
            raise HybridSessionError("mapping_directions_must_cover_all_six_joints")
        for name, direction in requested.items():
            if isinstance(direction, bool) or direction not in (-1, 1):
                raise HybridSessionError(f"invalid_mapping_direction:{name}")
        record = self.calibration_store.load(
            "mapping", self.observer.identities["mapping"])
        mapping = dict(record.data)
        joints = []
        for item in mapping.get("joints", []):
            updated = dict(item)
            follower = updated.get("follower")
            if follower not in allowed:
                raise HybridSessionError(f"mapping_follower_unknown:{follower}")
            updated["direction"] = int(requested[follower])
            joints.append(updated)
        if {item.get("follower") for item in joints} != set(ARM_JOINTS):
            raise HybridSessionError("mapping_arm_joints_incomplete")
        mapping["joints"] = joints
        gripper = dict(mapping.get("gripper") or {})
        gripper["direction"] = int(requested["r_joint"])
        mapping["gripper"] = gripper
        mapping["motion_profile"] = "operator_adjusted_direction_pending_live_validation"
        provenance = list(mapping.get("provenance") or [])
        provenance.append(
            "operator changed per-axis direction in the direction editor; "
            "release/realign required")
        mapping["provenance"] = provenance
        saved = self.calibration_store.save(
            "mapping", self.observer.identities["mapping"], mapping)
        return {
            "saved": True,
            "identity": saved.identity,
            "version": saved.version,
            "sha256": saved.sha256,
            "directions": {name: int(value) for name, value in requested.items()},
            "action_required": "reacquire_preview_align_before_enable",
        }

    def preview(self, request: Any = None) -> dict[str, Any]:
        out = super().preview(request)
        facts = self._facts if isinstance(self._facts, dict) else {}
        out["hardware_preview"] = facts.get("preview")
        out["leader"] = facts.get("leader")
        out["follower_feedback"] = facts.get("feedback")
        out["motion_commands_sent"] = 0
        out["outputs_enabled"] = False
        return out


def _calibration_root() -> Path:
    return Path(os.environ.get(
        "LELAB_HYBRID_CALIBRATION_ROOT",
        str(Path.home() / ".cache" / "lelab" / "hybrid_calibration"),
    )).expanduser()


def build_hardware_facade(agent_url: str | None = None) -> HardwarePreviewFacade:
    """Build inert real adapters; hardware is contacted only after an explicit request."""
    agent_url = (agent_url or os.environ.get("LELAB_ROS_AGENT_URL", "http://127.0.0.1:8110")).rstrip("/")
    transport = os.environ.get("LELAB_HYBRID_TRANSPORT", "legacy_http")
    if transport == "frame_stream_v1":
        from .hybrid_stream_hardware import build_stream_hardware_facade
        return build_stream_hardware_facade(agent_url)
    if transport != "legacy_http":
        raise HardwareCompositionError(f"unknown_hybrid_transport:{transport}")
    store = HybridCalibrationStore(_calibration_root())
    identities = {
        "leader": os.environ.get("LELAB_HYBRID_LEADER_ID", "landerpi-so101-leader-v1"),
        "follower": os.environ.get("LELAB_HYBRID_FOLLOWER_ID", "landerpi-follower-v1"),
        "mapping": os.environ.get("LELAB_HYBRID_MAPPING_ID", "landerpi-so101-mapping-v1"),
    }
    upper = UpperLeaderLease(
        agent_url=agent_url, store=store, leader_identity=identities["leader"])
    car = CarControlLease(agent_url)
    observer = HardwareObserver(
        upper=upper, agent_url=agent_url, store=store,
        leader_identity=identities["leader"],
        follower_identity=identities["follower"],
        mapping_identity=identities["mapping"],
    )
    facade = HardwarePreviewFacade(
        upper=upper, car=car, observer=observer, calibration_store=store)
    facade.bind_driver(CarHttpOutputDriver(car, observer, agent_url))
    facade.hardware_identities = identities
    return facade
