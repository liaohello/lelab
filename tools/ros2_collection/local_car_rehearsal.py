#!/usr/bin/env python3
"""Run the deployed car-side agent on this host against synthetic car topics.

This is a local rehearsal, not a hardware acceptance: the agent code is the same
`ros2_ws/src/lelab_mobile_agent` package the Pi runs, but the follower feedback, camera, odom and
joint states come from a local publisher instead of the car. Nothing here can move a servo because
no actuator driver is running - the gateway publishes trajectory requests into an empty domain.

What it proves that unit tests cannot:
- the real `contract.CommandGate` accepts a teleop envelope written by the real coordinator;
- `set_mode(leader_arm)` runs the real DDS publisher enumeration (`get_publishers_info_by_topic`),
  so the exclusivity verdict is produced by the transport that actually has to answer on the car;
- the recorder really spawns `ros2 bag record` and produces a sqlite3 bag plus manifest and READY;
- `/status` reports the nested `control.teleop.*` shape the upper computer gates on.

Usage (ROS 2 sourced environment, e.g. the project's ros_jazzy env):
    python3 tools/ros2_collection/local_car_rehearsal.py --root /tmp/lelab_car_rehearsal
Then drive it with `tools/ros2_collection/local_teleop_session_drive.py` from the app venv.
SIGINT/SIGTERM stops only the processes this script started (it records their pids).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "lelab_mobile_agent"))

ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]
CAMERA = "/ascamera/camera_publisher/rgb0/image"
CAMERA_INFO = "/ascamera/camera_publisher/rgb0/camera_info"
FEEDBACK = "/controller_manager/joint_states"


def log(msg: str) -> None:
    print(f"[local-car] {msg}", flush=True)


class Feed:
    """Synthetic car-side sensor sources at the topics the teleop profile records."""

    def __init__(self, node, feedback_hz: float, camera_hz: float, width: int, height: int):
        import math
        from sensor_msgs.msg import CameraInfo, Image, JointState  # noqa: F401
        from std_msgs.msg import Header

        self._math = math
        self._Header = Header
        self._JointState = JointState
        self._Image = Image
        self._CameraInfo = CameraInfo
        self.node = node
        self.joint_pub = node.create_publisher(JointState, FEEDBACK, 10)
        self.js_pub = node.create_publisher(JointState, "/joint_states", 10)
        from rclpy.qos import qos_profile_sensor_data
        self.img_pub = node.create_publisher(Image, CAMERA, qos_profile_sensor_data)
        self.info_pub = node.create_publisher(CameraInfo, CAMERA_INFO, 10)
        self.width, self.height = width, height
        self.t0 = node.get_clock().now()
        node.create_timer(1.0 / max(1.0, feedback_hz), self.publish_joints)
        node.create_timer(1.0 / max(1.0, camera_hz), self.publish_camera)
        self.joint_reads = 0
        self.image_reads = 0

    def _header(self, frame: str):
        header = self._Header()
        header.stamp = self.node.get_clock().now().to_msg()
        header.frame_id = frame
        return header

    def publish_joints(self) -> None:
        t = (self.node.get_clock().now() - self.t0).nanoseconds / 1e9
        msg = self._JointState()
        msg.header = self._header("local_rehearsal")
        msg.name = list(ARM_JOINTS)
        msg.position = [0.05 * self._math.sin(t * 0.5 + i) for i in range(len(ARM_JOINTS))]
        self.joint_pub.publish(msg)
        self.js_pub.publish(msg)
        self.joint_reads += 1

    def publish_camera(self) -> None:
        msg = self._Image()
        msg.header = self._header("rgb_camera_link")
        msg.width, msg.height = self.width, self.height
        msg.encoding = "bgr8"
        msg.is_bigendian = 0
        msg.step = self.width * 3
        msg.data = bytes([(i * 7) % 256 for i in range(self.width * self.height * 3)])
        self.img_pub.publish(msg)
        info = self._CameraInfo()
        info.header = msg.header
        info.width, info.height = self.width, self.height
        info.distortion_model = "plumb_bob"
        self.info_pub.publish(info)
        self.image_reads += 1


def wait_for_feedback(port: int, timeout_s: float) -> tuple[bool, str]:
    import urllib.request
    deadline = time.time() + timeout_s
    reason = "no_status_read"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=3) as r:
                control = json.loads(r.read()).get("control", {})
            feedback = control.get("follower_feedback") or {}
            if feedback.get("available"):
                return True, f"source={feedback.get('source')} age={feedback.get('oldest_age_s')}s"
            reason = str(feedback.get("reason"))
        except Exception as exc:  # noqa: BLE001
            reason = repr(exc)
        time.sleep(0.4)
    return False, reason


def wait_for_health(port: int, timeout_s: float) -> dict:
    import urllib.request
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                return json.loads(r.read())
        except Exception as exc:  # noqa: BLE001
            last = repr(exc)
            time.sleep(0.4)
    raise RuntimeError(f"agent never became healthy on port {port}: {last}")


def subprocess_listener_pid(port: int) -> int | None:
    """The pid that owns a listening TCP port, read from `ss` the same way an operator would."""
    out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True)
    for token in out.stdout.replace(",", " ").split():
        if token.startswith("pid="):
            try:
                return int(token[4:])
            except ValueError:
                return None
    return None


def verify_answerer(root: Path, port: int, agent) -> None:
    """Refuse to drive an agent we did not start: the pid answering the port must be ours.

    The launcher execs into python, so the pid it records is the process that owns the socket;
    the pid the parent sees is the wrapper, which gave that pid away to exec.
    """
    deadline = time.time() + 20.0
    recorded = None
    while time.time() < deadline:
        pid_file = root / "agent.pid"
        if pid_file.is_file():
            try:
                recorded = int(pid_file.read_text().strip())
                break
            except ValueError:
                pass
        if agent.poll() is not None:
            raise RuntimeError(f"agent launcher exited with code {agent.returncode}; see {root / 'agent.log'}")
        time.sleep(0.3)
    if recorded is None:
        raise RuntimeError("the agent never wrote its pid file")
    listener = subprocess_listener_pid(port)
    if listener != recorded:
        raise RuntimeError(f"the socket on :{port} is owned by pid {listener}, not the agent we "
                           f"started ({recorded}); stop the other rehearsal first")
    log(f"verified: pid {recorded} owns :{port}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/lelab_car_rehearsal", help="scratch car_root")
    ap.add_argument("--port", type=int, default=18115)
    ap.add_argument("--profile", default=str(REPO / "configs" / "ros2_collection" / "hardware-teleop-v1.json"))
    ap.add_argument("--domain-id", type=int, default=57)
    ap.add_argument("--feedback-hz", type=float, default=50.0)
    ap.add_argument("--camera-hz", type=float, default=14.74)
    ap.add_argument("--image-width", type=int, default=64)
    ap.add_argument("--image-height", type=int, default=48)
    ap.add_argument("--ros-setup", nargs="*", default=[str(REPO / "ros2_ws" / "install" / "setup.bash")],
                    help="workspace setup files to source before starting the agent")
    args = ap.parse_args()

    root = Path(args.root)
    # Set it for this process too: --domain-id only reaches the child through `env`, and a feed
    # node published into the default domain is invisible to the agent rather than obviously wrong.
    os.environ["ROS_DOMAIN_ID"] = str(args.domain_id)
    shutil.rmtree(root, ignore_errors=True)
    (root / "runtime").mkdir(parents=True)
    # The profile records into a path relative to car_root; keep the rehearsal self-contained.
    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    profile["recording"]["session_root"] = str(root / "sessions")
    scratch_profile = root / "hardware-teleop-v1.local.json"
    scratch_profile.write_text(json.dumps(profile), encoding="utf-8")

    # A rehearsal that silently attaches to an agent started earlier proves nothing, so refuse a
    # busy port up front and require that the pid answering /health is the pid we started.
    listener = subprocess.run(["ss", "-ltnpH", f"sport = :{args.port}"], capture_output=True, text=True)
    if listener.stdout.strip():
        raise SystemExit(f"REFUSED: port {args.port} is already in use ({listener.stdout.strip()}); "
                         "stop that rehearsal first instead of driving an unknown agent")

    env = dict(os.environ, ROS_DOMAIN_ID=str(args.domain_id),
               PYTHONPATH=f"{REPO / 'ros2_ws' / 'src' / 'lelab_mobile_agent'}:" + os.environ.get("PYTHONPATH", ""))

    rclpy_ok = True
    try:
        import rclpy  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        rclpy_ok = False
        log(f"rclpy unavailable ({exc}); starting the agent without a local feed")

    # `ros2 bag record` discovers its storage plugins through AMENT_PREFIX_PATH, so the agent has
    # to be started from a sourced shell exactly like the deployed run_readonly.sh wrapper does.
    # Sourcing also prepends the colcon build of this same package to PYTHONPATH, and that copy can
    # be stale: a rehearsal that silently runs ros2_ws/install/... proves nothing about the source
    # under test. So the wrapper re-pins the source tree and refuses to start on any other file.
    pkg_dir = REPO / "ros2_ws" / "src" / "lelab_mobile_agent"
    launcher = root / "run_local_agent.sh"
    source_lines = "\n".join(f'[ -f "{f}" ] && source "{f}"' for f in args.ros_setup)
    launcher.write_text(f"""#!/usr/bin/env bash
