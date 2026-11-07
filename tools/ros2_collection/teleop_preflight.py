#!/usr/bin/env python3
"""Read-only pre-flight for the SO-101 mobile teleop stage (runbook §5.2 steps 0-2).

Everything here is read-only by construction: remote probes come from a fixed allowlist and the
script refuses to run anything else. It never starts a container, never calls a service, never
writes the board and never sends motion - `start_car_arm_stack.sh --i-authorize-board-writes` stays
an explicit operator decision. Run `--render` to see exactly what it would execute.

    tools/ros2_collection/teleop_preflight.py --render
    tools/ros2_collection/teleop_preflight.py --pi user@car-host \
        --agent-url http://127.0.0.1:8110 --app-url http://127.0.0.1:8080 --json /tmp/preflight.json
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ARM_TOPIC = "/lelab/arm/trajectory_request"
GRIPPER_TOPIC = "/lelab/gripper/trajectory_request"
FEEDBACK_TOPIC = "/controller_manager/joint_states"
DEPLOYED_PKG = "/home/ubuntu/lelab_car/lelab_mobile_agent"
CONTAINER = "MentorPi"

# The remote probes, in order. Each entry is (check, in-container command); they are all read-only
# queries. Keeping them in one place is what makes `--render` and the test meaningful.
READ_ONLY_PROBES = [
    ("car_container", "docker inspect -f '{{{{.State.Running}}}} {{{{.State.ExitCode}}}} {{{{.HostConfig.RestartPolicy.Name}}}}' '{container}'"),
    ("deployed_agent_source", f"sha256sum {DEPLOYED_PKG}/lelab_mobile_agent/agent.py"),
    ("profile_on_car", f"ls -1 {DEPLOYED_PKG}/config/"),
    ("arm_publisher", f"ros2 topic info -v {ARM_TOPIC}"),
    ("gripper_publisher", f"ros2 topic info -v {GRIPPER_TOPIC}"),
    ("follower_feedback_publisher", f"ros2 topic info -v {FEEDBACK_TOPIC}"),
    # Two facts the 2026-11-03 rehearsal showed this pass could not see: the vendor board node
    # reads MACHINE_TYPE at import and dies with a KeyError when it is unset, and nothing reports
    # whether that node is the one currently driving the board.
    ("machine_type_env", "printenv MACHINE_TYPE || echo MACHINE_TYPE_UNSET"),
    ("vendor_board_node", "ros2 node list"),
]

WRITE_PATTERNS = re.compile(
    r"(docker (start|run|stop|restart|exec -i)|systemctl|ros2 (service|action) (call|send_goal)|"
    r"rosservice|/control/|/emergency|set_mode|start_episode|bag record|--apply|init_pose|pkill|kill|rm -)",
    re.IGNORECASE)


def probe_commands(container: str = CONTAINER) -> list[tuple[str, str]]:
    """The exact remote commands, with the container name substituted."""
    return [(name, cmd.format(container=container)) for name, cmd in READ_ONLY_PROBES]


def assert_read_only(commands: list[tuple[str, str]]) -> list[str]:
    """Anything that could change the car is a bug in this script, not a warning."""
    return [f"{name}: {cmd}" for name, cmd in commands if WRITE_PATTERNS.search(cmd)]


def remote_command(name: str, cmd: str, container: str) -> str:
    """Inspect Docker on the Pi host; query ROS and files inside the container."""
    if name == "car_container":
        return cmd
    return f"docker exec {shlex.quote(container)} bash -lc {shlex.quote(cmd)}"


def run(command: str, timeout: int) -> tuple[int, str]:
    try:
        done = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=timeout)
        return done.returncode, (done.stdout + done.stderr).strip()
    except FileNotFoundError as exc:
        return 127, f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def local_leader_ports() -> list[str]:
    base = Path("/dev/serial/by-id")
    return sorted(p.name for p in base.iterdir()) if base.is_dir() else []


def http_json(url: str, timeout: int) -> tuple[int, object]:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:200]
        return exc.code, body
    except Exception as exc:  # noqa: BLE001 - a probe must never abort the pass
        return 124, f"{type(exc).__name__}: {exc}"


def evaluate(status: dict | None, agent: dict | None, ports: list[str],
             remote: dict[str, tuple[int, str]], ssh_target: str,
             feedback_max_age: float = 0.5) -> list[dict]:
    """Turn raw probe output into the verdicts §5.2 asks for, naming what is missing."""
    checks: list[dict] = []

    def verdict(name: str, ok: bool, detail: str, blocking: bool = True) -> None:
        checks.append({"check": name, "ok": bool(ok), "blocking": blocking, "detail": detail})

    verdict("leader_serial_on_upper_host", bool(ports),
            ", ".join(ports) if ports else "no /dev/serial/by-id entry: no leader USB identity detected on this host")

    if agent is None:
        verdict("car_agent_reachable", False, f"GET /status failed: {agent_missing(remote, ssh_target)}")
    else:
        profile = str(agent.get("profile", ""))
        read_only = bool(agent.get("read_only"))
        verdict("car_agent_reachable", True, f"profile={profile} read_only={read_only}")
        verdict("car_profile_is_teleop", "hardware_teleop" in profile and not read_only,
                f"loaded profile is {profile!r}, read_only={read_only}; the accepted read-only "
                f"profile has no control plane and must not be widened")
        teleop = (agent.get("control") or {}).get("teleop") or {}
        verdict("chassis_locked_as_declared", teleop.get("chassis_locked") is True,
                f"chassis_locked={teleop.get('chassis_locked')}")
        verdict("hold_policy_declared_without_torque_off",
                bool(teleop.get("hold_policy")) and teleop.get("torque_control_offered") is False,
                json.dumps({k: teleop.get(k) for k in ("hold_policy", "torque_control_offered")}))
        ages = (agent.get("topics") or {}).get("ages_s") or {}
        feedback = {t: a for t, a in ages.items() if "joint_states" in t}
        fresh_feedback = any(isinstance(age, (float, int)) and not isinstance(age, bool)
                             and math.isfinite(age) and 0 <= age <= feedback_max_age
                             for age in feedback.values())
        verdict("follower_feedback_stream_visible", fresh_feedback,
                json.dumps(feedback) if feedback else
                "no /controller_manager/joint_states age: the vendor feedback stream is not up yet")
        verdict("exclusivity_verdict_reported", teleop.get("exclusivity") is not None,
                json.dumps(teleop.get("exclusivity"))[:160] if teleop.get("exclusivity") is not None
                else "the car has not attempted an enable yet, so publisher exclusivity is unknown",
                blocking=False)

    if status is None:
        verdict("upper_app_reachable", False, "GET /ros-mobile/teleop/status failed")
    else:
        verdict("upper_app_reachable", True, f"state={status.get('state')}")
        missing = status.get("missing_for_following") or []
        reasons = status.get("gate") or missing
        ready = not missing
        verdict("calibration_and_leases_ready_for_preview", ready,
                json.dumps(reasons)[:240] if not ready else f"version={status.get('calibration_version')}")

    for name, (code, text) in remote.items():
        if name == "car_container":
            verdict("car_container_running", code == 0 and text.startswith("true"),
                    text or f"docker inspect failed ({code})")
        elif name == "deployed_agent_source":
            verdict("deployed_agent_matches_source",
                    code == 0 and bool(text.split()) and text.split()[0] == local_agent_sha(),
                    (f"car={text.split()[0]} local={local_agent_sha()}" if code == 0 and text.split()
                     else f"car hash not read: {text[:120] or f'exit {code}'}"))
        elif name == "machine_type_env":
            if code != 0:
                verdict("vendor_node_machine_type_exported", False, f"not read: {text[:80]}",
                        blocking=False)
            elif "MACHINE_TYPE_UNSET" in text or not text:
                verdict("vendor_node_machine_type_exported", False,
                        "unset in the container: the vendor board node reads os.environ['MACHINE_TYPE'] "
                        "at construction and raises KeyError, so the minimal entry must export it before "
                        "starting that node", blocking=False)
            else:
                verdict("vendor_node_machine_type_exported", True, f"MACHINE_TYPE={text}")
        elif name == "vendor_board_node":
            if code != 0:
                verdict("vendor_board_node_driving_the_uart", False, f"not read: {text[:80]}",
                        blocking=False)
            else:
                board = [n for n in text.split() if "ros_robot_controller" in n]
                verdict("vendor_board_node_driving_the_uart", bool(board),
                        f"{', '.join(board)}: whatever publishes feedback is the vendor node, so the "
                        f"minimal entry must not start a second owner" if board else
                        "no board node in the graph: nothing drives the control board right now, so the "
                        "follower feedback stream cannot exist until the minimal entry starts it",
                        blocking=False)
        elif name.endswith("_publisher"):
            topic = {"arm_publisher": ARM_TOPIC, "gripper_publisher": GRIPPER_TOPIC,
                     "follower_feedback_publisher": FEEDBACK_TOPIC}[name]
            foreign = foreign_publishers(text)
            if name == "follower_feedback_publisher":
                count = re.search(r"publisher count:\s*(\d+)", text, re.IGNORECASE)
                verdict(name, code == 0 and count is not None and int(count.group(1)) > 0,
                        f"{topic}: {text[:120]}")
            else:
                verdict(f"no_foreign_publisher_on_{topic}", code == 0 and not foreign,
                        f"{topic}: foreign publishers {foreign or 'none'}" if code == 0
                        else f"{topic}: not read: {text[:120] or f'exit {code}'}", blocking=False)
    return checks


def agent_missing(remote: dict, ssh_target: str) -> str:
    if ssh_target:
        return f"agent unreachable and car probes ran={bool(remote)}"
    return "agent unreachable and no --pi given, so the car side was not probed"


def local_agent_sha() -> str:
    import hashlib

    return hashlib.sha256((REPO / "ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/agent.py")
                          .read_bytes()).hexdigest()


def foreign_publishers(topic_info: str) -> list[str]:
    """Node names publishing a trajectory topic that are not our own agent.

    The runbook's step 0 is exactly this question; the car refuses to enable when an unknown name
    shows up, so the pre-flight must surface it rather than let the operator discover it at engage.
    """
    names = re.findall(r"Node name:\s*(\S+)", topic_info)
    return [n for n in names if "lelab_mobile_agent" not in n and n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pi", default="", help="ssh target for the car, e.g. user@car-host")
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--agent-url", default="http://127.0.0.1:8110", help="car agent (via the tunnel)")
    ap.add_argument("--app-url", default="http://127.0.0.1:8080", help="upper-computer LeLab")
    ap.add_argument("--timeout", type=int, default=12)
    ap.add_argument("--feedback-max-age", type=float, default=0.5,
                    help="maximum joint-state age in seconds (default 0.5)")
    ap.add_argument("--json", default="", help="write the full report here")
    ap.add_argument("--render", action="store_true", help="print the commands and exit")
    args = ap.parse_args()
    if not math.isfinite(args.feedback_max_age) or args.feedback_max_age <= 0:
        ap.error("--feedback-max-age must be a positive finite number")

    commands = probe_commands(args.container)
    illegal = assert_read_only(commands)
    if illegal:
        print("REFUSED: the pre-flight list contains a non-read-only probe:\n  " + "\n  ".join(illegal),
              file=sys.stderr)
        return 2
    if args.render:
        print("# local (no network):")
        print("  ls /dev/serial/by-id")
        print(f"  GET {args.agent_url}/status")
        print(f"  GET {args.app_url}/ros-mobile/teleop/status")
        print(f"# remote via ssh {args.pi or '<--pi>'} (host and container):")
        for name, cmd in commands:
            print(f"  [{name}] {remote_command(name, cmd, args.container)}")
        return 0

    ports = local_leader_ports()
    agent_code, agent_body = http_json(args.agent_url.rstrip("/") + "/status", args.timeout)
    app_code, app_body = http_json(args.app_url.rstrip("/") + "/ros-mobile/teleop/status", args.timeout)
    remote: dict[str, tuple[int, str]] = {}
    if args.pi:
        for name, cmd in commands:
            shell = remote_command(name, cmd, args.container)
            remote[name] = run(f"ssh -o BatchMode=yes -o ConnectTimeout={args.timeout} "
                               f"{shlex.quote(args.pi)} {shlex.quote(shell)}", args.timeout + 6)
    else:
        for name, _cmd in commands:
            remote[name] = (127, "skipped: no --pi target")

    checks = evaluate(app_body if app_code == 200 else None,
                      agent_body if agent_code == 200 else None, ports, remote, args.pi,
                      args.feedback_max_age)
    blocking_missing = [c["check"] for c in checks if not c["ok"] and c["blocking"]]
    report = {"generated_by": "teleop_preflight.py", "checks": checks,
              "ready_for_readonly_session": not blocking_missing,
              "blocking_missing": blocking_missing,
              "note": "read-only probes only; starting the vendor stack and any board write stay "
                      "explicit operator actions (start_car_arm_stack.sh --i-authorize-board-writes)"}
    for c in checks:
        print(f"{'OK ' if c['ok'] else 'NO '} {'[blocking] ' if c['blocking'] and not c['ok'] else ''}"
              f"{c['check']}: {c['detail']}")
    print(f"\nready for a read-only session: {report['ready_for_readonly_session']}"
          f" (blocking gaps: {', '.join(blocking_missing) or 'none'})")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["ready_for_readonly_session"] else 1


if __name__ == "__main__":
    sys.exit(main())
