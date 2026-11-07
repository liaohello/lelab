"""Car-side hybrid gateway (T3 Wave A), ROS-free and actuator-free by default.

This is the T1 adapter boundary, not the actuator driver.  It composes the shared
`HybridCommandGate` (protocol v1 admission + local deadline) with:

  * per-channel permits (base / arm / gripper), never a global mode switch;
  * explicit epoch rollover that retires the old gate and clears old queues;
  * a trusted-feedback gate (servo readback only; command caches are refused);
  * stop()/rollback() that clear queued intents BEFORE any driver/network wrap-up;
  * an injectable T1 driver: capabilities(), read_snapshot(), submit(command),
    stop(reason).  With driver=None no output path exists at all.

Python 3.10 / ROS Humble compatible.  Importing this module never imports rclpy.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from .contract import ContractError, HybridCommandGate, hybrid_feedback_gate

CHANNELS = ("base", "arm", "gripper")
CHANNEL_SOURCES = {"base": "gamepad", "arm": "so101_leader", "gripper": "so101_leader"}
TRUSTED_SOURCE = "servo_position_readback"
UNTRUSTED_SOURCES = ("command_cache", "model_placeholder", "synthetic", "vendor_joint_states")
CONFIRMATION_LEVELS = ("requested", "gateway_accepted", "driver_sent",
                       "controller_acknowledged", "execution_observed")


class HybridGatewayError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class HybridGateway:
    """One session owner, three channels, one deadline-aware queue, zero outputs."""

    def __init__(self, profile: dict[str, Any] | None = None, *,
                 session_id: str = "unbound", session_epoch: int = 0,
                 bundle_hash: str = "unbound", max_ttl_ms: int = 200,
                 clock_ns=time.monotonic_ns, driver: Any | None = None) -> None:
        if type(session_epoch) is not int or not 0 <= session_epoch < 2 ** 53:
            raise HybridGatewayError("invalid_epoch")
        self.profile = dict(profile or {})
        self.session_id, self.epoch, self.bundle_hash = session_id, session_epoch, bundle_hash
        self.max_ttl_ms, self.clock_ns, self.driver = max_ttl_ms, clock_ns, driver
        self.lock = threading.RLock()
        self.outputs_enabled = False
        self.permits: dict[str, bool] = {name: False for name in CHANNELS}
        self.last_error: str | None = None
        self.events: list[dict[str, Any]] = []
        self._gate = self._new_gate()

    def _new_gate(self) -> HybridCommandGate:
        return HybridCommandGate(self.session_id, self.epoch, self.bundle_hash,
                                 self.max_ttl_ms, self.clock_ns)

    def _audit(self, event: str, detail: str = "") -> None:
        self.events.append({"event": event, "detail": detail, "epoch": self.epoch})

    # ------------------------------------------------------------------ capabilities
    def capabilities(self) -> dict[str, Any]:
        declared: dict[str, Any] = {}
        if self.driver is not None:
            try:
                result = self.driver.capabilities()
                if isinstance(result, dict):
                    declared = result
            except Exception as exc:  # noqa: BLE001
                declared = {"driver_capabilities_error": type(exc).__name__}
        return {
            "schema_version": 1,
            "profile": self.profile.get("profile") or "hardware_hybrid_manual_v1",
            "available": False, "outputs_enabled": self.outputs_enabled,
            "hardware_verified": False, "actuator_outputs": False,
            "channels": {name: {"permitted": self.permits[name],
                                "source": CHANNEL_SOURCES[name]} for name in CHANNELS},
            "trusted_feedback_source": TRUSTED_SOURCE,
            "rejected_feedback_sources": list(UNTRUSTED_SOURCES),
            "camera_capability": "unknown", "firmware_capability": "unknown",
            "firmware_watchdog_verified": False,
            "hardware_stop_effect": "unverified",
            "controller_acknowledged": "unavailable",
            "confirmation_levels": list(CONFIRMATION_LEVELS),
            "driver_declared": declared,
            "reason": ("trusted_output_driver_bound" if self.driver is not None
                       and declared.get("output_capable") is True
                       else "no_trusted_t1_driver_bound; legacy /control/* outputs refused"),
        }

    # ------------------------------------------------------------------ epoch / permits
    def begin_epoch(self, epoch: int, bundle_hash: str | None = None) -> dict[str, Any]:
        """New session, reconnect or calibration change: retire the old gate and queue."""
        with self.lock:
            if type(epoch) is not int or not 0 <= epoch < 2 ** 53:
                raise HybridGatewayError("invalid_epoch")
            if bundle_hash is not None and (not isinstance(bundle_hash, str) or not bundle_hash):
                raise HybridGatewayError("invalid_bundle_hash")
            dropped = len(self._gate.pending)
            self._gate.clear()
            self._gate.retired = True
            self.epoch = epoch
            if bundle_hash is not None:
                self.bundle_hash = bundle_hash
            self._gate = self._new_gate()
            self.permits = {name: False for name in CHANNELS}
            self.outputs_enabled = False
            self._audit("begin_epoch", f"queue_dropped={dropped}")
            return {"epoch": epoch, "queue_dropped": dropped,
                    "seq_slots_dropped": "retired_with_gate", "outputs_enabled": False}

    def grant(self, *channels: str) -> dict[str, Any]:
        with self.lock:
            for channel in channels:
                if channel not in CHANNELS:
                    raise HybridGatewayError("bad_channel", channel)
                self.permits[channel] = True
            self._audit("grant", ",".join(channels))
            return {"granted": sorted(name for name in CHANNELS if self.permits[name])}

    def revoke(self, *channels: str) -> dict[str, Any]:
        with self.lock:
            for channel in channels or CHANNELS:
                if channel in self.permits:
                    self.permits[channel] = False
            self._audit("revoke", ",".join(channels or CHANNELS))
            return {"granted": sorted(name for name in CHANNELS if self.permits[name])}

    def enable_outputs(self, feedback: dict[str, Any]) -> dict[str, Any]:
        """Explicit two-stage arm/engage gate; feedback and driver are rechecked here."""
        with self.lock:
            if self.driver is None:
                raise HybridGatewayError("output_driver_unavailable")
            capabilities = self.driver.capabilities()
            if capabilities.get("output_capable") is not True:
                raise HybridGatewayError("output_driver_not_capable")
            if feedback.get("valid") is not True or feedback.get("trusted") is not True:
                raise HybridGatewayError("trusted_feedback_unavailable")
            enable = getattr(self.driver, "enable", None)
            if not callable(enable):
                raise HybridGatewayError("output_driver_enable_unavailable")
            enable()
            self.outputs_enabled = True
            self._audit("outputs_enabled")
            return {"outputs_enabled": True, "output_capable": True,
                    "controller_acknowledged": "unavailable",
                    "execution_observed": "unavailable"}

    @property
    def permitted(self) -> set[str]:
        with self.lock:
            return {name for name in CHANNELS if self.permits[name]}

    # ------------------------------------------------------------------ commands
    def submit(self, command: dict[str, Any]) -> dict[str, Any]:
        """Admit one v1 command; a rejected frame consumes no sequence number."""
        with self.lock:
            try:
                accepted = self._gate.receive(command, self.permitted)
            except ContractError as exc:
                self.last_error = f"rejected:{exc.reason}"
                self._audit("rejected", exc.reason)
                raise HybridGatewayError(exc.reason, exc.detail) from exc
            self._audit("accepted", f"{accepted['channel']} seq={accepted['seq']}")
            return {"accepted": True, "confirmation": "gateway_accepted",
                    "channel": accepted["channel"], "seq": accepted["seq"],
                    "deadline_mono_ns": accepted["deadline_mono_ns"],
                    "outputs_enabled": self.outputs_enabled,
                    "controller_acknowledged": "unavailable", "execution_observed": False}

    def dispatch(self, channel: str) -> dict[str, Any] | None:
        """Drain one admitted intent. Wave A never calls driver.submit()."""
        with self.lock:
            command = self._gate.pop(channel, self.permitted)
            if command is None:
                return None
            if not self.outputs_enabled or self.driver is None:
                return {"command": command, "confirmation": "gateway_accepted",
                        "driver_sent": False, "controller_acknowledged": "unavailable",
                        "execution_observed": False, "outputs_enabled": False,
                        "reason": "no_output_wave_a"}
            try:
                self.driver.submit(command)
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
                    "outputs_enabled": True}

    def tick(self) -> dict[str, Any]:
        """Mirror driver watchdog state back into the gateway without emitting a command."""
        with self.lock:
            if not self.outputs_enabled or self.driver is None:
                return {"outputs_enabled": self.outputs_enabled, "changed": False}
            capabilities = self.driver.capabilities()
            if capabilities.get("outputs_enabled") is True:
                return {"outputs_enabled": True, "changed": False}
            self.outputs_enabled = False
            self.permits = {name: False for name in CHANNELS}
            self.last_error = "output_driver_watchdog"
            self._gate.clear()
            self._audit("output_watchdog")
            return {"outputs_enabled": False, "changed": True,
                    "reason": self.last_error}

    def forward(self, command: dict[str, Any]) -> dict[str, Any]:
        """Forward only remaining TTL; never reset the original local deadline."""
        try:
            return self._gate.forward(command)
        except ContractError as exc:
            raise HybridGatewayError(exc.reason, exc.detail) from exc

    # ------------------------------------------------------------------ feedback
    def read_feedback(self, samples: Any, *, now_ns: int | None = None,
                      max_age_ns: int = 150_000_000,
                      max_scan_span_ns: int | None = None) -> dict[str, Any]:
        """Only a T1 servo readback may be called measured. Never repairs a bad sample."""
        if isinstance(samples, dict):
            declared = samples.get("source_kind")
            if declared in UNTRUSTED_SOURCES:
                return {"valid": False, "trusted": False,
                        "reasons": [f"untrusted_source:{declared}"], "source_kind": None}
            snapshot = samples
            samples = snapshot.get("samples")
            if samples is None and isinstance(snapshot.get("joint_samples"), dict):
                samples = list(snapshot["joint_samples"].values())
        result = hybrid_feedback_gate(samples, self.bundle_hash,
                                      self.clock_ns() if now_ns is None else now_ns,
                                      max_age_ns, max_scan_span_ns)
        result["trusted"] = bool(result.get("valid"))
        result["note"] = "vendor /controller_manager/joint_states is a command cache, never measured"
        return result

    # ------------------------------------------------------------------ stop / rollback
    def stop(self, reason: str) -> dict[str, Any]:
        """Stop first: clear intents and permits, then ask the driver to stop."""
        with self.lock:
            dropped = len(self._gate.pending)
            self._gate.clear()
            self.permits = {name: False for name in CHANNELS}
            self.outputs_enabled = False
            driver_stopped = False
            driver_stop_error: str | None = None
            if self.driver is not None:
                try:
                    self.driver.stop(str(reason))
                    driver_stopped = True
                except Exception as exc:  # noqa: BLE001
                    driver_stop_error = f"driver_stop_failed:{type(exc).__name__}"
                    # Never mask the rollback/refusal cause that brought us here.
                    self.last_error = self.last_error or driver_stop_error
            self._audit("stop", f"reason={reason},queue_dropped={dropped}")
            return {"stopped": True, "reason": str(reason), "queue_dropped": dropped,
                    "outputs_enabled": False, "driver_stopped": driver_stopped,
                    "driver_stop_error": driver_stop_error,
                    "hardware_effect": "unverified", "last_error": self.last_error}

    def rollback(self, reason: str) -> dict[str, Any]:
        with self.lock:
            self.last_error = str(reason)
            result = self.stop(reason)
            self._gate.retired = True
            self._gate = self._new_gate()
            self._audit("rollback", reason)
            return {**result, "rolled_back": True}

    def status(self, *, now_ns: int | None = None) -> dict[str, Any]:
        with self.lock:
            now = self.clock_ns() if now_ns is None else now_ns
            return {
                "session_id": self.session_id, "session_epoch": self.epoch,
                "calibration_bundle_hash": self.bundle_hash,
                "profile": self.profile.get("profile") or "hardware_hybrid_manual_v1",
                "available": False, "outputs_enabled": self.outputs_enabled,
                "permits": dict(self.permits),
                "pending": {channel: {"seq": command["seq"],
                                      "remaining_ms": max(0, (command["deadline_mono_ns"] - now) // 1_000_000)}
                            for channel, command in self._gate.pending.items()},
                "last_seq": {"|".join(key): value for key, value in self._gate.last_seq.items()},
                "retired": self._gate.retired,
                "feedback": {"trusted": False, "source_kind": None,
                             "reason": "no_trusted_readback_verified"},
                "capabilities": {"camera_capability": "unknown", "firmware_capability": "unknown",
                                 "firmware_watchdog_verified": False,
                                 "hardware_stop_effect": "unverified"},
                "last_error": self.last_error,
                "confirmation_levels": list(CONFIRMATION_LEVELS),
            }
