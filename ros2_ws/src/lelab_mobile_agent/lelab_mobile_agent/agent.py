"""Car-side HTTP agent for the LeLab mobile manipulator collection loop."""
from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import secrets
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
import rclpy
from ament_index_python.packages import get_package_share_directory
from builtin_interfaces.msg import Duration
from geometry_msgs.msg import PoseStamped, Twist
from nav_msgs.msg import Odometry
try:
    from PIL import Image
except ImportError:  # camera-only images may provide OpenCV instead
    Image = None
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, Image as RosImage
from sensor_msgs.msg import JointState, LaserScan, Imu
from rosgraph_msgs.msg import Clock
from tf2_msgs.msg import TFMessage
from std_msgs.msg import Bool, Empty, String
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

from .contract import CommandGate, ContractError, load_profile, new_session_id, state_vector
try:
    from ros_gz_interfaces.msg import Contacts
except ImportError:  # hardware-only image builds may omit Gazebo interfaces
    Contacts = None

from .recorder import SessionRecorder

ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"]
GRIPPER_JOINTS = ["gripper_base_joint", "gripper_left_join", "gripper_right_joint", "left_jaw_joint", "right_jaw_joint"]
GRIPPER_RATIOS = [-1.5, -1.5, -0.0115995, 0.0115995]
GRIPPER_LIMITS = [(-1.638, 0.0), (-2.4569, 2.4569), (-2.4569, 2.4569), (0.0, 0.019), (-0.019, 0.0)]
ARM_LIMITS = {name: (-2.09, 2.09) for name in ARM_JOINTS}


class ApiError(Exception):
    def __init__(self, status: int, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.status = status
        self.reason = reason
        self.detail = detail


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pose_xyz(msg: PoseStamped | None) -> list[float] | None:
    if msg is None:
        return None
    p = msg.pose.position
    return [float(p.x), float(p.y), float(p.z)]


def _yaw(q) -> float:
    return float(np.arctan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)))


