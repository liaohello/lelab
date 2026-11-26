#!/usr/bin/env python3
"""Exercise the production capture API and ROS Joy/Twist in the domain-95 fixture."""
import argparse
import json
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROS_PROBE = r"""
import json
import sys
import time

import rclpy
from geometry_msgs.msg import Twist
from ros_robot_controller_msgs.msg import MotorsState
from sensor_msgs.msg import Joy

rclpy.init()
node = rclpy.create_node("offline_gamepad_probe")
joy = node.create_publisher(Joy, "/lelab_offline/base/joy", 10)
events = []
motor_events = []
node.create_subscription(Twist, "/lelab_offline/base/cmd_vel",
                         lambda m: events.append((m.linear.x, m.linear.y, m.angular.z)), 10)
node.create_subscription(MotorsState, "/lelab_offline/ros_robot_controller/set_motor",
                         lambda m: motor_events.append([(w.id, w.rps) for w in m.data]), 10)

def sample(seconds, axes=None):
    start = len(events)
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if axes is not None:
            msg = Joy()
            msg.header.stamp = node.get_clock().now().to_msg()
            msg.axes = axes
            msg.buttons = []
            joy.publish(msg)
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=max(0, deadline - time.monotonic()))
    return events[start:]

def moving(rows):
    return any(any(value != 0.0 for value in row) for row in rows)

zero, jog = [0.0, 0.0, 0.0], [0.8, -0.3, 0.4]
try:
    before_neutral = sample(0.45, jog)
    sample(0.5, zero)
    if sys.argv[1] == "initial":
        drove = sample(0.5, jog)
        lost = sample(0.6)
        center_without_authorization = sample(0.45, zero)
        jog_without_authorization = sample(0.5, jog)
        result = {
            "deflection_before_neutral_blocked": not moving(before_neutral),
            "three_axes_dispatched_without_buttons": any(vx > 0 and vy < 0 and wz < 0 for vx, vy, wz in drove),
            "input_timeout_zero": bool(lost) and not any(lost[-1]),
            "requires_web_reauthorization": not moving(center_without_authorization + jog_without_authorization),
        }
    else:
        rearmed = sample(0.5, jog)
        centered = sample(0.35, zero)
        result = {
            "held_stick_after_reauthorization_blocked": not moving(before_neutral),
            "centered_stick_rearms": moving(rearmed),
            "stick_return_to_center_zero": bool(centered) and not any(centered[-1]),
        }
    result["four_motor_targets_observed"] = bool(motor_events) and all(
        [w[0] for w in row] == [1, 2, 3, 4] for row in motor_events
    ) and any(any(w[1] for w in row) for row in motor_events)
    result["last_motor_target_zero"] = bool(motor_events) and not any(w[1] for w in motor_events[-1])
    print(json.dumps(result))
finally:
    node.destroy_node()
    rclpy.shutdown()
"""

class CaptureRejected(RuntimeError):
    def __init__(self, action, error):
        super().__init__(f"{action}: {error}")
        self.reason = error.get("reason")


