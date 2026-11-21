#!/usr/bin/env python3
"""Record the facts a ROS acceptance run depends on, before running anything.

The point is to make the environment auditable afterwards: which interpreter,
which ROS distribution (if any), whether the AV1 encoder LeRobot 0.7 writes by
default is actually available, how much thread headroom is left before the
process limit, and how much address space this shell may use.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
from pathlib import Path


def threads_total() -> int:
    total = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            for line in (entry / "status").read_text().splitlines():
                if line.startswith("Threads:"):
                    total += int(line.split()[1])
                    break
        except OSError:
            continue
    return total


def cmd(argv: list[str], timeout: float = 20.0) -> dict:
    if not shutil.which(argv[0]):
        return {"argv": argv, "available": False}
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {
            "argv": argv,
            "available": True,
            "exit": done.returncode,
            "stdout": done.stdout.strip()[:4000],
            "stderr": done.stderr.strip()[:1000],
        }
    except Exception as exc:  # noqa: BLE001
        return {"argv": argv, "available": True, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, required=True, help="environment built from the package")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    python = args.env / "bin/python"
    report: dict = {
        "host": os.uname().nodename,
        "kernel": os.uname().release,
        "python_argv0": sys.version.split()[0],
        "package_python": str(python),
        "package_python_exists": python.is_file(),
        "cwd": os.getcwd(),
        "env": {k: os.environ.get(k) for k in ("ROS_DISTRO", "ROS_VERSION", "AMENT_PREFIX_PATH",
                                               "PYTHONPATH", "HF_HOME")},
    }

    if python.is_file():
        probe = cmd([str(python), "-c",
                     "import sys, json; print(json.dumps({'version': sys.version.split()[0], "
                     "'executable': sys.executable, 'prefix': sys.prefix}))"])
        report["interpreter"] = probe
        report["imports"] = cmd([str(python), "-c",
                                 "import json, importlib.util as u; "
                                 "print(json.dumps({m: (u.find_spec(m) is not None) for m in "
                                 "['rclpy','rosbag2_py','lelab','lerobot','av','torch']}))"])

    report["threads"] = {
        "system_total": threads_total(),
        "rlimit_nproc_soft": resource.getrlimit(resource.RLIMIT_NPROC)[0],
        "rlimit_nproc_hard": resource.getrlimit(resource.RLIMIT_NPROC)[1],
        "headroom": (
            resource.getrlimit(resource.RLIMIT_NPROC)[0] - threads_total()
            if resource.getrlimit(resource.RLIMIT_NPROC)[0] > 0
            else None
        ),
    }
    report["address_space"] = {
        "rlimit_as_soft": resource.getrlimit(resource.RLIMIT_AS)[0],
        "rlimit_as_hard": resource.getrlimit(resource.RLIMIT_AS)[1],
        "rlimit_as_soft_gib": (
            round(resource.getrlimit(resource.RLIMIT_AS)[0] / 2**30, 2)
            if resource.getrlimit(resource.RLIMIT_AS)[0] > 0
            else "unlimited"
        ),
    }
    report["memory"] = {
        line.split(":")[0]: line.split(":")[1].strip()
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.split(":")[0] in {"MemTotal", "MemAvailable"}
    }
    report["disk"] = cmd(["df", "-h", str(Path.cwd())]).get("stdout", "").splitlines()[-1:]

    report["ros"] = {
        "ros2": cmd(["ros2", "--help"]),
        "distro_env": os.environ.get("ROS_DISTRO"),
        "rclpy_import": cmd([str(python), "-c", "import rclpy; print(rclpy.__file__)"])
        if python.is_file() else {"available": False},
    }
    encoders = cmd(["ffmpeg", "-hide_banner", "-encoders"])
    report["encoders"] = {
        "ffmpeg": encoders,
        "av1_lines": [
            line for line in (encoders.get("stdout") or "").splitlines()
            if "av1" in line.lower()
        ],
        "pyav": cmd([str(python), "-c",
                     "import av; print(sorted(c for c in av.codecs_available if 'av1' in c))"])
        if python.is_file() else {"available": False},
    }
    report["ffmpeg_version"] = cmd(["ffmpeg", "-version"])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    summary = {
        "python": report.get("interpreter", {}).get("stdout"),
        "ros_distro": report["ros"].get("distro_env"),
        "threads": report["threads"],
        "address_space": report["address_space"],
        "av1": report["encoders"]["av1_lines"],
        "out": str(args.out),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
