#!/usr/bin/env python3
"""One-joint, low-amplitude servo step with real readback and return-to-origin.

Runs inside the Humble container.  It never opens /dev/rrc: reads go through the
existing controller service and motion goes through the existing servo_controller
publisher.  A failed read, unexpected publisher, timeout or failed return refuses.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time

import rclpy
from rclpy.node import Node
from ros_robot_controller_msgs.msg import GetBusServoCmd
from ros_robot_controller_msgs.srv import GetBusServoState
from servo_controller_msgs.msg import ServoPosition, ServosPosition

IDS = {"joint1": 1, "joint2": 2, "joint3": 3,
       "joint4": 4, "joint5": 5, "r_joint": 10}


def read_raw(node: Node, client, servo_id: int, timeout_s: float) -> int:
    request = GetBusServoState.Request()
    command = GetBusServoCmd()
    command.id = servo_id
    command.get_id = 1
    command.get_position = 1
    request.cmd.append(command)
    future = client.call_async(request)
    rclpy.spin_until_future_complete(node, future, timeout_sec=timeout_s)
    response = future.result()
    if response is None or response.success is not True or len(response.state) != 1:
        raise RuntimeError("readback_failed")
    state = response.state[0]
    if list(state.present_id) != [servo_id] or len(state.position) != 1:
        raise RuntimeError(f"identity_or_position_mismatch:{list(state.present_id)}:{list(state.position)}")
    value = int(state.position[0])
    if not 0 <= value <= 1000:
        raise RuntimeError(f"raw_out_of_range:{value}")
    return value


def send(node: Node, publisher, servo_id: int, position: int, duration_s: float) -> None:
    message = ServosPosition()
    message.duration = float(duration_s)
    message.position_unit = "pulse"
    item = ServoPosition()
    item.id = servo_id
    item.position = float(position)
    message.position.append(item)
    publisher.publish(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--joint", choices=tuple(IDS), required=True)
    parser.add_argument("--delta", type=int, default=8)
    parser.add_argument("--duration", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--i-authorize-motion", action="store_true")
    parser.add_argument("--json", default="")
    args = parser.parse_args()
    if not args.i_authorize_motion:
        print("REFUSED: pass --i-authorize-motion after confirming support, clearance and stop access", file=sys.stderr)
        return 2
    if not 1 <= abs(args.delta) <= 12:
        print("REFUSED: abs(delta) must be 1..12 raw ticks", file=sys.stderr)
        return 2
    if not math.isfinite(args.duration) or not 1.0 <= args.duration <= 2.0:
        print("REFUSED: duration must be 1.0..2.0 s", file=sys.stderr)
        return 2

    rclpy.init()
    node = Node("lelab_servo_step_probe")
    client = node.create_client(GetBusServoState, "/ros_robot_controller/bus_servo/get_state")
    publisher = node.create_publisher(ServosPosition, "/servo_controller", 1)
    report = {"joint": args.joint, "servo_id": IDS[args.joint],
              "requested_delta_raw": args.delta, "duration_s": args.duration,
              "success": False, "motion_returned_to_origin": False}
    try:
        if not client.wait_for_service(timeout_sec=5.0):
            raise RuntimeError("servo_read_service_unavailable")
        time.sleep(0.3)
        publishers = node.get_publishers_info_by_topic("/servo_controller")
        names = [info.node_name for info in publishers]
        if names != ["lelab_servo_step_probe"]:
            raise RuntimeError(f"servo_controller_not_exclusive:{names}")
        before = read_raw(node, client, IDS[args.joint], args.timeout)
        target = before + args.delta
        if not 25 <= target <= 975:
            raise RuntimeError(f"target_outside_probe_envelope:{target}")
        report.update(before_raw=before, target_raw=target)
        send(node, publisher, IDS[args.joint], target, args.duration)
        time.sleep(args.duration + 0.6)
        after = read_raw(node, client, IDS[args.joint], args.timeout)
        report["after_raw"] = after
        report["observed_delta_raw"] = after - before
        report["direction"] = 1 if after > before else -1 if after < before else 0
        if after == before:
            raise RuntimeError("no_observed_motion")
        report["success"] = True
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if "before" in locals():
                send(node, publisher, IDS[args.joint], before, args.duration)
                time.sleep(args.duration + 0.6)
                returned = read_raw(node, client, IDS[args.joint], args.timeout)
                report["returned_raw"] = returned
                # A gravity-loaded joint can stop a few ticks short of the requested
                # position while still holding. Six ticks is 0.025 rad, below one
                # third of this probe's maximum 12-tick motion.
                report["motion_returned_to_origin"] = abs(returned - before) <= 6
        except Exception as exc:  # noqa: BLE001
            report["return_error"] = f"{type(exc).__name__}: {exc}"
        node.destroy_node()
        rclpy.shutdown()
    print(json.dumps(report, indent=2))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
    return 0 if report["success"] and report["motion_returned_to_origin"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
