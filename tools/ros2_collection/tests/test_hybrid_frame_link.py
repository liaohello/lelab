"""Upper-computer frame link tests. SIMULATED ONLY: an in-process loopback stream
server stands in for the car process; no ROS graph, no serial port, no hardware.

These cover the two properties the redesign is about - one exchange per input
snapshot, and status read from an age-carrying cache instead of inline on the control
path - plus what happens when the car refuses or stops answering.
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "app_ros_mobile"), str(ROOT / "ros2_ws" / "src" / "lelab_mobile_agent")]

from lelab.hybrid_frame_link import CachedCarStatus, FrameLink, FrameLinkError  # noqa: E402
from lelab.teleop_stream import StreamServer, token_digest  # noqa: E402

SESSION = "s-1"
EPOCH = "e-1"
BUNDLE = "b" * 64
TOKEN = "car-lease-token"


def fake_car(window_state: dict, receipts: list, refusals: list) -> StreamServer:
    """A car that behaves like the real admission: issues windows, answers frames."""

    def on_hello(session_id, session_epoch, lease_token):
        if session_id != SESSION or session_epoch != EPOCH:
            return False, "identity_mismatch"
        if token_digest(lease_token) != token_digest(TOKEN):
            return False, "lease_mismatch"
        return True, {}

    def on_frame(frame):
        receipts.append(frame)
        if refusals and frame["frame_seq"] in refusals[0]:
            return {"type": "receipt", "state": "refused", "reason": "ttl_too_long",
                    "frame_seq": frame["frame_seq"]}
        return {"type": "receipt", "state": "admitted", "frame_seq": frame["frame_seq"],
                "window_remaining_ms": 100.0, "window_age_ms": 40.0,
                "deadline_in_ms": 120.0, "confirmation": "gateway_accepted"}

    def on_preempt(action, reason):
        window_state["stopped"] = str(action)
        return {"type": "receipt", "state": "stopped", "action": str(action),
                "hold_published": True}

    server = StreamServer({"on_hello": on_hello, "on_frame": on_frame,
                           "on_preempt": on_preempt,
                           "window_provider": lambda: dict(window_state),
                           "stream_epoch_provider": lambda: 1},
                          port=0, idle_timeout_s=0.05)
    server.start()
    return server


class Fixture(unittest.TestCase):
    def setUp(self):
        self.receipts: list[dict] = []
        self.refusals: list[list[int]] = []
        self.window_state = {"window_id": 1, "window_remaining_ms": 200.0}
        self.server = fake_car(self.window_state, self.receipts, self.refusals)
        self.status_calls = []

    def tearDown(self):
        self.server.stop()

    def link(self, **kwargs) -> FrameLink:
        cache = CachedCarStatus(self.probe, period_s=0.05, max_age_s=kwargs.pop("max_age_s", 1.0))
        link = FrameLink(host="127.0.0.1", port=self.server.port, session_id=SESSION,
                         session_epoch=EPOCH, bundle_hash=BUNDLE, lease_token=TOKEN,
                         status_cache=cache, **kwargs)
        link.attach_lease(TOKEN)
        self.addCleanup(lambda: cache.stop())
        return link

    def probe(self):
        # recorded with the calling thread so a test can tell a background poll from a
        # status call made inline on the control path, which is what must not happen
        self.status_calls.append(threading.current_thread().name)
        return {"outputs_enabled": True, "lease": {"valid": True}}


class OneExchangePerSnapshot(Fixture):
    def test_a_snapshot_costs_one_frame_and_no_inline_status_call(self):
        # This fixture does not periodically issue windows like the real control
        # process, so its single test window must outlive the eleven samples.
        self.window_state["window_remaining_ms"] = 1000.0
        link = self.link()
        link.engage()
        self.status_calls.clear()
        for sample_seq in range(1, 12):
            outcome = link.send_snapshot(arm_positions=[0.01 * sample_seq] * 5,
                                         gripper_rad=0.01, sample_seq=sample_seq)
            self.assertTrue(outcome["sent"], outcome)
            time.sleep(0.02)      # one snapshot per period, as the control loop does
        state = link.state()
        self.assertEqual(state["counts"]["submitted"], 11)
        self.assertEqual(state["channel_seq"], {"arm": 11, "gripper": 11},
                         "one sequence per channel, both inside the same frame")
        self.assertEqual(state["sender"]["counters"]["frames_sent"], 11)
        self.assertEqual(state["sender"]["counters"]["frames_dropped"], 0,
                         "nothing was superseded at one snapshot per period")
        # every status read happened on the cache's own thread, never on the sender's
        main_thread = threading.current_thread().name
        self.assertTrue(all(name != main_thread for name in self.status_calls),
                        f"status polled inline from the control path: {self.status_calls}")
        self.assertLess(len(self.status_calls), 11, "background reads, not per-cycle ones")
        link.collect()
        self.assertEqual(link.state()["counts"]["admitted"], 11)
        self.assertEqual(len(self.receipts), 11)
        self.assertEqual(sorted(self.receipts[0]["commands"]), ["arm", "gripper"])
        link.stop("test end")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self.window_state.get("stopped") != "estop":
            time.sleep(0.02)
            link.collect()
        self.assertEqual(self.window_state.get("stopped"), "estop",
                         "an emergency stop is sent as a preemption, not queued behind frames")

    def test_status_is_read_from_the_cache_with_an_age(self):
        link = self.link()
        link.engage()
        deadline = time.monotonic() + 2.0
        while link.status_cache.age_s() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(link.status_cache.fresh())
        self.assertTrue(self.status_calls, "the background poller does the reading")
        outcome = link.send_snapshot(arm_positions=[0.0] * 5, gripper_rad=0.0, sample_seq=1)
        self.assertIsNotNone(outcome["status_age_s"])
        self.assertTrue(all(name != threading.current_thread().name
                            for name in self.status_calls or ["worker"]))
        link.collect()

    def test_a_stale_cache_is_not_permission(self):
        link = self.link(max_age_s=0.1)
        cache = link.status_cache
        cache.poll()
        time.sleep(0.2)
        self.assertIsNone(cache.fresh(), "an expired snapshot must not read as authorised")
        self.assertIsNotNone(cache.age_s())


class SenderRefusals(Fixture):
    def test_unrenewed_window_ages_out_locally(self):
        self.window_state["window_remaining_ms"] = 80.0
        link = self.link()
        link.engage()
        time.sleep(0.04)
        outcome = link.send_snapshot(arm_positions=[0.0] * 5, gripper_rad=0.0, sample_seq=1)
        self.assertFalse(outcome["sent"])
        self.assertEqual(outcome["reason"], "no_fresh_window")
        link.disengage("test")

    def test_send_before_engage_is_refused(self):
        link = self.link()
        with self.assertRaises(FrameLinkError) as ctx:
            link.send_snapshot(arm_positions=[0.0] * 5, gripper_rad=0.0, sample_seq=1)
        self.assertEqual(ctx.exception.reason, "not_engaged")

    def test_without_a_lease_token_there_is_no_connection(self):
        link = FrameLink(host="127.0.0.1", port=self.server.port, session_id=SESSION,
                         session_epoch=EPOCH, bundle_hash=BUNDLE, lease_token="")
        with self.assertRaises(FrameLinkError) as ctx:
            link.engage()
        self.assertEqual(ctx.exception.reason, "no_lease")

    def test_a_wrong_lease_token_is_refused_by_the_car(self):
        link = self.link()
        link.attach_lease("someone-elses-token")
        with self.assertRaises(FrameLinkError) as ctx:
            link.engage()
        # the car answers with its own wire reason, not the local token comparison
        self.assertEqual(ctx.exception.reason, "not_authorised")

    def test_ttl_beyond_the_car_ceiling_never_reaches_the_wire(self):
        with self.assertRaises(FrameLinkError) as ctx:
            self.link(ttl_ms=400)
        self.assertEqual(ctx.exception.reason, "ttl_out_of_range")

    def test_a_nearly_closed_window_pauses_the_sender(self):
        link = self.link(window_margin_ms=1000.0)
        link.engage()
        outcome = link.send_snapshot(arm_positions=[0.0] * 5, gripper_rad=0.0, sample_seq=1)
        self.assertFalse(outcome["sent"])
        self.assertEqual(outcome["reason"], "no_fresh_window")
        self.assertEqual(link.state()["counts"]["skipped_no_window"], 1)
        self.assertEqual(link.state()["counts"]["submitted"], 0)

    def test_default_margin_rejects_a_window_that_could_expire_during_handoff(self):
        # The observed live failure was a window that had 60+ ms left when
        # checked, but expired after the asynchronous writer got to it.
        self.window_state["window_remaining_ms"] = 100.0
        link = self.link()
        link.engage()
        outcome = link.send_snapshot(arm_positions=[0.0] * 5, gripper_rad=0.0, sample_seq=1)
        self.assertFalse(outcome["sent"])
        self.assertEqual(outcome["reason"], "no_fresh_window")
        self.assertEqual(link.state()["counts"]["submitted"], 0)

    def test_illegal_gripper_value_consumes_nothing(self):
        link = self.link()
        link.engage()
        with self.assertRaises(FrameLinkError) as ctx:
            link.send_snapshot(arm_positions=[0.0] * 5, gripper_rad=float("nan"),
                               sample_seq=1)
        self.assertEqual(ctx.exception.reason, "command_not_finite")
        state = link.state()
        self.assertEqual(state["frame_seq"], 0)
        self.assertEqual(state["channel_seq"], {"arm": 0, "gripper": 0})

    def test_car_refusals_are_counted_and_attributed(self):
        # This fixture supplies one static window, rather than the real stream's
        # periodic refresh. Keep it live long enough to test receipt accounting.
        self.window_state["window_remaining_ms"] = 1000.0
        self.refusals.append([2, 3])
        link = self.link()
        link.engage()
        for sample_seq in (1, 2, 3):
            link.send_snapshot(arm_positions=[0.01] * 5, gripper_rad=0.01,
                               sample_seq=sample_seq)
            time.sleep(0.05)
            link.collect()
        state = link.state()
        self.assertEqual(state["counts"]["admitted"], 1)
        self.assertEqual(state["counts"]["refused"], 2)
        self.assertEqual(state["refusals"].get("ttl_too_long"), 2)


class RecordedIdentity(Fixture):
    def test_leader_and_pre_clip_values_travel_inside_the_frame(self):
        link = self.link()
        link.engage()
        link.send_snapshot(arm_positions=[0.10] * 5, gripper_rad=0.02, sample_seq=1,
                           leader_rad=[0.11] * 5, pre_clip_rad=[0.105] * 5)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not self.receipts:
            time.sleep(0.02)
            link.collect()
        arm = self.receipts[0]["commands"]["arm"]["target"]
        self.assertEqual(arm["leader_rad"], [0.11] * 5)
        self.assertEqual(arm["pre_clip_rad"], [0.105] * 5)
        self.assertEqual(arm["positions_rad"], [0.10] * 5)


class ThreadSafety(unittest.TestCase):
    def test_cache_and_link_survive_a_collector_thread(self):
        calls: list[float] = []
        cache = CachedCarStatus(lambda: (calls.append(time.monotonic()),
                                         {"ok": True})[1], period_s=0.02)
        cache.start()
        threads = [threading.Thread(target=lambda: [cache.fresh() for _ in range(200)])
                   for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)
        time.sleep(0.15)          # at least one poll period must have elapsed
        cache.stop()
        self.assertTrue(cache.counts["ok"] > 0)
        self.assertIsNotNone(cache.fresh())


if __name__ == "__main__":
    unittest.main()
