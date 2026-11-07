#!/usr/bin/env python3
"""Check or apply the torque/readback contract patch to a stopped ROS SDK copy.

This runs after patch_guard.py has applied 0001..0003. It never operates on the
vendor reference tree and never restarts a node or opens a serial port.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

BASE = {
    "ros_robot_controller_sdk.py":
        "7ea4e4f5aeea69d778f5cf93e35abd2c5b227bf3a12796fdfd7bc84d6fa2b8b7",
    "ros_robot_controller_node.py":
        "7e171990ecbc7a99179266e00e6c915be10f01d74478b34f8af77d431d8e2615",
}
PATCHED = {
    "ros_robot_controller_sdk.py":
        "cdc04f097beaf4cfaa0ebc739dd21aec476f0b75709f7aa868db07ddb8747b2b",
    "ros_robot_controller_node.py":
        "9f7fcabf96b5fa68de1b5a4e20f7f9c3d6221b5f07055bef5296a992b528befe",
}
PATCH = Path(__file__).with_name("0004-torque-semantics-and-readback-contract.patch")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_patch(directory: Path, *, reverse: bool) -> int:
    command = ["patch", "--batch", "--forward", "-p1", "-i", str(PATCH)]
    if reverse:
        command.append("--reverse")
    return subprocess.run(command, cwd=directory).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("action", choices=("check", "apply", "check-reverse", "reverse"))
    args = parser.parse_args()
    reverse = args.action in {"check-reverse", "reverse"}
    expected = PATCHED if reverse else BASE
    for name, wanted in expected.items():
        path = args.directory / name
        if not path.is_file() or path.is_symlink() or digest(path) != wanted:
            print("refusing: missing, symlinked, or wrong baseline:", name, file=sys.stderr)
            return 2
    if args.action.startswith("check"):
        with tempfile.TemporaryDirectory(prefix="lelab-torque-contract-") as tmp:
            candidate = Path(tmp)
            for name in BASE:
                shutil.copy2(args.directory / name, candidate / name)
            if run_patch(candidate, reverse=reverse):
                return 2
            final = BASE if reverse else PATCHED
            return 0 if all(digest(candidate / name) == wanted
                            for name, wanted in final.items()) else 3
    if run_patch(args.directory, reverse=reverse):
        return 2
    final = BASE if reverse else PATCHED
    return 0 if all(digest(args.directory / name) == wanted
                    for name, wanted in final.items()) else 3


if __name__ == "__main__":
    raise SystemExit(main())
