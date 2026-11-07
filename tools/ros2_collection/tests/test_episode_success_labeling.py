"""Success labels on real hardware: manual or unknown, never a borrowed simulator verdict.

需求七 forbids judging teleop episodes with Gazebo ground truth. The physics gate in ``_evaluate()``
reads privileged cube/container poses that never arrive on the car, so without a policy branch every
teleop episode would be recorded as ``success: False`` and then dropped by the export filter. The
deployed agent is imported for real; only node construction is bypassed.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import types
import unittest
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "lelab_mobile_agent"))

from lelab_mobile_agent.contract import (evaluation_policy, load_profile,  # noqa: E402
                                         manual_evaluation, manual_success_label)

try:
    from lelab_mobile_agent.agent import MobileAgent
    AGENT_IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    MobileAgent = None
    AGENT_IMPORT_ERROR = exc

TELEOP_PROFILE = REPO / "configs" / "ros2_collection" / "hardware-teleop-v1.json"
SIM_PROFILE = REPO / "configs" / "ros2_collection" / "runtime-profile-v1.json"


class PolicyTests(unittest.TestCase):
    def test_the_teleop_profiles_declare_no_simulator_truth(self):
        for path in (TELEOP_PROFILE,
                     REPO / "ros2_ws" / "src" / "lelab_mobile_agent" / "config" / "hardware-teleop-v1.json"):
            policy = evaluation_policy(load_profile(path)["task"])
            self.assertEqual(policy, {"source": "manual_or_unknown", "simulator_truth_available": False},
                             path.name)

    def test_the_simulation_profile_keeps_the_physics_gate(self):
        """The verified scripted-expert behaviour must not be relaxed by this change."""
        self.assertTrue(evaluation_policy(load_profile(SIM_PROFILE)["task"])["simulator_truth_available"])

    def test_labels_are_the_only_switch(self):
        self.assertTrue(evaluation_policy({})["simulator_truth_available"])
        self.assertFalse(evaluation_policy({"labels": "manual_or_unknown"})["simulator_truth_available"])

    def test_an_unlabelled_episode_is_unknown_and_not_failed(self):
        evaluation = manual_evaluation({"labels": "manual_or_unknown"}, task="teleop",
                                       reason="no_simulator_ground_truth_declared_by_profile")
        self.assertEqual(evaluation["success"], "unknown")
        self.assertIsNot(evaluation["success"], False, "unknown must never be written as a failure")

    def test_a_manual_label_overrides_unknown(self):
        ok = manual_evaluation({"labels": "manual_or_unknown"}, task="teleop", reason="x",
                               manual_success=True, note="cube placed")
        self.assertIs(ok["success"], True)
        self.assertIs(ok["manual_label"], True)
        self.assertEqual(ok["note"], "cube placed")

    def test_client_labels_are_normalised_and_garbage_stays_unlabelled(self):
        self.assertIs(manual_success_label(True), True)
        self.assertIs(manual_success_label("false"), False)
        self.assertIs(manual_success_label("failure"), False)
        for junk in (None, "", "maybe", 2, {"success": True}):
            self.assertIsNone(manual_success_label(junk))


def fake_recording_agent(profile_path=TELEOP_PROFILE):
    """A MobileAgent holding only what end_episode() touches."""
    if MobileAgent is None:
        raise AGENT_IMPORT_ERROR
    profile = load_profile(profile_path)
    node = object.__new__(MobileAgent)
    node.profile = profile
    node.read_only = profile["runtime"]["mode"] == "hardware_readonly"
    node.lock = threading.RLock()
    node.control_token, node.control_expires = "tok", time.monotonic() + 30
    node.task_name = profile["task"]["id"]
    node.camera_profile = "wrist_original"
    node.session_id, node.episode_id = "ros_session", "ep_1"
    node.episode_phases = []
    node.clock_domain = "monotonic"
    node.events, node.rejections = deque(maxlen=100), deque(maxlen=100)
    node.get_name = lambda: "lelab_mobile_agent"
    node.get_logger = lambda: types.SimpleNamespace(
        warning=lambda *a: None, info=lambda *a: None, error=lambda *a: None)
    node.get_clock = lambda: types.SimpleNamespace(now=lambda: types.SimpleNamespace(nanoseconds=123))
    node._sim_ns = lambda: 123

    finished = {}

    def finish_episode(time_ns, evaluation, discarded=False, reason=""):
        finished.update({"evaluation": evaluation, "discarded": discarded, "reason": reason})
        return {"episode_id": "ep_1", "status": "saved"}

    node.sessions = types.SimpleNamespace(current_episode=object(), finish_episode=finish_episode)
    node.finished = finished
    return node


class DeployedAgentTests(unittest.TestCase):
    def setUp(self):
        if MobileAgent is None:
            raise AGENT_IMPORT_ERROR

    def test_end_episode_stores_unknown_instead_of_a_physics_failure(self):
        node = fake_recording_agent()
        out = node.end_episode("tok", reason="operator_finished")
        self.assertEqual(out["evaluation"]["success"], "unknown")
        self.assertEqual(node.finished["evaluation"]["success"], "unknown",
                         "the verdict that reaches the manifest is the one the export filter reads")
        self.assertEqual(node.finished["evaluation"]["labels"], "manual_or_unknown")
        self.assertEqual(node.finished["evaluation"]["failure_reasons"], [],
                         "no reasons may be invented from absent simulator topics")
        self.assertIsNone(out["evaluation"].get("manual_label"))

    def test_the_operators_label_is_what_gets_recorded(self):
        node = fake_recording_agent()
        node.end_episode("tok", reason="operator_finished", manual_success="success", note="placed on box")
        evaluation = node.finished["evaluation"]
        self.assertIs(evaluation["success"], True)
        self.assertEqual(evaluation["note"], "placed on box")
        self.assertEqual(evaluation["evaluation_policy"]["source"], "manual_or_unknown")

    def test_a_declared_failure_is_recorded_as_failure(self):
        node = fake_recording_agent()
        node.end_episode("tok", manual_success=False)
        self.assertIs(node.finished["evaluation"]["success"], False)

    def test_an_explicit_failure_label_is_not_confused_with_garbage(self):
        node = fake_recording_agent()
        node.end_episode("tok", manual_success="nope")
        self.assertEqual(node.finished["evaluation"]["success"], "unknown",
                         "an unreadable label keeps the episode unknown rather than inventing a verdict")

    def test_the_record_end_phase_carries_the_same_verdict_as_the_manifest(self):
        node = fake_recording_agent()
        node.end_episode("tok", manual_success=True)
        self.assertEqual(node.episode_phases[-1]["phase"], "record_end")
        self.assertIs(node.episode_phases[-1]["success"], True)

    def test_the_readonly_branch_is_untouched(self):
        """Read-only sessions keep their own labels and stay non-exportable; this change must not relabel them."""
        node = fake_recording_agent(REPO / "configs" / "ros2_collection" / "hardware-readonly-v1.json")
        node.end_episode("tok")
        evaluation = node.finished["evaluation"]
        self.assertEqual(evaluation["reason"], "hardware_readonly_has_no_actuation_or_state_labels")
        self.assertEqual(evaluation["labels"], "unlabeled")
        self.assertEqual(evaluation["success"], "unknown")


class FakeBagProcess:
    def __init__(self):
        self.pid = 2 ** 31 - 1
        self.returncode = 0
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def wait(self, timeout=None):
        self.stopped = True
        return 0


class SilentLog:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class ManifestPersistenceTests(unittest.TestCase):
    """The label has to survive onto disk, because the export filter reads it from the manifest."""

    def setUp(self):
        if MobileAgent is None:
            raise AGENT_IMPORT_ERROR
        from unittest.mock import patch

        from lelab_mobile_agent import recorder as recorder_module
        self.recorder_module = recorder_module
        self.patch = patch
        self.root = Path(tempfile.mkdtemp(prefix="label_manifest_"))

    def _recorder(self):
        profile = {"profile": "label_case", "runtime": {"clock_domain": "test_clock"},
                   "task": {"language": "teleop"},
                   "recording": {"topics": ["/lelab/events"], "required_topics": [],
                                 "min_free_bytes": 1, "raw_storage": "sqlite3"}}
        session = self.recorder_module.SessionRecorder(self.root, profile, SilentLog())
        return self.patch.object(self.recorder_module.subprocess, "Popen", return_value=FakeBagProcess()), \
            self.patch.object(session, "_startup_check", return_value=True), \
            self.patch.object(session, "_bag_info", return_value=(True, "Duration: 3.0s\nMessages: 3\nFiles: x")), session

    def test_the_agents_verdict_lands_in_the_manifest_the_exporter_reads(self):
        node = fake_recording_agent()
        popen, startup, bag_info, recorder = self._recorder()
        with popen, startup, bag_info:
            recorder.start_session("ros_label", "teleop", {}, 1)
            recorder.start_episode("ep_label", "teleop", "leader_arm", 2, {})
            node.sessions = recorder
            node.episode_id = "ep_label"
            node.end_episode("tok", reason="operator_end", manual_success="failure", note="dropped it")
            recorder.stop_session(reason="operator-stop")
        manifest = json.loads((self.root / "sessions" / "ros_label" / "manifest.json").read_text(encoding="utf-8"))
        episode = {e["episode_id"]: e for e in manifest["episodes"]}["ep_label"]
        self.assertEqual(episode["status"], "saved")
        self.assertIs(episode["evaluation"]["success"], False,
                      "an explicit failure label must be readable by the export filter")
        self.assertEqual(episode["evaluation"]["note"], "dropped it")

    def test_an_unlabelled_episode_is_saved_as_unknown_and_stays_exportable(self):
        node = fake_recording_agent()
        popen, startup, bag_info, recorder = self._recorder()
        with popen, startup, bag_info:
            recorder.start_session("ros_unknown", "teleop", {}, 1)
            recorder.start_episode("ep_open", "teleop", "leader_arm", 2, {})
            node.sessions = recorder
            node.episode_id = "ep_open"
            node.end_episode("tok")
            recorder.stop_session(reason="operator-stop")
        manifest = json.loads((self.root / "sessions" / "ros_unknown" / "manifest.json").read_text(encoding="utf-8"))
        episode = {e["episode_id"]: e for e in manifest["episodes"]}["ep_open"]
        self.assertEqual(episode["status"], "saved")
        self.assertEqual(episode["evaluation"]["success"], "unknown")
        self.assertIsNot(episode["evaluation"]["success"], False,
                         "unknown must not be written as a failure, which the exporter would drop")


if __name__ == "__main__":
    unittest.main()
