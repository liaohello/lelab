"""Frame-stream transport tests. SIMULATED ONLY: loopback sockets in one process,
no ROS graph, no serial port, no hardware.  Covers the wire framing that the freshness
window depends on (partial packets, coalesced packets, single-slot sending).
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "app_ros_mobile"), str(ROOT / "ros2_ws" / "src" / "lelab_mobile_agent")]

from lelab.teleop_stream import (  # noqa: E402
    MAX_MESSAGE_BYTES, MessageReader, StreamClient, StreamError, StreamServer,
    encode, read_message, token_digest, token_matches,
)


class Framing(unittest.TestCase):
    def test_size_cap_is_enforced_before_writing(self):
        with self.assertRaises(StreamError) as ctx:
            encode({"blob": "x" * MAX_MESSAGE_BYTES})
        self.assertEqual(ctx.exception.reason, "message_too_large")

    def test_length_prefix_roundtrip(self):
        a, b = socket.socketpair()
        try:
            a.sendall(encode({"type": "ping", "n": 7}))
            self.assertEqual(read_message(b, 1.0), {"type": "ping", "n": 7})
        finally:
            a.close()
            b.close()

    def test_partial_header_survives_a_timeout(self):
        """The consumed header must not be lost, or every later parse is misaligned."""
        a, b = socket.socketpair()
        try:
            reader = MessageReader(b)
            payload = encode({"type": "window", "window_id": 42})
            a.sendall(payload[:3])
            with self.assertRaises(StreamError) as ctx:
                reader.read(0.2)
            self.assertEqual(ctx.exception.reason, "read_timeout")
            a.sendall(payload[3:])
            self.assertEqual(reader.read(1.0), {"type": "window", "window_id": 42})
        finally:
            a.close()
            b.close()

    def test_coalesced_messages_are_split_not_merged(self):
        a, b = socket.socketpair()
        try:
            reader = MessageReader(b)
            a.sendall(encode({"n": 1}) + encode({"n": 2}))
            self.assertEqual(reader.read(1.0), {"n": 1})
            self.assertEqual(reader.read(1.0), {"n": 2})
            self.assertEqual(reader.read_available(), [])
        finally:
            a.close()
            b.close()

    def test_bad_length_is_refused_not_hung(self):
        a, b = socket.socketpair()
        try:
            a.sendall(b"\x00\xff\xff\xff")
            with self.assertRaises(StreamError) as ctx:
                read_message(b, 1.0)
            self.assertEqual(ctx.exception.reason, "bad_length")
        finally:
            a.close()
            b.close()


class Authentication(unittest.TestCase):
    def test_token_check_uses_the_digest_only(self):
        digest = token_digest("lease-1")
        self.assertTrue(token_matches("lease-1", digest))
        self.assertFalse(token_matches("lease-2", digest))
        self.assertFalse(token_matches("lease-1", None))
        self.assertFalse(token_matches("", digest))


class ClientServer(unittest.TestCase):
    def setUp(self):
        self.frames: list[dict] = []
        self.licence_digest = token_digest("lease-token")
        handlers = {
            "on_hello": lambda session_id, epoch, token: (
                token_matches(token, self.licence_digest) and epoch == "7", "epoch_mismatch"),
            "on_frame": lambda frame: (self.frames.append(frame),
                                       {"type": "receipt", "state": "admitted",
                                        "frame_seq": frame["frame_seq"]})[1],
            "on_preempt": lambda action, reason: {"type": "receipt", "state": "stopped",
                                                  "action": action},
            "window_provider": lambda: {"window_id": 1, "window_remaining_ms": 200.0},
            "stream_epoch_provider": lambda: 9,
        }
        self.server = StreamServer(handlers, port=0, idle_timeout_s=0.05)
        self.port = self.server.start()

    def tearDown(self):
        self.server.stop()

    def client(self) -> StreamClient:
        client = StreamClient(port=self.port, timeout_s=1.0)
        client.connect(lease_token="lease-token", session_id="s", session_epoch="7")
        return client

    def test_hello_is_answered_with_the_negotiated_generation(self):
        client = StreamClient(port=self.port)
        welcome = client.connect(lease_token="lease-token", session_id="s", session_epoch="7")
        self.assertEqual(welcome["transport"], "frame_stream")
        self.assertEqual(welcome["stream_epoch"], 9)
        self.assertEqual(welcome["window"]["window_remaining_ms"], 200.0)
        self.assertNotIn("lease_token", json.dumps(self.server.state()))
        client.close()

    def test_wrong_epoch_or_token_is_refused(self):
        for token, epoch, reason in (("nope", "7", "not_authorised"),
                                     ("lease-token", "8", "not_authorised")):
            client = StreamClient(port=self.port)
            with self.assertRaises(StreamError) as ctx:
                client.connect(lease_token=token, session_id="s", session_epoch=epoch)
            self.assertEqual(ctx.exception.reason, reason)
            client.close()

    def test_back_to_back_submit_keeps_only_the_newest(self):
        """Latest-wins is the point: a stalled link must not replay a trajectory."""
        client = self.client()
        client.start_writer()
        for seq in range(6):
            client.submit_frame({"frame_seq": seq})
        deadline = time.monotonic() + 2.0
        receipts: list[dict] = []
        while time.monotonic() < deadline and not receipts:
            client.pump(0.05)
            receipts.extend(client.drain_receipts())
            time.sleep(0.01)
        self.assertTrue(receipts, "the newest frame has to be answered")
        self.assertLessEqual(receipts[-1]["frame_seq"], 5)
        stats = client.stats()["counters"]
        self.assertEqual(stats["frames_sent"] + stats["frames_dropped"], 6,
                         "every submission is either sent or counted as superseded")
        client.close()

    def test_paced_frames_are_all_answered(self):
        client = self.client()
        client.start_writer()
        receipts: list[dict] = []
        for seq in range(6):
            client.submit_frame({"frame_seq": seq})
            time.sleep(0.08)
            client.pump(0.05)
            receipts.extend(client.drain_receipts())
        self.assertEqual(sorted(r["frame_seq"] for r in receipts), list(range(6)))
        self.assertEqual(client.stats()["counters"]["frames_dropped"], 0)
        client.close()

    def test_pump_with_zero_timeout_still_sees_arrived_bytes(self):
        """A non-blocking pump that never reads the socket would starve the sender
        of the freshness windows it has to name."""
        client = self.client()
        client.send({"type": "ping"})
        deadline = time.monotonic() + 2.0
        seen = 0
        while time.monotonic() < deadline and seen == 0:
            seen = client.pump(0.0)
            time.sleep(0.01)
        self.assertEqual(seen, 1, "the pong must be readable with a zero timeout")
        client.close()

    def test_preempt_and_unknown_types(self):
        client = self.client()
        client.send({"type": "preempt", "action": "estop", "reason": "test"})
        self.assertEqual(client.recv(1.0)["state"], "stopped")
        client.send({"type": "cartwheel"})
        self.assertEqual(client.recv(1.0)["reason"], "unknown_message_type")
        client.close()

    def test_a_second_connection_replaces_the_first_generation(self):
        first = self.client()
        second = self.client()
        self.assertEqual(self.server.state()["generation"], 2)
        self.assertTrue(second.connected)
        first.close()
        second.close()


class LoopbackOnly(unittest.TestCase):
    def test_a_non_loopback_listener_is_refused(self):
        with self.assertRaises(StreamError) as ctx:
            StreamServer({}, host="0.0.0.0", port=0)
        self.assertEqual(ctx.exception.reason, "listener_not_loopback")

    def test_a_busy_port_fails_loudly(self):
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        server = StreamServer({}, port=holder.getsockname()[1])
        try:
            with self.assertRaises(StreamError) as ctx:
                server.start()
            self.assertEqual(ctx.exception.reason, "bind_failed")
        finally:
            holder.close()


if __name__ == "__main__":
    unittest.main()


class SendRace(unittest.TestCase):
    """A socket dropped mid-write must surface as StreamError, never as a crashed thread."""

    def test_send_after_the_socket_is_gone_raises_stream_error(self):
        client = StreamClient(port=1)
        with self.assertRaises(StreamError) as ctx:
            client.send({"type": "frame", "frame_seq": 1})
        self.assertEqual(ctx.exception.reason, "not_connected")

    def test_send_reads_the_socket_once_and_reports_a_write_failure(self):
        """The race is between the None check and the write, so drop it in between.

        ``encode`` runs exactly there. With the socket attribute cleared by then, the
        pre-fix shape raises AttributeError inside the writer thread and kills it; the
        fixed shape keeps the socket it was given and reports a counted send_failed.
        """
        import lelab.teleop_stream as module

        client = StreamClient(port=1)
        writes = []

        class Recorded:
            def sendall(self, payload):
                writes.append(len(payload))
                raise OSError(32, "broken pipe")

        client._sock = Recorded()
        original_encode = module.encode

        def racing_encode(message):
            module.encode = original_encode
            client._sock = None          # abort() lands here, after the check
            return original_encode(message)

        module.encode = racing_encode
        try:
            with self.assertRaises(StreamError) as ctx:
                client.send({"type": "frame", "frame_seq": 1})
        finally:
            module.encode = original_encode
        self.assertEqual(ctx.exception.reason, "send_failed")
        self.assertEqual(len(writes), 1, "the write must go to the socket we were holding")
