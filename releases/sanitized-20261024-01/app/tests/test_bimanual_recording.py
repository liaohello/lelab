from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from pydantic import ValidationError

from lelab.record import RecordingRequest
from lelab.rig import (
    JOINT_NAMES,
    SIDE_JOINTS,
    BimanualRig,
    RigConfigError,
    assert_bimanual_robot_contract,
    build_bimanual_configs,
)
from lelab.utils.devices import connect_recording_devices, physical_devices, safe_disconnect_device


def rig_payload():
    return {
        side: {
            "leader_port": f"/dev/{side}-leader",
            "follower_port": f"/dev/{side}-follower",
            "leader_config": f"{side}-leader.json",
            "follower_config": f"{side}-follower.json",
        }
        for side in ("left", "right")
    }


def write_calibration(path: Path, seed: int) -> None:
    path.write_text(
        json.dumps(
            {
                joint: {
                    "id": index + 1,
                    "drive_mode": 0,
                    "homing_offset": seed + index,
                    "range_min": 100,
                    "range_max": 4000,
                }
                for index, joint in enumerate(SIDE_JOINTS)
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def calibration_dirs(monkeypatch, tmp_path):
    import lelab.rig as rig

    leader_dir = tmp_path / "leaders"
    follower_dir = tmp_path / "followers"
    leader_dir.mkdir()
    follower_dir.mkdir()
    for index, name in enumerate(("left-leader.json", "right-leader.json")):
        write_calibration(leader_dir / name, seed=index * 100 + 1)
    for index, name in enumerate(("left-follower.json", "right-follower.json")):
        write_calibration(follower_dir / name, seed=index * 100 + 11)
    monkeypatch.setattr(rig, "LEADER_CONFIG_PATH", str(leader_dir))
    monkeypatch.setattr(rig, "FOLLOWER_CONFIG_PATH", str(follower_dir))
    return leader_dir, follower_dir


def test_bimanual_rejects_duplicate_devices_and_cross_side_calibration(calibration_dirs):
    rig = rig_payload()
    rig["right"]["leader_port"] = rig["left"]["follower_port"]
    with pytest.raises(ValidationError, match="four distinct"):
        RecordingRequest(
            bimanual=rig,
            dataset_repo_id="local/demo",
            single_task="fold",
            cameras={"front": {"type": "opencv", "camera_index": 0, "width": 64, "height": 48, "fps": 30}},
        )

    rig = rig_payload()
    rig["right"]["follower_config"] = rig["left"]["follower_config"]
    with pytest.raises(ValidationError, match="independent calibration"):
        RecordingRequest(
            bimanual=rig,
            dataset_repo_id="local/demo",
            single_task="fold",
            cameras={"front": {"type": "opencv", "camera_index": 0, "width": 64, "height": 48, "fps": 30}},
        )

    rig = rig_payload()
    rig["right"]["leader_config"] = "left-follower.json"
    parsed = RecordingRequest(
        bimanual=rig,
        dataset_repo_id="local/demo",
        single_task="fold",
        cameras={"front": {"type": "opencv", "camera_index": 0, "width": 64, "height": 48, "fps": 30}},
    )
    # Role misuse is caught by the role-specific calibration directory, not by
    # the shape-only model validator.
    with pytest.raises(RigConfigError, match="Missing right leader calibration"):
        from lelab.rig import calibration_manifest

        calibration_manifest(parsed.bimanual)


def test_bimanual_rejects_shared_camera_device_and_requires_cameras():
    rig = rig_payload()
    with pytest.raises(ValidationError, match="at least one camera"):
        RecordingRequest(bimanual=rig, dataset_repo_id="local/demo", single_task="fold")
    with pytest.raises(ValidationError, match="same capture device"):
        RecordingRequest(
            bimanual=rig,
            dataset_repo_id="local/demo",
            single_task="fold",
            cameras={
                "front": {"type": "opencv", "camera_index": 0, "width": 64, "height": 48, "fps": 30},
                "left_wrist": {"type": "opencv", "camera_index": 0, "width": 64, "height": 48, "fps": 30},
            },
        )


class _FakeSOFollower:
    def __init__(self, config):
        self.config = config
        self.id = config.id
        self.is_connected = False
        self.calibration = None
        self.cameras = dict(config.cameras)

    @property
    def _motors_ft(self):
        return {f"{joint}.pos": float for joint in SIDE_JOINTS}

    @property
    def _cameras_ft(self):
        return dict.fromkeys(self.cameras, (24, 32, 3))


class _FakeSOLeader:
    def __init__(self, config):
        self.config = config
        self.id = config.id
        self.is_connected = False
        self.calibration = None

    @property
    def action_features(self):
        return {f"{joint}.pos": float for joint in SIDE_JOINTS}

    @property
    def feedback_features(self):
        return self.action_features


def _patch_bimanual_leaves(monkeypatch):
    import lerobot.robots.bi_so_follower.bi_so_follower as follower_mod
    import lerobot.teleoperators.bi_so_leader.bi_so_leader as leader_mod

    monkeypatch.setattr(follower_mod, "SOFollower", _FakeSOFollower)
    monkeypatch.setattr(leader_mod, "SOLeader", _FakeSOLeader)


def test_bimanual_configs_preserve_child_ids_safety_and_top_level_cameras(
    calibration_dirs, monkeypatch
):
    from lerobot.cameras.configs import Cv2Backends
    from lerobot.cameras.opencv import OpenCVCameraConfig
    from lerobot.robots import make_robot_from_config
    from lerobot.teleoperators import make_teleoperator_from_config

    _patch_bimanual_leaves(monkeypatch)
    monkeypatch.setenv("LELAB_MAX_RELATIVE_TARGET", "2.5")
    monkeypatch.setenv("LELAB_MAX_POSITION_DEVIATION", "9")
    monkeypatch.setenv("LELAB_CALIBRATION_RANGE_MARGIN", "-1")
    rig = BimanualRig.model_validate(rig_payload())
    camera = OpenCVCameraConfig(
        index_or_path=0,
        backend=Cv2Backends.V4L2,
        width=64,
        height=48,
        fps=30,
        fourcc="MJPG",
    )
    def setup(leader: str, follower: str):
        return Path(leader).stem, Path(follower).stem

    robot_config, teleop_config, metadata = build_bimanual_configs(
        rig, {"front": camera}, setup_calibration_files_fn=setup
    )
    robot = make_robot_from_config(robot_config)
    teleop = make_teleoperator_from_config(teleop_config)

    assert robot.left_arm.id == "left-follower"
    assert robot.right_arm.id == "right-follower"
    assert teleop.left_arm.id == "left-leader"
    assert teleop.right_arm.id == "right-leader"
    for arm in (robot.left_arm, robot.right_arm):
        assert arm.config.max_relative_target == 2.5
        assert arm.config.max_position_deviation == 9.0
        assert arm.config.calibration_range_margin == -1.0
    assert list(robot.action_features) == list(JOINT_NAMES)
    assert list(robot.observation_features)[:12] == list(JOINT_NAMES)
    assert list(robot.cameras) == ["front"]
    assert metadata["action_semantics"] == "sent_target_after_local_safety_clamps"
    assert metadata["sync"] == "software_synchronized_sequential_bus_io"
    assert metadata["devices"]["left_follower"]["calibration"]["sha256"]
    assert set(metadata["devices"]) == {
        "left_leader",
        "left_follower",
        "right_leader",
        "right_follower",
    }
    assert_bimanual_robot_contract(robot, ["front"])


def test_arm_camera_key_collision_is_prefixed_not_overwritten(calibration_dirs, monkeypatch):
    from lerobot.cameras.configs import Cv2Backends
    from lerobot.cameras.opencv import OpenCVCameraConfig
    from lerobot.robots.bi_so_follower import BiSOFollower, BiSOFollowerConfig
    from lerobot.robots.so_follower import SO101FollowerConfig

    _patch_bimanual_leaves(monkeypatch)
    left_cam = OpenCVCameraConfig(index_or_path=1, width=32, height=24, fps=10, backend=Cv2Backends.V4L2)
    right_cam = OpenCVCameraConfig(index_or_path=2, width=32, height=24, fps=10, backend=Cv2Backends.V4L2)
    config = BiSOFollowerConfig(
        id="pair",
        left_arm_config=SO101FollowerConfig(
            id="left-follower", port="/dev/lf", cameras={"wrist": left_cam}
        ),
        right_arm_config=SO101FollowerConfig(
            id="right-follower", port="/dev/rf", cameras={"wrist": right_cam}
        ),
        cameras={},
    )
    robot = BiSOFollower(config)
    assert set(robot.cameras) == {"left_wrist", "right_wrist"}
    assert robot.cameras["left_wrist"] is not robot.cameras["right_wrist"]
    assert robot.observation_features["left_wrist"] == (24, 32, 3)
    assert robot.observation_features["right_wrist"] == (24, 32, 3)


class _Bus:
    def __init__(self, events, name, fail=False):
        self.events = events
        self.name = name
        self.fail = fail
        self.closed = False

    def connect(self):
        self.events.append((self.name, "connect"))
        if self.fail:
            raise RuntimeError(f"{self.name} offline")

    def write_calibration(self, calibration):
        self.events.append((self.name, "calibration"))


class _Camera:
    def __init__(self, events, name, fail=False):
        self.events = events
        self.name = name
        self.fail = fail
        self.connected = False

    def connect(self):
        self.events.append((self.name, "camera-connect"))
        if self.fail:
            raise RuntimeError(f"{self.name} camera offline")
        self.connected = True

    def disconnect(self):
        self.events.append((self.name, "camera-disconnect"))
        self.connected = False


class _Leaf:
    def __init__(self, events, name, *, cameras=None, fail_connect=False, fail_camera=False):
        self.id = name
        self.calibration = {"joint": 1}
        self.cameras = cameras or {}
        self.bus = _Bus(events, name, fail=fail_connect)
        self.events = events
        self.fail_camera = fail_camera

    def configure(self):
        self.events.append((self.id, "configure"))

    def disconnect(self):
        self.events.append((self.id, "disconnect"))


def test_connect_helper_releases_everything_after_partial_failure():
    events = []
    camera = _Camera(events, "front", fail=True)
    left_follower = _Leaf(events, "lf", cameras={"front": camera})
    right_follower = _Leaf(events, "rf", fail_connect=True)
    left_leader = _Leaf(events, "ll")
    right_leader = _Leaf(events, "rl")
    robot = SimpleNamespace(left_arm=left_follower, right_arm=right_follower)
    teleop = SimpleNamespace(left_arm=left_leader, right_arm=right_leader)

    with pytest.raises(RuntimeError, match="rf offline"):
        connect_recording_devices(robot, teleop)

    assert events.count(("lf", "connect")) == 1
    assert events.count(("rf", "connect")) == 1
    # The follower bus failed before any leader bus/camera/configuration step.
    assert ("ll", "connect") not in events
    assert ("rl", "connect") not in events
    assert ("lf", "calibration") not in events  # all buses connect before calibration
    assert not camera.connected
    assert ("lf", "configure") not in events


def test_connect_helper_connects_shared_camera_once():
    events = []
    camera = _Camera(events, "front")
    left = _Leaf(events, "lf", cameras={"front": camera})
    right = _Leaf(events, "rf")
    robot = SimpleNamespace(left_arm=left, right_arm=right)
    connect_recording_devices(robot, None)
    assert events.count(("front", "camera-connect")) == 1
    assert [event for _, event in events] == [
        "connect",
        "connect",
        "calibration",
        "calibration",
        "camera-connect",
        "configure",
        "configure",
    ]


def test_partial_bimanual_disconnect_releases_both_children():
    called = []
    left = SimpleNamespace(disconnect=lambda: called.append("left"))
    right = SimpleNamespace(disconnect=lambda: called.append("right"))
    wrapper = SimpleNamespace(left_arm=left, right_arm=right, is_connected=False)
    safe_disconnect_device(wrapper, logging.getLogger(__name__))
    assert called == ["left", "right"]
    assert physical_devices(wrapper) == [left, right]


def test_stop_is_idempotent_and_does_not_discard_current_segment(monkeypatch):
    import lelab.record as record

    monkeypatch.setattr(record, "recording_active", True)
    monkeypatch.setattr(record, "current_phase", "recording")
    events = {"stop_recording": False, "exit_early": False, "rerecord_episode": False}
    monkeypatch.setattr(record, "recording_events", events)

    first = record.handle_stop_recording()
    second = record.handle_stop_recording()
    assert first["success"] is True
    assert second["success"] is True
    assert events["stop_recording"] is True
    assert events["exit_early"] is True
    assert events["rerecord_episode"] is False
    assert record.current_phase == "stopping"

    rerecord = record.handle_rerecord_episode()
    assert rerecord["success"] is False
    assert events["rerecord_episode"] is False


def test_status_exposes_snapshot_finalize_and_camera_health(monkeypatch):
    import lelab.record as record

    snapshot = {
        "mode": "bimanual",
        "sequence": 4,
        "monotonic_s": __import__("time").monotonic(),
        "arms": {
            "left": {"joints_rad": {"Rotation": 0.1}, "joints_deg": {"shoulder_pan": 5.0}},
            "right": {"joints_rad": {"Rotation": -0.1}, "joints_deg": {"shoulder_pan": -5.0}},
        },
        "errors": [],
    }
    monkeypatch.setattr(record, "recording_active", True)
    monkeypatch.setattr(record, "current_phase", "recording")
    monkeypatch.setattr(record, "latest_joint_snapshot", snapshot)
    monkeypatch.setattr(record, "finalize_state", {"state": "finalizing", "verified": False})
    monkeypatch.setattr(record, "saved_episode_details", [{"episode_index": 0, "frames": 3}])
    monkeypatch.setattr(record, "current_robot", None)
    request = SimpleNamespace(
        dataset_repo_id="local/demo",
        mode="bimanual",
        cameras={"front": {}},
        fps=30,
        num_episodes=2,
        episode_time_s=30,
        reset_time_s=10,
        lelab_rig={"action_semantics": "sent", "sync": "software"},
    )
    monkeypatch.setattr(record, "recording_config", request)
    monkeypatch.setattr(record, "recording_events", {"stop_recording": False})
    monkeypatch.setattr(record, "phase_start_time", None)
    monkeypatch.setattr(record, "recording_start_time", None)

    status = record.handle_recording_status()
    assert status["mode"] == "bimanual"
    assert status["joint_snapshot"]["age_ms"] >= 0
    assert status["finalize_state"]["state"] == "finalizing"
    assert status["saved_episode_details"][0]["frames"] == 3
    assert status["camera_health"][0]["name"] == "front"
    assert status["available_controls"]["save_episode"] is True


def test_remote_bimanual_client_command_uses_child_ids_and_safety():
    from lelab.remote_inference import _build_client_command
    from lelab.rollout import InferenceRequest

    request = InferenceRequest(
        bimanual=BimanualRig.model_validate(rig_payload()),
        policy_ref="/models/bimanual",
        cameras={"front": {"type": "opencv", "camera_index": 0, "width": 64, "height": 48, "fps": 30}},
        target="remote",
        policy_type="act",
    )
    cmd = _build_client_command(
        request,
        follower_id="lelab_bimanual",
        local_port=28081,
        remote_policy_path="/models/bimanual",
    )
    joined = " ".join(cmd)
    assert "--robot.type=bi_so_follower" in joined
    assert "--robot.left_arm_config.id=left-follower" in joined
    assert "--robot.right_arm_config.id=right-follower" in joined
    assert "--robot.left_arm_config.port=/dev/left-follower" in joined
    assert "--robot.right_arm_config.port=/dev/right-follower" in joined
    assert "--robot.max_relative_target" not in joined
    assert "--robot.left_arm_config.max_relative_target" in joined
    assert "--robot.right_arm_config.max_relative_target" in joined


def test_stop_recording_does_not_release_lease_early(monkeypatch):
    from fastapi.testclient import TestClient

    from lelab import server

    monkeypatch.setenv("LELAB_HARDWARE_LEASE_REQUIRED", "1")
    client = TestClient(server.app)
    headers = {"X-LeLab-Role": "operator"}
    acquired = client.post(
        "/hardware/lease/acquire",
        json={"task": "recording", "owner": "ui"},
        headers=headers,
    )
    assert acquired.status_code == 200
    monkeypatch.setattr(
        server,
        "handle_stop_recording",
        lambda: {"success": True, "session_ending": True, "message": "accepted"},
    )
    stopped = client.post("/stop-recording", headers=headers)
    assert stopped.status_code == 200
    assert stopped.json()["success"] is True
    held = client.get("/hardware/lease", headers=headers).json()
    assert held["state"] == "held"
    assert held["task"] == "recording"
    server.hardware_lease.preempt(reason="test cleanup")


def test_policy_contract_rejects_single_arm_act():
    from lelab.remote_inference import RemoteInferenceError, validate_bimanual_policy_contract

    with pytest.raises(RemoteInferenceError, match="state_dim=12"):
        validate_bimanual_policy_contract(
            {"state_dim": 6, "action_dim": 6},
            ["front"],
        )
    with pytest.raises(RemoteInferenceError, match="explicit policy contract"):
        validate_bimanual_policy_contract(None, ["front"])
    contract = {
        "state_dim": 12,
        "action_dim": 12,
        "camera_keys": ["observation.images.front", "observation.images.left_wrist"],
        "normalization_mapping": {"ACTION": "MEAN_STD", "OBSERVATION": "MEAN_STD"},
        "processor": "act-v1",
        "action_semantics": "sent_target_after_local_safety_clamps",
    }
    assert validate_bimanual_policy_contract(contract, ["front", "left_wrist"]) == contract
    with pytest.raises(RemoteInferenceError, match="camera keys"):
        validate_bimanual_policy_contract(contract, ["front"])


def test_timeout_saves_instead_of_discarding(monkeypatch, tmp_path):
    import lelab.record as record
    import lerobot.datasets
    import lerobot.robots
    import lerobot.teleoperators
    from lelab.rig import JOINT_NAMES
    from lerobot.configs.dataset import DatasetRecordConfig

    operations = []
    cameras = ["front"]
    features = {**dict.fromkeys(JOINT_NAMES, float), "front": (48, 64, 3)}
    robot = SimpleNamespace(
        name="bi_so_follower",
        action_features=dict.fromkeys(JOINT_NAMES, float),
        observation_features=features,
        cameras={"front": None},
        get_observation=lambda: {},
    )
    teleop = MagicMock()

    class Dataset:
        root = tmp_path / "timeout-dataset"
        fps = 30
        num_episodes = 0
        features = {}

        def __init__(self):
            self.writer = SimpleNamespace(episode_buffer={"size": 0})
            self.finalized = False

        def save_episode(self):
            operations.append("save")
            self.num_episodes += 1
            self.writer.episode_buffer = {"size": 0}

        def clear_episode_buffer(self):
            operations.append("discard")

        def finalize(self):
            if not self.finalized:
                operations.append("finalize")
                self.finalized = True

    dataset = Dataset()

    class FakeManager:
        def __init__(self, target):
            self.target = target

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.target.finalize()
            return False

    def loop(**kwargs):
        if kwargs.get("dataset") is None:
            return
        # Simulate control_time_s elapsing without Save/Stop: no tracking flag.
        kwargs["dataset"].writer.episode_buffer = {"size": 2}

    monkeypatch.setattr(lerobot.robots, "make_robot_from_config", lambda _: robot)
    monkeypatch.setattr(lerobot.teleoperators, "make_teleoperator_from_config", lambda _: teleop)
    monkeypatch.setattr(lerobot.datasets, "LeRobotDataset", SimpleNamespace(create=lambda *a, **kw: dataset))
    monkeypatch.setattr("lerobot.datasets.video_utils.VideoEncodingManager", FakeManager)
    monkeypatch.setattr("lerobot.scripts.lerobot_record.record_loop", loop)
    monkeypatch.setattr(record, "connect_recording_devices", lambda *args: None)
    monkeypatch.setattr(record, "safe_disconnect_device", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        record,
        "verify_finalized_dataset",
        lambda *args, **kwargs: {"num_episodes": 1, "num_frames": 2},
    )
    monkeypatch.setenv("LELAB_SESSION_DIAGNOSTICS_DIR", str(tmp_path / "diag"))
    cfg = SimpleNamespace(
        robot=object(),
        teleop=object(),
        resume=False,
        display_data=False,
        play_sounds=False,
        lelab_rig={"mode": "bimanual", "action_semantics": "sent", "sync": "software"},
        lelab_camera_names=cameras,
        dataset=DatasetRecordConfig(
            repo_id="local/timeout",
            root=str(tmp_path / "capture"),
            num_episodes=1,
            episode_time_s=1,
            reset_time_s=0,
            push_to_hub=False,
        ),
    )
    record.record_with_web_events(
        cfg, {"stop_recording": False, "exit_early": False, "rerecord_episode": False}
    )
    assert operations == ["save", "finalize"]
    assert record.finalize_state["state"] == "completed"


def test_two_real_episodes_plus_partial_last_with_three_videos(monkeypatch, tmp_path):
    """Actual record_loop + v3 writer + MP4 decode, with no serial/camera access."""

    import lelab.record as record
    import lerobot.datasets
    import lerobot.robots
    import lerobot.teleoperators
    from lelab.rig import JOINT_NAMES
    from lerobot.configs import RGBEncoderConfig
    from lerobot.configs.dataset import DatasetRecordConfig
    from lerobot.datasets import LeRobotDataset
    from lerobot.teleoperators import Teleoperator

    camera_names = ["front", "left_wrist", "right_wrist"]
    events = {"stop_recording": False, "exit_early": False, "rerecord_episode": False}
    count = 0

    def observation():
        nonlocal count
        count += 1
        obs = {
            name: float(index + count)
            for index, name in enumerate(JOINT_NAMES)
        }
        for channel, camera in enumerate(camera_names):
            image = np.zeros((48, 64, 3), dtype=np.uint8)
            image[:, :, channel] = 120 + channel * 40
            obs[camera] = image
        if count in (3, 6):
            events["exit_early"] = True
        if count == 8:
            events["stop_recording"] = True
        return obs

    class FakeCamera:
        is_connected = True
        fps = 30

        def __init__(self):
            self.latest_timestamp = __import__("time").perf_counter()

    robot = SimpleNamespace(
        name="bi_so_follower",
        id="pair",
        action_features=dict.fromkeys(JOINT_NAMES, float),
        observation_features={
            **dict.fromkeys(JOINT_NAMES, float),
            **dict.fromkeys(camera_names, (48, 64, 3)),
        },
        cameras={name: FakeCamera() for name in camera_names},
        get_observation=observation,
        send_action=lambda action: {key: value + 100.0 for key, value in action.items()},
    )
    teleop = MagicMock(spec=Teleoperator)
    teleop.get_action.return_value = dict.fromkeys(JOINT_NAMES, 5.0)

    monkeypatch.setattr(lerobot.robots, "make_robot_from_config", lambda _: robot)
    monkeypatch.setattr(lerobot.teleoperators, "make_teleoperator_from_config", lambda _: teleop)
    monkeypatch.setattr(record, "connect_recording_devices", lambda *args: None)
    monkeypatch.setattr(record, "safe_disconnect_device", lambda *args, **kwargs: None)
    monkeypatch.setenv("LELAB_SESSION_DIAGNOSTICS_DIR", str(tmp_path / "diagnostics"))

    cfg = SimpleNamespace(
        robot=object(),
        teleop=object(),
        resume=False,
        display_data=False,
        play_sounds=False,
        lelab_rig={
            "mode": "bimanual",
            "action_semantics": "sent_target_after_local_safety_clamps",
            "sync": "software_synchronized_sequential_bus_io",
            "devices": {},
        },
        lelab_camera_names=camera_names,
        dataset=DatasetRecordConfig(
            repo_id="local/bimanual_synthetic",
            root=str(tmp_path / "capture"),
            fps=30,
            num_episodes=3,
            episode_time_s=30,
            reset_time_s=0,
            video=True,
            push_to_hub=False,
            streaming_encoding=False,
            rgb_encoder=RGBEncoderConfig(vcodec="h264"),
            encoder_threads=1,
            num_image_writer_processes=0,
            num_image_writer_threads_per_camera=1,
        ),
    )
    record.record_with_web_events(cfg, events)

    reopened = LeRobotDataset(
        "local/bimanual_synthetic", root=cfg.dataset.root, video_backend="pyav"
    )
    assert reopened.num_episodes == 3
    assert reopened.num_frames == 8
    assert reopened.features["observation.state"]["names"] == list(JOINT_NAMES)
    assert reopened.features["action"]["names"] == list(JOINT_NAMES)
    assert reopened.features["action"]["shape"] == (12,)
    sample = reopened[3]
    assert sample["observation.state"].shape == (12,)
    assert float(sample["action"].mean()) == pytest.approx(105.0)
    for channel, camera in enumerate(camera_names):
        image = sample[f"observation.images.{camera}"]
        assert tuple(image.shape) == (3, 48, 64)
        assert image.mean(dim=(1, 2)).argmax().item() == channel

    assert record.finalize_state["state"] == "completed"
    assert record.finalize_state["verified"] is True
    assert set(record.finalize_state["verification"]["decoded_videos"]) == {
        f"observation.images.{camera}" for camera in camera_names
    }
    diag_path = Path(record.session_diagnostics_info["path"])
    assert len((diag_path / "frames.jsonl").read_text().splitlines()) == 8
    manifest = json.loads((diag_path / "session.json").read_text())
    assert manifest["action_semantics"] == "sent_target_after_local_safety_clamps"
    assert manifest["cycles_recorded"] == 8
