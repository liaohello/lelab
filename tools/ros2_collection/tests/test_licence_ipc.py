"""Licence mirror tests. SIMULATED ONLY: a unix socket inside the test process,
no ROS graph, no serial port, no hardware.

The property under test is authority, not transport: the car agent decides, and a
separate control process may only ever hold a bounded, expiring mirror of that
decision - with nothing the mirror consumer can do to talk itself into permission.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "app_ros_mobile"), str(ROOT / "ros2_ws" / "src" / "lelab_mobile_agent")]

from lelab_mobile_agent.licence_ipc import (  # noqa: E402
    LicenceError, LicenceMirror, LicencePublisher, licence_record)
from lelab_mobile_agent.teleop_stream import token_digest  # noqa: E402

BUNDLE = "b" * 64


class Clock:
    def __init__(self):
        self.now = 5_000_000_000

    def __call__(self) -> int:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += int(ms * 1_000_000)


def record(generation=1, outputs=False, permits=None, ttl_ms=300, epoch="e-1"):
    return licence_record(control_generation=generation, session_id="s-1", session_epoch=epoch,
                          calibration_bundle_hash=BUNDLE,
                          permits=permits or {"arm": True, "gripper": True, "base": False},
                          outputs_enabled=outputs, token_sha256=token_digest("lease-1"),
                          ttl_ms=ttl_ms)


class Schema(unittest.TestCase):
    def test_ttl_is_bounded_short_on_purpose(self):
        for bad in (10, 60_000):
            with self.assertRaises(LicenceError) as ctx:
                record(ttl_ms=bad)
            self.assertEqual(ctx.exception.reason, "invalid_ttl")

    def test_base_permit_keys_are_the_only_ones_allowed(self):
        with self.assertRaises(LicenceError) as ctx:
            licence_record(control_generation=1, session_id="s", session_epoch="e",
                           calibration_bundle_hash=BUNDLE,
                           permits={"arm": True, "chassis": True}, outputs_enabled=False,
                           token_sha256=token_digest("t"), ttl_ms=300)
        self.assertEqual(ctx.exception.reason, "invalid_permits")

    def test_a_token_is_never_carried_only_its_digest(self):
        document = record()
        self.assertNotIn("token", document)
        self.assertEqual(len(document["token_sha256"]), 64)


class MirrorRules(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.tmp = tempfile.TemporaryDirectory()
        self.mirror = LicenceMirror(Path(self.tmp.name) / "licence.sock", clock_ns=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_heartbeat_expiry_revokes_without_waiting_for_a_session_ttl(self):
        self.mirror.apply(record(generation=1, outputs=True, ttl_ms=300))
        self.assertTrue(self.mirror.current())
        self.clock.advance_ms(299)
        self.assertTrue(self.mirror.current(), "still inside the bridge window")
        self.clock.advance_ms(5)
        self.assertIsNone(self.mirror.current())
        self.assertEqual(self.mirror.state()["revoked_reason"], "heartbeat_expired")
        self.assertFalse(self.mirror.may_emit("arm")[0])

    def test_older_generation_is_refused_and_does_not_disturb_the_live_one(self):
        self.mirror.apply(record(generation=5, outputs=True))
        with self.assertRaises(LicenceError) as ctx:
            self.mirror.apply(record(generation=4, outputs=False))
        self.assertEqual(ctx.exception.reason, "generation_replayed")
        self.assertTrue(self.mirror.current()["outputs_enabled"])

    def test_same_generation_refreshes_and_replacement_swaps(self):
        self.mirror.apply(record(generation=7, outputs=True))
        self.mirror.apply(record(generation=7, outputs=True))
        self.assertEqual(self.mirror.state()["counters"]["refreshed"], 1)
        self.mirror.apply(record(generation=8, outputs=False))
        self.assertEqual(self.mirror.state()["counters"]["replaced"], 1)
        self.assertFalse(self.mirror.current()["outputs_enabled"])

    def test_revoke_clears_immediately_and_names_the_reason(self):
        self.mirror.apply(record(generation=3, outputs=True))
        self.mirror.apply({"schema_version": 1, "type": "revoke", "control_generation": 4,
                           "reason": "lease_released"})
        self.assertIsNone(self.mirror.current())
        self.assertEqual(self.mirror.state()["revoked_reason"], "lease_released")

    def test_may_emit_needs_permits_outputs_and_a_channel_grant(self):
        self.mirror.apply(record(generation=1, outputs=False,
                                 permits={"arm": True, "gripper": False, "base": False}))
        self.assertEqual(self.mirror.may_emit("arm"), (False, "outputs_disabled"))
        self.mirror.apply(record(generation=2, outputs=True,
                                 permits={"arm": True, "gripper": False, "base": False}))
        self.assertEqual(self.mirror.may_emit("gripper"), (False, "gripper_not_permitted"))
        self.assertTrue(self.mirror.may_emit("arm")[0])

    def test_identity_binding_rejects_another_epoch_or_bundle(self):
        self.mirror.apply(record(generation=1, outputs=True, epoch="e-1"))
        self.assertTrue(self.mirror.matches(session_id="s-1", session_epoch="e-1",
                                            bundle_hash=BUNDLE))
        self.assertFalse(self.mirror.matches(session_id="s-1", session_epoch="e-2",
                                             bundle_hash=BUNDLE))
        self.assertFalse(self.mirror.matches(session_id="other", session_epoch="e-1",
                                             bundle_hash=BUNDLE))

    def test_a_clear_estop_carries_no_permission_of_its_own(self):
        self.mirror.apply(record(generation=9, outputs=False))
        applied = self.mirror.apply({"schema_version": 1, "type": "clear_estop",
                                     "control_generation": 9, "reason": "operator_cleared"})
        self.assertTrue(applied["applied"])
        pending = self.mirror.state()["pending_clear"]
        self.assertEqual(pending["reason"], "operator_cleared")
        # consuming it must not change the licence itself
        licence_before = self.mirror.current()
        self.assertEqual(self.mirror.take_clear_estop()["control_generation"], 9)
        self.assertIsNone(self.mirror.take_clear_estop(), "a clearance is delivered once")
        self.assertEqual(self.mirror.current()["outputs_enabled"],
                         licence_before["outputs_enabled"])

    def test_an_older_clear_is_refused(self):
        self.mirror.apply(record(generation=12, outputs=True))
        self.mirror.apply({"schema_version": 1, "type": "clear_estop",
                           "control_generation": 12, "reason": "first"})
        self.mirror.take_clear_estop()
        with self.assertRaises(LicenceError) as ctx:
            self.mirror.apply({"schema_version": 1, "type": "clear_estop",
                               "control_generation": 3, "reason": "stale"})
        self.assertEqual(ctx.exception.reason, "generation_replayed")


class SocketRoundTrip(unittest.TestCase):
    """Publisher to mirror over a real unix socket, with the socket's own permissions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state" / "licence.sock"
        self.mirror = LicenceMirror(self.path)
        self.mirror.start()

    def tearDown(self):
        self.mirror.stop()
        self.tmp.cleanup()

    def test_publisher_heartbeat_reaches_the_mirror_and_eof_revokes(self):
        publisher = LicencePublisher(self.path)
        self.assertTrue(publisher.connect())
        publisher.publish(**record(generation=1, outputs=True))
        deadline = time.monotonic() + 3.0
        while self.mirror.current() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(self.mirror.current())
        self.assertTrue(self.mirror.current()["outputs_enabled"])
        publisher.close()
        deadline = time.monotonic() + 3.0
        while self.mirror.current() is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNone(self.mirror.current(), "a closed publisher is a revoked licence")
        self.assertEqual(self.mirror.state()["revoked_reason"], "ipc_eof")

    def test_the_socket_is_owner_only(self):
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600, "another user must not be able to drive the control process")

    def test_a_foreign_token_cannot_be_pushed_through_the_mirror(self):
        # the publisher is trusted by filesystem permission, not by a second handshake:
        # what the control process checks is the digest inside the record
        publisher = LicencePublisher(self.path)
        publisher.connect()
        publisher.publish(**record(generation=1, outputs=True))
        time.sleep(0.2)
        self.assertNotEqual(self.mirror.token_digest(), token_digest("not-the-lease-token"))
        publisher.close()


class PublisherFailure(unittest.TestCase):
    def test_publish_without_a_connection_is_false_not_an_exception(self):
        publisher = LicencePublisher("/nonexistent/licence.sock")
        self.assertFalse(publisher.connect())
        self.assertFalse(publisher.publish(**record()))
        self.assertFalse(publisher.revoke("nope"))


if __name__ == "__main__":
    unittest.main()
