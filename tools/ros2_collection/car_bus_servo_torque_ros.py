#!/usr/bin/env python3
"""Set and verify bus-servo torque through the existing ROS board owner.

This tool never opens a serial port. It writes one servo at a time through
``/ros_robot_controller/bus_servo/set_state`` and then reads the selected state
back through ``/ros_robot_controller/bus_servo/get_state``. It is intended for a
quiescent, exclusive arm stack: no hybrid control session and no velocity/position
producer should be active.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from typing import Any

import rclpy

SET_TOPIC = "/ros_robot_controller/bus_servo/set_state"
GET_SERVICE = "/ros_robot_controller/bus_servo/get_state"
DEFAULT_IDS = (1, 2, 3, 4, 5, 10)


def wait_for_subscriber(node: Any, publisher: Any, timeout_s: float) -> bool:
    """Wait for DDS discovery so the first state write is not silently dropped."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.05)
        if publisher.get_subscription_count() > 0:
            return True
    return publisher.get_subscription_count() > 0


def read_one(node: Any, client, servo_id: int, timeout_s: float,
             GetBusServoCmd, GetBusServoState) -> dict:
    request = GetBusServoState.Request()
    command = GetBusServoCmd()
    command.id = servo_id
    command.get_id = 1
    command.get_position = 1
    command.get_torque_state = 1
    request.cmd.append(command)
    future = client.call_async(request)
    rclpy.spin_until_future_complete(node, future, timeout_sec=timeout_s)
    response = future.result()
    if response is None or not response.success or len(response.state) != 1:
        return {"verified": False, "reason": "read_failed", "servo_id": servo_id}
    state = response.state[0]
    present = list(state.present_id)
    positions = list(state.position)
    torques = list(state.enable_torque)
    if present != [servo_id] or len(positions) != 1 or len(torques) != 1:
        return {"verified": False, "reason": "readback_identity_mismatch",
                "servo_id": servo_id, "present_id": present,
                "position": positions, "enable_torque": torques}
    return {"verified": True, "servo_id": servo_id,
            "position": positions[0], "enable_torque": torques[0]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", nargs="+", type=int, default=list(DEFAULT_IDS))
    parser.add_argument("--load", action="store_true")
    parser.add_argument("--unload", action="store_true")
    parser.add_argument("--i-authorize-servo-state-writes", action="store_true")
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--json", default="")
    args = parser.parse_args()
    if args.load == args.unload:
        print("REFUSED: choose exactly one of --load or --unload", file=sys.stderr)
        return 2
    if not args.i_authorize_servo_state_writes:
        print("REFUSED: pass --i-authorize-servo-state-writes", file=sys.stderr)
        return 2
    if any(servo_id not in DEFAULT_IDS for servo_id in args.ids):
        print("REFUSED: unsupported servo id", file=sys.stderr)
        return 2
    try:
        with urllib.request.urlopen("http://127.0.0.1:8110/status", timeout=2.0) as response:
            status = json.loads(response.read().decode())
        if ((status.get("control") or {}).get("active") is not False
                or (status.get("licence") or {}).get("frame_outputs_enabled") is not False):
            print("REFUSED: release the hybrid session and frame output first",
                  file=sys.stderr)
            return 3
    except Exception as exc:  # noqa: BLE001
        print(f"REFUSED: cannot prove the arm stack is quiescent: {exc}", file=sys.stderr)
        return 3

    expected = 1 if args.load else 0
    from rclpy.node import Node
    from ros_robot_controller_msgs.msg import (
        BusServoState,
        GetBusServoCmd,
        SetBusServoState,
    )
    from ros_robot_controller_msgs.srv import GetBusServoState

    report = {"action": "load" if args.load else "unload",
              "ids": args.ids, "expected_torque": expected, "rows": {}}
    rclpy.init()
    node = Node("lelab_ros_torque_verify")
    publisher = node.create_publisher(SetBusServoState, SET_TOPIC, 10)
    client = node.create_client(GetBusServoState, GET_SERVICE)
    try:
        if not client.wait_for_service(timeout_sec=5.0):
            raise RuntimeError("servo_state_service_unavailable")
        if not wait_for_subscriber(node, publisher, 5.0):
            raise RuntimeError("servo_state_subscriber_unavailable")
        for servo_id in args.ids:
            message = SetBusServoState()
            message.duration = 0.15
            state = BusServoState()
            state.present_id = [1, servo_id]
            state.enable_torque = [1, expected]
            message.state.append(state)
            publisher.publish(message)
            time.sleep(0.12)
            row = read_one(node, client, servo_id, args.timeout,
                           GetBusServoCmd, GetBusServoState)
            row["requested_enable_torque"] = bool(expected)
            report["rows"][str(servo_id)] = row
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        node.destroy_node()
        rclpy.shutdown()
    report["ok"] = bool(
        "error" not in report and
        len(report["rows"]) == len(args.ids) and
        all(row.get("verified") is True and row.get("enable_torque") == expected
            for row in report["rows"].values())
    )
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
