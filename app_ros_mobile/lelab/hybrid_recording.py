"""Hybrid manual recording controller: one lease, one episode, explicit phases.

This is the upper-host state machine behind ``/ros-mobile/hybrid/recording``.  It
sits between T3's ROS-free ``HybridSession`` facade (logical permission and
lease) and the car's ``SessionRecorder`` (the append-only timeline that actually
holds the sqlite3 bag), and it owns the rules that neither side can enforce
alone:

* Recording is a **subtask of the same control lease** -- it never acquires a
  second lease and never publishes a motion command.
* A phase change appends a manual boundary on the car clock and changes no
  motion state; it never opens an episode implicitly.
* ``end_episode`` / ``stop_session`` pause all motion *before* any wrap-up work.
  If pausing fails the episode is closed as discarded instead of being saved.
* An emergency stop returns immediately; saving continues in the background and
  is never on the critical path.

The car client is injected.  Production uses :class:`HttpCarClient` over the
existing agent HTTP bridge; tests inject a fake that drives a real
``SessionRecorder``.  Nothing here pretends the car accepted a request it did
not: a missing car route surfaces as :class:`CarRouteUnavailable` and marks the
episode's phase timeline incomplete, which blocks export.
"""
from __future__ import annotations

import copy
import threading
import time
import uuid
from typing import Any, Callable, Protocol

PHASES: tuple[str, ...] = ("drive", "observe", "grasp", "transport", "release")
MANUAL_RESULTS: tuple[str, ...] = ("success", "failure", "unknown")
RECORDING_ACTIONS: tuple[str, ...] = (
    "start_session", "start_episode", "set_phase", "end_episode", "stop_session",
)

# States.  ``invalid`` keeps the evidence of a failed run; it is never silently
# reset to ``not_started`` and its session is never auto-deleted.
NOT_STARTED = "not_started"
ACTIVE = "active"
STOPPED = "stopped"
INVALID = "invalid"

CAR_PHASE_ROUTE = "/recording/set_phase"


class RecordingError(RuntimeError):
    """A refusal with a stable reason token and an HTTP status for the router."""

    def __init__(self, reason: str, detail: str = "", *, status_code: int = 409):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason
        self.status_code = status_code

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail != self.reason else self.reason

    def as_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "detail": self.detail}


class CarRouteUnavailable(RecordingError):
    """The car agent does not expose the route this action needs."""

    def __init__(self, path: str, detail: str = ""):
        super().__init__(
            "car_route_unavailable",
            detail or f"car agent has no {path}; the car timeline cannot be written",
            status_code=503,
        )
        self.path = path


class CarClient(Protocol):
    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]: ...


class HttpCarClient:
    """Thin adapter over the existing agent bridge ``_agent_post`` helper."""

    def __init__(self, post: Callable[[str, dict[str, Any]], dict[str, Any]]):
        self._post = post

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        from .ros_mobile import HTTPException  # local import: avoid a cycle at module load

        try:
            out = self._post(path, body)
        except HTTPException as exc:
            if exc.status_code in (404, 405):
                raise CarRouteUnavailable(path, str(exc.detail)) from exc
            raise RecordingError("car_rejected", f"{path}: {exc.detail}",
                                 status_code=502) from exc
        if not isinstance(out, dict):
            raise RecordingError("car_bad_response", f"{path} returned non-object",
                                 status_code=502)
        if out.get("error"):
            raise RecordingError(str(out["error"]), str(out.get("detail") or ""),
                                 status_code=502)
        return out


