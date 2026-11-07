"""Tests for the dependency-free rosbridge read-only source (no live ROS graph needed)."""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "lelab_mobile_agent"))

from lelab_mobile_agent.rosbridge_source import (  # noqa: E402
    RosbridgeSensorSource,
    WebSocketTextClient,
    WebSocketError,
)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def server_frame(payload: bytes, opcode: int = 0x1, fin: bool = True) -> bytes:
    head = bytearray([(0x80 if fin else 0x00) | opcode])
    length = len(payload)
    if length < 126:
        head.append(length)
    elif length < 65536:
        head.append(126)
        head += struct.pack("!H", length)
    else:
        head.append(127)
        head += struct.pack("!Q", length)
    return bytes(head) + payload


class FakeRosbridge:
    """Single-connection fake rosbridge used to exercise the client framing and reconnects."""

    def __init__(self, script):
        self.script = script
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.port = self.listener.getsockname()[1]
        self.received: list[str] = []
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.listener.close()

    def _run(self):
        self.listener.settimeout(0.4)
        while not self._stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                self.connections += 1
                self._serve(conn)

    def _serve(self, conn: socket.socket) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        key = ""
        for line in data.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"sec-websocket-key":
                key = value.strip().decode()
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        conn.sendall(
            ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
             f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode()
        )
        conn.settimeout(0.4)
        leftover = data.split(b"\r\n\r\n", 1)[1]
        for action in self.script:
            kind = action[0]
            if kind == "send_text":
                conn.sendall(server_frame(json.dumps(action[1]).encode()))
            elif kind == "send_fragmented_text":
                parts = action[1]
                for index, part in enumerate(parts):
                    conn.sendall(server_frame(part.encode(), opcode=0x1 if index == 0 else 0x0,
                                              fin=index == len(parts) - 1))
            elif kind == "send_ping":
                conn.sendall(server_frame(action[1], opcode=0x9))
            elif kind == "expect_text":
                # wall-clock budget, not a countdown of recv timeouts: under a loaded
                # test session the client thread can be descheduled far beyond one tick
                deadline = time.monotonic() + 10.0
                buffer = leftover
                leftover = b""
                while time.monotonic() < deadline:
                    try:
                        chunk = conn.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise AssertionError("client closed before sending expected text")
                    buffer += chunk
                    message, consumed = decode_client_frame(buffer)
                    if message is None:
                        continue
                    buffer = buffer[consumed:]
                    self.received.append(message)
                    break
                else:
                    raise AssertionError("timeout waiting for client text frame")
            elif kind == "close":
                conn.sendall(server_frame(b"", opcode=0x8))
                return


def decode_client_frame(raw: bytes) -> tuple[str | None, int]:
    if len(raw) < 2:
        return None, 0
    length = raw[1] & 0x7F
    offset = 2
    if length == 126:
        if len(raw) < offset + 2:
            return None, 0
        length = struct.unpack("!H", raw[offset:offset + 2])[0]
        offset += 2
    elif length == 127:
        if len(raw) < offset + 8:
            return None, 0
        length = struct.unpack("!Q", raw[offset:offset + 8])[0]
        offset += 8
    if len(raw) < offset + 4 + length:
        return None, 0
    key = raw[offset:offset + 4]
    payload = raw[offset + 4:offset + 4 + length]
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return masked.decode(), offset + 4 + length


class WebSocketClientTest(unittest.TestCase):
    def test_handshake_send_and_receive(self):
        payload = {"op": "publish", "topic": "/imu", "msg": {"header": {"stamp": {"sec": 1, "nanosec": 2}}}}
        server = FakeRosbridge([("expect_text",), ("send_text", payload)])
        server.start()
        self.addCleanup(server.stop)
        client = WebSocketTextClient(server.url, connect_timeout=3.0, read_timeout=3.0)
        client.open()
        self.addCleanup(client.close)
        client.send_text(json.dumps({"op": "subscribe", "id": "x", "topic": "/imu", "type": "sensor_msgs/msg/Imu"}))
        self.assertEqual(json.loads(client.recv_text()), payload)
        self.assertEqual(json.loads(server.received[0])["op"], "subscribe")

    def test_large_fragmented_message_and_ping_pong(self):
        big = "y" * 70_000
        server = FakeRosbridge([
            ("send_ping", b"hb"),
            ("send_fragmented_text", [big[:30_000], big[30_000:]]),
        ])
        server.start()
        self.addCleanup(server.stop)
        client = WebSocketTextClient(server.url, connect_timeout=3.0, read_timeout=3.0)
        client.open()
        self.addCleanup(client.close)
        self.assertEqual(client.recv_text(), big)

    def test_rejects_non_101_handshake(self):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def refuse():
            conn, _ = listener.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")

        threading.Thread(target=refuse, daemon=True).start()
        client = WebSocketTextClient(f"ws://127.0.0.1:{listener.getsockname()[1]}", read_timeout=3.0)
        with self.assertRaises(WebSocketError):
            client.open()
        client.close()


