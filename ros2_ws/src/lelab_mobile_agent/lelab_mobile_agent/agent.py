"""Car-side HTTP agent for the LeLab mobile manipulator collection loop."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import queue
import secrets
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

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
try:  # both the car container's Humble and this host's Jazzy are expected to ship this; if a
    # build does not, the agent must still start - the missing name only costs a tidy log line.
    from rclpy.executors import ExternalShutdownException
    PLANNED_STOP_ERRORS = (KeyboardInterrupt, ExternalShutdownException)
except ImportError:
    PLANNED_STOP_ERRORS = (KeyboardInterrupt,)
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, Image as RosImage
from sensor_msgs.msg import JointState, LaserScan, Imu
from rosgraph_msgs.msg import Clock
from tf2_msgs.msg import TFMessage
from std_msgs.msg import Bool, Empty, String
from std_msgs.msg import Int32MultiArray
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

from .contract import (CommandGate, ContractError, evaluation_policy, exclusivity_report,
                       load_profile, manual_evaluation, manual_success_label,
                       new_session_id, state_vector, teleop_state_payload)
try:
    from ros_gz_interfaces.msg import Contacts
except ImportError:  # hardware-only image builds may omit Gazebo interfaces
    Contacts = None

from .follower_calibration_job import (ARM_JOINTS as CALIBRATION_ARM_JOINTS,
                                       GRIPPER_JOINT, CalibrationJobError,
                                       FollowerCalibrationJob)
from .recorder import SessionRecorder
from .recording_contract import command_event_fields, feedback_provenance_from_adapter
from .ros_servo_output import OUTPUT_TOPIC as SERVO_OUTPUT_TOPIC
from .ros_servo_output import RosServoOutputDriver
from .ros_servo_readback import (FEEDBACK_TOPIC as SERVO_FEEDBACK_TOPIC,
                                 RosServoFeedbackTopicDriver)
from .rosbridge_source import RosbridgeSensorSource
try:
    from ros_robot_controller_msgs.srv import GetBusServoState
except ImportError:  # Desktop/test images do not need the car controller messages.
    GetBusServoState = None

ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"]
FOLLOWER_SERVO_IDS = {"joint1": 1, "joint2": 2, "joint3": 3,
                      "joint4": 4, "joint5": 5, "r_joint": 10}
REMOTE_KIND_TYPES = {
    "rgb": RosImage, "depth": RosImage, "camera_info": CameraInfo,
    "imu": Imu, "joint_state": JointState, "odom": Odometry,
}
REMOTE_KIND_ROS_TYPES = {
    "rgb": "sensor_msgs/msg/Image", "depth": "sensor_msgs/msg/Image",
    "camera_info": "sensor_msgs/msg/CameraInfo", "imu": "sensor_msgs/msg/Imu",
    "joint_state": "sensor_msgs/msg/JointState", "odom": "nav_msgs/msg/Odometry",
}
GRIPPER_JOINTS = ["gripper_base_joint", "gripper_left_join", "gripper_right_joint", "left_jaw_joint", "right_jaw_joint"]
GRIPPER_RATIOS = [-1.5, -1.5, -0.0115995, 0.0115995]
GRIPPER_LIMITS = [(-1.638, 0.0), (-2.4569, 2.4569), (-2.4569, 2.4569), (0.0, 0.019), (-0.019, 0.0)]
ARM_LIMITS = {name: (-2.09, 2.09) for name in ARM_JOINTS}


@dataclass(frozen=True)
class PreviewSnapshot:
    jpeg: bytes | None
    frame_seq: int
    source_stamp_ns: int | None
    source_age_s: float | None
    source_topic: str | None
    backend: str

    def __iter__(self):
        """Keep compatibility with callers that historically unpacked (jpeg, seq)."""
        yield self.jpeg
        yield self.frame_seq


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


def _fill_header(msg, header: dict | None) -> None:
    header = header or {}
    stamp = header.get("stamp") or {}
    msg.header.stamp.sec = int(stamp.get("sec", 0) or 0)
    msg.header.stamp.nanosec = int(stamp.get("nanosec", stamp.get("nanosecond", 0)) or 0)
    msg.header.frame_id = str(header.get("frame_id", "") or "")


def _fixed(values, size: int) -> list[float]:
    data = [float(v) for v in (values or [])]
    return (data + [0.0] * size)[:size]


def _decode_remote_message(kind: str, values: dict):
    if kind == "rgb" or kind == "depth":
        msg = RosImage()
        _fill_header(msg, values.get("header"))
        msg.height = int(values.get("height", 0) or 0)
        msg.width = int(values.get("width", 0) or 0)
        msg.encoding = str(values.get("encoding", "rgb8") or "rgb8")
        msg.is_bigendian = int(values.get("is_bigendian", 0) or 0)
        msg.step = int(values.get("step", 0) or 0)
        data = values.get("data", "")
        msg.data = base64.b64decode(data) if isinstance(data, str) else bytes(data or b"")
        return msg
    if kind == "camera_info":
        msg = CameraInfo()
        _fill_header(msg, values.get("header"))
        msg.height = int(values.get("height", 0) or 0)
        msg.width = int(values.get("width", 0) or 0)
        msg.distortion_model = str(values.get("distortion_model", "") or "")
        msg.d = [float(v) for v in (values.get("d") or [])]
        msg.k = _fixed(values.get("k"), 9)
        msg.r = _fixed(values.get("r"), 9)
        msg.p = _fixed(values.get("p"), 12)
        msg.binning_x = int(values.get("binning_x", 0) or 0)
        msg.binning_y = int(values.get("binning_y", 0) or 0)
        return msg
    if kind == "imu":
        msg = Imu()
        _fill_header(msg, values.get("header"))
        orientation = values.get("orientation") or {}
        msg.orientation.w = float(orientation.get("w", 0.0))
        msg.orientation.x = float(orientation.get("x", 0.0))
        msg.orientation.y = float(orientation.get("y", 0.0))
        msg.orientation.z = float(orientation.get("z", 0.0))
        msg.orientation_covariance = _fixed(values.get("orientation_covariance"), 9)
        for attr, key in (("angular_velocity", "angular_velocity"), ("linear_acceleration", "linear_acceleration")):
            vector = values.get(key) or {}
            getattr(msg, attr).x = float(vector.get("x", 0.0))
            getattr(msg, attr).y = float(vector.get("y", 0.0))
            getattr(msg, attr).z = float(vector.get("z", 0.0))
            setattr(msg, f"{attr}_covariance", _fixed(values.get(f"{key}_covariance"), 9))
        return msg
    if kind == "joint_state":
        msg = JointState()
        _fill_header(msg, values.get("header"))
        msg.name = [str(name) for name in (values.get("name") or [])]
        msg.position = [float(v) for v in (values.get("position") or [])]
        msg.velocity = [float(v) for v in (values.get("velocity") or [])]
        msg.effort = [float(v) for v in (values.get("effort") or [])]
        return msg
    if kind == "odom":
        msg = Odometry()
        _fill_header(msg, values.get("header"))
        msg.child_frame_id = str(values.get("child_frame_id", "") or "")
        pose = (values.get("pose") or {}).get("pose") or {}
        position = pose.get("position") or {}
        msg.pose.pose.position.x = float(position.get("x", 0.0))
        msg.pose.pose.position.y = float(position.get("y", 0.0))
        msg.pose.pose.position.z = float(position.get("z", 0.0))
        orientation = pose.get("orientation") or {}
        msg.pose.pose.orientation.w = float(orientation.get("w", 0.0))
        msg.pose.pose.orientation.x = float(orientation.get("x", 0.0))
        msg.pose.pose.orientation.y = float(orientation.get("y", 0.0))
        msg.pose.pose.orientation.z = float(orientation.get("z", 0.0))
        msg.pose.covariance = _fixed((values.get("pose") or {}).get("covariance"), 36)
        twist = (values.get("twist") or {}).get("twist") or {}
        for attr in ("linear", "angular"):
            vector = twist.get(attr) or {}
            getattr(msg.twist.twist, attr).x = float(vector.get("x", 0.0))
            getattr(msg.twist.twist, attr).y = float(vector.get("y", 0.0))
            getattr(msg.twist.twist, attr).z = float(vector.get("z", 0.0))
        msg.twist.covariance = _fixed((values.get("twist") or {}).get("covariance"), 36)
        return msg
    raise ValueError(f"unsupported remote message kind {kind!r}")


def _read_host_serial() -> str | None:
    """Raspberry Pi host serial from /proc/cpuinfo; never presented as a board serial."""
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                if key.strip().lower() == "serial":
                    return value.strip() or None
    except OSError:
        return None
    return None


class _CalibrationCommandSink:
    """The only two actuator effects a calibration job may produce.

    ``publish_target`` is called for servo id 10 exclusively; ``set_torque`` carries
    the exact joint subset the job released.  Everything else in this phase is a
    read, which is what the sink test asserts.
    """

    def __init__(self, node: "MobileAgent") -> None:
        self.node = node

    def publish_target(self, servo_id: int, raw: int, duration_s: float) -> None:
        if servo_id != FOLLOWER_SERVO_IDS[GRIPPER_JOINT]:
            raise RuntimeError(f"calibration may only command id 10, not {servo_id}")
        self.node._calibration_publish(int(servo_id), int(raw), float(duration_s))

    def set_torque(self, servo_ids: list[int], enabled: bool) -> None:
        self.node._calibration_set_torque([int(item) for item in servo_ids], bool(enabled))


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
        # Local socket of the standalone ROS control process.  Empty keeps this agent
        # the only emitter, exactly as today.
        self.declare_parameter("licence_socket", "")
        self.declare_parameter("control_status_path", "")
        self.licence_socket = str(self.get_parameter("licence_socket").value or "")
        self.control_status_path = str(self.get_parameter("control_status_path").value or "")
        profile_path = self.get_parameter("profile_path").value
        if not profile_path:
            profile_path = str(Path(get_package_share_directory("lelab_mobile_agent")) / "config" / "runtime-profile-v1.json")
        self.profile = load_profile(profile_path)
        # Hybrid profiles use one ROS-free, adapter-free gateway. It stays at zero
        # output even when the explicit simulation profile publishes simulated data.
        self.hybrid_gateway = None
        self.hybrid_control_token: str | None = None
        self.hybrid_no_output = False
        profile_name = self.profile.get("profile")
        profile_mode = self.profile.get("runtime", {}).get("mode")
        if profile_name in {
                "hardware_hybrid_manual_v1", "hybrid_manual_sim_v1",
                "hardware_hybrid_arm_trial_v1", "hardware_hybrid_arm2_trial_v1",
                "hardware_hybrid_arm3_trial_v1", "hardware_hybrid_arm4_trial_v1",
                "hardware_hybrid_arm5_trial_v1", "hardware_hybrid_homing_v1",
                "hardware_hybrid_follow_v1",
                # The offline Humble profile is the same gateway with a simulated board
                # behind it and the frame transport in front; it is recognised here so
                # the offline loop exercises the production routes, not a fixture.
                "hardware_hybrid_manual_v1_offline6d",
                } or profile_mode in {
                    "hardware_hybrid_manual", "hybrid_manual_sim",
                    "hardware_hybrid_follow",
                    "simulated_hybrid_manual", "offline_humble_simulated_board",
                }:
            self.hybrid_gateway = self._new_hybrid_gateway()
            self.hybrid_no_output = profile_name == "hardware_hybrid_manual_v1" or \
                profile_mode == "hardware_hybrid_manual"
        self.profile_path = profile_path
        self.runtime = self.profile.get("runtime", {})
        self.read_only = self.runtime.get("mode") == "hardware_readonly"
        self.actuator_outputs = bool(self.runtime.get("actuator_outputs", not self.read_only)) and not self.hybrid_no_output
        # Which transport is allowed to emit for this profile.  "legacy" is every
        # existing path unchanged; "frame_stream" hands emission to the standalone
        # control process, and then each action entry point in this agent must refuse
        # rather than silently produce a second writer.
        self.output_transport = str(self.runtime.get("output_transport") or "legacy")
        self.frame_outputs_enabled = False
        self.licence_bridge = None
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
        self.teleop_cfg = self.profile.get("teleop", {}) or {}
        self.follower_feedback_cfg = self.profile.get("follower_feedback", {}) or {}
        self.leader_arm_enabled = bool(self.teleop_cfg.get("leader_arm_mode"))
        self.follower_feedback = None
        self.ros_servo_readback = None
        self.ros_servo_output = None
        self.follower_calibration_pub = None
        self.follower_torque_pub = None
        self.follower_calibration: FollowerCalibrationJob | None = None
        self._calibration_worker_stop = threading.Event()
        self._calibration_worker: threading.Thread | None = None
        self._calibration_last_phase = "idle"
        self.teleop_state_pub = None
        self.last_teleop = None
        self.chassis_locked = False
        self.state_topic = self.runtime.get("joint_state_topic")
        self.last_exclusivity: dict[str, Any] | None = None
        self.base_topic = self.runtime.get("base_velocity_topic")
        self.wheel_topic = self.runtime.get("wheel_odom_topic")
        self.record_joint_topic = self.runtime.get("record_joint_state_topic")
        self.record_joint_hz = float(self.runtime.get("joint_record_hz") or 0)
        self.car_root = Path(self.get_parameter("car_root").value).expanduser()
        self.car_root.mkdir(parents=True, exist_ok=True)
        self.model_sdf = Path(self.get_parameter("model_sdf").value or (self.car_root / "runtime" / "lelab_mobile.sdf"))
        self.lock = threading.RLock()
        self.sessions = SessionRecorder(self.car_root, self.profile, self.get_logger())
        # A container that was SIGKILLed leaves its `ros2 bag record` running with the session still
        # marked open, so the first thing this process does is close what its predecessor left.
        self.recovered_sessions = self.sessions.recover_interrupted_sessions()
        self.agent_version = "0.1.0"
        # Identifies this agent process to the control process.  A restart makes a new one,
        # which is what lets the mirrored recording context start a fresh sequence.
        self.boot_nonce = f"{os.getpid()}-{int(time.time() * 1000)}"

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
        self.last_clock_ns: int | None = None
        self.last_clock_mono: float | None = None
        self.camera_info_meta: dict[str, Any] = {}
        self.cube: PoseStamped | None = None
        self.container: PoseStamped | None = None
        self.box: PoseStamped | None = None
        self.task_wall = 0.0
        self.last_rx: dict[str, float] = {}
        # Topics this node publishes itself. The recorder requires them to be live before a
        # session can start, and nothing will ever arrive on them from outside.
        self.last_tx: dict[str, float] = {}
        self.image: RosImage | None = None
        self.image_stamp_ns = 0
        self.image_wall = 0.0
        self.image_count = 0
        self.image_window: deque[float] = deque(maxlen=30)
        self.preview_jpeg: bytes | None = None
        self.preview_seq = 0
        self.preview_wall = 0.0
        self.preview_source_wall = 0.0
        self.preview_stamp_ns: int | None = None
        self.preview_frames: deque[tuple[int, int | None, float, bytes]] = deque(maxlen=32)
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
        self.control_epoch: int | None = None
        self.control_mode = "idle"
        self.control_expires = 0.0
        self.control_acquired_wall = 0.0
        self.gate: CommandGate | None = None
        self.command_seq = -1
        self.base_target = Twist()
        self.base_valid_until = 0.0
        self.last_base_wall = 0.0
        self.estop_latched = False
        # Who held the control lease when the stop came, and when. Once the lease dies there
        # is no token any more, and that previous owner still has to be able to lift the
        # stop; nobody else may. Clearing grants no motion either way.
        self.estop_owner_client: str | None = None
        self.estop_requested_at: float | None = None
        self.last_reject: dict[str, Any] | None = None
        self.rejections: deque[dict[str, Any]] = deque(maxlen=100)
        self.session_id: str | None = None
        self.recording_session_epoch: int | None = None
        self.recording_clock_epoch: str | None = None
        self.requested_session_id: str | None = None
        self.requested_calibration_bundle_hash: str | None = None
        self.requested_export_schema: str | None = None
        self.requested_runtime_mode: str | None = None
        self.requested_profile: str | None = None
        self.requested_simulation: bool | dict[str, Any] | None = None
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
        # The control tick is the lease reaper: it drops the token, the owner identity and
        # any queued targets the moment a control lease expires. It therefore has to run in
        # every profile - frame mode creates none of the actuator publishers below, and with
        # the timer registered only inside that block an expired frame lease kept advertising
        # its owner and its licence mirror indefinitely.
        self.create_timer(0.02, self._control_tick)
        if self.actuator_outputs:
            self.chassis_locked = bool(self.teleop_cfg.get("chassis_locked", False))
            if not self.chassis_locked:
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
        if self.follower_feedback_cfg.get("enabled"):
            from .follower_feedback import FeedbackStore

            self.follower_feedback = FeedbackStore(
                expected_joints=list(self.follower_feedback_cfg.get("joints", [])),
                stale_after_s=float(self.follower_feedback_cfg.get("stale_after_s", 0.5)),
            )
            self.create_subscription(
                JointState,
                self.follower_feedback_cfg.get("joint_states_topic", "/controller_manager/joint_states"),
                self._on_follower_feedback, 10,
            )
            readback_topic = str(self.follower_feedback_cfg.get("servo_readback_topic") or "")
            if (self.hybrid_gateway is not None and GetBusServoState is not None
                    and (not self._is_simulated_profile() or readback_topic)):
                # A declared-simulation profile normally has no ROS driver at all.  The
                # offline rig is different: the board is simulated but the readback is a
                # real topic, so the motion gate can still be fed by an actual sample.
                # The data identity stays declared-simulated: reading that topic does not
                # promote it to a hardware measurement.
                self.ros_servo_readback = RosServoFeedbackTopicDriver(
                    self,
                    Int32MultiArray,
                    calibration_hash=None,
                    topic=str(self.follower_feedback_cfg.get(
                        "servo_readback_topic", SERVO_FEEDBACK_TOPIC)),
                    stale_after_s=float(self.follower_feedback_cfg.get("stale_after_s", 0.5)),
                    max_scan_span_s=float(self.follower_feedback_cfg.get("max_scan_span_s", 0.5)),
                    scan_period_s=float(self.follower_feedback_cfg.get("scan_period_s", 0.2)),
                )
                if self.output_transport == "frame_stream":
                    # Reading the board is not writing it.  With the frame transport the
                    # standalone control process owns the servo publisher, so binding one
                    # here would be the second writer this mode exists to rule out; the
                    # readback above still gives the agent its own trusted-feedback view.
                    self.get_logger().info(
                        "frame transport: readback bound, no agent-side servo output driver")
                else:
                    from ros_robot_controller_msgs.msg import ServoPosition, ServosPosition

                    limits = (self.profile.get("limits", {}) or {}).get(
                        "joint_positions_rad", {}) or {}
                    output_limits = {
                        name: (float(bounds[0]), float(bounds[1]))
                        for name, bounds in limits.items()
                        if isinstance(bounds, list) and len(bounds) == 2
                    }
                    output_publisher = self.create_publisher(
                        ServosPosition, SERVO_OUTPUT_TOPIC, 1)
                    self.ros_servo_output = RosServoOutputDriver(
                        self,
                        output_publisher,
                        servos_position_type=ServosPosition,
                        servo_position_type=ServoPosition,
                        readback=self.ros_servo_readback,
                        limits_rad=output_limits,
                        max_step_rad_s=float(self.teleop_cfg.get("max_step_rad_s", 3.0)),
                        max_silence_s=float(self.teleop_cfg.get("output_watchdog_s", 1.0)),
                        duration_s=float(self.teleop_cfg.get("output_duration_s", 0.15)),
                    )
                    self.hybrid_gateway.driver = self.ros_servo_output
                self.follower_calibration = FollowerCalibrationJob(
                    sink=_CalibrationCommandSink(self),
                    read_scan=self._calibration_scan_by_name,
                    readback_snapshot=self.ros_servo_readback.read_snapshot,
                    pause_readback=self.ros_servo_readback.pause_and_drain,
                    resume_readback=self.ros_servo_readback.resume,
                    device_identity=self._calibration_device_identity,
                    stale_after_s=float(self.follower_feedback_cfg.get("stale_after_s", 0.5)),
                    agent_version=self.agent_version,
                    profile=str(self.profile.get("profile") or "unknown"),
                )
                self._calibration_worker = threading.Thread(
                    target=self._calibration_worker_loop,
                    name="lelab-follower-calibration", daemon=True)
                self._calibration_worker.start()
        if self.teleop_cfg.get("enabled"):
            self.teleop_state_pub = self.create_publisher(
                String, self.teleop_cfg.get("state_topic", "/lelab/recording/teleop"), 10)
            self.create_timer(1.0 / max(1.0, float(self.teleop_cfg.get("publish_hz", 10.0))),
                              self._publish_teleop_state)
        if not self.read_only:
            self.create_subscription(PoseStamped, "/lelab/task/cube_pose", lambda m: self._on_task("cube", m), 10)
            self.create_subscription(PoseStamped, "/lelab/task/container_pose", lambda m: self._on_task("container", m), 10)
            self.create_subscription(PoseStamped, "/lelab/task/box_pose", lambda m: self._on_task("box", m), 10)
        self.create_timer(0.1, self._status_tick)
        self.remote_source = None
        self.remote_pubs: dict[str, tuple[Any, str]] = {}
        self.remote_queue: queue.Queue = queue.Queue(maxsize=1)
        self.remote_dropped = 0
        self._start_remote_source()
        self._preview_stop = threading.Event()
        self._preview_thread = threading.Thread(target=self._preview_loop, daemon=True)
        self._preview_thread.start()
        self.create_timer(0.05, self._hybrid_gateway_tick)
        self.get_logger().info(f"agent ready; profile={self.profile['profile']} car_root={self.car_root}")

    # ------------------------------------------------------------------ inputs
    def _hybrid_gateway_tick(self):
        gateway = self.hybrid_gateway
        if gateway is not None:
            gateway.tick()

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

    # ------------------------------------------------------------------ follower feedback / teleop
    def _on_follower_feedback(self, msg: JointState):
        if self.follower_feedback is None:
            return
        topic = str(self.follower_feedback_cfg.get("joint_states_topic",
                                                   "/controller_manager/joint_states"))
        self.follower_feedback.ingest_joint_states(
            list(msg.name), list(msg.position), time.monotonic(), topic)
        # Recording refuses to start while a required topic looks stale, and this is the only
        # subscription that reads the vendor feedback stream when joint_record_hz is 0. Without
        # this the teleop profile can never open a session: the data is fresh, the health table
        # is empty.
        self.last_rx[topic] = time.monotonic()

    def _feedback_gate(self) -> tuple[bool, dict]:
        readback = getattr(self, "ros_servo_readback", None)
        if readback is not None:
            snapshot = readback.read_snapshot()
            return bool(snapshot.get("available")), snapshot
        if self.follower_feedback is None:
            return False, {"available": False, "reason": "follower_feedback_not_configured"}
        ok, reason = self.follower_feedback.gate()
        return ok, self.follower_feedback.snapshot()

    # -------------------------------------------------------- follower calibration
    def _calibration_read_scan(self, servo_ids: list[int], timeout_s: float = 1.0) -> dict[int, int]:
        """One bounded read of the given servo ids through the unique /dev/rrc owner."""
        driver = getattr(self, "ros_servo_readback", None)
        if driver is None:
            raise ApiError(409, "follower_readback_unavailable", "readback driver is not bound")
        deadline = time.monotonic() + timeout_s
        snapshot = driver.read_snapshot()
        while not snapshot.get("available") and time.monotonic() < deadline:
            time.sleep(0.02)
            snapshot = driver.read_snapshot()
        if not snapshot.get("available"):
            raise ApiError(503, "follower_read_timeout", f"servos {servo_ids}")
        samples = snapshot.get("joint_samples") or {}
        positions: dict[int, int] = {}
        names_by_id = {value: key for key, value in FOLLOWER_SERVO_IDS.items()}
        for servo_id in servo_ids:
            name = names_by_id.get(int(servo_id))
            sample = samples.get(name) if name else None
            if not isinstance(sample, dict) or sample.get("valid") is not True:
                raise ApiError(502, "follower_read_mismatch", f"servo {servo_id}")
            raw = sample.get("raw_position")
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ApiError(502, "follower_read_mismatch", f"servo {servo_id}")
            if not -1000 <= raw <= 1000:
                raise ApiError(502, "follower_read_out_of_range", str(raw))
            positions[int(servo_id)] = int(raw)
        return positions

    def _calibration_scan_by_name(self) -> dict[str, int]:
        names = list(FOLLOWER_SERVO_IDS)
        positions = self._calibration_read_scan([FOLLOWER_SERVO_IDS[name] for name in names])
        return {name: positions[FOLLOWER_SERVO_IDS[name]] for name in names}

    def _calibration_publish(self, servo_id: int, position: int, duration_s: float) -> None:
        self._refuse_frame_stream_action('follower calibration target')
        """Publish exactly one ServoPosition: the calibration job can only reach id 10."""
        from ros_robot_controller_msgs.msg import ServoPosition, ServosPosition

        if self.follower_calibration_pub is None:
            self.follower_calibration_pub = self.create_publisher(
                ServosPosition, SERVO_OUTPUT_TOPIC, 1)
        message = ServosPosition()
        message.duration = float(duration_s)
        item = ServoPosition()
        item.id = int(servo_id)
        item.position = int(position)
        message.position.append(item)
        self.follower_calibration_pub.publish(message)

    def _check_calibration_publishers(self) -> None:
        if self.follower_calibration_pub is None:
            from ros_robot_controller_msgs.msg import ServosPosition

            self.follower_calibration_pub = self.create_publisher(
                ServosPosition, SERVO_OUTPUT_TOPIC, 1)
        names = [info.node_name for info in self.get_publishers_info_by_topic(
            SERVO_OUTPUT_TOPIC)]
        unexpected = sorted(set(names) - {self.get_name()})
        if unexpected:
            raise ApiError(409, "servo_controller_not_exclusive",
                           f"unexpected publishers: {unexpected}")

    def _calibration_set_torque(self, servo_ids: list[int], enabled: bool) -> None:
        self._refuse_frame_stream_action('follower calibration torque')
        from ros_robot_controller_msgs.msg import BusServoState, SetBusServoState

        if self.follower_torque_pub is None:
            self.follower_torque_pub = self.create_publisher(
                SetBusServoState, "/ros_robot_controller/bus_servo/set_state", 10)
        message = SetBusServoState()
        for servo_id in servo_ids:
            state = BusServoState()
            state.present_id = [int(servo_id), int(servo_id)]
            state.enable_torque = [1, 1 if enabled else 0]
            message.state.append(state)
        self.follower_torque_pub.publish(message)

    def _calibration_device_identity(self) -> dict[str, Any]:
        """Server-side device facts. Nothing here is ever taken from a browser body."""
        serial = os.environ.get("LELAB_BOARD_SERIAL", "").strip() or None
        return {
            "kind": "landerpi_bus_servo_follower",
            "control_board": "landerpi_mentorpi",
            "controller_owner": "ros_robot_controller",
            "joint_order": list(FOLLOWER_SERVO_IDS),
            "servo_ids": dict(FOLLOWER_SERVO_IDS),
            "board_serial": serial,
            "board_serial_source": "environment" if serial else "unavailable",
            "host_serial": _read_host_serial(),
            "agent_version": self.agent_version,
            "profile": str(self.profile.get("profile") or "unknown"),
            "feedback_topic": SERVO_FEEDBACK_TOPIC,
            "command_topic": SERVO_OUTPUT_TOPIC,
        }

    def _calibration_job(self) -> FollowerCalibrationJob:
        job = self.follower_calibration
        if job is None:
            raise ApiError(409, "follower_readback_unavailable",
                           "six-servo readback driver is not bound")
        return job

    @staticmethod
    def _calibration_call(handler):
        try:
            return handler()
        except CalibrationJobError as exc:
            raise ApiError(exc.status, exc.reason, exc.detail) from exc

    def _calibration_worker_loop(self) -> None:
        while not self._calibration_worker_stop.wait(0.02):
            job = self.follower_calibration
            if job is None:
                continue
            try:
                job.tick()
            except Exception as exc:  # noqa: BLE001
                self.get_logger().warning(
                    f"follower calibration worker: {type(exc).__name__}: {exc}")
                continue
            if job.phase != self._calibration_last_phase:
                self._calibration_last_phase = job.phase
                self.get_logger().info(
                    f"follower calibration phase -> {job.phase}"
                    f" reason={job.stop_reason} faults={job.faults}")
                self._emit("follower_calibration_phase", phase=job.phase,
                           stop_reason=job.stop_reason, faults=list(job.faults))

    def follower_calibration_start(self, body: dict | None = None) -> dict:
        body = dict(body or {})
        if self.control_token is not None:
            raise ApiError(409, "control_lease_active", "release control before follower calibration")
        if self.sessions.active:
            raise ApiError(409, "recording_active", "stop recording before follower calibration")
        driver = getattr(self, "ros_servo_readback", None)
        if driver is None:
            raise ApiError(409, "follower_readback_unavailable", "readback driver is not bound")
        job = self._calibration_job()
        self._check_calibration_publishers()
        driver.resume()
        result = self._calibration_call(lambda: job.start(body))
        self._calibration_last_phase = job.phase
        self._emit("follower_calibration_started", job_id=result["job_id"],
                   generation=result["generation"],
                   safe_window_raw=result["safe_window_raw"],
                   baseline_raw=result["baseline_raw"])
        return result

    def follower_calibration_status(self, body: dict) -> dict:
        return self._calibration_call(lambda: self._calibration_job().status(body))

    def follower_calibration_manual_ranges(self, body: dict) -> dict:
        job = self._calibration_job()
        result = self._calibration_call(lambda: job.manual_ranges(body))
        self._emit("follower_calibration_manual_ranges",
                   ranges=result["manual_arm_ranges"])
        return result

    def follower_calibration_torque(self, body: dict) -> dict:
        job = self._calibration_job()
        result = self._calibration_call(lambda: job.torque(body))
        self._emit("follower_calibration_torque", enabled=result["enabled"],
                   joints=result["joints"], released_joints=result["released_joints"])
        return result

    def follower_calibration_gripper_target(self, body: dict) -> dict:
        job = self._calibration_job()
        if job.phase != FollowerCalibrationJob.DRAG:
            # Only the first target of a drag session needs the exclusivity check;
            # repeating a ROS graph query at 10 Hz would add latency for nothing.
            self._check_calibration_publishers()
        return self._calibration_call(lambda: job.gripper_target(body))

    def follower_calibration_gripper_mark(self, body: dict) -> dict:
        job = self._calibration_job()
        result = self._calibration_call(lambda: job.gripper_mark(body))
        self._emit("follower_calibration_gripper_mark", end=result["marked"]["end"],
                   raw=result["marked"]["raw"], round=result["marked"]["round"])
        return result

    def follower_calibration_gripper_stop(self, body: dict) -> dict:
        job = self._calibration_job()
        result = self._calibration_call(lambda: job.gripper_stop(body))
        summary = result.get("gripper") or {}
        self._emit("follower_calibration_gripper_stopped",
                   commands_sent=summary.get("commands_sent"),
                   rounds=summary.get("rounds_completed"))
        return result

    def follower_calibration_stop(self, body: dict) -> dict:
        job = self._calibration_job()
        result = self._calibration_call(lambda: job.stop(body))
        self._emit("follower_calibration_stopped", phase=result["phase"],
                   stop_reason=result["stop_reason"],
                   released_joints=result["torque"]["released_joints"])
        return result

    def shutdown_calibration(self) -> None:
        self._calibration_worker_stop.set()
        worker = self._calibration_worker
        if worker is not None:
            worker.join(timeout=2.0)
        job = self.follower_calibration
        if job is not None:
            try:
                job.shutdown()
            except Exception as exc:  # noqa: BLE001
                self.get_logger().warning(f"calibration shutdown: {exc}")

    def _guard_topics(self) -> list[str]:
        configured = self.teleop_cfg.get("exclusive_guard_topics")
        topics = list(configured or ["/lelab/arm/trajectory_request", "/lelab/gripper/trajectory_request"])
        if self.base_topic:
            topics.append(self.base_topic)
        return list(dict.fromkeys(topics))

    def _publisher_names(self, topic: str) -> list[str] | None:
        """Bare node names publishing this topic; None when the graph cannot answer."""
        try:
            return [info.node_name for info in self.get_publishers_info_by_topic(topic)]
        except Exception as exc:  # noqa: BLE001
            self.get_logger().warning(f"publisher enumeration failed for {topic}: {exc}")
            return None

    def _exclusivity_report(self) -> dict:
        return exclusivity_report({topic: self._publisher_names(topic) for topic in self._guard_topics()},
                                  self.get_name(), self.teleop_cfg.get("exclusive_exempt_nodes") or [])

    def _publish_teleop_state(self):
        if self.teleop_state_pub is None:
            return
        with self.lock:
            ok, feedback = self._feedback_gate()
            payload = teleop_state_payload(
                now_ns=time.time_ns(), sim_ns=self._sim_ns(), clock_domain=self.clock_domain,
                control_mode=self.control_mode, leader_arm_enabled=self.leader_arm_enabled,
                last_teleop=self.last_teleop, last_arm_targets=self.last_arm_targets,
                last_gripper_target=self.last_gripper_target,
                last_action_seq=self.last_action_seq, feedback_ok=ok, feedback=feedback,
                applied=self.applied, last_reject=self.last_reject)
        self.teleop_state_pub.publish(String(data=json.dumps(payload)))
        self.last_tx[str(self.teleop_cfg.get("state_topic", "/lelab/recording/teleop"))] = time.monotonic()

    # ------------------------------------------------------------------ remote sensor source
    def _start_remote_source(self):
        cfg = self.profile.get("remote_source") or {}
        if not cfg.get("topics") or cfg.get("enabled") is False:
            return
        if not self.read_only:
            raise RuntimeError("remote_source is only valid in hardware_readonly mode")
        if cfg.get("backend") != "rosbridge":
            raise RuntimeError(f"unsupported remote_source backend {cfg.get('backend')!r}")
        url = cfg.get("url") or os.environ.get("LELAB_ROSBRIDGE_URL", "")
        if not url:
            raise RuntimeError("remote_source needs url or LELAB_ROSBRIDGE_URL")
        self.remote_pubs = {}
        subs = []
        for index, item in enumerate(cfg["topics"]):
            kind = item["kind"]
            topic = item["topic"]
            if kind not in REMOTE_KIND_TYPES:
                raise RuntimeError(f"unknown remote_source kind {kind!r}")
            self.remote_pubs[topic] = (
                self.create_publisher(REMOTE_KIND_TYPES[kind], topic, qos_profile_sensor_data), kind,
            )
            subs.append((f"remote_{index}", topic, item.get("type") or REMOTE_KIND_ROS_TYPES[kind]))
        self.remote_queue: queue.Queue = queue.Queue(maxsize=int(cfg.get("queue_size", 200)))
        self.remote_source = RosbridgeSensorSource(
            url, subs, self._on_remote_message,
            reconnect_s=float(cfg.get("reconnect_s", 2.0)),
            read_timeout=float(cfg.get("read_timeout", 10.0)),
        )
        self.remote_source.start()
        self.create_timer(0.02, self._drain_remote)
        self.get_logger().info(f"rosbridge sensor source → {url} ({len(subs)} observation topics)")

    def _on_remote_message(self, topic: str, values: dict):
        entry = self.remote_pubs.get(topic)
        if entry is None:
            return
        try:
            msg = _decode_remote_message(entry[1], values)
        except Exception:  # noqa: BLE001
            with self.lock:
                self.remote_dropped += 1
            return
        try:
            self.remote_queue.put_nowait((topic, msg))
        except queue.Full:
            with self.lock:
                self.remote_dropped += 1

    def _drain_remote(self):
        while True:
            try:
                topic, msg = self.remote_queue.get_nowait()
            except queue.Empty:
                return
            pub, kind = self.remote_pubs[topic]
            pub.publish(msg)
            if kind == "rgb":
                self._on_image(msg)
            elif kind == "camera_info":
                self._on_camera_info(msg)
            elif kind == "imu":
                self._on_imu(msg)
            elif kind == "joint_state":
                self._on_joints(msg)
            elif kind == "odom":
                self._on_odom(msg)

    def _remote_state(self) -> dict | None:
        if self.remote_source is None:
            return None
        state = self.remote_source.state()
        state["queue_size"] = self.remote_queue.qsize()
        state["local_dropped"] = self.remote_dropped
        state["republished_to"] = sorted(self.remote_pubs)
        return state

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
                stamp_ns = self.image_stamp_ns
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
                    self.preview_source_wall = stamp
                    self.preview_stamp_ns = stamp_ns
                    self.preview_frames.append(
                        (self.preview_seq, stamp_ns, stamp, bytes(jpeg)))
            except Exception as exc:  # noqa: BLE001
                self.get_logger().warning(f"preview encode failed: {exc}")

    # ------------------------------------------------------------------ control
    def _expire_control_lease(self, reason: str) -> None:
        """The one teardown for a control lease that is no longer live.

        The reaper tick and the lazy token check both land here, because a path that only
        drops the token leaves the owner name and the licence mirror behind: /status then
        reads "nobody is in control" next to "safety-repro owns the arm", and the emitter
        keeps a permission nobody can still exercise. Re-entrant on purpose.
        """
        self.control_mode = "idle"
        self.control_token = None
        self.control_epoch = None
        self.control_client = None
        self.gate = None
        self.frame_outputs_enabled = False
        self._reset_hybrid_control_binding(str(reason))

    def _check_token(self, token: str | None) -> None:
        if not token or token != self.control_token:
            raise ApiError(403, "control_token", "missing or invalid control token")
        if time.monotonic() > self.control_expires:
            self._expire_control_lease("control_expired")
            raise ApiError(409, "control_expired", "control session expired; re-acquire")

    def _require_session(self) -> None:
        if self.control_token is None:
            raise ApiError(401, "no_control_session", "acquire control first")

    @staticmethod
    def _validated_session_epoch(value: Any, *, field: str = "session_epoch") -> int | None:
        if value is None:
            return None
        if type(value) is not int or not 0 <= value < 2 ** 53:
            raise ApiError(400, "invalid_session_epoch",
                           f"{field} must be an integer in [0, 2**53)")
        return value

    def _new_hybrid_gateway(self, *, session_id: str = "unbound",
                            session_epoch: int = 0,
                            calibration_bundle_hash: str = "unbound"):
        from .hybrid_gateway import HybridGateway

        runtime = self.profile.get("runtime", {}) or {}
        max_ttl_ms = runtime.get("hybrid_max_ttl_ms", 200)
        if isinstance(max_ttl_ms, bool) or not isinstance(max_ttl_ms, int) or max_ttl_ms <= 0:
            max_ttl_ms = 200
        return HybridGateway(
            self.profile,
            session_id=session_id,
            session_epoch=session_epoch,
            bundle_hash=calibration_bundle_hash,
            max_ttl_ms=max_ttl_ms,
            driver=getattr(self, "ros_servo_readback", None),
        )

    def _reset_hybrid_control_binding(self, reason: str) -> None:
        self.hybrid_control_token = None
        # Replacing or expiring a lease is not a pause: the frame-mode output flag must
        # drop here, or the next acquire would inherit an armed mirror.
        self.frame_outputs_enabled = False
        gateway = getattr(self, "hybrid_gateway", None)
        if gateway is not None:
            try:
                gateway.stop(str(reason))
            except Exception:  # noqa: BLE001 - control lease teardown still proceeds
                pass
            self.hybrid_gateway = self._new_hybrid_gateway()
        self._bridge_wake(str(reason))

    def _require_hybrid_gateway(self, token: str):
        self._check_token(token)
        if self.hybrid_gateway is None:
            raise ApiError(409, "hybrid_gateway_unavailable",
                           "the loaded profile has no hybrid gateway")
        if self.hybrid_control_token is None:
            raise ApiError(409, "hybrid_epoch_not_started",
                           "begin a hybrid epoch for this control lease first")
        if self.hybrid_control_token != token:
            raise ApiError(409, "hybrid_control_binding_mismatch",
                           "hybrid epoch belongs to another control lease")
        return self.hybrid_gateway

    @staticmethod
    def _hybrid_event_target(channel: str, target: dict[str, Any]) -> list[float]:
        if channel == "base":
            return [float(target["vx_m_s"]), float(target["vy_m_s"]),
                    float(target["wz_rad_s"])]
        if channel == "arm":
            return [float(value) for value in target["positions_rad"]]
        return [float(target["position_rad"])]

    def _hybrid_status(self) -> dict[str, Any] | None:
        if self.hybrid_gateway is None:
            return None
        status = self.hybrid_gateway.status()
        return {
            **status,
            "simulated": self._is_simulated_profile(),
            "data_source": self.runtime.get("data_source") or "hardware",
            "hardware_available": self.runtime.get("hardware_available", "unknown"),
            "driver_bound": self.hybrid_gateway.driver is not None,
            "driver_sent": "unavailable",
            "actuator_outputs": False,
            "controller_acknowledged": "unavailable",
            "execution_observed": "unavailable",
        }

    def _is_simulated_profile(self) -> bool:
        profile = getattr(self, "profile", {}) or {}
        runtime = profile.get("runtime", {}) or {}
        simulation = profile.get("simulation", {}) or {}
        return (runtime.get("simulated") is True
                or runtime.get("simulation_only") is True
                or simulation.get("simulated") is True)

    def _runtime_feedback_provenance(self) -> dict[str, Any]:
        """Read explicit offline simulation provenance without promoting it to hardware."""
        runtime = self.profile.get("runtime", {}) or {}
        declared = runtime.get("feedback_provenance")
        unavailable = {
            "trusted": False,
            "available": False,
            "simulated": True,
            "hardware_measurement": False,
            "reason": "simulation_feedback_provenance_unavailable",
            "joint_source_kind": "unavailable",
            "base_source_kind": "unavailable",
            "calibration_hash": None,
            "base_estimate_has_covariance": False,
            "max_state_age_s": None,
            "joint_topic": runtime.get("joint_state_topic"),
            "base_topic": runtime.get("base_velocity_topic"),
            "scan_span_ms": None,
            "fact_source": "runtime.feedback_provenance",
            "execution_observed": "unavailable",
        }
        if not isinstance(declared, dict):
            return unavailable
        joint_kind = declared.get("joint_source_kind")
        if joint_kind not in {"synthetic", "model_placeholder"}:
            unavailable["reason"] = "simulation_claims_hardware_joint_source"
            unavailable["declared_joint_source_kind"] = joint_kind
            return unavailable
        base_kind = declared.get("base_source_kind")
        if not isinstance(base_kind, str) or not base_kind:
            unavailable["reason"] = "simulation_base_source_unavailable"
            return unavailable
        calibration_hash = declared.get("calibration_bundle_hash")
        if not isinstance(calibration_hash, str) or not calibration_hash:
            unavailable["reason"] = "simulation_calibration_identity_unavailable"
            return unavailable
        covariance = declared.get("base_estimate_has_covariance") is True
        stale_after_s = declared.get("max_state_age_s")
        if (isinstance(stale_after_s, bool)
                or not isinstance(stale_after_s, (int, float))
                or not 0 < float(stale_after_s) <= 0.5):
            unavailable["reason"] = "simulation_state_age_invalid"
            return unavailable
        scan_span_ms = declared.get("scan_span_ms")
        if (isinstance(scan_span_ms, bool)
                or not isinstance(scan_span_ms, (int, float))
                or float(scan_span_ms) < 0):
            unavailable["reason"] = "simulation_scan_span_invalid"
            return unavailable
        return {
            **unavailable,
            "available": True,
            "reason": "explicit_synthetic_simulation_feedback",
            "joint_source_kind": joint_kind,
            "base_source_kind": base_kind,
            "calibration_hash": calibration_hash,
            "base_estimate_has_covariance": covariance,
            "max_state_age_s": float(stale_after_s),
            "joint_topic": declared.get("joint_topic") or runtime.get("joint_state_topic"),
            "base_topic": declared.get("base_topic") or runtime.get("base_velocity_topic"),
            "scan_span_ms": float(scan_span_ms),
            "hardware_measurement": False,
        }

    def _adapter_calibration_hash(self) -> str | None:
        """Return only a calibration identity actually bound by the hybrid adapter."""
        if self._is_simulated_profile():
            provenance = self._runtime_feedback_provenance()
            value = provenance.get("calibration_hash")
            return value if isinstance(value, str) and value else None
        gateway = getattr(self, "hybrid_gateway", None)
        if gateway is None:
            return None
        value = gateway.status().get("calibration_bundle_hash")
        return value if isinstance(value, str) and value and value != "unbound" else None

    def _check_recording_identity(self, *, session_id: str | None = None,
                                  session_epoch: int | None = None,
                                  calibration_bundle_hash: str | None = None,
                                  bind_epoch: bool = False) -> None:
        session_epoch = self._validated_session_epoch(session_epoch)
        if session_id is not None and (
                not isinstance(session_id, str) or not session_id):
            raise ApiError(400, "invalid_session_id",
                           "session_id must be a non-empty string when provided")
        if calibration_bundle_hash is not None and (
                not isinstance(calibration_bundle_hash, str)
                or not calibration_bundle_hash
                or calibration_bundle_hash == "unbound"):
            raise ApiError(400, "invalid_calibration",
                           "calibration_bundle_hash must be a bound non-empty string")
        with self.lock:
            car_session_id = getattr(self, "session_id", None)
            requested_session_id = getattr(self, "requested_session_id", None)
            bound_epoch = getattr(self, "recording_session_epoch", None)
            if session_id is not None and session_id not in {
                    value for value in (car_session_id, requested_session_id) if value}:
                raise ApiError(409, "session_mismatch",
                               "request belongs to another recording session")
            if session_epoch is not None and bound_epoch is not None \
                    and session_epoch != bound_epoch:
                raise ApiError(409, "session_epoch_mismatch",
                               f"active recording epoch is {bound_epoch}")
            requested_calibration = getattr(
                self, "requested_calibration_bundle_hash", None)
            if calibration_bundle_hash is not None and requested_calibration is not None \
                    and calibration_bundle_hash != requested_calibration:
                raise ApiError(409, "calibration_mismatch",
                               "request calibration differs from the active session")
            if bind_epoch and session_epoch is not None and bound_epoch is None:
                self.recording_session_epoch = session_epoch

    def _feedback_provenance(self) -> dict[str, Any]:
        """Derive provenance from runtime simulation config or live hardware adapter facts."""
        if self._is_simulated_profile():
            return self._runtime_feedback_provenance()
        gateway = getattr(self, "hybrid_gateway", None)
        driver = getattr(gateway, "driver", None)
        if driver is None:
            result = feedback_provenance_from_adapter(None, None)
            result["reason"] = "no_trusted_t1_driver_bound"
            result["simulated"] = False
            result["hardware_measurement"] = False
            result["data_source"] = getattr(
                self, "runtime", {}).get("data_source") or "hardware"
            return result
        try:
            capabilities = driver.capabilities()
            snapshot = driver.read_snapshot()
            result = feedback_provenance_from_adapter(capabilities, snapshot)
            result["simulated"] = False
            result["hardware_measurement"] = bool(result.get("trusted"))
            result["data_source"] = getattr(
                self, "runtime", {}).get("data_source") or "hardware"
            return result
        except Exception as exc:  # noqa: BLE001 - absence of facts is reported, not repaired
            result = feedback_provenance_from_adapter({}, {})
            result["reason"] = f"adapter_fact_read_failed:{type(exc).__name__}"
            result["adapter_error"] = str(exc)
            result["simulated"] = False
            result["hardware_measurement"] = False
            result["data_source"] = getattr(
                self, "runtime", {}).get("data_source") or "hardware"
            return result

    def _command_audit_fields(self, channel: str,
                              payload: dict[str, Any]) -> dict[str, Any]:
        """Validate command identity and produce one uniform three-channel event shape."""
        return command_event_fields(
            channel=channel, payload=payload, profile=self.profile,
            control_epoch=getattr(self, "control_epoch", None),
            calibration_bundle_hash=self._adapter_calibration_hash(),
        )

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

    def acquire(self, client_id: str, ttl_s: float = 30.0, *,
                recovery: bool = False) -> dict:
        self._refuse_read_only("control acquisition")
        now = time.monotonic()
        with self.lock:
            if self.estop_latched and not recovery:
                raise ApiError(409, "estop_latched", "clear emergency stop first")
            if self.estop_latched and recovery:
                recovery_owner = self.estop_owner_client or self._estop_owner_from_events()
                if not recovery_owner or client_id != recovery_owner:
                    raise ApiError(403, "estop_recovery_owner", "recovery requires the original operator")
            if self.control_token and now < self.control_expires and self.control_client == client_id:
                return {"token": self.control_token, "session_epoch": self.control_epoch,
                        "mode": self.control_mode,
                        "expires_in_s": round(self.control_expires - now, 2)}
            if self.control_token and now < self.control_expires and self.control_client != client_id:
                raise ApiError(409, "control_held", f"control held by {self.control_client}")
            self._reset_hybrid_control_binding("control_lease_replaced")
            self.control_token = secrets.token_urlsafe(24)
            self.control_client = client_id
            self.control_epoch = secrets.randbelow(2 ** 53)
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
            self._emit("control_acquired", client_id=client_id, mode="idle",
                       recovery=bool(recovery and self.estop_latched))
            return {"token": self.control_token, "session_epoch": self.control_epoch,
                    "mode": self.control_mode,
                    "expires_in_s": round(self.control_expires - now, 2),
                    "recovery": bool(recovery and self.estop_latched)}

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
            self._reset_hybrid_control_binding("control_released")
            self.frame_outputs_enabled = False
            self.control_token = None
            self.control_client = None
            self.control_epoch = None
            self.control_mode = "idle"
            self.gate = None
            self._bridge_wake("control_released")
            return {"ok": True}

    # -- licence bridge ---------------------------------------------------
    def _hybrid_identity(self) -> tuple:
        """(session_id, session_epoch, bundle) from the gateway, or the bare lease."""
        gateway = self.hybrid_gateway
        if gateway is not None:
            try:
                status = gateway.status()
                return (status.get("session_id"), status.get("session_epoch"),
                        status.get("calibration_bundle_hash") or "unbound",
                        bool(status.get("outputs_enabled")), dict(status.get("permits") or {}))
            except Exception:  # noqa: BLE001 - a torn-down gateway means "no authority"
                pass
        return (None, self.control_epoch, "unbound", False, {})

    def _licence_facts(self) -> dict[str, Any]:
        """The mirror is derived only from state this agent already owns.

        Nothing here decides permission on its own: the lease, the epoch binding, the
        channel permits, the frame-mode output flag and the latched stop all come from
        the same objects the legacy routes consult.
        """
        session_id, session_epoch, bundle, gateway_outputs, permits = self._hybrid_identity()
        return {
            "session_id": session_id,
            "session_epoch": session_epoch,
            "calibration_bundle_hash": bundle,
            "lease_token": self.control_token,
            "permits": permits,
            "outputs_enabled": (bool(gateway_outputs) or bool(self.frame_outputs_enabled))
            and not self.estop_latched,
            "lease_expires_in_s": max(0.0, float(self.control_expires or 0.0) - time.monotonic()),
            "estop_latched": bool(self.estop_latched),
        }

    def _recording_facts(self) -> dict[str, Any]:
        """What the recorder believes it is recording right now, for the control process.

        The control process writes one row per emitted frame, and a row has to say which
        episode it belongs to.  That fact is the agent's, never the control process'.  It is
        sampled without waiting: if the agent lock is busy, this returns nothing and the
        context simply expires at the mirror, so a frame is recorded as uncontexted rather
        than stamped with an episode that may already have closed.
        """
        lock = getattr(self, "lock", None)
        if lock is None or not lock.acquire(blocking=False):
            return {}
        try:
            session_id = self.session_id
            if session_id is None:
                return {}
            episode_id = self.episode_id
            phase = None
            sessions = getattr(self, "sessions", None)
            if sessions is not None and episode_id is not None:
                episode = sessions.episodes.get(episode_id)
                if episode is not None and episode.phases:
                    phase = episode.phases[-1].get("phase")
            return {
                "session_id": session_id,
                "session_epoch": getattr(self, "recording_session_epoch", None),
                "episode_id": episode_id,
                "phase": phase,
                "clock_epoch": getattr(self, "recording_clock_epoch", None),
                "calibration_bundle_hash": getattr(self, "requested_calibration_bundle_hash",
                                                   None) or "unbound",
            }
        except Exception:  # noqa: BLE001 - no facts beats a half-read session
            return {}
        finally:
            lock.release()

    def _bridge_wake(self, reason: str = "") -> None:
        bridge = getattr(self, "licence_bridge", None)
        if bridge is not None:
            bridge.wake()

    def frame_stream_active(self, session_id=None, session_epoch=None) -> bool:
        bridge = getattr(self, "licence_bridge", None)
        if bridge is None or self.output_transport != "frame_stream":
            return False
        if session_id is None or session_epoch is None:
            live_id, live_epoch, _, _, _ = self._hybrid_identity()
            session_id = session_id if session_id is not None else live_id
            session_epoch = session_epoch if session_epoch is not None else live_epoch
        return bridge.frame_transport_active(session_id, session_epoch)

    def _refuse_frame_stream_action(self, route: str, token: str | None = None) -> None:
        """A second writer is the failure mode this guard exists to prevent.

        The refusal follows the profile's declared transport, not whether a frame
        client happens to be connected right now: a transient disconnect must not
        silently hand the servos back to the legacy routes while the control process
        still holds a live licence mirror.  The agent restart / re-engage paths are the
        only way back to a legacy emitter, and that is a deliberate operator action.

        The stop paths are deliberately never guarded: an emergency stop or a pause
        must keep working while a frame transport owns the output.
        """
        if self.output_transport != "frame_stream":
            return
        session_id, session_epoch, _, _, _ = self._hybrid_identity()
        raise ApiError(409, "frame_transport_active",
                       f"{route} cannot emit: the frame stream owns the output for "
                       f"session {session_id} epoch {session_epoch}")

    def start_licence_bridge(self, socket_path: str) -> bool:
        if not socket_path or self.output_transport != "frame_stream":
            return False
        from .agent_licence_bridge import AgentLicenceBridge

        root = Path(socket_path).parent
        root.mkdir(parents=True, exist_ok=True)
        bridge = AgentLicenceBridge(socket_path=Path(socket_path), authority=self._licence_facts,
                                    generation_path=root / "car_licence_generation.txt",
                                    status_path=self.control_status_path or None,
                                    recording_authority=self._recording_facts,
                                    agent_boot=self.boot_nonce,
                                    logger=self.get_logger())
        bridge.start()
        self.licence_bridge = bridge
        self.get_logger().info(f"licence bridge publishing to {socket_path}")
        return True

    def set_mode(self, token: str, mode: str) -> dict:
        self._refuse_read_only("mode switching")
        with self.lock:
            self._check_token(token)
            if mode == "leader_arm" and not self.leader_arm_enabled:
                raise ApiError(409, "leader_arm_unavailable",
                               "profile does not enable the leader_arm mode")
            if mode not in ("idle", "keyboard", "scripted_expert", "leader_arm"):
                raise ApiError(400, "bad_mode", mode)
            if self.expert_status.get("running") and mode != "scripted_expert":
                self.expert_cancel.set()
            if mode == "leader_arm" and self.follower_feedback is None:
                raise ApiError(409, "follower_feedback_missing",
                               "leader_arm mode requires configured follower feedback")
            if mode == "leader_arm":
                # 六: a vendor joystick or app drives the same servos without passing through this
                # gateway, so the lease alone never proves exclusivity. Refuse unless the graph says
                # nobody else publishes the commanded trajectories (unreadable counts as unsafe).
                report = self._exclusivity_report()
                self.last_exclusivity = report
                if not report["clean"]:
                    self._reject(report["reason"], json.dumps({"foreign": report["foreign"],
                                                               "unknown": report["unknown"]},
                                                              ensure_ascii=False))
                    raise ApiError(409, report["reason"],
                                   f"exclusive arm control unavailable: {report['foreign'] or report['unknown']}")
            if mode != self.control_mode:
                self.last_teleop = None
            self.control_mode = mode
            self.base_target = Twist()
            self.base_valid_until = 0.0
            self._zero_base_now()
            self._emit("mode_changed", mode=mode)
            return {"mode": mode}

    def base_command(self, token: str, payload: dict) -> dict:
        self._refuse_read_only("base commands")
        if self.chassis_locked:
            raise ApiError(403, "chassis_locked",
                           "phase 1 keeps the mobile base locked; no base publisher exists")
        with self.lock:
            self._check_token(token)
            if self.control_mode not in ("keyboard", "scripted_expert"):
                raise ApiError(409, "mode", f"base command rejected in mode {self.control_mode}")
            source = payload.get("source")
            if source != self.control_mode:
                raise ApiError(409, "source_mode", f"source {source!r} does not match mode {self.control_mode}")
            try:
                audit = self._command_audit_fields("base", payload)
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
                "command", **audit,
                action=self._action_vector(), target=cmd.as_list(), clipped=cmd.clipped,
            )
            return {"applied": cmd.as_list(), "clipped": cmd.clipped, "event": event}

    def arm_command(self, token: str, payload: dict) -> dict:
        self._refuse_frame_stream_action('/control/arm')
        self._refuse_read_only("arm commands")
        with self.lock:
            self._check_token(token)
            mode = self.control_mode if self.control_mode in ("scripted_expert", "leader_arm") else None
            if mode is None:
                raise ApiError(409, "mode", "arm command requires scripted_expert or leader_arm mode")
            if mode == "leader_arm":
                ok, feedback = self._feedback_gate()
                if not ok:
                    self._reject("follower_feedback_stale", feedback.get("reason", ""))
                    raise ApiError(409, "follower_feedback_stale", str(feedback.get("reason")))
            try:
                audit = self._command_audit_fields("arm", payload)
                cmd = self.gate.arm(payload, mode)
            except ContractError as exc:
                self._reject(exc.reason, exc.detail)
                raise ApiError(400, exc.reason, exc.detail) from exc
            self.last_arm_targets = list(cmd.positions)
            self.last_action[3:8] = cmd.positions
            self.last_action_source = mode
            self.last_action_seq = int(payload["seq"])
            self.command_seq = max(self.command_seq, int(payload["seq"]))
            self.last_action_sim_ns = self._sim_ns()
            self._note_teleop(payload)
            self._send_arm(cmd.positions, cmd.duration_s)
            event = self._emit(
                "command", **audit,
                action=self._action_vector(), target=cmd.positions, clipped=cmd.clipped,
                pre_clip=payload.get("pre_clip_rad"), leader_rad=payload.get("leader_rad"),
                calibration_version=payload.get("calibration_version"),
                clutch=payload.get("clutch"),
            )
            return {"applied": cmd.positions, "clipped": cmd.clipped, "mode": mode, "event": event}

    def gripper_command(self, token: str, payload: dict) -> dict:
        self._refuse_frame_stream_action('/control/gripper')
        self._refuse_read_only("gripper commands")
        with self.lock:
            self._check_token(token)
            mode = self.control_mode if self.control_mode in ("scripted_expert", "leader_arm") else None
            if mode is None:
                raise ApiError(409, "mode", "gripper command requires scripted_expert or leader_arm mode")
            if mode == "leader_arm":
                ok, feedback = self._feedback_gate()
                if not ok:
                    self._reject("follower_feedback_stale", feedback.get("reason", ""))
                    raise ApiError(409, "follower_feedback_stale", str(feedback.get("reason")))
            try:
                audit = self._command_audit_fields("gripper", payload)
                cmd = self.gate.gripper(payload, mode)
            except ContractError as exc:
                self._reject(exc.reason, exc.detail)
                raise ApiError(400, exc.reason, exc.detail) from exc
            master = cmd.positions[0]
            positions = self.gripper_positions(master)
            self.last_gripper_target = master
            self.last_action[8] = master
            self.last_action_source = mode
            self.last_action_seq = int(payload["seq"])
            self.command_seq = max(self.command_seq, int(payload["seq"]))
            self.last_action_sim_ns = self._sim_ns()
            self._note_teleop(payload)
            self._send_gripper(master, cmd.duration_s)
            event = self._emit(
                "command", **audit,
                action=self._action_vector(), target=positions, clipped=cmd.clipped,
                pre_clip=payload.get("pre_clip_rad"), leader_gripper_percent=payload.get("gripper_percent"),
                calibration_version=payload.get("calibration_version"),
                clutch=payload.get("clutch"),
            )
            return {"applied": positions, "closed": cmd.clipped, "mode": mode, "event": event}

    def _note_teleop(self, payload: dict) -> None:
        """Keep the newest leader reading and pre-clamp demand for the recorded teleop state."""
        if self.control_mode != "leader_arm":
            return
        self.last_teleop = {
            "leader_rad": payload.get("leader_rad"),
            "gripper_percent": payload.get("gripper_percent"),
            "pre_clip_rad": payload.get("pre_clip_rad"),
            "calibration_version": payload.get("calibration_version"),
            "clutch": payload.get("clutch"),
            "wall": time.monotonic(),
        }

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

    def _announce(self, publisher, message) -> bool:
        """Publish a safety announcement when this profile has the publisher at all.

        A profile with `actuator_outputs: false` has no actuator announcement topics, and
        the stop decision must not depend on whether one of them exists: the latch and
        the licence revocation happen either way, and the response says what was skipped.
        """
        if publisher is None:
            return False
        publisher.publish(message)
        return True

    def _estop_owner_from_events(self) -> str | None:
        """Recover only the owner preceding the first uncleared stop.

        Legacy agents overwrote this identity on a repeated stop after expiry.
        The agent's own bounded history may repair that metadata; a caller cannot
        nominate an owner, and a release or truncated history fails closed.
        """
        events = list(self.events)
        start = 0
        for index, event in enumerate(events):
            if event.get("kind") == "estop_cleared":
                start = index + 1
        first_stop = next((index for index in range(start, len(events))
                           if events[index].get("kind") == "estop"), None)
        if first_stop is None:
            return None
        for event in reversed(events[start:first_stop]):
            if event.get("kind") in {"control_released", "control_expired"}:
                return None
            if event.get("kind") == "control_acquired":
                owner = event.get("client_id")
                return owner if isinstance(owner, str) and owner else None
        return None

    def estop(self, token: str | None, reason: str = "operator") -> dict:
        self._refuse_read_only("emergency-stop output")
        with self.lock:
            if token:
                self._check_token(token)
            repeated = self.estop_latched
            if not repeated:
                self.estop_owner_client = self.control_client
                self.estop_requested_at = time.monotonic()
            self.estop_latched = True
            # The permission drops with the stop itself; waking the bridge here is an
            # event set, not socket work, so no I/O happens under the agent lock.
            self.frame_outputs_enabled = False
            self._bridge_wake("estop")
            announced = [self._announce(self.heartbeat_pub, Empty())]
            self.control_mode = "idle"
            self.base_target = Twist()
            self.base_valid_until = 0.0
            self.expert_cancel.set()
            announced.append(self._announce(self.estop_pub, Bool(data=True)))
            self._zero_base_now()
            self._emit("estop", reason=reason, repeated=repeated,
                       announcements="all" if all(announced) else "partial_no_actuator_topics")
            return {"estop": True, "reason": reason,
                    "announcements": "all" if all(announced) else "partial_no_actuator_topics"}

    def clear_estop(self, token: str | None, client_id: str | None = None) -> dict:
        # Lifting a stop is its own message to the control process and carries no
        # output permission: motion needs an explicit enable again afterwards.
        self._refuse_read_only("emergency-stop output")
        with self.lock:
            authorised_by = "control_token"
            live = bool(token) and token == self.control_token and time.monotonic() \
                <= (self.control_expires or 0.0)
            if not live:
                # An expired lease leaves nobody holding a usable token, and the stop still
                # has to be liftable. Only the owner that was in control when it was raised
                # may clear it, and clearing still grants no motion. Anything else falls
                # through to the ordinary 403 so there is no unauthenticated path.
                recovery_owner = self.estop_owner_client or self._estop_owner_from_events()
                if self.estop_latched and client_id and client_id == recovery_owner:
                    authorised_by = "prior_owner_recovery"
                else:
                    self._check_token(token)
            announced = [self._announce(self.heartbeat_pub, Empty())]
            announced.append(self._announce(self.estop_pub, Bool(data=False)))
            announced.append(self._announce(
                self.reset_pub, String(data=f"{self.control_token}|{time.time()}")))
            self.estop_latched = False
            self.estop_owner_client = None
            self.estop_requested_at = None
            self._emit("estop_cleared",
                       announcements="all" if all(announced) else "partial_no_actuator_topics")
            bridge = getattr(self, "licence_bridge", None)
            if bridge is not None:
                bridge.request_clear_estop()
            return {"estop": False, "clear_estop_sent": bridge is not None,
                    "authorised_by": authorised_by,
                    "motion_granted": False,
                    "announcements": "all" if all(announced) else "partial_no_actuator_topics"}

    def begin_hybrid_epoch(self, token: str, body: dict[str, Any]) -> dict:
        """Bind one protocol epoch to the current authenticated control lease."""
        self._check_token(token)
        allowed = {"session_id", "session_epoch", "calibration_bundle_hash", "channels"}
        if set(body) != allowed:
            raise ApiError(400, "hybrid_begin_fields",
                           f"expected exactly {sorted(allowed)}")
        session_id = body.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ApiError(400, "invalid_session_id",
                           "session_id must be a non-empty string")
        session_epoch = self._validated_session_epoch(body.get("session_epoch"))
        if session_epoch is None:
            raise ApiError(400, "invalid_session_epoch", "session_epoch is required")
        bundle_hash = body.get("calibration_bundle_hash")
        if (not isinstance(bundle_hash, str) or not bundle_hash
                or bundle_hash == "unbound"):
            raise ApiError(400, "invalid_calibration",
                           "calibration_bundle_hash must be a bound non-empty string")
        channels = body.get("channels")
        if (not isinstance(channels, list)
                or any(not isinstance(channel, str) or channel not in ("base", "arm", "gripper")
                       for channel in channels)
                or len(set(channels)) != len(channels)):
            raise ApiError(400, "invalid_channels",
                           "channels must be a unique subset of base, arm, gripper")
        if self.hybrid_gateway is None:
            raise ApiError(409, "hybrid_gateway_unavailable",
                           "the loaded profile has no hybrid gateway")

        previous = self.hybrid_gateway
        previous_stop = previous.stop("hybrid_epoch_replaced")
        dropped = int(previous_stop.get("queue_dropped", 0))
        gateway = self._new_hybrid_gateway(
            session_id=session_id,
            session_epoch=session_epoch,
            calibration_bundle_hash=bundle_hash,
        )
        readback = getattr(self, "ros_servo_readback", None)
        if readback is not None:
            readback.set_calibration_hash(bundle_hash)
        output_driver = getattr(self, "ros_servo_output", None)
        if output_driver is not None:
            gateway.driver = output_driver
        gateway.begin_epoch(session_epoch, bundle_hash)
        granted = gateway.grant(*channels)
        self.hybrid_gateway = gateway
        self.hybrid_control_token = token
        self.frame_outputs_enabled = False
        self._bridge_wake("hybrid_state_changed")
        event = self._emit(
            "hybrid_epoch_started",
            session_id=session_id,
            session_epoch=session_epoch,
            calibration_bundle_hash=bundle_hash,
            channels=list(granted["granted"]),
            simulated=self._is_simulated_profile(),
            outputs_enabled=False,
        )
        return {
            "session_id": session_id,
            "session_epoch": session_epoch,
            "calibration_bundle_hash": bundle_hash,
            "channels": list(granted["granted"]),
            "queue_dropped": dropped,
            "outputs_enabled": False,
            "driver_bound": self.hybrid_gateway.driver is not None,
            "simulated": self._is_simulated_profile(),
            "event": event,
        }

    def hybrid_command(self, token: str, envelope: dict[str, Any]) -> dict:
        """Pass a v1 envelope to the gateway without adding or rewriting fields."""
        self._refuse_frame_stream_action("hybrid command/dispatch/apply", token)
        gateway = self._require_hybrid_gateway(token)
        if not isinstance(envelope, dict):
            raise ApiError(400, "invalid_hybrid_envelope", "envelope must be an object")
        envelope = dict(envelope)
        ttl_ms = envelope.get("ttl_ms")
        if isinstance(ttl_ms, bool) or not isinstance(ttl_ms, int) or not 0 < ttl_ms <= 200:
            raise ApiError(400, "invalid_ttl", "ttl_ms must be in 1..200")
        # Monotonic epochs differ between the upper host and Pi. The car gate
        # stamps receipt/deadline locally from the relative TTL.
        envelope.pop("received_mono_ns", None)
        envelope.pop("deadline_mono_ns", None)
        try:
            result = gateway.submit(envelope)
        except Exception as exc:
            if hasattr(exc, "reason"):
                reason = str(exc.reason)
                detail = str(getattr(exc, "detail", "") or "")
            else:
                reason = type(exc).__name__
                detail = str(exc)
            self._reject(f"hybrid_{reason}", detail)
            raise ApiError(400, reason, detail) from exc

        channel = envelope["channel"]
        fields = command_event_fields(
            channel=channel,
            payload=envelope,
            profile=self.profile,
            control_epoch=envelope["session_epoch"],
            calibration_bundle_hash=envelope.get("calibration_bundle_hash"),
        )
        target = self._hybrid_event_target(channel, envelope["target"])
        event = self._emit(
            "command",
            **fields,
            target=target,
            gateway_target=dict(envelope["target"]),
            clutch=envelope.get("clutch"),
            gateway_envelope=dict(envelope),
            gateway_confirmation=result,
        )
        return {
            "accepted": True,
            "session_id": envelope["session_id"],
            "session_epoch": envelope["session_epoch"],
            "channel": channel,
            "seq": envelope["seq"],
            "ttl_ms": envelope["ttl_ms"],
            "calibration_bundle_hash": envelope["calibration_bundle_hash"],
            "outputs_enabled": bool(gateway.outputs_enabled),
            "driver_sent": "pending_dispatch",
            "controller_acknowledged": "unavailable",
            "execution_observed": "unavailable",
            "simulated": self._is_simulated_profile(),
            "event": event,
        }

    def hybrid_dispatch(self, token: str, body: dict[str, Any]) -> dict:
        self._refuse_frame_stream_action("hybrid command/dispatch/apply", token)
        """Drain one admitted intent; no driver means no send and no execution claim."""
        gateway = self._require_hybrid_gateway(token)
        if set(body) != {"channel"}:
            raise ApiError(400, "hybrid_dispatch_fields",
                           "expected exactly ['channel']")
        channel = body.get("channel")
        if channel not in ("base", "arm", "gripper"):
            raise ApiError(400, "unknown_channel", str(channel))
        result = gateway.dispatch(channel)
        if result is None:
            return {
                "channel": channel,
                "dispatched": False,
                "driver_sent": False,
                "outputs_enabled": False,
                "controller_acknowledged": "unavailable",
                "execution_observed": "unavailable",
                "hardware_effect": "unverified",
                "reason": "no_pending_command",
                "simulated": self._is_simulated_profile(),
            }
        return {
            "channel": channel,
            "dispatched": True,
            "driver_sent": bool(result.get("driver_sent", False)),
            "outputs_enabled": bool(gateway.outputs_enabled),
            "controller_acknowledged": result.get(
                "controller_acknowledged", "unavailable"),
            "execution_observed": "unavailable",
            "hardware_effect": "unverified",
            "simulated": self._is_simulated_profile(),
            "gateway": result,
        }

    def hybrid_enable_output(self, token: str, body: dict[str, Any]) -> dict:
        """Two-stage arm/engage gate for the real arm and gripper output driver."""
        # A no-output profile remains observation-only for every transport.  In
        # particular, a frame-stream licence must not bypass this profile gate.
        if self.hybrid_no_output or not self.actuator_outputs:
            raise ApiError(409, "hybrid_no_output", "this profile does not permit actuator outputs")
        gateway = self._require_hybrid_gateway(token)
        # Enabling during a latched stop would report success while the mirrored
        # permission stayed false, because the stop outranks the flag.  Refusing here
        # keeps the answer honest and keeps recovery on the two-step path it needs:
        # clear the stop, then enable.
        if self.estop_latched:
            raise ApiError(409, "estop_latched",
                           "clear the emergency stop before enabling outputs")
        allowed = {"session_id", "session_epoch", "calibration_bundle_hash"}
        if set(body) != allowed:
            raise ApiError(400, "hybrid_enable_fields",
                           f"expected exactly {sorted(allowed)}")
        status = gateway.status()
        for key in allowed:
            if body.get(key) != status.get(key):
                raise ApiError(409, "hybrid_identity_mismatch",
                               f"{key} does not match the active gateway epoch")
        ok, snapshot = self._feedback_gate()
        feedback = gateway.read_feedback(
            snapshot, now_ns=time.monotonic_ns(), max_age_ns=500_000_000)
        if not ok or feedback.get("valid") is not True or feedback.get("trusted") is not True:
            raise ApiError(409, "trusted_feedback_unavailable",
                           ",".join(feedback.get("reasons") or []))
        profile_name = getattr(self, "profile", {}).get("profile")
        if profile_name in {
                "hardware_hybrid_arm_trial_v1", "hardware_hybrid_arm2_trial_v1",
                "hardware_hybrid_arm3_trial_v1", "hardware_hybrid_arm4_trial_v1",
                "hardware_hybrid_arm5_trial_v1", "hardware_hybrid_homing_v1",
                "hardware_hybrid_follow_v1"}:
            bridge = self.licence_bridge
            state = bridge.state() if bridge is not None else {}
            process = state.get("frame_process") or {}
            age = process.get("age_s")
            if (not isinstance(age, (int, float)) or isinstance(age, bool)
                    or not math.isfinite(age) or not 0 <= age <= 1.0
                    or process.get("data_identity") != "hardware"
                    or process.get("configured_bundle_hash") != body["calibration_bundle_hash"]
                    or process.get("trial_motion") != self.teleop_cfg.get("trial_motion")
                    or process.get("homing_motion") != self.teleop_cfg.get("homing_motion")
                    or process.get("latched_fault") is not None
                    or process.get("outputs_enabled") is not False):
                raise ApiError(409, "trial_emitter_not_ready",
                               "the single-axis control process is missing, stale, or misconfigured")
        from .hybrid_gateway import HybridGatewayError

        if self.output_transport == "frame_stream":
            # The agent still decides; the control process still emits.  Enabling here
            # must not light up a second driver inside this process, so the identity
            # and trusted-feedback checks above are the gate and the licence mirror is
            # the only thing that changes.  There is no legacy fallback branch: a
            # profile that declares the frame transport gets frame semantics or a
            # refusal, never a silent second emitter.
            self.frame_outputs_enabled = True
            self._bridge_wake("frame_outputs_enabled")
            self._emit("frame_outputs_enabled", session_id=body["session_id"],
                       session_epoch=body["session_epoch"],
                       calibration_bundle_hash=body["calibration_bundle_hash"],
                       outputs_enabled=True, emitter="car_control_process",
                       frame_stream_connected=self.frame_stream_active(
                           body["session_id"], body["session_epoch"]))
            return {
                "enabled": True, "outputs_enabled": True, "emitter": "car_control_process",
                "driver_bound": False, "session_id": body["session_id"],
                "session_epoch": body["session_epoch"],
                "calibration_bundle_hash": body["calibration_bundle_hash"],
                "frame_stream_connected": self.frame_stream_active(
                    body["session_id"], body["session_epoch"]),
                "simulated": self._is_simulated_profile(),
                "controller_acknowledged": "unavailable",
                "execution_observed": "unavailable",
            }

        try:
            result = gateway.enable_outputs(feedback)
        except HybridGatewayError as exc:
            raise ApiError(409, exc.reason, exc.detail) from exc
        self._emit(
            "hybrid_outputs_enabled",
            session_id=body["session_id"],
            session_epoch=body["session_epoch"],
            calibration_bundle_hash=body["calibration_bundle_hash"],
            outputs_enabled=True,
        )
        return {
            **result,
            "session_id": body["session_id"],
            "session_epoch": body["session_epoch"],
            "calibration_bundle_hash": body["calibration_bundle_hash"],
            "simulated": self._is_simulated_profile(),
        }

    def hybrid_apply(self, token: str, body: dict[str, Any]) -> dict:
        """Submit and dispatch one accepted command in a single control request."""
        self._refuse_frame_stream_action("hybrid command/dispatch/apply", token)
        if set(body) != {"command"} or not isinstance(body.get("command"), dict):
            raise ApiError(400, "hybrid_apply_fields",
                           "expected exactly ['command']")
        command = dict(body["command"])
        accepted = self.hybrid_command(token, command)
        dispatched = self.hybrid_dispatch(token, {"channel": command["channel"]})
        return {"accepted": accepted, "dispatch": dispatched}

    def hybrid_stop(self, token: str, body: dict[str, Any]) -> dict:
        """Stop one token-bound gateway epoch; physical effect remains unverified."""
        gateway = self._require_hybrid_gateway(token)
        allowed = {"reason", "session_id", "session_epoch", "calibration_bundle_hash"}
        if not set(body).issubset(allowed):
            raise ApiError(400, "hybrid_stop_fields",
                           f"allowed fields are {sorted(allowed)}")
        reason = body.get("reason", "operator")
        if not isinstance(reason, str) or not reason:
            raise ApiError(400, "invalid_stop_reason",
                           "reason must be a non-empty string")
        status = gateway.status()
        if (status.get("session_id") in (None, "", "unbound")
                or status.get("calibration_bundle_hash") in (None, "", "unbound")):
            raise ApiError(409, "hybrid_epoch_not_started",
                           "no active hybrid epoch on this control lease")
        for key in ("session_id", "session_epoch", "calibration_bundle_hash"):
            if key in body and body[key] != status.get(key):
                raise ApiError(409, "hybrid_identity_mismatch",
                               f"{key} does not match the active gateway epoch")
        result = gateway.stop(reason)
        # A frame-mode stop has to revoke the mirror as well: the gateway flag the
        # legacy driver consults is not what the control process obeys.
        self.frame_outputs_enabled = False
        self._bridge_wake("frame_outputs_revoked_by_hybrid_stop")
        self._emit(
            "hybrid_stopped", reason=reason,
            session_id=status["session_id"],
            session_epoch=status["session_epoch"],
            calibration_bundle_hash=status["calibration_bundle_hash"],
            simulated=self._is_simulated_profile(),
            emitter="car_control_process" if self.output_transport == "frame_stream" else "agent_gateway",
        )
        return {
            **result,
            "session_id": status["session_id"],
            "session_epoch": status["session_epoch"],
            "calibration_bundle_hash": status["calibration_bundle_hash"],
            "outputs_enabled": False,
            "simulated": self._is_simulated_profile(),
        }

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
        now = time.monotonic()
        with self.lock:
            valid = self.control_token is not None and now <= self.control_expires and not self.estop_latched
            if not valid:
                if self.base_pub is not None and now - self.last_zero > 0.2:
                    self.base_pub.publish(Twist())
                    self.last_zero = now
                stale_identity = (self.control_client is not None
                                  or self.control_epoch is not None
                                  or self.frame_outputs_enabled)
                if (self.control_token is not None and now > self.control_expires) or (
                        self.control_token is None and stale_identity):
                    self._expire_control_lease("control_expired")
                # Targets queued while the lease was dropping must never burst out on the next valid
                # tick: that would replay expired arm poses, which 六 forbids.
                for pending in (self.arm_queue, self.gripper_queue):
                    while True:
                        try:
                            pending.get_nowait()
                        except queue.Empty:
                            break
                return
            # Frame mode creates none of these publishers, and the tick used to bail out on
            # that at the top: the expiry cleanup above therefore never ran, and the lease
            # identity plus the licence mirror outlived a dead lease. Only the publishing
            # below actually needs a legacy publisher.
            if self.base_pub is None and self.arm_pub is None:
                return
            # The base publisher is absent whenever the chassis is locked, and this phase locks it:
            # gating the whole tick on base_pub used to accept arm and gripper commands, record them
            # as applied, and never publish a single trajectory.
            if self.base_pub is not None and self.safety_wall and now - self.safety_wall > 1.0:
                self.base_target = Twist()
                self.base_valid_until = 0.0
                if now - getattr(self, "_last_safety_gz_zero", 0.0) > 1.0:
                    self._gz_zero()
                    self._last_safety_gz_zero = now
                    self._emit("safety_state_stale", age_s=round(now - self.safety_wall, 3))
            if self.heartbeat_pub is not None and now - self.last_heartbeat > 0.1:
                self.heartbeat_pub.publish(Empty())
                self.last_heartbeat = now
            if self.base_pub is not None:
                if now <= self.base_valid_until:
                    self.base_pub.publish(self.base_target)
                elif now - self.last_zero > 0.2:
                    self.base_pub.publish(Twist())
                    self.last_zero = now
            while self.arm_pub is not None:
                try:
                    msg = self.arm_queue.get_nowait()
                except queue.Empty:
                    break
                self.arm_pub.publish(msg)
            while self.gripper_pub is not None:
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
        self._check_clock_jump()
        with self.lock:
            if self.episode_id is not None and not self.read_only:
                cube = _pose_xyz(self.cube)
                if cube:
                    self.episode_max_lift = max(self.episode_max_lift, cube[2])
            ages = self._topic_ages(time.monotonic())
        if self.sessions.session_id is not None:
            self.sessions.poll_health(ages)

    def _check_clock_jump(self):
        if not self.read_only:
            return
        clock_ns = self._sim_ns()
        now = time.monotonic()
        with self.lock:
            previous = self.last_clock_ns
            previous_wall = self.last_clock_mono
            self.last_clock_ns = clock_ns
            self.last_clock_mono = now
            session_id = self.session_id
        if previous is None or previous_wall is None or session_id is None:
            return
        elapsed = max(0.0, now - previous_wall)
        backward = clock_ns < previous - 1_000_000_000
        forward = clock_ns > previous + int((5.0 + 2.0 * elapsed) * 1e9)
        if not (backward or forward):
            return
        detail = f"previous={previous} current={clock_ns} elapsed_wall={elapsed:.3f}s direction={'backward' if backward else 'forward'}"
        self.get_logger().error(f"clock jump detected: {detail}")
        self._emit("clock_jump_detected", detail=detail)
        self.sessions.note_clock_jump(detail)

    # ------------------------------------------------------------------ recording
    def _metadata(self) -> dict:
        profile_hash = hashlib.sha256(Path(self.profile_path).read_bytes()).hexdigest() if Path(self.profile_path).is_file() else None
        source_kind = self.profile.get("source") or (
            "hardware_readonly" if self.read_only
            else self.runtime.get("data_source")
            or "simulation"
        )
        data_source = self.runtime.get("data_source") or source_kind
        runtime_mode = getattr(self, "requested_runtime_mode", None) or self.runtime.get("mode")
        export_schema = (
            getattr(self, "requested_export_schema", None)
            or self.runtime.get("export_schema")
            or (self.profile.get("training_export") or {}).get("schema")
        )
        hardware_available = self.runtime.get("hardware_available", "unknown")
        simulation_declaration = self.profile.get("simulation") or {
            "simulated": False,
            "hardware_available": hardware_available,
        }
        common = {
            "agent_version": self.agent_version,
            "ros_distro": os.environ.get("ROS_DISTRO"),
            "profile": self.profile.get("profile"),
            "requested_profile": getattr(self, "requested_profile", None),
            "profile_sha256": profile_hash,
            "runtime_mode": runtime_mode,
            "agent_runtime_mode": self.runtime.get("mode"),
            "adapter": self.runtime.get("adapter"),
            "simulated": self._is_simulated_profile(),
            # The identity the profile declares, recorded in the session rather than derived
            # later: a manifest that cannot say what its numbers are is not evidence.
            "data_identity": (self.profile.get("recording") or {}).get("data_identity"),
            "data_source": data_source,
            "simulation": simulation_declaration,
            "hardware_available": hardware_available,
            "controller_session": {
                "export_schema": getattr(self, "requested_export_schema", None),
                "runtime_mode": getattr(self, "requested_runtime_mode", None),
                "profile": getattr(self, "requested_profile", None),
                "simulation": getattr(self, "requested_simulation", None),
            },
            "session_id": self.session_id,
            "session_epoch": getattr(self, "recording_session_epoch", None),
            "clock_epoch": getattr(self, "recording_clock_epoch", None),
            "requested_session_id": getattr(self, "requested_session_id", None),
            "requested_calibration_bundle_hash": getattr(
                self, "requested_calibration_bundle_hash", None),
            "session_identity_status": (
                "bound" if getattr(self, "recording_session_epoch", None) is not None
                else "session_epoch_unavailable"
            ),
            "source": source_kind,
            "clock_domain": self.clock_domain if hasattr(self, "clock_domain") else self.runtime.get("clock_domain"),
            "sensor_transport": "rosbridge_relay" if self.remote_source else "local_ros2_topic",
            "relay_clock_note": (
                "header.stamp comes from the car clock and may be unsynchronised; bag receive time is the "
                "recording host clock" if self.remote_source else None
            ),
            "actuator_outputs": self.actuator_outputs,
            "camera_profile": self.camera_profile,
            "camera_extrinsics": self.camera_extrinsics,
            "task_name": self.task_name,
            "training_export": bool(self.profile.get("training_export", {}).get("enabled")) and not self.read_only,
            "export_schema": export_schema,
            "features": self.profile.get("features"),
            "base_velocity_topic": self.runtime.get("base_velocity_topic"),
            "teleop_state_topic": (self.teleop_cfg.get("state_topic")
                                   if self.teleop_cfg.get("enabled") else None),
            "required_topics": list(self.profile.get("recording", {}).get("required_topics", [])),
            "recording_topics": list(self.profile.get("recording", {}).get("topics", [])),
            "recording_extra_topics": list(
                self.profile.get("recording", {}).get("extra_topics", [])),
            "recording_event_topic": self.profile.get("recording", {}).get(
                "event_topic", "/lelab/events"),
            "feedback_provenance": self._feedback_provenance(),
            "camera_topic": self.rgb_topic,
            "camera_info_topic": self.camera_info_topic,
            "preview_backend": self.preview_backend,
            "sensor_source": self._remote_state(),
            "record_joint_state_topic": self.record_joint_topic,
            "timestamp_sources": {"event": self.runtime.get("clock_domain", "ros_time"), "ttl": "monotonic_wall_clock"},
            "clock_jump_policy": "hardware_readonly stops the bag on >1s backward or >5s+2*elapsed forward jump; no READY",
            "command_semantics": {
                "requested": "operator_or_expert_request",
                "sent": "gateway_forwarded_target",
                "acknowledged": ("gateway_action_accepted_or_rejected_on_/lelab/applied_command"
                                 if self.teleop_cfg.get("enabled") else "not_available"),
                "measured_state": ("follower_joint_states" if self.follower_feedback is not None
                                   else ("unavailable_readonly" if self.read_only
                                         else "joint_states_and_model_odom")),
                "executed": "not_directly_observable_inferred_from_later_feedback",
                "teleop_recorded_fields": {
                    "leader_rad": "measured leader reading forwarded by the upper computer",
                    "pre_clip_rad": "mapped target before gateway clamping",
                    "target": "post-clamp target sent through the gateway",
                    "calibration_version": "leader|follower|mapping content hashes at the time of the command",
                    "state_topic": self.teleop_cfg.get("state_topic", "/lelab/recording/teleop"),
                },
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

    def start_session(self, token: str, task: str, *, requested_session_id: str | None = None,
                      session_epoch: int | None = None, clock_epoch: str | None = None,
                      calibration_bundle_hash: str | None = None,
                      export_schema: str | None = None,
                      runtime_mode: str | None = None,
                      requested_profile: str | None = None,
                      simulation: bool | dict[str, Any] | None = None) -> dict:
        if not self.read_only:
            self._check_token(token)
        if self.read_only and not self.rgb_topic:
            raise ApiError(409, "camera_topic_unconfigured", "hardware_readonly camera topic has not been verified")
        if requested_session_id is not None and (
                not isinstance(requested_session_id, str) or not requested_session_id):
            raise ApiError(400, "invalid_session_id",
                           "session_id must be a non-empty string when provided")
        session_epoch = self._validated_session_epoch(session_epoch)
        if clock_epoch is not None and (
                not isinstance(clock_epoch, str) or not clock_epoch):
            raise ApiError(400, "invalid_clock_epoch",
                           "clock_epoch must be a non-empty string when provided")
        if calibration_bundle_hash is not None and (
                not isinstance(calibration_bundle_hash, str)
                or not calibration_bundle_hash
                or calibration_bundle_hash == "unbound"):
            raise ApiError(400, "invalid_calibration",
                           "calibration_bundle_hash must be a bound non-empty string")
        configured_simulation = self._is_simulated_profile()
        requested_simulation = simulation
        if simulation is not None:
            if isinstance(simulation, bool):
                simulation_flag = simulation
            elif isinstance(simulation, dict) and isinstance(simulation.get("simulated"), bool):
                simulation_flag = simulation["simulated"]
            else:
                raise ApiError(400, "invalid_simulation",
                               "simulation must be a bool or an object with boolean simulated")
            if simulation_flag is not configured_simulation:
                raise ApiError(409, "simulation_profile_mismatch",
                               "controller simulation flag differs from the loaded profile")
        if requested_profile is not None:
            if not isinstance(requested_profile, str) or not requested_profile:
                raise ApiError(400, "invalid_profile",
                               "profile must be a non-empty string")
            if requested_profile != self.profile.get("profile"):
                raise ApiError(409, "profile_mismatch",
                               f"loaded profile is {self.profile.get('profile')!r}")
        if runtime_mode is not None:
            if not isinstance(runtime_mode, str) or not runtime_mode:
                raise ApiError(400, "invalid_runtime_mode",
                               "runtime_mode must be a non-empty string")
            allowed_modes = ({"simulated_hybrid_manual", "hybrid_manual_sim"}
                             if configured_simulation else {"hardware_hybrid_manual"})
            if runtime_mode not in allowed_modes:
                raise ApiError(409, "runtime_mode_mismatch",
                               f"{runtime_mode!r} does not match the loaded profile")
        expected_schema = (
            self.runtime.get("export_schema")
            or (self.profile.get("training_export") or {}).get("schema")
        )
        if export_schema is not None:
            if not isinstance(export_schema, str) or not export_schema:
                raise ApiError(400, "invalid_export_schema",
                               "export_schema must be a non-empty string")
            if expected_schema and export_schema != expected_schema:
                raise ApiError(409, "export_schema_mismatch",
                               f"loaded profile expects {expected_schema!r}")
        if self.sessions.active:
            raise ApiError(409, "session_active", self.sessions.session_id or "")
        missing = self.topic_health(missing_only=True)
        if missing:
            raise ApiError(409, "topics_missing", ", ".join(missing))
        sid = new_session_id()
        with self.lock:
            self.session_id = sid
            self.requested_session_id = requested_session_id
            self.recording_session_epoch = session_epoch
            self.recording_clock_epoch = clock_epoch
            self.requested_calibration_bundle_hash = calibration_bundle_hash
            self.requested_export_schema = export_schema
            self.requested_runtime_mode = runtime_mode
            self.requested_profile = requested_profile
            self.requested_simulation = requested_simulation
            self.episode_id = None
            self.episode_phases = []
            self.episode_contacts = 0
        try:
            info = self.sessions.start_session(sid, task or self.profile.get("task", {}).get("language", "observe"), self._metadata(), self._sim_ns())
        except Exception:
            with self.lock:
                self.session_id = None
                self.requested_session_id = None
                self.recording_session_epoch = None
                self.recording_clock_epoch = None
                self.requested_calibration_bundle_hash = None
                self.requested_export_schema = None
                self.requested_runtime_mode = None
                self.requested_profile = None
                self.requested_simulation = None
            raise
        self._emit("recording_session_started", session_id=sid, task=task, topics=info["topics"])
        return info

    def start_episode(self, token: str, source: str = "scripted_expert", task: str = "",
                      requested_episode_id: str | None = None,
                      session_id: str | None = None, session_epoch: int | None = None,
                      calibration_bundle_hash: str | None = None) -> dict:
        if not self.read_only:
            self._check_token(token)
        if self.read_only:
            source = "hardware_readonly"
        self._check_recording_identity(
            session_id=session_id, session_epoch=session_epoch,
            calibration_bundle_hash=calibration_bundle_hash)
        if self.sessions.current_episode is not None:
            raise ApiError(409, "episode_open", self.sessions.current_episode)
        if not self.sessions.active:
            raise ApiError(409, "no_session", "start a recording session first")
        if not self.read_only and self.control_mode not in ("idle", "keyboard", "scripted_expert",
                                                            "leader_arm"):
            # leader_arm belongs in this list: recording is a subtask of the teleop session, so the
            # session is already in leader_arm mode when an episode opens. set_mode refuses that mode
            # unless the profile enables it, so this cannot widen the read-only profile.
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
        if requested_episode_id is not None:
            if (not isinstance(requested_episode_id, str) or not requested_episode_id
                    or len(requested_episode_id) > 128
                    or any(not (char.isalnum() or char in "_-") for char in requested_episode_id)):
                raise ApiError(400, "invalid_episode_id",
                               "episode_id must use letters, digits, '_' or '-' (max 128)")
        eid = requested_episode_id or f"ep_{int(time.time())}_{secrets.token_hex(3)}"
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

    def set_phase(self, token: str, phase: str, *, episode_id: str | None = None,
                  clock_epoch: str | None = None, session_id: str | None = None,
                  session_epoch: int | None = None,
                  calibration_bundle_hash: str | None = None) -> dict:
        """Append an operator phase boundary to the active car recording timeline."""
        if not self.read_only:
            self._check_token(token)
        if not isinstance(phase, str) or not phase:
            raise ApiError(400, "unknown_phase", "phase must be a non-empty string")
        if not isinstance(clock_epoch, str) or not clock_epoch:
            raise ApiError(400, "missing_clock_epoch",
                           "clock_epoch is required for a phase event")
        if episode_id is not None and (
                not isinstance(episode_id, str) or not episode_id):
            raise ApiError(400, "invalid_episode_id",
                           "episode_id must be a non-empty string when provided")
        with self.lock:
            current_episode = self.sessions.current_episode
            bound_clock_epoch = getattr(self, "recording_clock_epoch", None)
            if not self.sessions.active or current_episode is None:
                raise ApiError(409, "no_active_episode",
                               "start a recording session and episode first")
            if episode_id is not None and episode_id != current_episode:
                raise ApiError(409, "episode_mismatch",
                               f"active episode is {current_episode!r}")
            self._check_recording_identity(
                session_id=session_id, session_epoch=session_epoch,
                calibration_bundle_hash=calibration_bundle_hash, bind_epoch=True)
            if bound_clock_epoch is not None and clock_epoch != bound_clock_epoch:
                raise ApiError(409, "clock_epoch_mismatch",
                               "phase request uses a different clock epoch")
            self.recording_clock_epoch = clock_epoch
        try:
            event = self.sessions.set_phase(
                phase, self._sim_ns(),
                session_epoch=self.recording_session_epoch,
                clock_epoch=self.recording_clock_epoch,
                source="operator",
            )
        except ValueError as exc:
            raise ApiError(400, "phase_rejected", str(exc)) from exc
        except RuntimeError as exc:
            raise ApiError(409, "phase_rejected", str(exc)) from exc
        self._emit("phase_started", phase=phase, phase_time_ns=event["time_ns"],
                   clock_epoch=clock_epoch, source="operator")
        return {
            **event,
            "session_id": self.session_id,
            "session_epoch": self.recording_session_epoch,
            "clock_epoch": self.recording_clock_epoch,
        }

    def _evaluate(self, manual_success: bool | None = None, note: str = "") -> dict:
        if self.read_only:
            return {
                "success": "unknown", "labels": "unlabeled", "task": self.task_name, "failure_reasons": [],
                "reason": "hardware_readonly_has_no_actuation_or_state_labels",
                "camera_profile": self.camera_profile,
                "privileged_truth_used_for_scripted_expert_only": False,
                "timestamp_wall": time.time(),
            }
        if not evaluation_policy(self.profile.get("task", {}))["simulator_truth_available"]:
            return manual_evaluation(
                self.profile.get("task", {}), task=self.task_name,
                reason="no_simulator_ground_truth_declared_by_profile",
                camera_profile=self.camera_profile,
                manual_success=manual_success, note=note)
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

    def end_episode(self, token: str, discarded: bool = False, reason: str = "",
                    manual_success=None, note: str = "",
                    requested_episode_id: str | None = None,
                    session_id: str | None = None, session_epoch: int | None = None,
                    calibration_bundle_hash: str | None = None) -> dict:
        """Close the open episode. ``manual_success`` is the operator's label; None keeps it unknown."""
        if not self.read_only:
            self._check_token(token)
        self._check_recording_identity(
            session_id=session_id, session_epoch=session_epoch,
            calibration_bundle_hash=calibration_bundle_hash)
        if self.sessions.current_episode is None:
            raise ApiError(409, "no_episode", "no open episode")
        if requested_episode_id is not None and requested_episode_id != self.sessions.current_episode:
            raise ApiError(409, "episode_mismatch",
                           f"active episode is {self.sessions.current_episode!r}")
        evaluation = self._evaluate(manual_success=manual_success_label(manual_success),
                                    note=str(note or ""))
        with self.lock:
            eid = self.episode_id
            self.episode_phases.append({"phase": "record_end", "sim_time_ns": self._sim_ns(), "success": evaluation["success"]})
        event = self.sessions.finish_episode(self._sim_ns(), evaluation, discarded=discarded, reason=reason)
        self._emit("episode_ended", episode_id=eid, discarded=discarded, reason=reason, evaluation=evaluation)
        with self.lock:
            self.episode_id = None
        return {"episode_id": eid, "evaluation": evaluation, "event": event}

    def stop_session(self, token: str, reason: str = "normal", *,
                     session_id: str | None = None, session_epoch: int | None = None,
                     calibration_bundle_hash: str | None = None) -> dict:
        if not self.read_only:
            self._check_token(token)
        self._check_recording_identity(
            session_id=session_id, session_epoch=session_epoch,
            calibration_bundle_hash=calibration_bundle_hash)
        if self.sessions.current_episode is not None:
            cause = self.sessions.error
            self.end_episode(
                token, discarded=True,
                reason=f"forced_by_{cause}" if cause else "episode_open_at_stop",
            )
        result = self.sessions.stop_session(reason=reason, time_ns=self._sim_ns())
        with self.lock:
            self.session_id = None
            self.episode_id = None
            self.requested_session_id = None
            self.recording_session_epoch = None
            self.recording_clock_epoch = None
            self.requested_calibration_bundle_hash = None
            self.requested_export_schema = None
            self.requested_runtime_mode = None
            self.requested_profile = None
            self.requested_simulation = None
        self._emit("recording_session_stopped", **result)
        return result

    def topic_health(self, missing_only: bool = False) -> list[str] | dict:
        required = list(self.profile.get("recording", {}).get("required_topics", []))
        ages = self._topic_ages(time.monotonic())
        ages = {topic: ages.get(topic) for topic in required}
        missing = [t for t, age in ages.items() if age is None or age > 2.0]
        if missing_only:
            return missing
        return {"ages_s": ages, "missing": missing}

    def _topic_ages(self, now: float) -> dict[str, float | None]:
        """Age of the last traffic on each topic, whether this node received or published it.

        /lelab/recording/teleop is produced by this node, so a receive-only table reads as
        permanently stale: the start-session gate refuses, and the running health poll marks the
        session `required_stream_missing`, which blocked READY for every teleop recording.
        """
        merged: dict[str, float] = {}
        for table in (self.last_rx, self.last_tx):
            for topic, wall in table.items():
                if wall is not None and wall > merged.get(topic, 0.0):
                    merged[topic] = wall
        return {topic: (None if wall is None else round(now - wall, 3))
                for topic, wall in merged.items()}

    # ------------------------------------------------------------------ expert
    def run_expert(self, token: str) -> dict:
        self._refuse_frame_stream_action('/expert/run')
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
        self._refuse_frame_stream_action('/task/reset')
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
            ages = self._topic_ages(now)
            preview = self._latest_preview_locked(now)
            return {
                "ok": True,
                "agent_version": self.agent_version,
                "profile": self.profile["profile"],
                "task": self.profile["task"],
                "sim_time_ns": self._sim_ns() if not self.read_only else None,
                "time_ns": self._sim_ns(),
                "clock_domain": self.clock_domain,
                "read_only": self.read_only,
                "simulated": self._is_simulated_profile(),
                "data_source": self.runtime.get("data_source") or "hardware",
                "hardware_available": self.runtime.get("hardware_available", "unknown"),
                "hybrid": self._hybrid_status(),
                # Who may emit, and what the mirror currently says.  Read-only view:
                # the facts below come from the same objects the routes consult.
                "licence": {
                    "output_transport": getattr(self, "output_transport", "legacy"),
                    # Two different questions, two different fields. What the driver asked
                    # for is a request; whether anything may actually be emitted also needs
                    # a live lease and no latched stop. Reporting only the request let an
                    # expired lease keep advertising outputs the emitter would not produce.
                    "frame_outputs_requested": bool(getattr(self, "frame_outputs_enabled", False)),
                    "frame_outputs_enabled": (
                        bool(getattr(self, "frame_outputs_enabled", False))
                        and self.control_token is not None
                        and time.monotonic() <= (self.control_expires or 0.0)
                        and not self.estop_latched),
                    "frame_lease_live": bool(self.control_token is not None
                                             and time.monotonic() <= (self.control_expires or 0.0)),
                    "frame_estop_latched": bool(self.estop_latched),
                    "frame_estop_age_s": (round(time.monotonic() - self.estop_requested_at, 3)
                                          if getattr(self, "estop_requested_at", None) else None),
                    "frame_stream_connected": self.frame_stream_active(),
                    "bridge": getattr(self, "licence_bridge", None) and
                               self.licence_bridge.state(),
                },
                "runtime_mode": self.runtime.get("mode"),
                "camera_profile": self.camera_profile,
                "camera_extrinsics": self.camera_extrinsics,
                "control": {
                    "active": self.control_token is not None and now <= self.control_expires and not self.estop_latched,
                    "client_id": self.control_client,
                    "mode": self.control_mode,
                    "session_epoch": self.control_epoch,
                    "expires_in_s": round(self.control_expires - now, 2) if self.control_token else 0.0,
                    "estop_latched": self.estop_latched,
                    "last_reject": self.last_reject,
                    "leader_arm_available": self.leader_arm_enabled,
                    "follower_feedback": self._feedback_gate()[1],
                    "teleop": {
                        "enabled": bool(self.teleop_cfg.get("enabled")),
                        "state_topic": self.teleop_cfg.get("state_topic"),
                        "last": self.last_teleop,
                        "chassis_locked": bool(self.teleop_cfg.get("chassis_locked", True)),
                        "exclusivity_required": True,
                        # What "stopping" physically means on this controller. This gateway never
                        # writes torque-off, so the servos keep the last commanded position.
                        "hold_policy": self.teleop_cfg.get("hold_policy"),
                        "torque_control_offered": False,
                        "exclusivity_guard_topics": self._guard_topics(),
                        "exclusivity": self.last_exclusivity,
                        # Detection only: the arm stream's own TTL, on this machine's monotonic clock.
                        # "the upper stopped sending" must never be read as "the arm is commanded to stop".
                        "stream": (self.gate.arm_stream() if self.gate else
                                   {"expected": False, "live": False, "ttl_s": None,
                                    "age_s": None, "remaining_s": None, "clock_domain": "monotonic"}),
                    },
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
                            "stamp_ns": preview.source_stamp_ns,
                            "source_stamp_ns": preview.source_stamp_ns,
                            "latest_image_stamp_ns": self.image_stamp_ns,
                            "preview_seq": preview.frame_seq,
                            "preview_age_s": preview.source_age_s,
                            "preview_source_topic": preview.source_topic,
                            "preview_backend": preview.backend,
                            "stale": (ages.get(self.rgb_topic) is None or ages.get(self.rgb_topic) > 1.5) if self.rgb_topic else True},
                    "camera_info": {**self.camera_info_meta, "topic": self.camera_info_topic,
                                    "age_s": ages.get(self.camera_info_topic) if self.camera_info_topic else None},
                    "depth": {**self.depth_meta, "topic": self.depth_topic, "age_s": ages.get(self.depth_topic) if self.depth_topic else None},
                    "lidar": {**self.lidar_meta, "topic": self.lidar_topic, "age_s": ages.get(self.lidar_topic) if self.lidar_topic else None},
                    "imu": {**self.imu_meta, "topic": self.imu_topic, "age_s": ages.get(self.imu_topic) if self.imu_topic else None},
                    "remote_source": self._remote_state(),
                    "contacts": {"count": self.contact_count, "recent": list(self.contact_pairs)[-5:]},
                },
                "topics": self.topic_health(),
                "recording": {
                    **self.sessions.status_snapshot(),
                    "session_id": self.session_id,
                    "session_epoch": getattr(self, "recording_session_epoch", None),
                    "clock_epoch": getattr(self, "recording_clock_epoch", None),
                    "requested_session_id": getattr(self, "requested_session_id", None),
                    "feedback_provenance": self._feedback_provenance(),
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
    def _latest_preview_locked(self, now: float) -> PreviewSnapshot:
        source_wall = self.preview_source_wall or self.preview_wall
        if self.preview_jpeg is not None and source_wall <= 0:
            source_wall = self.image_wall
        stamp_ns = self.preview_stamp_ns
        if self.preview_jpeg is not None and stamp_ns is None:
            # Compatibility with pre-upgrade in-memory state; new frames always set
            # preview_stamp_ns at the same commit as preview_seq.
            stamp_ns = self.image_stamp_ns
        age_s = None
        if self.preview_jpeg is not None and source_wall > 0:
            age_s = max(0.0, now - source_wall)
        return PreviewSnapshot(
            jpeg=self.preview_jpeg,
            frame_seq=self.preview_seq,
            source_stamp_ns=stamp_ns,
            source_age_s=age_s,
            source_topic=self.rgb_topic,
            backend=self.preview_backend,
        )

    def preview_status(self) -> dict[str, Any]:
        with self.lock:
            snapshot = self._latest_preview_locked(time.monotonic())
        return {
            "preview_seq": snapshot.frame_seq,
            "stamp_ns": snapshot.source_stamp_ns,
            "source_stamp_ns": snapshot.source_stamp_ns,
            "latest_image_stamp_ns": getattr(self, "image_stamp_ns", None),
            "source_age_s": snapshot.source_age_s,
            "source_topic": snapshot.source_topic,
            "preview_backend": snapshot.backend,
        }

    def latest_preview(self) -> PreviewSnapshot:
        with self.lock:
            return self._latest_preview_locked(time.monotonic())

    def preview_at(self, frame_seq: int | None = None) -> PreviewSnapshot | None:
        now = time.monotonic()
        with self.lock:
            if frame_seq is None:
                return self._latest_preview_locked(now)
            for seq, stamp_ns, source_wall, jpeg in reversed(self.preview_frames):
                if seq == frame_seq:
                    return PreviewSnapshot(
                        jpeg=jpeg,
                        frame_seq=seq,
                        source_stamp_ns=stamp_ns,
                        source_age_s=max(0.0, now - source_wall),
                        source_topic=self.rgb_topic,
                        backend=self.preview_backend,
                    )
            return None


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
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ApiError(400, "json", str(exc)) from exc
        if not isinstance(body, dict):
            raise ApiError(400, "body", "request body must be a JSON object")
        return body

    def _token(self, body: dict | None = None) -> str:
        return self.headers.get("X-LeLab-Control-Token") or (
            body.get("token") if isinstance(body, dict) else ""
        ) or ""

    def do_GET(self):
        try:
            parsed = urlsplit(self.path)
            request_path = parsed.path
            if request_path == "/health":
                self._json(200, {"ok": True, "agent": self.server.agent.agent_version})
            elif request_path == "/status":
                self._json(200, self.server.agent.status())
            elif request_path == "/preview.mjpg":
                self._mjpeg()
            elif request_path == "/preview.jpg":
                requested = parse_qs(parsed.query).get("frame_seq", [None])[-1]
                if requested is not None:
                    try:
                        requested = int(requested)
                    except ValueError:
                        self._json(400, {"error": "invalid_frame_seq", "detail": requested})
                        return
                    if requested < 0:
                        self._json(400, {"error": "invalid_frame_seq", "detail": str(requested)})
                        return
                preview = self.server.agent.preview_at(requested)
                if preview is None:
                    self._json(409, {"error": "frame_not_available",
                                     "detail": f"frame_seq={requested} is outside the current buffer"})
                    return
                if (preview.jpeg is None or preview.source_stamp_ns is None
                        or preview.source_age_s is None):
                    self._json(503, {"error": "preview_unavailable",
                                     "detail": "no source-bound preview frame"})
                else:
                    headers = self._preview_headers(preview)
                    self.send_response(200)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(preview.jpeg)))
                    self.end_headers()
                    self.wfile.write(preview.jpeg)
            elif request_path == "/sessions":
                self._json(200, {"sessions": self.server.agent.sessions.list_sessions()})
            elif request_path.startswith("/sessions/"):
                parts = request_path.strip("/").split("/")
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
            # Hybrid profile: never let a legacy actuator/expert endpoint bypass the
            # channel gateway, even if a control token happens to be valid.
            if agent.hybrid_no_output and self.path in (
                    "/control/mode", "/control/base", "/control/arm", "/control/gripper",
                    "/expert/run", "/expert/stop", "/task/reset"):
                raise ApiError(409, "hybrid_no_output",
                               "hybrid profile refuses legacy actuator paths; use the v1 channel gateway")
            if self.path == "/hybrid/begin_epoch":
                out = agent.begin_hybrid_epoch(token, body)
            elif self.path == "/hybrid/enable_output":
                out = agent.hybrid_enable_output(token, body)
            elif self.path == "/hybrid/apply":
                out = agent.hybrid_apply(token, body)
            elif self.path == "/calibration/follower/start":
                out = agent.follower_calibration_start(body)
            elif self.path == "/calibration/follower/status":
                out = agent.follower_calibration_status(body)
            elif self.path == "/calibration/follower/manual-ranges":
                out = agent.follower_calibration_manual_ranges(body)
            elif self.path == "/calibration/follower/torque":
                out = agent.follower_calibration_torque(body)
            elif self.path == "/calibration/follower/gripper/target":
                out = agent.follower_calibration_gripper_target(body)
            elif self.path == "/calibration/follower/gripper/mark":
                out = agent.follower_calibration_gripper_mark(body)
            elif self.path == "/calibration/follower/gripper/stop":
                out = agent.follower_calibration_gripper_stop(body)
            elif self.path == "/calibration/follower/stop":
                out = agent.follower_calibration_stop(body)
            elif self.path == "/hybrid/command":
                out = agent.hybrid_command(token, body)
            elif self.path == "/hybrid/dispatch":
                out = agent.hybrid_dispatch(token, body)
            elif self.path == "/hybrid/stop":
                out = agent.hybrid_stop(token, body)
            elif self.path == "/control/acquire":
                out = agent.acquire(
                    str(body.get("client_id", "browser")),
                    float(body.get("ttl_s", 30)),
                    recovery=body.get("recovery") is True)
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
                out = agent.clear_estop(token, str(body.get("client_id") or "") or None)
            elif self.path == "/recording/start_session":
                out = agent.start_session(
                    token, str(body.get("task", "")),
                    requested_session_id=body.get("session_id"),
                    session_epoch=body.get("session_epoch"),
                    clock_epoch=body.get("clock_epoch"),
                    calibration_bundle_hash=body.get("calibration_bundle_hash"),
                    export_schema=body.get("export_schema"),
                    runtime_mode=body.get("runtime_mode"),
                    requested_profile=body.get("profile"),
                    simulation=body.get("simulation"),
                )
            elif self.path == "/recording/start_episode":
                out = agent.start_episode(
                    token, str(body.get("source", "scripted_expert")),
                    str(body.get("task", "")),
                    requested_episode_id=body.get("episode_id"),
                    session_id=body.get("session_id"),
                    session_epoch=body.get("session_epoch"),
                    calibration_bundle_hash=body.get("calibration_bundle_hash"),
                )
            elif self.path == "/recording/set_phase":
                out = agent.set_phase(
                    token, str(body.get("phase", "")),
                    episode_id=body.get("episode_id"),
                    clock_epoch=body.get("clock_epoch"),
                    session_id=body.get("session_id"),
                    session_epoch=body.get("session_epoch"),
                    calibration_bundle_hash=body.get("calibration_bundle_hash"),
                )
            elif self.path == "/recording/end_episode":
                out = agent.end_episode(token, bool(body.get("discarded", False)), str(body.get("reason", "")),
                                        manual_success=body.get("success"), note=str(body.get("note", "")),
                                        requested_episode_id=body.get("episode_id"),
                                        session_id=body.get("session_id"),
                                        session_epoch=body.get("session_epoch"),
                                        calibration_bundle_hash=body.get("calibration_bundle_hash"))
            elif self.path == "/recording/discard_episode":
                out = agent.end_episode(
                    token, True, str(body.get("reason", "discarded")),
                    requested_episode_id=body.get("episode_id"),
                    session_id=body.get("session_id"),
                    session_epoch=body.get("session_epoch"),
                    calibration_bundle_hash=body.get("calibration_bundle_hash"))
            elif self.path == "/recording/stop_session":
                out = agent.stop_session(
                    token, str(body.get("reason", "normal")),
                    session_id=body.get("session_id"),
                    session_epoch=body.get("session_epoch"),
                    calibration_bundle_hash=body.get("calibration_bundle_hash"))
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
                preview = agent.latest_preview()
                if (preview.jpeg is not None and preview.frame_seq != last
                        and preview.source_stamp_ns is not None
                        and preview.source_age_s is not None):
                    frame_headers = self._preview_headers(preview)
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(
                        b"Content-Type: image/jpeg\r\nContent-Length: "
                        + str(len(preview.jpeg)).encode() + b"\r\n")
                    for name, value in frame_headers.items():
                        self.wfile.write(
                            f"{name}: {value}\r\n".encode("ascii"))
                    self.wfile.write(b"\r\n")
                    self.wfile.write(preview.jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    last = preview.frame_seq
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            return

    @staticmethod
    def _preview_headers(preview: PreviewSnapshot) -> dict[str, str]:
        age_s = float(preview.source_age_s)
        return {
            "X-LeLab-Frame-Seq": str(preview.frame_seq),
            "X-LeLab-Source-Stamp-Ns": str(preview.source_stamp_ns),
            "X-LeLab-Source-Age-Ms": f"{age_s * 1000.0:.3f}",
            "X-LeLab-Source-Age-S": f"{age_s:.6f}",
            "X-LeLab-Source-Topic": str(preview.source_topic or ""),
            "Cache-Control": "no-store",
            "Access-Control-Expose-Headers": (
                "X-LeLab-Frame-Seq, X-LeLab-Source-Stamp-Ns, "
                "X-LeLab-Source-Age-Ms, X-LeLab-Source-Age-S"
            ),
        }

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
    agent.start_licence_bridge(agent.licence_socket)
    agent.get_logger().info(f"HTTP agent listening on http://{host}:{port}")
    try:
        rclpy.spin(agent)
    except BaseException as stop:  # decided below: anything that is not a planned stop re-raises
        if not _is_planned_stop(stop):
            raise
        agent.get_logger().info(f"agent stopping: {type(stop).__name__}")
    finally:
        _teardown(agent, server)


def _is_planned_stop(stop: BaseException) -> bool:
    """Whether a spin-loop exit was the operator asking this agent to stop.

    A stop arrives as a signal, and rclpy turns it into an exception instead of a clean return - but
    which exception depends on where the signal lands: ``ExternalShutdownException``, an ``RCLError``
    out of the wait set (both seen on 2026-11-03), or a bare ``KeyboardInterrupt``. Letting any of
    them escape printed a traceback over the exit status of every planned 停止/回退, and the deployer
    could not tell that apart from a crash. The context is the reliable witness: nothing in this
    process tears it down before main() returns from the spin loop.
    """
    return isinstance(stop, PLANNED_STOP_ERRORS) or not rclpy.ok()


def _teardown(agent, server) -> None:
    """Stop everything the agent was commanding, then release the ROS context exactly once.

    Order matters: the session is closed and the chassis zeroed while the node can still publish,
    because "the upper computer stopped sending" is not the same as "the car stopped". rclpy's own
    signal handler has already torn the context down when a spin loop exits on SIGINT/SIGTERM, and a
    second shutdown() raises RCLError - that traceback would replace the real exit status, so the
    context is only shut down when it is still live.
    """
    # Revoke the licence before anything blocking happens: a dying agent must not
    # leave the control process emitting on a permission that no longer has an owner.
    bridge = getattr(agent, "licence_bridge", None)
    if bridge is not None:
        try:
            bridge.stop("agent_teardown")
        except Exception:  # noqa: BLE001 - teardown continues regardless
            pass
    agent._preview_stop.set()
    try:
        # A calibration job may hold released torque; stop it before the node goes away
        # so the servos are re-enabled instead of staying limp.
        agent.shutdown_calibration()
    except Exception:
        pass
    try:
        if agent.sessions.active:
            agent.sessions.stop_session()
    except Exception:
        pass
    if agent.base_pub is not None:
        agent.base_pub.publish(Twist())
    server.shutdown()
    agent.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()


if __name__ == "__main__":
    main()
