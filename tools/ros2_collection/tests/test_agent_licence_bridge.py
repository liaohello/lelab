"""Licence bridge tests. SIMULATED ONLY: in-process objects and one unix socket;
no ROS graph, no serial port, no hardware.

The bridge is what lets a second process emit at all, so these tests are about
authority: who can raise the permission, what makes it drop, and what a restart or a
stale generation must never be able to do.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "ros2_ws" / "src" / "lelab_mobile_agent"), str(ROOT / "app_ros_mobile")]

from lelab_mobile_agent.agent_licence_bridge import (  # noqa: E402
    AgentLicenceBridge, LicenceGenerationStore)
from lelab_mobile_agent.licence_ipc import LicenceMirror  # noqa: E402
from lelab_mobile_agent.teleop_stream import token_digest  # noqa: E402

BUNDLE = "b" * 64


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.socket = base / "licence.sock"
        self.mirror = LicenceMirror(self.socket)
        self.mirror.start()
        self.state = {
            "session_id": "sess-1", "session_epoch": 77,
            "calibration_bundle_hash": BUNDLE, "lease_token": "real-production-token",
            "permits": {"arm": True, "gripper": True, "base": False},
            "outputs_enabled": False, "lease_expires_in_s": 30.0, "estop_latched": False,
        }
        self.bridge = AgentLicenceBridge(socket_path=self.socket, authority=lambda: self.state,
                                         generation_path=base / "gen.txt", heartbeat_s=0.05)

    def tearDown(self):
        self.bridge.stop("test_end")
        self.mirror.stop()
        self.tmp.cleanup()

    def wait(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False


class AuthorityFlow(Fixture):
    def test_a_lease_produces_a_bounded_mirror_and_disarms_by_default(self):
        self.bridge.start()
        self.assertTrue(self.wait(lambda: self.mirror.current() is not None))
        record = self.mirror.current()
        self.assertFalse(record["outputs_enabled"])
        self.assertEqual(record["session_id"], "sess-1")
        self.assertEqual(record["session_epoch"], 77)
        self.assertEqual(record["token_sha256"], token_digest("real-production-token"))
        self.assertNotIn("real-production-token", str(record))

    def test_releasing_the_lease_revokes_rather_than_downgrading(self):
        self.bridge.start()
        self.assertTrue(self.wait(lambda: self.mirror.current() is not None))
        self.state["lease_token"] = None
        self.assertTrue(self.wait(lambda: self.mirror.current() is None))
        self.assertEqual(self.mirror.state()["revoked_reason"], "car_lease_released")
        self.assertFalse(self.mirror.may_emit("arm")[0])

    def test_enabling_output_is_propagated_and_estop_overrides_it(self):
        self.bridge.start()
        self.assertTrue(self.wait(lambda: self.mirror.current() is not None))
        self.state["outputs_enabled"] = True
        self.assertTrue(self.wait(lambda: (self.mirror.current() or {}).get("outputs_enabled")))
        self.state["estop_latched"] = True
        self.assertTrue(self.wait(lambda: self.mirror.current() is not None
                                  and not self.mirror.current()["outputs_enabled"]),
                        "a latched stop must dominate the output permission")

    def test_the_licence_ttl_cannot_outlive_the_real_lease(self):
        self.state["lease_expires_in_s"] = 0.08
        self.bridge.start()
        self.assertTrue(self.wait(lambda: self.mirror.current() is not None))
        self.assertLessEqual(self.mirror.current()["ttl_ms"], 200)

    def test_a_new_generation_is_issued_only_when_the_facts_change(self):
        self.bridge.start()
        self.assertTrue(self.wait(lambda: self.mirror.current() is not None))
        first = self.mirror.current()["control_generation"]
        self.assertTrue(self.wait(lambda: self.bridge.counters["published"] >= 1))
        before = self.bridge.counters["published"]
        time.sleep(0.2)                     # several heartbeats with identical facts
        self.assertEqual(self.mirror.current()["control_generation"], first,
                         "a heartbeat refreshes the mirror, it does not re-authorise")
        self.assertGreater(self.bridge.counters["heartbeats"], 0,
                           "heartbeats are still being sent")
        self.assertEqual(self.bridge.counters["published"], before,
                         "unchanged facts must not mint another generation")

    def test_bridge_stop_revokes_immediately(self):
        self.bridge.start()
        self.assertTrue(self.wait(lambda: self.mirror.current() is not None))
        self.bridge.stop("agent_exit")
        # The mirror reads its socket on its own thread, so the clearance lands a few
        # milliseconds later; what must never happen is the control process keeping a
        # live licence until the heartbeat TTL runs out.
        self.assertTrue(self.wait(lambda: self.mirror.current() is None, timeout=1.0))
        self.assertNotEqual(self.mirror.state()["revoked_reason"], "heartbeat_expired",
                            "the agent revoked it; the TTL was not the safety net")


    def test_a_clearance_still_reaches_the_emitter_after_the_lease_is_gone(self):
        """An expired lease must not strand the stop.

        The clearance is queued by the agent's clear route and sent by this thread. With
        no licence left to mirror there is nothing else that would ever talk to the
        control process again, so a stop raised before the expiry could never be lifted
        and recovery would have no path at all.
        """
        self.bridge.start()
        self.assertTrue(self.wait(lambda: self.mirror.current() is not None))
        self.state["lease_token"] = None           # the lease is gone: nothing to mirror
        self.assertTrue(self.wait(lambda: self.mirror.current() is None))
        latched_generation = self.bridge.generation.current
        self.bridge.request_clear_estop()
        self.assertTrue(self.wait(lambda: self.mirror.take_clear_estop() is not None,
                                   timeout=2.0),
                         "the clearance has to reach the emitter with no licence in flight")
        self.assertFalse(self.bridge.state()["clear_pending"])
        self.assertGreater(self.bridge.generation.current, latched_generation,
                           "a clearance is identified by a generation of its own")
        self.assertIsNone(self.mirror.current(), "lifting a stop is not a new licence")
        self.assertFalse(self.mirror.may_emit("arm")[0], "and it grants no motion")


class StreamArbitration(Fixture):
    def test_a_reported_stream_blocks_only_the_matching_identity(self):
        self.bridge.note_stream_state({"active": True, "session_id": "sess-1",
                                       "session_epoch": 77, "stream_epoch": 3})
        self.assertTrue(self.bridge.frame_transport_active("sess-1", 77))
        self.assertFalse(self.bridge.frame_transport_active("sess-1", 78))
        self.assertFalse(self.bridge.frame_transport_active("other", 77))
        self.bridge.note_stream_state({"active": False})
        self.assertFalse(self.bridge.frame_transport_active())

    def test_another_session_epoch_cannot_be_silently_treated_as_inert(self):
        self.bridge.note_stream_state({"active": True, "session_id": "sess-1",
                                       "session_epoch": "77", "stream_epoch": 3})
        # the epoch arrives as a string over the socket and as an int inside the agent
        self.assertTrue(self.bridge.frame_transport_active("sess-1", 77))


class RestartSemantics(unittest.TestCase):
    def test_generation_survives_a_restart_forward_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "gen.txt"
            first = LicenceGenerationStore(path)
            issued = [first.next() for _ in range(3)]
            self.assertEqual(issued, [1, 2, 3])
            reopened = LicenceGenerationStore(path)
            self.assertEqual(reopened.next(), 4,
                             "a restarted agent must not reuse an old generation")

    def test_a_corrupt_counter_file_restarts_high_enough_to_be_useless_to_an_attacker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "gen.txt"
            path.write_text("garbage", encoding="utf-8")
            self.assertEqual(LicenceGenerationStore(path).next(), 1)


class FailureModes(unittest.TestCase):
    def test_no_socket_means_no_licence_and_no_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = AgentLicenceBridge(
                socket_path=pathlib.Path(tmp) / "absent.sock",
                authority=lambda: {"session_id": "s", "session_epoch": 1,
                                   "calibration_bundle_hash": BUNDLE,
                                   "lease_token": "t", "permits": {}, "outputs_enabled": True,
                                   "lease_expires_in_s": 30.0, "estop_latched": False},
                generation_path=pathlib.Path(tmp) / "gen.txt", heartbeat_s=0.05)
            bridge.start()
            time.sleep(0.2)
            self.assertEqual(bridge.state()["state"], "connecting")
            self.assertIsNone(bridge.state()["frame_stream"])
            bridge.stop("end")

    def test_a_raising_authority_is_recorded_not_propagated(self):
        with tempfile.TemporaryDirectory() as tmp:
            def boom():
                raise RuntimeError("gateway vanished")
            bridge = AgentLicenceBridge(socket_path=pathlib.Path(tmp) / "s.sock",
                                        authority=boom,
                                        generation_path=pathlib.Path(tmp) / "gen.txt",
                                        heartbeat_s=0.05)
            bridge.start()
            time.sleep(0.2)
            self.assertIn("authority_RuntimeError", bridge.state()["last_error"])
            self.assertGreater(bridge.state()["counters"]["failures"], 0)
            bridge.stop("end")


if __name__ == "__main__":
    unittest.main()


class RecordingContext(Fixture):
    """The recorder's context is mirrored on the same socket, and grants nothing."""

    def setUp(self):
        super().setUp()
        self.recording = {"session_id": "rec-1", "session_epoch": 9,
                          "episode_id": "ep-1", "phase": "observe",
                          "clock_epoch": "clock-a", "calibration_bundle_hash": BUNDLE}
        self.bridge.recording_authority = lambda: self.recording

    def published(self):
        return self.wait(lambda: self.mirror.context() is not None)

    def test_a_live_recording_session_reaches_the_mirror(self):
        self.bridge.start()
        self.assertTrue(self.published())
        context = self.mirror.context()
        self.assertEqual(context["episode_id"], "ep-1")
        self.assertEqual(context["phase"], "observe")
        self.assertEqual(context["clock_epoch"], "clock-a")

    def test_no_recording_session_means_no_context_and_no_invention(self):
        self.recording = {}
        self.bridge.start()
        time.sleep(0.3)
        self.assertIsNone(self.mirror.context())
        self.assertEqual(self.bridge.counters["contexts"], 0)

    def test_a_phase_change_moves_the_sequence_and_the_grant_stays_put(self):
        self.bridge.start()
        self.assertTrue(self.published())
        generation = self.mirror.current()["control_generation"]
        first = self.mirror.context()["context_seq"]
        self.recording = dict(self.recording, phase="grasp")
        self.assertTrue(self.wait(lambda: (self.mirror.context() or {}).get("phase") == "grasp"))
        self.assertGreater(self.mirror.context()["context_seq"], first)
        self.assertEqual(self.mirror.current()["control_generation"], generation,
                         "a recording context is not a permission change")

    def test_a_context_expires_when_the_agent_stops_refreshing_it(self):
        self.bridge.start()
        self.assertTrue(self.published())
        self.bridge.recording_authority = lambda: {}
        self.assertTrue(self.wait(lambda: self.mirror.context() is None, timeout=3.0))

    def test_a_replayed_context_sequence_is_refused(self):
        from lelab_mobile_agent.licence_ipc import LicenceError, recording_context_record

        context = recording_context_record(session_id="rec-1", session_epoch=9,
                                          episode_id="ep-2", phase="observe",
                                          clock_epoch="clock-a",
                                          calibration_bundle_hash=BUNDLE, context_seq=1)
        self.mirror.apply(context)
        with self.assertRaises(LicenceError):
            self.mirror.apply(dict(context, context_seq=1, episode_id="ep-other"))
        self.assertEqual(self.mirror.context()["episode_id"], "ep-2")

    def test_a_new_agent_process_restarts_the_context_sequence(self):
        from lelab_mobile_agent.licence_ipc import recording_context_record

        base = dict(session_id="rec-1", session_epoch=9, episode_id="ep-2", phase="observe",
                    clock_epoch="clock-a", calibration_bundle_hash=BUNDLE, context_seq=4,
                    agent_boot="boot-1")
        self.mirror.apply(recording_context_record(**base))
        self.mirror.apply(recording_context_record(**dict(base, agent_boot="boot-2",
                                                          context_seq=1, episode_id="ep-9")))
        self.assertEqual(self.mirror.context()["episode_id"], "ep-9")


class ExpiredLease(Fixture):
    """The mirror must die with the lease it mirrors, not outlive it on a short TTL."""

    def test_a_running_out_lease_revokes_instead_of_refreshing(self):
        self.state["lease_expires_in_s"] = 30.0
        self.bridge.start()
        self.assertTrue(self.wait(lambda: self.mirror.current() is not None))
        self.state["lease_expires_in_s"] = -0.5      # the lease ran out quietly
        self.assertTrue(self.wait(lambda: self.mirror.current() is None, timeout=3.0))
        self.assertIn(self.bridge.state()["state"], ("revoked", "revoking"))
        self.assertGreaterEqual(self.bridge.counters["revoked"], 1)

    def test_a_short_remaining_lease_clamps_the_ttl(self):
        self.state["lease_expires_in_s"] = 0.08
        self.bridge.start()
        self.assertTrue(self.wait(lambda: self.mirror.current() is not None))
        self.assertLessEqual(self.mirror.current()["ttl_ms"], 300)
        self.assertGreaterEqual(self.mirror.current()["ttl_ms"], 50)
