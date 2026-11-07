"""Tests for the read-only SO-101 leader reader (injected fake bus, no hardware)."""
from __future__ import annotations

import errno
import fcntl
import importlib.util
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "mobile_leader_reader", str(REPO / "app_ros_mobile" / "lelab" / "mobile_leader_reader.py"))
reader_module = importlib.util.module_from_spec(_spec)
sys.modules["mobile_leader_reader"] = reader_module
_spec.loader.exec_module(reader_module)

LeaderConfig = reader_module.LeaderConfig
SO101LeaderReader = reader_module.SO101LeaderReader
LEADER_JOINTS = reader_module.LEADER_JOINTS
list_leader_ports = reader_module.list_leader_ports

WRITE_METHODS = ("write", "sync_write", "torque_enable", "disable_torque", "write_calibration",
                 "reset_calibration", "set_turning_range", "ping", "set_motors_id", "configure")


class FakeBus:
    def __init__(self, values=None, raise_on_read=None):
        self.calls: list[str] = []
        if values is None:
            # Default fixture: a complete read including the gripper.
            values = {name: float(i * 10) for i, name in enumerate(LEADER_JOINTS)}
            values["gripper"] = 42.0
        self.values = dict(values)
        self.raise_on_read = raise_on_read or 0

    def connect(self):
        self.calls.append("connect")

    def disconnect(self, disable_torque=True):
        self.calls.append(f"disconnect:disable_torque={disable_torque}")

    def sync_read(self, model, ids, num_retry=0):
        self.calls.append(f"sync_read:{model}:{num_retry}")
        self.raise_on_read -= 1
        if self.raise_on_read >= 0:
            raise OSError("bus timeout")
        return dict(self.values)

    def __getattr__(self, name):
        if name in WRITE_METHODS:
            raise AssertionError(f"leader reader must never call {name}()")
        raise AttributeError(name)


class ClosingBus(FakeBus):
    """A fake bus over a real fd, mirroring how LeRobot closes the port on disconnect."""

    def __init__(self, fd):
        super().__init__()
        self.fd = fd
        self._closed = False
        self.port_handler = SimpleNamespace(ser=SimpleNamespace(fileno=lambda: fd))

    def disconnect(self, disable_torque=True):
        super().disconnect(disable_torque=disable_torque)
        if not self._closed:
            self._closed = True
            os.close(self.fd)


