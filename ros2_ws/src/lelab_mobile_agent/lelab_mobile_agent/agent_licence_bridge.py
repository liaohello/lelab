"""Agent-side licence bridge: the car agent stays the only authority.

Why this exists: the standalone ROS control process must not mint its own permission,
and it must not have to trust the upper computer either.  The agent already decides
who holds the car control lease, which epoch and calibration bundle are bound, which
channels are granted, whether outputs are enabled and whether an emergency stop is
latched.  This bridge samples exactly those facts and mirrors them, bounded, to the
control process over the local socket.

Design choices that matter
--------------------------
* sampling at a fixed cadence instead of a call in every route: the permission state
  is derived from the agent's own objects, so a new decision path cannot forget to
  notify the mirror; an explicit ``wake()`` is offered for the stop/estop paths that
  must not wait for the next tick;
* nothing here blocks the caller: ``wake()`` only sets an event, and the publisher's
  socket work happens on this thread.  No agent lock is held across socket I/O;
* the lease token never crosses the socket - only its digest - and the token is what
  the upper computer presents on the frame stream, so the control process can check
  it without holding a secret it could leak;
* generations move forward monotonically and survive an agent restart through a
  persisted counter, so a restarting agent cannot re-issue a stale generation and a
  control process can keep refusing older ones;
* clearing an emergency stop is a separate message that carries no permission.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .licence_ipc import DEFAULT_CONTEXT_TTL_MS, LicencePublisher, licence_record
from .teleop_stream import token_digest

DEFAULT_HEARTBEAT_S = 0.1
DEFAULT_MIRROR_TTL_MS = 300
# The licence must never outlive the real car lease by a wide margin.
MAX_TTL_MS = 2000


class LicenceGenerationStore:
    """A forward-only counter, so a restarted agent cannot reuse an old generation."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._value = self._read()

    def _read(self) -> int:
        try:
            return max(0, int(self.path.read_text().strip()))
        except (FileNotFoundError, ValueError):
            return 0

    def next(self) -> int:
        self._value += 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(f"{self._value}\n", encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            # Losing the file only costs the cross-restart property, not safety:
            # the in-memory counter keeps advancing for this process.
            pass
        return self._value

    @property
    def current(self) -> int:
        return self._value


class AgentLicenceBridge:
    """Publish a bounded mirror of the agent's own control decisions.

    ``authority`` is a zero-argument callable returning the live facts:

        session_id, session_epoch, calibration_bundle_hash, lease_token,
        permits {arm, gripper}, outputs_enabled, lease_expires_in_s, estop_latched

    Returning ``lease_token=None`` means "nobody holds control", which the bridge
    turns into a revocation rather than a permission with empty fields.

    ``recording_authority`` is an optional second callable returning the recorder's live
    context (session_id, session_epoch, episode_id, phase, clock_epoch).  It grants no
    permission at all: it exists so a frame recorded in another process can name the
    episode the agent put it in, instead of having that reconstructed later.
    """

    def __init__(self, *, socket_path: Path, authority: Callable[[], dict[str, Any]],
                 generation_path: Path, status_path: Path | None = None,
                 recording_authority: Callable[[], dict[str, Any]] | None = None,
                 agent_boot: str = "",
                 heartbeat_s: float = DEFAULT_HEARTBEAT_S,
                 ttl_ms: int = DEFAULT_MIRROR_TTL_MS,
                 context_ttl_ms: int = DEFAULT_CONTEXT_TTL_MS,
                 logger: Any = None):
        self.authority = authority
        self.recording_authority = recording_authority
        self.agent_boot = str(agent_boot or "")
        self.context_ttl_ms = max(100, min(int(context_ttl_ms), 5000))
        self.heartbeat_s = max(0.02, float(heartbeat_s))
        self.ttl_ms = max(50, min(int(ttl_ms), MAX_TTL_MS))
        self.publisher = LicencePublisher(socket_path)
        # The control process is the only one that knows whether a frame stream is
        # attached.  It reports through its own status file, which it rewrites the
        # moment a hello is accepted; the bridge polls it at the heartbeat rate, so
        # the window in which a legacy route could double-emit is bounded by that
        # interval rather than by a cache nobody refreshes.
        self.status_path = Path(status_path) if status_path else None
        self.generation = LicenceGenerationStore(generation_path)
        self.logger = logger
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._published: dict[str, Any] | None = None
        self._clear_pending = False
        # The recording context is a separate, permission-free mirror.  It is refreshed
        # at half its own TTL, so a phase change is picked up immediately and a dead agent
        # still lets the context expire on its own.
        self._context_seq = 0
        self._published_context: dict[str, Any] | None = None
        self._context_ticks = max(1, int((self.context_ttl_ms / 2000.0) / self.heartbeat_s) + 1)
        self._tick = 0
        # What the control process reports back about the frame transport.
        self.frame_stream: dict[str, Any] | None = None
        self.frame_process: dict[str, Any] | None = None
        self.counters = {"published": 0, "revoked": 0, "failures": 0, "wakes": 0,
                         "cleared": 0, "contexts": 0, "context_refused": 0}
        self.last_error: str | None = None
        self.last_state = "idle"

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="licence-bridge", daemon=True)
        self._thread.start()

    def stop(self, reason: str = "agent_stopping") -> None:
        """Stop publishing, revoke, then close.

        The order matters: revoking while the heartbeat thread is still running let a
        later heartbeat re-issue the permission and leave the control process holding a
        live licence after the agent was gone.
        """
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._published is not None:
            self.publisher.revoke(reason)
            self.counters["revoked"] += 1
            self._published = None
            self.last_state = "revoked"
        self.publisher.close()

    def wake(self) -> None:
        """Ask for an immediate re-publish (stop/estop paths).  Never blocks."""
        self._wake.set()
        self.counters["wakes"] += 1

    def request_clear_estop(self) -> int:
        """Announce that an emergency stop was lifted, without granting output.

        The clearance is queued, not sent here: the socket work stays on this thread.  It
        gets its own generation when it goes out (see ``_publish_once``), because two
        stop/clear cycles inside one licence would otherwise look like a replayed message
        to the mirror - and a refused clearance would leave the control process latched
        with nothing left that could lift it.
        """
        self._clear_pending = True
        self.wake()
        return self.generation.current

    # -- the loop ----------------------------------------------------------
    def _facts_differ(self, facts: dict[str, Any]) -> bool:
        published = self._published or {}
        for key in ("session_id", "session_epoch", "calibration_bundle_hash",
                    "token_sha256", "outputs_enabled"):
            if published.get(key) != facts.get(key):
                return True
        if published.get("permits") != facts.get("permits"):
            return True
        return False

    def _snapshot(self) -> dict[str, Any] | None:
        try:
            facts = self.authority() or {}
        except Exception as error:      # noqa: BLE001 - a bad sample must not kill the loop
            self.last_error = f"authority_{type(error).__name__}: {error}"
            self.counters["failures"] += 1
            return None
        token = facts.get("lease_token")
        if not token or not facts.get("session_id"):
            return None
        remaining_s = facts.get("lease_expires_in_s")
        if isinstance(remaining_s, (int, float)) and float(remaining_s) <= 0.0:
            # The lease this mirror reflects has run out.  Refreshing a short TTL would
            # keep the control process emitting on a permission nobody holds any more, so
            # the mirror is revoked instead - the same answer a release gets.
            return None
        ttl_ms = self.ttl_ms
        if isinstance(remaining_s, (int, float)):
            ttl_ms = max(50, min(ttl_ms, int(max(0.0, float(remaining_s)) * 1000)))
        return {
            "session_id": facts["session_id"],
            "session_epoch": facts.get("session_epoch"),
            "calibration_bundle_hash": facts.get("calibration_bundle_hash") or "unbound",
            "permits": dict(facts.get("permits") or {}),
            "outputs_enabled": bool(facts.get("outputs_enabled"))
            and not facts.get("estop_latched"),
            "token_sha256": token_digest(str(token)),
            "ttl_ms": ttl_ms,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._publish_once()
            except Exception as error:  # noqa: BLE001 - fail closed by stopping the heartbeat
                self.last_error = f"{type(error).__name__}: {error}"
                self.counters["failures"] += 1
            try:
                self._publish_context_once()
            except Exception as error:  # noqa: BLE001 - a lost context is fail-safe: no stamp
                self.last_error = f"context_{type(error).__name__}: {error}"
                self.counters["context_refused"] += 1
            if self._wake.wait(self.heartbeat_s):
                self._wake.clear()

    def _context_snapshot(self) -> dict[str, Any] | None:
        if self.recording_authority is None:
            return None
        try:
            facts = self.recording_authority() or {}
        except Exception as error:      # noqa: BLE001 - the recorder is not the licence
            self.last_error = f"recording_authority_{type(error).__name__}: {error}"
            return None
        if not facts.get("session_id"):
            return None
        return {
            "session_id": facts["session_id"],
            "session_epoch": facts.get("session_epoch"),
            "episode_id": facts.get("episode_id"),
            "phase": facts.get("phase"),
            "clock_epoch": facts.get("clock_epoch"),
            "calibration_bundle_hash": facts.get("calibration_bundle_hash") or "unbound",
            "ttl_ms": self.context_ttl_ms,
            "agent_boot": self.agent_boot,
        }

    def _publish_context_once(self) -> None:
        self._tick += 1
        snapshot = self._context_snapshot()
        if snapshot is None:
            # Nothing to mirror: the recorder is between sessions.  The context is left to
            # expire at the mirror, which is what makes the control process say
            # "uncontexted" instead of stamping a stale episode onto new rows.
            self._published_context = None
            return
        published = self._published_context or {}
        changed = any(published.get(key) != snapshot.get(key) for key in
                      ("session_id", "session_epoch", "episode_id", "phase", "clock_epoch",
                       "calibration_bundle_hash"))
        if not changed and self._tick % self._context_ticks:
            return
        if changed or self._published_context is None:
            self._context_seq += 1
        snapshot["context_seq"] = self._context_seq
        if self.publisher.publish_context(**snapshot):
            self._published_context = snapshot
            self.counters["contexts"] += 1
        else:
            # Drop the view so the next tick retries with the same sequence number: a
            # context that never reached the mirror simply expires there, and the control
            # process then records rows it marks as uncontexted.
            self._published_context = None

    def _poll_stream_state(self) -> None:
        if self.status_path is None:
            return
        try:
            with self.status_path.open(encoding="utf-8") as handle:
                document = json.load(handle)
                age_s = time.time() - os.fstat(handle.fileno()).st_mtime
        except (OSError, ValueError):
            self.frame_process = None
            return                      # not written yet, or mid-rotation: keep the last view
        if age_s < -0.25:
            self.frame_process = None
            return
        age_s = max(0.0, age_s)
        context = (document.get("licence") or {}).get("recording_context") or {}
        # Publish only non-secret confirmation fields; the licence contains a
        # token digest and must not be copied into the public agent status.
        self.frame_process = {
            "observed_mono": time.monotonic() - age_s,
            "data_identity": document.get("data_identity"),
            "configured_bundle_hash": document.get("configured_bundle_hash"),
            "trial_motion": document.get("trial_motion"),
            "trial_first_frame_pending": document.get(
                "trial_first_frame_pending"),
            "homing_motion": document.get("homing_motion"),
            "homing_first_frame_pending": document.get("homing_first_frame_pending"),
            "homing_active_joint": document.get("homing_active_joint"),
            "homing_last_target_raw": document.get("homing_last_target_raw"),
            "publisher_subscriptions": document.get("publisher_subscriptions"),
            "homing_torque_enabled": document.get("homing_torque_enabled"),
            "homing_torque_state": document.get("homing_torque_state"),
            "homing_torque_results": document.get("homing_torque_results"),
            "homing_torque_error": document.get("homing_torque_error"),
            "torque_publish_count": document.get("torque_publish_count"),
            "torque_servo_ids": document.get("torque_servo_ids"),
            "ticks": (document.get("counters") or {}).get("ticks"),
            "publish_count": document.get("publish_count"),
            "effective_emit_hz": document.get("effective_emit_hz"),
            "emit_intervals": document.get("emit_intervals"),
            "frames_received": (document.get("counters") or {}).get("frames_received"),
            "frames_emitted": (document.get("counters") or {}).get("frames_emitted"),
            "frames_refused": (document.get("counters") or {}).get("frames_refused"),
            "last_refusal": document.get("last_refusal"),
            "paused": document.get("paused"),
            "outputs_enabled": document.get("outputs_enabled"),
            # The emitter's own stop state. The agent's latch says nobody may grant
            # motion; only this field says the process that emits has lifted its own
            # refusal, so the upper host can confirm both layers over one HTTP reply
            # instead of reading a file that only exists on the car.
            "latched_fault": document.get("latched_fault"),
            "reengage_required": document.get("reengage_required"),
            "last_clear": document.get("last_clear"),
            "queue_depth": (document.get("admission") or {}).get("queue_depth", 0),
            "context": {key: context.get(key) for key in
                        ("session_id", "session_epoch", "episode_id", "phase", "clock_epoch",
                         "calibration_bundle_hash", "context_seq", "agent_boot")},
        }
        stream = document.get("stream") or {}
        hello = stream.get("hello") or {}
        if not stream.get("client_connected"):
            self.note_stream_state({"active": False})
            return
        self.note_stream_state({"active": True, "session_id": hello.get("session_id"),
                               "session_epoch": hello.get("session_epoch"),
                               "stream_epoch": stream.get("generation")})

    def _deliver_clear(self) -> None:
        """Send the queued clearance, if one is waiting.

        It has to go out even when there is no live licence to mirror: after a lease
        expires the snapshot is None, and a stop the emitter could only lift by a new
        hello would leave recovery with no path at all. The message still carries no
        permission - motion needs the agent's own enable afterwards.
        """
        if not self._clear_pending:
            return
        if not self.publisher._sock and not self.publisher.connect():
            self.last_state = "connecting"
            self.last_error = self.last_error or "control_process_socket_unavailable"
            return                      # stays queued, and the next tick retries
        if self.publisher.clear_estop(self.generation.next(), "operator_cleared_estop"):
            self.counters["cleared"] += 1
            self._clear_pending = False
        else:
            self.counters["failures"] += 1
            self.last_state = "clear_failed"

    def _publish_once(self) -> None:
        self._poll_stream_state()
        snapshot = self._snapshot()
        if snapshot is None:
            if self._published is not None:
                if self.publisher.revoke("car_lease_released"):
                    self.counters["revoked"] += 1
                    self._published = None
                    self.last_state = "revoked"
                else:
                    # Not revoked yet: keep trying, and let the mirror's own expiry
                    # stop the control process in the meantime.
                    self.counters["failures"] += 1
                    self.last_state = "revoking"
            else:
                self.last_state = "idle"
            self._deliver_clear()
            return
        if not self.publisher._sock and not self.publisher.connect():
            self.last_state = "connecting"
            self.last_error = self.last_error or "control_process_socket_unavailable"
            return
        changed = self._facts_differ(snapshot)
        published_now = False
        if changed or self._published is None:  # a real change gets a new generation
            generation = self.generation.next()
            record = licence_record(control_generation=generation,
                                    session_id=snapshot["session_id"],
                                    session_epoch=snapshot["session_epoch"],
                                    calibration_bundle_hash=snapshot["calibration_bundle_hash"],
                                    permits=snapshot["permits"],
                                    outputs_enabled=snapshot["outputs_enabled"],
                                    token_sha256=snapshot["token_sha256"],
                                    ttl_ms=snapshot["ttl_ms"])
            if self.publisher.publish(**record):
                self._published = record
                self.counters["published"] += 1
                self.last_state = "issuing"
                self.last_error = None
                published_now = True
            else:
                self.counters["failures"] += 1
                self.last_state = "publish_failed"
                return                      # keep the clearance queued and retry both
        if self._clear_pending:
            # The clearance carries no permission, so it is not a licence generation - but
            # it does need one of its own.  Reusing the live generation would make the
            # second stop/clear cycle inside one licence look like a replay, and the
            # control process compares the clearance against the generation it latched on.
            self._deliver_clear()
            return
        if published_now:
            return
        # A heartbeat refreshes the permission only; it never extends an action
        # sample and never mints a new generation.
        if self.publisher.publish(**self._published):
            self.counters["heartbeats"] = self.counters.get("heartbeats", 0) + 1

    # -- observability -----------------------------------------------------
    def note_stream_state(self, message: dict[str, Any]) -> None:
        """Record what the control process says about the active frame transport."""
        active = bool(message.get("active"))
        self.frame_stream = None if not active else {
            "session_id": message.get("session_id"),
            "session_epoch": message.get("session_epoch"),
            "stream_epoch": message.get("stream_epoch"),
            "since": time.monotonic(),
        }

    def frame_transport_active(self, session_id=None, session_epoch=None) -> bool:
        stream = self.frame_stream
        if not stream:
            return False
        if session_id is not None and stream["session_id"] != session_id:
            return False
        if session_epoch is not None and str(stream["session_epoch"]) != str(session_epoch):
            return False
        return True

    def state(self) -> dict[str, Any]:
        process = None if self.frame_process is None else dict(self.frame_process)
        if process is not None:
            process["age_s"] = max(0.0, time.monotonic() - process.pop("observed_mono"))
        return {"state": self.last_state, "generation": self.generation.current,
                "context_seq": self._context_seq,
                "recording_context": self._published_context,
                "ttl_ms": self.ttl_ms, "heartbeat_s": self.heartbeat_s,
                "connected": self.publisher._sock is not None,
                "counters": dict(self.counters), "last_error": self.last_error,
                "issuing": bool(self._published),
                "clear_pending": self._clear_pending,
                "frame_process": process,
                "frame_stream": None if self.frame_stream is None else
                                {key: value for key, value in self.frame_stream.items()
                                 if key != "since"}}
