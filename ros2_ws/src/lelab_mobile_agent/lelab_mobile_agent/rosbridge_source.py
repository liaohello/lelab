"""Dependency-free read-only rosbridge client (RFC 6455 text frames, stdlib only).

Used as an observation-only sensor source when the car's ROS graph is reachable over
rosbridge but the agent process cannot join the car's DDS domain. It never advertises a
publisher and never calls a service other than rosapi read-only introspection.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME_BYTES = 32 * 1024 * 1024


class WebSocketError(RuntimeError):
    pass


def _mask(payload: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


class WebSocketTextClient:
    """Minimal blocking WebSocket client that speaks text frames to one rosbridge."""

    def __init__(self, url: str, connect_timeout: float = 10.0, read_timeout: float = 10.0):
        parts = urlsplit(url)
        if parts.scheme != "ws":
            raise WebSocketError(f"unsupported scheme in {url!r}")
        self.host = parts.hostname or ""
        self.port = parts.port or 80
        self.path = parts.path or "/"
        if parts.query:
            self.path += "?" + parts.query
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.sock: socket.socket | None = None
        self._rx = bytearray()

    def open(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        sock.settimeout(self.read_timeout)
        try:
            sock.sendall(request)
            header, leftover = self._read_until_header(sock)
            status_line = header.split(b"\r\n", 1)[0]
            if b" 101 " not in status_line:
                raise WebSocketError(f"handshake rejected: {status_line!r}")
            accept = None
            for line in header.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"sec-websocket-accept":
                    accept = value.strip()
            expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            if accept != expected.encode():
                raise WebSocketError("handshake accept key mismatch")
        except Exception:
            sock.close()
            raise
        self._rx = bytearray(leftover)
        self.sock = sock

    @staticmethod
    def _read_until_header(sock: socket.socket) -> tuple[bytes, bytes]:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            data += chunk
            if len(data) > 64 * 1024:
                raise WebSocketError("handshake header too large")
        header, _, leftover = data.partition(b"\r\n\r\n")
        return header, leftover

    def send_text(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        key = os.urandom(4)
        self._require().sendall(bytes(header) + key + _mask(payload, key))

    def recv_text(self) -> str:
        chunks: list[bytes] = []
        total = 0
        while True:
            opcode, fin, payload = self._read_frame()
            if opcode == 0x8:
                raise WebSocketError("server sent close frame")
            if opcode == 0x9:
                self._send_control(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode not in (0x0, 0x1, 0x2):
                raise WebSocketError(f"unsupported opcode {opcode}")
            total += len(payload)
            if total > MAX_FRAME_BYTES:
                raise WebSocketError("message exceeds 32 MiB limit")
            chunks.append(payload)
            if fin:
                return b"".join(chunks).decode("utf-8", errors="replace")

    def _read_frame(self) -> tuple[int, bool, bytes]:
        sock = self._require()
        head = self._read_exact(sock, 2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(sock, 8))[0]
        if length > MAX_FRAME_BYTES:
            raise WebSocketError("frame exceeds 32 MiB limit")
        key = self._read_exact(sock, 4) if masked else b""
        payload = self._read_exact(sock, length) if length else b""
        if masked:
            payload = _mask(payload, key)
        return opcode, fin, payload

    def _read_exact(self, sock: socket.socket, count: int) -> bytes:
        while len(self._rx) < count:
            chunk = sock.recv(max(count - len(self._rx), 4096))
            if not chunk:
                raise WebSocketError("connection closed mid-frame")
            self._rx += chunk
        data = bytes(self._rx[:count])
        del self._rx[:count]
        return data

    def _send_control(self, opcode: int, payload: bytes) -> None:
        key = os.urandom(4)
        frame = bytes([0x80 | opcode, 0x80 | len(payload)]) + key + _mask(payload, key)
        self._require().sendall(frame)

    def _require(self) -> socket.socket:
        if self.sock is None:
            raise WebSocketError("socket is not open")
        return self.sock

    def close(self) -> None:
        if self.sock is not None:
            try:
                self._send_control(0x8, struct.pack("!H", 1000))
            except Exception:  # noqa: BLE001
                pass
            self.sock.close()
            self.sock = None
        self._rx = bytearray()


class RosbridgeSensorSource:
    """Keep read-only subscriptions alive on one rosbridge and hand messages to a callback."""

    def __init__(
        self,
        url: str,
        subscriptions: list[tuple[str, str, str]],
        handler: Callable[[str, dict], None],
        reconnect_s: float = 2.0,
        read_timeout: float = 10.0,
    ):
        self.url = url
        self.subscriptions = subscriptions
        self.handler = handler
        self.reconnect_s = reconnect_s
        self.read_timeout = read_timeout
        self.lock = threading.Lock()
        self.frames = 0
        self.dropped = 0
        self.connects = 0
        self.last_receive_monotonic = 0.0
        self.connected = False
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._client: WebSocketTextClient | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="rosbridge-source")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        client = self._client
        if client is not None:
            client.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            client = WebSocketTextClient(self.url, read_timeout=self.read_timeout)
            self._client = client
            try:
                client.open()
                with self.lock:
                    self.connects += 1
                    self.connected = True
                    self.last_error = None
                for sub_id, topic, msg_type in self._subscription_ops():
                    client.send_text(json.dumps(
                        {"op": "subscribe", "id": sub_id, "topic": topic, "type": msg_type}
                    ))
                while not self._stop.is_set():
                    self._dispatch(client.recv_text())
            except Exception as exc:  # noqa: BLE001
                with self.lock:
                    self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                client.close()
                self._client = None
                with self.lock:
                    self.connected = False
            if not self._stop.is_set():
                time.sleep(self.reconnect_s)

    def _subscription_ops(self):
        return self.subscriptions

    def _dispatch(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            msg = None
        if not isinstance(msg, dict) or msg.get("op") != "publish":
            with self.lock:
                self.dropped += 1
            return
        values = msg.get("msg")
        topic = msg.get("topic")
        if not isinstance(values, dict):
            with self.lock:
                self.dropped += 1
            return
        with self.lock:
            self.frames += 1
            self.last_receive_monotonic = time.monotonic()
        self.handler(topic, values)

    def state(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        with self.lock:
            age = None if not self.last_receive_monotonic else round(now - self.last_receive_monotonic, 3)
            return {
                "backend": "rosbridge",
                "url": self.url,
                "connected": self.connected,
                "connects": self.connects,
                "frames": self.frames,
                "dropped": self.dropped,
                "last_receive_age_s": age,
                "last_error": self.last_error,
                "topics": [topic for _, topic, _ in self.subscriptions],
            }
