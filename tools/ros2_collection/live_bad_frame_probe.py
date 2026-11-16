#!/usr/bin/env python3
"""T6 bad/replay-frame rejection probe (leader-independent, no output).

Publishes deliberately invalid TeleopFrame and TransportHello messages to the
car's live DDS command topics over the real bus. No auth key is loaded; every
ticket and key id is intentionally wrong, and the frame sequence is replayed.
The car must reject them (frames_rejected grows) without emitting any position
target (frames_emitted stays flat) or consuming a valid stream sequence.

Runs inside the MentorPi container with ROS Humble + the car's DDS env:
  ROS_DOMAIN_ID=0 ROS_LOCALHOST_ONLY=0 RMW=rmw_cyclonedds_cpp
  CYCLONEDDS_URI=file://.../ros2_native_20261108/state/cyclonedds.xml
"""
from __future__ import annotations

import json
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy

from lelab_mobile_interfaces.msg import TeleopFrame, TransportHello

JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"]
# Public key fingerprint from the car control status (NOT the secret key).
KNOWN_KEY_ID = "6022c6ad4d52fae9"
FRAME_TOPIC = "/lelab_ros/command/frame"
HELLO_TOPIC = "/lelab_ros/command/hello"


def main() -> int:
    rclpy.init()
    node = Node("t6_bad_frame_probe")
    best_effort = QoSProfile(reliability=QoSReliabilityPolicy.BEST_EFFORT,
                             history=QoSHistoryPolicy.KEEP_LAST, depth=1)
    reliable = QoSProfile(reliability=QoSReliabilityPolicy.RELIABLE,
                          history=QoSHistoryPolicy.KEEP_LAST, depth=1)
    frame_pub = node.create_publisher(TeleopFrame, FRAME_TOPIC, best_effort)
    hello_pub = node.create_publisher(TransportHello, HELLO_TOPIC, reliable)
    time.sleep(1.0)  # discovery

    hellos = frames = 0
    hello = TransportHello()
    hello.transport = "dds_v1"
    hello.session_id = "t6-fault"
    hello.session_epoch = 1
    hello.calibration_bundle_hash = "x"
    hello.sender_id = "t6-fault-inject"
    hello.auth_key_id = KNOWN_KEY_ID
    hello.auth_ticket = "forged-ticket"
    hello.transport_epoch = "t6"
    for _ in range(3):
        hello_pub.publish(hello)
        hellos += 1
        time.sleep(0.05)

    for _ in range(5):
        frame = TeleopFrame()
        frame.frame_protocol_version = 1
        frame.frame_seq = 1  # replayed sequence number
        frame.stream_epoch = "t6-stream"
        frame.session_id = "t6-fault"
        frame.session_epoch = 1
        frame.calibration_bundle_hash = "x"
        frame.sample_seq = 1
        frame.freshness_window_id = "t6-win"
        frame.sender_id = "t6-fault-inject"
        frame.auth_key_id = KNOWN_KEY_ID
        frame.auth_ticket = "forged-ticket"
        frame.transport_epoch = "t6"
        frame.source = "dds_v1"
        frame.arm_seq = 1
        frame.arm_ttl_ms = 200
        frame.joint_names = JOINTS
        frame.positions_rad = [0.0] * 5
        frame.has_leader_rad = False
        frame.leader_rad = []
        frame.has_pre_clip_rad = False
        frame.pre_clip_rad = []
        frame.arm_clutch = False
        frame.arm_clutch_label = "released"
        frame.gripper_seq = 1
        frame.gripper_ttl_ms = 200
        frame.gripper_joint = "r_joint"
        frame.gripper_rad = 0.0
        frame.gripper_clutch = False
        frame.gripper_clutch_label = "released"
        frame_pub.publish(frame)
        frames += 1
        time.sleep(0.1)

    time.sleep(0.6)
    print(json.dumps({"sent_hellos": hellos, "sent_frames": frames,
                      "frame_topic": FRAME_TOPIC, "hello_topic": HELLO_TOPIC,
                      "note": "all tickets/key ids intentionally invalid; seq replayed"}))
    node.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())