class InMemoryRecordingCarClient:
    """Offline recording transport for direct core use and explicit simulation."""

    def __init__(self, *, clock_ns: Callable[[], int] = time.monotonic_ns,
                 simulated: bool = False):
        self.clock_ns = clock_ns
        self.simulated = bool(simulated)
        self.transport = "in_memory"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._last_time_ns = 0

    def _time(self) -> int:
        self._last_time_ns = max(int(self.clock_ns()), self._last_time_ns + 1_000_000)
        return self._last_time_ns

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        body = dict(body)
        self.calls.append((path, body))
        common = {"simulated": self.simulated, "transport": "in_memory",
                  "session_id": body.get("session_id"),
                  "session_epoch": body.get("session_epoch")}
        if path == "/recording/start_session":
            return {**common, "session_id": body.get("session_id"),
                    "path": f"memory://{body.get('session_id')}",
                    "clock_domain": "upper_monotonic_ns", "topics": []}
        if path == "/recording/start_episode":
            now = self._time()
            return {**common, "episode_id": body.get("episode_id"),
                    "initial": {"time_ns": now, "clock_domain": "upper_monotonic_ns"}}
        if path == "/recording/set_phase":
            now = self._time()
            return {**common, "kind": "phase_started", "time_ns": now,
                    "clock_domain": "upper_monotonic_ns",
                    "payload": {"phase": body.get("phase")}}
        if path == "/recording/end_episode":
            now = self._time()
            return {**common, "episode_id": body.get("episode_id"),
                    "event": {"time_ns": now, "clock_domain": "upper_monotonic_ns"},
                    "evaluation": {"success": body.get("success", "unknown")}}
        if path == "/recording/stop_session":
            return {**common, "stopped": True, "time_ns": self._time()}
        raise CarRouteUnavailable(path)


