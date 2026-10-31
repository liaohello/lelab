"""Read-only admission checks for mobileCar scripted-expert episode folders.

This does not import mobileCar, run a simulator, export a dataset or publish ROS actions.
NPZ numeric arrays are read without NumPy or pickle. Images are decoded with Pillow.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import struct
import sys
import zipfile
from pathlib import Path

ACTION_NAMES = (
    "vx_body_m_s", "vy_body_m_s", "wz_body_rad_s",
    *(f"joint{i}_target_user_rad" for i in range(1, 6)), "gripper_width_target_m",
)
STATE_NAMES = (
    "front_left_wheel_rad_s", "rear_left_wheel_rad_s",
    "front_right_wheel_rad_s", "rear_right_wheel_rad_s",
    "imu_accel_x_m_s2", "imu_accel_y_m_s2", "imu_accel_z_m_s2",
    "imu_gyro_x_rad_s", "imu_gyro_y_rad_s", "imu_gyro_z_rad_s",
    *(f"joint{i}_user_rad" for i in range(1, 6)), "gripper_width_m",
)
TIMES = ("sim_time_s", "wall_time_s", "capture_time_s", "decision_time_s", "dispatch_time_s", "applied_time_s")
WRIST_KEY = "observation.images.link4_rgb"


def _reject_constant(value: str):
    raise ValueError(f"Invalid JSON numeric constant: {value}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _numeric_array(archive: zipfile.ZipFile, name: str, expected_shape: tuple[int, ...]) -> list[float] | None:
    """Validate only float32/64 C-order NPY; return values only for time vectors."""
    member = name + ".npy"
    if archive.namelist().count(member) != 1:
        raise ValueError(f"Expected exactly one {member}")
    info = archive.getinfo(member)
    if info.file_size > math.prod(expected_shape) * 8 + 16384:
        raise ValueError(f"{name}: array exceeds declared shape budget")
    with archive.open(member) as handle:
        prefix = handle.read(8)
        if prefix[:6] != b"\x93NUMPY" or prefix[6:] not in (b"\x01\x00", b"\x02\x00", b"\x03\x00"):
            raise ValueError(f"{name}: unsupported NPY format")
        length_size = 2 if prefix[6] == 1 else 4
        length_bytes = handle.read(length_size)
        if len(length_bytes) != length_size:
            raise ValueError(f"{name}: truncated NPY header")
        header_size = int.from_bytes(length_bytes, "little")
        if header_size > 16000:
            raise ValueError(f"{name}: oversized NPY header")
        header_bytes = handle.read(header_size)
        if len(header_bytes) != header_size:
            raise ValueError(f"{name}: truncated NPY header")
        header = ast.literal_eval(header_bytes.decode("utf-8" if prefix[6] == 3 else "latin1"))
        dtype = header.get("descr")
        if not isinstance(dtype, str) or len(dtype) != 3 or dtype[0] not in "<>=|" or dtype[1:] not in {"f4", "f8"}:
            raise ValueError(f"{name}: only numeric float32/64 is accepted; object/pickle arrays are rejected")
        if header.get("shape") != expected_shape or header.get("fortran_order") is not False:
            raise ValueError(f"{name}: expected C-order shape {expected_shape}, got {header.get('shape')}")
        byte_order = dtype[0] if dtype[0] in "<>" else ("<" if sys.byteorder == "little" else ">")
        item_size = int(dtype[2])
        expected_bytes = math.prod(expected_shape) * item_size
        raw = handle.read(expected_bytes + 1)
        if len(raw) != expected_bytes:
            raise ValueError(f"{name}: numeric payload length mismatch")
    values = [] if len(expected_shape) == 1 else None
    for (value,) in struct.iter_unpack(byte_order + ("f" if item_size == 4 else "d"), raw):
        if not math.isfinite(value):
            raise ValueError(f"{name}: non-finite value")
        if values is not None:
            values.append(value)
    return values


def inspect_episode(root: Path, *, width: int = 640, height: int = 480, max_frames: int = 100000) -> dict:
    """Admit the source's native 16-state/9-action schema; never silently remap."""
    from PIL import Image

    root = root.resolve(strict=True)
    if max_frames <= 0 or width <= 0 or height <= 0:
        raise ValueError("Image dimensions and max_frames must be positive")
    meta_path, numeric_path = root / "meta.json", root / "episode.npz"
    for path in (meta_path, numeric_path):
        if not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("Source asset escapes episode directory")
    meta = json.loads(meta_path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    if not isinstance(meta, dict) or not isinstance(meta.get("expert"), dict):
        raise ValueError("Expected an episode object with explicit expert metadata")
    count = meta.get("frames")
    if type(count) is not int or not 0 < count <= max_frames:
        raise ValueError("frames must be a positive integer within max_frames")
    fps = meta.get("fps")
    if isinstance(fps, bool) or not isinstance(fps, (float, int)) or not math.isfinite(fps) or fps <= 0:
        raise ValueError("fps must be finite and positive")
    if meta.get("expert", {}).get("kind") != "scripted_oracle":
        raise ValueError("This importer accepts only explicitly labelled scripted_oracle data")
    if meta.get("state_dim") != 16 or tuple(meta.get("state_layout", ())) != STATE_NAMES:
        raise ValueError("Source state must preserve the declared 16-dimensional named layout")
    if meta.get("action_dim") != 9 or tuple(meta.get("action_layout", ())) != ACTION_NAMES:
        raise ValueError("Source action layout/units mismatch; no implicit rad/metre conversion")
    if not isinstance(meta.get("instruction_en"), str) or not meta["instruction_en"].strip():
        raise ValueError("Source episode requires a non-empty task instruction")
    keys = meta.get("camera_keys")
    if not isinstance(keys, list) or not keys or any(not isinstance(key, str) for key in keys):
        raise ValueError("camera_keys must be a non-empty string list")
    if WRIST_KEY not in keys or len(set(keys)) != len(keys):
        raise ValueError("Source must declare the link4 wrist RGB once")
    if any(not key.startswith("observation.images.") or "/" in key or "\\" in key or key.endswith(".") for key in keys):
        raise ValueError("Unsafe camera key")
    with zipfile.ZipFile(numeric_path) as archive:
        _numeric_array(archive, "state", (count, 16))
        _numeric_array(archive, "action", (count, 9))
        _numeric_array(archive, "requested_action", (count, 9))
        times = {name: _numeric_array(archive, name, (count,)) for name in TIMES}
    for name, values in times.items():
        if any(value < 0 for value in values):
            raise ValueError(f"{name}: negative timestamp")
        if any(a >= b for a, b in zip(values, values[1:])):
            raise ValueError(f"{name}: timestamps must strictly increase; split reset/duplicate frames explicitly")
    for capture, decision, dispatch, applied in zip(*(times[key] for key in TIMES[2:])):
        if not capture <= decision <= dispatch <= applied:
            raise ValueError("Non-causal capture/decision/dispatch/applied order")
    image_summary, image_digest = {}, hashlib.sha256()
    expected_names = [f"{index:05d}.jpg" for index in range(count)]
    for key in keys:
        directory = root / "images" / key
        if not directory.resolve(strict=True).is_relative_to(root):
            raise ValueError("Camera directory escapes episode directory")
        frames = sorted(directory.glob("*.jpg"))
        if [path.name for path in frames] != expected_names:
            raise ValueError(f"{key}: image frame names/count do not match numeric frames")
        for path in frames:
            if not path.resolve().is_relative_to(root):
                raise ValueError("Image asset escapes episode directory")
            with Image.open(path) as image:
                if image.format != "JPEG" or image.mode != "RGB" or image.size != (width, height):
                    raise ValueError(f"{key}/{path.name}: expected RGB JPEG {width}x{height}")
                image.load()
            relative = path.relative_to(root).as_posix()
            image_digest.update((relative + "\0" + _sha256(path) + "\n").encode())
        image_summary[key] = {"frames": count, "width": width, "height": height, "decoded": True}
    gaps = [b - a for a, b in zip(times["sim_time_s"], times["sim_time_s"][1:])]
    max_error = max((abs(gap - 1 / fps) for gap in gaps), default=0)
    return {
        "status": "preflight_passed", "episode": str(root), "frames": count, "source_fps": fps,
        "source_expert_kind": "scripted_oracle", "source_reported_status": meta.get("status"),
        "state_layout": list(STATE_NAMES), "action_layout": list(ACTION_NAMES),
        "task": meta["instruction_en"], "images": image_summary,
        "max_sim_frame_interval_error_s": max_error,
        "uniform_timing_within_1ms": max_error <= 0.001,
        "hashes": {"meta.json": _sha256(meta_path), "episode.npz": _sha256(numeric_path), "rgb_tree": image_digest.hexdigest()},
        "physical_success_verified": False, "human_demonstration": False,
        "ros_gazebo_verified": False, "lerobot_exported": False,
        "mapping_required_before_ros_replay": ["user_joint_zero_and_sign", "gripper_width_to_joint_or_pulse", "simulator_dynamics"],
        "depth_and_lidar": "Not checked by this RGB/numeric preflight; inspect raw modalities separately.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--max-frames", type=int, default=100000)
    args = parser.parse_args()
    try:
        report = inspect_episode(args.episode, width=args.width, height=args.height, max_frames=args.max_frames)
    except (ValueError, OSError, KeyError, SyntaxError, zipfile.BadZipFile) as error:
        print(json.dumps({"status": "preflight_failed", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
