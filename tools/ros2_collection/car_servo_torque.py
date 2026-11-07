#!/usr/bin/env python3
"""Load / unload a car bus servo's torque, with read-back verification.

Why this exists: "the joint is stuck at its limit" is a conclusion, not an observation.
On 2026-11-04 joint1 read `position=500` (dead centre of 0..1000), `angle_limit=[0,1000]`
(full range) and `torque_state=1`: there was no limit to release — the joint was simply
being held by servo torque, which feels exactly like a mechanical stop when you push it.

The vendor firmware (RosRobotControllerLite `packet_handle.c`) defines the two commands used
here:

    0x0B  unload torque  -> serial_servo_load_unload(..., 0)
    0x0C  load torque    -> serial_servo_load_unload(..., 1)
    0x0D  read torque    -> serial_servo_read_load_unload(...)

Torque is per servo. Unloading one joint never touches the others, so a yaw joint (joint1,
URDF axis ~= Z) can be freed for a hand test while the shoulder/elbow joints keep holding
the arm up against gravity. Unloading a gravity-loaded joint WILL drop the arm: check the
URDF axis before using this on anything but joint1.

This talks to the bus directly, so it refuses to run while another process holds the
control-board UART. That is not a formality: the vendor SDK's own read path
(`bus_servo_read_and_unpack`) does `bus_servo_queue.get(block=True)` with no timeout and no
frame validation, so a second writer interleaving replies can hang or kill that node.

Usage (inside the MentorPi container):
    python3 car_servo_torque.py --ids 1                      # read torque only, no writes
    python3 car_servo_torque.py --ids 1 --unload --i-authorize-servo-writes
    python3 car_servo_torque.py --ids 1 --load   --i-authorize-servo-writes
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
CMD_TORQUE_STATE = 0x0D
CMD_UNLOAD = 0x0B
CMD_LOAD = 0x0C


def write_servo(board, servo_id: int, command: int) -> dict[str, Any]:
    """Send a no-reply write. The firmware does not answer 0x0B/0x0C, so nothing to match."""
    from ros_robot_controller.ros_robot_controller_sdk import PacketFunction

    with board.servo_read_lock:
        board.buf_write(int(PacketFunction.PACKET_FUNC_BUS_SERVO), [command, servo_id])
    return {"sent": True, "command": command, "servo_id": servo_id}


def read_torque(board, servo_id: int, timeout_s: float, retries: int) -> dict[str, Any]:
    """Read 0x0D, tolerating the board's own echo and any stale reply in the queue.

    Mirrors car_servo_readout.py: the protocol has no transaction id, so the only safe
    match is (servo id, command, status==0) plus the exact frame length.
    """
    from ros_robot_controller.ros_robot_controller_sdk import PacketFunction

    unpack = "<BBbb"
    wanted = struct.calcsize(unpack)
    attempts: list[str] = []
    with board.servo_read_lock:
        while True:
            try:
                board.bus_servo_queue.get_nowait()
            except Exception:
                break
        board.buf_write(int(PacketFunction.PACKET_FUNC_BUS_SERVO), [CMD_TORQUE_STATE, servo_id])
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
            found_id, found_cmd, status, load = struct.unpack(unpack, frame)
            if found_cmd != CMD_TORQUE_STATE or found_id != servo_id:
                attempts.append(f"stale_reply(id={found_id},cmd={found_cmd:#04x})")
                continue
            if status != 0:
                return {"value": None, "attempts": attempts + [f"servo_error({status})"],
                        "raw": list(frame)}
            return {"value": load, "attempts": attempts, "raw": list(frame)}


def port_holders(device: str) -> list[list[str]]:
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
    parser.add_argument("--ids", nargs="+", type=int)
    parser.add_argument("--unload", action="store_true", help="0x0B: free the joint")
    parser.add_argument("--load", action="store_true", help="0x0C: hold the joint again")
    parser.add_argument("--i-authorize-servo-writes", action="store_true",
                        help="required for --unload/--load; without it this is read-only")
    parser.add_argument("--timeout", type=float, default=0.8)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--json", help="also write the report here")
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()

    if args.render:
        print("scp car_servo_torque.py pi@<car>:/home/pi/docker/tmp/")
        print("ssh pi@<car> \"docker exec -u ubuntu MentorPi bash -lc "
              "'python3 /home/ubuntu/shared/car_servo_torque.py --ids 1 "
              "--unload --i-authorize-servo-writes'\"")
        return 0

    if args.unload and args.load:
        print("REFUSED: --unload and --load are mutually exclusive", file=sys.stderr)
        return 2
    if not args.ids:
        print("REFUSED: --ids is required", file=sys.stderr)
        return 2
    writes = args.unload or args.load
    if writes and not args.i_authorize_servo_writes:
        print("REFUSED: this changes servo state; pass --i-authorize-servo-writes",
              file=sys.stderr)
        return 2

    holders = port_holders(args.device)
    if holders:
        print(f"REFUSED: {args.device} is held by {holders}; the vendor node owns the bus",
              file=sys.stderr)
        return 3

    from ros_robot_controller.ros_robot_controller_sdk import Board

    board = Board(device=args.device, baudrate=BAUDRATE, timeout=args.timeout + 0.5)
    board.enable_reception(True)
    report: dict[str, Any] = {"device": args.device, "baudrate": BAUDRATE,
                              "action": "unload" if args.unload else "load" if args.load else "read",
                              "servos": {}}
    try:
        for servo_id in args.ids:
            row: dict[str, Any] = {"before": read_torque(board, servo_id, args.timeout,
                                                         args.retries)}
            if writes:
                row["write"] = write_servo(board, servo_id,
                                           CMD_UNLOAD if args.unload else CMD_LOAD)
                time.sleep(0.15)
                row["after"] = read_torque(board, servo_id, args.timeout, args.retries)
            expect = 0 if args.unload else 1
            after = row.get("after", row["before"]).get("value")
            row["verified"] = (after == expect) if writes else None
            report["servos"][str(servo_id)] = row
            print(f"servo {servo_id}: before={row['before']['value']}"
                  + (f" after={row.get('after', {}).get('value')} "
                     f"verified={row['verified']}" if writes else ""))
    finally:
        board.enable_reception(False)
        time.sleep(0.3)

    ok = all(row["verified"] for row in report["servos"].values()) if writes else True
    report["ok"] = ok
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        print(f"report: {args.json}")
    if not ok:
        print("FAILED: torque state did not read back as requested — check the servo "
              "is powered and on the bus", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
