"""Recorder fault-path tests that do not require a live ROS graph."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "lelab_mobile_agent"))

from lelab_mobile_agent import recorder as recorder_module  # noqa: E402


class FakeBagProcess:
    def __init__(self):
        self.pid = 2**31 - 1
        self.returncode = 0
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def wait(self, timeout=None):
        self.stopped = True
        return 0


class SilentLog:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class RecorderFaultTests(unittest.TestCase):
    def test_mid_session_disk_low_stops_without_ready(self):
        root = Path(tempfile.mkdtemp(prefix="lelab_mid_disk_"))
        profile = {
            "profile": "mid_disk_fault",
            "runtime": {"clock_domain": "test_clock"},
            "task": {"language": "observe"},
            "recording": {
                "topics": ["/lelab/events"],
                "required_topics": [],
                "min_free_bytes": 1,
                "raw_storage": "sqlite3",
            },
        }
        session = recorder_module.SessionRecorder(root, profile, SilentLog())
        with patch.object(recorder_module.subprocess, "Popen", return_value=FakeBagProcess()), \
             patch.object(session, "_startup_check", return_value=True), \
             patch.object(session, "_bag_info", return_value=(False, "fake unreadable bag")):
            session.start_session("mid_disk_session", "observe", {}, 1)
            session.min_free_bytes = 10**18
            session.poll_health({})
            self.assertEqual(session.error, "disk_low")
            result = session.stop_session(reason="mid_disk_fault")
        self.assertEqual(result["status"], "error")
        self.assertFalse((root / "sessions" / "mid_disk_session" / "READY").exists())
        manifest = (root / "sessions" / "mid_disk_session" / "manifest.json").read_text()
        self.assertIn("disk_low", manifest)

    def test_clock_jump_stops_without_ready(self):
        root = Path(tempfile.mkdtemp(prefix="lelab_clock_jump_"))
        profile = {
            "profile": "clock_jump_fault",
            "runtime": {"clock_domain": "hardware_system_time"},
            "task": {"language": "observe"},
            "recording": {
                "topics": ["/lelab/events"],
                "required_topics": [],
                "min_free_bytes": 1,
                "raw_storage": "sqlite3",
            },
        }
        session = recorder_module.SessionRecorder(root, profile, SilentLog())
        with patch.object(recorder_module.subprocess, "Popen", return_value=FakeBagProcess()), \
             patch.object(session, "_startup_check", return_value=True), \
             patch.object(session, "_bag_info", return_value=(False, "fake unreadable bag")):
            session.start_session("clock_jump_session", "observe", {}, 1)
            self.assertTrue(session.note_clock_jump("backward 120s"))
            self.assertEqual(session.error, "clock_jump")
            result = session.stop_session(reason="clock_jump")
        self.assertEqual(result["status"], "error")
        self.assertFalse((root / "sessions" / "clock_jump_session" / "READY").exists())
        manifest = (root / "sessions" / "clock_jump_session" / "manifest.json").read_text()
        self.assertIn("clock_jump", manifest)


    def test_episode_end_reason_is_persisted_and_honest(self):
        root = Path(tempfile.mkdtemp(prefix="lelab_end_reason_"))
        profile = {
            "profile": "end_reason_case",
            "runtime": {"clock_domain": "test_clock"},
            "task": {"language": "observe"},
            "recording": {
                "topics": ["/lelab/events"],
                "required_topics": [],
                "min_free_bytes": 1,
                "raw_storage": "sqlite3",
            },
        }
        session = recorder_module.SessionRecorder(root, profile, SilentLog())
        with patch.object(recorder_module.subprocess, "Popen", return_value=FakeBagProcess()), \
             patch.object(session, "_startup_check", return_value=True), \
             patch.object(session, "_bag_info", return_value=(True, "Duration: 3.0s\nMessages: 3\nFiles: x")):
            session.start_session("end_reason_session", "observe", {}, 1)
            session.start_episode("ep_saved", "observe", "hardware_readonly", 2, {})
            session.finish_episode(3, {"success": "unknown"}, discarded=False, reason="operator_end")
            session.start_episode("ep_forced", "observe", "hardware_readonly", 4, {})
            session.finish_episode(5, {"success": "unknown"}, discarded=True, reason="forced_by_required_stream_missing")
            session.start_episode("ep_open", "observe", "hardware_readonly", 6, {})
            session.stop_session(reason="operator-stop")
        manifest = json.loads((root / "sessions" / "end_reason_session" / "manifest.json").read_text())
        reasons = {ep["episode_id"]: (ep["status"], ep.get("end_reason")) for ep in manifest["episodes"]}
        self.assertEqual(reasons["ep_saved"], ("saved", "operator_end"))
        self.assertEqual(reasons["ep_forced"], ("discarded", "forced_by_required_stream_missing"))
        self.assertEqual(reasons["ep_open"], ("interrupted", "episode_open_at_stop"))
        episode_file = json.loads(
            (root / "sessions" / "end_reason_session" / "episodes" / "ep_forced.json").read_text()
        )
        self.assertEqual(episode_file["end_reason"], "forced_by_required_stream_missing")
        self.assertEqual(episode_file["ended_time_ns"], 5)
        open_file = json.loads(
            (root / "sessions" / "end_reason_session" / "episodes" / "ep_open.json").read_text()
        )
        self.assertIsNone(open_file["ended_time_ns"], "interrupted episode must not claim an end stamp")
        self.assertEqual(open_file["end_reason"], "episode_open_at_stop")

    def test_stop_session_with_open_episode_is_not_ready(self):
        root = Path(tempfile.mkdtemp(prefix="lelab_open_ep_"))
        profile = {
            "profile": "open_ep_fault",
            "runtime": {"clock_domain": "test_clock"},
            "task": {"language": "observe"},
            "recording": {
                "topics": ["/lelab/events"],
                "required_topics": [],
                "min_free_bytes": 1,
                "raw_storage": "sqlite3",
            },
        }
        session = recorder_module.SessionRecorder(root, profile, SilentLog())
        with patch.object(recorder_module.subprocess, "Popen", return_value=FakeBagProcess()), \
             patch.object(session, "_startup_check", return_value=True), \
             patch.object(session, "_bag_info", return_value=(True, "Duration: 3.0s\nMessages: 3\nFiles: x")):
            session.start_session("open_ep_session", "observe", {}, 1)
            session.start_episode("ep_open", "observe", "hardware_readonly", 2, {})
            result = session.stop_session(reason="upper_disconnected")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "episode_open_at_stop")
        self.assertFalse((root / "sessions" / "open_ep_session" / "READY").exists())


    def test_bag_record_cli_is_humble_compatible(self):
        """Humble's `ros2 bag record` has no --topics flag; topics must stay positional."""
        root = Path(tempfile.mkdtemp(prefix="lelab_bag_cli_"))
        profile = {
            "profile": "bag_cli",
            "runtime": {"clock_domain": "test_clock"},
            "task": {"language": "observe"},
            "recording": {
                "topics": ["/rgb/image", "/rgb/camera_info"],
                "required_topics": ["/rgb/image"],
                "min_free_bytes": 1,
                "raw_storage": "sqlite3",
            },
        }
        session = recorder_module.SessionRecorder(root, profile, SilentLog())
        captured = {}

        def fake_popen(cmd, *args, **kwargs):
            captured["cmd"] = list(cmd)
            return FakeBagProcess()

        with patch.object(recorder_module.subprocess, "Popen", side_effect=fake_popen), \
             patch.object(session, "_startup_check", return_value=True), \
             patch.object(session, "_bag_info", return_value=(True, "Duration: 1.0s\nMessages: 1\nFiles: x")):
            session.start_session("bag_cli_session", "observe", {}, 1)
            session.stop_session(reason="normal")
        cmd = captured["cmd"]
        self.assertNotIn("--topics", cmd)
        self.assertEqual(cmd[:3], ["ros2", "bag", "record"])
        self.assertIn("--output", cmd)
        self.assertEqual(cmd[-2:], ["/rgb/image", "/rgb/camera_info"])