def run(args):
    def request(path, body=None, *, port=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port or args.web_port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-LeLab-Client": "offline-gamepad-probe"})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{path}: HTTP {exc.code}: {exc.read().decode()}") from exc

    def status():
        return request("/ros-mobile/hybrid/capture/status")

    def action(verb, **payload):
        out = request("/ros-mobile/hybrid/capture/" + verb, {"request_id": uuid.uuid4().hex, **payload})
        if out.get("state") != "completed":
            raise CaptureRejected(verb, out.get("error") or {})
        return out["result"]

    def wait_for(read, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reading = read()
            if predicate(reading):
                return reading
            time.sleep(0.1)
        raise RuntimeError(f"timed out waiting for {predicate.__code__.co_filename}:{predicate.__code__.co_firstlineno}")

    car = request("/status", port=18142)
    before = status()
    base = ((car.get("licence", {}).get("bridge") or {}).get("frame_process") or {}).get("base_gamepad") or {}
    if (car.get("simulated") is not True or car.get("data_source") != "simulated"
            or (car.get("control") or {}).get("active") is not False
            or base.get("data_identity") != "simulated_offline"
            or before["control"].get("simulated") is not True
            or before["control"].get("session_id") is not None
            or before["operator_stage"] not in ("no_control", "done", "pipeline_failed")):
        raise RuntimeError("requires an idle isolated simulated facade and gamepad agent")
    limits_path = "/ros-mobile/hybrid/feedback-limits"
    limits = request(limits_path)
    request(limits_path, {"ranges": limits["ranges"], "expected_version": limits["version"]})
    action("acquire", owner="offline-gamepad-probe")
    control = status()["control"]
    identity = {key: control[key] for key in ("session_id", "session_epoch")}

    def control_action(name):
        return request("/ros-mobile/hybrid/control", {"action": name, **identity})

    def fresh():
        wait_for(status, lambda s: (s["control"].get("feedback") or {}).get("trusted") is True)

    def resume():
        # The isolated simulated stream can briefly lose trust at a phase
        # boundary. Exercise the explicit operator action again on fresh data.
        for attempt in range(4):
            fresh()
            try:
                action("resume_follow")
                return
            except CaptureRejected as exc:
                if exc.reason not in ("trusted_feedback_unavailable", "feedback_untrusted") or attempt == 3:
                    raise
                time.sleep(0.25)

    def mark(phase):
        fresh()
        try:
            action(f"mark_{phase}")
        except CaptureRejected as exc:
            if exc.reason != f"{phase}_paused":
                raise
            assert status()["attempt"]["phase"] == phase
            resume()

    try:
        if status()["operator_stage"] == "interrupted":
            action("recover")
            wait_for(status, lambda s: s["operator_stage"] in ("done", "pipeline_failed"), 25)
        for step in ("preview", "align", "arm"):
            control_action(step)
        for attempt in range(4):
            fresh()
            try:
                control_action("engage")
                break
            except RuntimeError as exc:
                if "trusted_feedback_unavailable" not in str(exc) or attempt == 3:
                    raise
                time.sleep(0.25)
        time.sleep(0.6)
        control_action("pause_all")
        wait_for(status, lambda s: (s["control"].get("frame_process") or {}).get(
            "base_gamepad", {}).get("available") is True)
        action("open_dataset", name=f"gamepad-raw-{uuid.uuid4().hex[:8]}",
               task="Drive, observe, and grasp in the offline fixture", fps=14, target_episodes=1)
        fresh()
        try:
            attempt = action("start_episode")["attempt_id"]
        except CaptureRejected as exc:
            if exc.reason != "episode_paused_before_follow":
                raise
            attempt = status()["attempt"]["attempt_id"]
            resume()
        driving = wait_for(status, lambda s: s["recording_active"] and (
            s["control"].get("frame_process") or {}).get("base_gamepad", {}).get("permitted") is True)
        assert driving["attempt"]["phase"] == "drive"
        shell = ("source /opt/ros/humble/setup.bash; "
                 "export ROS_DOMAIN_ID=95 ROS_LOCALHOST_ONLY=1 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp; "
                 "source /work/out/local_humble/docker/ws/install/setup.bash; "
                 "exec python3 - ")

        def ros_probe(mode):
            proc = subprocess.run(
                ["docker", "exec", "-i", args.container, "bash", "-lc", shell + mode],
                input=ROS_PROBE, text=True, check=True, capture_output=True, timeout=20)
            return json.loads(proc.stdout.strip())

        motion = ros_probe("initial")
        assert all(motion.values()), motion
        wait_for(status, lambda s: (s["control"].get("frame_process") or {}).get(
            "base_gamepad", {}).get("reauthorization_required") is True)
        resume()
        wait_for(status, lambda s: (s["control"].get("frame_process") or {}).get(
            "base_gamepad", {}).get("permitted") is True)
        motion.update(ros_probe("reauthorized"))
        assert all(motion.values()), motion

        mark("observe")
        wait_for(status, lambda s: s["attempt"]["phase"] == "observe" and (
            s["control"].get("frame_process") or {}).get("base_gamepad", {}).get("permitted") is False)
        time.sleep(0.5)
        mark("grasp")
        time.sleep(0.5)
        action("end_episode", result="unknown")
        action("review", attempt_id=attempt, decision="save")
        action("close_dataset")
        finished = wait_for(status, lambda s: s["operator_stage"] in ("done", "pipeline_failed"), 25)
        task = finished["task"]
        assert task["pipeline"]["stage"] == "raw_synced", task["pipeline"]
        assert task["base_capture_raw_only"] is True
        assert finished["control"]["outputs_enabled"] is False
        sid = task["car_recording_id"]
        synced = Path(task["pipeline"]["sync_path"])
        if args.root.resolve() not in synced.resolve().parents:
            raise RuntimeError(f"unexpected sync path {synced}")
        databases = list(synced.rglob("*.db3"))
        assert databases, sid
        rows = []
        for database in databases:
            uri = database.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as db:
                assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
                for (data,) in db.execute(
                    "SELECT messages.data FROM messages JOIN topics ON topics.id=messages.topic_id "
                    "WHERE topics.name='/lelab_offline/recording/base' ORDER BY messages.timestamp"
                ):
                    # std_msgs/String CDR contains the JSON string after the 8-byte header.
                    rows.append(json.loads(data[8:].rstrip(b"\0")))
        episode = [row for row in rows if row["episode_id"] == attempt]
        assert {row["phase"] for row in episode} >= {"drive", "observe", "grasp"}
        assert any(any(row["target_si"] or ()) for row in episode)
        assert all(row["phase"] == "drive" for row in episode if any(row["target_si"] or ()))
        assert all(row["state_si"] is None and row["training_eligible"] is False
                   and row["recording_session_id"] == sid for row in episode)
        wheel_targets = [row["wheel_target_rps"] for row in episode if any(row["target_si"] or ())]
        assert wheel_targets and all(len(target) == 4 and any(target) for target in wheel_targets)
        estimates = [row["command_odometry"] for row in episode if row["phase"]]
        assert all(item["source_kind"] == "command_integrated_estimate"
                   and item["episode_id"] == attempt for item in estimates)
        assert any(pose[0] > 0 and pose[1] < 0 and pose[2] < 0
                   for pose in (item["pose_xy_yaw"] for item in estimates))
        board = json.loads((args.root / "run/sim_board_owner.json").read_text())["base_motor_command"]
        assert board["received"] > 0 and not any(board["target_rps"]), board
        report = {"data_identity": "simulated_offline", "training_eligible": False,
                  "session_id": sid, "episode_id": attempt, "sqlite_diagnostic_rows": len(episode),
                  "four_wheel_target_rows": len(wheel_targets), "simulated_board_owner": board,
                  "final_command_odometry": estimates[-1],
                  "phases": sorted({row["phase"] for row in episode if row["phase"]}), **motion,
                  "pipeline": task["pipeline"]["stage"]}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        if status()["control"].get("session_id") == identity["session_id"]:
            try:
                control_action("pause_all")
            finally:
                request("/ros-mobile/hybrid/session", {"action": "release", **identity})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-port", type=int, default=18289)
    parser.add_argument("--container", default="lelab-humble-dev")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    run(parser.parse_args())
