"""Hybrid public facade, ROS-free and actuator-free until adapters are integrated.

Lease adapters implement acquire(request), valid(token), renew(token), release(token, reason).
The car adapter additionally implements stop(reason). The observer supplies snapshot()
and calibration_readiness(); these must be trusted server-side providers, never HTTP input.
No adapter is installed by default. Methods return JSON-serializable dictionaries.
"""
from __future__ import annotations

import copy
import threading
import time
import uuid
from typing import Any

from .hybrid_calibration import KINDS, ROLES, validate_asset, validate_bundle
from .hybrid_gamepad import GamepadIntent


def adapter_unavailable(detail: str) -> Exception:
    """Build the bridge-layer "not wired yet" refusal, which answers HTTP 503.

    Status codes belong to the HTTP bridge (mobile_teleop_coordinator.BridgeError), so
    this module never imports it eagerly: doing so would drag serial/HTTP deps into the
    ROS-free protocol core. The import only happens on the unavailability path.
    """
    from .mobile_teleop_coordinator import BridgeError

    return BridgeError(503, detail)


def error_marker(prefix: str, exc: BaseException) -> str:
    """Render a stop/heartbeat failure so the cause survives the string.

    Type name alone was not enough to tell a refused token from a timed-out request
    after the fact, which is exactly the distinction a lost lease hinges on.
    """
    detail = " ".join(str(exc).split())[:160]
    return f"{prefix}:{type(exc).__name__}" + (f":{detail}" if detail else "")


def _lease_report(lease: Any, held: bool) -> dict[str, Any]:
    """Ask an adapter for its own expiry/reason, if it can say.

    Adapters are a documented plug-in boundary, so a bare fake that only implements
    valid() still reports the boolean it is able to.
    """
    report = {"held": held}
    state = getattr(lease, "lease_state", None)
    if callable(state):
        try:
            detail = state()
        except Exception:  # noqa: BLE001 - reporting must not break a status poll
            return report
        if isinstance(detail, dict):
            report.update({k: v for k, v in detail.items() if k != "held"})
    return report