set -euo pipefail
set +u
{source_lines}
export PYTHONPATH="{pkg_dir}:${{PYTHONPATH:-}}"
set -u
RESOLVED=$("{sys.executable}" -c 'import lelab_mobile_agent.agent as m; print(m.__file__)')
echo "resolved agent module: $RESOLVED"
if [ "$RESOLVED" != "{pkg_dir}/lelab_mobile_agent/agent.py" ]; then
  echo "REFUSED: the agent resolves outside the source under test ($RESOLVED)" >&2
  exit 1
fi
echo "$RESOLVED" > {root}/agent_module.txt
# This script execs into python, so $$ is the exact agent pid.
echo $$ > {root}/agent.pid
exec "{sys.executable}" -m lelab_mobile_agent.agent --ros-args \
  -p profile_path:={scratch_profile} -p car_root:={root} \
  -p host:=127.0.0.1 -p port:={args.port} -p use_sim_time:=false
""", encoding="utf-8")
    launcher.chmod(0o755)
    agent = subprocess.Popen([str(launcher)], env=env, cwd=str(root),
                             stdout=open(root / "agent.log", "wb"), stderr=subprocess.STDOUT,
                             start_new_session=True)
    log(f"agent pid={agent.pid} (bash wrapper) port={args.port} domain={args.domain_id} root={root}")
    pids = [agent.pid]

    stop = threading.Event()

    def shutdown(*_a):
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    feed_thread = None
    node = None
    try:
        health = wait_for_health(args.port, 40.0)
        log(f"health: {health}")
        if rclpy_ok:
            import rclpy
            from rclpy.node import Node

            rclpy.init()
            node = Node("lelab_local_car_feed")
            feed = Feed(node, args.feedback_hz, args.camera_hz, args.image_width, args.image_height)
            spinner = threading.Thread(target=lambda: rclpy.spin(node), daemon=True,
                                       name="local-car-feed")
            spinner.start()
            log("synthetic feed running")
            # A feed node in a different ROS domain looks exactly like a feed node that is running,
            # so ask the agent itself whether the feedback actually arrived before declaring ready.
            available, why = wait_for_feedback(args.port, 20.0)
            if not available:
                raise RuntimeError(f"agent never saw follower feedback: {why}")
            log(f"agent ingested follower feedback ({why})")
        verify_answerer(root, args.port, agent)
        module_file = (root / "agent_module.txt").read_text(encoding="utf-8").strip()
        (root / "READY_FOR_DRIVER.json").write_text(json.dumps(
            {"port": args.port, "agent_pid": int((root / "agent.pid").read_text().strip()),
             "domain_id": args.domain_id, "profile": profile["profile"], "car_root": str(root),
             "agent_module": module_file,
             "agent_module_sha256": hashlib.sha256(Path(module_file).read_bytes()).hexdigest()[:16],
             "feedback_hz": args.feedback_hz, "camera_hz": args.camera_hz,
             "image": [args.image_width, args.image_height], "health": health}), encoding="utf-8")
        log(f"marker written: {root / 'READY_FOR_DRIVER.json'}")
        while not stop.is_set():
            if agent.poll() is not None:
                log(f"agent exited early with code {agent.returncode}; see {root / 'agent.log'}")
                return 1
            time.sleep(0.5)
    finally:
        log("stopping only the processes this script started")
        for pid in pids:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except OSError:
                try:
                    agent.terminate()
                except Exception:  # noqa: BLE001
                    pass
        try:
            agent.wait(timeout=15)
        except Exception:  # noqa: BLE001
            agent.kill()
        if node is not None:
            try:
                node.destroy_node()
                rclpy.shutdown()
            except Exception:  # noqa: BLE001
                pass
        log(f"logs and sessions kept under {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
