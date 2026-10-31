"""Read Xacro sensor declarations without importing ROS or executing Xacro."""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

FIND = re.compile(r"^\$\(find ([A-Za-z0-9_]+)\)/(.*)$")


def inspect_sensors(entry: Path, packages: dict[str, Path]) -> dict:
    """Inspect includes statically; conditions/macros are never evaluated."""
    sensors, visited, unresolved = [], set(), []

    def walk(path: Path, conditions: tuple[str, ...] = ()) -> None:
        path = path.resolve(strict=True)
        identity = (path, conditions)
        if identity in visited:
            return
        visited.add(identity)
        if len(visited) > 256:
            raise ValueError("Include graph exceeds 256 entries")
        root = ET.parse(path).getroot()

        def visit(node: ET.Element, reference: str | None, gates: tuple[str, ...]) -> None:
            tag = node.tag.rsplit("}", 1)[-1]
            if tag in {"if", "unless", "macro"}:
                gates += (f"{tag}:{node.get('value', node.get('name', '?'))}",)
            if tag == "gazebo":
                reference = node.get("reference")
            if tag == "include":
                name = node.get("filename", "")
                match = FIND.fullmatch(name)
                if match and match[1] in packages:
                    target = packages[match[1]] / match[2]
                elif "$" not in name and name:
                    target = path.parent / name
                else:
                    unresolved.append({"from": str(path), "include": name})
                    return
                if target.suffix not in {".xacro", ".urdf", ".xml"} or not target.is_file():
                    unresolved.append({"from": str(path), "include": name})
                else:
                    walk(target, gates)
                return
            if tag == "sensor":
                sensors.append({
                    "name": node.get("name"), "type": node.get("type"),
                    "reference_link": reference, "topic": node.findtext("topic"),
                    "update_rate_hz": node.findtext("update_rate"),
                    "width": node.findtext("camera/image/width"),
                    "height": node.findtext("camera/image/height"),
                    "source_file": str(path), "unevaluated_conditions": list(gates),
                })
            for child in node:
                visit(child, reference, gates)

        visit(root, None, conditions)

    walk(entry)
    kinds = {sensor["type"] for sensor in sensors}
    return {
        "inspection": "static_declarations_only", "runtime_verified": False,
        "entry": str(entry.resolve()),
        "capability_declarations": {
            "rgb": bool(kinds & {"camera", "rgbd_camera"}),
            "depth": bool(kinds & {"depth_camera", "rgbd_camera"}),
            "lidar": bool(kinds & {"lidar", "gpu_lidar", "ray", "gpu_ray"}),
            "imu": "imu" in kinds,
        },
        "sensors": sensors, "unresolved_includes": unresolved,
        "files_read": sorted({str(path) for path, _ in visited}),
        "limitation": "Xacro conditions, macro calls, plugin loading and ROS topics are not evaluated.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", type=Path, required=True)
    parser.add_argument("--package", action="append", default=[], metavar="NAME=PATH")
    args = parser.parse_args()
    packages = {}
    for value in args.package:
        if "=" not in value:
            parser.error("--package requires NAME=PATH")
        name, path = value.split("=", 1)
        packages[name] = Path(path).resolve(strict=True)
    print(json.dumps(inspect_sensors(args.entry, packages), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
