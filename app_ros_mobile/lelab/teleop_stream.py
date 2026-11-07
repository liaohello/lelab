"""Length-prefixed teleop frame stream: upper computer <-> car control process.

Pure Python 3.10, stdlib only, so the same file runs on the upper computer and on
the Pi (and the car-side copy is a symlink, guarded by a hash test).  It is the
long-lived transport that replaces the per-cycle HTTP round trips; ``teleop_frame_v1``
decides whether a frame is admissible, this module only moves it.

Deliberate properties
---------------------
* ``127.0.0.1`` only, and no authentication of its own: the caller presents the
  car control-lease token it already holds, and the control process compares its
  digest against the licence mirror published by the agent.  No second lease.
* one control connection at a time.  A new hello closes the previous generation;
  frames buffered in a socket cannot be admitted after a reconnect because the
  stream epoch they carry has been retired.
* the writer keeps at most one frame.  A stalled link drops the older candidate
  instead of queueing a trajectory, and every drop is counted.
* receipts are asynchronous: nothing in the send path waits for the car.
"""
from __future__ import annotations

import errno
import hashlib
import hmac
import json
import select
import socket
import struct
import threading
import time
from typing import Any, Callable

from .teleop_frame_v1 import FRAME_PROTOCOL_VERSION, monotonic_ns

STREAM_PROTOCOL_VERSION = 1
LENGTH_PREFIX = ">I"
MAX_MESSAGE_BYTES = 64 * 1024
DEFAULT_PORT = 18121
HELLO_TIMEOUT_S = 5.0
MS_TO_S = 0.001


