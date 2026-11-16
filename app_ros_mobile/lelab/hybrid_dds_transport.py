"""Upper-computer DDS command transport over the local Humble bridge.

The app never imports rclpy and never publishes a ROS topic itself.  It sends a
complete ``teleop_frame_v1`` dict plus an HMAC ticket to the bridge over a local
Unix socket; the bridge publishes a typed TeleopFrame; the car verifies the
ticket and feeds its existing FrameAdmission.  The raw control lease token is
not part of this path.

The client is deliberately shaped like ``teleop_stream.StreamClient`` so
``FrameLink`` can use it unchanged.  A DDS write failure marks the transport
disengaged and drops the queued frame; it never silently falls back to
FrameLink.
"""
from __future__ import annotations

import copy
import json
import socket
import threading
import time
from collections import deque
from typing import Any

from .teleop_frame_v1 import (
    FrameError, auth_key_id, frame_auth_ticket, hello_auth_ticket,
    preempt_auth_ticket, verify_frame_auth_ticket,
)
from .teleop_stream import StreamError

BRIDGE_SCHEMA = "lelab.upper_dds_bridge.v1"
MAX_LINE_BYTES = 1_048_576


class DdsStreamClient:
    """Uses the bridge's local snapshot as the window/receipt channel."""

    def __init__(self, *, socket_path: str, sender_id: str, transport_epoch: str,
                 auth_key: bytes, timeout_s: float = 1.0,
                 poll_period_s: float = 0.05,
                 clock_ns=time.monotonic_ns) -> None:
        if not isinstance(auth_key, bytes) or len(auth_key) < 32:
            raise StreamError("dds_auth_key_invalid", "a pre-shared key is required")
        self.socket_path = str(socket_path)
        self.sender_id = str(sender_id)
        self.transport_epoch = str(transport_epoch)
        self.auth_key = auth_key
        self.key_id = auth_key_id(auth_key)
        self.timeout_s = max(0.05, float(timeout_s))
        self.poll_period_s = max(0.05, float(poll_period_s))
        self.clock_ns = clock_ns
        self.session_id = self.session_epoch = None
        self.stream_epoch: str | None = None
        self.engaged = False
        self.closed = False
        self.last_error: str | None = None
        self.command_enabled: bool | None = None
        self._window: dict[str, Any] | None = None
        self._window_mono_ns: int | None = None
        self._snapshot: dict[str, Any] | None = None
        self._control: socket.socket | None = None
        self._status: socket.socket | None = None
        self._control_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._status_buffer = b""
        self._outbox: dict[str, Any] | None = None
        self._outbox_event = threading.Event()
        self._stop = threading.Event()
        self._writer: threading.Thread | None = None
        self._poller: threading.Thread | None = None
        self._pending_receipts: deque[dict[str, Any]] = deque()
        self._seen_receipts: deque[tuple] = deque(maxlen=512)
        self.counts = {"frames_enqueued": 0, "frames_sent": 0, "send_errors": 0,
                       "polls": 0, "poll_errors": 0, "receipts": 0,
                       "preempts": 0}

    # -- sockets -----------------------------------------------------------
    def _open(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout_s)
        sock.connect(self.socket_path)
        return sock

    def _line_request(self, sock: socket.socket, payload: dict[str, Any],
                      buffer: bytes = b"") -> tuple[dict[str, Any], bytes]:
        sock.sendall((json.dumps(payload, sort_keys=True) + "\n").encode())
        while b"\n" not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("bridge_closed")
            buffer += chunk
            if len(buffer) > MAX_LINE_BYTES:
                raise ValueError("response_too_large")
        line, buffer = buffer.split(b"\n", 1)
        document = json.loads(line)
        if not isinstance(document, dict):
            raise ValueError("response_not_an_object")
        return document, buffer

    def _status_snapshot(self) -> dict[str, Any]:
        with self._status_lock:
            if self._status is None:
                self._status = self._open()
                self._status_buffer = b""
            try:
                document, self._status_buffer = self._line_request(
                    self._status, {"op": "snapshot"}, self._status_buffer)
            except (OSError, ValueError, ConnectionError):
                self._close_status()
                raise
            if document.get("schema") != BRIDGE_SCHEMA or document.get("ok") is not True:
                raise ValueError("bridge_schema_mismatch")
            return document

    def _close_status(self) -> None:
        sock, self._status = self._status, None
        self._status_buffer = b""
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # -- session -----------------------------------------------------------
    def connect(self, *, lease_token: str, session_id, session_epoch) -> dict[str, Any]:
        del lease_token  # the raw token is deliberately not carried on DDS
        self.session_id, self.session_epoch = session_id, session_epoch
        try:
            self._control = self._open()
            ticket = hello_auth_ticket(
                self.auth_key, transport="dds_v1", session_id=session_id,
                session_epoch=session_epoch,
                calibration_bundle_hash=self.bundle_hash if hasattr(self, "bundle_hash") else "",
                sender_id=self.sender_id, transport_epoch=self.transport_epoch)
        except (OSError, FrameError) as error:
            raise StreamError("dds_bridge_unavailable", str(error))
        # The bundle hash is supplied by the caller through bind_bundle() before
        # connect; refusing an empty one keeps the hello ticket meaningful.
        if not getattr(self, "bundle_hash", ""):
            self.close()
            raise StreamError("dds_bundle_missing", "bind the session bundle before connect")
        hello = {
            "op": "dds_hello", "transport": "dds_v1",
            "session_id": str(session_id), "session_epoch": int(session_epoch),
            "calibration_bundle_hash": self.bundle_hash,
            "sender_id": self.sender_id, "auth_key_id": self.key_id,
            "auth_ticket": ticket, "transport_epoch": self.transport_epoch,
        }
        try:
            response, _ = self._line_request(self._control, hello)
        except (OSError, ValueError, ConnectionError) as error:
            self.close()
            raise StreamError("dds_hello_failed", str(error))
        if response.get("ok") is not True:
            self.close()
            raise StreamError("dds_hello_refused", str(response.get("error") or response))
        # The bridge publishes the hello asynchronously to the car.  The status
        # stream can still contain the previous epoch's window for a few polls;
        # adopting that window makes every later ready window look foreign and
        # causes a false frame_admission_timeout.  Wait for a live sendable window
        # instead.  The car writes an immediate gate_closed window on hello and
        # turns command_transport_ready on once the authenticated licence and
        # driver are active.
        deadline = time.monotonic() + max(1.0, self.timeout_s * 2)
        while time.monotonic() < deadline:
            try:
                snapshot = self._status_snapshot()
            except (OSError, ValueError, ConnectionError) as error:
                self.last_error = f"window_poll:{type(error).__name__}:{error}"
                time.sleep(0.05)
                continue
            self._ingest_snapshot(snapshot)
            window = self._window
            if (window and str(window.get("session_id")) == str(session_id)
                    and int(window.get("session_epoch") or 0) == int(session_epoch)
                    and window.get("stream_epoch") is not None
                    and window.get("command_transport_ready") is True
                    and float(window.get("remaining_ms_conservative") or 0.0) > 0.0):
                self.stream_epoch = window["stream_epoch"]
                self.engaged = True
                self._start_threads()
                return {"stream_epoch": self.stream_epoch,
                        "frame_protocol_version": 1, "transport": "dds_v1",
                        "window": dict(window)}
            time.sleep(0.05)
        self.close()
        raise StreamError("dds_window_timeout",
                          "the car did not publish a freshness window for this epoch")

    def bind_bundle(self, bundle_hash: str) -> None:
        if not isinstance(bundle_hash, str) or not bundle_hash:
            raise StreamError("dds_bundle_missing", "a non-empty bundle hash is required")
        self.bundle_hash = bundle_hash

    def start_writer(self) -> None:
        """Threads start in connect(); kept for FrameLink's transport contract."""
        return None

    def _start_threads(self) -> None:
        self._stop.clear()
        self._writer = threading.Thread(target=self._writer_loop,
                                        name="dds-frame-writer", daemon=True)
        self._poller = threading.Thread(target=self._poll_loop,
                                        name="dds-bridge-poller", daemon=True)
        self._writer.start()
        self._poller.start()

    # -- snapshots/windows -------------------------------------------------
    def _ingest_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot
        window = snapshot.get("window")
        if isinstance(window, dict) and window.get("stream_epoch") is not None:
            if self.stream_epoch is None or str(window["stream_epoch"]) == str(self.stream_epoch):
                self._window = dict(window)
                self._window_mono_ns = self.clock_ns()
        receipts = snapshot.get("receipts")
        if isinstance(receipts, list):
            for item in receipts:
                if not isinstance(item, dict):
                    continue
                # The bridge keeps a bounded ring across sessions; a receipt from
                # another session or generation is not evidence for this link.
                if str(item.get("session_id")) != str(self.session_id):
                    continue
                if str(item.get("stream_epoch")) != str(self.stream_epoch):
                    continue
                key = (str(item.get("stream_epoch")), int(item.get("frame_seq") or 0),
                       str(item.get("state")), bool(item.get("emitted")))
                if key in self._seen_receipts:
                    continue
                self._seen_receipts.append(key)
                self._pending_receipts.append({
                    "state": ("stopped" if item.get("state") == "stopped"
                              else ("emitted" if item.get("emitted") is True
                                    else ("admitted" if item.get("admitted") is True
                                          else "refused"))),
                    "reason": str(item.get("reason") or ""),
                    "detail": str(item.get("detail") or ""),
                    "frame_seq": int(item.get("frame_seq") or 0),
                    "sample_seq": int(item.get("sample_seq") or 0),
                    "window_remaining_ms": float(item.get("window_remaining_ms") or 0.0),
                    "window_age_ms": float(item.get("window_age_ms") or 0.0),
                    "emitted": bool(item.get("emitted")),
                    "confirmation": ("emitted" if item.get("emitted") else
                                     ("gateway_accepted" if item.get("admitted") else "refused")),
                })
                self.counts["receipts"] += 1
        command = snapshot.get("bridge") if isinstance(snapshot.get("bridge"), dict) else {}
        if "command_enabled" in command:
            self.command_enabled = bool(command["command_enabled"])

    @property
    def current_window(self) -> dict[str, Any] | None:
        window = self._window
        stamp = self._window_mono_ns
        if not isinstance(window, dict) or stamp is None:
            return None
        age_ms = max(0.0, (self.clock_ns() - stamp) / 1_000_000.0)
        remaining = max(0.0, float(window.get("remaining_ms_conservative") or 0.0) - age_ms)
        return {"window_id": window.get("window_id"),
                "stream_epoch": window.get("stream_epoch"),
                "duration_ms": int(window.get("duration_ms") or 0),
                "window_remaining_ms": remaining,
                "command_transport_ready": bool(window.get("command_transport_ready"))}

    # -- sending -----------------------------------------------------------
    def submit_frame(self, frame: dict[str, Any]) -> None:
        if not self.engaged or self.closed:
            raise StreamError("dds_not_engaged", "connect the DDS transport first")
        if self.command_enabled is False:
            raise StreamError("dds_command_disabled", "the bridge has no command key")
        ticket = frame_auth_ticket(self.auth_key, frame, sender_id=self.sender_id,
                                   transport_epoch=self.transport_epoch)
        self._outbox = {"op": "dds_frame", "frame": copy.deepcopy(frame),
                        "sender_id": self.sender_id,
                        "transport_epoch": self.transport_epoch,
                        "auth_key_id": self.key_id, "auth_ticket": ticket}
        self.counts["frames_enqueued"] += 1
        self._outbox_event.set()

    def _writer_loop(self) -> None:
        while not self._stop.is_set():
            if not self._outbox_event.wait(0.1):
                continue
            self._outbox_event.clear()
            payload, self._outbox = self._outbox, None
            if payload is None:
                continue
            try:
                with self._control_lock:
                    if self._control is None:
                        raise ConnectionError("control socket closed")
                    response, _ = self._line_request(self._control, payload)
                if response.get("ok") is not True:
                    raise StreamError("dds_frame_refused",
                                      str(response.get("error") or response))
            except (OSError, ValueError, ConnectionError, StreamError) as error:
                self.counts["send_errors"] += 1
                self.last_error = f"{type(error).__name__}:{error}"
                self.engaged = False
                self._window = None
                self.close_control()
                # No fallback: the caller sees no window and stops sending.
            else:
                self.counts["frames_sent"] += 1
                self.last_error = None

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_period_s):
            try:
                snapshot = self._status_snapshot()
            except (OSError, ValueError, ConnectionError) as error:
                self.counts["poll_errors"] += 1
                self.last_error = f"poll:{type(error).__name__}:{error}"
                continue
            self.counts["polls"] += 1
            self._ingest_snapshot(snapshot)

    def pump(self, timeout_s: float = 0.0) -> None:
        del timeout_s  # the poller owns the cadence; pump never blocks a control cycle

    def drain_receipts(self) -> list[dict[str, Any]]:
        receipts = list(self._pending_receipts)
        self._pending_receipts.clear()
        return receipts

    def send(self, payload: dict[str, Any]) -> None:
        if payload.get("type") != "preempt":
            raise StreamError("unsupported_control_message", str(payload.get("type")))
        action = str(payload.get("action") or "")
        reason = str(payload.get("reason") or "")
        stream_epoch = str(self.stream_epoch or "")
        ticket = preempt_auth_ticket(
            self.auth_key, action=action, reason=reason,
            session_id=self.session_id, session_epoch=self.session_epoch,
            stream_epoch=stream_epoch, sender_id=self.sender_id)
        message = {"op": "dds_preempt", "action": action, "reason": reason,
                   "session_id": str(self.session_id),
                   "session_epoch": int(self.session_epoch or 0),
                   "stream_epoch": stream_epoch, "sender_id": self.sender_id,
                   "auth_key_id": self.key_id, "auth_ticket": ticket}
        try:
            sock = self._open()
            try:
                response, _ = self._line_request(sock, message)
            finally:
                sock.close()
        except (OSError, ValueError, ConnectionError) as error:
            self.counts["send_errors"] += 1
            self.last_error = f"preempt:{type(error).__name__}:{error}"
            raise StreamError("dds_preempt_failed", str(error))
        if response.get("ok") is not True:
            raise StreamError("dds_preempt_refused", str(response.get("error") or response))
        self.counts["preempts"] += 1

    # -- lifecycle ---------------------------------------------------------
    def close_control(self) -> None:
        sock, self._control = self._control, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def abort(self) -> None:
        self.engaged = False
        self.close_control()
        self._close_status()

    def close(self) -> None:
        self.closed = True
        self.engaged = False
        self._stop.set()
        self._outbox_event.set()
        for thread in (self._writer, self._poller):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=self.timeout_s + 0.2)
        self._writer = self._poller = None
        self.close_control()
        self._close_status()
        self._window = None

    def stats(self) -> dict[str, Any]:
        return {"transport": "dds_v1", "socket": self.socket_path,
                "sender_id": self.sender_id,
                "transport_epoch": self.transport_epoch,
                "engaged": self.engaged, "stream_epoch": self.stream_epoch,
                "command_enabled": self.command_enabled,
                "counts": dict(self.counts), "last_error": self.last_error}


