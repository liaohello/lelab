"""Independent ROS safety watchdog.

The HTTP agent is deliberately not the only owner of motion safety.  This
process watches a wall-clock heartbeat and request topics, forwards accepted
trajectories to the controllers, and stops the base / cancels and holds the
arm when the agent, its control session, or the bridge disappears.
"""
from __future__ import annotations

import json
import subprocess
import time

import rclpy
from control_msgs.action import FollowJointTrajectory
from geometry_msgs.msg import Twist
from rclpy.action import ActionClient
from rclpy.node import Node
from std_msgs.msg import Bool, Empty, String
from trajectory_msgs.msg import JointTrajectory

ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"]
GRIPPER_JOINTS = ["gripper_base_joint", "gripper_left_join", "gripper_right_joint", "left_jaw_joint", "right_jaw_joint"]
LIMITS = {name: (-2.09, 2.09) for name in ARM_JOINTS}
LIMITS.update({
    "gripper_base_joint": (-1.638, 0.0),
    "gripper_left_join": (-2.4569, 2.4569),
    "gripper_right_joint": (-2.4569, 2.4569),
    "left_jaw_joint": (0.0, 0.019),
    "right_jaw_joint": (-0.019, 0.0),
})


class Watchdog(Node):
    def __init__(self):
        super().__init__("lelab_safety_watchdog")
        self.declare_parameter("heartbeat_timeout_s", 0.6)
        self.declare_parameter("base_ttl_s", 0.30)
        self.heartbeat_timeout = float(self.get_parameter("heartbeat_timeout_s").value)
        self.base_ttl = float(self.get_parameter("base_ttl_s").value)
        self.last_heartbeat = 0.0
        self.last_base = 0.0
        self.base_target = Twist()
        self.joints: dict[str, float] = {}
        self.estop = False
        self.arm_goal = None
        self.gripper_goal = None
        self.hold_pending = False
        self.hold_timer = None
        self.last_seq = -1
        self.last_odom = 0.0
        self.last_gz_zero = 0.0
        self.forwarded = 0
        self.rejected = 0
        self.last_reject = ""

        self.base_pub = self.create_publisher(Twist, "/lelab/base/cmd_vel", 10)
        self.state_pub = self.create_publisher(String, "/lelab/safety_state", 10)
        self.applied_pub = self.create_publisher(String, "/lelab/applied_command", 10)
        self.create_subscription(Empty, "/lelab/heartbeat", self._heartbeat, 10)
        self.create_subscription(Twist, "/lelab/base/cmd_vel_request", self._base_request, 10)
        self.create_subscription(JointTrajectory, "/lelab/arm/trajectory_request", lambda m: self._trajectory("arm", m), 10)
        self.create_subscription(JointTrajectory, "/lelab/gripper/trajectory_request", lambda m: self._trajectory("gripper", m), 10)
        self.create_subscription(Bool, "/lelab/estop", self._estop, 10)
        self.create_subscription(String, "/lelab/safety_reset", self._reset, 10)
        from sensor_msgs.msg import JointState
        self.create_subscription(JointState, "/joint_states", self._joints, 10)
        from nav_msgs.msg import Odometry
        self.create_subscription(Odometry, "/lelab/base/odom", lambda _m: setattr(self, "last_odom", time.monotonic()), 10)
        self.arm_client = ActionClient(self, FollowJointTrajectory, "/arm_controller/follow_joint_trajectory")
        self.gripper_client = ActionClient(self, FollowJointTrajectory, "/gripper_controller/follow_joint_trajectory")
        self.create_timer(0.02, self._tick)
        self.create_timer(0.1, self._publish_state)
        self.get_logger().info("safety watchdog ready (wall-clock TTL, no sim-time freeze)")

    def _heartbeat(self, _msg):
        self.last_heartbeat = time.monotonic()

    def _base_request(self, msg: Twist):
        self.last_base = time.monotonic()
        self.base_target = msg
        seq = getattr(msg, "_lelab_seq", None)
        if seq is not None:
            self.last_seq = int(seq)
        self.forwarded += 1
        self.applied_pub.publish(String(data=json.dumps({
            "kind": "base", "wall_time": time.time(),
            "vx": msg.linear.x, "vy": msg.linear.y, "wz": msg.angular.z,
        })))

    def _joints(self, msg):
        self.joints = dict(zip(msg.name, msg.position))

    def _estop(self, msg):
        if msg.data:
            self.estop = True
            self._stop_and_hold("estop")
        else:
            self.estop = False
            self.get_logger().warn("estop flag cleared without safety_reset")

    def _reset(self, msg):
        age = time.monotonic() - self.last_heartbeat
        if self.estop and age <= self.heartbeat_timeout:
            self.estop = False
            self.hold_pending = False
            self.get_logger().warn(f"estop reset by {msg.data[:80]}")
        elif self.estop:
            self.get_logger().error("safety_reset refused: heartbeat is stale")

    def _clamp(self, names, positions):
        return [max(LIMITS[n][0], min(LIMITS[n][1], float(v))) for n, v in zip(names, positions, strict=True)]

    def _trajectory(self, kind: str, msg: JointTrajectory):
        if self.estop:
            self.rejected += 1
            self.last_reject = f"{kind}: estop latched"
            return
        if time.monotonic() - self.last_heartbeat > self.heartbeat_timeout:
            self.rejected += 1
            self.last_reject = f"{kind}: stale heartbeat"
            return
        expected = ARM_JOINTS if kind == "arm" else GRIPPER_JOINTS
        if list(msg.joint_names) != expected:
            self.rejected += 1
            self.last_reject = f"{kind}: unexpected joints"
            return
        if not msg.points:
            self.rejected += 1
            self.last_reject = f"{kind}: empty trajectory"
            return
        point = msg.points[0]
        point.positions = self._clamp(expected, point.positions)
        goal = FollowJointTrajectory.Goal()
        goal.trajectory = msg
        client = self.arm_client if kind == "arm" else self.gripper_client
        if not client.server_is_ready():
            self.rejected += 1
            self.last_reject = f"{kind}: controller unavailable"
            return
        self.hold_pending = False
        if self.hold_timer is not None:
            self.destroy_timer(self.hold_timer)
            self.hold_timer = None
        if kind == "arm" and self.arm_goal is not None:
            self.arm_goal.cancel_goal_async()
        if kind == "gripper" and self.gripper_goal is not None:
            self.gripper_goal.cancel_goal_async()
        future = client.send_goal_async(goal)
        future.add_done_callback(lambda f, k=kind: self._goal_accepted(k, f))
        self.forwarded += 1
        self.applied_pub.publish(String(data=json.dumps({
            "kind": kind, "wall_time": time.time(),
            "positions": list(point.positions),
            "duration_s": point.time_from_start.sec + point.time_from_start.nanosec / 1e9,
        })))

    def _goal_accepted(self, kind, future):
        try:
            handle = future.result()
        except Exception as exc:  # noqa: BLE001
            self.rejected += 1
            self.last_reject = f"{kind}: goal error {exc}"
            return
        if handle is None or not handle.accepted:
            self.rejected += 1
            self.last_reject = f"{kind}: controller rejected goal"
            return
        if kind == "arm":
            self.arm_goal = handle
        else:
            self.gripper_goal = handle
        handle.get_result_async().add_done_callback(lambda _f, k=kind: self._goal_done(k))

    def _goal_done(self, kind):
        if kind == "arm":
            self.arm_goal = None
        else:
            self.gripper_goal = None

    def _hold(self, kind: str):
        names = ARM_JOINTS if kind == "arm" else GRIPPER_JOINTS
        positions = [self.joints.get(n, 0.0) for n in names]
        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = names
        from trajectory_msgs.msg import JointTrajectoryPoint
        from builtin_interfaces.msg import Duration
        point = JointTrajectoryPoint()
        point.positions = positions
        point.time_from_start = Duration(sec=0, nanosec=500_000_000)
        goal.trajectory.points = [point]
        client = self.arm_client if kind == "arm" else self.gripper_client
        if client.server_is_ready():
            future = client.send_goal_async(goal)
            future.add_done_callback(lambda f, k=kind: self._goal_accepted(k, f))

    def _stop_and_hold(self, reason: str):
        self.base_pub.publish(Twist())
        if self.arm_goal is not None:
            self.arm_goal.cancel_goal_async()
            self.arm_goal = None
        if self.gripper_goal is not None:
            self.gripper_goal.cancel_goal_async()
            self.gripper_goal = None
        if not self.hold_pending:
            self.hold_pending = True
            if self.hold_timer is None:
                self.hold_timer = self.create_timer(0.4, self._delayed_hold)
        self.get_logger().warn(f"motion stopped: {reason}")

    def _delayed_hold(self):
        if self.hold_timer is not None:
            self.destroy_timer(self.hold_timer)
            self.hold_timer = None
        self._hold("arm")
        self._hold("gripper")
        self.hold_pending = False

    def _gz_zero(self):
        """Fallback path that does not depend on the ROS-Gazebo bridge.

        If ros_gz_bridge dies, the ROS zero command cannot reach Gazebo.  The
        Gazebo-side velocity controller would otherwise keep its last target.
        """
        try:
            subprocess.run(
                ["gz", "topic", "-t", "/lelab/base/cmd_vel", "-m", "gz.msgs.Twist",
                 "-p", "linear: {x: 0, y: 0, z: 0} angular: {x: 0, y: 0, z: 0}"],
                capture_output=True, timeout=2.0,
            )
        except Exception as exc:  # noqa: BLE001
            self.get_logger().error(f"gz zero fallback failed: {exc}")

    def _tick(self):
        now = time.monotonic()
        heartbeat_age = now - self.last_heartbeat if self.last_heartbeat else 1e9
        if self.estop or heartbeat_age > self.heartbeat_timeout:
            if self.arm_goal is not None or self.gripper_goal is not None:
                self._stop_and_hold("heartbeat lost")
            self.base_pub.publish(Twist())
            if now - self.last_gz_zero > 1.0:
                self._gz_zero()
                self.last_gz_zero = now
            return
        if self.hold_pending:
            return
        if self.last_odom and now - self.last_odom > 0.8:
            self.base_pub.publish(Twist())
            if now - self.last_gz_zero > 1.0:
                self._gz_zero()
                self.last_gz_zero = now
            self.get_logger().error("base odometry stale; forcing Gazebo zero (bridge likely down)")
            return
        if now - self.last_base > self.base_ttl:
            self.base_pub.publish(Twist())
            if now - self.last_gz_zero > 2.0:
                self._gz_zero()
                self.last_gz_zero = now
            return
        self.base_pub.publish(self.base_target)

    def _publish_state(self):
        heartbeat_age = time.monotonic() - self.last_heartbeat if self.last_heartbeat else None
        self.state_pub.publish(String(data=json.dumps({
            "estop": self.estop,
            "heartbeat_age_s": None if heartbeat_age is None else round(heartbeat_age, 3),
            "last_base_age_s": round(time.monotonic() - self.last_base, 3) if self.last_base else None,
            "arm_goal_active": self.arm_goal is not None,
            "gripper_goal_active": self.gripper_goal is not None,
            "forwarded": self.forwarded,
            "rejected": self.rejected,
            "last_reject": self.last_reject,
            "wall_time": time.time(),
        })))


def main(args=None):
    rclpy.init(args=args)
    node = Watchdog()
    try:
        rclpy.spin(node)
    finally:
        node.base_pub.publish(Twist())
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
