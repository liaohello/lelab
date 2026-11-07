#!/usr/bin/env python3
"""OFFLINE ONLY - drive the production SessionRecorder against a real Humble bag.

This is the recorder path the car-side agent uses; only the *device boundary* is
simulated (the board owner and camera here are synthetic).  Nothing here fakes the
bag subprocess: `ros2 bag record` is started by SessionRecorder itself, the sqlite3
archive is then read back with rosbag2_py, and the per-topic counts and the READY
document are printed for the acceptance report.

Data written here is tagged simulated_offline and is not training eligible.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

CAR_PKG = os.environ.get("CAR_PKG", "/repo/ros2_ws/src/lelab_mobile_agent")
if CAR_PKG not in sys.path:
    sys.path.insert(0, CAR_PKG)

from lelab_mobile_agent.recorder import SessionRecorder  # noqa: E402
from rosbag2_py import (ConverterOptions, SequentialReader, StorageFilter,
                              StorageOptions)  # noqa: E402

DATA_IDENTITY = "simulated_offline"


def bag_counts(bag_dir: Path) -> dict[str, int]:
    """Read the archive back with the ROS 2 storage API rather than trusting files."""
    reader = SequentialReader()
    # Humble signature: open(StorageOptions, ConverterOptions); a filter is set after open.
    reader.open(StorageOptions(uri=str(bag_dir), storage_id="sqlite3"), ConverterOptions())
    reader.set_filter(StorageFilter(topics=[]))
    counts: dict[str, int] = {}
    while reader.has_next():
        topic, _payload, _stamp = reader.read_next()
        counts[topic] = counts.get(topic, 0) + 1
    # SequentialReader has no close() in Humble; dropping the object releases the file.
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--car-root", default="/out/data")
    parser.add_argument("--episodes", type=int, default=2)
    parser.add_argument("--observe-s", type=float, default=2.0)
    parser.add_argument("--grasp-s", type=float, default=2.0)
    parser.add_argument("--drive", default="", help="command to run while recording")
    parser.add_argument("--report", default="/out/evidence/recording-report.json")
    args = parser.parse_args()

    profile_path = Path(args.profile)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    logger = logging.getLogger("offline_recorder")
    recorder = SessionRecorder(args.car_root, profile, logger)
    recovered = recorder.recover_interrupted_sessions()
    summary = {"data_identity": DATA_IDENTITY, "training_eligible": False,
               "profile": profile["profile"],
               "profile_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
               "recovered_orphans": len(recovered), "sessions": []}

    driver = None
    if args.drive:
        driver = subprocess.Popen(args.drive, shell=True, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.STDOUT)
    for index in range(args.episodes):
        session_id = f"offline6d-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{index + 1}"
        started = recorder.start_session(
            session_id, task="mobile_arm_gripper_teleop",
            metadata={"profile_sha256": summary["profile_sha256"],
                      "data_identity": DATA_IDENTITY, "training_eligible": False,
                      "origin": "local_humble_offline_acceptance"},
            time_ns=time.time_ns())
        recorder.bind_epochs(session_epoch=1, clock_epoch="offline-car-clock")
        episode_id = f"ep{index + 1}"
        recorder.start_episode(episode_id, task="observe then grasp", source="so101_leader",
                               time_ns=time.time_ns(),
                               initial={"note": "simulated board positions"})
        recorder.set_phase("observe", time_ns=time.time_ns(), session_epoch=1)
        time.sleep(args.observe_s)
        recorder.set_phase("grasp", time_ns=time.time_ns(), session_epoch=1)
        time.sleep(args.grasp_s)
        recorder.set_phase("drive", time_ns=time.time_ns(), session_epoch=1)
        recorder.finish_episode(time_ns=time.time_ns(),
                                evaluation={"status": "simulated", "success": "unknown"},
                                discarded=False)
        health = recorder.poll_health()
        # Captured before the stop: stop_session clears both paths on the way out.
        bag_dir = str(recorder.bag_dir or "")
        session_dir = str(recorder.session_dir or "")
        stopped = recorder.stop_session("normal")
        entry = {"session_id": session_id, "start": started, "stop": stopped,
                 "health": {"ok": health.get("ok"), "problems": health.get("problems")},
                 "bag_dir": bag_dir, "session_dir": session_dir}
        if driver is not None and driver.poll() is not None:
            entry["drive_exit"] = driver.returncode
        summary["sessions"].append(entry)

    if driver is not None and driver.poll() is None:
        driver.terminate()
        driver.wait(timeout=15)

    # Verify what the recorder claims to have kept.
    for entry in summary["sessions"]:
        session_dir = Path(entry["session_dir"])
        ready = session_dir / "READY"
        manifest = session_dir / "manifest.json"
        entry["ready"] = ready.exists()
        entry["ready_document"] = json.loads(ready.read_text()) if ready.exists() else None
        entry["manifest_status"] = (json.loads(manifest.read_text()).get("status")
                                    if manifest.exists() else None)
        entry["bag_dir_exists"] = Path(entry["bag_dir"]).is_dir()
        try:
            entry["topic_message_counts"] = bag_counts(Path(entry["bag_dir"]))
        except Exception as error:      # noqa: BLE001 - reported, not hidden
            entry["bag_read_error"] = f"{type(error).__name__}: {error}"
    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n",
                      encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    failed = [entry["session_id"] for entry in summary["sessions"]
              if not entry.get("ready") or entry.get("bag_read_error")]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
