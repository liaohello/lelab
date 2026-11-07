"""Local licence mirror: the agent stays the authority, the control process obeys.

The car-side agent already owns the control lease, the output permission and the
emergency stop.  A separate control process must not mint its own permission, so the
agent pushes a short-lived *mirror* of its decision over a Unix socket in the
deployment state directory, and the control process refuses to emit anything unless
a current mirror says it may.

Consequences that matter for safety
-----------------------------------
* the mirror keeps its own deadline (default 300 ms).  A missing heartbeat therefore
  stops control within a bounded time instead of waiting out the 30 s session lease;
* a socket EOF, an agent exit or a revoke clears the mirror immediately;
* a heartbeat refreshes the *permission* only.  It never lengthens the life of an
  action sample - those are bounded by their own TTL and the freshness window;
* the lease token itself never crosses this boundary, only its digest, which the
  control process compares against what a connecting upper computer presents.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1
DEFAULT_TTL_MS = 300
DEFAULT_CONTEXT_TTL_MS = 1000
DEFAULT_HEARTBEAT_S = 0.1
MAX_LINE_BYTES = 32 * 1024
FIELDS = ("control_generation", "session_id", "session_epoch", "calibration_bundle_hash",
          "permits", "outputs_enabled", "token_sha256", "ttl_ms")


def monotonic_ns() -> int:
    return time.monotonic_ns()


class LicenceError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def _identity(value: Any, name: str):
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise LicenceError("invalid_identity", f"{name} must be a non-empty string or integer")


def licence_record(*, control_generation: int, session_id, session_epoch,
                   calibration_bundle_hash: str, permits: dict[str, bool],
                   outputs_enabled: bool, token_sha256: str,
                   ttl_ms: int = DEFAULT_TTL_MS) -> dict[str, Any]:
    if isinstance(control_generation, bool) or not isinstance(control_generation, int) \
            or control_generation < 0:
        raise LicenceError("invalid_generation", "control_generation must be a non-negative int")
    if not isinstance(permits, dict) or set(permits) - {"base", "arm", "gripper"}:
        raise LicenceError("invalid_permits", f"unexpected permit keys: {sorted(permits)}")
    for key, value in permits.items():
        if not isinstance(value, bool):
            raise LicenceError("invalid_permits", f"{key} permit must be boolean")
    if not isinstance(outputs_enabled, bool):
        raise LicenceError("invalid_outputs", "outputs_enabled must be boolean")
    if not isinstance(token_sha256, str) or len(token_sha256) < 16:
        raise LicenceError("invalid_token_digest", "the agent must publish a token digest")
    ttl = int(ttl_ms)
    if not 20 <= ttl <= 5000:
        raise LicenceError("invalid_ttl", "the bridge TTL stays short on purpose")
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "licence",
        "control_generation": control_generation,
        "session_id": _identity(session_id, "session_id"),
        "session_epoch": _identity(session_epoch, "session_epoch"),
        "calibration_bundle_hash": _identity(calibration_bundle_hash, "calibration_bundle_hash"),
        "permits": dict(permits),
        "outputs_enabled": outputs_enabled,
        "token_sha256": token_sha256,
        "ttl_ms": ttl,
    }


def recording_context_record(*, session_id, session_epoch, episode_id, phase,
                             clock_epoch, calibration_bundle_hash: str, context_seq: int,
                             ttl_ms: int = DEFAULT_CONTEXT_TTL_MS,
                             agent_boot: str = "") -> dict[str, Any]:
    """The recorder's context, mirrored so a control row can name its own episode.

    This is deliberately not a licence: it grants nothing.  It is the agent saying which
    session/episode/phase is live right now, so a frame recorded by the control process is
    attributed to an episode by a fact the agent published, not by a guess made at export
    time.  It expires on its own, and a missing context is reported as missing.
    """
    if isinstance(context_seq, bool) or not isinstance(context_seq, int) or context_seq < 0:
        raise LicenceError("invalid_context_seq", "context_seq must be a non-negative int")
    ttl = int(DEFAULT_CONTEXT_TTL_MS if ttl_ms is None else ttl_ms)
    if not 100 <= ttl <= 5000:
        raise LicenceError("invalid_ttl", "a recording context stays short-lived")
    if episode_id is not None and (isinstance(episode_id, bool) or not episode_id):
        raise LicenceError("invalid_episode_id", "episode_id must be a non-empty string or null")
    if phase is not None and (not isinstance(phase, str) or not phase):
        raise LicenceError("invalid_phase", "phase must be a non-empty string or null")
    if clock_epoch is not None and (not isinstance(clock_epoch, str) or not clock_epoch):
        raise LicenceError("invalid_clock_epoch", "clock_epoch must be a non-empty string or null")
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "recording_context",
        "session_id": _identity(session_id, "session_id"),
        "session_epoch": _identity(session_epoch, "session_epoch"),
        "episode_id": episode_id,
        "phase": phase,
        "clock_epoch": clock_epoch,
        "calibration_bundle_hash": _identity(calibration_bundle_hash,
                                             "calibration_bundle_hash"),
        "context_seq": context_seq,
        "agent_boot": str(agent_boot or ""),
        "ttl_ms": ttl,
    }


class LicenceMirror:
    """The control process' view of the agent's decision, with its own deadline."""

    def __init__(self, socket_path: Path | str, *, clock_ns: Callable[[], int] = monotonic_ns,
                 default_ttl_ms: int = DEFAULT_TTL_MS):
        self.path = Path(socket_path)
        self._clock = clock_ns
        self._default_ttl_ms = default_ttl_ms
        self._lock = threading.Lock()
        self._current: dict[str, Any] | None = None
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._revoked_by = None
        self._clear: dict[str, Any] | None = None
        self._last_clear_generation = -1
        self._context: dict[str, Any] | None = None
        self._context_boot: str | None = None
        self._last_context_seq = -1
        self.counters = {"received": 0, "refreshed": 0, "replaced": 0, "refused": 0,
                         "expired": 0, "revoked": 0, "eof": 0, "contexts": 0,
                         "context_expired": 0}
        self.last_error: str | None = None

    # -- server side -------------------------------------------------------
    def start(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        # Only the deploying user may drive the control process.
        os.chmod(str(self.path), 0o600)
        server.listen(1)
        server.settimeout(0.2)
        self._server = server
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="licence-mirror", daemon=True)
        self._thread.start()
        return self.path

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._server = None
        try:
            self.path.unlink()
        except OSError:
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle(conn)
            except OSError as exc:
                self.last_error = f"io_error:{exc.errno}"
            finally:
                # The publisher going away is itself a revocation: no agent, no control.
                # The recording context goes with it, because a row stamped into an
                # episode nobody is authorising any more would be worse than no stamp.
                with self._lock:
                    self._current = None
                    self._revoked_by = "ipc_eof"
                    self._context = None
                self.counters["eof"] += 1
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(2.0)
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if len(line) > MAX_LINE_BYTES:
                    self.counters["refused"] += 1
                    self.last_error = "line_too_long"
                    buffer = b""
                    continue
                try:
                    self.apply(json.loads(line.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self.counters["refused"] += 1
                    self.last_error = f"malformed:{type(exc).__name__}"

    # -- state ------------------------------------------------------------
    def apply(self, message: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(message, dict):
            raise LicenceError("malformed", "a licence message must be an object")
        if message.get("schema_version") != SCHEMA_VERSION:
            raise LicenceError("schema_version", str(message.get("schema_version")))
        if message.get("type") == "clear_estop":
            # Clearing a stop is its own message: it cannot smuggle outputs_enabled
            # or a permit change, so an operator lifting the emergency stop never
            # silently re-arms motion.
            generation = int(message.get("control_generation", 0))
            with self._lock:
                # A clearance is identified by the licence generation it was issued
                # under and may only move forward, so a replayed or older one is
                # refused even after the live one has been consumed.
                if generation <= self._last_clear_generation:
                    self.counters["refused"] += 1
                    raise LicenceError("generation_replayed",
                                       f"clear {generation} follows "
                                       f"{self._last_clear_generation}")
                self._last_clear_generation = generation
                self._clear = {"control_generation": generation,
                               "reason": str(message.get("reason") or "cleared"),
                               "received_ns": self._clock()}
            return {"applied": True, "state": self.state()}
        if message.get("type") == "revoke":
            generation = int(message.get("control_generation", 0))
            with self._lock:
                if self._current is None or generation >= self._current["control_generation"]:
                    self._current = None
                    self._revoked_by = str(message.get("reason", "revoked"))
                    self.counters["revoked"] += 1
            return {"applied": True, "state": self.state()}
        if message.get("type") == "recording_context":
            context = recording_context_record(
                session_id=message["session_id"], session_epoch=message["session_epoch"],
                episode_id=message.get("episode_id"), phase=message.get("phase"),
                clock_epoch=message.get("clock_epoch"),
                calibration_bundle_hash=message["calibration_bundle_hash"],
                context_seq=message["context_seq"], ttl_ms=message.get("ttl_ms"),
                agent_boot=message.get("agent_boot", ""))
            now = self._clock()
            with self._lock:
                boot = context["agent_boot"]
                if self._context_boot is not None and boot != self._context_boot:
                    # A different agent process restarts the sequence: its contexts cannot
                    # be compared with the old one's, and refusing every one of them would
                    # strand the control process without a context for good.
                    self._context_boot = boot
                    self._last_context_seq = -1
                elif self._context_boot is None:
                    self._context_boot = boot
                elif context["context_seq"] < self._last_context_seq:
                    self.counters["refused"] += 1
                    raise LicenceError("context_replayed",
                                       f"{context['context_seq']} follows "
                                       f"{self._last_context_seq}")
                elif context["context_seq"] == self._last_context_seq:
                    # The same sequence number may only repeat the same facts: that is a
                    # refresh before the TTL.  A different episode under an already-seen
                    # number is a replay, and would silently re-stamp new rows.
                    if any(self._context.get(key) != context.get(key) for key in
                           ("session_id", "session_epoch", "episode_id", "phase",
                            "clock_epoch", "calibration_bundle_hash")):
                        self.counters["refused"] += 1
                        raise LicenceError("context_replayed",
                                           f"seq {context['context_seq']} changed content")
                self._last_context_seq = context["context_seq"]
                context["received_ns"] = now
                context["deadline_ns"] = now + context["ttl_ms"] * 1_000_000
                self._context = context
                self.counters["contexts"] += 1
            return {"applied": True, "state": self.state()}
        record = licence_record(
            control_generation=message["control_generation"],
            session_id=message["session_id"], session_epoch=message["session_epoch"],
            calibration_bundle_hash=message["calibration_bundle_hash"],
            permits=message["permits"], outputs_enabled=message["outputs_enabled"],
            token_sha256=message["token_sha256"], ttl_ms=message.get("ttl_ms", self._default_ttl_ms))
        now = self._clock()
        with self._lock:
            previous = self._current
            if previous is not None:
                if record["control_generation"] < previous["control_generation"]:
                    self.counters["refused"] += 1
                    raise LicenceError("generation_replayed",
                                       f"{record['control_generation']} follows "
                                       f"{previous['control_generation']}")
                if record["control_generation"] == previous["control_generation"]:
                    self.counters["refreshed"] += 1
                else:
                    self.counters["replaced"] += 1
            record["received_ns"] = now
            record["deadline_ns"] = now + record["ttl_ms"] * 1_000_000
            self._current = record
            self._revoked_by = None
            self.counters["received"] += 1
        return {"applied": True, "state": self.state()}

    def take_clear_estop(self) -> dict[str, Any] | None:
        """The pending explicit clear, consumed once by the control loop."""
        with self._lock:
            clear, self._clear = self._clear, None
        return clear

    def current(self) -> dict[str, Any] | None:
        """The live mirror, or None.  Expiry is evaluated here, never cached."""
        with self._lock:
            record = self._current
            if record is None:
                return None
            if self._clock() >= record["deadline_ns"]:
                self._current = None
                self._revoked_by = "heartbeat_expired"
                self.counters["expired"] += 1
                return None
            return dict(record)

    def may_emit(self, channel: str) -> tuple[bool, str]:
        record = self.current()
        if record is None:
            return False, str(self._revoked_by or "no_licence")
        if not record["outputs_enabled"]:
            return False, "outputs_disabled"
        if not record["permits"].get(channel, False):
            return False, f"{channel}_not_permitted"
        return True, ""

    def matches(self, *, session_id, session_epoch, bundle_hash) -> bool:
        record = self.current()
        if record is None:
            return False
        # The epoch is an opaque integer identity, and it reaches this process on two
        # different wires: the agent publishes it as a number, the frame client names it
        # as the string it was handed.  Comparing the rendered value keeps "the same
        # epoch" true without letting a different epoch through.
        return (record["session_id"] == session_id
                and str(record["session_epoch"]) == str(session_epoch)
                and record["calibration_bundle_hash"] == bundle_hash)

    def token_digest(self) -> str | None:
        record = self.current()
        return None if record is None else record["token_sha256"]

    def context(self) -> dict[str, Any] | None:
        """The live recording context, or None when the agent has not published one.

        Expiry is checked here, like the licence: a context that stopped being refreshed
        must stop being stamped onto rows, because the episode it names may have ended.
        """
        with self._lock:
            context = self._context
            if context is None:
                return None
            if self._clock() >= context["deadline_ns"]:
                self._context = None
                self.counters["context_expired"] += 1
                return None
            return dict(context)

    def state(self) -> dict[str, Any]:
        record = self.current()
        return {"authorised": record is not None,
                "revoked_reason": self._revoked_by,
                "pending_clear": dict(self._clear) if self._clear else None,
                "licence": record,
                "recording_context": self.context(),
                "counters": dict(self.counters),
                "last_error": self.last_error}


class LicencePublisher:
    """Agent side: push the mirror, and push a revocation on the way out."""

    def __init__(self, socket_path: Path | str, *,
                 clock_ns: Callable[[], int] = monotonic_ns):
        self.path = Path(socket_path)
        self._clock = clock_ns
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._generation = 0
        self.sent = 0

    def connect(self, timeout_s: float = 0.5) -> bool:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout_s)
            sock.connect(str(self.path))
        except OSError:
            return False
        self._sock = sock
        return True

    def send(self, message: dict[str, Any]) -> bool:
        with self._lock:
            sock = self._sock
            if sock is None:
                return False
            try:
                sock.sendall(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
                self.sent += 1
                return True
            except OSError:
                self._sock = None
                try:
                    sock.close()
                except OSError:
                    pass
                return False

    def publish(self, **fields: Any) -> bool:
        # Accept the output of licence_record() as well as its keyword arguments.
        fields.pop("schema_version", None)
        fields.pop("type", None)
        record = licence_record(**fields)
        self._generation = record["control_generation"]
        return self.send(record)

    def publish_context(self, **fields: Any) -> bool:
        """Push the recorder's live context.  Grants nothing, so it mints no generation."""
        fields.pop("schema_version", None)
        fields.pop("type", None)
        return self.send(recording_context_record(**fields))

    def clear_estop(self, control_generation: int, reason: str) -> bool:
        return self.send({"schema_version": SCHEMA_VERSION, "type": "clear_estop",
                          "control_generation": int(control_generation), "reason": str(reason)})

    def revoke(self, reason: str) -> bool:
        generation = self._generation + 1 if self._sock is None else self._generation
        return self.send({"schema_version": SCHEMA_VERSION, "type": "revoke",
                          "control_generation": generation, "reason": str(reason)})

    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
