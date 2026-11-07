#!/usr/bin/env python3
"""五 and 六 on the deployed path: the emergency stop while a teleop episode is recording.

The requirement is specific about three things that unit tests only assert against a fake gateway:
pressing 急停 must leave `following` immediately, must latch on the car so nothing else can drive the
servos, and must not be held hostage by the recorder ("急停不等待保存"). Recovery then has to be
explicit - clearing the latch may not silently resume motion or replay the last target. This rehearses
all of it against the deployed `lelab_mobile_agent`, with a real recording session open.

Run `local_car_rehearsal.py --root <root>` first, then:

    app_ros_mobile/.venv/bin/python tools/ros2_collection/local_car_estop_rehearsal.py --root <root>

Nothing is attached and nothing moves: the leader and the follower feedback are synthetic, so the
latch, the refusal and the recorded events are all that can be observed here - not the servo state.
Exits non-zero with a named reason if any expectation fails.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for path in (REPO / "tools" / "ros2_collection", REPO / "app_ros_mobile"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from local_teleop_session_drive import SyntheticLeader, seed  # noqa: E402

from lelab.hardware_lease import HardwareLeaseManager  # noqa: E402
from lelab.mobile_teleop import CalibrationStore  # noqa: E402
from lelab.mobile_teleop_coordinator import (BridgeError,  # noqa: E402
                                             CoordinatorConfig, MobileTeleopCoordinator)

PORT = "/dev/serial/by-id/synthetic-if00"
IDENTITIES = {"leader": "leader_local", "follower": "car_local", "mapping": "map_local"}


def car_view(url: str) -> dict:
    import urllib.request
    with urllib.request.urlopen(url + "/status", timeout=2.0) as response:
        body = json.loads(response.read())
    control = body.get("control") or {}
    return {"active": control.get("active"), "estop_latched": control.get("estop_latched"),
            "mode": control.get("control_mode"), "stream": (control.get("teleop") or {}).get("stream"),
            "last": (control.get("teleop") or {}).get("last")}


def estop_events_in_the_bag(bag_dir: Path) -> dict:
    """Count the raw bag's /lelab/events messages that mention the stop.

    The agent's `estop` / `estop_cleared` events are published on /lelab/events, so they land in the
    sqlite3 bag rather than in the session's own events.jsonl (which only carries session/episode
    bookkeeping). 需求七 asks for the faults to be in the raw recording, so that is the witness.
    """
    import sqlite3
    out = {"db": None, "event_messages": 0, "estop_messages": 0, "unreadable": None}
    dbs = sorted(bag_dir.glob("bag_*.db3"))
    if not dbs:
        out["unreadable"] = f"no bag database under {bag_dir}"
        return out
    out["db"] = dbs[0].name
    try:
        con = sqlite3.connect(dbs[0])
        topic = con.execute("select id from topics where name='/lelab/events'").fetchone()
        if not topic:
            out["unreadable"] = "/lelab/events is not in the bag"
        else:
            rows = con.execute("select data from messages where topic_id=?", (topic[0],)).fetchall()
            out["event_messages"] = len(rows)
            out["estop_messages"] = sum(b"estop" in bytes(r[0]) for r in rows)
    except Exception as exc:  # noqa: BLE001 - the report has to say why it could not look
        out["unreadable"] = f"{type(exc).__name__}: {exc}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/lelab_car_rehearsal")
    ap.add_argument("--follow-seconds", type=float, default=2.0)
    ap.add_argument("--observe-seconds", type=float, default=1.5)
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

    steps: dict = {"agent_url": url}
    coord.open_leader(PORT, leader_identity=IDENTITIES["leader"])
    coord.acquire(owner="estop-rehearsal", leader_identity=IDENTITIES["leader"],
                  follower_identity=IDENTITIES["follower"], mapping_identity=IDENTITIES["mapping"])
    coord.set_state("enter_preview")
    coord.align()
    coord.set_state("arm")
    coord.set_state("engage")
    coord.recording("start_session", {"task": "estop rehearsal"})
    session_id = coord.recording_session
    coord.recording("start_episode", {"source": "leader_arm", "task": "estop rehearsal"})
    time.sleep(args.follow_seconds)

    before = coord.status()
    car_before = car_view(url)
    steps["before_estop"] = {"state": before["state"], "seq": before["seq"],
                             "car": car_before}

    estop_out = coord.emergency_stop("rehearsal-operator-estop")
    # The stop takes effect on the loop's next cycle, and one arm+gripper pair may already be in
    # flight, so "nothing after the stop" is measured from the downgrade - not from the pre-stop
    # snapshot, which legitimately predates that last cycle.
    downgraded = None
    wait_until = time.monotonic() + 3.0
    while time.monotonic() < wait_until:
        probe = coord.status()
        if probe["state"] != "following":
            downgraded = probe
            break
        time.sleep(0.05)
    car_at_downgrade = car_view(url)
    time.sleep(args.observe_seconds)
    after = downgraded or coord.status()
    car_after = car_view(url)
    steps["emergency_stop_call"] = estop_out
    steps["after_estop"] = {"detected_downgrade": downgraded is not None, "state": after["state"],
                            "seq": after["seq"], "cycles_at_the_stop": after["seq"] - before["seq"],
                            "last_error": after.get("last_error"), "car": car_after,
                            "car_last_unchanged": json.dumps(car_after.get("last"), sort_keys=True,
                                                             default=str)
                                                  == json.dumps(car_at_downgrade.get("last"),
                                                                sort_keys=True, default=str)}

    # A command that arrives while the latch is up must be refused at the gateway, not applied.
    refused = {}
    with coord.lock:
        token = coord.token
    try:
        coord._transport("/control/arm", "POST", {"session_id": token, "seq": after["seq"] + 1,
                                                  "source": "estop_rehearsal", "mode": "leader_arm",
                                                  "ttl_s": 0.5, "arm": {"positions": [0.0] * 5}}, token)
        refused = {"raised": None}
    except BridgeError as exc:
        refused = {"raised": f"{exc.status}:{exc.detail}"}
    steps["command_while_latched"] = refused
    steps["car_at_downgrade"] = car_at_downgrade

    cleared = {}
    try:
        cleared = coord._transport("/control/clear_estop", "POST", {}, token)
    except BridgeError as exc:
        cleared = {"error": f"{exc.status}:{exc.detail}"}
    time.sleep(args.observe_seconds)
    post_clear = coord.status()
    car_post = car_view(url)
    steps["after_clear"] = {"clear_result": cleared, "state": post_clear["state"],
                            "seq": post_clear["seq"], "car": car_post}

    end_out: dict = {}
    try:
        end_out = coord.recording("end_episode", {})
    except Exception as exc:  # noqa: BLE001 - how the recorder reacts is part of the finding
        end_out = {"error": f"{type(exc).__name__}: {exc}"}
    coord.recording("stop_session", {"reason": "estop rehearsal"})
    coord.release(reason="estop rehearsal")

    session_dir = Path(marker["car_root"]) / "sessions" / str(session_id)
    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (session_dir / "events.jsonl").read_text()
              .splitlines() if line.strip()]
    event_kinds = sorted({str(e.get("kind")) for e in events})
    bag_events = estop_events_in_the_bag(session_dir / "raw" / "bag")

    latched = car_after.get("estop_latched")
    expectations = {
        "was_following_before_the_stop": before["state"] == "following" and before["seq"] > 0,
        "the_stop_reached_the_car": estop_out.get("sent_estop") is True,
        "the_upper_side_left_following": after["state"] != "following",
        "the_car_latched_the_stop": latched is True,
        "a_latched_car_is_not_an_active_controller": car_after.get("active") is False,
        "no_sends_after_the_stop": after["seq"] - before["seq"] <= 2,
        "a_new_command_was_refused_while_latched": refused.get("raised", "").startswith("409"),
        "nothing_was_applied_while_latched": steps["after_estop"]["car_last_unchanged"]
                                             and post_clear["seq"] == after["seq"],
        "clearing_the_latch_did_not_resume_motion": post_clear["state"] != "following",
        "the_stop_is_in_the_raw_recording": bag_events["estop_messages"] > 0,
        "the_recording_survived_the_stop": session_dir.is_dir() and bool(manifest.get("episodes")),
        "the_session_finalised_one_way_or_the_other": manifest.get("status") in ("raw_ready", "invalid"),
    }
    expectations["failed"] = [k for k, v in expectations.items() if v is False]

    out = {"steps": steps, "manifest": {"session_id": manifest["session_id"], "status": manifest["status"],
                                        "episodes": [{"id": e["episode_id"],
                                                      "invalid": e.get("invalid"),
                                                      "reason": e.get("invalid_reason"),
                                                      "success": e.get("evaluation", {}).get("success")}
                                                     for e in manifest["episodes"]],
                                        "has_ready": (session_dir / "READY").is_file()},
           "session_event_kinds": event_kinds, "bag_events": bag_events,
           "expectations": expectations}
    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 1 if expectations["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
