#!/usr/bin/env python3
"""OFFLINE ONLY - simulated control-board owner for the local Humble runtime.

Stand-in for the vendor ``ros_robot_controller`` node, which is the single owner of
``/dev/rrc`` on the car.  Like the real owner it accepts one six-axis
``ServosPosition`` message, answers per-ID torque writes and the ``get_state``
service, and publishes a sequential measured readback scan. It is the only place in
this repository that invents servo positions instead of reading them.

It never imports a serial module, never uses the real topic names (the launcher
remaps everything under ``/lelab_offline``), and stamps every state file with
``data_identity=simulated_offline`` so nothing produced here can be mistaken for a
measurement.

The readback envelope is v2 ``[2, owner_instance, seq, scan_us, id,pos x6]``. The
owner_instance is a software id drawn once at startup (not an MCU boot ID); the
``--owner-restart-after-s`` knob draws a new one and resets the sequence to exercise
consumer revocation.

Fault knobs (all default off):
  --scan-period-s         how long a full six-servo scan takes/should look like
  --stall-after-s         stop publishing readback after N seconds (stale feedback)
  --reject-commands       accept the position topic but move nothing
  --extra-servo-id        emit an unexpected id, to prove the consumer refuses
  --torque-fail-id        report this id as unloaded even after a load write
  --owner-restart-after-s simulate a publisher restart (new owner instance, seq=0)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import signal
import sys
import threading
import time
from pathlib import Path

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import Int32MultiArray

from ros_robot_controller_msgs.msg import (
    BusServoState, ServoPosition, ServosPosition, SetBusServoState,
)
from ros_robot_controller_msgs.srv import GetBusServoState

SERVO_IDS = (1, 2, 3, 4, 5, 10)
NAMES = {1: "joint1", 2: "joint2", 3: "joint3", 4: "joint4", 5: "joint5", 10: "r_joint"}
RADIANS_PER_TICK = 240 / 360 * (2 * math.pi) / 1000.0
DATA_IDENTITY = "simulated_offline"
READBACK_FORMAT_V2 = 2


def new_owner_instance() -> int:
    return secrets.randbits(31) | 1


class SimBoardOwner(Node):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("lelab_sim_board_owner")
        self.args = args
        self.lock = threading.Lock()
        self.raw = {servo_id: 500 for servo_id in SERVO_IDS}
        if args.initial_raw:
            name_to_id = {name: servo_id for servo_id, name in NAMES.items()}
            for item in str(args.initial_raw).split(","):
                key, _, value = item.partition("=")
                key, value = key.strip(), value.strip()
                if key not in name_to_id or not value.lstrip("-").isdigit():
                    raise SystemExit(f"invalid --initial-raw item: {item!r}")
                self.raw[name_to_id[key]] = max(0, min(1000, int(value)))
        self.target = dict(self.raw)
        self.torque = {servo_id: 0 for servo_id in SERVO_IDS}
        self.owner_instance = new_owner_instance()
        self.owner_restarts = 0
        self.sequence = 0
        self.outputs_received = 0
        self.positions_applied = 0
        self.commands_rejected = 0
        self.torque_writes = 0
        self.torque_writes_rejected = 0
        self.last_command_mono: float | None = None
        self.last_command: dict[str, object] | None = None
        self.started = time.monotonic()
        qos = QoSProfile(reliability=QoSReliabilityPolicy.RELIABLE,
                        history=QoSHistoryPolicy.KEEP_LAST, depth=1)
        self.create_subscription(ServosPosition, args.output_topic, self._on_command, qos)
        self.create_subscription(SetBusServoState, args.state_topic, self._on_state, 10)
        self.create_service(GetBusServoState, args.state_service, self._get_state)
        self.readback = self.create_publisher(Int32MultiArray, args.readback_topic, qos)
        self.create_timer(args.scan_period_s, self._scan)
        self.create_timer(0.2, self._write_state)

    def _on_command(self, message: ServosPosition) -> None:
        ids = [int(item.id) for item in message.position]
        with self.lock:
            self.outputs_received += 1
            self.last_command_mono = time.monotonic()
            self.last_command = {
                "ids": ids,
                "positions": [int(item.position) for item in message.position],
                "duration_s": float(message.duration),
                "sequence": self.outputs_received,
            }
            if self.args.reject_commands:
                self.commands_rejected += 1
                return
            unknown = [servo_id for servo_id in ids if servo_id not in self.raw]
            if unknown:
                self.commands_rejected += 1
                return
            for item in message.position:
                self.target[int(item.id)] = int(item.position)
                self.positions_applied += 1

    def _on_state(self, message: SetBusServoState) -> None:
        with self.lock:
            for state in message.state:
                present = list(state.present_id)
                enable = list(state.enable_torque)
                if (len(present) != 2 or present[0] != 1
                        or len(enable) != 2 or enable[0] != 1
                        or enable[1] not in (0, 1)
                        or present[1] not in self.torque):
                    self.torque_writes_rejected += 1
                    continue
                self.torque[present[1]] = int(enable[1])
                self.torque_writes += 1

    def _get_state(self, request, response):
        with self.lock:
            for cmd in request.cmd:
                servo_id = int(cmd.id)
                state = BusServoState()
                if servo_id not in self.raw:
                    response.success = False
                    return response
                if cmd.get_id:
                    state.present_id = [servo_id]
                if cmd.get_position:
                    state.position = [int(self.raw[servo_id]) & 0xFFFF]
                if cmd.get_torque_state:
                    reported = self.torque[servo_id]
                    if self.args.torque_fail_id == servo_id:
                        reported = 0
                    state.enable_torque = [reported]
                response.state.append(state)
        response.success = True
        return response

    def _scan(self) -> None:
        with self.lock:
            elapsed = time.monotonic() - self.started
            if self.args.stall_after_s and elapsed > self.args.stall_after_s:
                return
            if (self.args.owner_restart_after_s
                    and elapsed > self.args.owner_restart_after_s
                    and self.owner_restarts == 0):
                self.owner_instance = new_owner_instance()
                self.sequence = 0
                self.owner_restarts += 1
            span_us = int(self.args.scan_period_s * 1_000_000)
            ids = list(SERVO_IDS)
            if self.args.extra_servo_id:
                ids = ids + [int(self.args.extra_servo_id)]
            for servo_id in ids:
                if servo_id not in self.raw:
                    continue
                # First-order approach: the board moves toward the last target.
                step = max(1, int(abs(self.target[servo_id] - self.raw[servo_id]) * 0.4)) + 1
                delta = self.target[servo_id] - self.raw[servo_id]
                self.raw[servo_id] += max(-step, min(step, delta))
            self.sequence += 1
            payload = [READBACK_FORMAT_V2, self.owner_instance, self.sequence, span_us]
            for servo_id in ids:
                payload.extend([servo_id, int(self.raw.get(servo_id, 0))])
        message = Int32MultiArray()
        message.data = [int(value) for value in payload]
        self.readback.publish(message)

    def _write_state(self) -> None:
        path = Path(os.environ.get("LROS_SIM_STATE", "")) if os.environ.get("LROS_SIM_STATE") \
            else Path(self.args.state_path)
        with self.lock:
            document = {
                "data_identity": DATA_IDENTITY,
                "training_eligible": False,
                "hardware_effect": "simulated_only",
                "node": "lelab_sim_board_owner",
                "uptime_s": round(time.monotonic() - self.started, 3),
                "owner_instance": self.owner_instance,
                "owner_restarts": self.owner_restarts,
                "outputs_received": self.outputs_received,
                "positions_applied": self.positions_applied,
                "commands_rejected": self.commands_rejected,
                "torque_writes": self.torque_writes,
                "torque_writes_rejected": self.torque_writes_rejected,
                "torque": {NAMES[servo_id]: self.torque[servo_id] for servo_id in SERVO_IDS},
                "readback_sequence": self.sequence,
                "scan_period_s": self.args.scan_period_s,
                "raw": {NAMES[servo_id]: self.raw[servo_id] for servo_id in SERVO_IDS
                        if servo_id in self.raw},
                "target": {NAMES[servo_id]: self.target[servo_id] for servo_id in SERVO_IDS
                           if servo_id in self.target},
                "last_command": self.last_command,
                "faults": {"reject_commands": bool(self.args.reject_commands),
                           "stall_after_s": self.args.stall_after_s,
                           "extra_servo_id": self.args.extra_servo_id,
                           "torque_fail_id": self.args.torque_fail_id,
                           "owner_restart_after_s": self.args.owner_restart_after_s},
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-topic", default="/lelab_offline/ros_robot_controller/bus_servo/set_position")
    parser.add_argument("--state-topic", default="/lelab_offline/ros_robot_controller/bus_servo/set_state")
    parser.add_argument("--state-service", default="/lelab_offline/ros_robot_controller/bus_servo/get_state")
    parser.add_argument("--readback-topic", default="/lelab_offline/lelab/ros_robot_controller/servo_readback")
    parser.add_argument("--scan-period-s", type=float, default=0.2)
    parser.add_argument("--stall-after-s", type=float, default=0.0)
    parser.add_argument("--reject-commands", action="store_true")
    parser.add_argument("--extra-servo-id", type=int, default=0)
    parser.add_argument("--torque-fail-id", type=int, default=0)
    parser.add_argument("--owner-restart-after-s", type=float, default=0.0)
    parser.add_argument("--initial-raw", default="",
                        help="e.g. joint1=527,joint2=632 (clamped 0-1000)")
    parser.add_argument("--state-path", default="/out/state/sim_board_owner.json")
    args = parser.parse_args()
    if "serial" in sys.modules:
        print("refusing to start: a serial module is already loaded", file=sys.stderr)
        return 2
    rclpy.init()
    node = SimBoardOwner(args)
    signal.signal(signal.SIGTERM, lambda *_: rclpy.shutdown())
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, RuntimeError):
        pass
    finally:
        node.destroy_node()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())