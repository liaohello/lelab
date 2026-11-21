#!/usr/bin/env python3
"""Decide whether a real ROS 2 target environment is available to this run.

The acceptance asks for a target-side start, node list and log check under a
simulated/no-motion configuration. That needs an actual ROS 2 installation with
`rclpy`, `rosbag2_py` and the agent package's launch file. This probe records
what exists so the report can say BLOCKED with evidence instead of quietly
substituting the compatible environment.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


def run(argv: list[str], timeout: float = 20.0) -> dict:
    if not shutil.which(argv[0]):
        return {"argv": argv, "found": False}
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {"argv": argv, "found": True, "exit": done.returncode,
                "stdout": done.stdout.strip()[:800], "stderr": done.stderr.strip()[:400]}
    except Exception as exc:  # noqa: BLE001
        return {"argv": argv, "found": True, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tree", required=True, type=Path, help="unpacked ros payload")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    report: dict = {
        "ros_distro": os.environ.get("ROS_DISTRO"),
        "ros_version": os.environ.get("ROS_VERSION"),
        "ament_prefix": os.environ.get("AMENT_PREFIX_PATH"),
        "opt_ros": sorted(p.name for p in Path("/opt/ros").glob("*")) if Path("/opt/ros").is_dir() else [],
        "which": {name: shutil.which(name) for name in ("ros2", "rosbag2", "colcon")},
        "probes": {},
        "launch_files": [],
        "checks": {},
    }

    agent = args.tree / "lelab_mobile_agent"
    report["agent_present"] = agent.is_dir()
    if agent.is_dir():
        report["launch_files"] = sorted(
            str(path.relative_to(args.tree)) for path in agent.rglob("*.launch.py")
        )
        report["package_xml"] = sorted(
            str(path.relative_to(args.tree)) for path in agent.rglob("package.xml")
        )
        report["agent_files"] = sum(1 for _ in agent.rglob("*") if _.is_file())

    report["probes"]["ros2_pkg_list"] = run(["ros2", "pkg", "list"])
    report["probes"]["rclpy"] = run(["python3", "-c", "import rclpy; print(rclpy.__file__)"])
    report["probes"]["rosbag2_py"] = run(
        ["python3", "-c", "import rosbag2_py; print(rosbag2_py.__file__)"]
    )

    checks = report["checks"]
    checks["rclpy_importable"] = report["probes"]["rclpy"].get("exit") == 0
    checks["rosbag2_importable"] = report["probes"]["rosbag2_py"].get("exit") == 0
    checks["ros2_cli_present"] = report["probes"]["ros2_pkg_list"].get("exit") == 0
    checks["agent_package_present"] = bool(report["agent_present"] and report["package_xml"])
    checks["launch_file_present"] = bool(report["launch_files"])

    ready = all(checks.values())
    report["status"] = "ready" if ready else "blocked"
    report["blocking"] = [name for name, ok in checks.items() if not ok]
    report["operator_command"] = (
        "source /opt/ros/<distro>/setup.bash && source install/setup.bash && "
        "ros2 launch lelab_mobile_agent <launch>.launch.py use_sim_time:=true"
        if not ready else "ready"
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": report["status"], "blocking": report["blocking"],
                      "launch_files": report["launch_files"][:3], "out": str(args.out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
