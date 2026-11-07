"""Stopping a recorded fixture must really stop it, and must never kill a recycled pid."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
import importlib.util

REPO = pathlib.Path(__file__).resolve().parents[3]

_spec = importlib.util.spec_from_file_location(
    "container_ops",
    str(REPO / "tools" / "ros2_collection" / "local_ros2" / "offline" / "container_ops.py"))
container_ops = importlib.util.module_from_spec(_spec)
sys.modules["container_ops"] = container_ops
_spec.loader.exec_module(container_ops)

IGNORES_TERM = (
    "import signal, time, sys;"
    "signal.signal(signal.SIGTERM, lambda *a: None);"
    "open(sys.argv[1], 'w').write('ready');"
    "time.sleep(60)"
)
HANDS_TERM = (
    "import time, sys;"
    "open(sys.argv[1], 'w').write('ready');"
    "time.sleep(60)"
)


class StopRecorded(unittest.TestCase):
    def spawn(self, directory: pathlib.Path, code: str, marker: str,
              name: str) -> subprocess.Popen:
        """Start a fixture process and wait until it really is in the state under test.

        A child that has not yet installed its handler would die on the first SIGTERM,
        and the test would silently measure the wrong disposition.
        """
        ready = directory / f"{name}.ready"
        proc = subprocess.Popen([sys.executable, "-c", code, str(ready), marker],
                                start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 10.0
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists(), f"{name} never reported readiness")
        return proc

    def pid_file(self, directory: pathlib.Path, proc: subprocess.Popen,
                 stem: str = "camera") -> pathlib.Path:
        path = directory / f"{stem}.pid"
        path.write_text(f"{proc.pid}\n", encoding="utf-8")
        path.with_suffix(".identity.json").write_text(json.dumps({
            "pid": proc.pid,
            "start_ticks": container_ops.proc_start_ticks(proc.pid),
        }), encoding="utf-8")
        return path

    def test_a_process_ignoring_sigterm_is_still_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            proc = self.spawn(directory, IGNORES_TERM, "lelab-fixture-under-test", "stubborn")
            path = self.pid_file(directory, proc)
            report = container_ops.stop_recorded(path, "lelab-fixture-under-test", 0.5)
            self.assertTrue(report["stopped"], report)
            self.assertEqual(report["reason"], "killed_after_term_timeout")
            self.assertEqual(proc.wait(timeout=10), -9)
            self.assertFalse(path.exists(), "the pid file must not outlive the process")

    def test_a_process_that_honours_sigterm_is_not_killed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            proc = self.spawn(directory, HANDS_TERM, "lelab-fixture-under-test", "polite")
            path = self.pid_file(directory, proc)
            report = container_ops.stop_recorded(path, "lelab-fixture-under-test", 10.0)
            self.assertTrue(report["stopped"], report)
            self.assertEqual(report["reason"], "terminated")
            self.assertEqual(proc.wait(timeout=10), -15)

    def test_a_different_command_line_is_never_signalled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            proc = self.spawn(directory, HANDS_TERM, "somebody-elses-process", "foreign")
            path = self.pid_file(directory, proc)
            report = container_ops.stop_recorded(path, "lelab-fixture-under-test", 1.0)
            self.assertFalse(report["stopped"], report)
            self.assertTrue(report["reason"].startswith("cmdline_mismatch"), report)
            self.assertIsNone(proc.poll(), "the wrong process must still be running")

    def test_a_recycled_pid_is_never_signalled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            proc = self.spawn(directory, HANDS_TERM, "lelab-fixture-under-test", "recycled")
            path = self.pid_file(directory, proc)
            path.with_suffix(".identity.json").write_text(
                json.dumps({"pid": proc.pid, "start_ticks": "999999999"}), encoding="utf-8")
            report = container_ops.stop_recorded(path, "lelab-fixture-under-test", 0.5)
            self.assertFalse(report["stopped"], report)
            self.assertIn("pid_recycled", report["reason"])
            self.assertIsNone(proc.poll(), "a recycled pid must not be killed")


if __name__ == "__main__":
    unittest.main()