class DdsFrameLink:
    """Factory wrapper that keeps FrameLink's contract and names the transport."""

    @staticmethod
    def build(*, socket_path: str, sender_id: str, transport_epoch: str,
              auth_key: bytes, session: dict[str, Any], ttl_ms: int = 150,
              window_margin_ms: float | None = None, status_cache=None,
              clock_ns=time.monotonic_ns):
        from .hybrid_frame_link import FrameLink

        client = DdsStreamClient(socket_path=socket_path, sender_id=sender_id,
                                 transport_epoch=transport_epoch, auth_key=auth_key)
        client.bind_bundle(session["calibration_bundle_hash"])
        return FrameLink(
            host="", port=0, session_id=session["session_id"],
            session_epoch=session["session_epoch"],
            bundle_hash=session["calibration_bundle_hash"], lease_token="",
            ttl_ms=ttl_ms,
            # The car's 250 ms window is still the safety gate. This is only the
            # sender-side skip threshold: the local bridge adds one poll interval
            # and one IPC trip before a frame reaches DDS, so the TCP 120 ms margin
            # would skip most frames even while the car's window is open.
            window_margin_ms=(60.0 if window_margin_ms is None else window_margin_ms),
            status_cache=status_cache, clock_ns=clock_ns, client=client,
            transport_name="dds_v1")