class LeaderReaderTests(unittest.TestCase):
    def config(self, **kw):
        base = dict(port="/dev/serial/by-id/usb-1a86_test_serial1-if00", read_hz=50.0,
                    stale_after_s=0.5, unhealthy_after_failures=3)
        base.update(kw)
        return LeaderConfig(**base)

    def test_open_and_read_only_uses_sync_read(self):
        bus = FakeBus()
        reader = SO101LeaderReader(self.config(), bus=bus)
        reader.open()
        status = reader.poll_once()
        self.assertEqual(bus.calls, ["connect", "sync_read:Present_Position:2"])
        self.assertTrue(status["connected"])
        self.assertEqual(status["reads"], 1)
        self.assertTrue(status["healthy"], status["reason"])

    def test_degree_to_radian_conversion_and_gripper_percent_units(self):
        bus = FakeBus(values={name: 90.0 for name in LEADER_JOINTS} | {"gripper": 75.0})
        reader = SO101LeaderReader(self.config(), bus=bus)
        reader.open()
        snap = reader.poll_once()
        self.assertAlmostEqual(snap["positions_rad"]["shoulder_pan"], math.pi / 2, places=6)
        self.assertEqual(snap["gripper_percent"], 75.0)
        self.assertIn("not an angle", snap["units"]["gripper"])

    def test_repeated_failures_mark_unhealthy_and_drop_connection(self):
        bus = FakeBus(raise_on_read=99)
        reader = SO101LeaderReader(self.config(), bus=bus)
        reader.open()
        for _ in range(3):
            snap = reader.poll_once()
        self.assertEqual(snap["consecutive_failures"], 3)
        self.assertFalse(snap["healthy"])
        self.assertFalse(snap["connected"], "unreadable bus must not stay marked connected")
        self.assertIn("leader_unreachable", snap["reason"])

    def test_stale_reads_report_age_and_stop_being_healthy(self):
        bus = FakeBus()
        reader = SO101LeaderReader(self.config(stale_after_s=0.2), bus=bus)
        reader.open()
        reader.poll_once()
        snap = reader.snapshot(now=reader.last_wall + 0.5)
        self.assertFalse(snap["healthy"])
        self.assertTrue(snap["reason"].startswith("leader_read_stale"))
        self.assertAlmostEqual(snap["age_s"], 0.5, places=2)

    def test_missing_joints_are_listed_not_filled_with_zero(self):
        values = {"shoulder_pan": 1.0}
        bus = FakeBus(values=values)
        reader = SO101LeaderReader(self.config(), bus=bus)
        reader.open()
        snap = reader.poll_once()
        self.assertEqual(snap["positions_deg"], {"shoulder_pan": 1.0})
        self.assertIn("wrist_roll", snap["missing_joints"])
        self.assertIsNone(snap["gripper_percent"], "absent gripper reading must not default to 0")

    def test_partial_read_never_reuses_previous_gripper_or_reports_healthy(self):
        bus = FakeBus()
        reader = SO101LeaderReader(self.config(), bus=bus)
        reader.open()
        self.assertTrue(reader.poll_once()["healthy"])
        del bus.values["gripper"]
        snap = reader.poll_once()
        self.assertFalse(snap["healthy"])
        self.assertIn("gripper", snap["missing_joints"])
        self.assertIsNone(snap["gripper_percent"])
        bus.values["gripper"] = 40.0
        self.assertTrue(reader.poll_once()["healthy"])

    def test_nonfinite_joint_and_gripper_are_not_healthy_measurements(self):
        bus = FakeBus()
        bus.values["wrist_roll"] = float("nan")
        bus.values["gripper"] = float("inf")
        reader = SO101LeaderReader(self.config(), bus=bus)
        reader.open()
        snap = reader.poll_once()
        self.assertFalse(snap["healthy"])
        self.assertIn("wrist_roll", snap["missing_joints"])
        self.assertIn("gripper", snap["missing_joints"])
        self.assertIsNone(snap["gripper_percent"])

    def test_close_clears_state(self):
        bus = FakeBus()
        reader = SO101LeaderReader(self.config(), bus=bus)
        reader.open()
        reader.poll_once()
        out = reader.close()
        self.assertTrue(out["closed"])
        self.assertIn("disconnect:disable_torque=False", bus.calls,
                      "closing a passive reader must never write torque-off to the bus")
        snap = reader.snapshot()
        self.assertFalse(snap["connected"])
        self.assertEqual(snap["positions_deg"], {})
        self.assertEqual(snap["reason"], "leader_unreachable")

    def test_failed_connect_raises_and_marks_error(self):
        class BrokenBus(FakeBus):
            def connect(self):
                raise SystemError("port busy")

        reader = SO101LeaderReader(self.config(), bus=BrokenBus())
        with self.assertRaises(RuntimeError):
            reader.open()
        self.assertIn("port busy", reader.last_error)

    # ------------------------------------------------------------------ serial exclusivity
    def test_claim_on_a_real_tty_fd_refuses_a_second_opener(self):
        master, slave = os.openpty()
        name = os.ttyname(slave)
        try:
            reader = SO101LeaderReader(self.config(), bus=ClosingBus(fd=slave))
            reader.open()
            self.assertEqual(reader.snapshot()["port_claim"], "flock+tioxcl")
            with self.assertRaises(OSError) as ctx:
                os.open(name, os.O_RDWR)
            self.assertEqual(ctx.exception.errno, errno.EBUSY,
                             "TIOCEXCL must refuse a second opener while the reader holds the port")
        finally:
            os.close(master)
            try:
                os.close(slave)
            except OSError:
                pass  # the fd was already released with the bus

    def test_flock_is_held_while_open_and_released_on_close(self):
        """A non-tty fd exercises flock alone: contention, then release with the port."""
        fd_path = tempfile.NamedTemporaryFile(prefix="leader_port_")
        holder = os.open(fd_path.name, os.O_RDWR)
        contender = os.open(fd_path.name, os.O_RDWR)
        try:
            bus = ClosingBus(fd=holder)
            reader = SO101LeaderReader(self.config(), bus=bus)
            reader.open()
            # No ioctl on a regular file, so the claim must downgrade and say so.
            self.assertEqual(reader.snapshot()["port_claim"], "flock_only")
            self.assertIn("tioxcl_unavailable", reader.last_error)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            reader.close()
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(contender, fcntl.LOCK_UN)
        finally:
            os.close(contender)
            try:
                os.close(holder)
            except OSError:
                pass  # ClosingBus already released it

    def test_claim_refuses_when_another_process_holds_the_port(self):
        master, slave = os.openpty()
        contender = os.open(os.ttyname(slave), os.O_RDWR)
        try:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            reader = SO101LeaderReader(self.config(), bus=ClosingBus(fd=slave))
            with self.assertRaises(RuntimeError) as ctx:
                reader.open()
            self.assertIn("leader_serial_busy", str(ctx.exception))
            self.assertFalse(reader.connected, "a port we could not claim must not be marked connected")
        finally:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)
            os.close(master)
            os.close(slave)

    def test_bus_without_a_fd_reports_the_claim_as_unverified(self):
        """No fd means no OS-level lock, so the status must not imply exclusivity."""
        reader = SO101LeaderReader(self.config(), bus=FakeBus())
        reader.open()
        self.assertEqual(reader.snapshot()["port_claim"], "unverified:no_fileno")
        self.assertIn("leader_port_claim_unverified", reader.last_error)

    def test_list_leader_ports_reads_by_id_names_without_touching_devices(self):
        root = Path(tempfile.mkdtemp(prefix="byid_"))
        target = root / "ttyACM7"
        target.write_text("")
        (root / "usb-1a86_USB_Single_Serial_5B91045575-if00").symlink_to(target)
        (root / "pci-0000:00:14.0-usb-0:1.2:1.0").symlink_to(target)
        ports = list_leader_ports(str(root))
        self.assertEqual(len(ports), 1)
        self.assertEqual(ports[0]["serial"], "5B91045575")
        self.assertEqual(ports[0]["device"], "ttyACM7")


