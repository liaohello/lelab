"""Synthetic protocol fixtures; no reference-project or simulator execution."""

import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from expert_preflight import ACTION_NAMES, STATE_NAMES, TIMES, WRIST_KEY, inspect_episode
from inspect_sensors import inspect_sensors


def npy(values, shape, dtype="<f8"):
    header = repr({"descr": dtype, "fortran_order": False, "shape": shape}).encode("latin1")
    header += b" " * ((-(10 + len(header) + 1)) % 16) + b"\n"
    data = struct.pack("<" + "d" * len(values), *values) if dtype == "<f8" else b"not-a-pickle"
    return b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header + data


class ExpertChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "attempt_000"
        self.root.mkdir()
        self.meta = {
            "frames": 3, "fps": 5, "expert": {"kind": "scripted_oracle"}, "status": "success",
            "instruction_en": "Pick up the cube and place it in the box.",
            "state_dim": 16, "action_dim": 9,
            "state_layout": list(STATE_NAMES), "action_layout": list(ACTION_NAMES), "camera_keys": [WRIST_KEY],
        }
        self.arrays = {
            "state": npy([0.0] * 48, (3, 16)),
            "action": npy([0.0] * 27, (3, 9)),
            "requested_action": npy([0.0] * 27, (3, 9)),
            **{name: npy([0.0, 0.2, 0.4], (3,)) for name in TIMES},
        }
        self.images = self.root / "images" / WRIST_KEY
        self.images.mkdir(parents=True)
        for index in range(3):
            Image.new("RGB", (8, 6), color=(index * 40, 1, 2)).save(self.images / f"{index:05d}.jpg")
        self.write()

    def write(self):
        (self.root / "meta.json").write_text(json.dumps(self.meta))
        with zipfile.ZipFile(self.root / "episode.npz", "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in self.arrays.items():
                archive.writestr(name + ".npy", content)

    def check(self):
        return inspect_episode(self.root, width=8, height=6)

    def test_valid_source_preserves_schema_units_and_provenance(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        report = self.check()
        self.assertEqual(report["status"], "preflight_passed")
        self.assertEqual(report["source_fps"], 5)
        self.assertEqual(report["action_layout"][-1], "gripper_width_target_m")
        self.assertEqual(len(report["state_layout"]), 16)
        self.assertFalse(report["human_demonstration"])
        self.assertFalse(report["physical_success_verified"])
        self.assertFalse(report["lerobot_exported"])
        self.assertTrue(report["uniform_timing_within_1ms"])
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_reordered_joint_names_rejected(self):
        self.meta["state_layout"][10:12] = self.meta["state_layout"][10:12][::-1]
        self.write()
        with self.assertRaisesRegex(ValueError, "state"):
            self.check()

    def test_gripper_rad_cannot_silently_replace_width_m(self):
        self.meta["action_layout"][-1] = "gripper_joint_target_rad"
        self.write()
        with self.assertRaisesRegex(ValueError, "units"):
            self.check()

    def test_object_arrays_are_never_unpickled(self):
        self.arrays["state"] = npy([], (3, 16), "|O8")
        self.write()
        with self.assertRaisesRegex(ValueError, "pickle"):
            self.check()

    def test_nonfinite_action_rejected(self):
        self.arrays["action"] = npy([float("nan")] + [0.0] * 26, (3, 9))
        self.write()
        with self.assertRaisesRegex(ValueError, "non-finite"):
            self.check()

    def test_action_frame_mismatch_rejected(self):
        self.arrays["action"] = npy([0.0] * 18, (2, 9))
        self.write()
        with self.assertRaisesRegex(ValueError, "shape"):
            self.check()

    def test_reset_time_requires_explicit_segmentation(self):
        self.arrays["sim_time_s"] = npy([0.0, 0.2, 0.0], (3,))
        self.write()
        with self.assertRaisesRegex(ValueError, "reset"):
            self.check()

    def test_noncausal_action_order_rejected(self):
        self.arrays["decision_time_s"] = npy([0.01, 0.21, 0.41], (3,))
        self.write()
        with self.assertRaisesRegex(ValueError, "Non-causal"):
            self.check()

    def test_timing_jitter_reported_without_silent_resampling(self):
        self.arrays["sim_time_s"] = npy([0.0, 0.2, 0.5], (3,))
        self.write()
        report = self.check()
        self.assertFalse(report["uniform_timing_within_1ms"])
        self.assertEqual(report["source_fps"], 5)

    def test_missing_image_rejected(self):
        (self.images / "00001.jpg").unlink()
        with self.assertRaisesRegex(ValueError, "count"):
            self.check()

    def test_corrupt_image_is_decoded_not_just_counted(self):
        (self.images / "00001.jpg").write_bytes(b"not a JPEG")
        with self.assertRaises(OSError):
            self.check()

    def test_human_label_is_not_accepted_as_scripted_fixture(self):
        self.meta["expert"]["kind"] = "human_teleop"
        self.write()
        with self.assertRaisesRegex(ValueError, "scripted_oracle"):
            self.check()

    def test_metadata_cannot_escape_source_directory(self):
        (self.root / "meta.json").unlink()
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text(json.dumps(self.meta))
        (self.root / "meta.json").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.check()


class SensorChecks(unittest.TestCase):
    def test_include_graph_distinguishes_rgb_from_depth_and_unused_imu(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "main.xacro").write_text('''<robot xmlns:xacro="http://ros.org/wiki/xacro">
              <xacro:include filename="$(find sensors)/camera.xacro"/>
              <xacro:include filename="$(find missing)/missing.xacro"/>
            </robot>''')
            (root / "camera.xacro").write_text('''<robot><gazebo reference="depth_cam_link">
              <sensor name="wrist" type="camera"><topic>/depth_cam/depth_cam</topic></sensor>
            </gazebo></robot>''')
            (root / "unused_imu.xacro").write_text('<robot><sensor name="imu" type="imu"/></robot>')
            result = inspect_sensors(root / "main.xacro", {"sensors": root})
            self.assertFalse(result["runtime_verified"])
            self.assertEqual(result["capability_declarations"], {"rgb": True, "depth": False, "lidar": False, "imu": False})
            self.assertEqual(result["sensors"][0]["reference_link"], "depth_cam_link")
            self.assertEqual(len(result["unresolved_includes"]), 1)

    def test_macro_definition_is_not_claimed_to_be_a_running_sensor(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sensors.xacro"
            path.write_text('''<robot xmlns:xacro="http://ros.org/wiki/xacro">
              <xacro:macro name="optional"><sensor name="rgbd" type="rgbd_camera"/></xacro:macro>
            </robot>''')
            report = inspect_sensors(path, {})
            self.assertEqual(report["sensors"][0]["unevaluated_conditions"], ["macro:optional"])
            self.assertFalse(report["runtime_verified"])


if __name__ == "__main__":
    unittest.main()