class InterruptedSessionRecoveryTests(unittest.TestCase):
    """A SIGKILLed agent (how the Pi loses this service on a power cut) must not orphan anything.

    The graceful-shutdown path already finalises the session, so nothing before this covered the
    crash: the `ros2 bag record` child keeps writing into a directory nobody owns any more and
    `session.json` stays open forever. These spawn a real stand-in recorder process, because the
    whole point is which process the recovery is allowed to signal.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def recorder(self):
        return recorder_module.SessionRecorder(self.root, {"recording": {}}, SilentLog())

    def session(self, name: str, status: str) -> Path:
        session_dir = self.root / "sessions" / name
        (session_dir / "raw" / "bag").mkdir(parents=True)
        (session_dir / "session.json").write_text(json.dumps(
            {"session_id": name, "status": status, "task": "teleop"}), encoding="utf-8")
        return session_dir

    def sleeper(self, output_dir: Path):
        import subprocess
        import time
        proc = subprocess.Popen(
            [sys.executable, "-c", "import signal,sys,time;"
             "signal.signal(signal.SIGINT, lambda *_a: sys.exit(0)); time.sleep(60)",
             "ros2", "bag", "record", "--output", str(output_dir)], start_new_session=True)
        time.sleep(0.4)
        return proc

    def test_a_session_left_open_is_closed_and_only_its_own_recorder_is_stopped(self):
        import time
        crashed = self.session("ros_crash", "recording")
        other = self.session("ros_other", "raw_ready")
        victim = self.sleeper(crashed / "raw" / "bag")
        bystander = self.sleeper(other / "raw" / "bag")
        try:
            out = self.recorder().recover_interrupted_sessions()
            self.assertEqual([o["session_id"] for o in out], ["ros_crash"])
            self.assertEqual(victim.wait(timeout=5) is not None, True, "its recorder must be reaped")
            self.assertIsNone(bystander.poll(), "a recorder owned by another session stays alive")
            meta = json.loads((crashed / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["status"], "error")
            self.assertEqual(meta["stop_reason"], "agent_restarted_mid_session")
            self.assertIs(meta["ready_eligible"], False,
                          "a half-written bag is kept for forensics, never published as ready")
            self.assertFalse((crashed / "READY").exists())
            self.assertIn(victim.pid, out[0]["orphan_recorders"])
            time.sleep(0.1)
        finally:
            for proc in (victim, bystander):
                proc.terminate()
                proc.wait(timeout=5)

    def test_finalised_sessions_are_not_touched(self):
        good = self.session("ros_done", "raw_ready")
        bad = self.session("ros_broken", "error")
        out = self.recorder().recover_interrupted_sessions()
        self.assertEqual(out, [])
        for session_dir in (good, bad):
            self.assertNotEqual(json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
                                .get("stop_reason"), "agent_restarted_mid_session")

    def test_recovery_survives_a_missing_or_unreadable_root(self):
        self.assertEqual(recorder_module.SessionRecorder(
            self.root / "does-not-exist", {"recording": {}}, SilentLog()
        ).recover_interrupted_sessions(), [])
        (self.recorder().root / "ros_junk").mkdir()
        (self.recorder().root / "ros_junk" / "session.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.recorder().recover_interrupted_sessions(), [],
                         "an unparsable session is left for list_sessions to label, not rewritten")

class HeldLock:
    """Hold the recorder lock from another thread, the way a bag finalization does.

    ``status_snapshot`` is allowed to re-enter its own thread's lock, so only a second
    thread makes a non-blocking read actually non-blocking.
    """

    def __init__(self, lock):
        self.lock = lock
        self.holding = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with self.lock:
            self.holding.set()
            self.release.wait(10)

    def __enter__(self):
        self.thread.start()
        if not self.holding.wait(2):
            raise AssertionError("the helper thread never took the recorder lock")
        return self

    def __exit__(self, *_exc):
        self.release.set()
        self.thread.join(2)


class RecorderStatusReadTests(unittest.TestCase):
    """A ``/status`` read must never queue behind the recorder's own finalization.

    The upper computer polls ``/status`` several times a second for its lease and
    observation freshness, and ``stop_session`` holds the recorder lock while it waits
    up to 20 s for the bag process and hashes every file. That convoy previously made a
    healthy lease read as ``lease_lost`` and kept ``/hybrid/stop`` waiting behind it.
    """

    def session(self, root):
        profile = {
            "profile": "status_read", "runtime": {"clock_domain": "test_clock"},
            "task": {"language": "observe"},
            "recording": {"topics": ["/lelab/events"], "required_topics": [],
                          "min_free_bytes": 1, "raw_storage": "sqlite3"},
        }
        return recorder_module.SessionRecorder(Path(root), profile, SilentLog())

    def live(self, session):
        with patch.object(recorder_module.subprocess, "Popen", return_value=FakeBagProcess()), \
             patch.object(session, "_startup_check", return_value=True):
            session.start_session("ros_status_read", "observe", {"recording_topics": []}, 1)

    def test_status_snapshot_answers_from_the_cache_while_locked(self):
        root = tempfile.mkdtemp(prefix="lelab_status_read_")
        session = self.session(root)
        self.live(session)
        before = session.status()
        self.assertNotIn("recording_status_cached", before)
        with HeldLock(session.lock):
            started = time.monotonic()
            out = session.status_snapshot()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5, "the reader waited for the recorder lock")
        self.assertIs(out["recording_status_cached"], True)
        self.assertEqual(out["session_id"], before["session_id"])
        self.assertIsNotNone(out["recording_status_cache_age_s"])

    def test_status_snapshot_says_unavailable_before_any_cache(self):
        root = tempfile.mkdtemp(prefix="lelab_status_read_cold_")
        session = self.session(root)
        self.live(session)
        with HeldLock(session.lock):
            out = session.status_snapshot()
        self.assertIs(out["recording_status_unavailable"], True)
        self.assertEqual(out["reason"], "recorder_busy")
        self.assertIsNone(out["session_id"])

    def test_status_snapshot_reads_live_once_the_lock_is_free(self):
        root = tempfile.mkdtemp(prefix="lelab_status_read_free_")
        session = self.session(root)
        self.live(session)
        with HeldLock(session.lock):
            pass
        out = session.status_snapshot()
        self.assertNotIn("recording_status_cached", out)
        self.assertEqual(out["session_id"], "ros_status_read")

    def test_the_blocking_read_still_shows_the_shape_of_the_old_convoy(self):
        """What ``/status`` used to do, measured rather than asserted in prose.

        ``agent.status()`` reached the recorder through ``status()``, so a held lock
        held the whole status route; that is how a healthy lease came to read as lost.
        """
        root = tempfile.mkdtemp(prefix="lelab_status_read_convoy_")
        session = self.session(root)
        self.live(session)
        session.status()  # publish once, so the non-blocking reader has something to serve
        result = []
        with HeldLock(session.lock):
            worker = threading.Thread(target=lambda: result.append(session.status()),
                                      daemon=True)
            worker.start()
            worker.join(0.6)
            self.assertTrue(worker.is_alive(),
                            "the blocking reader should still be waiting on the lock")
            snapshot = session.status_snapshot()
            self.assertIs(snapshot["recording_status_cached"], True,
                          "the non-blocking reader is the one that stays available")
        worker.join(5)
        self.assertFalse(worker.is_alive(), "the blocking reader finishes once the lock frees")

    def test_the_cached_snapshot_is_a_detached_copy(self):
        root = tempfile.mkdtemp(prefix="lelab_status_read_detached_")
        session = self.session(root)
        self.live(session)
        session.status()
        with session.lock:
            session._status_cache["session_id"] = "cache_only"
        with HeldLock(session.lock):
            out = session.status_snapshot()
        self.assertEqual(out["session_id"], "cache_only",
                         "a locked-out read must be served from the cache")
        self.assertEqual(session.status()["session_id"], "ros_status_read",
                         "the cache is a copy, not the recorder's own state")


if __name__ == "__main__":
    unittest.main()
