#!/usr/bin/env python3
"""Generate and spawn the LeLab mobile manipulator into Gazebo Harmonic.

The reference model is an Ignition/Gazebo Classic URDF whose wheel contact
model cannot express the 45 degree friction directions required by
MecanumDrive.  This script expands the xacro, converts it with `gz sdf -p`,
patches the four wheel surfaces, verifies that the ros2_control block survived
the URDF->SDF conversion, and spawns the resulting model through
ros_gz_sim.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from ament_index_python.packages import get_package_share_directory

ET.register_namespace("gz", "http://gazebosim.org/schema")

WHEEL_FRICTION = {
    "wheel_left_front_link": (1.0, -1.0, 0.0),
    "wheel_right_back_link": (1.0, -1.0, 0.0),
    "wheel_right_front_link": (1.0, 1.0, 0.0),
    "wheel_left_back_link": (1.0, 1.0, 0.0),
}


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find_child(parent: ET.Element, name: str) -> ET.Element | None:
    for child in parent:
        if local(child.tag) == name:
            return child
    return None


def find_children(parent: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in parent if local(c.tag) == name]


def find_model(root: ET.Element) -> ET.Element:
    for elem in root.iter():
        if local(elem.tag) == "model":
            return elem
    raise RuntimeError("converted SDF has no model")


def patch_wheels(model: ET.Element) -> None:
    patched = []
    for link in find_children(model, "link"):
        name = link.attrib.get("name", "")
        if name not in WHEEL_FRICTION:
            continue
        for collision in find_children(link, "collision"):
            geometry = find_child(collision, "geometry")
            if geometry is None:
                geometry = ET.SubElement(collision, "geometry")
            for child in list(geometry):
                geometry.remove(child)
            sphere = ET.SubElement(geometry, "sphere")
            ET.SubElement(sphere, "radius").text = "0.0324"

            surface = find_child(collision, "surface")
            if surface is None:
                surface = ET.SubElement(collision, "surface")
            friction = find_child(surface, "friction")
            if friction is None:
                friction = ET.SubElement(surface, "friction")
            ode = find_child(friction, "ode")
            if ode is None:
                ode = ET.SubElement(friction, "ode")
            for child in list(ode):
                ode.remove(child)
            mode = os.environ.get("LELAB_WHEEL_FRICTION", "mecanum")
            vec = (1.0, 0.0) if mode == "longitudinal" else WHEEL_FRICTION[name]
            ET.SubElement(ode, "mu").text = "1.0"
            ET.SubElement(ode, "mu2").text = "1.0" if mode == "longitudinal" else os.environ.get("LELAB_WHEEL_MU2", "0.30")
            fdir = ET.SubElement(ode, "fdir1")
            norm = (vec[0] ** 2 + vec[1] ** 2) ** 0.5
            fdir.text = "{} {} {}".format(vec[0] / norm, vec[1] / norm, 0.0)
            # Keep the friction direction fixed in the chassis; the wheel
            # link (and therefore the collision frame) rotates with the joint.
            fdir.set("{http://gazebosim.org/schema}expressed_in", "base_footprint")
            contact = find_child(surface, "contact")
            if contact is None:
                contact = ET.SubElement(surface, "contact")
            ode_contact = find_child(contact, "ode")
            if ode_contact is None:
                ode_contact = ET.SubElement(contact, "ode")
            ET.SubElement(ode_contact, "kp").text = "1e4"
            ET.SubElement(ode_contact, "kd").text = "100"
            ET.SubElement(ode_contact, "max_vel").text = "0.01"
            ET.SubElement(ode_contact, "min_depth").text = "0.0005"
        patched.append(name)
    if len(patched) != 4:
        raise RuntimeError(f"expected 4 wheel links, patched {sorted(patched)}")


GRIPPER_CONTACT_LINKS = {
    "left_jaw_link": "left_jaw_link_collision",
    "right_jaw_link": "right_jaw_link_collision",
    "gripper_base_link": "gripper_base_link_collision",
}


def patch_gripper(model: ET.Element) -> None:
    patched = []
    for link in find_children(model, "link"):
        name = link.attrib.get("name", "")
        if name not in GRIPPER_CONTACT_LINKS:
            continue
        for collision in find_children(link, "collision"):
            surface = find_child(collision, "surface")
            if surface is None:
                surface = ET.SubElement(collision, "surface")
            friction = find_child(surface, "friction")
            if friction is None:
                friction = ET.SubElement(surface, "friction")
            ode = find_child(friction, "ode")
            if ode is None:
                ode = ET.SubElement(friction, "ode")
            for child in list(ode):
                ode.remove(child)
            ET.SubElement(ode, "mu").text = "5.0"
            ET.SubElement(ode, "mu2").text = "5.0"
        sensor = ET.SubElement(link, "sensor")
        sensor.set("name", f"{name}_contact")
        sensor.set("type", "contact")
        ET.SubElement(sensor, "always_on").text = "1"
        ET.SubElement(sensor, "update_rate").text = "50"
        contact = ET.SubElement(sensor, "contact")
        ET.SubElement(contact, "collision").text = GRIPPER_CONTACT_LINKS[name]
        ET.SubElement(sensor, "topic").text = f"/lelab/contact/{name}"
        patched.append(name)
    if len(patched) != 3:
        raise RuntimeError(f"gripper contact patch incomplete: {patched}")


def remove_mimic(model: ET.Element) -> None:
    removed = 0
    for joint in find_children(model, "joint"):
        for child in list(joint):
            if local(child.tag) == "mimic":
                joint.remove(child)
                removed += 1
    print(f"[spawn_robot] removed {removed} SDF mimic constraints; joints are commanded explicitly")


def patch_sensor_frames(model: ET.Element) -> None:
    for sensor in (e for e in model.iter() if local(e.tag) == "sensor"):
        name = sensor.attrib.get("name", "")
        if name == "wrist_rgbd":
            frame = "wrist_camera_optical_frame"
        elif name == "lidar":
            frame = "lidar_frame"
        elif name == "imu":
            frame = "imu_link"
        else:
            continue
        current = find_child(sensor, "gz_frame_id")
        if current is None:
            current = ET.SubElement(sensor, "gz_frame_id")
        current.text = frame


def patch_model_name(model: ET.Element, name: str) -> None:
    model.set("name", name)


def ensure_ros2_control(root: ET.Element, urdf: str) -> None:
    if "<ros2_control" not in urdf or "<joint name=\"joint1\">" not in urdf:
        raise RuntimeError("URDF has no usable ros2_control block")
    plugins = [
        e for e in root.iter()
        if local(e.tag) == "plugin" and e.attrib.get("name", "").startswith("gz_ros2_control")
    ]
    if not plugins:
        raise RuntimeError("converted SDF lost the gz_ros2_control plugin")
    if any(find_child(p, "robot_param") is None for p in plugins):
        raise RuntimeError("gz_ros2_control plugin has no robot_param")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--world", default="pick_place")
    parser.add_argument("--name", default="lelab_mobile")
    parser.add_argument("--x", type=float, default=0.0)
    parser.add_argument("--y", type=float, default=0.0)
    parser.add_argument("--z", type=float, default=0.06)
    parser.add_argument("--yaw", type=float, default=0.0)
    parser.add_argument("--output", default="")
    parser.add_argument("--no-spawn", action="store_true")
    args = parser.parse_args()

    share = Path(get_package_share_directory("lelab_mobile_description"))
    xacro_file = share / "urdf" / "robot.urdf.xacro"
    urdf = subprocess.check_output(["xacro", str(xacro_file)], text=True)
    with tempfile.TemporaryDirectory(prefix="lelab_spawn_") as tmp:
        urdf_path = Path(tmp) / "robot.urdf"
        urdf_path.write_text(urdf)
        converted = subprocess.check_output(["gz", "sdf", "-p", str(urdf_path)], text=True)
    root = ET.fromstring(converted)
    model = find_model(root)
    patch_wheels(model)
    patch_gripper(model)
    remove_mimic(model)
    patch_sensor_frames(model)
    patch_model_name(model, args.name)
    ensure_ros2_control(root, urdf)

    output = Path(args.output) if args.output else Path(tempfile.gettempdir()) / f"{args.name}.sdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="  ")
    output.write_text(ET.tostring(root, encoding="unicode"))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(f"[spawn_robot] model SDF: {output} sha256={digest}")

    if args.no_spawn:
        return 0

    cmd = [
        "ros2", "run", "ros_gz_sim", "create",
        "-world", args.world,
        "-file", str(output),
        "-name", args.name,
        "-x", str(args.x),
        "-y", str(args.y),
        "-z", str(args.z),
        "-Y", str(args.yaw),
    ]
    print("[spawn_robot] " + " ".join(cmd), flush=True)
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
