"""T4-facing public facade for the hybrid manual session (Wave A).

`hybrid_session.HybridSession` is the core facade.  This thin subclass adds the three
route methods T4 calls (capabilities / calibration / recording), the protocol-v1
channel router, the strict asset policy and the trusted-feedback gate.  It deliberately
does not modify the core file, which is shared with a concurrently editing session.

Frozen public API (parallel plan section 3.3):
    status(), acquire_session(request), release_session(reason), set_arm_state(event),
    ingest_gamepad(sample), preview(), calibration capability/readiness methods.
Additional route-facing methods: capabilities(), calibration(action, kind, body),
    recording(action, payload), submit_intent(...), dispatch(channel), bind_driver(...),
    trusted_feedback().

No rclpy import, no device access, no actuator output.  With no adapters installed
the factory returns a fail-closed facade whose status() is safe to poll.
"""
from __future__ import annotations

import copy
import datetime as _dt
import math
import time
from typing import Any

from .hybrid_calibration import KINDS, ROLES, validate_asset
from .hybrid_channel import (CHANNELS, CHANNEL_SOURCES, CONFIRMATION_LEVELS,
                             TRUSTED_SOURCE, UNTRUSTED_SOURCES, HybridChannelRouter,
                             HybridProtocolError, HybridSessionError, TrustedFeedbackGate,
                             command_v1)
from .hybrid_session import HybridSession

HARDWARE_PROFILE = "hardware_hybrid_manual_v1"
SIMULATED_PROFILE = "simulated_hybrid_manual_v1"
PROFILE = HARDWARE_PROFILE
MAX_TTL_MS = 200
DEFAULT_TTL_MS = 150
DEFAULT_MAX_CALIBRATION_AGE_DAYS = 30
SIMULATED_ACTIONS = (
    "preview", "align", "arm", "engage", "release_clutch", "pause_all",
    "estop", "clear_estop", "enable_base", "lock_base",
    "validate", "save",
    "start_session", "start_episode", "set_phase", "end_episode", "stop_session",
    "sync", "export",
)


def validate_asset_strict(kind: str, asset: Any, *, now_ts: float | None = None,
                          max_age_days: int | None = DEFAULT_MAX_CALIBRATION_AGE_DAYS) -> list[str]:
    """validate_asset + explicit rejection of examples, simulated vectors and stale copies.

    A JSON document passing this check is still not proof of a physical calibration.
    """
    if not isinstance(asset, dict):
        return ["asset_must_be_object"]
    reasons = list(validate_asset(kind, asset))
    if asset.get("simulated") is True or asset.get("_example_not_measured"):
        reasons.append("simulated_or_example_asset")
    stamp = asset.get("measured_at_utc")
    parsed: _dt.datetime | None = None
    if isinstance(stamp, str):
        try:
            parsed = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            parsed = None
    if parsed is None:
        reasons.append("measurement_time_missing_or_invalid")
    elif max_age_days is not None:
        now = time.time() if now_ts is None else float(now_ts)
        if not math.isfinite(now):
            reasons.append("invalid_now")
        elif (now - parsed.timestamp()) > max_age_days * 86400 or parsed.timestamp() > now + 86400:
            reasons.append("calibration_over_age")
    return reasons