class MobileAgent(Node):
    def __init__(self):
        super().__init__("lelab_mobile_agent")
        self.declare_parameter("profile_path", "")
        self.declare_parameter("car_root", os.path.expanduser("~/Desktop/lelab/work/ros2_collection/car"))
        self.declare_parameter("host", "127.0.0.1")
        self.declare_parameter("port", 8110)
        self.declare_parameter("model_sdf", "")
        self.declare_parameter("camera_profile", "")
        self.declare_parameter("task_name", "")
        profile_path = self.get_parameter("profile_path").value
        if not profile_path:
            profile_path = str(Path(get_package_share_directory("lelab_mobile_agent")) / "config" / "runtime-profile-v1.json")
        self.profile = load_profile(profile_path)
        self.profile_path = profile_path
        self.runtime = self.profile.get("runtime", {})
        self.read_only = self.runtime.get("mode") == "hardware_readonly"
        self.actuator_outputs = bool(self.runtime.get("actuator_outputs", not self.read_only))
        sensor_cfg = self.profile.get("sensors", {}).get("wrist_rgb", {})
        self.camera_profile = self.get_parameter("camera_profile").value or sensor_cfg.get("profile_id") or self.runtime.get("camera_profile_default")
        self.camera_extrinsics = (sensor_cfg.get("profiles", {}) or {}).get(self.camera_profile, {})
        default_task = self.profile.get("task", {}).get("id") if self.read_only else "place_in_tray"
        self.task_name = self.get_parameter("task_name").value or default_task
        if not self.read_only and self.task_name not in ("place_in_tray", "place_on_box"):
            raise RuntimeError(f"unknown task_name {self.task_name!r}")
        self.rgb_topic = sensor_cfg.get("rgb_topic") or self.profile.get("features", {}).get("image", {}).get("topic")
        self.camera_info_topic = sensor_cfg.get("camera_info_topic")
        self.depth_topic = (self.profile.get("sensors", {}).get("depth", {}) or {}).get("topic")
        self.lidar_topic = (self.profile.get("sensors", {}).get("lidar", {}) or {}).get("topic")
        self.imu_topic = (self.profile.get("sensors", {}).get("imu", {}) or {}).get("topic")
        self.state_topic = self.runtime.get("joint_state_topic")
        self.base_topic = self.runtime.get("base_velocity_topic")
        self.wheel_topic = self.runtime.get("wheel_odom_topic")
        self.record_joint_topic = self.runtime.get("record_joint_state_topic")
        self.record_joint_hz = float(self.runtime.get("joint_record_hz") or 0)
        self.car_root = Path(self.get_parameter("car_root").value).expanduser()
        self.car_root.mkdir(parents=True, exist_ok=True)
        self.model_sdf = Path(self.get_parameter("model_sdf").value or (self.car_root / "runtime" / "lelab_mobile.sdf"))
        self.lock = threading.RLock()
        self.sessions = SessionRecorder(self.car_root, self.profile, self.get_logger())
        self.agent_version = "0.1.0"

        self.joints: dict[str, float] = {}
        self.joint_stamp_ns = 0
        self.joints_wall = 0.0
        self.odom_vx = 0.0
        self.odom_vy = 0.0
        self.odom_wz = 0.0
        self.odom_pose = (0.0, 0.0, 0.0)
        self.odom_wall = 0.0
        self.wheel_odom: dict[str, float] = {}
        self.wheel_odom_wall = 0.0
        self.clock_domain = self.runtime.get("clock_domain") or "ros_time"
        self.camera_info_meta: dict[str, Any] = {}
        self.cube: PoseStamped | None = None
        self.container: PoseStamped | None = None
        self.box: PoseStamped | None = None
        self.task_wall = 0.0
        self.last_rx: dict[str, float] = {}
        self.image: RosImage | None = None
        self.image_stamp_ns = 0
        self.image_wall = 0.0
        self.image_count = 0
        self.image_window: deque[float] = deque(maxlen=30)
        self.preview_jpeg: bytes | None = None
        self.preview_seq = 0
        self.preview_wall = 0.0
        self.preview_backend = "pillow" if Image is not None else "unchecked"
        self.depth_meta: dict[str, Any] = {}
        self.lidar_meta: dict[str, Any] = {}
        self.imu_meta: dict[str, Any] = {}
        self.contact_count = 0
        self.contact_pairs: deque[dict[str, Any]] = deque(maxlen=100)
        self.safety: dict[str, Any] = {}
        self.safety_wall = 0.0
        self.applied: deque[dict[str, Any]] = deque(maxlen=200)

        self.control_token: str | None = None
        self.control_client: str | None = None
        self.control_mode = "idle"
        self.control_expires = 0.0
        self.control_acquired_wall = 0.0
        self.gate: CommandGate | None = None
        self.command_seq = -1
        self.base_target = Twist()
        self.base_valid_until = 0.0
        self.last_base_wall = 0.0
        self.estop_latched = False
        self.last_reject: dict[str, Any] | None = None
        self.rejections: deque[dict[str, Any]] = deque(maxlen=100)
        self.session_id: str | None = None
        self.episode_id: str | None = None
        self.episode_source = ""
        self.episode_started_wall = 0.0
        self.episode_initial: dict[str, Any] = {}
        self.episode_max_lift = 0.0
        self.episode_contacts = 0
        self.episode_phases: list[dict[str, Any]] = []
        self.last_action = [0.0] * 9
        self.last_arm_targets = [0.0] * 5
        self.last_gripper_target = 0.0
        self.last_action_sim_ns = 0
        self.last_action_source = ""
        self.last_action_seq = -1
        self.events: deque[dict[str, Any]] = deque(maxlen=5000)
        self.expert_status: dict[str, Any] = {"running": False, "phase": "idle", "error": None, "started_wall": None, "result": None}
        self.expert_cancel = threading.Event()
        self.expert_thread: threading.Thread | None = None
        self.arm_queue: queue.Queue = queue.Queue()
        self.gripper_queue: queue.Queue = queue.Queue()
        self.last_heartbeat = 0.0
        self.last_zero = 0.0
        self.start_wall = time.time()

        # Actuator publishers/clients are created only for the simulation path.
        # In hardware-readonly mode their absence is part of the safety contract.
        self.base_pub = None
        self.arm_pub = None
        self.gripper_pub = None
        self.heartbeat_pub = None
        self.estop_pub = None
        self.reset_pub = None
        self.record_joint_pub = None
        self.task_record_pubs: dict[str, Any] = {}
        self.task_record_last: dict[str, float] = {}
        self.event_pub = self.create_publisher(String, "/lelab/events", 20)
        if self.actuator_outputs:
            self.base_pub = self.create_publisher(Twist, "/lelab/base/cmd_vel_request", 10)
            self.arm_pub = self.create_publisher(JointTrajectory, "/lelab/arm/trajectory_request", 10)
            self.gripper_pub = self.create_publisher(JointTrajectory, "/lelab/gripper/trajectory_request", 10)
            self.heartbeat_pub = self.create_publisher(Empty, "/lelab/heartbeat", 10)
            self.estop_pub = self.create_publisher(Bool, "/lelab/estop", 10)
            self.reset_pub = self.create_publisher(String, "/lelab/safety_reset", 10)
            if self.record_joint_topic and self.record_joint_hz > 0:
                self.record_joint_pub = self.create_publisher(JointState, self.record_joint_topic, 10)
                self.create_timer(1.0 / self.record_joint_hz, self._record_joint_tick)
            for name in ("cube", "container", "box"):
                self.task_record_pubs[name] = self.create_publisher(PoseStamped, f"/lelab/recording/{name}_pose", 10)
            self.create_timer(0.02, self._control_tick)
            self.create_subscription(String, "/lelab/safety_state", self._on_safety, 10)
            self.create_subscription(String, "/lelab/applied_command", self._on_applied, 10)
            self.create_subscription(Clock, "/clock", lambda _m: self.last_rx.__setitem__("/clock", time.monotonic()), 10)
            self.create_subscription(TFMessage, "/tf", lambda _m: self.last_rx.__setitem__("/tf", time.monotonic()), 10)
            if Contacts is not None:
                for topic in ("/lelab/contact/left_jaw_link", "/lelab/contact/right_jaw_link", "/lelab/contact/gripper_base_link"):
                    self.create_subscription(Contacts, topic, lambda m, t=topic: self._on_contacts(t, m), 10)
        if self.state_topic:
            self.create_subscription(JointState, self.state_topic, self._on_joints, 10)
        if self.base_topic:
            self.create_subscription(Odometry, self.base_topic, self._on_model_odom, 10)
        if self.wheel_topic:
            self.create_subscription(Odometry, self.wheel_topic, self._on_odom, 10)
        if self.rgb_topic:
            self.create_subscription(RosImage, self.rgb_topic, self._on_image, qos_profile_sensor_data)
        else:
            self.get_logger().error("no wrist camera topic configured; read-only health will be unavailable")
        if self.camera_info_topic:
            self.create_subscription(CameraInfo, self.camera_info_topic, self._on_camera_info, qos_profile_sensor_data)
        if self.depth_topic:
            self.create_subscription(RosImage, self.depth_topic, self._on_depth, qos_profile_sensor_data)
        if self.lidar_topic:
            self.create_subscription(LaserScan, self.lidar_topic, self._on_lidar, 5)
        if self.imu_topic:
            self.create_subscription(Imu, self.imu_topic, self._on_imu, 5)
        if not self.read_only:
            self.create_subscription(PoseStamped, "/lelab/task/cube_pose", lambda m: self._on_task("cube", m), 10)
            self.create_subscription(PoseStamped, "/lelab/task/container_pose", lambda m: self._on_task("container", m), 10)
            self.create_subscription(PoseStamped, "/lelab/task/box_pose", lambda m: self._on_task("box", m), 10)
        self.create_timer(0.1, self._status_tick)
        self._preview_stop = threading.Event()
        self._preview_thread = threading.Thread(target=self._preview_loop, daemon=True)
        self._preview_thread.start()
        self.get_logger().info(f"agent ready; profile={self.profile['profile']} car_root={self.car_root}")

    # ------------------------------------------------------------------ inputs
    def _on_joints(self, msg: JointState):
        with self.lock:
            self.joints = dict(zip(msg.name, msg.position))
            self.joint_stamp_ns = int(msg.header.stamp.sec * 1e9 + msg.header.stamp.nanosec)
            self.joints_wall = time.monotonic()
            if self.state_topic:
                self.last_rx[self.state_topic] = self.joints_wall

    def _on_model_odom(self, msg: Odometry):
        with self.lock:
            self.odom_vx = float(msg.twist.twist.linear.x)
            self.odom_vy = float(msg.twist.twist.linear.y)
            self.odom_wz = float(msg.twist.twist.angular.z)
            self.odom_pose = (msg.pose.pose.position.x, msg.pose.pose.position.y, _yaw(msg.pose.pose.orientation))
            self.odom_wall = time.monotonic()
            if self.base_topic:
                self.last_rx[self.base_topic] = self.odom_wall

    def _on_odom(self, msg: Odometry):
        # Wheel odometry is a diagnostic stream only; it is never used as the
        # simulation body state when a model-odometry topic is configured.
        with self.lock:
            self.wheel_odom = {
                "vx": float(msg.twist.twist.linear.x),
                "vy": float(msg.twist.twist.linear.y),
                "wz": float(msg.twist.twist.angular.z),
                "pose": [msg.pose.pose.position.x, msg.pose.pose.position.y, _yaw(msg.pose.pose.orientation)],
            }
            self.wheel_odom_wall = time.monotonic()
            if self.wheel_topic:
                self.last_rx[self.wheel_topic] = self.wheel_odom_wall

    def _on_camera_info(self, msg: CameraInfo):
        with self.lock:
            self.camera_info_meta = {
                "width": msg.width, "height": msg.height, "frame_id": msg.header.frame_id,
                "k": list(msg.k), "d": list(msg.d), "distortion_model": msg.distortion_model,
                "stamp_ns": int(msg.header.stamp.sec * 1e9 + msg.header.stamp.nanosec),
            }
            if self.camera_info_topic:
                self.last_rx[self.camera_info_topic] = time.monotonic()

    def _record_joint_tick(self):
        if self.record_joint_pub is None:
            return
        with self.lock:
            if not self.joints:
                return
            msg = JointState()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.name = list(self.joints.keys())
            msg.position = [float(v) for v in self.joints.values()]
            self.record_joint_pub.publish(msg)
            if self.record_joint_topic:
                self.last_rx[self.record_joint_topic] = time.monotonic()

    def _on_task(self, name: str, msg: PoseStamped):
        now = time.monotonic()
        with self.lock:
            setattr(self, name, msg)
            self.task_wall = now
            self.last_rx[f"/lelab/task/{name}_pose"] = now
            publisher = self.task_record_pubs.get(name)
            if publisher is not None and now - self.task_record_last.get(name, 0.0) >= 0.05:
                self.task_record_last[name] = now
                publisher.publish(msg)

    def _on_image(self, msg: RosImage):
        now = time.monotonic()
        with self.lock:
            self.image = msg
            self.image_stamp_ns = int(msg.header.stamp.sec * 1e9 + msg.header.stamp.nanosec)
            self.image_wall = now
            self.image_count += 1
            self.image_window.append(now)
            if self.rgb_topic:
                self.last_rx[self.rgb_topic] = now

    def _on_depth(self, msg: RosImage):
        with self.lock:
            self.depth_meta = {
                "width": msg.width, "height": msg.height, "encoding": msg.encoding,
                "stamp_ns": int(msg.header.stamp.sec * 1e9 + msg.header.stamp.nanosec),
                "frame_id": msg.header.frame_id, "bytes": len(msg.data),
            }
            if self.depth_topic:
                self.last_rx[self.depth_topic] = time.monotonic()

    def _on_lidar(self, msg: LaserScan):
        with self.lock:
            self.lidar_meta = {"ranges": len(msg.ranges), "frame_id": msg.header.frame_id, "stamp_ns": int(msg.header.stamp.sec * 1e9 + msg.header.stamp.nanosec)}
            if self.lidar_topic:
                self.last_rx[self.lidar_topic] = time.monotonic()

    def _on_imu(self, msg: Imu):
        with self.lock:
            self.imu_meta = {"frame_id": msg.header.frame_id, "stamp_ns": int(msg.header.stamp.sec * 1e9 + msg.header.stamp.nanosec)}
            if self.imu_topic:
                self.last_rx[self.imu_topic] = time.monotonic()

    def _on_safety(self, msg: String):
        try:
            with self.lock:
                self.safety = json.loads(msg.data)
                self.safety_wall = time.monotonic()
        except json.JSONDecodeError:
            pass

    def _on_applied(self, msg: String):
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        with self.lock:
            self.applied.append(data)

    def _on_contacts(self, topic: str, msg: Contacts):
        now = time.monotonic()
        with self.lock:
            self.last_rx["/lelab/contacts"] = now
            if msg.contacts:
                self.contact_count += len(msg.contacts)
                self.episode_contacts += len(msg.contacts)
                for contact in msg.contacts[:5]:
                    self.contact_pairs.append({
                        "topic": topic,
                        "collision1": contact.collision1.name,
                        "collision2": contact.collision2.name,
                        "wall_time": now,
                    })

    # ------------------------------------------------------------------ preview
    def _preview_loop(self):
        while not self._preview_stop.is_set():
            time.sleep(0.08)
            with self.lock:
                msg = self.image
                if msg is None or self.image_wall == self.preview_wall:
                    continue
                data = bytes(msg.data)
                width, height, encoding = msg.width, msg.height, msg.encoding
                stamp = self.image_wall
            try:
                arr = np.frombuffer(data, dtype=np.uint8)
                if encoding in ("rgb8", "bgr8"):
                    arr = arr.reshape(height, width, 3)
                elif encoding in ("mono8", "8UC1"):
                    arr = arr.reshape(height, width)
                else:
                    continue
                if Image is not None:
                    rgb = arr[:, :, ::-1] if encoding == "bgr8" else arr
                    image = Image.fromarray(rgb, "RGB" if rgb.ndim == 3 else "L")
                    buf = io.BytesIO()
                    image.save(buf, format="JPEG", quality=78)
                    jpeg = buf.getvalue()
                    backend = "pillow"
                else:
                    import cv2

                    bgr = arr[:, :, ::-1] if encoding == "rgb8" else arr
                    ok, encoded = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 78])
                    if not ok:
                        raise RuntimeError("cv2.imencode returned false")
                    jpeg = encoded.tobytes()
                    backend = "opencv"
                with self.lock:
                    self.preview_jpeg = jpeg
                    self.preview_backend = backend
                    self.preview_seq += 1
                    self.preview_wall = stamp
            except Exception as exc:  # noqa: BLE001
                self.get_logger().warning(f"preview encode failed: {exc}")

    # ------------------------------------------------------------------ control
    def _check_token(self, token: str | None) -> None:
        if not token or token != self.control_token:
            raise ApiError(403, "control_token", "missing or invalid control token")
        if time.monotonic() > self.control_expires:
            self.control_mode = "idle"
            self.control_token = None
            raise ApiError(409, "control_expired", "control session expired; re-acquire")

    def _require_session(self) -> None:
        if self.control_token is None:
            raise ApiError(401, "no_control_session", "acquire control first")

    def _emit(self, kind: str, **payload) -> dict:
        event = {
            "kind": kind,
            "session_id": self.session_id,
            "episode_id": self.episode_id,
            "time_ns": int(self.get_clock().now().nanoseconds),
            "sim_time_ns": int(self.get_clock().now().nanoseconds) if not self.read_only else None,
            "clock_domain": self.clock_domain,
            "wall_time": time.time(),
            **payload,
        }
        with self.lock:
            self.events.append(event)
        try:
            self.event_pub.publish(String(data=json.dumps(event, ensure_ascii=False)))
        except Exception:
            pass
        return event

    def _reject(self, reason: str, detail: str = "") -> None:
        self.last_reject = {"reason": reason, "detail": detail, "wall_time": time.time()}
        self.rejections.append(self.last_reject)
        self._emit("command_rejected", reason=reason, detail=detail)

    def _action_vector(self) -> list[float]:
        return [float(v) for v in self.last_action]

    def _sim_ns(self) -> int:
        return int(self.get_clock().now().nanoseconds)

    def _refuse_read_only(self, what: str) -> None:
        if self.read_only:
            raise ApiError(403, "read_only_mode", f"{what} is disabled in hardware_readonly mode")

    def acquire(self, client_id: str, ttl_s: float = 30.0) -> dict:
        self._refuse_read_only("control acquisition")
        now = time.monotonic()
        with self.lock:
            if self.estop_latched:
                raise ApiError(409, "estop_latched", "clear emergency stop first")
            if self.control_token and now < self.control_expires and self.control_client == client_id:
                return {"token": self.control_token, "mode": self.control_mode, "expires_in_s": round(self.control_expires - now, 2)}
            if self.control_token and now < self.control_expires and self.control_client != client_id:
                raise ApiError(409, "control_held", f"control held by {self.control_client}")
            self.control_token = secrets.token_urlsafe(24)
            self.control_client = client_id
            self.control_expires = now + max(5.0, min(float(ttl_s), 300.0))
            self.control_acquired_wall = now
            self.control_mode = "idle"
            self.base_valid_until = 0.0
            self.base_target = Twist()
            self.last_arm_targets = [float(self.joints.get(n, 0.0)) for n in ARM_JOINTS]
            self.last_gripper_target = float(self.joints.get("gripper_base_joint", 0.0))
            self.last_action = [0.0, 0.0, 0.0, *self.last_arm_targets, self.last_gripper_target]
            self.gate = CommandGate(self.profile, self.control_token)
            self.command_seq = -1
            self._emit("control_acquired", client_id=client_id, mode="idle")
            return {"token": self.control_token, "mode": self.control_mode, "expires_in_s": round(self.control_expires - now, 2)}

    def renew(self, token: str) -> dict:
        self._refuse_read_only("control renewal")
        with self.lock:
            self._check_token(token)
            self.control_expires = time.monotonic() + 30.0
            return {"expires_in_s": 30.0, "mode": self.control_mode}

    def release(self, token: str) -> dict:
        self._refuse_read_only("control release")
        with self.lock:
            self._check_token(token)
            self.base_target = Twist()
            self.base_valid_until = 0.0
            self._zero_base_now()
            self._emit("control_released", client_id=self.control_client)
            self.control_token = None
            self.control_client = None
            self.control_mode = "idle"
            self.gate = None
            return {"ok": True}

    def set_mode(self, token: str, mode: str) -> dict:
        self._refuse_read_only("mode switching")
        with self.lock:
            self._check_token(token)
            if mode == "leader_arm":
                raise ApiError(409, "leader_arm_unavailable", "leader arm driver and calibration are not configured")
            if mode not in ("idle", "keyboard", "scripted_expert"):
                raise ApiError(400, "bad_mode", mode)
            if self.expert_status.get("running") and mode != "scripted_expert":
                self.expert_cancel.set()
            self.control_mode = mode
            self.base_target = Twist()
            self.base_valid_until = 0.0
            self._zero_base_now()
            self._emit("mode_changed", mode=mode)
            return {"mode": mode}

    def base_command(self, token: str, payload: dict) -> dict:
        self._refuse_read_only("base commands")
        with self.lock:
            self._check_token(token)
            if self.control_mode not in ("keyboard", "scripted_expert"):
                raise ApiError(409, "mode", f"base command rejected in mode {self.control_mode}")
            source = payload.get("source")
            if source != self.control_mode:
                raise ApiError(409, "source_mode", f"source {source!r} does not match mode {self.control_mode}")
            try:
                cmd = self.gate.base(payload, self.control_mode)
            except ContractError as exc:
                self._reject(exc.reason, exc.detail)
                raise ApiError(400, exc.reason, exc.detail) from exc
            self.base_target = Twist()
            self.base_target.linear.x = cmd.vx
            self.base_target.linear.y = cmd.vy
            self.base_target.angular.z = cmd.wz
            self.base_valid_until = time.monotonic() + self.profile["limits"]["base_command_ttl_s"]
            self.last_base_wall = time.monotonic()
            self.last_action[0:3] = cmd.as_list()
            self.last_action_source = source
            self.last_action_seq = int(payload["seq"])
            self.command_seq = max(self.command_seq, int(payload["seq"]))
            self.last_action_sim_ns = self._sim_ns()
            event = self._emit(
                "command", component="base", source=source, seq=int(payload["seq"]),
                action=self._action_vector(), clipped=cmd.clipped,
            )
            return {"applied": cmd.as_list(), "clipped": cmd.clipped, "event": event}

    def arm_command(self, token: str, payload: dict) -> dict:
        self._refuse_read_only("arm commands")
        with self.lock:
            self._check_token(token)
            if self.control_mode != "scripted_expert":
                raise ApiError(409, "mode", "arm command requires scripted_expert mode")
            try:
                cmd = self.gate.arm(payload, "scripted_expert")
            except ContractError as exc:
                self._reject(exc.reason, exc.detail)
                raise ApiError(400, exc.reason, exc.detail) from exc
            self.last_arm_targets = list(cmd.positions)
            self.last_action[3:8] = cmd.positions
            self.last_action_source = "scripted_expert"
            self.last_action_seq = int(payload["seq"])
            self.command_seq = max(self.command_seq, int(payload["seq"]))
            self.last_action_sim_ns = self._sim_ns()
            self._send_arm(cmd.positions, cmd.duration_s)
            event = self._emit(
                "command", component="arm", source="scripted_expert", seq=int(payload["seq"]),
                action=self._action_vector(), target=cmd.positions, clipped=cmd.clipped,
            )
            return {"applied": cmd.positions, "clipped": cmd.clipped, "event": event}

    def gripper_command(self, token: str, payload: dict) -> dict:
        self._refuse_read_only("gripper commands")
        with self.lock:
            self._check_token(token)
            if self.control_mode != "scripted_expert":
                raise ApiError(409, "mode", "gripper command requires scripted_expert mode")
            try:
                cmd = self.gate.gripper(payload, "scripted_expert")
            except ContractError as exc:
                self._reject(exc.reason, exc.detail)
                raise ApiError(400, exc.reason, exc.detail) from exc
            master = cmd.positions[0]
            positions = self.gripper_positions(master)
            self.last_gripper_target = master
            self.last_action[8] = master
            self.last_action_source = "scripted_expert"
            self.last_action_seq = int(payload["seq"])
            self.command_seq = max(self.command_seq, int(payload["seq"]))
            self.last_action_sim_ns = self._sim_ns()
            self._send_gripper(master, cmd.duration_s)
            event = self._emit(
                "command", component="gripper", source="scripted_expert", seq=int(payload["seq"]),
                action=self._action_vector(), target=positions, clipped=cmd.clipped,
            )
            return {"applied": positions, "closed": cmd.clipped, "event": event}

    def gripper_positions(self, master: float) -> list[float]:
        values = [master] + [ratio * master for ratio in GRIPPER_RATIOS]
        return [max(lo, min(hi, v)) for v, (lo, hi) in zip(values, GRIPPER_LIMITS, strict=True)]

    def _send_arm(self, positions: list[float], duration: float):
        msg = JointTrajectory()
        msg.joint_names = ARM_JOINTS
        point = JointTrajectoryPoint()
        point.positions = [float(v) for v in positions]
        point.time_from_start = Duration(sec=int(duration), nanosec=int((duration % 1) * 1e9))
        msg.points = [point]
        self.arm_queue.put(msg)

    def _send_gripper(self, master: float, duration: float):
        msg = JointTrajectory()
        msg.joint_names = GRIPPER_JOINTS
        point = JointTrajectoryPoint()
        point.positions = self.gripper_positions(master)
        point.time_from_start = Duration(sec=int(duration), nanosec=int((duration % 1) * 1e9))
        msg.points = [point]
        self.gripper_queue.put(msg)

    def _zero_base_now(self):
        if self.base_pub is None:
            return
        self.base_pub.publish(Twist())
        self.last_zero = time.monotonic()

    def estop(self, token: str | None, reason: str = "operator") -> dict:
        self._refuse_read_only("emergency-stop output")
        with self.lock:
            if token:
                self._check_token(token)
            self.estop_latched = True
            self.heartbeat_pub.publish(Empty())
            self.control_mode = "idle"
            self.base_target = Twist()
            self.base_valid_until = 0.0
            self.expert_cancel.set()
            self.estop_pub.publish(Bool(data=True))
            self._zero_base_now()
            self._emit("estop", reason=reason)
            return {"estop": True, "reason": reason}

    def clear_estop(self, token: str) -> dict:
        self._refuse_read_only("emergency-stop output")
        with self.lock:
            self._check_token(token)
            self.heartbeat_pub.publish(Empty())
            self.estop_pub.publish(Bool(data=False))
            self.reset_pub.publish(String(data=f"{self.control_token}|{time.time()}"))
            self.estop_latched = False
            self._emit("estop_cleared")
            return {"estop": False}

    def _gz_zero(self):
        try:
            subprocess.run(
                ["gz", "topic", "-t", "/lelab/base/cmd_vel", "-m", "gz.msgs.Twist",
                 "-p", "linear: {x: 0, y: 0, z: 0} angular: {x: 0, y: 0, z: 0}"],
                capture_output=True, timeout=2.0,
            )
        except Exception as exc:  # noqa: BLE001
            self.get_logger().error(f"gz zero fallback failed: {exc}")

    def _control_tick(self):
        if self.base_pub is None:
            return
        now = time.monotonic()
        with self.lock:
            valid = self.control_token is not None and now <= self.control_expires and not self.estop_latched
            if valid and self.safety_wall and now - self.safety_wall > 1.0:
                self.base_target = Twist()
                self.base_valid_until = 0.0
                if now - getattr(self, "_last_safety_gz_zero", 0.0) > 1.0:
                    self._gz_zero()
                    self._last_safety_gz_zero = now
                    self._emit("safety_state_stale", age_s=round(now - self.safety_wall, 3))
            if not valid:
                if now - self.last_zero > 0.2:
                    self.base_pub.publish(Twist())
                    self.last_zero = now
                if self.control_token is not None and now > self.control_expires:
                    self.control_mode = "idle"
                    self.control_token = None
                    self.gate = None
                return
            if now - self.last_heartbeat > 0.1:
                self.heartbeat_pub.publish(Empty())
                self.last_heartbeat = now
            if now <= self.base_valid_until:
                self.base_pub.publish(self.base_target)
            elif now - self.last_zero > 0.2:
                self.base_pub.publish(Twist())
                self.last_zero = now
            while True:
                try:
                    msg = self.arm_queue.get_nowait()
                except queue.Empty:
                    break
                self.arm_pub.publish(msg)
            while True:
                try:
                    msg = self.gripper_queue.get_nowait()
                except queue.Empty:
                    break
                self.gripper_pub.publish(msg)
            if self.episode_id is not None:
                cube = _pose_xyz(self.cube)
                if cube:
                    self.episode_max_lift = max(self.episode_max_lift, cube[2])

    def _status_tick(self):
        with self.lock:
            if self.episode_id is not None and not self.read_only:
                cube = _pose_xyz(self.cube)
                if cube:
                    self.episode_max_lift = max(self.episode_max_lift, cube[2])
            ages = {topic: (None if wall is None else round(time.monotonic() - wall, 3)) for topic, wall in self.last_rx.items()}
        if self.sessions.session_id is not None:
            self.sessions.poll_health(ages)

    # ------------------------------------------------------------------ recording
    def _metadata(self) -> dict:
        profile_hash = hashlib.sha256(Path(self.profile_path).read_bytes()).hexdigest() if Path(self.profile_path).is_file() else None
        common = {
            "agent_version": self.agent_version,
            "ros_distro": os.environ.get("ROS_DISTRO"),
            "profile": self.profile.get("profile"),
            "profile_sha256": profile_hash,
            "runtime_mode": self.runtime.get("mode"),
            "adapter": self.runtime.get("adapter"),
            "source": self.runtime.get("data_source") or ("hardware" if self.read_only else "simulation"),
            "clock_domain": self.clock_domain if hasattr(self, "clock_domain") else self.runtime.get("clock_domain"),
            "actuator_outputs": self.actuator_outputs,
            "camera_profile": self.camera_profile,
            "camera_extrinsics": self.camera_extrinsics,
            "task_name": self.task_name,
            "training_export": bool(self.profile.get("training_export", {}).get("enabled")) and not self.read_only,
            "required_topics": list(self.profile.get("recording", {}).get("required_topics", [])),
            "camera_topic": self.rgb_topic,
            "camera_info_topic": self.camera_info_topic,
            "preview_backend": self.preview_backend,
            "record_joint_state_topic": self.record_joint_topic,
            "timestamp_sources": {"event": self.runtime.get("clock_domain", "ros_time"), "ttl": "monotonic_wall_clock"},
            "command_semantics": {
                "requested": "operator_or_expert_request",
                "sent": "gateway_forwarded_target",
                "acknowledged": "not_available",
                "measured_state": "unavailable_readonly" if self.read_only else "joint_states_and_model_odom",
            },
        }
        if self.read_only:
            return common
        model_hash = _sha256_file(self.model_sdf)
        try:
            gz_version = subprocess.check_output(["gz", "sim", "--versions"], text=True, timeout=5).strip().splitlines()[0]
        except Exception:
            gz_version = None
        common.update({
            "gazebo_version": gz_version,
            "model_sdf": str(self.model_sdf), "model_sdf_sha256": model_hash,
            "base_velocity_topic": self.base_topic, "wheel_odometry_topic": self.wheel_topic,
            "task_config": self.profile.get("task", {}).get(self.task_name, {}),
            "task_truth_topics": {
                "cube": "/lelab/task/cube_pose",
                "container": "/lelab/task/container_pose",
                "box": "/lelab/task/box_pose",
            },
            "record_task_truth_topics": {
                "cube": "/lelab/recording/cube_pose",
                "container": "/lelab/recording/container_pose",
                "box": "/lelab/recording/box_pose",
            },
        })
        return common

    def start_session(self, token: str, task: str) -> dict:
        if not self.read_only:
            self._check_token(token)
        if self.read_only and not self.rgb_topic:
            raise ApiError(409, "camera_topic_unconfigured", "hardware_readonly camera topic has not been verified")
        if self.sessions.active:
            raise ApiError(409, "session_active", self.sessions.session_id or "")
        missing = self.topic_health(missing_only=True)
        if missing:
            raise ApiError(409, "topics_missing", ", ".join(missing))
        sid = new_session_id()
        with self.lock:
            self.session_id = sid
            self.episode_id = None
            self.episode_phases = []
            self.episode_contacts = 0
        try:
            info = self.sessions.start_session(sid, task or self.profile.get("task", {}).get("language", "observe"), self._metadata(), self._sim_ns())
        except Exception:
            with self.lock:
                self.session_id = None
            raise
        self._emit("recording_session_started", session_id=sid, task=task, topics=info["topics"])
        return info

    def start_episode(self, token: str, source: str = "scripted_expert", task: str = "") -> dict:
        if not self.read_only:
            self._check_token(token)
        if self.read_only:
            source = "hardware_readonly"
        if self.sessions.current_episode is not None:
            raise ApiError(409, "episode_open", self.sessions.current_episode)
        if not self.sessions.active:
            raise ApiError(409, "no_session", "start a recording session first")
        if not self.read_only and self.control_mode not in ("idle", "keyboard", "scripted_expert"):
            raise ApiError(409, "mode", self.control_mode)
        initial = {
            "cube": None if self.read_only else _pose_xyz(self.cube),
            "container": None if self.read_only else _pose_xyz(self.container),
            "box": None if self.read_only else _pose_xyz(self.box),
            "task": self.task_name,
            "joints": {} if self.read_only else dict(self.joints),
            "time_ns": self._sim_ns(), "clock_domain": self.runtime.get("clock_domain"),
            "camera_profile": self.camera_profile,
        }
        eid = f"ep_{int(time.time())}_{secrets.token_hex(3)}"
        with self.lock:
            self.episode_id = eid
            self.episode_source = source
            self.episode_started_wall = time.time()
            self.episode_initial = initial
            self.episode_max_lift = float(initial["cube"][2]) if initial.get("cube") else 0.0
            self.episode_contacts = 0
            self.episode_phases = [{"phase": "record_start", "time_ns": self._sim_ns()}]
            if not self.read_only:
                self.last_arm_targets = [float(self.joints.get(n, 0.0)) for n in ARM_JOINTS]
                self.last_gripper_target = float(self.joints.get("gripper_base_joint", 0.0))
                self.last_action = [0.0, 0.0, 0.0, *self.last_arm_targets, self.last_gripper_target]
                self._emit("command", component="initial_hold", source="initial_hold", action=self._action_vector(), seq=self.command_seq)
        self.sessions.start_episode(eid, task or self.profile.get("task", {}).get("language", "observe"), source, self._sim_ns(), initial)
        self._emit("episode_started", episode_id=eid, source=source, initial=initial)
        return {"episode_id": eid, "source": source, "initial": initial}

    def _evaluate(self) -> dict:
        if self.read_only:
            return {
                "success": "unknown", "labels": "unlabeled", "task": self.task_name, "failure_reasons": [],
                "reason": "hardware_readonly_has_no_actuation_or_state_labels",
                "camera_profile": self.camera_profile,
                "privileged_truth_used_for_scripted_expert_only": False,
                "timestamp_wall": time.time(),
            }
        with self.lock:
            cube = _pose_xyz(self.cube)
            container = _pose_xyz(self.container)
            box = _pose_xyz(self.box)
            joints = dict(self.joints)
            initial = self.episode_initial
            max_lift = self.episode_max_lift
            contacts = self.episode_contacts
            phases = list(self.episode_phases)
        initial_z = initial.get("cube", [0, 0, 0])[2] if initial.get("cube") else None
        lift = max_lift - initial_z if initial_z is not None else None
        task_cfg = self.profile.get("task", {})
        threshold = float(task_cfg.get("success_min_lift_m", 0.03))
        reasons = []
        target = None
        target_source = None
        support = None
        inside = False
        retreated = None
        if self.task_name == "place_on_box":
            box_cfg = task_cfg.get("place_on_box", {})
            target = box
            target_source = "gazebo_model_truth" if box else None
            top_z = float(box_cfg.get("box_top_z_m", 0.06)) if box else None
            half = box_cfg.get("box_top_half_extent_m", [0.075, 0.075])
            on_top = bool(
                cube and box
                and abs(cube[0] - box[0]) <= float(half[0]) - 0.015
                and abs(cube[1] - box[1]) <= float(half[1]) - 0.015
            )
            support = bool(cube and top_z is not None and abs(cube[2] - (top_z + 0.02)) <= 0.02)
            home = (0.0, 0.83776, -2.03156, -1.1938, 0.0)
            retreated = bool(all(name in joints and abs(joints[name] - target_value) <= 0.15 for name, target_value in zip(ARM_JOINTS, home, strict=True)))
            inside = on_top and support
            if lift is None or lift < threshold:
                reasons.append("insufficient_lift")
            if contacts <= 0:
                reasons.append("no_gripper_contact")
            if not on_top:
                reasons.append("not_over_box_top")
            if not support:
                reasons.append("not_supported_by_box")
            if not retreated:
                reasons.append("gripper_not_retreated")
            success = bool(lift is not None and lift >= threshold and contacts > 0 and inside and retreated)
        else:
            target = container
            target_source = "gazebo_model_truth" if container else None
            if cube and container:
                inside = abs(cube[0] - container[0]) <= 0.075 and abs(cube[1] - container[1]) <= 0.075 and cube[2] <= 0.09
            success = bool(lift is not None and lift >= threshold and contacts > 0 and inside)
            if lift is None or lift < threshold:
                reasons.append("insufficient_lift")
            if contacts <= 0:
                reasons.append("no_gripper_contact")
            if not inside:
                reasons.append("not_inside_container")
        return {
            "success": success,
            "task": self.task_name,
            "failure_reasons": reasons,
            "cube_final": cube,
            "cube_initial": initial.get("cube"),
            "container": container,
            "box": box,
            "target_pose": target,
            "target_source": target_source,
            "target_confirmation": {
                "required": bool(task_cfg.get("place_on_box", {}).get("target_confirmation_required", False)) if self.task_name == "place_on_box" else False,
                "source": target_source,
                "frame": "world",
            },
            "max_lift_m": max_lift,
            "lift_above_initial_m": lift,
            "gripper_contact_events": contacts,
            "inside_container": inside,
            "box_top_support": support,
            "gripper_retreated": retreated,
            "lift_threshold_m": threshold,
            "evaluation_policy": {"threshold_key": "success_min_lift_m", "threshold_m": threshold, "task": self.task_name},
            "phases": phases,
            "privileged_truth_used_for_scripted_expert_only": True,
            "timestamp_wall": time.time(),
        }

    def end_episode(self, token: str, discarded: bool = False, reason: str = "") -> dict:
        if not self.read_only:
            self._check_token(token)
        if self.sessions.current_episode is None:
            raise ApiError(409, "no_episode", "no open episode")
        evaluation = self._evaluate()
        with self.lock:
            eid = self.episode_id
            self.episode_phases.append({"phase": "record_end", "sim_time_ns": self._sim_ns(), "success": evaluation["success"]})
        event = self.sessions.finish_episode(self._sim_ns(), evaluation, discarded=discarded, reason=reason)
        self._emit("episode_ended", episode_id=eid, discarded=discarded, reason=reason, evaluation=evaluation)
        with self.lock:
            self.episode_id = None
        return {"episode_id": eid, "evaluation": evaluation, "event": event}

    def stop_session(self, token: str, reason: str = "normal") -> dict:
        if not self.read_only:
            self._check_token(token)
        if self.sessions.current_episode is not None:
            self.end_episode(token, discarded=True, reason="session_stop")
        result = self.sessions.stop_session(reason=reason, time_ns=self._sim_ns())
        with self.lock:
            self.session_id = None
            self.episode_id = None
        self._emit("recording_session_stopped", **result)
        return result

    def topic_health(self, missing_only: bool = False) -> list[str] | dict:
        now = time.monotonic()
        required = list(self.profile.get("recording", {}).get("required_topics", []))
        ages = {}
        for topic in required:
            wall = self.last_rx.get(topic)
            ages[topic] = None if wall is None else round(now - wall, 3)
        missing = [t for t, age in ages.items() if age is None or age > 2.0]
        if missing_only:
            return missing
        return {"ages_s": ages, "missing": missing}

    # ------------------------------------------------------------------ expert
    def run_expert(self, token: str) -> dict:
        self._refuse_read_only("scripted expert")
        self._check_token(token)
        if self.control_mode != "scripted_expert":
            raise ApiError(409, "mode", "set scripted_expert mode first")
        if self.expert_status.get("running"):
            raise ApiError(409, "expert_running", "expert is already running")
        if not self.sessions.active:
            raise ApiError(409, "no_session", "start a recording session first")
        if self.sessions.current_episode is None:
            self.start_episode(token, source="scripted_expert")
        self.expert_cancel.clear()
        self.expert_status = {"running": True, "phase": "starting", "error": None, "started_wall": time.time(), "result": None}
        self.expert_thread = threading.Thread(target=self._expert_worker, args=(token,), daemon=True)
        self.expert_thread.start()
        return {"started": True, "episode_id": self.episode_id, "session_id": self.session_id}

    def stop_expert(self, token: str) -> dict:
        self._refuse_read_only("scripted expert")
        self._check_token(token)
        self.expert_cancel.set()
        return {"stopping": True}

    def _phase(self, name: str, **extra):
        entry = {"phase": name, "sim_time_ns": self._sim_ns(), "wall_time": time.time(), **extra}
        with self.lock:
            self.episode_phases.append(entry)
            self.expert_status["phase"] = name
        self._emit("expert_phase", phase=name, **extra)

    def _expert_worker(self, token: str):
        error = None
        try:
            self._expert_sequence(token)
            self.expert_status.update({"running": False, "phase": "done"})
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            self.get_logger().error(f"expert failed: {exc}")
            self.expert_status.update({"running": False, "phase": "error", "error": error})
            self._emit("expert_error", error=error)
        finally:
            if self.episode_id is not None:
                discarded = error is not None or self.expert_cancel.is_set()
                try:
                    result = self.end_episode(token, discarded=discarded, reason="expert_interrupted" if discarded else "expert_finished")
                    self.expert_status["result"] = result.get("evaluation")
                except Exception as exc:  # noqa: BLE001
                    self.get_logger().warning(f"could not close expert episode: {exc}")

    def _expert_sequence(self, token: str):
        self._phase("reset_task")
        self._reset_task()
        self._refresh_episode_initial()
        self._confirm_target()
        self._set_mode_expert()
        self._move_joints([0.0, 0.83776, -2.03156, -1.1938, 0.0], 2.0, wait=True)
        self._set_gripper(0.0, 1.0, wait=True)
        self._phase("pre_grasp")
        self._move_joints([-0.27, -2.09, -0.55, 1.01, 0.03], 2.5, wait=True)
        self._phase("grasp")
        self._move_joints([-0.27, -2.09, -0.55, 0.95, 0.02], 1.0, wait=True)
        self._set_gripper(-1.5, 1.0, wait=True)
        self._phase("lift")
        self._move_joints([-0.27, -1.45, -1.15, 1.30, -0.08], 2.5, wait=True)
        self._phase("carry_base")
        self._servo_cube_over_target()
        self._phase("release")
        self._set_gripper(0.0, 1.0, wait=True)
        time.sleep(1.0)
        with self.lock:
            settled = _pose_xyz(self.cube)
        self._phase("release_settled", cube_pose=settled)
        self._phase("retreat")
        self._move_joints([0.0, 0.83776, -2.03156, -1.1938, 0.0], 2.5, wait=True)
        self._phase("complete")

    def _gz_set_pose(self, request: str, label: str):
        proc = subprocess.run(
            ["gz", "service", "-s", "/world/pick_place/set_pose", "--reqtype", "gz.msgs.Pose",
             "--reptype", "gz.msgs.Boolean", "--timeout", "2000", "--req", request],
            capture_output=True, text=True, timeout=8,
        )
        if "data: true" not in proc.stdout:
            raise RuntimeError(f"{label} reset failed: {proc.stdout.strip()} {proc.stderr.strip()}")

    def _reset_task(self):
        self._refuse_read_only("task reset")
        self._set_base_expert(0.0, 0.0)
        self._gz_set_pose('name: "task_cube", position: {x: 0.26, y: -0.06, z: 0.021}', "cube")
        # The expert's base transfer leaves the chassis beside the tray; reset
        # the simulation chassis as well so episode planning starts from the
        # documented origin.  This is a privileged simulation reset, recorded
        # in the phases and never used as a policy observation.
        self._gz_set_pose(
            'name: "lelab_mobile", position: {x: 0, y: 0, z: 0.0005}, orientation: {w: 1}',
            "chassis",
        )
        time.sleep(0.8)

    def _set_mode_expert(self):
        with self.lock:
            if self.control_mode != "scripted_expert":
                self.control_mode = "scripted_expert"
                self._emit("mode_changed", mode="scripted_expert", actor="expert")

    def _gate_payload(self, kind: str, values: dict) -> dict:
        with self.lock:
            self.command_seq = max(self.command_seq, self.gate.last_seq) + 1
            return {
                "session_id": self.control_token,
                "seq": self.command_seq,
                "source": "scripted_expert",
                "mode": "scripted_expert",
                kind: values,
            }

    def _set_base_expert(self, vx: float, vy: float, wz: float = 0.0):
        payload = self._gate_payload("base", {"vx": vx, "vy": vy, "wz": wz})
        try:
            self.base_command(self.control_token, payload)
        except ApiError as exc:
            if exc.reason not in ("stale_seq",):
                raise

    def _move_joints(self, positions, duration, wait=True):
        payload = self._gate_payload("arm", {"positions": list(positions), "duration_s": duration})
        self.arm_command(self.control_token, payload)
        if wait:
            self._wait_joints(ARM_JOINTS, positions, 0.035, timeout=duration + 8.0)
            time.sleep(0.25)

    def _set_gripper(self, master, duration, wait=True):
        payload = self._gate_payload("gripper", {"position": master, "duration_s": duration})
        self.gripper_command(self.control_token, payload)
        if wait:
            deadline = time.monotonic() + duration + 5.0
            while time.monotonic() < deadline:
                if self.expert_cancel.is_set() or self.estop_latched:
                    raise RuntimeError("expert cancelled during gripper motion")
                with self.lock:
                    value = self.joints.get("gripper_base_joint")
                if value is not None and abs(value - master) <= 0.06:
                    break
                time.sleep(0.05)
            self._emit("gripper_settled", requested=master, measured=self.joints.get("gripper_base_joint"))
            time.sleep(0.2)

    def _wait_joints(self, names, targets, tol, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.expert_cancel.is_set():
                raise RuntimeError("expert cancelled")
            if self.estop_latched:
                raise RuntimeError("estop during expert")
            with self.lock:
                current = [self.joints.get(n) for n in names]
            if all(v is not None and abs(v - t) <= tol for v, t in zip(current, targets, strict=True)):
                return
            time.sleep(0.05)
        raise RuntimeError(f"joint motion timeout for {names}")

    def _refresh_episode_initial(self):
        # The expert episode opens before its privileged reset, so the initial
        # reference must be captured after reset and before any task motion.
        initial = {
            "cube": _pose_xyz(self.cube),
            "container": _pose_xyz(self.container),
            "box": _pose_xyz(self.box),
            "joints": dict(self.joints),
            "time_ns": self._sim_ns(),
            "clock_domain": self.runtime.get("clock_domain"),
            "camera_profile": self.camera_profile,
            "task": self.task_name,
        }
        with self.lock:
            self.episode_initial = initial
            self.episode_max_lift = float(initial["cube"][2]) if initial.get("cube") else 0.0
            episode = self.sessions.episodes.get(self.episode_id) if self.episode_id else None
            if episode is not None:
                episode.initial_cube = initial["cube"]
                episode.initial_container = initial["container"]
                episode.initial_joints = initial["joints"]
        self._emit("episode_initial_refreshed", initial=initial)

    def _confirm_target(self):
        with self.lock:
            target = _pose_xyz(self.box if self.task_name == "place_on_box" else self.container)
        if not target:
            raise RuntimeError(f"missing target truth for {self.task_name}")
        self._phase(
            "target_confirm", task=self.task_name, target_pose=target,
            target_source="gazebo_model_truth", target_frame="world",
            confirmation="scripted_expert_privileged",
        )

    def _servo_cube_over_target(self):
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            if self.expert_cancel.is_set() or self.estop_latched:
                raise RuntimeError("expert cancelled during base transfer")
            with self.lock:
                cube = _pose_xyz(self.cube)
                target = _pose_xyz(self.box if self.task_name == "place_on_box" else self.container)
            if not cube or not target:
                raise RuntimeError("missing task truth for base transfer")
            ex = target[0] - cube[0]
            ey = target[1] - cube[1]
            if abs(ex) < 0.008 and abs(ey) < 0.008:
                break
            vx = max(-0.06, min(0.06, 0.7 * ex))
            vy = max(-0.06, min(0.06, 0.7 * ey))
            self._set_base_expert(vx, vy)
            time.sleep(0.05)
        self._set_base_expert(0.0, 0.0)
        time.sleep(0.5)

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        now = time.monotonic()
        with self.lock:
            image_fps = 0.0
            if len(self.image_window) >= 2:
                span = self.image_window[-1] - self.image_window[0]
                image_fps = round((len(self.image_window) - 1) / span, 2) if span > 0 else 0.0
            ages = {topic: (None if wall is None else round(now - wall, 3)) for topic, wall in self.last_rx.items()}
            return {
                "ok": True,
                "agent_version": self.agent_version,
                "profile": self.profile["profile"],
                "task": self.profile["task"],
                "sim_time_ns": self._sim_ns() if not self.read_only else None,
                "time_ns": self._sim_ns(),
                "clock_domain": self.clock_domain,
                "read_only": self.read_only,
                "runtime_mode": self.runtime.get("mode"),
                "camera_profile": self.camera_profile,
                "camera_extrinsics": self.camera_extrinsics,
                "control": {
                    "active": self.control_token is not None and now <= self.control_expires and not self.estop_latched,
                    "client_id": self.control_client,
                    "mode": self.control_mode,
                    "expires_in_s": round(self.control_expires - now, 2) if self.control_token else 0.0,
                    "estop_latched": self.estop_latched,
                    "last_reject": self.last_reject,
                    "leader_arm_available": False,
                },
                "safety": self.safety if not self.read_only else {"read_only": True},
                "joints": dict(self.joints),
                "base": {
                    "vx": self.odom_vx, "vy": self.odom_vy, "wz": self.odom_wz,
                    "odom_pose": self.odom_pose,
                    "source": "model_odometry" if self.base_topic else "unavailable_readonly",
                    "age_s": ages.get(self.base_topic) if self.base_topic else None,
                    "wheel_odometry": self.wheel_odom,
                    "wheel_age_s": ages.get(self.wheel_topic) if self.wheel_topic else None,
                },
                "task_name": self.task_name,
                "task_state": {"cube": _pose_xyz(self.cube), "container": _pose_xyz(self.container), "box": _pose_xyz(self.box)} if not self.read_only else {"labels": "unknown", "success": "unknown"},
                "sensors": {
                    "rgb": {"topic": self.rgb_topic, "fps": image_fps,
                            "age_s": ages.get(self.rgb_topic) if self.rgb_topic else None,
                            "stamp_ns": self.image_stamp_ns, "preview_seq": self.preview_seq,
                            "preview_backend": self.preview_backend,
                            "stale": (ages.get(self.rgb_topic) is None or ages.get(self.rgb_topic) > 1.5) if self.rgb_topic else True},
                    "camera_info": {**self.camera_info_meta, "topic": self.camera_info_topic,
                                    "age_s": ages.get(self.camera_info_topic) if self.camera_info_topic else None},
                    "depth": {**self.depth_meta, "topic": self.depth_topic, "age_s": ages.get(self.depth_topic) if self.depth_topic else None},
                    "lidar": {**self.lidar_meta, "topic": self.lidar_topic, "age_s": ages.get(self.lidar_topic) if self.lidar_topic else None},
                    "imu": {**self.imu_meta, "topic": self.imu_topic, "age_s": ages.get(self.imu_topic) if self.imu_topic else None},
                    "contacts": {"count": self.contact_count, "recent": list(self.contact_pairs)[-5:]},
                },
                "topics": self.topic_health(),
                "recording": {
                    **self.sessions.status(),
                    "session_id": self.session_id,
                    "episode_id": self.episode_id,
                    "episode_source": self.episode_source,
                    "episode_max_lift_m": round(self.episode_max_lift, 4),
                    "episode_contacts": self.episode_contacts,
                },
                "expert": dict(self.expert_status),
                "last_action": {
                    "values": self._action_vector(),
                    "names": self.profile["features"]["action"]["names"],
                    "units": self.profile["features"]["action"]["units"],
                    "source": self.last_action_source,
                    "seq": self.last_action_seq,
                },
                "events": list(self.events)[-30:],
            }

    # ------------------------------------------------------------------ HTTP glue
    def latest_preview(self) -> tuple[bytes | None, int]:
        with self.lock:
            return self.preview_jpeg, self.preview_seq


class AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, agent: MobileAgent):
        super().__init__(address, AgentHandler)
        self.agent = agent


