"""The emission boundary re-checks the permission it is about to act on.

A frame is admitted at one instant and emitted up to a control tick later. If the licence
identity, the generation or the output permission moved in between, the older check is
worth nothing - so the emitter compares the admitted frame against the permission that is
live right now, and drops it instead of publishing a target nobody authorised.
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "lelab_mobile_agent"))
sys.path.insert(0, str(REPO / "app_ros_mobile"))

try:
    from lelab_mobile_agent.car_control_process import CarControlRuntime
    IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001 - rclpy lives on the car side
    CarControlRuntime = None
    IMPORT_ERROR = exc

SESSION = "ros_test_session"
EPOCH = 111
BUNDLE = "bundle-hash"


class FakeLicence:
    def __init__(self, record):
        self.record = record

    def current(self):
        return dict(self.record) if self.record is not None else None

    def take_clear_estop(self):
        return None


def licence_record(**over):
    record = {"session_id": SESSION, "session_epoch": EPOCH,
              "calibration_bundle_hash": BUNDLE, "outputs_enabled": True,
              "permits": {"arm": True, "gripper": True}, "deadline_ns": 2 ** 62,
              "control_generation": 4}
    record.update(over)
    return record


def frame(**over):
    built = {"session_id": SESSION, "session_epoch": EPOCH,
             "calibration_bundle_hash": BUNDLE, "stream_epoch": 7, "frame_seq": 3,
             "sample_seq": 91}
    built.update(over)
    return built


class StubDriver:
    def __init__(self, outputs_enabled=True):
        self.outputs_enabled = outputs_enabled
        self.submitted = []
        self.stopped_for = []

    def submit_frame(self, targets):
        self.submitted.append(targets)

    def stop(self, reason):
        self.stopped_for.append(reason)
        self.outputs_enabled = False


class StubAdmission:
    def __init__(self, pending):
        self.pending = pending
        self.cleared = []

    def take(self):
        pending, self.pending = self.pending, None
        return pending

    def clear(self, reason):
        self.cleared.append(reason)


class StubWindows:
    def issue(self, *, duration_ms):
        return {"window_id": "w1", "stream_epoch": 7, "duration_ms": duration_ms,
                "issued_ns": 0}

    def remaining_ms(self):
        return 200.0


def runtime(licence, pending, stream_epoch=7, driver=None):
    node = object.__new__(CarControlRuntime)
    node.control_lock = threading.RLock()
    node.counters = {"ticks": 0, "window_issues": 0, "empty_ticks": 0,
                     "licences_lost": 0, "hold_frames": 0, "frames_emitted": 0,
                     "frames_received": 0, "frames_refused": 0, "frames_expired": 0}
    node.windows = StubWindows()
    node.stream = None
    node.latched_fault = None
    node.paused = False
    node.reengage_required = False
    node.licence = FakeLicence(licence)
    node._licence_was_present = True
    node.bundle_hash = BUNDLE
    node.stream_epoch = stream_epoch
    node.admission = StubAdmission(pending)
    node.driver = driver or StubDriver()
    node.last_refusal = None
    node.emit_intervals_ms = []
    node.last_emit_mono = None
    node.max_ttl_ms = 200
    node.window_ms = 250
    node._write_status = lambda: None
    node._record_frame = lambda accepted: None
    node._targets = lambda decoded: {"targets": decoded}
    return node


def pending_for(f):
    return {"frame": f, "decoded": {"arm": {"seq": 1}}, "received_ns": 0,
            "deadline_ns": 2 ** 62}


@unittest.skipIf(CarControlRuntime is None, f"needs rclpy: {IMPORT_ERROR}")
class EmissionIdentity(unittest.TestCase):
    def test_a_matching_frame_is_allowed_through(self):
        node = runtime(licence_record(), None)
        ok, why = node._emission_identity_ok(frame())
        self.assertTrue(ok, why)

    def test_a_newer_session_epoch_invalidates_an_admitted_frame(self):
        """The plan's "identity changed while the token is still valid" case."""
        node = runtime(licence_record(session_epoch=EPOCH + 1), None)
        ok, why = node._emission_identity_ok(frame())
        self.assertFalse(ok)
        self.assertEqual(why, "licence_identity_changed_before_emit:session_epoch")

    def test_a_different_session_id_invalidates_it(self):
        node = runtime(licence_record(session_id="someone-else"), None)
        ok, why = node._emission_identity_ok(frame())
        self.assertFalse(ok)
        self.assertIn("session_id", why)

    def test_a_retired_generation_invalidates_it(self):
        node = runtime(licence_record(), None, stream_epoch=9)
        ok, why = node._emission_identity_ok(frame(stream_epoch=7))
        self.assertFalse(ok)
        self.assertEqual(why, "stream_epoch_retired_before_emit")

    def test_a_withdrawn_output_permission_invalidates_it(self):
        node = runtime(licence_record(outputs_enabled=False), None)
        ok, why = node._emission_identity_ok(frame())
        self.assertFalse(ok)
        self.assertEqual(why, "licence_output_permission_withdrawn")

    def test_a_lost_licence_invalidates_it(self):
        node = runtime(None, None)
        ok, why = node._emission_identity_ok(frame())
        self.assertFalse(ok)
        self.assertEqual(why, "licence_lost_before_emit")

    def test_the_tick_drops_an_admitted_frame_instead_of_publishing_it(self):
        """End to end inside the runtime: nothing reaches the driver after the move."""
        driver = StubDriver()
        node = runtime(licence_record(session_epoch=EPOCH + 1),
                       pending_for(frame()), driver=driver)
        node._tick_locked()
        self.assertEqual(driver.submitted, [], "a stale target must not be published")
        self.assertEqual(node.counters.get("frames_dropped_before_emit"), 1)
        self.assertEqual(node.last_refusal["reason"],
                         "licence_identity_changed_before_emit:session_epoch")
        self.assertEqual(node.admission.cleared,
                         ["licence_identity_changed_before_emit:session_epoch"])

    def test_the_tick_still_publishes_when_nothing_moved(self):
        driver = StubDriver()
        node = runtime(licence_record(), pending_for(frame()), driver=driver)
        node._tick_locked()
        self.assertEqual(len(driver.submitted), 1)
        self.assertIsNone(node.last_refusal)
        self.assertEqual(node.counters["frames_emitted"], 1)


if __name__ == "__main__":
    unittest.main()
