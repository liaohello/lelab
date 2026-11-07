#!/usr/bin/env python3
"""Check or apply the readback owner-instance patch (0005) to a stopped ROS node copy.

This runs after patch_guard_torque.py has applied 0004. It never operates on the
vendor reference tree and never restarts a node or opens a serial port. Only the
node file changes; the SDK is untouched by this patch.
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
    "ros_robot_controller_node.py":
        "9f7fcabf96b5fa68de1b5a4e20f7f9c3d6221b5f07055bef5296a992b528befe",
}
PATCHED = {
    "ros_robot_controller_node.py":
        "8da2f4b7ab2d0f1e69fb2542670cc344607283e0df601116c5ab3e312d914537",
}
PATCH = Path(__file__).with_name("0005-readback-owner-instance.patch")


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
        with tempfile.TemporaryDirectory(prefix="lelab-readback-owner-") as tmp:
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