def capability_report(*, adapters_bound: dict[str, bool] | None = None,
                      simulated: bool = False, profile: str | None = None) -> dict[str, Any]:
    """Unknown camera / firmware capabilities are reported, never guessed as healthy."""
    active_profile = profile or (SIMULATED_PROFILE if simulated else HARDWARE_PROFILE)
    profiles = [active_profile]
    actions = list(SIMULATED_ACTIONS) if simulated else ["estop"]
    return {
        "schema_version": 1, "profile": active_profile,
        "profiles": profiles, "actions": actions,
        "profiles_detail": {
            HARDWARE_PROFILE: {
                "simulated": False, "available": False, "actions": ["estop"],
                "reason": "hardware_capabilities_unverified",
            },
            SIMULATED_PROFILE: {
                "simulated": True, "available": simulated, "actions": list(SIMULATED_ACTIONS),
                "reason": ("explicit_simulated_profile" if simulated
                           else "simulation_adapter_not_injected"),
            },
        },
        # `available` describes the physical hardware profile.  A simulation can be
        # positively usable for software integration without claiming physical output.
        "available": False, "simulated_available": simulated,
        "outputs_enabled": False, "hardware_verified": False,
        "control_semantics": "simulated_intent_only" if simulated else "offline_intent_only",
        "hardware_fail_closed": not simulated,
        "simulated": simulated,
        "calibration": {"kinds": list(KINDS), "roles": dict(ROLES),
                        "bundle_kind": "rig_bundle",
                        "camera_capability": "unknown", "firmware_capability": "unknown"},
        "channels": {name: {"permitted": False, "source": CHANNEL_SOURCES[name]}
                     for name in CHANNELS},
        "trusted_feedback_sources": [TRUSTED_SOURCE],
        "rejected_feedback_sources": list(UNTRUSTED_SOURCES),
        "confirmation_levels": list(CONFIRMATION_LEVELS),
        "camera_capability": "unknown", "firmware_capability": "unknown",
        "firmware_watchdog_verified": False, "hardware_stop_effect": "unverified",
        "controller_acknowledged": "unavailable",
        "adapters_bound": adapters_bound or {"upper": False, "car": False,
                                             "observer": False, "driver": False},
        "reason": ("explicit_simulated_profile" if simulated
                   else "wave_a_no_hardware_adapters_bound"),
    }


