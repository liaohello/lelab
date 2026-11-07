#!/usr/bin/env python3
"""Read-only identification of the servos hanging off the car's control board.

/dev/rrc on the Pi is NOT the servo bus: it is the vendor control-board UART, and the
servos sit behind that board. The board's own protocol (ros_robot_controller_sdk) has
read commands for each servo, so the arm can be identified without starting the vendor
ROS stack and without writing anything: this script only ever sends the command bytes in
READ_ONLY_COMMANDS, never a position/torque/offset/ID write, and never touches the board
functions that would persist settings.

Run it inside the MentorPi container (that is where the vendor SDK lives). From the
workstation:

    tools/ros2_collection/car_servo_readout.py --render
    bash tools/ros2_collection/car_servo_readout.sh

Requirements 三's rules for a protocol without transaction ids are implemented here:
requests are serialized under the SDK's read lock, stale replies are drained and a reply
that does not echo the servo id and command we asked for is discarded and retried, and
every wait is finite so a dead bus cannot hang the caller.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from typing import Any

DEVICE = "/dev/rrc"
BAUDRATE = 1000000

# (board command byte, struct for the reply tail) — every entry is a *read*. The bytes come
# from the vendor SDK's own read helpers (bus_servo_read_id/position/offset/vin/temp/
# torque_state/...), which all go through buf_write(PACKET_FUNC_BUS_SERVO, [cmd, id]).
READ_ONLY_COMMANDS: dict[str, tuple[int, str]] = {
    "id": (0x12, "<BBbB"),
    "position": (0x05, "<BBbh"),
    "offset": (0x22, "<BBbb"),
    "vin": (0x07, "<BBbH"),
    "temp": (0x09, "<BBbB"),
    "torque_state": (0x0D, "<BBbb"),
    "temp_limit": (0x3A, "<BBbB"),
    "angle_limit": (0x32, "<BBb2H"),
    "vin_limit": (0x36, "<BBb2H"),
}
DEFAULT_FIELDS = ("id", "position", "temp", "vin", "torque_state")


def read_servo(board, servo_id: int, field: str, timeout_s: float, retries: int) -> dict[str, Any]:
    """One serialized, finite read that tolerates the board echoing its own request.

    The board puts the echo of what we sent (2 payload bytes: command + servo id) and the
    servo's answer (4+) into the same single-slot queue, and the protocol has no
    transaction id. So: send once, then keep consuming frames until one matches the
    servo and command we asked for. Discarding the queue before the *next* send would
    throw the real answer away with the echo, which is the failure this loop exists for.
    """
    from ros_robot_controller.ros_robot_controller_sdk import PacketFunction

    command, unpack = READ_ONLY_COMMANDS[field]
    wanted = struct.calcsize(unpack)
    attempts: list[str] = []
    with board.servo_read_lock:
        while True:
            try:
                board.bus_servo_queue.get_nowait()
            except Exception:
                break
        board.buf_write(int(PacketFunction.PACKET_FUNC_BUS_SERVO), [command, servo_id])
        deadline = time.monotonic() + timeout_s * (retries + 1)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"value": None, "attempts": attempts or ["timeout"], "raw": None}
            try:
                data = board.bus_servo_queue.get(timeout=remaining)
            except Exception:
                attempts.append("timeout")
                return {"value": None, "attempts": attempts, "raw": None}
            frame = bytes(data)
            if len(frame) != wanted:
                attempts.append(f"echo_or_short({len(frame)}B:{list(frame)})")
                continue
            found_id, found_cmd, status, *info = struct.unpack(unpack, frame)
            if found_cmd != command or found_id != servo_id:
                attempts.append(f"stale_reply(id={found_id},cmd={found_cmd:#04x})")
                continue
            if status != 0:
                # The vendor helper returns the payload only when this byte is 0: it is an
                # error status, not a success flag. Non-zero means the board forwarded the
                # read and the servo did not answer — and the remaining bytes are then
                # whatever the board put there, not a position.
                return {"value": None, "attempts": attempts + [f"servo_error({status})"],
                        "raw": list(frame)}
            return {"value": list(info), "attempts": attempts, "raw": list(frame)}


def port_holders(device: str = DEVICE) -> list[list[str]]:
    """PIDs that already have the board UART open — a second writer means we must not start."""
    import glob
    import os

    real = os.path.realpath(device) if os.path.exists(device) else None
    if not real:
        return []
    me = str(os.getpid())
    holders = []
    for fd in glob.glob("/proc/[0-9]*/fd/*"):
        pid = fd.split("/")[2]
        if pid == me:
            continue
        try:
            if os.path.realpath(fd) == real:
                with open(f"/proc/{pid}/cmdline", "rb") as handle:
                    cmd = handle.read().decode("utf-8", "replace").replace("\0", " ").strip()
                holders.append([pid, cmd[:120]])
        except OSError:
            continue
    return holders


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default=DEVICE)
    parser.add_argument("--ids", nargs="*", type=int, default=[1, 2, 3, 4, 5, 6])
    parser.add_argument("--fields", nargs="*", default=list(DEFAULT_FIELDS),
                        choices=sorted(READ_ONLY_COMMANDS))
    parser.add_argument("--timeout", type=float, default=0.8)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--allow-busy", action="store_true",
                        help="read even if another process holds the port (not recommended)")
    parser.add_argument("--json", help="also write the report here")
    parser.add_argument("--render", action="store_true", help="print how to run this on the car")
    args = parser.parse_args()

    if args.render:
        print("scp car_servo_readout.py user@car-host:/home/pi/docker/tmp/")
        print("ssh user@car-host \"docker exec MentorPi bash -lc "
              "'for f in /opt/ros/humble/setup.bash /home/ubuntu/ros2_ws/install/setup.bash; do "
              "[ -f $f ] && source $f; done; python3 /home/ubuntu/shared/car_servo_readout.py'\"")
        return 0

    unknown = [field for field in args.fields if field not in READ_ONLY_COMMANDS]
    if unknown:
        print(f"REFUSED: {unknown} are not read-only commands", file=sys.stderr)
        return 2

    holders = [] if args.allow_busy else port_holders(args.device)
    if holders:
        print(f"REFUSED: {args.device} is already held by {holders}; "
              "a second reader can interleave with the vendor node's replies", file=sys.stderr)
        return 3

    from ros_robot_controller.ros_robot_controller_sdk import Board

    board = Board(device=args.device, baudrate=BAUDRATE, timeout=args.timeout + 0.5)
    # The SDK's receive thread only drains the UART while this flag is set, and
    # Board.__init__ leaves it False: without this, every read would time out even though
    # the board is answering. It is a local flag only (self.enable_recv = True) — no packet
    # is sent, so nothing reaches the board or the servos.
    board.enable_reception(True)
    report: dict[str, Any] = {"device": args.device, "baudrate": BAUDRATE,
                              "port_holders": holders, "servos": {}, "writes_sent": 0}
    try:
        for servo_id in args.ids:
            row = {}
            for field in args.fields:
                row[field] = read_servo(board, servo_id, field, args.timeout, args.retries)
            report["servos"][str(servo_id)] = row
            answered = sum(1 for field in args.fields if row[field]["value"] is not None)
            print(f"servo {servo_id}: {answered}/{len(args.fields)} "
                  + " ".join(f"{field}={row[field]['value']}" for field in args.fields
                             if row[field]["value"] is not None))
    finally:
        # Stop feeding the parser, then leave the descriptor alone: the SDK's receive thread
        # blocks in select() on it, so closing here only makes that daemon thread raise
        # Bad file descriptor during shutdown. Process exit releases the port.
        board.enable_reception(False)
        time.sleep(0.3)

    responding = [sid for sid, row in report["servos"].items()
                  if any(entry["value"] is not None for entry in row.values())]
    report["responding_ids"] = responding
    report["expected_joints"] = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]
    print(f"\nresponding servo ids: {responding or 'none'}")
    if not responding:
        print("Nothing answered. Either the arm is unpowered, the servos use other ids, or "
              "the board is not forwarding reads. Nothing was written either way.")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        print(f"report: {args.json}")
    return 0 if responding else 4


if __name__ == "__main__":
    sys.exit(main())
