"""The leader bus must be built against the LeRobot version actually installed here.

Fake-bus unit tests cannot catch an API drift in `build_bus`, and every one of those mistakes only
surfaces when someone tries to open a real arm — so this constructs the bus without any hardware.
"""
from __future__ import annotations

import pytest

from lelab import mobile_leader_reader as ldr

CALIB = {name: {"id": i + 1, "drive_mode": 0, "homing_offset": 100 + i,
                "range_min": 200 + i, "range_max": 3000 + i}
         for i, name in enumerate((*ldr.LEADER_JOINTS, ldr.LEADER_GRIPPER))}


def config():
    return ldr.LeaderConfig(port="/dev/serial/by-id/usb-1a86_USB_Single_Serial_TEST-if00")


def test_build_bus_uses_the_installed_lerobot_api():
    pytest.importorskip("lerobot")
    from lerobot.motors.feetech.feetech import FeetechMotorsBus
    from lerobot.motors.motors_bus import MotorNormMode

    bus = ldr.build_bus(config(), CALIB)
    assert isinstance(bus, FeetechMotorsBus)
    assert set(bus.motors) == {*ldr.LEADER_JOINTS, ldr.LEADER_GRIPPER}
    assert bus.motors["shoulder_pan"].norm_mode is MotorNormMode.DEGREES
    assert bus.motors[ldr.LEADER_GRIPPER].norm_mode is MotorNormMode.RANGE_0_100
    assert {n: m.id for n, m in bus.motors.items()} == {n: c["id"] for n, c in CALIB.items()}
    assert bus.motors["wrist_roll"].model == config().motor_model
    assert bus.calibration["wrist_roll"].homing_offset == CALIB["wrist_roll"]["homing_offset"]
    # Building the object must not touch the port.
    assert not getattr(bus, "is_connected", False)


def test_reader_refuses_to_open_without_a_leader_calibration():
    with pytest.raises(ValueError, match="leader calibration missing"):
        ldr.build_bus(config(), None)


def test_partial_calibration_is_refused_instead_of_zero_filled():
    broken = {k: dict(v) for k, v in CALIB.items()}
    broken["elbow_flex"].pop("homing_offset")
    with pytest.raises(ValueError, match="elbow_flex"):
        ldr.build_bus(config(), broken)


def test_calibration_missing_a_joint_is_refused():
    broken = {k: v for k, v in CALIB.items() if k != ldr.LEADER_GRIPPER}
    with pytest.raises(ValueError, match="gripper"):
        ldr.build_bus(config(), broken)