class HybridPublicFacade(HybridSession):
    """Core facade + route methods + epoch/channel/deadline coordination."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.router: HybridChannelRouter | None = None
        self.driver: Any | None = None
        self._intents: dict[str, int] = {name: 0 for name in CHANNELS}
        self._feedback_gate: dict[str, Any] = {
            "valid": False, "trusted": False, "reasons": ["trusted_feedback_unavailable"],
            "source_kind": None, "scan_span_ns": None, "joints": {},
        }

    # ------------------------------------------------------------------ safety hooks
    def _stop(self, reason: str) -> None:
        # Logical stop first: clear intents, permits and the deadline queue.
        if self.router is not None:
            try:
                self.router.stop(reason)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"channel_stop_failed:{type(exc).__name__}"
        super()._stop(reason)

    def _refresh(self) -> None:
        super()._refresh()
        feedback = self._facts.get("feedback") if isinstance(self._facts, dict) else None
        if isinstance(feedback, dict):
            self._feedback_gate = TrustedFeedbackGate.gate(
                feedback, calibration_hash=self.bundle_hash or "",
                now_ns=self.clock_ns(), max_age_ns=500_000_000)
            declared = feedback.get("source_kind")
            if declared in UNTRUSTED_SOURCES and self.state == "ready":
                self.last_error = f"untrusted_feedback_source:{declared}"
                self._stop(self.last_error)
                self.state = "fault"
        else:
            # The legacy `feedback_valid` hint is NOT proof of a measured readback.
            self._feedback_gate = {"valid": False, "trusted": False,
                                   "reasons": ["trusted_feedback_unavailable"],
                                   "source_kind": None, "scan_span_ns": None, "joints": {}}

    # ------------------------------------------------------------------ frozen API
    def status(self) -> dict[str, Any]:
        out = super().status()
        now = self.clock_ns()
        router = self.router.snapshot(now) if self.router is not None else None
        granted = router["permits"]["granted"] if router else {name: False for name in CHANNELS}
        for name in CHANNELS:
            channel = out["channels"].setdefault(name, {})
            channel["permitted"] = bool(granted.get(name))
            channel["seq"] = self._intents[name]
            channel["pending"] = router["pending"].get(name) if router else None
            channel["source"] = CHANNEL_SOURCES[name]
        feedback = copy.deepcopy(out.get("feedback")) if isinstance(out.get("feedback"), dict) else {}
        gate_joints = self._feedback_gate.get("joints")
        if isinstance(gate_joints, dict):
            samples = feedback.get("samples")
            if isinstance(samples, list):
                enriched = []
                for sample in samples:
                    if not isinstance(sample, dict):
                        continue
                    item = dict(sample)
                    trusted = gate_joints.get(item.get("name"))
                    if isinstance(trusted, dict):
                        item["age_ns"] = trusted.get("age_ns")
                        item["age_s"] = (
                            float(trusted["age_ns"]) / 1_000_000_000
                            if isinstance(trusted.get("age_ns"), int) else None
                        )
                    enriched.append(item)
                feedback["samples"] = enriched
        out["feedback"] = {**feedback, "trusted": bool(self._feedback_gate.get("trusted")),
                           "trusted_reasons": list(self._feedback_gate.get("reasons", [])),
                           "source_kind": self._feedback_gate.get("source_kind"),
                           "scan_span_ns": self._feedback_gate.get("scan_span_ns"),
                           "note": "vendor /controller_manager joint_states is a command cache, never measured"}
        out["calibration_bundle_hash"] = self.bundle_hash
        out["calibrations"] = {**(out.get("calibrations") or {}),
                               "camera_capability": "unknown", "firmware_capability": "unknown"}
        out["channels_meta"] = router
        out["outputs_enabled"] = bool(
            self.router is not None and self.router.outputs_enabled)
        out["control_semantics"] = (
            "hardware_follow_with_verified_stop_pending"
            if out["outputs_enabled"] else "preview_readback_only")
        out["simulated"] = self._is_simulated()
        out["profile"] = self._active_profile()
        return out

    def acquire_session(self, request: dict[str, Any]) -> dict[str, Any]:
        super().acquire_session(request)
        self.router = HybridChannelRouter(
            session_id=self.session_id, session_epoch=self.session_epoch,
            bundle_hash=self.bundle_hash, max_ttl_ms=MAX_TTL_MS,
            clock_ns=self.clock_ns, driver=self.driver, outputs_enabled=False)
        self._intents = {name: 0 for name in CHANNELS}
        return self.status()

    def release_session(self, reason: str, session_id=None,
                        session_epoch=None) -> dict[str, Any]:
        out = super().release_session(reason, session_id=session_id,
                                      session_epoch=session_epoch)
        self.router = None
        self._intents = {name: 0 for name in CHANNELS}
        self._feedback_gate = {"valid": False, "trusted": False,
                               "reasons": ["trusted_feedback_unavailable"],
                               "source_kind": None, "scan_span_ns": None, "joints": {}}
        return self.status()

    def set_arm_state(self, event: Any) -> dict[str, Any]:
        action = event.get("action") if isinstance(event, dict) else event
        if action != "estop":
            self._validate_optional_identity(event)
        if action == "engage" and self.arm_state == "armed":
            self._refresh()
            if self.state != "ready":
                raise HybridSessionError("session_not_ready")
            declared = (self.driver.capabilities()
                        if self.driver is not None else {})
            if declared.get("output_capable") is True:
                if not self._feedback_gate.get("trusted"):
                    raise HybridSessionError("trusted_feedback_unavailable")
                enable = getattr(self.driver, "enable", None)
                if not callable(enable):
                    raise HybridSessionError("output_driver_enable_unavailable")
                enable({
                    "session_id": self.session_id,
                    "session_epoch": self.session_epoch,
                    "calibration_bundle_hash": self.bundle_hash,
                })
        out = super().set_arm_state(event)
        if self.router is not None:
            if action in ("pause_all", "estop", "reconnect", "calibration_changed", "fault",
                          "clear_estop", "release_clutch", "lock_base"):
                self.router.revoke(*CHANNELS)
                if action in ("release_clutch", "lock_base"):
                    self.router.stop(action)
                if action in ("reconnect", "calibration_changed"):
                    # No auto-follow: old intents are cleared and the session is parked.
                    self.router.rollback(f"no_auto_follow:{action}")
                    self.state = "fault"
                    self.last_error = f"realign_required_after:{action}"
            elif action == "enable_base" and self.base_enabled:
                self.router.grant("base")
            elif action == "engage" and self.arm_state == "following":
                declared = self.driver.capabilities() if self.driver is not None else {}
                if declared.get("output_capable") is True:
                    self.router.enable_outputs()
                self.router.grant("arm", "gripper")
            elif action == "set_phase" and self.phase == "grasp":
                self.router.revoke("base")
        return self.status()

    def ingest_gamepad(self, sample: dict[str, Any]) -> dict[str, Any]:
        try:
            out = super().ingest_gamepad(sample)
        except RuntimeError as exc:
            # Map unconfigured gamepad / inactive session to a readable 409 instead of 500.
            raise HybridSessionError(str(exc)) from exc
        out["command_submitted"] = False
        intent = out.get("intent")
        if self.router is None or not self.base_enabled or not out.get("armed") or not intent:
            out["command_reason"] = "base_channel_not_permitted"
            return out
        self._intents["base"] += 1
        try:
            command = command_v1(
                session_id=self.session_id, session_epoch=self.session_epoch,
                channel="base", source="gamepad", seq=self._intents["base"],
                sample_seq=sample["seq"], ttl_ms=DEFAULT_TTL_MS,
                calibration_bundle_hash=self.bundle_hash,
                target={"vx_m_s": float(intent[0]), "vy_m_s": float(intent[1]),
                        "wz_rad_s": float(intent[2])})
            result = self.router.submit(command, now_ns=self.clock_ns())
        except (HybridProtocolError, KeyError, TypeError, ValueError) as exc:
            out["command_reason"] = getattr(exc, "reason", type(exc).__name__)
            return out
        out["command_submitted"] = True
        out["command"] = command
        out["confirmation"] = result["confirmation"]
        out["outputs_enabled"] = False
        return out

    def preview(self, request: Any = None) -> dict[str, Any]:
        out = super().preview(request)
        out["channels_meta"] = self.router.snapshot(self.clock_ns()) if self.router else None
        out["trusted_feedback"] = dict(self._feedback_gate)
        out["motion_commands_sent"] = 0
        out["reason"] = ("preview_only_explicit_simulated_profile" if self._is_simulated()
                         else "preview_only_no_trusted_hardware_driver_bound")
        return out

    # ------------------------------------------------------------------ route methods
    def capabilities(self) -> dict[str, Any]:
        simulated = self._is_simulated()
        report = capability_report(
            adapters_bound={"upper": self.upper is not None, "car": self.car is not None,
                            "observer": self.observer is not None, "driver": self.driver is not None},
            simulated=simulated, profile=self._active_profile())
        report["calibration_readiness"] = self.calibration_readiness()
        if self.driver is not None:
            try:
                declared = self.driver.capabilities()
                if isinstance(declared, dict):
                    report["driver_declared"] = declared
            except Exception as exc:  # noqa: BLE001
                report["driver_error"] = type(exc).__name__
        return report

    def calibration(self, action: str, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        """Route-facing calibration capability (validate / save). No device probing."""
        if kind not in KINDS:
            raise HybridSessionError(f"unknown_calibration_kind:{kind}")
        asset = (body.get("payload") if isinstance(body.get("payload"), dict)
                 else body.get("asset"))
        if action == "validate":
            reasons = validate_asset_strict(kind, asset, now_ts=body.get("now_ts"))
            return {"valid": not reasons, "reasons": reasons, "action": "validate",
                    "kind": kind, "simulated": False,
                    "camera_capability": "unknown", "firmware_capability": "unknown"}
        if action == "save":
            if kind == "follower":
                # `measured: true`, the board serial and the method tag must come from a
                # car-side calibration job, never from a document the browser composed.
                raise HybridSessionError(
                    "follower_calibration_requires_job_commit: use "
                    "/ros-mobile/hybrid/follower-calibration/commit")
            if self.session_id is not None:
                raise HybridSessionError("release_session_before_calibration_write")
            store = self.calibration_store
            if store is None:
                from .hybrid_session import adapter_unavailable

                raise adapter_unavailable("hybrid calibration store unavailable")
            identity = body.get("identity")
            if not isinstance(identity, str) or not identity.strip():
                raise HybridSessionError("calibration_identity_required")
            reasons = validate_asset_strict(kind, asset, now_ts=body.get("now_ts"))
            if reasons:
                return {"saved": False, "reasons": reasons, "action": "save", "kind": kind}
            record = store.save_validated(kind, identity, asset)
            return {"saved": True, "reasons": [], "action": "save", "kind": kind,
                    "record": record.as_dict()}
        raise HybridSessionError(f"unknown_calibration_action:{action}")

    def recording(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Recording subtask under the SAME lease. Phase changes emit no motion command."""
        if self._recording_controller is not None:
            return self._recording_controller.dispatch(action, payload)
        return super().recording(action, payload)

    def _status_recording(self) -> dict[str, Any]:
        return {"ok": True, "recording": self.recording_status(),
                "output_enabled": False, "simulated": self._is_simulated()}

    # ------------------------------------------------------------------ integration hooks
    def bind_driver(self, driver: Any) -> dict[str, Any]:
        """Inject a T1-interface adapter (capabilities/read_snapshot/submit/stop)."""
        if self.session_id is not None:
            raise HybridSessionError("release_session_before_binding_driver")
        self.driver = driver
        try:
            return dict(driver.capabilities())
        except Exception as exc:  # noqa: BLE001
            return {"driver_capabilities_error": type(exc).__name__}

    def trusted_feedback(self) -> dict[str, Any]:
        return dict(self._feedback_gate)

    def submit_intent(self, channel: str, target: dict[str, Any], *, sample_seq: int = 0,
                      ttl_ms: int = DEFAULT_TTL_MS, clutch: bool = True,
                      now_ns: int | None = None) -> dict[str, Any]:
        """Build and admit a protocol-v1 command. Requires a trusted readback; no output."""
        if self.router is None or self.state != "ready":
            raise HybridSessionError("session_not_ready")
        if not self._feedback_gate.get("trusted"):
            raise HybridSessionError("trusted_feedback_unavailable")
        if channel not in CHANNELS:
            raise HybridSessionError(f"unknown_channel:{channel}")
        self._intents[channel] += 1
        command = command_v1(
            session_id=self.session_id, session_epoch=self.session_epoch,
            channel=channel, source=CHANNEL_SOURCES[channel], seq=self._intents[channel],
            sample_seq=sample_seq, ttl_ms=ttl_ms,
            calibration_bundle_hash=self.bundle_hash, target=target, clutch=clutch)
        result = self.router.submit(command, now_ns=now_ns if now_ns is not None else self.clock_ns())
        return {**result, "command": command, "outputs_enabled": False,
                "controller_acknowledged": "unavailable", "execution_observed": False}

    def dispatch(self, channel: str, now_ns: int | None = None) -> dict[str, Any] | None:
        """Drain one admitted intent (Wave A: still no actuator output)."""
        if self.router is None:
            return None
        return self.router.dispatch(channel, now_ns=now_ns if now_ns is not None else self.clock_ns())


def build_public_facade(**kwargs) -> HybridPublicFacade:
    """Factory used by the coordinator; no adapters are installed by default."""
    return HybridPublicFacade(**kwargs)