class RosbridgeSourceTest(unittest.TestCase):
    def test_receives_frames_and_reconnects_after_close(self):
        message = {"op": "publish", "topic": "/imu", "id": "remote_0",
                   "msg": {"header": {"stamp": {"sec": 7, "nanosec": 500}, "frame_id": "imu_link"}}}
        server = FakeRosbridge([
            ("expect_text",), ("send_text", message), ("send_text", message), ("close",),
            ("expect_text",), ("send_text", message),
        ])
        server.start()
        self.addCleanup(server.stop)
        seen: list[tuple[str, dict]] = []
        source = RosbridgeSensorSource(
            server.url, [("remote_0", "/imu", "sensor_msgs/msg/Imu")],
            lambda topic, values: seen.append((topic, values)), reconnect_s=0.2, read_timeout=2.0,
        )
        source.start()
        self.addCleanup(source.stop)
        deadline = 12.0
        while source.frames < 3 and deadline > 0:
            threading.Event().wait(0.2)
            deadline -= 0.2
        state = source.state()
        self.assertGreaterEqual(len(seen), 3)
        self.assertEqual(seen[0][0], "/imu")
        self.assertEqual(seen[0][1]["header"]["frame_id"], "imu_link")
        self.assertGreaterEqual(state["connects"], 2, state)
        self.assertEqual(state["topics"], ["/imu"])
        self.assertIsNotNone(state["last_receive_age_s"])

    def test_counts_malformed_payloads_as_dropped(self):
        server = FakeRosbridge([("expect_text",), ("send_text", {"op": "service_response", "id": "other"})])
        server.start()
        self.addCleanup(server.stop)
        source = RosbridgeSensorSource(
            server.url, [("remote_0", "/imu", "sensor_msgs/msg/Imu")], lambda *_: None,
            reconnect_s=0.2, read_timeout=2.0,
        )
        source.start()
        self.addCleanup(source.stop)
        deadline = 6.0
        while source.dropped < 1 and deadline > 0:
            threading.Event().wait(0.2)
            deadline -= 0.2
        self.assertGreaterEqual(source.dropped, 1)
        self.assertEqual(source.frames, 0)


class RemoteDecodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from lelab_mobile_agent.agent import _decode_remote_message  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"rclpy message types unavailable: {exc}")

    def test_image_camera_info_joint_odom_imu(self):
        from lelab_mobile_agent.agent import _decode_remote_message

        raw = bytes(range(24))
        image = _decode_remote_message("rgb", {
            "header": {"stamp": {"sec": 3, "nanosec": 4}, "frame_id": "rgb_optical_frame"},
            "height": 2, "width": 4, "encoding": "bgr8", "is_bigendian": 0, "step": 12,
            "data": base64.b64encode(raw).decode(),
        })
        self.assertEqual((image.height, image.width, image.step), (2, 4, 12))
        self.assertEqual(image.encoding, "bgr8")
        self.assertEqual(bytes(image.data), raw)
        self.assertEqual(image.header.stamp.sec, 3)
        self.assertEqual(image.header.frame_id, "rgb_optical_frame")

        info = _decode_remote_message("camera_info", {
            "header": {"stamp": {"sec": 1, "nanosec": 1}}, "height": 2, "width": 4,
            "distortion_model": "plumb_bob", "d": [0.0], "k": [500.0, 0.0, 2.0],
            "p": [500.0, 0.0, 2.0, 0.0, 0.0, 500.0, 1.0, 0.0],
        })
        self.assertEqual(len(info.k), 9)
        self.assertEqual(list(info.k[:3]), [500.0, 0.0, 2.0])
        self.assertEqual(len(info.p), 12)
        self.assertEqual(list(info.d), [0.0])

        joints = _decode_remote_message("joint_state", {
            "header": {"stamp": {"sec": 9, "nanosec": 0}},
            "name": ["joint1", "gripper_base_joint"], "position": [0.1, -0.2], "velocity": [], "effort": [],
        })
        self.assertEqual(list(joints.name), ["joint1", "gripper_base_joint"])
        self.assertEqual(list(joints.position), [0.1, -0.2])

        odom = _decode_remote_message("odom", {
            "header": {"stamp": {"sec": 2, "nanosec": 3}}, "child_frame_id": "base_link",
            "pose": {"pose": {"position": {"x": 1.0, "y": 2.0, "z": 3.0},
                              "orientation": {"w": 1.0, "x": 0.0, "y": 0.0, "z": 0.0}}, "covariance": []},
            "twist": {"twist": {"linear": {"x": 0.5}, "angular": {"z": -0.25}}, "covariance": []},
        })
        self.assertEqual(odom.child_frame_id, "base_link")
        self.assertEqual(odom.pose.pose.position.y, 2.0)
        self.assertEqual(odom.twist.twist.linear.x, 0.5)
        self.assertEqual(odom.twist.twist.angular.z, -0.25)
        self.assertEqual(len(odom.pose.covariance), 36)

        imu = _decode_remote_message("imu", {
            "header": {"stamp": {"sec": 5, "nanosec": 6}},
            "orientation": {"w": 1.0, "x": 0.0, "y": 0.0, "z": 0.0},
            "angular_velocity": {"x": 0.1, "y": 0.2, "z": 0.3},
            "linear_acceleration": {"x": 0.0, "y": 0.0, "z": 9.8},
        })
        self.assertEqual(imu.angular_velocity.y, 0.2)
        self.assertEqual(imu.linear_acceleration.z, 9.8)
        self.assertEqual(len(imu.linear_acceleration_covariance), 9)

    def test_unknown_kind_raises(self):
        from lelab_mobile_agent.agent import _decode_remote_message

        with self.assertRaises(ValueError):
            _decode_remote_message("wrench", {})


if __name__ == "__main__":
    unittest.main()