class HybridSession:
    MAX_SESSION_EPOCH = (1 << 52) - 1
    OBSERVATION_RETRIES = 3
    OBSERVATION_RETRY_DELAY_S = 0.12
    DIAG_EVENTS = 24

    def __init__(self, *, upper=None, car=None, observer=None, calibration_store=None, clock_ns=time.monotonic_ns,
                 gamepad_deadzone=None, gamepad_timeout_ns=None):
        self.upper, self.car, self.observer, self.clock_ns = upper, car, observer, clock_ns
        self.calibration_store = calibration_store
        self.lock = threading.RLock()
        if (gamepad_deadzone is None) != (gamepad_timeout_ns is None):
            raise ValueError("gamepad deadzone and timeout must be supplied together")
        self.gamepad = (GamepadIntent(gamepad_deadzone, gamepad_timeout_ns)
                        if gamepad_deadzone is not None else None)
        self.session_id = self.session_epoch = self.bundle_hash = None
        self.upper_token = self.car_token = None
        self.state = "disconnected"
        self.arm_state = "idle"
        self.base_enabled = False
        self.phase = "grasp"
        self.last_error = None
        # Preserve the cause that first parked the session. Later stop/release
        # failures are operational results, not a replacement diagnosis.
        self._primary_error: str | None = None
        self._primary_fault_detail: dict | None = None
        self._last_stop: dict | None = None
        self.estop_latched = False
        self._estop_requested = False
        # Stage-by-stage record of the last stop and the last clearance, so a
        # reader can tell what the car confirmed from what this host requested.
        self._stop_stage: dict | None = None
        self._car_clear_last: dict | None = None
        self._estop_dispatch_lock = threading.Lock()
        self._lease_valid = {"upper": False, "car": False}
        self._facts: dict[str, Any] = {}
        self._readiness: dict[str, Any] = {"ready": False, "reasons": ["calibration_provider_unavailable"]}
        self._alignment = None
        # Lease and heartbeat history. A lost lease is only diagnosable after the fact
        # if the renew that should have prevented it is still on the record: when the
        # last one started, how long it took, what both TTLs said at the transition.
        self._diag_events: list[dict[str, Any]] = []
        self._diag = {"heartbeats": 0, "heartbeat_failures": 0,
                      "last_heartbeat_outcome": None,
                      "last_heartbeat_duration_s": None,
                      "operator_presence_age_s": None, "state": None}
        self._last_heartbeat_mono: float | None = None
        # The recording controller, when present, is the sole production source of
        # recording truth.  The small fallback below is retained only for direct
        # adapter-free core use and is never consulted while a controller is bound.
        self._recording_controller = None

    def _is_simulated(self) -> bool:
        context = getattr(self, "simulated_context", None)
        return isinstance(context, dict) and context.get("simulated") is True

    def _active_profile(self) -> str:
        return "simulated_hybrid_manual_v1" if self._is_simulated() else "hardware_hybrid_manual_v1"

    def bind_recording_controller(self, controller, *, replace: bool = False):
        """Bind the route recording controller as the single recording state source."""
        if controller is None or not callable(getattr(controller, "status", None)):
            raise ValueError("invalid_recording_controller")
        with self.lock:
            bound = self._recording_controller
            if bound is not None and bound is not controller and not replace:
                raise RuntimeError("recording_controller_already_bound")
            self._recording_controller = controller
        return controller.status()

    def recording_status(self) -> dict[str, Any]:
        """Return the one authoritative recording snapshot for this facade."""
        controller = self._recording_controller
        if controller is not None:
            status = controller.status()
            if not isinstance(status, dict):
                raise RuntimeError("recording_controller_status_invalid")
            return copy.deepcopy(status)
        return {"status": "not_started", "session_id": self.session_id,
                "session_epoch": self.session_epoch, "episode_id": None,
                "phases": [], "events": [], "motion_commands_sent": 0,
                "lease_acquired": False, "outputs_enabled": False}

    def validate_session_request(self, request: Any, *, allow_estop: bool = False) -> tuple[str, int]:
        """Require the exact current session identity, without coercing types.

        ``estop`` is the one explicit preemption exception.  A stale client must not
        release, control or write recording state into a newer session merely because
        it can still reach the HTTP route.
        """
        if not isinstance(request, dict):
            raise RuntimeError("session_identity_required")
        action = request.get("action")
        if allow_estop and action == "estop":
            return (self.session_id or "", self.session_epoch or 0)
        session_id = request.get("session_id")
        epoch = request.get("session_epoch")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("session_identity_required")
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            raise RuntimeError("session_epoch_invalid")
        if epoch < 0 or epoch > self.MAX_SESSION_EPOCH:
            raise RuntimeError("session_epoch_out_of_range")
        with self.lock:
            if self.session_id is None:
                raise RuntimeError("session_not_acquired")
            if session_id != self.session_id or epoch != self.session_epoch:
                raise RuntimeError("session_epoch_mismatch")
            return session_id, epoch

    def validate_session_request_if_active(self, request: Any) -> tuple[str, int] | None:
        """Require the current identity once a control session is active.

        Calibration validation/save is also used before a session exists.  In that
        state there is no current identity to compare; once a session is active,
        stale or missing calibration credentials are refused like other mutations.
        """
        with self.lock:
            if self.session_id is None:
                return None
            return self.validate_session_request(request)

    def _validate_optional_identity(self, request: Any) -> None:
        """Validate identity when supplied; route production calls always supply it."""
        if not isinstance(request, dict):
            return
        if "session_id" in request or "session_epoch" in request:
            self.validate_session_request(request)

    def calibration_capabilities(self):
        return {"schema_version": 1, "kinds": list(KINDS), "roles": dict(ROLES),
                "bundle_kind": "rig_bundle", "camera_capability": "unknown", "firmware_capability": "unknown"}

    def calibration_readiness(self):
        if self.observer is None:
            return {"ready": False, "reasons": ["calibration_provider_unavailable"]}
        try:
            result = self.observer.calibration_readiness()
            if not isinstance(result, dict) or result.get("ready") is not True or not isinstance(result.get("bundle_hash"), str) or not result["bundle_hash"]:
                return {"ready": False, "reasons": (result.get("reasons") or ["calibration_not_ready"]) if isinstance(result, dict) else ["invalid_readiness"]}
            return dict(result)
        except Exception as exc:
            return {"ready": False, "reasons": [f"calibration_provider_error:{type(exc).__name__}"]}

    def validate_calibration(self, kind, asset):
        reasons = validate_asset(kind, asset)
        return {"valid": not reasons, "reasons": reasons}

    def validate_calibration_bundle(self, bundle, assets):
        return validate_bundle(bundle, assets)

    def save_calibration(self, kind, identity, asset):
        with self.lock:
            if self.session_id is not None:
                raise RuntimeError("release_session_before_calibration_change")
            if self.calibration_store is None:
                raise RuntimeError("calibration_store_unavailable")
            return self.calibration_store.save_validated(kind, identity, asset).as_dict()

    def _diag_note(self, kind: str, **payload: Any) -> None:
        """Record one lease/state transition for later diagnosis; bounded ring."""
        entry = {"kind": kind, "mono_ns": self.clock_ns(), "state": self.state,
                 "arm_state": self.arm_state, "session_epoch": self.session_epoch,
                 **payload}
        self._diag_events.append(entry)
        if len(self._diag_events) > self.DIAG_EVENTS:
            del self._diag_events[:-self.DIAG_EVENTS]
        self._diag["state"] = self.state

    def _set_state(self, state: str, reason: str | None = None) -> None:
        """Change state with the reason on the record, because state alone lies."""
        if state == self.state:
            return
        previous = self.state
        self.state = state
        self._diag_note("state_change", previous=previous, reason=reason,
                        last_error=self.last_error)

    def _stop(self, reason):
        self.base_enabled = False
        if self.gamepad is not None:
            self.gamepad.reset(preserve_sequence=True)
        self.arm_state = "idle"
        self._alignment = None
        if self.car_token is not None and self.car is not None:
            started = time.monotonic_ns()
            try:
                result = self.car.stop(reason)
            except Exception as exc:
                marker = error_marker("stop_failed", exc)
                self._last_stop = {
                    "state": "failed",
                    "reason": str(reason),
                    "error": marker,
                    "started_mono_ns": started,
                    "finished_mono_ns": time.monotonic_ns(),
                }
                self._diag_note("stop_failed", reason=type(exc).__name__, detail=str(exc)[:200])
            else:
                self._last_stop = {
                    "state": "confirmed",
                    "reason": str(reason),
                    "result": copy.deepcopy(result),
                    "started_mono_ns": started,
                    "finished_mono_ns": time.monotonic_ns(),
                }
        else:
            self._last_stop = {
                "state": "not_requested",
                "reason": str(reason),
                "finished_mono_ns": time.monotonic_ns(),
            }

    def _lease_reason(self, upper_valid: bool, car_valid: bool) -> str:
        """Say which lease failed and why, rather than guessing from the last state."""
        parts = []
        for name, lease, held in (("upper", self.upper, upper_valid),
                                  ("car", self.car, car_valid)):
            if held:
                continue
            reason = getattr(lease, "last_reason", None)
            parts.append(f"{name}={reason or 'not_held'}")
        return ",".join(parts) or "lease_lost"

    def _refresh(self):
        if self.session_id is None:
            return
        if self._estop_requested:
            self._set_state("estop", "estop_requested")
            self.arm_state = "idle"
            self.base_enabled = False
            return
        try:
            ready = self.calibration_readiness()
            upper_valid = self.upper.valid(self.upper_token) is True
            car_valid = self.car.valid(self.car_token) is True
            self._lease_valid = {"upper": upper_valid, "car": car_valid}
            valid = upper_valid and car_valid
            required = ("leader_connected", "leader_healthy", "feedback_valid",
                        "exclusive_control")
            facts = None
            missing = required
            last_error = None
            for attempt in range(self.OBSERVATION_RETRIES):
                try:
                    candidate = self.observer.snapshot()
                except Exception as exc:  # noqa: BLE001 - bounded transient retry
                    last_error = exc
                else:
                    if isinstance(candidate, dict):
                        facts = candidate
                        missing = tuple(
                            key for key in required if candidate.get(key) is not True)
                        if not missing:
                            break
                    else:
                        last_error = ValueError("invalid_observer_snapshot")
                if attempt + 1 < self.OBSERVATION_RETRIES:
                    time.sleep(self.OBSERVATION_RETRY_DELAY_S)
            if facts is None:
                raise last_error or ValueError("invalid_observer_snapshot")
            self._facts, self._readiness = facts, ready
            if self._estop_requested:
                self._set_state("estop", "estop_requested")
                self.arm_state = "idle"
                self.base_enabled = False
                return
            reasons = []
            if not valid:
                reasons.append(f"lease_lost:{self._lease_reason(upper_valid, car_valid)}")
            if not ready.get("ready") or ready.get("bundle_hash") != self.bundle_hash:
                reasons.append("calibration_changed_or_unavailable")
            reasons.extend(key + "_missing" for key in missing)
            if reasons:
                primary = ", ".join(reasons)
                if self._primary_error is None:
                    self._primary_error = primary
                    self.last_error = primary
                elif self.last_error is None:
                    self.last_error = self._primary_error
                # Status polling must not replay a stop with an already expired
                # token. The first transition performs one bounded stop attempt;
                # later reads only report the preserved fault and stop result.
                if self.state not in ("fault", "estop"):
                    self._stop(primary)
                    self._set_state(
                        "estop" if self.estop_latched else "fault",
                        self._primary_error or primary)
        except Exception as exc:
            self._lease_valid = {"upper": False, "car": False}
            primary = f"state_check_failed:{type(exc).__name__}"
            if self._primary_error is None:
                self._primary_error = primary
                self.last_error = primary
            elif self.last_error is None:
                self.last_error = self._primary_error
            if self.state not in ("fault", "estop"):
                self._stop(primary)
                self._set_state(
                    "estop" if self.estop_latched else "fault",
                    self._primary_error or primary)

    def _diagnostics(self) -> dict[str, Any]:
        """The timing a lease failure has to be read against.

        ``lease_lost`` says what was observed, not what happened; these fields are what
        separates an operator who closed the page from a renewal that never landed.
        """
        out = dict(self._diag)
        out["state"] = self.state
        out["operator_presence_age_s"] = (
            round(time.monotonic() - self._last_heartbeat_mono, 3)
            if self._last_heartbeat_mono is not None else None)
        out["state_changes"] = list(self._diag_events)
        cache_state = getattr(self.observer, "cache_state", None)
        out["observation_cache"] = cache_state() if callable(cache_state) else None
        return out

    def status(self):
        recording_controller = self._recording_controller
        with self.lock:
            self._refresh()
            gp = (self.gamepad.snapshot(self.clock_ns()) if self.gamepad is not None else
                  {"armed": False, "neutral_seen": False, "intent": None,
                   "last_seq": -1, "last_received_mono_ns": None, "reason": "gamepad_unconfigured"})
            if not gp["armed"]:
                self.base_enabled = False
            following = self.arm_state == "following"
            missing = (["hardware_driver_not_integrated"]
                       if getattr(self, "driver", None) is None and not self._is_simulated()
                       else [])
            if self.state != "ready":
                missing.append(self.last_error or "session_not_ready")
            video = copy.deepcopy(self._facts.get(
                "video", {"available": False, "reason": "unknown"}))
            if isinstance(video, dict) and video.get("frame_seq") is None:
                preview_seq = video.get("preview_seq")
                if (isinstance(preview_seq, int) and not isinstance(preview_seq, bool)
                        and preview_seq >= 0):
                    # Explicit contract alias for the car's existing field.  This is
                    # copied from the upstream snapshot, never generated by the UI.
                    video["frame_seq"] = preview_seq
                else:
                    video["frame_seq"] = None
            leases = {"upper": _lease_report(self.upper, self._lease_valid["upper"]),
                      "car": _lease_report(self.car, self._lease_valid["car"])}
            diagnostics = self._diagnostics()
            out = copy.deepcopy({"state": self.state, "session_id": self.session_id, "session_epoch": self.session_epoch,
                    "profile": self._active_profile(), "simulated": self._is_simulated(),
                    "leases": leases,
                    "diagnostics": diagnostics,
                    "channels": {"base": {"enabled": False, "permitted": self.base_enabled, "locked": not self.base_enabled, "gamepad": gp},
                                 "arm": {"state": self.arm_state, "enabled": False, "permitted": following},
                                 "gripper": {"state": self.arm_state, "enabled": False, "permitted": following}},
                    "devices": self._facts.get("devices", {}), "calibrations": self._readiness,
                    "feedback": self._facts.get("feedback", {"valid": False, "reason": "unavailable"}),
                    "video": video,
                    "last_error": self.last_error,
                    "primary_error": self._primary_error or self.last_error,
                    "fault_detail": copy.deepcopy(self._primary_fault_detail),
                    "last_stop": copy.deepcopy(self._last_stop),
                    "missing_for_following": missing,
                    "outputs_enabled": False,
                    "control_semantics": ("simulated_intent_only" if self._is_simulated()
                                          else "offline_intent_only"),
                    "phase": self.phase})
        # Never call the recording controller while holding the facade lock.  The
        # controller may be reading facade.status() during its own transition, and
        # lock inversion would make an emergency path wait on a recording request.
        if recording_controller is not None:
            try:
                out["recording"] = self.recording_status()
            except Exception as exc:  # noqa: BLE001 - status must stay pollable
                out["recording"] = {"status": "unavailable",
                                    "reason": f"recording_status_failed:{type(exc).__name__}"}
        else:
            out["recording"] = self.recording_status()
        return out

    def acquire_session(self, request):
        with self.lock:
            if self.session_id is not None:
                raise ValueError("session_already_active")
            if self.estop_latched:
                raise RuntimeError("estop_latched")
            if not isinstance(request, dict) or not isinstance(request.get("owner"), str) or not request["owner"].strip():
                raise ValueError("owner_required")
            if self.upper is None or self.car is None or self.observer is None:
                raise RuntimeError("hybrid_adapters_unavailable")
            ready = self.calibration_readiness()
            if not ready.get("ready"):
                raise RuntimeError("calibration_not_ready:" + ", ".join(ready.get("reasons", [])))
            # Generate identities on the server before either adapter acquires, so
            # both endpoints bind exactly the same session and calibration bundle.
            session_id = uuid.uuid4().hex
            session_epoch = uuid.uuid4().int & ((1 << 52) - 1)
            context = {**request, "session_id": session_id, "session_epoch": session_epoch,
                       "calibration_bundle_hash": ready["bundle_hash"]}
            upper = car = None
            try:
                upper = self.upper.acquire(dict(context))
                if not upper:
                    raise RuntimeError("upper_lease_not_granted")
                car = self.car.acquire(dict(context))
                if not car:
                    raise RuntimeError("car_lease_not_granted")
                if self.upper.valid(upper) is not True or self.car.valid(car) is not True:
                    raise RuntimeError("lease_not_valid")
                required = ("leader_connected", "leader_healthy", "feedback_valid", "exclusive_control")
                facts = {}
                missing = required
                for attempt in range(4):
                    facts = self.observer.snapshot()
                    missing = required if not isinstance(facts, dict) else tuple(
                        key for key in required if facts.get(key) is not True)
                    if not missing:
                        break
                    if attempt < 3:
                        time.sleep(0.25)
                if missing:
                    raise RuntimeError("observed_prerequisites_missing:" + ",".join(missing))
                ready2 = self.calibration_readiness()
                if not ready2.get("ready") or ready2.get("bundle_hash") != ready["bundle_hash"]:
                    raise RuntimeError("calibration_changed_during_acquire")
            except BaseException:
                errors = []
                callbacks = []
                if car:
                    callbacks.extend([(self.car.stop, ("acquire_rollback",)),
                                      (self.car.release, (car, "acquire_rollback"))])
                if upper:
                    callbacks.append((self.upper.release, (upper, "acquire_rollback")))
                for callback, args in callbacks:
                    try:
                        callback(*args)
                    except Exception as exc:
                        errors.append(type(exc).__name__)
                if errors:
                    self.last_error = "acquire_rollback_failed:" + ",".join(errors)
                raise
            self.upper_token, self.car_token = upper, car
            # The epoch is an integer exactly representable in JavaScript, compatible
            # with the car supervisor. The session ID is independently random.
            self.session_id, self.session_epoch = session_id, session_epoch
            self.bundle_hash = ready["bundle_hash"]
            self._facts, self._readiness = facts, ready2
            self.arm_state, self.phase = "idle", "grasp"
            self.base_enabled, self._alignment = False, None
            if self.gamepad is not None:
                self.gamepad.reset()
            self.last_error = None
            self._primary_error = None
            self._primary_fault_detail = None
            self._last_stop = None
            self._set_state("ready", "session_acquired")
        return self.status()

    def heartbeat(self):
        """Renew both injected leases; any failure parks all logical channels."""
        started = time.monotonic()
        with self.lock:
            if self.session_id is None or self.state != "ready":
                self._heartbeat_record(started, "refused_not_ready")
                raise RuntimeError("session_not_ready")
            try:
                self.car.renew(self.car_token)
                self.upper.renew(self.upper_token)
                self._refresh()
                if self.state != "ready":
                    raise RuntimeError("lease_or_observation_lost")
            except Exception as exc:
                primary = error_marker("heartbeat_failed", exc)
                if self._primary_error is None:
                    self._primary_error = primary
                self.last_error = self._primary_error
                self._stop(primary)
                self._heartbeat_record(started, "failed")
                self._set_state(
                    "estop" if self.estop_latched else "fault",
                    self._primary_error)
                raise RuntimeError(self._primary_error) from exc
            self._heartbeat_record(started, "ok")
        return self.status()

    def _heartbeat_record(self, started: float, outcome: str) -> None:
        """Keep the renew timing that a later lease loss has to be read against."""
        now = time.monotonic()
        self._diag["heartbeats"] += 1
        if outcome != "ok":
            self._diag["heartbeat_failures"] += 1
        self._diag["last_heartbeat_outcome"] = outcome
        self._diag["last_heartbeat_duration_s"] = round(now - started, 3)
        self._last_heartbeat_mono = now

    def clear_estop_unbound(self, owner: str | None = None):
        """Clear a latched emergency stop only after the session has fully ended."""
        del owner  # Hardware subclasses may use the stable operator identity.
        with self.lock:
            if self.session_id is not None or self.upper_token is not None or self.car_token is not None:
                raise RuntimeError("session_identity_required")
            if self.estop_latched:
                self._stop("clear_unbound_estop")
                self.estop_latched = False
                self._estop_requested = False
                self.last_error = None
                self._primary_error = None
                self._primary_fault_detail = None
                self._last_stop = None
                self._set_state("disconnected", "clear_unbound_estop")
        return self.status()

    def release_session(self, reason, session_id=None, session_epoch=None):
        with self.lock:
            if session_id is not None or session_epoch is not None:
                self.validate_session_request({"session_id": session_id,
                                               "session_epoch": session_epoch})
            self._stop(str(reason or "released"))
            errors = []
            for adapter, token in ((self.car, self.car_token), (self.upper, self.upper_token)):
                if adapter is not None and token is not None:
                    try:
                        adapter.release(token, str(reason or "released"))
                    except Exception as exc:
                        errors.append(f"{type(exc).__name__}:{exc}")
            self.upper_token = self.car_token = None
            self._lease_valid = {"upper": False, "car": False}
            self.session_id = self.session_epoch = self.bundle_hash = None
            self._set_state("estop" if self.estop_latched else "disconnected",
                            str(reason or "released"))
            self._facts = {}
            self._readiness = {"ready": False, "reasons": ["session_not_acquired"]}
            if errors:
                self.last_error = "release_failed:" + ",".join(errors)
        return self.status()

    def set_arm_state(self, event):
        with self.lock:
            action = event.get("action") if isinstance(event, dict) else event
            if action != "estop":
                self._validate_optional_identity(event)
            if action == "estop":
                # Emergency stop deliberately skips the ordinary state and session
                # gates.  It must be able to preempt a stale, faulty or contended
                # client request; the route still records that no session matched.
                reason = str(event.get("reason") or "estop") if isinstance(event, dict) else "estop"
                self.emergency_stop(reason)
            else:
                self._refresh()
                if action in ("pause_all", "reconnect", "calibration_changed", "fault"):
                    self._stop(action)
                    self._set_state("estop" if self.estop_latched else
                                    ("fault" if action == "fault" else self.state), action)
                elif action == "clear_estop":
                    if not self.estop_latched:
                        raise ValueError("estop_not_latched")
                    self._stop(action)
                    car_clear = self._car_clear_estop()
                    self._car_clear_last = car_clear
                    if car_clear.get("car_clear") in ("failed", "unconfirmed",
                                                      "pending_or_failed"):
                        # No confirmation from the car: the stop stays latched and the
                        # operator sees why, instead of a cleared pill over a car that
                        # still holds the stop.
                        self.last_error = "estop_clear_" + str(car_clear.get("car_clear")) + \
                            (":" + str(car_clear.get("car_clear_error"))[:160]
                             if car_clear.get("car_clear_error") else "")
                        self._set_state("estop", "car_clear_unconfirmed")
                    else:
                        self.estop_latched = False
                        # _refresh() re-forces state "estop" from this flag, so a clear that
                        # left it set kept the page sitting in estop forever after the
                        # operator had been told the stop was cleared.
                        self._estop_requested = False
                        self._set_state("disconnected" if self.session_id is None else "fault",
                                        action)
                elif action == "release_clutch":
                    self.arm_state, self._alignment = "idle", None
                elif action == "lock_base":
                    self.base_enabled = False
                    if self.gamepad is not None:
                        self.gamepad.reset(preserve_sequence=True)
                elif self.state != "ready":
                    raise RuntimeError("session_not_ready")
                elif action == "preview":
                    self.arm_state = "preview"
                    self._alignment = None
                elif action == "align" and self.arm_state == "preview":
                    # Observer owns the measurement/finite/age/calibration validation.
                    reference = self._facts.get("alignment_reference")
                    if not isinstance(reference, dict) or self._facts.get("alignment_valid") is not True:
                        raise RuntimeError("alignment_reference_unavailable")
                    self._alignment = dict(reference)
                    self.arm_state = "aligned"
                elif action == "arm" and self.arm_state == "aligned":
                    self.arm_state = "armed"
                elif action == "engage" and self.arm_state == "armed":
                    self.arm_state = "following"
                elif action == "enable_base":
                    if self.phase == "grasp" or self.gamepad is None or not self.gamepad.snapshot(self.clock_ns())["armed"]:
                        raise RuntimeError("base_locked_or_deadman_not_armed")
                    self.base_enabled = True
                elif action == "set_phase" and isinstance(event, dict) and event.get("phase") in ("drive", "observe", "grasp", "transport", "release"):
                    self.phase = event["phase"]
                    if self.phase == "grasp":
                        self.base_enabled = False
                        if self.gamepad is not None:
                            self.gamepad.reset(preserve_sequence=True)
                else:
                    raise ValueError("invalid_transition")
        return self.status()

    def _car_clear_estop(self) -> dict:
        """Ask the car to lift its own stop. A facade without a car lease has nothing to ask.

        The frame transport overrides this; clearing here never grants motion, it only makes
        the car willing to accept a new session's explicit enable again.
        """
        return {"car_clear": "not_applicable"}

    def emergency_stop(self, reason="estop"):
        """Enter the stop path without waiting for recording/status serialization."""
        with self._estop_dispatch_lock:
            # Publish the logical stop first. Status polling can be blocked in slow
            # network I/O, so the emergency path must never wait for that lock.
            self._estop_requested = True
            self.estop_latched = True
            primary = str(reason or "estop")
            if self._primary_error is None:
                self._primary_error = primary
            self.last_error = self._primary_error
            self._set_state("estop", primary)
            self.arm_state = "idle"
            self.base_enabled = False
            self._alignment = None
            if self.gamepad is not None:
                self.gamepad.reset(preserve_sequence=True)
            car, token = self.car, self.car_token
            if car is not None and token is not None:
                try:
                    car.stop(primary)
                except Exception as exc:  # noqa: BLE001 - stop remains latched
                    if self._primary_error is None:
                        self._primary_error = primary
                    self.last_error = self._primary_error
                    self._diag_note("stop_failed", reason=type(exc).__name__,
                                    detail=str(exc)[:200])
            return {"estop": True, "latched": True, "state": "estop",
                    "outputs_enabled": False, "reason": str(reason or "estop")}

    def ingest_gamepad(self, sample):
        with self.lock:
            self._refresh()
            if self.state != "ready":
                raise RuntimeError("session_not_ready")
            if self.gamepad is None:
                raise RuntimeError("gamepad_unconfigured")
            try:
                result = self.gamepad.ingest(sample, self.clock_ns())
            except ValueError:
                self.base_enabled = False
                raise
            if not result["armed"]:
                self.base_enabled = False
            return result

    def preview(self, request=None):
        with self.lock:
            self._validate_optional_identity(request)
            self._refresh()
            if self.session_id is not None:
                if self.state != "ready":
                    raise RuntimeError("session_not_ready")
                self.arm_state = "preview"
                self._alignment = None
            return copy.deepcopy({"state": self.state, "arm_state": self.arm_state, "alignment": self._alignment,
                    "gamepad": (self.gamepad.snapshot(self.clock_ns()) if self.gamepad is not None else
                                {"armed": False, "intent": None, "reason": "gamepad_unconfigured"}), "outputs_enabled": False,
                    "motion_commands_sent": 0, "command_submitted": False,
                    "reason": ("preview_only_explicit_simulated_profile" if self._is_simulated()
                               else "preview_only_driver_not_connected")})

    def apply_recording_phase(self, phase: str, episode_id: str | None = None) -> None:
        """Project a recorder-owned phase into control state without recording again."""
        if phase not in ("drive", "observe", "grasp", "transport", "release"):
            raise ValueError(f"unknown_phase:{phase}")
        with self.lock:
            self.phase = phase
            if phase == "grasp":
                self.base_enabled = False
                if self.gamepad is not None:
                    self.gamepad.reset(preserve_sequence=True)
            router = getattr(self, "router", None)
            if phase == "grasp" and router is not None:
                router.revoke("base")

    # ------------------------------------------------------------------
    # T3 Wave A: route-facing methods. T4's /ros-mobile/hybrid/* glue imports
    # HybridSession directly, so these must live on the core class as well as on
    # HybridPublicFacade. They are pure bookkeeping: no actuator, no network, no
    # second hardware lease.
    # ------------------------------------------------------------------
    def capabilities(self):
        out = self.calibration_capabilities()
        simulated = self._is_simulated()
        profile = self._active_profile()
        out.update({
            "profile": profile,
            "profiles": [profile],
            "actions": (["preview", "align", "arm", "engage", "release_clutch", "pause_all",
                         "estop", "clear_estop", "enable_base", "lock_base",
                         "start_session", "start_episode", "set_phase", "end_episode",
                         "stop_session", "sync", "export"] if simulated else ["estop"]),
            "simulated": simulated,
            "available": False, "simulated_available": simulated,
            "outputs_enabled": False, "hardware_verified": False,
            "camera_capability": "unknown", "firmware_capability": "unknown",
            "firmware_watchdog_verified": False, "hardware_stop_effect": "unverified",
            "controller_acknowledged": "unavailable",
            "trusted_feedback_sources": ["servo_position_readback"],
            "rejected_feedback_sources": ["command_cache", "model_placeholder", "synthetic",
                                          "vendor_joint_states"],
            "channels": {"base": {"permitted": False, "source": "gamepad"},
                         "arm": {"permitted": False, "source": "so101_leader"},
                         "gripper": {"permitted": False, "source": "so101_leader"}},
            "reason": ("explicit_simulated_profile" if simulated
                       else "no_trusted_hardware_driver_bound"),
        })
        return out

    def calibration(self, action, kind, body):
        """Validate (or refuse to save) one versioned asset. Never probes a device."""
        from .hybrid_facade import validate_asset_strict

        if kind not in KINDS:
            raise ValueError(f"unknown_calibration_kind:{kind}")
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else body.get("asset")
        if action == "validate":
            reasons = validate_asset_strict(kind, payload, now_ts=body.get("now_ts"))
            return {"valid": not reasons, "reasons": reasons, "action": "validate",
                    "kind": kind, "camera_capability": "unknown", "firmware_capability": "unknown"}
        if action == "save":
            if self.session_id is not None:
                raise ValueError("release_session_before_calibration_change")
            if self.calibration_store is None:
                # Not a conflict: the versioned calibration store is simply not wired.
                raise adapter_unavailable("hybrid calibration store unavailable")
            reasons = validate_asset_strict(kind, payload, now_ts=body.get("now_ts"))
            if reasons:
                return {"saved": False, "reasons": reasons, "action": "save", "kind": kind}
            identity = body.get("identity")
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError("calibration_identity_required")
            return {"saved": True, "reasons": [],
                    "record": self.save_calibration(kind, identity, payload),
                    "action": "save", "kind": kind}
        raise ValueError(f"unknown_calibration_action:{action}")

    def recording(self, action, payload):
        """Recording is a subtask of the SAME lease; phase changes emit no motion."""
        if action not in ("start_session", "start_episode", "set_phase", "end_episode", "stop_session"):
            raise ValueError(f"unknown_recording_action:{action}")
        if self.session_id is None:
            # No hybrid session at all: the recorder is not wired, which is a
            # deployment gap (503), not a state conflict (409).
            if action == "start_session":
                raise adapter_unavailable("hybrid recording adapter unavailable: no hybrid session")
            raise ValueError("recording_session_not_started")
        if self._recording_controller is None:
            from .hybrid_recording import HybridRecordingController, InMemoryRecordingCarClient

            controller = HybridRecordingController(
                self, InMemoryRecordingCarClient(clock_ns=self.clock_ns),
                clock_ns=self.clock_ns, background=False)
            self.bind_recording_controller(controller)
        return self._recording_controller.dispatch(action, payload)
