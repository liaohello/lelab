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


def test_completing_a_calibration_binds_the_file_lerobot_actually_wrote(
    tmp_lerobot_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding table only helps if its hash matches the calibration file on disk.

    This drives the real ``_complete_calibration`` against a real LeRobot leader device
    and lets LeRobot's own writer produce the file — a dict hand-built in the test would
    prove nothing about the JSON draccus emits. Only the board write is stubbed, since
    that is the serial boundary. The measured port then has to pass the same
    ``check_pair`` the teleoperation start path runs, with no "changed since measured"
    warning.
    """
    import json
    from pathlib import Path

    from lerobot.teleoperators.so_leader import SO101Leader, SO101LeaderConfig

    from lelab import arm_bindings
    from lelab.calibrate import CalibrationManager, CalibrationRequest

    port = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149723-if00"
    monkeypatch.setenv("LELAB_ARM_BINDINGS", str(tmp_lerobot_home / "lelab" / "arm_bindings.json"))

    leader = SO101Leader(SO101LeaderConfig(port=port, id="left_rig"))
    # Keep the guard reading the same tree LeRobot writes to: the app resolves the
    # calibration dir through lelab.utils.config, which this fixture redirects.
    monkeypatch.setattr("lelab.utils.config.LEADER_CONFIG_PATH", str(Path(leader.calibration_fpath).parent))

    written_to_boards: list[dict] = []
    leader.bus.write_calibration = lambda calibration, cache=True: written_to_boards.append(dict(calibration))

    mgr = CalibrationManager()
    mgr.device = leader
    mgr._current_request = CalibrationRequest(device_type="teleop", port=port, config_file="left_rig")
    for index, name in enumerate(leader.bus.motors):
        mgr._homing_offsets[name] = -1000 + index
        mgr._mins[name] = 800
        mgr._maxes[name] = 3200

    mgr._complete_calibration()

    saved = json.loads(Path(leader.calibration_fpath).read_text(encoding="utf-8"))
    assert len(written_to_boards) == 1 and len(written_to_boards[0]) == 6

    binding = arm_bindings.describe(port)
    assert binding["role"] == "leader"
    assert binding["config"] == "left_rig.json"
    assert binding["content_sha256"] == arm_bindings.canonical_sha256(saved)
    assert binding["ids"] == {name: index + 1 for index, name in enumerate(leader.bus.motors)}

    verdict = arm_bindings.check_pair(
        arm_bindings.side(port, "leader", "left_rig"), arm_bindings.side("", "follower", "")
    )
    assert verdict["ready"] is True
    assert not any(w.startswith("leader") for w in verdict["warnings"]), verdict["warnings"]


def test_a_binding_written_by_the_wizard_makes_the_other_side_refuse(
    tmp_lerobot_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The producer and the consumer have to agree on one thing: the port the wizard was
    told about is the port the teleop request names. Nothing re-normalises it in between."""
    from dataclasses import asdict

    from lerobot.motors import MotorCalibration

    from lelab import arm_bindings
    from lelab.calibrate import CalibrationManager, CalibrationRequest

    port = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149723-if00"
    bindings = tmp_lerobot_home / "lelab" / "arm_bindings.json"
    monkeypatch.setenv("LELAB_ARM_BINDINGS", str(bindings))
    # The same values _complete_calibration builds for every joint.
    calibration = {
        name: MotorCalibration(id=index + 1, drive_mode=0, homing_offset=-1000 + index,
                               range_min=800, range_max=3200)
        for index, name in enumerate(["shoulder_pan", "shoulder_lift", "elbow_flex",
                                      "wrist_flex", "wrist_roll", "gripper"])
    }

    CalibrationManager()._record_arm_binding(
        CalibrationRequest(device_type="robot", port=port, config_file="left_rig"), calibration
    )

    assert arm_bindings.describe(port)["role"] == "follower"
    assert (arm_bindings.load(bindings)["ports"][port]["content_sha256"]
            == arm_bindings.canonical_sha256({name: asdict(body) for name, body in calibration.items()}))
