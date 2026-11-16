#!/usr/bin/env python3
"""Run the T7 live two-episode recording without an automatic homing move.

This runner is intentionally separate from t7_record_runner.py.  The older runner
uses the bounded home task to load torque, which can fail on a mechanically
constrained pose and is not the right operation for a recording session.

The caller must provide a recent per-ID torque readback file whose six axes are
verified enabled.  The runner still re-reads torque through the unique board
owner before every explicit engage.  It never changes torque, never calls home,
and never replays an old target.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE = "http://127.0.0.1:18288/ros-mobile/hybrid"
AGENT = "http://127.0.0.1:18110/status"
PROFILE = "hardware_hybrid_manual_v1"
JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint")
SERVO_IDS = (1, 2, 3, 4, 5, 10)
TASK = "mobile_arm_gripper_teleop"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def http_json(path: str, body: dict[str, Any] | None = None,
              method: str | None = None, timeout: float = 12.0) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        path, data=data, method=method or ("POST" if body is not None else "GET"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read(2_000_000)
            return int(response.status), json.loads(raw or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read(20_000)
        try:
            doc = json.loads(raw or b"{}")
        except ValueError:
            doc = {"detail": raw.decode("utf-8", "replace")[:500]}
        return int(exc.code), doc
    except Exception as exc:  # noqa: BLE001 - caller records the transport failure
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def require(code: int, label: str, doc: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    evidence[label] = {"http": code, "response": doc}
    if code != 200:
        raise RuntimeError(f"{label}: http={code}: {doc.get('error') or doc.get('detail') or doc}")
    return doc


def get_status() -> dict[str, Any]:
    code, doc = http_json(f"{BASE}/status")
    if code != 200:
        raise RuntimeError(f"status: http={code}: {doc}")
    return doc


def get_recording() -> dict[str, Any]:
    code, doc = http_json(f"{BASE}/recording/status")
    if code != 200:
        raise RuntimeError(f"recording_status: http={code}: {doc}")
    return doc


def get_agent() -> dict[str, Any]:
    code, doc = http_json(AGENT)
    if code != 200:
        raise RuntimeError(f"agent_status: http={code}: {doc}")
    return doc


def assert_quiescent() -> None:
    status = get_status()
    agent = get_agent()
    recording = get_recording()
    if status.get("state") != "disconnected" or status.get("session_id") is not None:
        raise RuntimeError(f"refusing: upper state/session is {status.get('state')}/{status.get('session_id')}")
    if status.get("outputs_enabled") is not False:
        raise RuntimeError("refusing: upper outputs are not off")
    if (agent.get("control") or {}).get("active") is not False:
        raise RuntimeError("refusing: car control lease is active")
    if (agent.get("licence") or {}).get("frame_outputs_enabled") is not False:
        raise RuntimeError("refusing: car frame outputs are enabled")
    if (agent.get("licence") or {}).get("frame_estop_latched") is not False:
        raise RuntimeError("refusing: car emergency stop is latched")
    if recording.get("status") not in ("not_started", "stopped"):
        raise RuntimeError(f"refusing: recording status is {recording.get('status')}")
    if recording.get("episode_id") is not None:
        raise RuntimeError("refusing: an episode is already open")


def torque_read() -> dict[str, Any]:
    target = os.environ.get("LELAB_T7_PI_TARGET", "").strip()
    if not target:
        raise RuntimeError("LELAB_T7_PI_TARGET is required for per-ID torque verification")
    remote = r'''
import datetime, json, rclpy
from rclpy.node import Node
from ros_robot_controller_msgs.msg import GetBusServoCmd
from ros_robot_controller_msgs.srv import GetBusServoState
rclpy.init(); node=Node("lelab_t7_torque_gate")
client=node.create_client(GetBusServoState, "/ros_robot_controller/bus_servo/get_state")
out={"observed_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "ids": {}}
try:
    out["service_available"] = bool(client.wait_for_service(timeout_sec=5.0))
    for sid in (1, 2, 3, 4, 5, 10):
        req=GetBusServoState.Request(); cmd=GetBusServoCmd()
        cmd.id=sid; cmd.get_id=1; cmd.get_position=1; cmd.get_torque_state=1
        req.cmd.append(cmd)
        fut=client.call_async(req)
        rclpy.spin_until_future_complete(node, fut, timeout_sec=2.0)
        res=fut.result()
        if res is None or not res.success or len(res.state) != 1:
            out["ids"][str(sid)] = {"valid": False, "reason": "read_failed"}
            continue
        st=res.state[0]
        out["ids"][str(sid)] = {
            "valid": list(st.present_id) == [sid] and len(st.position) == 1 and len(st.enable_torque) == 1,
            "position": list(st.position), "enable_torque": list(st.enable_torque),
        }
    out["all_verified"] = all(
        row.get("valid") is True and row.get("enable_torque") == [1]
        for row in out["ids"].values()
    )
finally:
    node.destroy_node(); rclpy.shutdown()
print(json.dumps(out, sort_keys=True))
'''
    command = "source /opt/ros/humble/setup.bash; " \
              'source "${LELAB_ROS_WS:-$HOME/ros2_ws}/install/setup.bash" 2>/dev/null || true; ' \
              "export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp; " \
              "export ROS_DOMAIN_ID=0; export ROS_LOCALHOST_ONLY=0; " \
              'export CYCLONEDDS_URI="file://${LELAB_CAR_ROOT:-$HOME/shared/lelab_car}/' \
              'ros2_native_20261108/state/cyclonedds.xml"; python3 -'
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target,
         f"docker exec -i MentorPi bash -lc {shlex.quote(command)}"],
        input=remote, text=True, capture_output=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"torque readback process failed ({result.returncode}): {result.stderr[-500:]}")
    try:
        doc = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"torque readback returned non-JSON: {result.stdout[-500:]}") from exc
    if doc.get("all_verified") is not True:
        raise RuntimeError(f"per-ID torque verification failed: {doc}")
    return doc


class Samples:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.items: list[dict[str, Any]] = []

    def add(self, label: str, status: dict[str, Any]) -> None:
        row = {
            "t_utc": utc_now(), "label": label, "state": status.get("state"),
            "session_id": status.get("session_id"), "arm_state": (status.get("channels") or {}).get("arm", {}).get("state"),
            "outputs_enabled": status.get("outputs_enabled"),
            "feedback": {
                key: status.get("feedback", {}).get(key)
                for key in ("valid", "trusted", "reason", "stale_after_s", "scan_span_s", "servo_pulses")
            },
            "video": {
                key: status.get("video", {}).get(key)
                for key in ("available", "valid", "stale", "reason", "age_s", "preview_seq")
            },
            "recording": status.get("recording"),
            "follow_runtime": status.get("follow_runtime"),
            "missing_for_following": status.get("missing_for_following"),
            "last_error": status.get("last_error"),
        }
        with self.lock:
            self.items.append(row)

    def flush(self) -> None:
        with self.lock:
            rows = list(self.items)
        self.path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")


def slim(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            low = str(key).lower()
            if any(word in low for word in ("token", "secret", "key", "password", "authorization")):
                out[key] = "<redacted>"
            else:
                out[key] = slim(item)
        return out
    if isinstance(value, list):
        return [slim(item) for item in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--observe-s", type=float, default=12.0)
    parser.add_argument("--grasp-s", type=float, default=12.0)
    parser.add_argument("--run-label", default="t7-live-two-episode")
    args = parser.parse_args()
    evidence_dir = Path(args.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    samples = Samples(evidence_dir / "recording-status.jsonl")
    evidence: dict[str, Any] = {
        "runner": "t7_live_two_episode.py",
        "started_utc": utc_now(),
        "operator_present": True,
        "task": TASK,
        "observe_s": args.observe_s,
        "grasp_s": args.grasp_s,
        "steps": [],
        "torque": [],
        "errors": [],
    }
    identity: dict[str, Any] | None = None
    heartbeat_stop = threading.Event()
    heartbeat_error: list[str] = []
    session_id: str | None = None

    def note(label: str, code: int, doc: dict[str, Any]) -> None:
        evidence["steps"].append({"utc": utc_now(), "label": label, "http": code, "response": slim(doc)})

    def heartbeat_loop() -> None:
        while not heartbeat_stop.wait(2.0):
            if identity is None:
                continue
            code, doc = http_json(f"{BASE}/heartbeat", identity, timeout=5.0)
            if code != 200:
                heartbeat_error.append(f"http={code}:{doc.get('error') or doc.get('detail') or doc}")
                return

    def control(action: str) -> tuple[int, dict[str, Any]]:
        code, doc = http_json(f"{BASE}/control", {"action": action, **(identity or {})})
        note(action, code, doc)
        return code, doc

    def require_status(label: str, predicate, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = get_status()
            samples.add(label, last)
            if predicate(last):
                return last
            if heartbeat_error:
                raise RuntimeError(f"heartbeat failed: {heartbeat_error[-1]}")
            time.sleep(0.2)
        raise RuntimeError(f"{label}: status timeout: {slim(last)}")

    def motion_wait(seconds: float, label: str) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            status = get_status()
            samples.add(label, status)
            if status.get("outputs_enabled") is not True:
                raise RuntimeError(f"{label}: output stopped during recording")
            if heartbeat_error:
                raise RuntimeError(f"heartbeat failed: {heartbeat_error[-1]}")
            time.sleep(0.5)

    def reengage(label: str) -> None:
        code, doc = control("preview")
        require(code, f"{label}:preview", doc, evidence)
        code, doc = control("align")
        require(code, f"{label}:align", doc, evidence)
        if not (doc.get("hardware_preview") or {}).get("available"):
            raise RuntimeError(f"{label}: hardware preview not available")
        code, doc = control("arm")
        require(code, f"{label}:arm", doc, evidence)
        torque = torque_read()
        evidence["torque"].append({"label": label, "utc": utc_now(), "readback": torque})
        code, doc = control("engage")
        require(code, f"{label}:engage", doc, evidence)
        if doc.get("outputs_enabled") is not True:
            raise RuntimeError(f"{label}: engage did not enable outputs: {slim(doc)}")
        samples.add(f"{label}:engaged", get_status())

    def pause_and_confirm(label: str) -> dict[str, Any]:
        code, doc = control("pause_all")
        require(code, f"{label}:pause", doc, evidence)
        status = require_status(f"{label}:pause-status",
                                lambda item: item.get("outputs_enabled") is False, timeout=10.0)
        return status

    def close_episode(label: str) -> dict[str, Any]:
        pause_and_confirm(label)
        rec = get_recording()
        if rec.get("episode_id") is None:
            raise RuntimeError(f"{label}: episode disappeared before end")
        code, doc = http_json(f"{BASE}/recording", {
            "action": "end_episode", "success": None,
            "note": f"{args.run_label}:{label}:manual_unknown", **identity,
        })
        note(f"{label}:end_episode", code, doc)
        require(code, f"{label}:end_episode", doc, evidence)
        samples.add(f"{label}:ended", get_status())
        return doc

    def run_episode(number: int) -> None:
        code, doc = http_json(f"{BASE}/recording", {"action": "start_episode", **identity})
        note(f"ep{number}:start_episode", code, doc)
        require(code, f"ep{number}:start_episode", doc, evidence)
        episode_id = doc.get("episode_id")
        if not episode_id:
            raise RuntimeError(f"ep{number}: episode_id missing")
        code, doc = http_json(f"{BASE}/recording", {"action": "set_phase", "phase": "observe",
                                                   "episode_id": episode_id, **identity})
        note(f"ep{number}:observe_phase", code, doc)
        require(code, f"ep{number}:observe_phase", doc, evidence)
        if get_status().get("outputs_enabled") is not False:
            raise RuntimeError(f"ep{number}: phase switch left output enabled")
        reengage(f"ep{number}:observe")
        print(f"EP{number}_OBSERVE_READY move the leader for {args.observe_s:.0f}s", flush=True)
        motion_wait(args.observe_s, f"ep{number}:observe")
        code, doc = http_json(f"{BASE}/recording", {"action": "set_phase", "phase": "grasp",
                                                   "episode_id": episode_id, **identity})
        note(f"ep{number}:grasp_phase", code, doc)
        require(code, f"ep{number}:grasp_phase", doc, evidence)
        if get_status().get("outputs_enabled") is not False:
            raise RuntimeError(f"ep{number}: grasp phase switch left output enabled")
        reengage(f"ep{number}:grasp")
        print(f"EP{number}_GRASP_READY operate the gripper for {args.grasp_s:.0f}s", flush=True)
        motion_wait(args.grasp_s, f"ep{number}:grasp")
        close_episode(f"ep{number}")
        print(f"EP{number}_ENDED paused and closed with result=unknown", flush=True)

    try:
        assert_quiescent()
        initial = get_status()
        samples.add("initial", initial)
        evidence["initial"] = slim(initial)
        code, acquired = http_json(f"{BASE}/session", {
            "action": "acquire", "profile": PROFILE, "owner": args.run_label,
        })
        require(code, "acquire", acquired, evidence)
        session_id = acquired.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("acquire: session_id missing")
        identity = {
            "session_id": session_id,
            "session_epoch": acquired.get("session_epoch"),
            "calibration_bundle_hash": acquired.get("calibration_bundle_hash"),
        }
        if not isinstance(identity["session_epoch"], int):
            raise RuntimeError("acquire: session_epoch missing")
        samples.add("session acquired", get_status())
        heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        heartbeat_thread.start()
        code, started = http_json(f"{BASE}/recording", {"action": "start_session",
                                                        "task": TASK, **identity})
        note("start_session", code, started)
        require(code, "start_session", started, evidence)
        if not started.get("session_id"):
            raise RuntimeError("start_session: recording session id missing")
        if started.get("status") != "active":
            raise RuntimeError(f"start_session: unexpected status {started.get('status')}")
        run_episode(1)
        run_episode(2)
        pause_and_confirm("final")
        code, stopped = http_json(f"{BASE}/recording", {"action": "stop_session",
                                                        "reason": "t7_two_episode_complete", **identity})
        note("stop_session", code, stopped)
        require(code, "stop_session", stopped, evidence)
        code, released = http_json(f"{BASE}/session", {
            "action": "release", "reason": "t7_two_episode_complete", **identity,
        })
        note("release", code, released)
        require(code, "release", released, evidence)
        evidence["status"] = "pass"
    except Exception as exc:  # noqa: BLE001 - preserve the first failure and stop
        evidence["status"] = "fail"
        evidence["failure"] = f"{type(exc).__name__}: {exc}"
        evidence["errors"].append(evidence["failure"])
        try:
            if identity is not None:
                control("pause_all")
                rec = get_recording()
                if rec.get("episode_id") is not None:
                    http_json(f"{BASE}/recording", {
                        "action": "end_episode", "discarded": True,
                        "reason": "t7_runner_failure", **identity,
                    })
                if rec.get("status") in ("active", "invalid"):
                    http_json(f"{BASE}/recording", {
                        "action": "stop_session", "reason": "t7_runner_failure", **identity,
                    })
                http_json(f"{BASE}/session", {
                    "action": "release", "reason": "t7_runner_failure", **identity,
                })
        except Exception as cleanup_exc:  # noqa: BLE001
            evidence["cleanup_error"] = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
    finally:
        heartbeat_stop.set()
        samples.flush()
        evidence["finished_utc"] = utc_now()
        evidence["final_upper_status"] = slim(get_status())
        evidence["final_agent_status"] = slim(get_agent())
        (evidence_dir / "t7-live-run.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False))
    print(json.dumps({
        "status": evidence.get("status"),
        "failure": evidence.get("failure"),
        "evidence": str(evidence_dir / "t7-live-run.json"),
        "final_state": (evidence.get("final_upper_status") or {}).get("state"),
        "final_outputs": (evidence.get("final_upper_status") or {}).get("outputs_enabled"),
    }, indent=2), flush=True)
    return 0 if evidence.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