class StreamError(RuntimeError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def encode(message: dict[str, Any]) -> bytes:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise StreamError("message_too_large", f"{len(payload)} bytes exceeds the cap")
    return struct.pack(LENGTH_PREFIX, len(payload)) + payload


def _read_exactly(sock: socket.socket, count: int, timeout_s: float) -> bytes:
    """Collect exactly count bytes across short reads; raises on timeout or EOF."""
    chunks: list[bytes] = []
    got = 0
    sock.settimeout(timeout_s)
    while got < count:
        try:
            part = sock.recv(count - got)
        except (TimeoutError, socket.timeout) as exc:
            raise StreamError("read_timeout", f"needed {count}, have {got}") from exc
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            raise StreamError("read_error", str(exc)) from exc
        if not part:
            raise StreamError("closed", "peer closed mid-message")
        chunks.append(part)
        got += len(part)
    return b"".join(chunks)


class MessageReader:
    """Buffered reader that survives partial reads and coalesced messages.

    A length prefix can arrive split across packets, and two messages can arrive in
    one packet.  Reading a header and then waiting for the body is not enough: if the
    body does not come before the timeout, the consumed header is lost and every later
    parse in that direction is misaligned.  The buffer is kept per direction here, so
    a timeout costs a retry, not the stream.
    """

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buffer = bytearray()
        self.closed = False

    def _fill(self, timeout_s: float) -> bool:  # noqa: PLR0912
        """Wait up to timeout_s for bytes; False when nothing arrived in time."""
        remaining = max(0.0, timeout_s)
        while True:
            ready, _, _ = select.select([self.sock], [], [], min(remaining, 0.05) if remaining else 0.0)
            if ready:
                try:
                    chunk = self.sock.recv(65536)
                except (TimeoutError, socket.timeout):
                    return False
                except OSError as exc:
                    if exc.errno == errno.EINTR:
                        continue
                    self.closed = True
                    raise StreamError("read_error", str(exc)) from exc
                if not chunk:
                    self.closed = True
                    raise StreamError("closed", "peer closed the connection")
                self.buffer.extend(chunk)
                return True
            if remaining <= 0.0:
                return False
            remaining -= 0.05
            if remaining <= 0.0:
                return False

    def _take(self) -> dict[str, Any] | None:
        if len(self.buffer) < 4:
            return None
        (length,) = struct.unpack(LENGTH_PREFIX, bytes(self.buffer[:4]))
        if length == 0 or length > MAX_MESSAGE_BYTES:
            self.buffer.clear()
            raise StreamError("bad_length", f"declared {length} bytes")
        if len(self.buffer) < 4 + length:
            return None
        body = bytes(self.buffer[4:4 + length])
        del self.buffer[:4 + length]
        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StreamError("malformed_json", str(exc)) from exc
        if not isinstance(message, dict):
            raise StreamError("malformed_json", "a message must be an object")
        return message

    def read(self, timeout_s: float) -> dict[str, Any]:
        """Block until one complete message is available, or the deadline passes."""
        deadline = time.monotonic() + timeout_s
        while True:
            message = self._take()
            if message is not None:
                return message
            if time.monotonic() >= deadline:
                raise StreamError("read_timeout", "no complete message before the deadline")
            self._fill(max(0.0, deadline - time.monotonic()))

    def read_available(self) -> list[dict[str, Any]]:
        """Everything already parsed, without waiting."""
        out: list[dict[str, Any]] = []
        while True:
            message = self._take()
            if message is None:
                break
            out.append(message)
        return out

def read_message(sock: socket.socket, timeout_s: float = 1.0) -> dict[str, Any]:
    """One-shot helper kept for callers without a persistent reader."""
    header = _read_exactly(sock, 4, timeout_s)
    (length,) = struct.unpack(LENGTH_PREFIX, header)
    if length == 0 or length > MAX_MESSAGE_BYTES:
        raise StreamError("bad_length", f"declared {length} bytes")
    body = _read_exactly(sock, length, timeout_s)
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StreamError("malformed_json", str(exc)) from exc
    if not isinstance(message, dict):
        raise StreamError("malformed_json", "a message must be an object")
    return message


def token_digest(token: str) -> str:
    """The licence carries only this digest, never the token itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str, digest: str | None) -> bool:
    """Constant-time on the digests: the token itself never leaves the upper computer."""
    if not token or not digest:
        return False
    return hmac.compare_digest(token_digest(token), digest)


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------
class StreamClient:
    """One-writer / one-reader client that never queues more than one frame."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, *,
                 timeout_s: float = 1.0, clock_ns: Callable[[], int] = monotonic_ns):
        self.address = (host, port)
        self.timeout_s = timeout_s
        self._clock = clock_ns
        self._sock: socket.socket | None = None
        self._reader: MessageReader | None = None
        self._send_lock = threading.Lock()
        self._recv_lock = threading.Lock()
        self._outbox: dict[str, Any] | None = None
        self._wake = threading.Event()
        self._writer: threading.Thread | None = None
        self._receipts: list[dict[str, Any]] = []
        self._window: dict[str, Any] | None = None
        self._window_received_ns: int | None = None
        self._stop_writer = threading.Event()
        self.stream_epoch: int | None = None
        self.counters = {"frames_sent": 0, "frames_dropped": 0, "reconnects": 0,
                         "receipts": 0, "windows": 0}

    # -- connection --------------------------------------------------------
    def connect(self, *, lease_token: str, session_id, session_epoch,
                supported: tuple[int, ...] = (FRAME_PROTOCOL_VERSION,),
                capabilities: tuple[str, ...] = ("teleop_frame_v1",)) -> dict[str, Any]:
        if not lease_token:
            raise StreamError("no_lease", "the stream authenticates with an existing lease")
        self._stop_writer.clear()
        try:
            sock = socket.create_connection(self.address, timeout=self.timeout_s)
        except OSError as exc:
            raise StreamError("connect_failed", str(exc)) from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        self._reader = MessageReader(sock)
        self.counters["reconnects"] += 1
        self.send({"type": "hello", "protocol": STREAM_PROTOCOL_VERSION,
                   "lease_token": lease_token, "session_id": session_id,
                   "session_epoch": session_epoch, "frame_protocols": list(supported),
                   "capabilities": list(capabilities)})
        welcome = self.recv(HELLO_TIMEOUT_S)
        if welcome.get("type") != "welcome":
            # Surface the server's own reason: "not_authorised" and "stale_lease"
            # are different problems for the operator, "hello_refused" is neither.
            reason = str(welcome.get("reason") or "hello_refused")
            raise StreamError(reason, str(welcome.get("detail") or ""))
        self.stream_epoch = welcome.get("stream_epoch")
        self._window = welcome.get("window")
        self._window_received_ns = self._clock() if self._window else None
        return welcome

    def abort(self) -> None:
        """Discard pending output and interrupt the socket without joining."""
        self._stop_writer.set()
        self._outbox = None
        self._wake.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def close(self) -> None:
        # Interrupt a blocked write before joining, without retaining an outbox
        # across connections.
        self.abort()
        if self._writer is not None and self._writer is not threading.current_thread():
            self._writer.join(timeout=2.0)
        self._writer = None
        self._window = None
        self._window_received_ns = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # -- messages ----------------------------------------------------------
    def send(self, message: dict[str, Any]) -> None:
        # Read the socket once: abort() can clear it between the check and the write,
        # and an AttributeError there would kill the writer thread instead of becoming
        # the send_failure the caller already knows how to count.
        sock = self._sock
        if sock is None:
            raise StreamError("not_connected", "call connect() first")
        payload = encode(message)
        with self._send_lock:
            try:
                sock.sendall(payload)
            except OSError as exc:
                raise StreamError("send_failed", str(exc)) from exc

    def start_writer(self) -> None:
        """Hand sending to its own thread so a stalled link cannot stall the worker."""
        if self._writer is not None and self._writer.is_alive():
            return
        self._writer = threading.Thread(target=self._write_loop, name="teleop-stream-writer",
                                        daemon=True)
        self._writer.start()

    def _write_loop(self) -> None:
        # The event is cleared before the slot is taken.  Leaving it set would turn
        # this thread into a busy loop that steals the interpreter lock from the
        # caller at every opportunity - which is exactly how a 10 Hz sender ends up
        # stamping frames that arrive after their own freshness window has closed.
        while not self._stop_writer.is_set():
            if not self._wake.wait(timeout=0.05):
                continue
            self._wake.clear()
            message, self._outbox = self._outbox, None
            if message is None:
                continue
            try:
                self.send(message)
                self.counters["frames_sent"] += 1
            except StreamError:
                self.counters["send_failures"] = self.counters.get("send_failures", 0) + 1

    def submit_frame(self, frame: dict[str, Any]) -> bool:
        """Never blocks, and never queues: the newest frame replaces the unsent one."""
        message = {"type": "frame", **frame}
        if self._outbox is not None:
            # The previous candidate is still unwritten: it is superseded, not stored.
            self.counters["frames_dropped"] += 1
        self._outbox = message
        self._wake.set()
        return True

    def _note(self, message: dict[str, Any]) -> dict[str, Any]:
        kind = message.get("type")
        if kind == "window":
            self._window = message
            self._window_received_ns = self._clock()
            self.counters["windows"] += 1
        elif kind == "receipt":
            self.counters["receipts"] += 1
            with self._recv_lock:
                self._receipts.append(message)
                if len(self._receipts) > 256:
                    del self._receipts[:-256]
        return message

    def recv(self, timeout_s: float | None = None) -> dict[str, Any]:
        if self._reader is None:
            raise StreamError("not_connected", "call connect() first")
        return self._note(self._reader.read(self.timeout_s if timeout_s is None else timeout_s))

    def pump(self, timeout_s: float = 0.0) -> int:
        """Take what is already on the wire without blocking the caller."""
        if self._reader is None:
            return 0
        # timeout_s == 0 still has to drain the socket: _fill(0) is a non-blocking
        # read, and skipping it would mean never seeing what the peer already sent.
        self._reader._fill(timeout_s)
        messages = self._reader.read_available()
        for message in messages:
            self._note(message)
        return len(messages)

    @property
    def current_window(self) -> dict[str, Any] | None:
        """None until a real window exists; an empty record is not an admission."""
        if not self._window or self._window.get("window_id") is None:
            return None
        window = dict(self._window)
        remaining = window.get("window_remaining_ms")
        if (self._window_received_ns is not None and isinstance(remaining, (int, float))
                and not isinstance(remaining, bool)):
            elapsed_ms = max(0, self._clock() - self._window_received_ns) / 1_000_000
            window["window_remaining_ms"] = max(0.0, float(remaining) - elapsed_ms)
        return window

    @property
    def current_window_id(self):
        return None if self._window is None else self._window.get("window_id")

    def drain_receipts(self) -> list[dict[str, Any]]:
        with self._recv_lock:
            out, self._receipts = list(self._receipts), []
        return out

    def stats(self) -> dict[str, Any]:
        return {"connected": self.connected, "stream_epoch": self.stream_epoch,
                "counters": dict(self.counters), "waiting": 0 if self._outbox is None else 1}


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------
class StreamServer:
    """Car-side single-connection frame receiver.

    ``handlers`` supplies the policy; this class only enforces framing, size,
    timeouts, authentication against the licence mirror and generation rotation:

      on_hello(session_id, session_epoch, lease_token) -> (ok, detail)
      on_frame(frame) -> receipt dict
      on_preempt(action, reason) -> receipt dict
      window_provider() -> the window record the control side is currently issuing
    """

    def __init__(self, handlers: dict[str, Callable], *, host: str = "127.0.0.1",
                 port: int = DEFAULT_PORT, idle_timeout_s: float = 1.0,
                 send_timeout_s: float = 1.0, max_idle_rounds: int = 50,
                 clock_ns: Callable[[], int] = monotonic_ns):
        if host != "127.0.0.1":
            raise StreamError("listener_not_loopback", "the control stream binds loopback only")
        self._handlers = handlers
        self.address = (host, port)
        self.idle_timeout_s = idle_timeout_s
        self.send_timeout_s = send_timeout_s
        self.max_idle_rounds = max(1, int(max_idle_rounds))
        self._clock = clock_ns
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._client: socket.socket | None = None
        self._generation = 0
        self._last_hello: dict[str, Any] | None = None
        self.push_failures = 0
        self.max_push_failures = 20
        self.counters = {"connections": 0, "frames": 0, "refused_hellos": 0,
                         "malformed": 0, "preempts": 0, "push_failures": 0,
                         "handler_errors": 0}
        self.last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> int:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._listener.bind(self.address)
        except OSError as exc:
            self._listener.close()
            self._listener = None
            raise StreamError("bind_failed", f"{self.address}: {exc}") from exc
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="teleop-stream", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self._stop.set()
        client = self._client
        if client is not None:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._listener = None
        self._client = None

    # -- loop --------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._accept()
            except StreamError as exc:
                if exc.reason == "stopping":
                    break
                self.last_error = exc.reason
                continue
            self.counters["connections"] += 1
            # A new connection retires the previous generation immediately, so a
            # frame that sat in the old socket can never be admitted again.
            self._generation += 1
            try:
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                # A blocking send to a sender that has stopped reading would wedge this
                # thread - and with it the only path that accepts the next connection.
                # Writes are bounded instead, and a timeout drops the client.
                client.settimeout(self.send_timeout_s)
            except OSError:
                pass
            self._client = client
            try:
                self._serve(client)
            except StreamError as exc:
                self.last_error = exc.reason
            except OSError as exc:
                self.last_error = f"oserror:{exc.errno}"
            finally:
                try:
                    client.close()
                except OSError:
                    pass
                self._client = None

    def _accept(self) -> tuple[socket.socket, Any]:
        if self._listener is None:
            raise StreamError("stopping", "the server is already closed")
        self._listener.settimeout(self.idle_timeout_s)
        try:
            return self._listener.accept()
        except TimeoutError:
            raise StreamError("idle") from None
        except socket.timeout:
            raise StreamError("idle") from None
        except OSError as exc:
            raise StreamError("stopping" if self._stop.is_set() else "accept_failed",
                              str(exc)) from exc

    def _serve(self, client: socket.socket) -> None:
        reader = MessageReader(client)
        hello = reader.read(HELLO_TIMEOUT_S)
        if hello.get("type") != "hello":
            self.counters["refused_hellos"] += 1
            self._send(client, {"type": "error", "reason": "hello_required"})
            raise StreamError("hello_required", str(hello.get("type")))
        check = self._handlers["on_hello"](hello.get("session_id"), hello.get("session_epoch"),
                                           hello.get("lease_token") or "")
        ok, detail = (check[0], check[1]) if isinstance(check, (tuple, list)) else (check, "")
        if not ok:
            self.counters["refused_hellos"] += 1
            self._send(client, {"type": "error", "reason": "not_authorised", "detail": detail})
            raise StreamError("not_authorised", detail)
        self._last_hello = dict(hello)
        self._send(client, {"type": "welcome", "protocol": STREAM_PROTOCOL_VERSION,
                            "frame_protocol_version": FRAME_PROTOCOL_VERSION,
                            # The generation the admission will actually honour, which
                            # is the window issuer's counter, not this server's own.
                            "stream_epoch": self._handlers.get(
                                "stream_epoch_provider", lambda: self._generation)(),
                            "transport": "frame_stream",
                            "window": self._handlers["window_provider"](),
                            "server_time_ns": self._clock()})
        idle_rounds = 0
        while not self._stop.is_set():
            try:
                message = reader.read(self.idle_timeout_s)
            except StreamError as exc:
                if exc.reason == "read_timeout":
                    # A silent sender has stopped producing frames.  Holding the only
                    # control connection open would leave the next sender out in the
                    # cold, so the session is dropped after a bounded silence and the
                    # server returns to accept().
                    idle_rounds += 1
                    if idle_rounds >= self.max_idle_rounds:
                        self.last_error = "idle_client_evicted"
                        return
                    continue
                if exc.reason == "closed":
                    return
                self.counters["malformed"] += 1
                self._send(client, {"type": "error", "reason": exc.reason})
                continue
            idle_rounds = 0
            try:
                self._dispatch(client, message)
            except StreamError:
                raise
            except Exception as error:      # a policy bug must not kill the loop
                self.counters["handler_errors"] = self.counters.get("handler_errors", 0) + 1
                self.last_error = f"handler_error:{type(error).__name__}"
                try:
                    self._send(client, {"type": "error", "reason": "handler_error",
                                        "detail": type(error).__name__})
                except StreamError:
                    return

    def _dispatch(self, client: socket.socket, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "frame":
            self.counters["frames"] += 1
            frame = {key: value for key, value in message.items() if key != "type"}
            self._send(client, self._handlers["on_frame"](frame))
        elif kind == "preempt":
            self.counters["preempts"] += 1
            self._send(client, self._handlers["on_preempt"](message.get("action"),
                                                            message.get("reason")))
        elif kind == "ping":
            self._send(client, {"type": "pong", "server_time_ns": self._clock(),
                                "stream_epoch": self._handlers.get(
                                    "stream_epoch_provider", lambda: self._generation)()})
        elif kind == "bye":
            raise StreamError("peer_bye", "client closed the session")
        else:
            self.counters["malformed"] += 1
            self._send(client, {"type": "error", "reason": "unknown_message_type",
                                "got": str(kind)})

    def _send(self, client: socket.socket, message: dict[str, Any]) -> None:
        try:
            with self._send_lock:
                client.sendall(encode(message))
        except (TimeoutError, socket.timeout) as exc:
            raise StreamError("send_timeout", "peer stopped draining receipts") from exc
        except OSError as exc:
            raise StreamError("send_failed", str(exc)) from exc

    # -- server push -------------------------------------------------------
    def push_window(self, window: dict[str, Any]) -> None:
        self._push({"type": "window", **window})

    def push(self, message: dict[str, Any]) -> None:
        self._push(message)

    def _push(self, message: dict[str, Any]) -> None:
        """Server-initiated write from the control thread; never blocks that thread.

        Losing a window push is not a failure on its own, but a peer that stops
        reading has to be dropped rather than left holding the only control slot.
        """
        client = self._client
        if client is None:
            return
        try:
            with self._send_lock:
                client.sendall(encode(message))
            self.push_failures = 0
        except (OSError, OverflowError):
            self.push_failures += 1
            self.counters["push_failures"] = self.push_failures
            self.last_error = "push_failed"
            if self.push_failures >= self.max_push_failures and client is not None:
                self.last_error = "client_dropped_after_push_failures"
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def state(self) -> dict[str, Any]:
        return {"generation": self._generation, "client_connected": self._client is not None,
                "counters": dict(self.counters), "last_error": self.last_error,
                "hello": None if self._last_hello is None else
                        {key: value for key, value in self._last_hello.items()
                         if key != "lease_token"}}
