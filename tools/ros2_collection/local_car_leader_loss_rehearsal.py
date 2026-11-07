#!/usr/bin/env python3
"""六 on the deployed path: the leader arm goes dark while the car side stays healthy.

The unplug-a-leader leg of the requirement's loss list used to be covered only by upper-computer
tests against a fake gateway. This rehearses it against the real agent: the session is following and
recording, then the leader reports itself gone. Three things have to hold at once - the coordinator
leaves `following` and names the leader (not a generic `follow_error`), it stops polling and stops
sending, and the car marks the arm stream expired on its own monotonic clock without replaying the
last pose. Re-engaging without re-aligning must stay refused.

Run `local_car_rehearsal.py --root <root>` first, then:

    app_ros_mobile/.venv/bin/python tools/ros2_collection/local_car_leader_loss_rehearsal.py --root <root>

No hardware moves: the leader is synthetic and the follower feedback is synthetic, so nothing is
attached. Exits non-zero with a named reason if any expectation fails.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
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


def car_status(url: str, timeout: float = 1.0) -> dict:
    try:
        with urllib.request.urlopen(url + "/status", timeout=timeout) as response:
            body = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {"unreadable": str(exc)}
    control = body.get("control") or {}
    teleop = control.get("teleop") or {}
    return {"stream": teleop.get("stream"), "last": teleop.get("last"), "active": control.get("active")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/lelab_car_rehearsal/car")
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
    coord = MobileTeleopCoordinator(
        CoordinatorConfig(agent_url=url, command_hz=10.0, lease_ttl_s=60.0, control_ttl_s=30.0,
                          request_timeout_s=5.0, leader={"read_hz": 30.0}),
        profile, store=store, leader_reader=leader, lease_manager=HardwareLeaseManager())

    steps: dict = {"agent_url": url, "marker": marker}
    coord.open_leader(PORT, leader_identity=IDENTITIES["leader"])
    coord.acquire(owner="leader-loss-rehearsal", leader_identity=IDENTITIES["leader"],
                  follower_identity=IDENTITIES["follower"], mapping_identity=IDENTITIES["mapping"])
    coord.set_state("enter_preview")
    coord.align()
    coord.set_state("arm")
    coord.set_state("engage")
    coord.recording("start_session", {"task": "leader loss rehearsal"})
    session_id = coord.recording_session
    coord.recording("start_episode", {"source": "leader_arm", "task": "leader loss rehearsal"})
    episode_id = coord.recording_episode
    time.sleep(args.follow_seconds)

    before = coord.status()
    car_before = car_status(url)
    steps["before_loss"] = {"state": before["state"], "seq": before["seq"],
                            "car_stream_live": (car_before.get("stream") or {}).get("live"),
                            "leader_connected": (before.get("leader") or {}).get("connected")}

    leader.close()
    steps["loss_injected"] = {"leader_snapshot": leader.snapshot().get("reason"),
                              "signal": "SyntheticLeader.close() - no serial involved"}

    # The gate is checked once per control cycle, so one in-flight cycle is allowed to complete
    # before the loop parks. Watch for that moment instead of assuming the next read is already
    # downgraded: 2026-11-03's first run read seq 43 -> 45 across the injection and called it a
    # failure, when the pair is one cycle (arm + gripper consume one sequence each).
    downgraded = None
    wait_until = time.monotonic() + 3.0
    while time.monotonic() < wait_until:
        probe = coord.status()
        if probe["state"] != "following":
            downgraded = probe
            break
        time.sleep(0.05)
    car_after = car_status(url)
    after = downgraded or coord.status()
    steps["downgrade_detected"] = {"found": downgraded is not None, "seq": after["seq"],
                                   "state": after["state"],
                                   "cycles_after_injection": after["seq"] - before["seq"]}
    steps["after_loss"] = {"status_keys": sorted(after.keys()), "state": after["state"], "seq": after["seq"],
                           "last_error": after.get("last_error"),
                           "missing_for_following": after.get("missing_for_following"),
                           "leader_gate": (after.get("gate") or {}),
                           "car_stream": car_after.get("stream"), "car_active": car_after.get("active"),
                           "car_last": car_after.get("last")}

    frozen_seq = after["seq"]
    time.sleep(args.observe_seconds)
    still = coord.status()
    car_later = car_status(url)
    steps["after_observation"] = {"seq": still["seq"], "state": still["state"],
                                  "car_stream": car_later.get("stream"),
                                  "car_last_unchanged_since_downgrade":
                                      json.dumps(car_later.get("last"), sort_keys=True, default=str)
                                      == json.dumps(car_after.get("last"), sort_keys=True, default=str)}

    refused: dict = {}
    try:
        coord.set_state("engage")
        refused = {"raised": None, "state": coord.status()["state"], "seq": coord.status()["seq"]}
    except Exception as exc:  # noqa: BLE001 - the refusal text is the finding
        refused = {"raised": f"{type(exc).__name__}: {exc}", "state": coord.status()["state"],
                   "seq": coord.status()["seq"]}
    steps["reengage_without_realign"] = refused
    car_final = car_status(url)

    coord.recording("end_episode", {})
    coord.recording("stop_session", {"reason": "leader-loss rehearsal"})
    coord.release(reason="leader-loss rehearsal")

    session_dir = Path(marker["car_root"]) / "sessions" / str(session_id)
    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))

    # "expired on its own" is about what the car reports after the TTL has passed, which is the
    # later sample - at the downgrade instant the stream is legitimately still inside its TTL.
    stream = (car_later.get("stream") or {})
    reason = str(after.get("last_error") or "")
    expectations = {
        "was_following_before_the_loss": before["state"] == "following" and before["seq"] > 0,
        "leader_loss_left_following": after["state"] != "following",
        "the_reason_names_the_leader": "leader" in reason.lower(),
        "one_cycle_races_through_then_silence": 0 <= after["seq"] - before["seq"] <= 2,
        "the_loop_stays_downgraded": still["seq"] == frozen_seq and still["state"] != "following",
        "the_car_expired_the_stream_on_its_own": bool(stream) and stream.get("expected") is True
                                                and stream.get("live") is False
                                                and (stream.get("age_s") or 0) > (stream.get("ttl_s") or 0),
        "nothing_new_was_applied_after_the_downgrade": steps["after_observation"][
            "car_last_unchanged_since_downgrade"],
        "car_kept_its_lease_without_the_leader": car_after.get("active") is True,
        "reengage_without_realign_is_refused": refused["state"] != "following"
                                               and refused["seq"] == frozen_seq
                                               and "leader" in str(refused.get("raised", "")).lower(),
        "the_session_still_finalised": manifest.get("status") == "raw_ready"
                                       and len(manifest.get("episodes", [])) == 1,
    }
    expectations["failed"] = [k for k, v in expectations.items() if v is False]

    out = {"steps": steps, "manifest": {"session_id": manifest["session_id"],
                                         "status": manifest["status"],
                                         "episodes": [{"id": e["episode_id"], "id_matches": e["episode_id"] == episode_id,
                                                      "success": e.get("evaluation", {}).get("success")}
                                                     for e in manifest["episodes"]],
                                         "runtime_mode": manifest["metadata"].get("runtime_mode")},
           "car_stream_at_end": (car_final.get("stream") or {}),
           "expectations": expectations}
    text = json.dumps(out, indent=2, default=str, ensure_ascii=False)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 1 if expectations["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
