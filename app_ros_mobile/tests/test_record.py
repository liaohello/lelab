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
"""Tests for lelab.record — request schemas and handler entry points."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest


def test_recording_request_rejects_missing_required_fields() -> None:
    from pydantic import ValidationError

    from lelab.record import RecordingRequest

    with pytest.raises(ValidationError):
        RecordingRequest()


def test_recording_status_handler_exposes_state_fields() -> None:
    from lelab.record import handle_recording_status

    result = handle_recording_status()
    assert isinstance(result, dict)
    # Pinning the exact keys so a rename in handle_recording_status surfaces here.
    assert "recording_active" in result
    assert "current_phase" in result
    assert "session_ended" in result
    assert "available_controls" in result


def test_prepare_dataset_repo_id_adds_local_namespace_and_timestamp() -> None:
    from lelab.record import _prepare_dataset_repo_id
    from lerobot.common.control_utils import sanity_check_dataset_name

    result = _prepare_dataset_repo_id(
        "red_bowl",
        resume=False,
        now=datetime(2026, 9, 19, 14, 41, 0),
    )

    assert result == "local/red_bowl_20260919_144100"
    sanity_check_dataset_name(result, None)


def test_prepare_dataset_repo_id_preserves_explicit_namespace() -> None:
    from lelab.record import _prepare_dataset_repo_id

    result = _prepare_dataset_repo_id("robot-team/red bowl", resume=True)

    assert result == "robot-team/red_bowl"


@pytest.mark.parametrize("repo_id", ["", "   ", ".", "..", "/dataset", "owner/"])
def test_prepare_dataset_repo_id_rejects_invalid_ids(repo_id: str) -> None:
    from lelab.record import _prepare_dataset_repo_id

    with pytest.raises(ValueError, match="Dataset"):
        _prepare_dataset_repo_id(repo_id, resume=False)


def test_handle_stop_recording_when_idle_returns_dict(tmp_lerobot_home) -> None:
    from lelab.record import handle_stop_recording

    result = handle_stop_recording()
    assert isinstance(result, dict)


def test_create_record_config_pins_dshow_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, recording must use the DSHOW backend so a camera_index opens
    the same device /available-cameras enumerated (via pygrabber, DSHOW order).
    """
    import lelab.record as record
    from lerobot.cameras.configs import Cv2Backends

    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr(record, "setup_calibration_files", lambda leader, follower: ("leader", "follower"))

    request = record.RecordingRequest(
        leader_port="COM_LEADER",
        follower_port="COM_FOLLOWER",
        leader_config="leader",
        follower_config="follower",
        dataset_repo_id="user/dataset",
        single_task="pick up the cube",
        cameras={"wrist": {"type": "opencv", "camera_index": 0, "width": 640, "height": 480, "fps": 30}},
    )

    config = record.create_record_config(request)
    assert config.robot.cameras["wrist"].backend == Cv2Backends.DSHOW
    assert config.robot.max_relative_target == 3.0
    assert config.robot.max_position_deviation == 12.0


def test_build_camera_configs_uses_default_backend_when_unset() -> None:
    from lelab.record import _build_camera_configs
    from lerobot.cameras.configs import Cv2Backends

    cameras = {"cam": {"type": "opencv", "camera_index": 0, "width": 640, "height": 480, "fps": 30}}
    configs = _build_camera_configs(cameras, Cv2Backends.AVFOUNDATION)

    assert configs["cam"].backend == Cv2Backends.AVFOUNDATION
    assert configs["cam"].fourcc is None
    assert configs["cam"].index_or_path == 0


def test_build_camera_configs_defaults_v4l2_cameras_to_mjpg() -> None:
    from lelab.record import _build_camera_configs
    from lerobot.cameras.configs import Cv2Backends

    cameras = {"cam": {"type": "opencv", "camera_index": 0, "width": 640, "height": 480, "fps": 30}}
    configs = _build_camera_configs(cameras, Cv2Backends.V4L2)

    assert configs["cam"].fourcc == "MJPG"


def test_build_camera_configs_passes_fourcc_through() -> None:
    from lelab.record import _build_camera_configs
    from lerobot.cameras.configs import Cv2Backends

    cameras = {"cam": {"type": "opencv", "camera_index": 0, "fourcc": "MJPG"}}
    configs = _build_camera_configs(cameras, Cv2Backends.ANY)

    assert configs["cam"].fourcc == "MJPG"


def test_build_camera_configs_explicit_backend_overrides_default() -> None:
    from lelab.record import _build_camera_configs
    from lerobot.cameras.configs import Cv2Backends

    cameras = {"cam": {"type": "opencv", "camera_index": 0, "backend": "V4L2"}}
    configs = _build_camera_configs(cameras, Cv2Backends.AVFOUNDATION)

    assert configs["cam"].backend == Cv2Backends.V4L2


