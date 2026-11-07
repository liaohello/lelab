#!/usr/bin/env python3
"""Drive one teleop session against the locally running car agent (see local_car_rehearsal.py).

Runs in the app virtualenv because the coordinator speaks plain HTTP to the car gateway; the agent
itself runs under the ROS environment. The leader arm is synthetic (there is no USB device on this
host), so nothing here is a hardware acceptance - but every other layer is the production code path:
the real HardwareLeaseManager, the real coordinator and state machine, the real car-side
contract.CommandGate, the real recorder spawning `ros2 bag record`, and a sqlite3 bag that this
script then reads back with sqlite3 to prove what was actually captured.

    python3 tools/ros2_collection/local_teleop_session_drive.py --root /tmp/lelab_car_rehearsal
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "app_ros_mobile"))

from lelab.hardware_lease import HardwareLeaseManager  # noqa: E402
from lelab.mobile_teleop import CalibrationStore  # noqa: E402
from lelab.mobile_teleop_coordinator import (  # noqa: E402
    CoordinatorConfig, MobileTeleopCoordinator)

ARM = ["joint1", "joint2", "joint3", "joint4", "joint5"]
FOLLOWER = [*ARM, "r_joint"]
LEADER = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]
EXAMPLES = REPO / "configs" / "mobile_teleop"


class SyntheticLeader:
    """The only fake in this chain: a leader arm that reports a slow sine sweep plus a gripper."""

    def __init__(self, port: str):
        import math
        self._math = math
        from types import SimpleNamespace
        self.config = SimpleNamespace(port=port, read_hz=30.0)
        self.t0 = time.monotonic()
        self.closed = False

    def snapshot(self, now: float | None = None) -> dict:
        t = time.monotonic() - self.t0
        deg = {name: 12.0 * self._math.sin(t * 0.6 + i) for i, name in enumerate(LEADER)}
        return {"connected": not self.closed, "healthy": not self.closed,
                "reason": "ok" if not self.closed else "leader_closed",
                "positions_deg": deg,
                "positions_rad": {k: v * self._math.pi / 180.0 for k, v in deg.items()},
                "gripper_percent": 50.0 + 10.0 * self._math.sin(t),
                "missing_joints": [], "age_s": 0.02, "port_claim": "synthetic:no_serial",
                "port": self.config.port, "reads": int(t * 30)}

    def start(self) -> None:
        pass

    def close(self) -> dict:
        self.closed = True
        return {"closed": True, "reason": "rehearsal_end"}


UNMEASURED_PREFIXES = ("example_not_measured", "derived_not_measured")


def as_rehearsal_provenance(payload):
    """Turn a shipped template into the rehearsal's own declared provenance.

    The wizard refuses a layer that still holds the shipped example values, and it is right to:
    those are placeholders. This rehearsal feeds the agent real (synthetic) joint states that the
    agent actually consumes, so the layers say `rehearsal_synthetic` rather than inheriting the
    "not measured" marker - and production still refuses anything that does inherit it.
    """
    if isinstance(payload, dict):
        return {key: as_rehearsal_provenance(value) for key, value in payload.items()
                if key != "_example_not_measured"}
    if isinstance(payload, list):
        return [as_rehearsal_provenance(value) for value in payload]
    if isinstance(payload, str) and payload.strip().startswith(UNMEASURED_PREFIXES):
        return "rehearsal_synthetic"
    return payload


def seed(store: CalibrationStore, identities: dict[str, str]) -> None:
    leader = as_rehearsal_provenance(json.loads((EXAMPLES / "leader.example.json").read_text(encoding="utf-8")))
    follower = as_rehearsal_provenance(json.loads((EXAMPLES / "follower.example.json").read_text(encoding="utf-8")))
    mapping = as_rehearsal_provenance(json.loads((EXAMPLES / "mapping.example.json").read_text(encoding="utf-8")))
    # Layer A must carry the same shape the existing wizard produced, and its ids have to agree
    # with the bus ids the reader polls - that is the guard added in this branch.
    leader["lerobot_calibration"] = {name: {"id": i + 1, "drive_mode": 0, "homing_offset": 0,
                                            "range_min": 0, "range_max": 4095}
                                     for i, name in enumerate([*LEADER, "gripper"])}
    leader["joint_order"] = LEADER
    follower["joint_order"] = FOLLOWER
    follower.setdefault("limits_rad", {j: [-2.09, 2.09] for j in FOLLOWER})
    follower.setdefault("soft_limits_rad", {j: [-1.9, 1.9] for j in FOLLOWER})
    mapping["joint_order"] = FOLLOWER
    mapping["bound_identities"] = {"leader": identities["leader"], "follower": identities["follower"]}
    store.save("leader", identities["leader"], leader)
    store.save("follower", identities["follower"], follower)
    store.save("mapping", identities["mapping"], mapping)


def bag_report(bag_dir: Path) -> dict:
    """Read the recorded bag back with sqlite3 and count what actually landed per topic."""
    out: dict[str, Any] = {"bag_dir": str(bag_dir), "files": sorted(p.name for p in bag_dir.iterdir())}
    dbs = [p for p in bag_dir.rglob("*.db3")]
    if not dbs:
        out["error"] = "no sqlite3 bag file found"
        return out
    conn = sqlite3.connect(f"file:{dbs[0]}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT t.name, COUNT(m.id) FROM topics t JOIN messages m "
                            "ON m.topic_id = t.id GROUP BY t.name").fetchall()
        out["messages_by_topic"] = {name: count for name, count in rows}
    finally:
        conn.close()
    return out



def bag_string_topics(bag_dir: Path, topics: list[str]) -> dict:
    """Decode the JSON-carrying topics and report which of the 七 field classes really landed."""
    dbs = [p for p in bag_dir.rglob("*.db3")]
    if not dbs:
        return {"error": "no sqlite3 bag file found"}
    conn = sqlite3.connect(f"file:{dbs[0]}?mode=ro", uri=True)
    out: dict[str, Any] = {}
    try:
        rows = conn.execute("SELECT t.name, m.data FROM topics t JOIN messages m "
                            "ON m.topic_id = t.id").fetchall()
    finally:
        conn.close()
    decoder = json.JSONDecoder()
    by_topic: dict[str, list[dict]] = {}
    for name, data in rows:
        if name not in topics:
            continue
        raw = bytes(data)
        length = int.from_bytes(raw[4:8], "little")
        try:
            payload, _ = decoder.raw_decode(raw[8:8 + length].decode())
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            by_topic.setdefault(name, []).append(payload)

    events = by_topic.get("/lelab/events", [])
    arm = [e for e in events if e.get("component") == "arm"]
    gripper = [e for e in events if e.get("component") == "gripper"]
    # 七 lists the classes the car must record; each is checked against a field that has to be on
    # every command event, so a missing class shows up as a count of 0 rather than a green report.
    required_on_command = ["leader_rad", "pre_clip", "target", "action", "calibration_version",
                           "clutch", "ttl_s", "seq", "clipped"]
    out["events"] = {
        "kinds": {kind: sum(1 for e in events if e.get("kind") == kind)
                  for kind in sorted({e.get("kind") for e in events if e.get("kind")})},
        "arm_commands": len(arm), "gripper_commands": len(gripper),
        "commands_with_every_required_field": {
            field: sum(1 for e in arm if field in e) for field in required_on_command},
        "sample_arm_command": arm[0] if arm else None,
        "sample_gripper_command": gripper[0] if gripper else None,
    }
    states = by_topic.get("/lelab/recording/teleop", [])
    out["teleop_state"] = {
        "count": len(states), "keys": sorted(states[0]) if states else [],
        "sample": states[len(states) // 2] if states else None}
    out["trajectory_requests"] = {
        topic: conn_count for topic, conn_count in
        ((t, sum(1 for name, _ in rows if name == t))
         for t in ("/lelab/arm/trajectory_request", "/lelab/gripper/trajectory_request"))}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/lelab_car_rehearsal")
    ap.add_argument("--follow-seconds", type=float, default=3.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    root = Path(args.root)
    marker = json.loads((root / "READY_FOR_DRIVER.json").read_text(encoding="utf-8"))
    agent_url = f"http://127.0.0.1:{marker['port']}"
    profile = json.loads((REPO / "configs" / "ros2_collection" / "hardware-teleop-v1.json")
                         .read_text(encoding="utf-8"))

    scratch = root / "upper"
    (scratch / "cal").mkdir(parents=True, exist_ok=True)
    identities = {"leader": "leader_local", "follower": "car_local", "mapping": "map_local"}
    store = CalibrationStore(scratch / "cal")
    seed(store, identities)

    lease = HardwareLeaseManager()

    coord = MobileTeleopCoordinator(
        CoordinatorConfig(agent_url=agent_url, command_hz=10.0, lease_ttl_s=60.0,
                          control_ttl_s=30.0, request_timeout_s=5.0,
                          leader={"read_hz": 30.0}),
        profile, store=store, leader_reader=SyntheticLeader("/dev/serial/by-id/synthetic-if00"),
        lease_manager=lease)

    steps: dict[str, Any] = {"agent_url": agent_url, "marker": marker}
    coord.open_leader("/dev/serial/by-id/synthetic-if00", leader_identity=identities["leader"])
    steps["after_open"] = coord.status()["state"]

    coord.acquire(owner="local-rehearsal", leader_identity=identities["leader"],
                    follower_identity=identities["follower"],
                    mapping_identity=identities["mapping"])
    status = coord.status()
    steps["after_acquire"] = {"state": status["state"], "upper_lease": status["upper_lease_active"],
                              "car_token": status["car_token_active"],
                              "car_exclusivity": status["car_exclusivity"],
                              "car_hold_policy": status["car_hold_policy"]}

    coord.set_state("enter_preview")
    preview_before = coord.status()["preview"]
    steps["preview_without_engage"] = {"outputs_actuators": preview_before.get("outputs_actuators"),
                                       "targets": sorted((preview_before.get("targets") or {}).keys())}
    car_before = coord._transport("/status")
    last_before = (car_before.get("control") or {}).get("teleop", {}).get("last")

    coord.align()
    coord.set_state("arm")
    coord.set_state("engage")
    steps["after_engage"] = coord.status()["state"]

    coord.recording("start_session", {"task": "local rehearsal teleop"})
    session_id = coord.recording_session
    coord.recording("start_episode", {"source": "leader_arm", "task": "local rehearsal teleop"})
    ep1 = coord.recording_episode
    time.sleep(args.follow_seconds)
    after_follow = coord.status()
    steps["during_follow"] = {"seq": after_follow["seq"], "last_sent": after_follow["last_sent"],
                             "preview_arm_stream": (after_follow["preview"] or {}).get("arm_stream")}
    coord.recording("end_episode", {})
    steps["after_end_episode_state"] = coord.status()["state"]

    coord.set_state("enter_preview")
    coord.align()
    coord.set_state("arm")
    coord.set_state("engage")
    coord.recording("start_episode", {"source": "leader_arm", "task": "local rehearsal teleop"})
    ep2 = coord.recording_episode
    time.sleep(args.follow_seconds)
    coord.recording("end_episode", {})
    coord.recording("stop_session", {"reason": "normal"})

    car_after = coord._transport("/status")
    teleop_after = (car_after.get("control") or {}).get("teleop", {})
    steps["car_side"] = {"teleop_last_before_engage": last_before,
                         "teleop_last_after": teleop_after.get("last"),
                         "stream": teleop_after.get("stream"),
                         "acks": (teleop_after.get("acks") or [])[:3],
                         "feedback": (car_after.get("control") or {}).get("follower_feedback", {})
                         .get("available"),
                         "command_rejects": teleop_after.get("rejected")}

    coord.release(reason="rehearsal_end")
    steps["after_release"] = {"state": coord.status()["state"],
                             "car_active": (coord._transport("/status").get("control") or {}).get("active")}

    session_dir = Path(marker["car_root"]) / "sessions" / str(session_id)
    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    steps["manifest"] = {"session_id": manifest["session_id"], "status": manifest["status"],
                         "episodes": [{"episode_id": e["episode_id"], "source": e.get("source"),
                                       "success": e.get("evaluation", {}).get("success")}
                                      for e in manifest["episodes"]],
                         "runtime_mode": manifest["metadata"].get("runtime_mode"),
                         "export_schema": manifest["metadata"].get("export_schema"),
                         "has_ready": (session_dir / "READY").is_file()}
    steps["bag"] = bag_report(session_dir / "raw" / "bag")
    steps["recorded_fields"] = bag_string_topics(
        session_dir / "raw" / "bag", ["/lelab/events", "/lelab/recording/teleop"])
    counts = steps["bag"].get("messages_by_topic") or {}
    steps["expectations"] = {
        "two_episodes": [ep1, ep2],
        "end_episode_parks_following": steps["after_end_episode_state"] == "paused",
        "preview_sent_nothing": last_before is None,
        "car_saw_commands": teleop_after.get("last") is not None,
        # The accepted command must leave the gateway as an actual trajectory; with the chassis
        # locked the old control tick returned before draining the queue and nothing was published.
        "arm_trajectory_published": counts.get("/lelab/arm/trajectory_request", 0) > 0,
        "gripper_trajectory_published": counts.get("/lelab/gripper/trajectory_request", 0) > 0,
        "command_events_recorded": (steps["recorded_fields"].get("events") or {}).get("arm_commands", 0) > 0,
        "session_finalised_cleanly": {"status": manifest["status"], "ready": steps["manifest"]["has_ready"]},
    }
    failed = [name for name, ok in (
        ("end_episode_parks_following", steps["expectations"]["end_episode_parks_following"]),
        ("preview_sent_nothing", steps["expectations"]["preview_sent_nothing"]),
        ("car_saw_commands", steps["expectations"]["car_saw_commands"]),
        ("arm_trajectory_published", steps["expectations"]["arm_trajectory_published"]),
        ("gripper_trajectory_published", steps["expectations"]["gripper_trajectory_published"]),
        ("command_events_recorded", steps["expectations"]["command_events_recorded"]),
        ("session_ready", steps["manifest"]["has_ready"]),
    ) if not ok]
    steps["expectations"]["failed"] = failed

    text = json.dumps(steps, indent=2, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
