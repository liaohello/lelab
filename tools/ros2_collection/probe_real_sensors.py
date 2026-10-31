#!/usr/bin/env python3
"""Passive sensor probe for the Pi read-only phase.

Creates subscriptions only: no publishers, services, action clients, parameter
writes or actuator-related nodes.  Reports message type, publisher QoS, receive
span/rate, header stamps and image metadata as JSON for later comparison.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CameraInfo, Image, Imu, JointState, LaserScan
from nav_msgs.msg import Odometry

TYPES = {
    "/ascamera/camera_publisher/rgb0/image": Image,
    "/ascamera/camera_publisher/rgb0/camera_info": CameraInfo,
    "/ascamera/camera_publisher/depth0/image_raw": Image,
    "/ascamera/camera_publisher/depth0/camera_info": CameraInfo,
    "/imu": Imu,
    "/imu_corrected": Imu,
    "/joint_states": JointState,
    "/controller_manager/joint_states": JointState,
    "/odom": Odometry,
    "/odom_raw": Odometry,
    "/scan": LaserScan,
    "/scan_raw": LaserScan,
    # Simulation names are kept so the same passive probe can be rehearsed
    # locally before it is run against the Pi.
    "/lelab/wrist/rgb/image_raw": Image,
    "/lelab/wrist/depth/image_raw": Image,
    "/lelab/wrist/rgb/camera_info": CameraInfo,
    "/lelab/base/model_odom": Odometry,
    "/lelab/base/odom": Odometry,
    "/lelab/recording/joint_states": JointState,
    "/lelab/lidar/scan": LaserScan,
    "/lelab/imu": Imu,
}


def stamp_ns(msg) -> int:
    stamp = getattr(getattr(msg, "header", None), "stamp", None)
    if stamp is None:
        return 0
    return int(stamp.sec * 1e9 + stamp.nanosec)


class Probe(Node):
    def __init__(self, topics: list[str], duration: float, reliability: str = "auto"):
        super().__init__("lelab_passive_sensor_probe")
        self.duration = duration
        self.reliability = reliability
        self.group = ReentrantCallbackGroup()
        self.records = {
            topic: {"topic": topic, "count": 0, "receive_wall": [], "header_ns": [], "metadata": {}, "seen": set()}
            for topic in topics
        }
        for topic in topics:
            msg_type = TYPES.get(topic)
            if msg_type is None:
                self.records[topic]["error"] = "unsupported_topic_type"
                continue
            # Match the offered reliability so the probe works with both the
            # Gazebo bridge (reliable) and best-effort camera drivers.
            if self.reliability == "reliable":
                offered_reliable = True
            elif self.reliability == "best_effort":
                offered_reliable = False
            else:
                try:
                    endpoints = self.get_publishers_info_by_topic(topic)
                    offered_reliable = any(
                        endpoint.qos_profile.reliability == ReliabilityPolicy.RELIABLE
                        for endpoint in endpoints
                    )
                except Exception:
                    offered_reliable = False
            reliabilities = [ReliabilityPolicy.RELIABLE if offered_reliable else ReliabilityPolicy.BEST_EFFORT]
            if self.reliability == "auto" and msg_type is not JointState:
                # Graph discovery may not have completed at subscription time.
                # Listening on both policies avoids a false missing stream.
                reliabilities = [ReliabilityPolicy.RELIABLE, ReliabilityPolicy.BEST_EFFORT]
            for reliability in reliabilities:
                qos = QoSProfile(
                    reliability=reliability,
                    durability=DurabilityPolicy.VOLATILE,
                    history=HistoryPolicy.KEEP_LAST,
                    depth=10,
                )
                self.create_subscription(msg_type, topic, lambda msg, t=topic: self.callback(t, msg), qos, callback_group=self.group)
        self.end = time.monotonic() + duration

    def callback(self, topic: str, msg) -> None:
        rec = self.records[topic]
        key = (stamp_ns(msg), len(getattr(msg, "data", b"")) if hasattr(msg, "data") else 0)
        if key != (0, 0) and key in rec["seen"]:
            return
        rec["seen"].add(key)
        rec["count"] += 1
        rec["receive_wall"].append(time.time())
        rec["header_ns"].append(stamp_ns(msg))
        if isinstance(msg, Image):
            rec["metadata"] = {
                "width": msg.width, "height": msg.height, "encoding": msg.encoding,
                "step": msg.step, "frame_id": msg.header.frame_id,
            }
        elif isinstance(msg, CameraInfo):
            rec["metadata"] = {
                "width": msg.width, "height": msg.height, "frame_id": msg.header.frame_id,
                "distortion_model": msg.distortion_model,
            }
        elif isinstance(msg, JointState):
            rec["metadata"] = {
                "joint_names": list(msg.name),
                "has_position": len(msg.position) == len(msg.name),
                "has_velocity": len(msg.velocity) == len(msg.name),
                "has_effort": len(msg.effort) == len(msg.name),
            }
        elif isinstance(msg, Imu):
            rec["metadata"] = {"frame_id": msg.header.frame_id}
        elif isinstance(msg, LaserScan):
            rec["metadata"] = {"frame_id": msg.header.frame_id, "ranges": len(msg.ranges)}
        elif isinstance(msg, Odometry):
            rec["metadata"] = {"frame_id": msg.header.frame_id, "child_frame_id": msg.child_frame_id}


def summarize(probe: Probe) -> dict:
    output = {}
    for topic, rec in probe.records.items():
        receive = rec["receive_wall"]
        span = receive[-1] - receive[0] if len(receive) > 1 else None
        rate = (len(receive) - 1) / span if span and span > 0 else None
        headers = [v for v in rec["header_ns"] if v > 0]
        endpoint_qos = []
        try:
            for endpoint in probe.get_publishers_info_by_topic(topic):
                q = endpoint.qos_profile
                endpoint_qos.append({
                    "reliability": q.reliability.name,
                    "durability": q.durability.name,
                    "history": q.history.name,
                    "depth": q.depth,
                })
        except Exception as exc:  # noqa: BLE001
            endpoint_qos = [{"error": str(exc)}]
        output[topic] = {
            "count": rec["count"],
            "receive_span_s": round(span, 4) if span is not None else None,
            "rate_hz_receiver_side": round(rate, 3) if rate is not None else None,
            "first_header_ns": headers[0] if headers else None,
            "last_header_ns": headers[-1] if headers else None,
            "header_stamp_span_s": round((headers[-1] - headers[0]) / 1e9, 4) if len(headers) > 1 else None,
            "publisher_qos": endpoint_qos,
            "metadata": rec.get("metadata") or {},
            "error": rec.get("error"),
        }
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--output", default="")
    parser.add_argument("--topic", action="append", default=[])
    parser.add_argument("--use-sim-time", action="store_true")
    parser.add_argument("--reliability", choices=("auto", "reliable", "best_effort"), default="auto")
    args = parser.parse_args()
    topics = args.topic or list(TYPES)
    init_args = ["--ros-args", "-p", "use_sim_time:=true"] if args.use_sim_time else None
    rclpy.init(args=init_args)
    probe = Probe(topics, args.duration, args.reliability)
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(probe)
    worker = threading.Thread(target=executor.spin, daemon=True)
    worker.start()
    time.sleep(args.duration)
    executor.remove_node(probe)
    executor.shutdown()
    worker.join(timeout=5.0)
    result = {
        "schema_version": 1,
        "probe": "lelab_passive_sensor_probe",
        "duration_s": args.duration,
        "use_sim_time": args.use_sim_time,
        "topics": summarize(probe),
        "note": "Passive subscriptions only; actuator publishers/clients were not created.",
    }
    probe.destroy_node()
    rclpy.shutdown()
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
