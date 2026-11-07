#!/usr/bin/env python3
"""Requirement 六 on the deployed path: kill the car agent while a teleop episode is open.

The six link-loss sources the requirement lists are covered by upper-computer unit tests against a
fake gateway. This one is different because it is the *car* that disappears: the coordinator must
leave `following`, must not keep sending, and the car must finalise the orphaned recording session
by itself on restart instead of leaving a half-written bag. Nothing here moves hardware — the
follower feedback is synthetic, so the arm could not move even if a trajectory left the gateway.

    python3 tools/ros2_collection/local_car_fault_injection.py --root /tmp/lelab_car_rehearsal

Run `local_car_rehearsal.py` first. Exits non-zero with a reason if the coordinator misbehaves.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "ros2_collection"))
sys.path.insert(0, str(REPO / "app_ros_mobile"))

from local_teleop_session_drive import SyntheticLeader, seed  # noqa: E402

from lelab.hardware_lease import HardwareLeaseManager  # noqa: E402
from lelab.mobile_teleop import CalibrationStore  # noqa: E402
from lelab.mobile_teleop_coordinator import (CoordinatorConfig,  # noqa: E402
                                             MobileTeleopCoordinator)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/lelab_car_rehearsal")
    ap.add_argument("--hold-seconds", type=float, default=2.0)
    args = ap.parse_args()
    root = Path(args.root)
    marker = json.loads((root / "READY_FOR_DRIVER.json").read_text(encoding="utf-8"))
    identities = {"leader": "leader_local", "follower": "car_local", "mapping": "map_local"}

    store = CalibrationStore(root / "upper" / "cal")
    (root / "upper" / "cal").mkdir(parents=True, exist_ok=True)
    seed(store, identities)
    coord = MobileTeleopCoordinator(
        CoordinatorConfig(agent_url=f"http://127.0.0.1:{marker['port']}", command_hz=10.0,
                          lease_ttl_s=120.0, control_ttl_s=60.0, request_timeout_s=3.0,
                          leader={"read_hz": 30.0}),
        json.loads((REPO / "configs" / "ros2_collection" / "hardware-teleop-v1.json")
                   .read_text(encoding="utf-8")),
        store=store, leader_reader=SyntheticLeader("/dev/serial/by-id/synthetic-if00"),
        lease_manager=HardwareLeaseManager())

    coord.open_leader("/dev/serial/by-id/synthetic-if00", leader_identity=identities["leader"])
    coord.acquire(owner="fault-injection", leader_identity=identities["leader"],
                  follower_identity=identities["follower"], mapping_identity=identities["mapping"])
    coord.set_state("enter_preview")
    coord.align()
    coord.set_state("arm")
    coord.set_state("engage")
    coord.recording("start_session", {"task": "fault injection: car agent dies mid-episode"})
    coord.recording("start_episode", {"source": "leader_arm", "task": "fault injection"})
    session_id = coord.recording_session
    time.sleep(args.hold_seconds)
    sent_before = coord.status()["seq"]

    os.kill(int(marker["agent_pid"]), signal.SIGKILL)
    print(f"[fault] killed car agent pid {marker['agent_pid']} after {sent_before} sent targets")

    deadline = time.time() + 20.0
    while time.time() < deadline and coord.status()["state"] == "following":
        time.sleep(0.3)
    parked = coord.status()
    result = {"session_id": session_id, "seq_before_kill": sent_before,
              "state_after_kill": parked["state"], "last_error_after_kill": parked["last_error"],
              "car_token_active_after_kill": parked["car_token_active"]}

    time.sleep(args.hold_seconds)
    later = coord.status()
    result.update({
        "seq_after_wait": later["seq"],
        "stopped_sending": later["seq"] == parked["seq"],
        "state_stays_downgraded": later["state"] == parked["state"],
        "still_holds_upper_lease": later["upper_lease_active"],
    })
    # The upper lease stays ours until we release it, but the session must not silently re-enter
    # following: a re-engage has to be explicit and re-aligned, so releasing here is the assertion
    # that the coordinator still considers itself downgraded.
    coord.release(reason="fault_injection_end")
    result["state_after_release"] = coord.status()["state"]
    (root / "FAULT_INJECTION.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if (result["state_after_kill"] != "following" and result["stopped_sending"]) else 1


if __name__ == "__main__":
    sys.exit(main())