class LeaderCalibrationReuseTests(unittest.TestCase):
    """Layer A must reuse the numbers the existing SO-101 wizard wrote, and only its own arm's.

    The shape below is the one actually produced by lerobot (five fields per motor, ids 1..6),
    read off this host's calibration tree; discovering it must stay a pure read.
    """

    LEROBOT_SHAPE = {
        "shoulder_pan": {"id": 1, "drive_mode": 0, "homing_offset": 1467, "range_min": 796, "range_max": 3216},
        "shoulder_lift": {"id": 2, "drive_mode": 0, "homing_offset": -133, "range_min": 956, "range_max": 3349},
        "elbow_flex": {"id": 3, "drive_mode": 0, "homing_offset": 86, "range_min": 741, "range_max": 2954},
        "wrist_flex": {"id": 4, "drive_mode": 0, "homing_offset": -327, "range_min": 903, "range_max": 3238},
        "wrist_roll": {"id": 5, "drive_mode": 0, "homing_offset": -342, "range_min": 155, "range_max": 3968},
        "gripper": {"id": 6, "drive_mode": 0, "homing_offset": 91, "range_min": 1561, "range_max": 2772},
    }

    def tree(self, files: dict[str, str]) -> Path:
        root = Path(tempfile.mkdtemp(prefix="calib_")) / "teleoperators"
        for name, body in files.items():
            path = root / name
            path.mkdir(parents=True, exist_ok=True)
            (path / "rig.json").write_text(body, encoding="utf-8")
        return root

    def test_discovery_offers_the_existing_wizard_file_with_its_real_numbers(self):
        import json
        root = self.tree({"so_leader": json.dumps(self.LEROBOT_SHAPE)})
        out = reader_module.list_leader_calibrations(str(root))
        self.assertTrue(out["exists"])
        self.assertEqual(len(out["entries"]), 1)
        entry = out["entries"][0]
        self.assertTrue(entry["usable"], entry)
        self.assertEqual(entry["robot_type"], "so_leader")
        self.assertEqual(entry["motors"], {name: body["id"] for name, body in self.LEROBOT_SHAPE.items()})
        self.assertEqual(entry["calibration"], self.LEROBOT_SHAPE)
        self.assertTrue(entry["sha256"])

    def test_partial_or_broken_files_are_reported_and_not_offered_for_reuse(self):
        import json
        no_gripper = {k: v for k, v in self.LEROBOT_SHAPE.items() if k != "gripper"}
        short_field = {**self.LEROBOT_SHAPE,
                       "elbow_flex": {k: v for k, v in self.LEROBOT_SHAPE["elbow_flex"].items()
                                      if k != "homing_offset"}}
        root = self.tree({"so_leader": json.dumps(no_gripper),
                          "bi_so_leader": json.dumps(short_field),
                          "broken_leader": "{not json",
                          "list_leader": "[1, 2, 3]"})
        entries = {e["robot_type"]: e for e in reader_module.list_leader_calibrations(str(root))["entries"]}
        self.assertEqual(entries["so_leader"]["missing_motors"], ["gripper"])
        self.assertFalse(entries["so_leader"]["usable"])
        self.assertNotIn("calibration", entries["so_leader"], "a partial file must never be handed back as reusable")
        self.assertEqual(entries["bi_so_leader"]["incomplete_motors"], {"elbow_flex": ["homing_offset"]})
        self.assertFalse(entries["bi_so_leader"]["usable"])
        self.assertEqual(entries["broken_leader"]["reason"], "unparsable:JSONDecodeError")
        self.assertEqual(entries["list_leader"]["reason"], "not_a_mapping")

    def test_a_missing_directory_is_reported_as_absent_rather_than_raising(self):
        out = reader_module.list_leader_calibrations("/definitely/not/here/teleoperators")
        self.assertFalse(out["exists"])
        self.assertEqual(out["entries"], [])

    def test_discovery_writes_nothing_into_the_calibration_tree(self):
        import json
        root = self.tree({"so_leader": json.dumps(self.LEROBOT_SHAPE)})

        def fingerprint():
            return sorted((str(path), path.read_bytes(), path.stat().st_mtime_ns)
                          for path in root.rglob("*") if path.is_file())

        before = fingerprint()
        reader_module.list_leader_calibrations(str(root))
        reader_module.list_leader_calibrations(str(root))
        self.assertEqual(before, fingerprint(), "reusing a leader calibration must stay read-only")

    def test_matching_ids_pass_and_a_single_swapped_id_is_named(self):
        ids = {name: body["id"] for name, body in self.LEROBOT_SHAPE.items()}
        self.assertEqual(reader_module.calibration_id_mismatches(self.LEROBOT_SHAPE, ids), [])
        swapped = {**self.LEROBOT_SHAPE, "gripper": {**self.LEROBOT_SHAPE["gripper"], "id": 2}}
        out = reader_module.calibration_id_mismatches(swapped, ids)
        self.assertEqual(len(out), 1, out)
        self.assertIn("gripper", out[0])
        self.assertIn("bus_id=6", out[0])

    def test_absent_or_extra_motors_are_refused_instead_of_reading_the_wrong_arm(self):
        ids = {name: body["id"] for name, body in self.LEROBOT_SHAPE.items()}
        without_wrist = {k: v for k, v in self.LEROBOT_SHAPE.items() if k != "wrist_roll"}
        out = reader_module.calibration_id_mismatches(without_wrist, ids)
        self.assertEqual(out, ["wrist_roll:no_id"], out)
        extra = {**self.LEROBOT_SHAPE, "extra_motor": {"id": 9, "drive_mode": 0, "homing_offset": 0,
                                                       "range_min": 0, "range_max": 4095}}
        self.assertEqual(reader_module.calibration_id_mismatches(extra, ids),
                         ["extra_motor:unexpected_motor_in_calibration"])
        self.assertEqual(reader_module.calibration_id_mismatches(None, ids), ["leader_calibration_missing"])


if __name__ == "__main__":
    unittest.main()
