#!/usr/bin/env python3
"""六's remaining local case: the upper computer exits while a teleop session is live.

LeLab's shutdown hook calls `MobileTeleopCoordinator.shutdown()`; unit tests cover that hook against
a fake gateway. This runs the same production entry point against the deployed car agent, with a
session following and recording, and checks what the car is left with: no follow stream, no upper
lease, no car control lease, the leader port released, and a session that ended in one of the two
declared ways rather than silently half-open. The property that matters for safety is that nothing
may still be sent after the exit - not that the exit is graceful.

Run `local_car_rehearsal.py --root <root>` first, then:

    app_ros_mobile/.venv/bin/python tools/ros2_collection/local_car_upper_exit_rehearsal.py --root <root>

Leader and follower feedback stay synthetic; nothing here can move an arm. Exits non-zero with the
named expectation that failed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for path in (REPO / "tools" / "ros2_collection", REPO / "app_ros_mobile"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from local_teleop_session_drive import SyntheticLeader, seed  # noqa: E402

from lelab.hardware_lease import HardwareLeaseManager  # noqa: E402
from lelab.mobile_teleop import CalibrationStore  # noqa: E402
from lelab.mobile_teleop_coordinator import (CoordinatorConfig,  # noqa: E402
                                             MobileTeleopCoordinator)

PORT = "/dev/serial/by-id/synthetic-if00"
IDENTITIES = {"leader": "leader_local", "follower": "car_local", "mapping": "map_local"}


def car_view(url: str) -> dict:
    with urllib.request.urlopen(url + "/status", timeout=2.0) as response:
        body = json.loads(response.read())
    control = body.get("control") or {}
    teleop = control.get("teleop") or {}
    return {"active": control.get("active"), "owner": control.get("client_id"),
            "mode": control.get("control_mode"), "stream": teleop.get("stream"),
            "last": teleop.get("last")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/lelab_car_rehearsal")
    ap.add_argument("--follow-seconds", type=float, default=2.0)
    ap.add_argument("--observe-seconds", type=float, default=2.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    root = Path(args.root)
    marker = json.loads((root / "READY_FOR_DRIVER.json").read_text(encoding="utf-8"))
    url = f"http://127.0.0.1:{marker['port']}"
    profile = json.loads((REPO / "configs" / "ros2_collection" / "hardware-teleop-v1.json")
                         .read_text(encoding="utf-8"))
    scratch = root / "upper"
    (scratch / "cal").mkdir(parents=True, exist_ok=True)
    store = CalibrationStore(scratch / "cal")
    seed(store, IDENTITIES)
    leader = SyntheticLeader(PORT)
    lease = HardwareLeaseManager()
    coord = MobileTeleopCoordinator(
        CoordinatorConfig(agent_url=url, command_hz=10.0, lease_ttl_s=60.0, control_ttl_s=30.0,
                          request_timeout_s=5.0, leader={"read_hz": 30.0}),
        profile, store=store, leader_reader=leader, lease_manager=lease)

    steps: dict = {"agent_url": url}
    coord.open_leader(PORT, leader_identity=IDENTITIES["leader"])
    coord.acquire(owner="exit-rehearsal", leader_identity=IDENTITIES["leader"],
                  follower_identity=IDENTITIES["follower"], mapping_identity=IDENTITIES["mapping"])
    coord.set_state("enter_preview")
    coord.align()
    coord.set_state("arm")
    coord.set_state("engage")
    coord.start_follow_loop()
    coord.recording("start_session", {"task": "upper exit rehearsal"})
    session_id = coord.recording_session
    coord.recording("start_episode", {"source": "leader_arm", "task": "upper exit rehearsal"})
    time.sleep(args.follow_seconds)

    before = coord.status()
    car_before = car_view(url)
    steps["before_exit"] = {"state": before["state"], "seq": before["seq"],
                            "upper_lease": before["upper_lease_active"],
                            "car_token": before["car_token_active"], "car": car_before}

    exit_out = coord.shutdown(reason="rehearsal-server-exit")
    time.sleep(args.observe_seconds)
    after = coord.status()
    car_after = car_view(url)
    steps["shutdown_return"] = exit_out
    steps["after_exit"] = {"state": after["state"], "seq": after["seq"],
                           "upper_lease": after["upper_lease_active"],
                           "car_token": after["car_token_active"],
                           "follow_thread_alive": bool(coord.follow_thread and coord.follow_thread.is_alive()),
                           "leader_connected": (after.get("leader") or {}).get("connected"),
                           "car": car_after}

    # Anything attempted after the exit must not reach the car.
    late: dict = {}
    try:
        late = {"result": coord.send_once()}
    except Exception as exc:  # noqa: BLE001 - the refusal text is the finding
        late = {"raised": f"{type(exc).__name__}: {exc}"}
    steps["late_send_after_exit"] = late
    car_after_late = car_view(url)

    with coord.lock:
        token = coord.token
    steps["car_lease_after_release"] = {"token_still_held": token is not None}
    session_dir = Path(marker["car_root"]) / "sessions" / str(session_id)
    manifest_path = session_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    session_json = json.loads((session_dir / "session.json").read_text(encoding="utf-8")) \
        if (session_dir / "session.json").is_file() else {}

    expectations = {
        "was_following_before_the_exit": before["state"] == "following" and before["seq"] > 0,
        "the_follow_loop_is_not_alive_after_exit": not steps["after_exit"]["follow_thread_alive"],
        "the_upper_lease_was_handed_back": after["upper_lease_active"] is False,
        "the_car_control_lease_was_handed_back": after["car_token_active"] is False,
        "the_car_no_longer_reports_an_active_controller": car_after.get("active") is False,
        "the_leader_serial_is_released": (after.get("leader") or {}).get("connected") is not True,
        "no_target_was_applied_after_the_exit": json.dumps(car_after_late.get("last"), sort_keys=True,
                                                           default=str)
                                                 == json.dumps(car_after.get("last"), sort_keys=True,
                                                               default=str),
        "a_send_attempted_after_exit_does_not_reach_the_car": (late.get("raised") is not None
                                                              or late.get("result") is None),
        "the_car_session_was_not_left_recording": session_json.get("status") in ("raw_ready", "invalid",
                                                                                 "stopped", None)
                                                   and (bool(manifest) or bool(session_json.get("error"))),
        "the_exit_returned_a_report": isinstance(exit_out, dict) and bool(exit_out),
    }
    expectations["failed"] = [k for k, v in expectations.items() if v is False]

    out = {"steps": steps, "session_dir_exists": session_dir.is_dir(),
           "session_json": {"status": session_json.get("status"), "error": session_json.get("error"),
                            "recording": session_json.get("recording")},
           "manifest": {"status": manifest.get("status"),
                        "episodes": len(manifest.get("episodes") or []),
                        "has_ready": (session_dir / "READY").is_file()},
           "upper_lease_state_now": lease.status() if hasattr(lease, "status") else None,
           "expectations": expectations}
    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 1 if expectations["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
