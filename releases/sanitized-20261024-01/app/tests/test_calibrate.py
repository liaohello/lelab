# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for lelab.calibrate — manager initial state and request schema."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "pos,expected",
    [
        (-5, False),  # negative — bad frame
        (0, False),  # lower bound is exclusive (old check: pos > 0)
        (1, True),
        (100, True),
        (2000, True),
        (4095, True),  # encoder max
        (4999, True),
        (5000, False),  # upper bound is exclusive (old check: pos < 5000)
        (6000, False),  # extreme — bad frame
    ],
)
def test_is_valid_position_boundaries(pos, expected) -> None:
    """Pins the plausible-encoder-range filter that replaced three duplicated
    inline `pos > 0 and pos < 5000` checks. Boundaries are exclusive on both ends."""
    from lelab.calibrate import _is_valid_position

    assert _is_valid_position(pos) is expected


def test_calibration_status_defaults_to_idle() -> None:
    from lelab.calibrate import CalibrationStatus

    status = CalibrationStatus()
    assert status.calibration_active is False
    assert status.status == "idle"
    assert status.device_type is None
    assert status.error is None
    assert status.step == 0


def test_calibration_request_dataclass_round_trip() -> None:
    from lelab.calibrate import CalibrationRequest

    req = CalibrationRequest(
        device_type="teleop",
        port="/dev/ttyUSB0",
        config_file="my_calib",
    )
    assert req.device_type == "teleop"
    assert req.port == "/dev/ttyUSB0"
    assert req.config_file == "my_calib"
    assert req.robot_name is None


def test_calibration_manager_starts_idle() -> None:
    from lelab.calibrate import CalibrationManager

    mgr = CalibrationManager()
    assert mgr.status.calibration_active is False
    assert mgr.status.status == "idle"
    assert mgr.device is None
    assert mgr.calibration_thread is None


def test_calibration_manager_get_status_when_idle_returns_status_object() -> None:
    from lelab.calibrate import CalibrationManager, CalibrationStatus

    mgr = CalibrationManager()
    s = mgr.get_status()
    assert isinstance(s, CalibrationStatus)
    assert s.status == "idle"


def test_calibration_bus_reads_are_serialized() -> None:
    """UI status polling must never overlap the worker's Feetech transaction."""
    import threading
    import time

    from lelab.calibrate import CalibrationManager

    class Bus:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.state_lock = threading.Lock()

        def sync_read(self, register: str, normalize: bool = False) -> dict[str, int]:
            assert register == "Present_Position"
            assert normalize is False
            with self.state_lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.01)
            with self.state_lock:
                self.active -= 1
            return {"shoulder_pan": 2048}

    bus = Bus()
    mgr = CalibrationManager()
    mgr.device = type("Device", (), {"bus": bus})()

    threads = [threading.Thread(target=mgr._read_raw_positions) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert bus.max_active == 1


def test_calibration_manager_rejects_double_start_via_message() -> None:
    """When calibration_active is True, start_calibration returns success=False."""
    from lelab.calibrate import CalibrationManager, CalibrationRequest

    mgr = CalibrationManager()
    mgr.status.calibration_active = True  # simulate already running

    result = mgr.start_calibration(
        CalibrationRequest(device_type="teleop", port="/dev/null", config_file="x")
    )
    assert result.get("success") is False
    assert "already" in result.get("message", "").lower()


def test_cleanup_device_force_releases_and_clears_when_disconnect_fails() -> None:
    """A failed device.disconnect() must still force-close the port and clear the
    device handle — otherwise the COM port stays busy and blocks the next run."""
    from lelab.calibrate import CalibrationManager

    class PortHandler:
        def __init__(self) -> None:
            self.closed = False

        def closePort(self) -> None:  # noqa: N802 - mirrors LeRobot port handler API
            self.closed = True

    class Device:
        def __init__(self) -> None:
            self.bus = type("Bus", (), {"port_handler": PortHandler()})()

        def disconnect(self) -> None:
            raise RuntimeError("Failed to write 'Torque_Enable' on id_=6")

    mgr = CalibrationManager()
    device = Device()
    mgr.device = device

    mgr._cleanup_device()

    assert device.bus.port_handler.closed is True  # force-released despite failure
    assert mgr.device is None  # handle cleared so a new calibration can start


def test_homing_step_passes_the_vendor_sign_and_lands_on_the_half_turn(monkeypatch) -> None:
    """`_get_half_turn_homings` requires per-motor signs in LeRobot 0.7.

    Calling it with positions only raises TypeError; substituting a hard-coded
    sign would send every joint the wrong way. This drives the real
    CalibrationManager against the real Feetech definition so the mapping from
    "raw position" to "written Homing_Offset" is checked end to end.
    """
    from lerobot.motors import Motor, MotorNormMode
    from lerobot.motors.feetech import FeetechMotorsBus

    from lelab.calibrate import CalibrationManager

    real_bus = FeetechMotorsBus(
        port="/dev/null",
        motors={
            "shoulder_pan": Motor(1, "sts3215", MotorNormMode.DEGREES),
            "gripper": Motor(10, "sts3215", MotorNormMode.RANGE_0_100),
        },
    )
    raw_positions = {"shoulder_pan": 3000, "gripper": 2047}
    written: dict[str, int] = {}

    class Bus:
        motors = tuple(raw_positions)

        def __getattr__(self, name):
            # Everything except the homing maths is delegated to the real bus, so
            # the definition/sign lookup is the vendor one.
            return getattr(real_bus, name)

        def disable_torque(self, *args, **kwargs):
            return None

        def write(self, register, motor, value, *args, **kwargs):
            if register == "Homing_Offset":
                written[motor] = int(value)
            return None

        def reset_calibration(self, *args, **kwargs):
            return None

        def sync_read(self, register, *args, **kwargs):
            assert register == "Present_Position"
            return dict(raw_positions)

    mgr = CalibrationManager()
    mgr.device = type("Device", (), {"bus": Bus()})()
    # _step_homing writes the operating mode first; keep that off the fake.
    monkeypatch.setattr(
        "lelab.calibrate.OperatingMode",
        type("OM", (), {"POSITION": type("P", (), {"value": 0})()}),
    )

    mgr._step_homing()

    half_turn = (4096 - 1) // 2
    assert set(written) == set(raw_positions), written
    # A joint already sitting at the half turn needs no correction.
    assert written["gripper"] == 0
    # Anywhere else, the correction must be non-zero and use the vendor sign:
    # Feetech reports homing_offset_sign = -1, so offset = -(half - raw).
    assert written["shoulder_pan"] == -(half_turn - 3000)
    assert written["shoulder_pan"] == mgr._homing_offsets["shoulder_pan"]
