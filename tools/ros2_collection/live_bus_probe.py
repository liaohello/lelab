#!/usr/bin/env python3
"""Read-only real-bus baseline collector. Creates subscriptions only, no publishers.

Records the owner's v2 measured frames and driver health, plus the control
process consumer counters, then summarizes rate/interval/span/failures. It never
opens a serial port and never commands torque or position.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import rclpy
from rclpy.node import Node
from std_msgs.msg import Int32MultiArray, String

JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]
IDS = [1, 2, 3, 4, 5, 10]


class Probe(Node):
    def __init__(self, args):
        super().__init__("lelab_live_bus_probe")
        self.args = args
        self.frames = []
        self.health = []
        self.create_subscription(Int32MultiArray, args.readback_topic, self._on_frame, 10)
        self.create_subscription(String, args.health_topic, self._on_health, 10)

    def _on_frame(self, msg):
        data = list(msg.data)
        now = time.monotonic_ns()
        if len(data) == 16 and data[0] == 2:
            pairs = data[4:]
            raw = {JOINTS[i]: pairs[2 * i + 1] for i in range(6)}
            self.frames.append({"recv_mono_ns": now, "owner": data[1], "seq": data[2],
                                "scan_us": data[3], "raw": raw,
                                "ids": [pairs[2 * i] for i in range(6)]})
        else:
            self.frames.append({"recv_mono_ns": now, "invalid_envelope": data[:4]})

    def _on_health(self, msg):
        try:
            doc = json.loads(msg.data)
        except ValueError:
            return
        doc["recv_mono_ns"] = time.monotonic_ns()
        self.health.append(doc)


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--readback-topic", default="/lelab/ros_robot_controller/servo_readback")
    parser.add_argument("--health-topic", default="/lelab/ros_robot_controller/driver_health")
    parser.add_argument("--control-status", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rclpy.init()
    node = Probe(args)
    started = time.monotonic()
    control_before = control_after = None
    if args.control_status:
        try:
            control_before = json.loads(Path(args.control_status).read_text())
        except (OSError, ValueError):
            control_before = None
    try:
        while time.monotonic() - started < args.duration:
            rclpy.spin_once(node, timeout_sec=0.2)
    finally:
        if args.control_status:
            try:
                control_after = json.loads(Path(args.control_status).read_text())
            except (OSError, ValueError):
                control_after = None
        node.destroy_node()
        rclpy.shutdown()
    elapsed = time.monotonic() - started
    with (out / "bus-readonly.jsonl").open("w") as fh:
        for frame in node.frames:
            fh.write(json.dumps(frame, sort_keys=True) + "\n")
    valid = [f for f in node.frames if "seq" in f]
    recv = [f["recv_mono_ns"] for f in valid]
    intervals_ms = [(b - a) / 1e6 for a, b in zip(recv, recv[1:])]
    spans_ms = [f["scan_us"] / 1000.0 for f in valid]
    seqs = [f["seq"] for f in valid]
    owners = sorted({f["owner"] for f in valid})
    seq_gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if b != a + 1)
    last_health = node.health[-1] if node.health else {}
    first_health = node.health[0] if node.health else {}
    emitted_before = ((control_before or {}).get("counters") or {}).get("frames_emitted")
    emitted_after = ((control_after or {}).get("counters") or {}).get("frames_emitted")
    consumer_after = ((control_after or {}).get("feedback") or {}).get("consumer") or {}
    summary = {
        "id": "T2", "hardware_or_simulated": "hardware",
        "duration_s": round(elapsed, 3),
        "frames_total": len(node.frames), "frames_valid": len(valid),
        "frames_invalid_envelope": len(node.frames) - len(valid),
        "effective_hz": round(len(valid) / elapsed, 3) if elapsed else None,
        "owners": owners, "seq_min": min(seqs) if seqs else None,
        "seq_max": max(seqs) if seqs else None, "seq_gaps": seq_gaps,
        "interval_ms": {"p50": percentile(intervals_ms, 0.5),
                        "p95": percentile(intervals_ms, 0.95),
                        "max": max(intervals_ms) if intervals_ms else None},
        "scan_span_ms": {"p50": percentile(spans_ms, 0.5),
                         "p95": percentile(spans_ms, 0.95),
                         "max": max(spans_ms) if spans_ms else None},
        "owner_health_last": {k: last_health.get(k) for k in
                              ("scans_ok", "scans_failed", "consecutive_failures",
                               "max_scan_interval_s", "first_failure", "last_failure",
                               "last_recovery", "write_failures", "stale")},
        "owner_first_failure": first_health.get("first_failure"),
        "consumer_after": consumer_after,
        "position_output": {"frames_emitted_before": emitted_before,
                            "frames_emitted_after": emitted_after,
                            "delta": (None if emitted_before is None or emitted_after is None
                                      else emitted_after - emitted_before)},
        "health_messages": len(node.health),
    }
    (out / "bus-summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    (out / "driver-first-failure.json").write_text(
        json.dumps({"owner_first_failure": first_health.get("first_failure"),
                    "owner_last_failure": last_health.get("last_failure"),
                    "consumer_first_failure": consumer_after.get("first_failure")},
                   indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
