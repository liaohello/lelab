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
from types import SimpleNamespace

import pytest


@pytest.fixture
def saved_dataset_info(tmp_path, monkeypatch):
    from lelab import record

    root = tmp_path / "local" / "interrupted"
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text("{}")
    monkeypatch.setenv("HF_LEROBOT_HOME", str(tmp_path))
    monkeypatch.setenv("LELAB_SESSION_DIAGNOSTICS_DIR", str(tmp_path / "diagnostics"))
    monkeypatch.setattr(record, "recording_active", False)
    monkeypatch.setattr(record, "last_recording_info", None)
    dataset = SimpleNamespace(
        repo_id="local/interrupted", root=root, num_episodes=2, num_frames=42,
        fps=30, features={"action": {}, "observation.state": {}},
        meta=SimpleNamespace(robot_type="bi_so_follower", tasks=SimpleNamespace(index=["pick"])),
    )
    monkeypatch.setattr(record, "LeRobotDataset", lambda *args, **kwargs: dataset)
    monkeypatch.setattr(record, "repair_local_dataset", lambda repo: None)
    return record, dataset


def test_dataset_info_reads_saved_data_after_recording_failure(saved_dataset_info, monkeypatch):
    record, dataset = saved_dataset_info
    failed = {"dataset_repo_id": dataset.repo_id, "success": False, "error": "camera timeout",
              "mode": "bimanual", "saved_episodes": 2, "episodes": [{"frames": 20}, {"frames": 22}]}
    monkeypatch.setattr(record, "last_recording_info", failed)
    calls = []

    def verify(loaded, **kwargs):
        calls.append((loaded, kwargs))
        return {"num_episodes": 2, "num_frames": 42}

    monkeypatch.setattr(record, "verify_finalized_dataset", verify)
    result = record.handle_get_dataset_info(record.DatasetInfoRequest(dataset_repo_id=dataset.repo_id))
    assert result["success"] is True
    assert result["num_episodes"] == 2
    assert result["total_frames"] == 42
    assert result["single_task"] == "pick"
    assert result["recording_error"] == "camera timeout"
    assert result["recording_interrupted"] is True
    assert result["finalize"]["verified"] is True
    assert calls == [(dataset, {"expected_episodes": 2, "expected_frames": 42, "mode": "bimanual"})]
    assert failed["success"] is False  # Metadata success must not erase the failed session.


def test_dataset_info_preserves_interruption_after_service_restart(saved_dataset_info, monkeypatch):
    from lelab.session_diagnostics import SessionDiagnostics

    record, dataset = saved_dataset_info
    diagnostics = SessionDiagnostics(dataset_repo_id=dataset.repo_id, dataset_root=dataset.root,
                                     mode="bimanual", metadata={}, fps=30)
    diagnostics.close({"status": "failed", "error": "camera timeout", "saved_episodes": 2})
    monkeypatch.setattr(record, "verify_finalized_dataset", lambda *args, **kwargs: {"num_frames": 42})
    result = record.handle_get_dataset_info(record.DatasetInfoRequest(dataset_repo_id=dataset.repo_id))
    assert result["success"] is True
    assert result["recording_error"] == "camera timeout"
    assert result["finalize"]["verified"] is True


def test_dataset_info_reports_verification_failure(saved_dataset_info, monkeypatch):
    record, dataset = saved_dataset_info
    monkeypatch.setattr(record, "last_recording_info", {"dataset_repo_id": dataset.repo_id,
                        "success": False, "saved_episodes": 2, "mode": "bimanual"})

    def fail(*args, **kwargs):
        raise RuntimeError("video did not decode")

    monkeypatch.setattr(record, "verify_finalized_dataset", fail)
    result = record.handle_get_dataset_info(record.DatasetInfoRequest(dataset_repo_id=dataset.repo_id))
    assert result["success"] is False
    assert "video did not decode" in result["message"]


def test_dataset_info_does_not_read_or_repair_active_dataset(saved_dataset_info, monkeypatch):
    record, dataset = saved_dataset_info
    monkeypatch.setattr(record, "recording_active", True)
    monkeypatch.setattr(record, "recording_config", SimpleNamespace(dataset_repo_id=dataset.repo_id))
    monkeypatch.setattr(record, "repair_local_dataset", lambda repo: pytest.fail("active data was repaired"))
    result = record.handle_get_dataset_info(record.DatasetInfoRequest(dataset_repo_id=dataset.repo_id))
    assert result["success"] is False
    assert "still active" in result["message"]


def test_dataset_info_missing_local_metadata_never_downloads(tmp_path, monkeypatch):
    from lelab import record

    monkeypatch.setenv("HF_LEROBOT_HOME", str(tmp_path))
    monkeypatch.setattr(record, "recording_active", False)
    monkeypatch.setattr(record, "LeRobotDataset", lambda *a, **kw: pytest.fail("unexpected download"))
    result = record.handle_get_dataset_info(record.DatasetInfoRequest(dataset_repo_id="local/missing"))
    assert result["success"] is False
    assert "not found locally" in result["message"]


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
