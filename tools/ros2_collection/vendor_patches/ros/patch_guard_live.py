#!/usr/bin/env python3
"""Check, apply, or revert the live deadline/diagnostics patch (0007) for Pi baseline acd1aa8e.

The target is a stopped, explicitly selected copy of the vendor package after
patches 0001-0005 plus the live owner_instance naming. This script never touches the vendor reference tree and never
restarts a node or opens a serial port. It copies the pure support module next to
the node before applying the patch.
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
        "acd1aa8ea482bc068b5f802b498e0040232507f973a5e95b6b2bd7b96d749828",
    "ros_robot_controller_sdk.py":
        "cdc04f097beaf4cfaa0ebc739dd21aec476f0b75709f7aa868db07ddb8747b2b",
}
PATCHED = {
    "ros_robot_controller_node.py":
        "4a9f14e29ea1b39cd1b8a22be73ff710ae995751cb1f37a3f46c82386a6ddc98",
    "ros_robot_controller_sdk.py":
        "97a19b2bc336ae743bb333e2b39318785b2b7ddb02182ffc6217cca9d6ccb4c3",
}
SUPPORT_NAME = "servo_scan_support.py"
SUPPORT_SHA256 = "96959537c1b55a6805b707c1fbcc04db9b81868dc84175ebe6e9c53e28162230"
PATCH = Path(__file__).with_name("0007-deadline-scan-and-health-live.patch")
SUPPORT = Path(__file__).with_name(SUPPORT_NAME)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_patch(directory: Path, *, reverse: bool) -> int:
    command = ["patch", "--batch", "--forward", "-p1", "-i", str(PATCH)]
    if reverse:
        command.append("--reverse")
    return subprocess.run(command, cwd=directory).returncode


def verify_support() -> None:
    if not SUPPORT.is_file() or digest(SUPPORT) != SUPPORT_SHA256:
        raise SystemExit("refusing: servo_scan_support.py is missing or has the wrong hash")


def verify_files(directory: Path, expected: dict[str, str]) -> None:
    for name, wanted in expected.items():
        path = directory / name
        if not path.is_file() or path.is_symlink() or digest(path) != wanted:
            raise SystemExit(f"refusing: missing, symlinked, or wrong hash: {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("action", choices=("check", "apply", "check-reverse", "reverse"))
    args = parser.parse_args()
    verify_support()
    reverse = args.action in {"check-reverse", "reverse"}
    expected = PATCHED if reverse else BASE
    verify_files(args.directory, expected)
    if args.action.startswith("check"):
        with tempfile.TemporaryDirectory(prefix="lelab-vendor-deadline-") as tmp:
            candidate = Path(tmp)
            for name in BASE:
                shutil.copy2(args.directory / name, candidate / name)
            shutil.copy2(SUPPORT, candidate / SUPPORT_NAME)
            if run_patch(candidate, reverse=reverse):
                return 2
            final = BASE if reverse else PATCHED
            return 0 if all(digest(candidate / name) == wanted
                            for name, wanted in final.items()) else 3
    if not reverse:
        shutil.copy2(SUPPORT, args.directory / SUPPORT_NAME)
    if run_patch(args.directory, reverse=reverse):
        return 2
    final = BASE if reverse else PATCHED
    return 0 if all(digest(args.directory / name) == wanted
                    for name, wanted in final.items()) else 3


if __name__ == "__main__":
    sys.exit(main())