def test_build_camera_configs_invalid_backend_raises() -> None:
    from lelab.record import _build_camera_configs
    from lerobot.cameras.configs import Cv2Backends

    cameras = {"cam": {"type": "opencv", "camera_index": 0, "backend": "NOPE"}}
    with pytest.raises(KeyError):
        _build_camera_configs(cameras, Cv2Backends.ANY)


def test_build_camera_configs_skips_non_opencv_type() -> None:
    from lelab.record import _build_camera_configs
    from lerobot.cameras.configs import Cv2Backends

    cameras = {"cam": {"type": "realsense", "camera_index": 0}}
    configs = _build_camera_configs(cameras, Cv2Backends.ANY)

    assert configs == {}


def _recording_devices(tmp_lerobot_home, leader_cal: dict, follower_cal: dict, log: list):
    """Fake robot/teleop pointing at real calibration files in the redirected trees."""
    import json
    from types import SimpleNamespace

    from lelab.utils import config as cfg

    paths = {}
    for role, directory, name, payload in (("leader", cfg.LEADER_CONFIG_PATH, "left_rig", leader_cal),
                                           ("follower", cfg.FOLLOWER_CONFIG_PATH, "left_rig", follower_cal)):
        path = Path(directory) / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths[role] = path

    class _Bus:
        motors = {}

        def __init__(self, label: str) -> None:
            self.label = label

        def connect(self) -> None:
            log.append(f"connect:{self.label}")

        def write_calibration(self, calibration, cache=True) -> None:
            log.append(f"write:{self.label}")

    robot = SimpleNamespace(bus=_Bus("follower"), calibration={},
                            calibration_fpath=str(paths["follower"]))
    teleop = SimpleNamespace(bus=_Bus("leader"), calibration={}, calibration_fpath=str(paths["leader"]))
    cfg_obj = SimpleNamespace(robot=SimpleNamespace(port=""), teleop=SimpleNamespace(port=""))
    return robot, teleop, cfg_obj


def test_recording_writes_the_calibration_only_after_the_identity_check(
    tmp_lerobot_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import arm_bindings, record

    leader_port = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149723-if00"
    follower_port = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149339-if00"
    monkeypatch.setenv("LELAB_ARM_BINDINGS", str(tmp_lerobot_home / "lelab" / "arm_bindings.json"))
    joints = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]
    leader_cal = {n: {"id": i + 1, "drive_mode": 0, "homing_offset": -1000 + i,
                      "range_min": 800, "range_max": 3200} for i, n in enumerate(joints)}
    follower_cal = {n: {"id": i + 1, "drive_mode": 0, "homing_offset": 2000 - i,
                       "range_min": 800, "range_max": 3100} for i, n in enumerate(joints)}

    log: list = []
    robot, teleop, cfg_obj = _recording_devices(tmp_lerobot_home, leader_cal, follower_cal, log)
    cfg_obj.teleop.port = leader_port
    cfg_obj.robot.port = follower_port
    arm_bindings.record(leader_port, "teleop", "left_rig", leader_cal)
    arm_bindings.record(follower_port, "robot", "left_rig", follower_cal)

    verdict = record.bring_up_devices(robot, teleop, cfg_obj)

    assert verdict["refusals"] == []
    assert log == ["connect:follower", "connect:leader", "write:follower", "write:leader"]


def test_recording_refuses_a_calibration_measured_on_the_other_arm(
    tmp_lerobot_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording writes the same boards teleoperation does; refusing only on /move-arm
    would leave the same wrong calibration one button away from being applied."""
    from lelab import arm_bindings, record

    leader_port = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149723-if00"
    follower_port = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149339-if00"
    bindings = tmp_lerobot_home / "lelab" / "arm_bindings.json"
    monkeypatch.setenv("LELAB_ARM_BINDINGS", str(bindings))
    joints = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]
    cal = {n: {"id": i + 1, "drive_mode": 0, "homing_offset": -1000 + i,
               "range_min": 800, "range_max": 3200} for i, n in enumerate(joints)}

    log: list = []
    robot, teleop, cfg_obj = _recording_devices(tmp_lerobot_home, cal, cal, log)
    # The ports are swapped relative to what was measured here.
    cfg_obj.teleop.port = follower_port
    cfg_obj.robot.port = leader_port
    arm_bindings.record(leader_port, "teleop", "left_rig", cal, path=bindings)
    arm_bindings.record(follower_port, "robot", "left_rig", cal, path=bindings)

    with pytest.raises(arm_bindings.ArmIdentityError, match="last calibrated as"):
        record.bring_up_devices(robot, teleop, cfg_obj)

    assert log == [], "a refusal must not open a port or write a board"