class HybridRecordingController:
    """Episode/phase state machine for one hybrid manual session."""

    def __init__(self, facade: Any, car: CarClient, *,
                 clock_ns: Callable[[], int] = time.monotonic_ns,
                 wall_ns: Callable[[], int] = time.time_ns,
                 background: bool = True, offline_6d: bool = False):
        self.facade = facade
        self.car = car
        self.clock_ns = clock_ns
        self.wall_ns = wall_ns
        self.background = background
        self.offline_6d = bool(offline_6d)
        self.lock = threading.RLock()
        self._estop_worker_lock = threading.Lock()
        self._estop_worker_started = False
        self._reset()

    # -- state -------------------------------------------------------------
    def _reset(self) -> None:
        self.status_value = NOT_STARTED
        self.session_id: str | None = None
        self.session_epoch: int | None = None
        self.clock_domain: str | None = None
        self.clock_epoch: str | None = None
        self.bundle_hash: str | None = None
        self.car_session_path: str | None = None
        self.car_recording_id: str | None = None
        self.episode_id: str | None = None
        self.episode_started_ns: int | None = None
        self.phases: list[dict[str, Any]] = []
        self.phase: str | None = None
        self.motion_commands_sent = 0
        self.phase_timeline_complete = True
        self.last_error: str | None = None
        self.error_detail: str | None = None
        self.paused_before_wrapup: bool | None = None
        self.events: list[dict[str, Any]] = []
        self.episodes: list[dict[str, Any]] = []
        self._pending_estop_save: threading.Thread | None = None

    def _fail(self, reason: str, detail: str = "", *, invalid: bool = True) -> None:
        if self.last_error is None:
            self.last_error = reason
            self.error_detail = detail
        self.phase_timeline_complete = False
        if invalid:
            self.status_value = INVALID

    def _note(self, kind: str, **payload: Any) -> dict[str, Any]:
        entry = {"kind": kind, "time_ns": self.wall_ns(), "monotonic_ns": self.clock_ns(),
                 "session_id": self.session_id, "session_epoch": self.session_epoch,
                 "clock_domain": self.clock_domain, "clock_epoch": self.clock_epoch,
                 "episode_id": self.episode_id, **payload}
        self.events.append(entry)
        return entry

    def _facade_status(self) -> dict[str, Any]:
        status = self.facade.status()
        if not isinstance(status, dict):
            raise RecordingError("facade_status_invalid", "facade.status() is not an object")
        return status

    def _require_lease(self, status: dict[str, Any]) -> tuple[str, int, str | None]:
        session_id = status.get("session_id")
        if status.get("state") != "ready" or not session_id:
            raise RecordingError("session_not_ready",
                                 f"hybrid session state={status.get('state')!r}; "
                                 "acquire the lease before recording")
        epoch = status.get("session_epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            raise RecordingError("session_epoch_missing", "facade reported no integer session_epoch")
        bundle = status.get("calibration_bundle_hash")
        calibrations = status.get("calibrations")
        if isinstance(calibrations, dict):
            bundle = bundle or calibrations.get("bundle_hash")
        return str(session_id), epoch, bundle

    @staticmethod
    def _payload_identity(payload: dict[str, Any]) -> tuple[str, int] | None:
        has_id = "session_id" in payload
        has_epoch = "session_epoch" in payload
        if not has_id and not has_epoch:
            return None
        session_id, epoch = payload.get("session_id"), payload.get("session_epoch")
        if not isinstance(session_id, str) or not session_id:
            raise RecordingError("session_identity_required", status_code=409)
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            raise RecordingError("session_epoch_invalid", status_code=409)
        if epoch < 0 or epoch > (1 << 52) - 1:
            raise RecordingError("session_epoch_out_of_range", status_code=409)
        return session_id, epoch

    def _assert_payload_identity(self, payload: dict[str, Any],
                                 status: dict[str, Any]) -> tuple[str, int]:
        requested = self._payload_identity(payload)
        current_id, current_epoch, _ = self._require_lease(status)
        if requested is not None and requested != (current_id, current_epoch):
            raise RecordingError(
                "session_epoch_mismatch",
                f"request belongs to {requested[0]}/{requested[1]}, current is "
                f"{current_id}/{current_epoch}",
                status_code=409,
            )
        return current_id, current_epoch

    def _assert_same_control_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Refuse old recording work after the facade has moved to a new epoch."""
        status = self._facade_status()
        current_id, current_epoch = self._assert_payload_identity(payload, status)
        if self.session_id is not None and (
                self.session_id != current_id or self.session_epoch != current_epoch):
            self._fail("control_session_changed",
                       f"recording belongs to {self.session_id}/{self.session_epoch}; "
                       f"facade now has {current_id}/{current_epoch}")
            raise RecordingError(
                "control_session_changed",
                f"recording belongs to {self.session_id}/{self.session_epoch}; "
                f"facade now has {current_id}/{current_epoch}",
                status_code=409,
            )
        return status

    # -- actions -----------------------------------------------------------
    def dispatch(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if action not in RECORDING_ACTIONS:
            raise RecordingError("unknown_recording_action", str(action), status_code=400)
        payload = dict(payload or {})
        with self.lock:
            # Validate the request identity and the facade's current epoch before
            # any local state transition or car request.  A stale release, phase
            # change or episode close therefore cannot mutate a newer session.
            if action != "start_session":
                self._assert_same_control_session(payload)
            else:
                status = self._facade_status()
                self._assert_payload_identity(payload, status)
                current = self._require_lease(status)
                if self.session_id is not None and (
                        self.session_id != current[0] or self.session_epoch != current[1]):
                    # A previous recording belongs to an older control epoch.  It
                    # remains on the car for audit, but this controller starts a new
                    # local session instead of reusing the old state.
                    self._reset()
            if self.status_value == INVALID and action in ("start_episode", "set_phase"):
                # A failed run keeps its evidence; only an explicit new session clears it.
                raise RecordingError("recording_invalid",
                                     f"recording is invalid: {self.last_error}")
            handler = getattr(self, action)
            return handler(payload)

    def start_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.status_value == ACTIVE:
                raise RecordingError("recording_session_already_started")
            status = self._facade_status()
            session_id, epoch, bundle = self._require_lease(status)
            self._assert_payload_identity(payload, status)
            if (self.session_id is not None and
                    (self.session_id != session_id or self.session_epoch != epoch)):
                self._reset()
            elif self.status_value == STOPPED:
                self._reset()
            if self.status_value == INVALID:
                raise RecordingError("recording_invalid",
                                     f"previous session is invalid: {self.last_error}; "
                                     "start a new control session to clear it")
            profile = str(payload.get("profile") or
                          ("hardware_hybrid_manual_v1_offline6d" if self.offline_6d else None) or status.get("profile")
                          or "hardware_hybrid_manual_v1")
            if self.offline_6d and (status.get("simulated") is not True or
                                    profile != "hardware_hybrid_manual_v1_offline6d"):
                raise RecordingError("offline_6d_identity_mismatch")
            runtime_mode = ("simulated_hybrid_manual" if status.get("simulated") is True
                            else "hardware_hybrid_manual")
            body = {
                "task": str(payload.get("task") or ""),
                "source": "manual_hybrid",
                "export_schema": ("arm_gripper_6dof_phase1" if self.offline_6d
                                  else "mobile_hybrid_manual_9d_v1"),
                "runtime_mode": runtime_mode,
                "profile": profile,
                "session_id": session_id,
                "session_epoch": epoch,
                "calibration_bundle_hash": bundle,
            }
            out = self.car.post("/recording/start_session", body)
            self.session_id = session_id
            self.session_epoch = epoch
            self.bundle_hash = bundle
            self.clock_domain = str(out.get("clock_domain") or payload.get("clock_domain") or "")
            if not self.clock_domain:
                # Without an authoritative clock domain a phase boundary cannot be
                # placed on the timeline, so refuse instead of inventing one.
                self._fail("clock_domain_missing", "car start_session returned no clock_domain")
                raise RecordingError("clock_domain_missing",
                                     "car start_session returned no clock_domain")
            self.clock_epoch = uuid.uuid4().hex
            self.car_session_path = out.get("path")
            self.car_recording_id = str(out.get("session_id") or "") or None
            self.status_value = ACTIVE
            self.last_error = None
            self.error_detail = None
            self.phase_timeline_complete = True
            self._note("recording_session_started", car_path=self.car_session_path,
                       topics=out.get("topics"))
            return self.status()

    def start_episode(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.status_value != ACTIVE:
                raise RecordingError("recording_session_not_started")
            if self.episode_id is not None:
                raise RecordingError("episode_already_open", str(self.episode_id))
            episode_id = str(payload.get("episode_id") or f"ep_{uuid.uuid4().hex[:12]}")
            # Quiesce before the car opens the window.  A frame emitted after the episode
            # start stamp but before the control process mirrors the new episode is
            # recorded without an agent context, and the exporter rightly refuses such a
            # row; the operator resumes through the phase switch, which confirms the
            # context before any output is allowed again.
            prepare = getattr(self.facade, "prepare_recording_phase", None)
            if callable(prepare):
                try:
                    prepare()
                except Exception as exc:
                    self._fail("episode_stop_unconfirmed", str(exc))
                    raise RecordingError("episode_stop_unconfirmed", str(exc)) from exc
            out = self.car.post("/recording/start_episode", {
                "episode_id": episode_id,
                "source": "manual_hybrid",
                "task": str(payload.get("task") or ""),
                "session_id": self.session_id,
                "session_epoch": self.session_epoch,
                "calibration_bundle_hash": self.bundle_hash,
            })
            initial = out.get("initial") if isinstance(out.get("initial"), dict) else {}
            car_time = initial.get("time_ns")
            if isinstance(car_time, bool) or not isinstance(car_time, int):
                self._fail("car_episode_time_missing",
                           "car start_episode returned no initial.time_ns")
                raise RecordingError("car_episode_time_missing",
                                     "car start_episode returned no initial.time_ns")
            car_clock = initial.get("clock_domain")
            if car_clock and self.clock_domain and car_clock != self.clock_domain:
                self._fail("clock_domain_changed",
                           f"episode clock {car_clock!r} != session clock {self.clock_domain!r}")
                raise RecordingError("clock_domain_changed", "car clock domain changed mid-session")
            self.episode_id = str(out.get("episode_id") or episode_id)
            self.episode_started_ns = int(car_time)
            self.phases = []
            self.phase = None
            self.paused_before_wrapup = None
            self._note("episode_started", car_time_ns=car_time, episode_id=self.episode_id)
            return self.status()

    def set_phase(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.status_value != ACTIVE or self.episode_id is None:
                raise RecordingError("episode_not_open")
            phase = payload.get("phase")
            if phase not in PHASES:
                raise RecordingError("unknown_phase", str(phase), status_code=400)
            if self.phase == phase:
                raise RecordingError("phase_already_active", str(phase))
            prepare = getattr(self.facade, "prepare_recording_phase", None)
            if callable(prepare):
                try:
                    prepare()
                except Exception as exc:
                    self._fail("phase_stop_unconfirmed", str(exc))
                    raise RecordingError("phase_stop_unconfirmed", str(exc)) from exc
            # Logical bookkeeping only.  The facade may lock the chassis on grasp;
            # it never publishes a command, and an unlock never happens implicitly.
            apply_phase = getattr(self.facade, "apply_recording_phase", None)
            if callable(apply_phase):
                apply_phase(phase, self.episode_id)
            try:
                out = self.car.post(CAR_PHASE_ROUTE, {
                    "phase": phase,
                    "episode_id": self.episode_id,
                    "clock_epoch": self.clock_epoch,
                    "session_id": self.session_id,
                    "session_epoch": self.session_epoch,
                    "calibration_bundle_hash": self.bundle_hash,
                })
            except CarRouteUnavailable:
                # Keep the local intent but mark the car timeline incomplete so the
                # episode can never be exported as a phased episode.
                self._fail("phase_timeline_incomplete",
                           f"car agent does not route {CAR_PHASE_ROUTE}")
                self.phases.append({"phase": phase, "started_time_ns": None,
                                    "ended_time_ns": None, "car_recorded": False})
                self.phase = phase
                raise
            car_time = _extract_time(out)
            if car_time is None:
                self._fail("phase_time_missing", "car set_phase returned no time_ns")
                raise RecordingError("phase_time_missing",
                                     "car set_phase returned no time_ns")
            if car_time < (self.episode_started_ns or 0):
                self._fail("phase_time_precedes_episode", f"{car_time}")
                raise RecordingError("phase_time_precedes_episode")
            confirm = getattr(self.facade, "confirm_recording_phase", None)
            if callable(confirm):
                try:
                    confirm(recording_id=self.car_recording_id, episode_id=self.episode_id,
                            phase=phase, clock_epoch=self.clock_epoch)
                except Exception as exc:
                    self._fail("phase_context_unconfirmed", str(exc))
                    raise RecordingError("phase_context_unconfirmed", str(exc)) from exc
            if self.phases:
                previous = self.phases[-1]
                if car_time <= (previous.get("started_time_ns") or 0):
                    raise RecordingError("phase_time_not_increasing")
                previous["ended_time_ns"] = car_time
            self.phases.append({"phase": phase, "started_time_ns": car_time,
                                "ended_time_ns": None, "car_recorded": True})
            self.phase = phase
            self._note("phase_started", phase=phase, car_time_ns=car_time)
            return self.status()

    def end_episode(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.episode_id is None:
                raise RecordingError("no_open_episode")
            paused, pause_error = self._pause_all()
            self.paused_before_wrapup = paused
            label = _manual_label(payload.get("success", payload.get("result")))
            body: dict[str, Any] = {
                "episode_id": self.episode_id,
                "reason": str(payload.get("reason") or "manual_end"),
                "note": str(payload.get("note") or ""),
                "session_id": self.session_id,
                "session_epoch": self.session_epoch,
                "calibration_bundle_hash": self.bundle_hash,
            }
            if not paused:
                # Motion was not confirmed stopped, so this episode may not be
                # presented as a valid run; the car keeps it as discarded evidence.
                body["discarded"] = True
                body["reason"] = f"control_pause_failed:{pause_error or 'unknown'}"
            if label in ("success", "failure"):
                body["success"] = label == "success"
            elif label == "unknown":
                body["success"] = None
            try:
                out = self.car.post("/recording/end_episode", body)
            except RecordingError as exc:
                self._fail("car_end_episode_failed", exc.detail)
                raise
            event = out.get("event") if isinstance(out.get("event"), dict) else {}
            car_time = _extract_time(event) or _extract_time(out)
            if car_time is not None and self.phases:
                if self.phases[-1].get("ended_time_ns") is None:
                    self.phases[-1]["ended_time_ns"] = car_time
            evaluation = out.get("evaluation") if isinstance(out.get("evaluation"), dict) else {}
            episode = {
                "episode_id": self.episode_id,
                "started_time_ns": self.episode_started_ns,
                "ended_time_ns": car_time,
                "phases": copy.deepcopy(self.phases),
                "result": label,
                "evaluation": evaluation,
                "paused_before_wrapup": paused,
                "pause_error": pause_error,
            }
            self.episodes.append(episode)
            self.episode_id = None
            self.episode_started_ns = None
            self.phases = []
            self.phase = None
            self._note("episode_ended", result=label, car_time_ns=car_time,
                       paused_before_wrapup=paused)
            if not paused:
                self._fail("control_pause_failed",
                           pause_error or "pause_all did not confirm")
                raise RecordingError("control_pause_failed",
                                     pause_error or "pause_all did not confirm",
                                     status_code=503)
            snapshot = self.status()
            return {**snapshot, "recording": copy.deepcopy(snapshot),
                    "stopped_first": True}

    def stop_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.status_value not in (ACTIVE, INVALID):
                raise RecordingError("recording_session_not_started")
            paused, pause_error = self._pause_all()
            self.paused_before_wrapup = paused
            forced = None
            if self.episode_id is not None:
                forced = self.episode_id
                self.car.post("/recording/end_episode", {
                    "episode_id": self.episode_id,
                    "discarded": True,
                    "reason": f"forced_by_stop:{pause_error or 'stop_session'}",
                    "session_id": self.session_id,
                    "session_epoch": self.session_epoch,
                    "calibration_bundle_hash": self.bundle_hash,
                })
                self.episode_id = None
                self.phases = []
                self.phase = None
            out = self.car.post("/recording/stop_session",
                                {"reason": str(payload.get("reason") or "normal"),
                                 "session_id": self.session_id,
                                 "session_epoch": self.session_epoch,
                                 "calibration_bundle_hash": self.bundle_hash})
            self._note("recording_session_stopped", forced_episode=forced,
                       paused_before_wrapup=paused, car=out)
            self.status_value = STOPPED if paused else INVALID
            if not paused:
                self._fail("control_pause_failed", pause_error or "pause_all did not confirm")
                raise RecordingError("control_pause_failed",
                                     pause_error or "pause_all did not confirm",
                                     status_code=503)
            snapshot = self.status()
            return {**snapshot, "recording": copy.deepcopy(snapshot),
                    "stopped_first": True}

    def release_for_session(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Close recording work for the matching control session before release."""
        payload = dict(payload or {})
        with self.lock:
            status = self._facade_status()
            session_id, epoch = self._assert_payload_identity(payload, status)
            if self.session_id is not None and (
                    self.session_id != session_id or self.session_epoch != epoch):
                raise RecordingError("control_session_changed", status_code=409)
            if self.status_value in (ACTIVE, INVALID):
                return self.stop_session({
                    **payload,
                    "reason": str(payload.get("reason") or "released"),
                })
            if self.status_value != NOT_STARTED:
                self._note("control_session_released",
                           reason=str(payload.get("reason") or "released"))
                self.status_value = STOPPED
            return self.status()

    def estop(self, reason: str = "operator") -> dict[str, Any]:
        """Preempt first; recording serialization and saving stay off the stop path."""
        facade_result: Any = None
        try:
            emergency_stop = getattr(self.facade, "emergency_stop", None)
            facade_result = (emergency_stop(str(reason or "operator"))
                             if callable(emergency_stop)
                             else self.facade.set_arm_state("estop"))
        except Exception as exc:  # noqa: BLE001 - the car stop belongs to the facade
            facade_result = {"error": type(exc).__name__}

        acquired = self.lock.acquire(blocking=False)
        if acquired:
            try:
                pending = self._latch_estop_locked(reason)
                if pending:
                    if self.background:
                        thread = threading.Thread(
                            target=self._finish_after_estop,
                            args=(reason,), name="hybrid-estop-save", daemon=True)
                        self._pending_estop_save = thread
                        thread.start()
                    else:
                        self._finish_after_estop(reason)
                return {
                    "estop": True, "save_deferred": bool(pending),
                    "preempted_recording_lock": False,
                    "outputs_enabled": False, "facade": facade_result,
                    "recording": self.status(),
                }
            finally:
                self.lock.release()

        # Another recording/car operation owns the controller lock.  The facade
        # stop above has already run; finish latching and persistence asynchronously.
        with self._estop_worker_lock:
            if not self._estop_worker_started:
                self._estop_worker_started = True
                thread = threading.Thread(
                    target=self._finish_queued_estop, args=(reason,),
                    name="hybrid-estop-preempt", daemon=True)
                self._pending_estop_save = thread
                thread.start()
        return {
            "estop": True, "save_deferred": True, "preempted_recording_lock": True,
            "outputs_enabled": False, "facade": facade_result,
            "recording": {"status": "estop_pending",
                          "session_id": self.session_id,
                          "session_epoch": self.session_epoch},
        }

    def _latch_estop_locked(self, reason: str) -> bool:
        was_active = self.status_value == ACTIVE
        self._fail("estop", reason, invalid=True)
        pending = self.episode_id is not None or was_active
        self._note("estop", reason=reason, save_deferred=pending)
        return pending

    def _finish_queued_estop(self, reason: str) -> None:
        try:
            with self.lock:
                pending = self._latch_estop_locked(reason)
            if pending:
                self._finish_after_estop(reason)
        finally:
            with self._estop_worker_lock:
                self._estop_worker_started = False

    def _finish_after_estop(self, reason: str) -> None:
        with self.lock:
            session_identity = (self.session_id, self.session_epoch)
            try:
                status = self._facade_status()
            except Exception as exc:  # noqa: BLE001
                self._fail("estop_save_failed", f"facade_status:{type(exc).__name__}")
                return
            current_identity = (status.get("session_id"), status.get("session_epoch"))
            if session_identity != current_identity:
                self._note("estop_save_skipped_stale_session",
                           reason=reason, current_session_id=status.get("session_id"),
                           current_session_epoch=status.get("session_epoch"))
                return
            try:
                if self.episode_id is not None:
                    self.car.post("/recording/end_episode", {
                        "episode_id": self.episode_id, "discarded": True,
                        "reason": f"estop:{reason}",
                        "session_id": self.session_id,
                        "session_epoch": self.session_epoch,
                        "calibration_bundle_hash": self.bundle_hash,
                    })
                    self.episode_id = None
                self.car.post("/recording/stop_session", {
                    "reason": f"estop:{reason}",
                    "session_id": self.session_id,
                    "session_epoch": self.session_epoch,
                    "calibration_bundle_hash": self.bundle_hash,
                })
                self._note("estop_save_completed", reason=reason)
            except Exception as exc:  # noqa: BLE001 - reported, not raised into the estop path
                self._fail("estop_save_failed", f"{type(exc).__name__}: {exc}")

    def _pause_all(self) -> tuple[bool, str | None]:
        """Stop motion before any wrap-up.  Never raises; the caller decides."""
        try:
            out = self.facade.set_arm_state("pause_all")
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if not isinstance(out, dict):
            return False, "facade returned a non-object"
        err = str(out.get("last_error") or "")
        if err.startswith("stop_failed"):
            return False, err
        return True, None

    # -- read models -------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self.lock:
            phases = (self.phases if self.episode_id is not None else
                      (self.episodes[-1]["phases"] if self.episodes else []))
            return {
                "source": "hybrid_recording_controller",
                "status": self.status_value,
                "session_id": self.session_id,
                "session_epoch": self.session_epoch,
                "clock_domain": self.clock_domain,
                "clock_epoch": self.clock_epoch,
                "calibration_bundle_hash": self.bundle_hash,
                "car_session_path": self.car_session_path,
                "car_recording_id": self.car_recording_id,
                "episode_id": self.episode_id,
                "episode_started_time_ns": self.episode_started_ns,
                "phase": self.phase,
                "phases": copy.deepcopy(phases),
                "episodes": copy.deepcopy(self.episodes),
                "phase_timeline_complete": self.phase_timeline_complete,
                "paused_before_wrapup": self.paused_before_wrapup,
                "motion_commands_sent": self.motion_commands_sent,
                "lease_acquired": False,
                "outputs_enabled": False,
                "last_error": self.last_error,
                "error_detail": self.error_detail,
                "event_count": len(self.events),
                "estop_save_pending": bool(self._pending_estop_save
                                           and self._pending_estop_save.is_alive()),
            }


def _extract_time(record: dict[str, Any] | None) -> int | None:
    if not isinstance(record, dict):
        return None
    for key in ("time_ns", "monotonic_ns"):
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    payload = record.get("payload")
    if isinstance(payload, dict):
        value = payload.get("time_ns")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _manual_label(value: Any) -> str:
    """success / failure / unknown only.  Nothing is inferred from the car."""
    if value is None or value == "":
        return "unknown"
    if value is True or value == "success":
        return "success"
    if value is False or value in ("failure", "failed"):
        return "failure"
    if value == "unknown":
        return "unknown"
    raise RecordingError("invalid_manual_result", f"unreadable manual result {value!r}",
                         status_code=400)


def describe_recording(controller: HybridRecordingController | None) -> dict[str, Any]:
    """Stable read model for ``GET /hybrid/recording/status``."""
    if controller is None:
        return {"status": "unavailable", "reason": "recording_controller_not_created"}
    return controller.status()
