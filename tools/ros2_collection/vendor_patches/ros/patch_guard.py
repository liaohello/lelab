#!/usr/bin/env python3
"""Check, apply, or revert the T1 patch to a stopped, explicitly selected SDK copy."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

BASE = {
    "ros_robot_controller_sdk.py": "f7c1584b5b1f393041bd86c16c68310d38969c15bf8dfd1f92df10373a6cc504",
    "ros_robot_controller_node.py": "0c4b5df728f18c4a7d1850f0647e26a0d1c892ab2455f3fa0079319a9d2c619f",
}
PATCHED = {
    "ros_robot_controller_sdk.py": "7ea4e4f5aeea69d778f5cf93e35abd2c5b227bf3a12796fdfd7bc84d6fa2b8b7",
    "ros_robot_controller_node.py": "7e171990ecbc7a99179266e00e6c915be10f01d74478b34f8af77d431d8e2615",
}
PATCHES = ("0001-bounded-servo-read.patch", "0002-ros-uint16-state-guard.patch",
           "0003-measured-readback-topic.patch")


def run_patches(directory: Path, *, reverse: bool) -> int:
    patches = [Path(__file__).with_name(name).resolve()
               for name in (reversed(PATCHES) if reverse else PATCHES)]
    for patch in patches:
        command = ["patch", "--batch", "--forward", "-p1", "-i", str(patch)]
        if reverse:
            command.append("--reverse")
        result = subprocess.run(command, cwd=directory)
        if result.returncode:
            return result.returncode
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="directory containing both ROS Python files")
    parser.add_argument("action", choices=("check", "apply", "check-reverse", "reverse"))
    args = parser.parse_args()
    reverse = args.action in ("check-reverse", "reverse")
    expected = PATCHED if reverse else BASE
    for name, digest in expected.items():
        path = args.directory / name
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            print("refusing: missing, symlinked, or wrong baseline:", name, file=sys.stderr)
            return 2
    if args.action.startswith("check"):
        with tempfile.TemporaryDirectory(prefix="lelab-vendor-patch-check-") as tmp:
            candidate = Path(tmp)
            for name in BASE:
                shutil.copy2(args.directory / name, candidate / name)
            if run_patches(candidate, reverse=reverse):
                return 2
            final = BASE if reverse else PATCHED
            if not all(hashlib.sha256((candidate / name).read_bytes()).hexdigest() == digest
                       for name, digest in final.items()):
                return 3
        return 0
    result = run_patches(args.directory, reverse=reverse)
    if result:
        return result
    final = BASE if reverse else PATCHED
    return 0 if all(hashlib.sha256((args.directory / n).read_bytes()).hexdigest() == h for n, h in final.items()) else 3


if __name__ == "__main__":
    sys.exit(main())
