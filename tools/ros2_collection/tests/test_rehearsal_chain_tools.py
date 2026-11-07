"""The rehearsal tools are part of the evidence chain, so their own guards need tests.

Two live failures on 2026-11-03 are pinned here. The §5.2 step -1 driver stopped working the moment
the wizard began refusing shipped example values - it seeds straight from those templates, and no
test exercised the driver, so the chain only broke when someone actually ran it. The chain closer
took its session id from a report path that an earlier run had left behind, then asked the live
agent for a session it had never recorded; the 404 was the only symptom.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TOOLS = REPO / "tools" / "ros2_collection"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(REPO / "app_ros_mobile"))

import local_car_upper_stream_stall as stall  # noqa: E402
import local_full_chain_sync_export as chain  # noqa: E402
import local_teleop_session_drive as drive  # noqa: E402
from lelab.mobile_teleop import CalibrationStore  # noqa: E402

EXAMPLES = REPO / "configs" / "mobile_teleop"
IDENTITIES = {"leader": "leader_local", "follower": "car_local", "mapping": "map_local"}
ROOT = Path("/tmp/rehearsal-car")
URL = "http://127.0.0.1:18115"


def marker(port=18115, car_root=ROOT):
    return {"port": port, "car_root": str(car_root), "agent_pid": 1247523}


def listing(*entries):
    return list(entries)


def entry(sid, status="raw_ready", path=None):
    return {"session_id": sid, "status": status,
            "path": str(path if path is not None else ROOT / "sessions" / sid)}


class RehearsalSeedTests(unittest.TestCase):
    """The driver must seed layers the real gate accepts, without weakening that gate."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="rehearsal_seed_"))
        self.store = CalibrationStore(self.dir)

    def test_the_unedited_shipped_templates_are_still_refused(self):
        for kind, identity in IDENTITIES.items():
            self.store.save(kind, identity, json.loads(
                (EXAMPLES / f"{kind}.example.json").read_text(encoding="utf-8")))
        ready = self.store.readiness(*IDENTITIES.values())
        self.assertFalse(ready["ready"], "the example guard must stay where the wizard put it")
        self.assertTrue([r for r in ready["reasons"] if "shipped example values" in r])

    def test_the_driver_seed_passes_the_shipped_example_gate(self):
        """This is the call that broke §5.2 step -1: readiness refused the rehearsal's own seed."""
        drive.seed(self.store, IDENTITIES)
        ready = self.store.readiness(*IDENTITIES.values())
        self.assertTrue(ready["ready"], ready["reasons"])
        self.assertEqual(len(ready["calibration_version"].split("|")), 3)

    def test_provenance_labels_the_placeholder_without_touching_a_number(self):
        out = drive.as_rehearsal_provenance({
            "_example_not_measured": True,
            "limits_rad": {"joint1": [-1.5, 1.5]},
            "reference_pose_rad": {"joint1": 0.0, "_provenance": "derived_not_measured: template"},
            "joints": [{"leader": "shoulder_pan", "_provenance": "example_not_measured: guess"}],
            "span_note": "example_not_measured: assume full travel"})
        self.assertNotIn("_example_not_measured", out)
        self.assertEqual(out["limits_rad"]["joint1"], [-1.5, 1.5], "values must survive untouched")
        self.assertEqual(out["reference_pose_rad"]["joint1"], 0.0)
        self.assertEqual(out["reference_pose_rad"]["_provenance"], "rehearsal_synthetic")
        self.assertEqual(out["joints"][0]["_provenance"], "rehearsal_synthetic")
        self.assertEqual(out["span_note"], "rehearsal_synthetic")


class UpperStreamStallWitnessTests(unittest.TestCase):
    """The stall rehearsal proves "the car expired the stream itself"; that has to stay provable."""

    FROZEN = {"pre_clip_rad": [0.002398], "gripper_percent": 50.349, "clutch": "engaged",
              "wall": 103205.435889826}

    def test_a_later_command_with_a_changed_payload_counts_as_new(self):
        newer = dict(self.FROZEN, wall=103210.1, gripper_percent=52.859)
        self.assertTrue(stall.target_is_newer(self.FROZEN, newer))

    def test_the_same_target_again_is_a_replay_not_a_resume(self):
        self.assertFalse(stall.target_is_newer(self.FROZEN, dict(self.FROZEN)))

    def test_a_changed_payload_from_the_same_moment_is_not_proof_of_forward_motion(self):
        self.assertFalse(stall.target_is_newer(self.FROZEN, dict(self.FROZEN, wall=103205.435889826,
                                                                 gripper_percent=90.0)))

    def test_a_missing_side_is_refused(self):
        self.assertFalse(stall.target_is_newer(None, self.FROZEN))
        self.assertFalse(stall.target_is_newer(self.FROZEN, None))
        self.assertFalse(stall.target_is_newer({}, self.FROZEN), "no wall stamp, no claim")


class ChainSessionResolutionTests(unittest.TestCase):
    """The closer asks the agent that is answering; it never trusts a leftover report."""

    def test_the_one_finalised_session_is_the_one_used(self):
        sid = chain.ready_session(
            listing(entry("ros_old", status="recording"), entry("ros_a")), marker(), ROOT, URL)
        self.assertEqual(sid, "ros_a")

    def test_a_marker_written_by_another_agent_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            chain.ready_session(listing(entry("ros_a")), marker(port=18116), ROOT, URL)
        self.assertIn("18116", str(caught.exception))
        self.assertIn("not the one that wrote", str(caught.exception))

    def test_a_url_without_a_port_cannot_be_matched_to_the_marker(self):
        with self.assertRaises(SystemExit) as caught:
            chain.ready_session(listing(entry("ros_a")), marker(), ROOT, "http://127.0.0.1")
        self.assertIn("no port", str(caught.exception))

    def test_a_marker_naming_a_different_car_root_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            chain.ready_session(listing(entry("ros_a")), marker(car_root="/tmp/other-car"), ROOT, URL)
        self.assertIn("car_root", str(caught.exception))

    def test_two_finalised_sessions_are_refused_instead_of_guessed(self):
        with self.assertRaises(SystemExit) as caught:
            chain.ready_session(listing(entry("ros_a"), entry("ros_b")), marker(), ROOT, URL)
        self.assertIn("exactly one", str(caught.exception))

    def test_a_session_that_never_reached_ready_is_not_synced(self):
        with self.assertRaises(SystemExit):
            chain.ready_session(listing(entry("ros_a", status="recording")), marker(), ROOT, URL)

    def test_a_session_outside_this_car_root_is_not_the_rehearsals(self):
        other = entry("ros_a", path=Path("/tmp/other-car/sessions/ros_a"))
        with self.assertRaises(SystemExit):
            chain.ready_session(listing(other), marker(), ROOT, URL)


if __name__ == "__main__":
    unittest.main()