class AgentHandler(BaseHTTPRequestHandler):
    server: AgentHTTPServer

    def log_message(self, fmt, *args):
        return

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 2_000_000:
            raise ApiError(400, "body", "invalid body size")
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ApiError(400, "json", str(exc)) from exc

    def _token(self, body: dict | None = None) -> str:
        return self.headers.get("X-LeLab-Control-Token") or (body or {}).get("token") or ""

    def do_GET(self):
        try:
            if self.path == "/health":
                self._json(200, {"ok": True, "agent": self.server.agent.agent_version})
            elif self.path == "/status":
                self._json(200, self.server.agent.status())
            elif self.path == "/preview.mjpg":
                self._mjpeg()
            elif self.path == "/preview.jpg":
                jpeg, _ = self.server.agent.latest_preview()
                if jpeg is None:
                    self._json(503, {"error": "preview_unavailable"})
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(jpeg)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(jpeg)
            elif self.path == "/sessions":
                self._json(200, {"sessions": self.server.agent.sessions.list_sessions()})
            elif self.path.startswith("/sessions/"):
                parts = self.path.strip("/").split("/")
                sid = parts[1]
                if len(parts) == 3 and parts[2] == "manifest":
                    self._json(200, self.server.agent.sessions.manifest(sid))
                elif len(parts) == 3 and parts[2] == "archive":
                    self._archive(sid)
                else:
                    self._json(404, {"error": "not_found"})
            else:
                self._json(404, {"error": "not_found"})
        except ApiError as exc:
            self._json(exc.status, {"error": exc.reason, "detail": exc.detail})
        except FileNotFoundError:
            self._json(404, {"error": "session_not_found"})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": type(exc).__name__, "detail": str(exc)})

    def do_POST(self):
        try:
            body = self._body()
            token = self._token(body)
            agent = self.server.agent
            if self.path == "/control/acquire":
                out = agent.acquire(str(body.get("client_id", "browser")), float(body.get("ttl_s", 30)))
            elif self.path == "/control/renew":
                out = agent.renew(token)
            elif self.path == "/control/release":
                out = agent.release(token)
            elif self.path == "/control/mode":
                out = agent.set_mode(token, str(body.get("mode", "idle")))
            elif self.path == "/control/base":
                out = agent.base_command(token, body)
            elif self.path == "/control/arm":
                out = agent.arm_command(token, body)
            elif self.path == "/control/gripper":
                out = agent.gripper_command(token, body)
            elif self.path == "/control/estop":
                out = agent.estop(token, str(body.get("reason", "operator")))
            elif self.path == "/control/clear_estop":
                out = agent.clear_estop(token)
            elif self.path == "/recording/start_session":
                out = agent.start_session(token, str(body.get("task", "")))
            elif self.path == "/recording/start_episode":
                out = agent.start_episode(token, str(body.get("source", "scripted_expert")), str(body.get("task", "")))
            elif self.path == "/recording/end_episode":
                out = agent.end_episode(token, bool(body.get("discarded", False)), str(body.get("reason", "")))
            elif self.path == "/recording/discard_episode":
                out = agent.end_episode(token, True, str(body.get("reason", "discarded")))
            elif self.path == "/recording/stop_session":
                out = agent.stop_session(token, str(body.get("reason", "normal")))
            elif self.path == "/expert/run":
                out = agent.run_expert(token)
            elif self.path == "/expert/stop":
                out = agent.stop_expert(token)
            elif self.path == "/task/reset":
                agent._refuse_read_only("task reset")
                agent._check_token(token)
                agent._reset_task()
                out = {"reset": True}
            else:
                self._json(404, {"error": "not_found"})
                return
            self._json(200, out)
        except ApiError as exc:
            self._json(exc.status, {"error": exc.reason, "detail": exc.detail})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": type(exc).__name__, "detail": str(exc)})

    def _mjpeg(self):
        agent = self.server.agent
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = -1
        try:
            while True:
                jpeg, seq = agent.latest_preview()
                if jpeg is not None and seq != last:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n")
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    last = seq
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _archive(self, session_id: str):
        import tarfile
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.server.agent.sessions.archive(session_id, self.wfile)
        except BrokenPipeError:
            pass


def main(args=None):
    rclpy.init(args=args)
    agent = MobileAgent()
    host = str(agent.get_parameter("host").value)
    port = int(agent.get_parameter("port").value)
    server = AgentHTTPServer((host, port), agent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    agent.get_logger().info(f"HTTP agent listening on http://{host}:{port}")
    try:
        rclpy.spin(agent)
    finally:
        agent._preview_stop.set()
        try:
            if agent.sessions.active:
                agent.sessions.stop_session()
        except Exception:
            pass
        if agent.base_pub is not None:
            agent.base_pub.publish(Twist())
        server.shutdown()
        agent.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
