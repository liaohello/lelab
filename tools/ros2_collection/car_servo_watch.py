#!/usr/bin/env python3
"""Watch car bus-servo positions live, so "it won't turn past here" becomes a number.

Reading a joint by hand is the fastest mechanical test there is, but only if the number
moves with it. This prints one line per sample and a min/max summary at the end:

  - position tracks your hand, smooth, no gaps     -> joint + encoder are fine
  - position frozen while you push                  -> the horn is blocked, the servo is
                                                       decoupled from the joint, or you are
                                                       turning a different joint
  - position jumps in large steps                   -> gear backlash / encoder resolution
  - position saturates at one value while pushing    -> that value is the mechanical stop

It only ever sends the read commands from car_servo_readout (0x05 position), and refuses to
run while the vendor node holds /dev/rrc: two writers on one bus interleave replies and the
vendor SDK's blocking read has no timeout.

Usage (inside the MentorPi container):
    python3 car_servo_watch.py --ids 1 2 3 --seconds 30 --rate 5
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from car_servo_readout import DEVICE, BAUDRATE, port_holders, read_servo


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default=DEVICE)
    parser.add_argument("--ids", nargs="+", type=int, default=[1])
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--rate", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=0.4)
    parser.add_argument("--json", help="write the per-sample series here")
    args = parser.parse_args()

    holders = port_holders(args.device)
    if holders:
        print(f"REFUSED: {args.device} is held by {holders}; the vendor node owns the bus",
              file=sys.stderr)
        return 3

    from ros_robot_controller.ros_robot_controller_sdk import Board

    board = Board(device=args.device, baudrate=BAUDRATE, timeout=args.timeout + 0.5)
    board.enable_reception(True)
    series: list[dict] = []
    lo: dict[int, int] = {}
    hi: dict[int, int] = {}
    missed: dict[int, int] = {}
    interval = 1.0 / args.rate if args.rate > 0 else 0.2
    started = time.monotonic()
    try:
        while time.monotonic() - started < args.seconds:
            row: dict[str, object] = {"t": round(time.monotonic() - started, 3)}
            for servo_id in args.ids:
                value = read_servo(board, servo_id, "position", args.timeout, 0)["value"]
                pos = value[0] if value else None
                row[str(servo_id)] = pos
                if pos is None:
                    missed[servo_id] = missed.get(servo_id, 0) + 1
                else:
                    lo[servo_id] = min(lo.get(servo_id, pos), pos)
                    hi[servo_id] = max(hi.get(servo_id, pos), pos)
            series.append(row)
            print(" ".join(f"{k}={v}" for k, v in row.items()), flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        board.enable_reception(False)
        time.sleep(0.3)

    print("\n--- summary ---")
    for servo_id in args.ids:
        if servo_id in lo:
            print(f"servo {servo_id}: min={lo[servo_id]} max={hi[servo_id]} "
                  f"span={hi[servo_id] - lo[servo_id]} missed={missed.get(servo_id, 0)}")
        else:
            print(f"servo {servo_id}: no reading ({missed.get(servo_id, 0)} timeouts)")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"ids": args.ids, "samples": series,
                       "min": lo, "max": hi, "missed": missed}, handle, indent=1)
        print(f"report: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
