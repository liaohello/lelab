"""Recorder fault-path tests that do not require a live ROS graph."""
from __future__ import annotations

import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
