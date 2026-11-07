"""End-to-end export of a teleop session: real LeRobotDataset write and readback, no car involved.

Requirement 八.7 asks that schema-conforming teleop data be read back with the local LeRobot SDK.
Before this test the 6-D arm+gripper layout was only checked at the schema-selection and
bag-parsing seams, so nothing proved that a teleop session survives the actual
create/add_frame/save_episode/finalize path, that the exported ``action`` is the post-clamp
target rather than the leader reading, that ``observation.state`` is the measured follower
feedback, or that an ``unknown`` label keeps the episode exportable while an explicit failure
is dropped. This uses no hardware: only the bag reader and the image-geometry probe are faked,
so a mismatch between what the car records and what the exporter expects fails here instead of
on the car after the operator has recorded real episodes.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from lelab import ros_mobile

WIDTH, HEIGHT = 64, 48
DECLARED_FPS = 14.74  # the car camera's measured rate: fractional, as the real profile declares
SAMPLE_FPS = 15
JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]
ARM = ["joint1", "joint2", "joint3", "joint4", "joint5"]
IMAGE_NAME = "observation.images.wrist_real"
IMAGE_TOPIC = "/ascamera/camera_publisher/rgb0/image"
STATE_TOPIC = "/controller_manager/joint_states"

# Deliberately different numbers for each role, so a wrong pick is visible in the assertion.
ARM_TARGET = [0.10, 0.20, 0.30, 0.40, 0.50]
GRIPPER_TARGET = [0.90]
LEADER_RAD = [1.10, 1.20, 1.30, 1.40, 1.50]
MEASURED = [0.05, 0.15, 0.25, 0.35, 0.45, 0.55]
T0 = 1_000_000_000


class _Stamp:
    def __init__(self, ns: int):
        self.sec = ns // 10**9
        self.nanosec = ns % 10**9


class _Header:
    def __init__(self, ns: int):
        self.stamp = _Stamp(ns)


class _Msg:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class _Conn:
    msgtype = "fake"

    def __init__(self, topic: str):
        self.topic = topic


class _FakeReader:
    def __init__(self, messages):
        self.messages_list = messages

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def messages(self):
        for topic, obj, bag_ts in self.messages_list:
            yield _Conn(topic), bag_ts, obj

    def deserialize(self, raw, _msgtype):
        return raw


def _stream(start_ns: int) -> list:
    """0.6 s of teleop traffic: 20 Hz images and feedback, 5 Hz arm+gripper commands."""
    return _camera_stream(start_ns, 50_000_000, 650_000_000)


def _camera_stream(start_ns: int, period_ns: int, duration_ns: int) -> list:
    """Teleop traffic whose camera+feedback cadence is `period_ns`, not a round number."""
    out = []
    frame = np.full((HEIGHT, WIDTH, 3), 7, dtype=np.uint8)
    steps = duration_ns // period_ns + 1
    for step in range(steps):
        ts = start_ns + step * period_ns
        out.append((IMAGE_TOPIC, _Msg(header=_Header(ts), width=WIDTH, height=HEIGHT,
                                      encoding="bgr8", data=frame.tobytes()), ts + 1_000_000))
        out.append((STATE_TOPIC, _Msg(header=_Header(ts), name=list(JOINTS),
                                      position=list(MEASURED)), ts + 2_000_000))
    for step in range(duration_ns // 200_000_000 + 1):
        ts = start_ns + step * 200_000_000
        arm = {"kind": "command", "component": "arm", "seq": 100 + step, "time_ns": ts,
               "target": list(ARM_TARGET), "clipped": ["joint5"], "pre_clip": [9.9] * 5,
               "leader_rad": list(LEADER_RAD), "clutch": True, "calibration_version": "sha256:abc"}
        gripper = {"kind": "command", "component": "gripper", "seq": 200 + step, "time_ns": ts,
                   "target": list(GRIPPER_TARGET), "clipped": [],
                   "leader_gripper_percent": 55.0, "clutch": True,
                   "calibration_version": "sha256:abc"}
        out.append(("/lelab/events", _Msg(data=json.dumps(arm)), ts + 3_000_000))
        out.append(("/lelab/events", _Msg(data=json.dumps(gripper)), ts + 3_000_000))
    return out


def _episode(episode_id: str, start_ns: int, success, duration_ns: int = 600_000_000) -> dict:
    return {"episode_id": episode_id, "task": "teleop grasp", "source": "leader_arm",
            "status": "saved", "started_time_ns": start_ns,
            "ended_time_ns": start_ns + duration_ns,
            "evaluation": {"success": success, "labels": "manual_or_unknown",
                           "note": None if success == "unknown" else "operator note"}}


def _session(tmp_path: Path, monkeypatch, episodes, session_id: str = "ros_teleopexport",
             messages: list | None = None) -> str:
    root = tmp_path / "upper" / "sessions" / session_id
    (root / "raw" / "bag").mkdir(parents=True)
    manifest = {
        "session_id": session_id, "status": "raw_ready", "ready_eligible": True,
        "profile": "hardware-teleop-v1", "task": "teleop grasp", "episodes": episodes,
        "metadata": {
            "source": "hardware_teleop_arm_gripper", "runtime_mode": "hardware_teleop",
            "training_export": True, "export_schema": "arm_gripper_6dof_phase1",
            "clock_domain": "ros_time", "camera_topic": IMAGE_TOPIC,
            "camera_profile": "wrist_original", "camera_extrinsics": {"verified": False},
            "record_joint_state_topic": STATE_TOPIC, "base_velocity_topic": None,
            "teleop_state_topic": "/lelab/recording/teleop",
            "profile_sha256": "deadbeef",
            "features": {
                "fps": DECLARED_FPS,
                "image": {"name": IMAGE_NAME, "topic": IMAGE_TOPIC},
                "state": {"names": JOINTS, "units": ["rad"] * 6,
                          "source": "measured_follower_joint_states"},
                "action": {"names": JOINTS, "units": ["rad"] * 6,
                           "source": "gateway_sent_target_after_clamp",
                           "leader_reading_is_not_action": True},
            },
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "READY").write_text(json.dumps({"session_id": session_id}), encoding="utf-8")

    messages = messages if messages is not None else _stream(T0) + _stream(T0 + 1_000_000_000)
    monkeypatch.setattr(ros_mobile, "UPPER_SESSIONS", tmp_path / "upper" / "sessions")
    monkeypatch.setattr(ros_mobile, "UPPER_EXPORTS", tmp_path / "upper" / "exports")
    monkeypatch.setattr(ros_mobile, "lerobot_dataset_root", lambda: tmp_path / "datasets")
    monkeypatch.setattr(ros_mobile, "_first_image_geometry", lambda _dir, _topic: (HEIGHT, WIDTH, "bgr8"))
    monkeypatch.setattr(ros_mobile, "_open_bag_reader", lambda _path: _FakeReader(messages))
    return session_id


def test_a_teleop_session_writes_and_reads_back_as_six_dimension_arm_gripper_data(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch, [_episode("ep_unknown", T0, "unknown")])
    report = ros_mobile.export_session(session_id)

    assert report["status"] == "validated"
    assert report["export_schema"] == {"kind": "arm_gripper_6dof_phase1", "dims": 6,
                                       "chassis_included": False}
    assert report["fps"] == SAMPLE_FPS and report["declared_camera_fps"] == DECLARED_FPS
    assert any("whole-number rate" in w for w in report["warnings"]), \
        "a rounded sampling rate must be declared in the report, not hidden"
    assert report["success_labeling"] == "manual_or_unknown_no_simulator_ground_truth"
    assert report["action_source"] == "gateway_sent_target_after_clamp"
    assert report["base_velocity_source"] == "not_included_locked_chassis"
    assert [e["episode_id"] for e in report["episodes"]] == ["ep_unknown"]
    assert report["episodes"][0]["success"] == "unknown", "the unknown label survives into the report"

    from lerobot.datasets import LeRobotDataset
    dataset = LeRobotDataset(repo_id=report["repo_id"], root=Path(report["dataset_root"]))
    try:
        assert int(dataset.meta.total_episodes) == 1
        sample = dataset[0]
        assert IMAGE_NAME in sample, "the teleop camera key must be the one the session recorded"
        assert tuple(sample["action"].shape) == (6,)
        assert tuple(sample["observation.state"].shape) == (6,)
        np.testing.assert_allclose(sample["action"].tolist(), ARM_TARGET + GRIPPER_TARGET, atol=1e-5)
        np.testing.assert_allclose(sample["observation.state"].tolist(), MEASURED, atol=1e-5)
        assert not np.allclose(sample["action"].tolist(), LEADER_RAD + [55.0], atol=1e-3), \
            "the leader reading must never be exported as the action"
    finally:
        dataset = None


def test_per_frame_provenance_keeps_leader_pre_clip_and_sent_targets_separate(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch, [_episode("ep_unknown", T0, "unknown")])
    report = ros_mobile.export_session(session_id)
    detail = report["episodes"][0]["frames_detail"][0]
    assert detail["leader_rad"] == LEADER_RAD
    assert detail["pre_clip_rad"] == [9.9] * 5
    assert detail["clipped_joints"] == ["joint5"]
    assert detail["arm_command_seq"] == 100 and detail["gripper_command_seq"] == 200
    assert detail["clutch"] is True
    assert detail["calibration_version"] == "sha256:abc"
    assert detail["action_semantics"]["executed"].startswith("inferred"), \
        "mechanical execution is never claimed as observed"


def test_only_explicit_failure_labels_are_dropped_by_default(tmp_path, monkeypatch):
    episodes = [_episode("ep_unknown", T0, "unknown"),
                _episode("ep_failed", T0 + 1_000_000_000, False)]
    session_id = _session(tmp_path, monkeypatch, episodes)
    report = ros_mobile.export_session(session_id)
    assert [e["episode_id"] for e in report["episodes"]] == ["ep_unknown"], \
        "unknown stays exportable; only a human-declared failure is excluded"


def test_include_failed_keeps_manually_labelled_failures(tmp_path, monkeypatch):
    episodes = [_episode("ep_unknown", T0, "unknown"),
                _episode("ep_failed", T0 + 1_000_000_000, False)]
    session_id = _session(tmp_path, monkeypatch, episodes, session_id="ros_teleopallframes")
    report = ros_mobile.export_session(session_id, include_failed=True)
    assert [e["episode_id"] for e in report["episodes"]] == ["ep_unknown", "ep_failed"]
    assert [e["success"] for e in report["episodes"]] == ["unknown", False]


def test_re_exporting_a_session_is_refused_rather_than_overwriting(tmp_path, monkeypatch):
    """The published-dataset guard stays in force for teleop exports too."""
    from fastapi import HTTPException

    session_id = _session(tmp_path, monkeypatch, [_episode("ep_unknown", T0, "unknown")])
    ros_mobile.export_session(session_id)
    with pytest.raises(HTTPException) as raised:
        ros_mobile.export_session(session_id)
    assert raised.value.status_code == 409
    assert "already exists" in str(raised.value.detail)


def test_a_teleop_episode_without_command_events_is_refused_instead_of_inventing_actions(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch, [_episode("ep_unknown", T0, "unknown")])

    def without_arm(_path):
        keep = [m for m in _stream(T0)
                if not str(m[1].__dict__.get("data", "")).startswith('{"kind": "command", "component": "arm"')]
        return _FakeReader(keep)

    monkeypatch.setattr(ros_mobile, "_open_bag_reader", without_arm)
    with pytest.raises(ros_mobile.ExportRejected, match="no arm or gripper command events"):
        ros_mobile.export_session(session_id)


def test_the_rounded_sampling_rate_keeps_dataset_and_video_timestamps_agreeing(tmp_path, monkeypatch):
    """Rounding 14.74 Hz up to 15 Hz must not make the timeline lie about wall-clock time.

    The exporter samples on a grid of `1/fps` in *bag* time and LeRobot stamps each frame
    `frame_index / fps`, so both agree with the recording by construction - but that only holds
    if the video the encoder writes has the same spacing, no frame is ever taken from the future,
    and the freshness guard still holds when the camera is slower than the grid. The measurable
    cost of rounding is duplicate camera frames, so this also pins that the duplication is real,
    bounded and visible in the report instead of silently smoothed away.
    """
    import av

    camera_period = int(1e9 / DECLARED_FPS)  # 67_842_605 ns, as measured on the car
    duration = 3_000_000_000
    episode = _episode("ep_timing", T0, "unknown", duration_ns=duration)
    messages = _camera_stream(T0, camera_period, duration)
    session_id = _session(tmp_path, monkeypatch, [episode], session_id="ros_timestamptick",
                          messages=messages)
    report = ros_mobile.export_session(session_id)

    frames = report["episodes"][0]["frames"]
    detail = report["episodes"][0]["frames_detail"]
    assert len(detail) == frames
    assert report["fps"] == SAMPLE_FPS and report["declared_camera_fps"] == DECLARED_FPS

    # Every dataset frame must be at exactly index/SAMPLE_FPS seconds, in bag time.
    grid = [T0 + index * int(1e9 / SAMPLE_FPS) for index in range(frames)]
    assert [row["time_ns"] for row in detail] == grid, \
        "the sampling grid must advance by the same step the encoder will use"

    # Nothing may be read from the future, and the drift must stay inside the production guard.
    assert all(row["image_age_s"] >= 0 for row in detail), "an image newer than its frame time"
    assert max(row["image_age_s"] for row in detail) < 1.0 / DECLARED_FPS + 1e-6
    assert max(row["image_age_s"] for row in detail) < 0.20, "rounded sampling must not breach freshness"

    # The honest price of sampling above the camera rate: some frames reuse one camera image.
    stamps = [row["image_stamp_ns"] for row in detail]
    duplicates = frames - len(set(stamps))
    assert duplicates > 0, "sampling a 14.74 Hz camera at 15 Hz must reuse frames; if this is 0 " \
        "the grid no longer matches the recorded cadence and the timestamps are unverified"
    assert duplicates / frames < 0.05, "rounding may not throw away more than a few percent"
    assert stamps == sorted(stamps), "the camera image backing each frame must never go backwards"

    from lerobot.datasets import LeRobotDataset
    dataset = LeRobotDataset(repo_id=report["repo_id"], root=Path(report["dataset_root"]))
    try:
        timestamps = [float(dataset[i]["timestamp"]) for i in range(frames)]
        frame_index = [int(dataset[i]["frame_index"]) for i in range(frames)]
    finally:
        dataset = None
    assert frame_index == list(range(frames))
    np.testing.assert_allclose(timestamps, [i / SAMPLE_FPS for i in range(frames)], atol=1e-6)
    assert all(b > a for a, b in zip(timestamps, timestamps[1:])), "timestamps must be strictly rising"

    # The mp4 the operator watches has to carry the same clock: one frame per dataset frame,
    # spaced by exactly 1/SAMPLE_FPS.
    video = Path(report["dataset_root"]) / "videos"
    mp4s = sorted(video.glob("**/*.mp4"))
    assert len(mp4s) == 1
    container = av.open(str(mp4s[0]))
    try:
        stream = container.streams.video[0]
        decoded = [frame for frame in container.decode(video=0)]
        assert len(decoded) == frames, "the video frame count must equal the dataset frame count"
        assert float(stream.average_rate) == float(SAMPLE_FPS)
        pts_step = {int(b.pts - a.pts) for a, b in zip(decoded, decoded[1:])}
        assert pts_step == {int(1 / SAMPLE_FPS / stream.time_base)}, \
            f"video frame spacing drifted: {pts_step}"
        span = (int(decoded[-1].pts) - int(decoded[0].pts)
                + int(1 / SAMPLE_FPS / stream.time_base)) * float(stream.time_base)
        assert abs(span - frames / SAMPLE_FPS) < 1e-6, \
            "the clip must last exactly frames/15 s, i.e. the same span the dataset claims"
    finally:
        container.close()


def test_the_web_dataset_viewer_serves_the_exported_teleop_episode(tmp_path, monkeypatch):
    """八.7: the same data has to come back through the endpoints the dataset page calls.

    The viewer is what turns an export into something a human can check, so it must show the
    teleop camera and exactly the six published columns — no nine-column zero fill.
    """
    from lelab import dataset_viewer

    session_id = _session(tmp_path, monkeypatch, [_episode("ep_unknown", T0, "unknown")])
    report = ros_mobile.export_session(session_id)
    root = Path(report["dataset_root"])

    listing = dataset_viewer.list_episodes(root)
    assert listing["cameras"] == ["wrist_real"], "the page must list the camera this session recorded"
    assert listing["robot_type"] == "lelab_mobile_arm_gripper_phase1"
    assert listing["fps"] == SAMPLE_FPS
    entry = listing["episodes"][0]
    assert entry["length"] == report["episodes"][0]["frames"]
    assert entry["task"] == "teleop grasp"

    video = dataset_viewer.video_path(root, 0, "wrist_real")
    assert video is not None and video.is_file() and video.suffix == ".mp4"

    series = dataset_viewer.episode_timeseries(root, 0)
    assert series["total_frames"] == entry["length"]
    assert set(series["series"]) == {"state", "action"}
    assert all(len(row) == 6 for row in series["series"]["action"] + series["series"]["state"]), \
        "the viewer must show six columns, not a zero-filled nine"
    np.testing.assert_allclose(series["series"]["action"][0], ARM_TARGET + GRIPPER_TARGET, atol=1e-5)
    np.testing.assert_allclose(series["series"]["state"][0], MEASURED, atol=1e-5)


CAR_GRIPPER_MIMIC = [0.90, -1.35, -1.35, -0.0104, 0.0104]


def _as_recorded_by_the_car(messages: list) -> list:
    """Rewrite every gripper command into the shape `agent.gripper_command` really emits.

    The exporter used to demand a one-value gripper target, so the mimic set the car records was
    filtered out entirely and every hardware teleop session died with "has no arm or gripper
    command events". The rehearsal produced that real shape; this test keeps the two sides pinned
    together.
    """
    out = []
    for topic, obj, ts in messages:
        if topic == "/lelab/events":
            event = json.loads(obj.data)
            if event.get("component") == "gripper":
                event["target"] = list(CAR_GRIPPER_MIMIC)
                obj = _Msg(data=json.dumps(event))
        out.append((topic, obj, ts))
    return out


def test_the_mimic_set_the_car_records_collapses_to_one_gripper_action_column(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch, [_episode("ep_unknown", T0, "unknown")],
                          messages=_as_recorded_by_the_car(_stream(T0)))
    report = ros_mobile.export_session(session_id)
    assert report["export_schema"]["dims"] == 6, "the mimic set must not widen the action"

    from lerobot.datasets import LeRobotDataset
    dataset = LeRobotDataset(repo_id=report["repo_id"], root=Path(report["dataset_root"]))
    try:
        action = dataset[0]["action"].tolist()
        assert len(action) == 6
        np.testing.assert_allclose(action, ARM_TARGET + [CAR_GRIPPER_MIMIC[0]], atol=1e-5)
    finally:
        dataset = None


# ---------------------------------------------------------------------------
# The restricted offline simulated six-dimensional contract.
#
# These tests exist because the contract is the only way a declared-simulation session can
# reach the exporter at all.  What has to be provable is that it opens a *narrower* door, not
# a wider one: the ordinary entry still refuses, the dataset stays labelled
# simulated_offline / training_eligible=false, and every contradiction between a recorded row
# and the session it claims to belong to stops the export instead of being smoothed over.
# ---------------------------------------------------------------------------

SIM_ROW_TOPIC = "/lelab_offline/recording/teleop"
SIM_JOINTS = JOINTS
SIM_LIMITS = {"joint5": 1.717404}


def _sim_row(tick: int, start_ns: int, *, episode_id: str, phase: str,
             clock_epoch: str = "clock-epoch-1", context: str = "agent",
             frame_seq: int | None = None, feedback_age_s: float = 0.12) -> dict:
    """One teleop row as the control process records it, with all three target kinds apart."""
    frame_seq = tick if frame_seq is None else frame_seq
    sent = [0.10 + 0.01 * tick, 0.20, 0.30, 0.40, SIM_LIMITS["joint5"], 0.05]
    return {
        "kind": "teleop_frame", "data_identity": "simulated_offline",
        "clock_domain": "car_system_time", "stamp_ns": start_ns + tick * 100_000_000,
        "control_clock_domain": "car_process_monotonic",
        "control_mono_ns": 1_000_000 + tick * 100_000_000,
        "session_id": "offline-frames", "session_epoch": 77, "stream_epoch": 2,
        "frame_seq": frame_seq, "sample_seq": tick,
        "channel_seq": {"arm": frame_seq, "gripper": frame_seq},
        "calibration_bundle_hash": "a" * 64,
        "recording_context": context, "recording_session_id": "ros_simulated6d",
        "recording_session_epoch": 77, "episode_id": episode_id, "phase": phase,
        "clock_epoch": clock_epoch, "context_seq": 4,
        "freshness_window_id": 100 + tick, "window_remaining_ms": 180.0,
        "window_age_ms": 12.0, "ttl_ms": {"arm": 150, "gripper": 150},
        "clutch": {"arm": True, "gripper": True},
        "leader_rad": [value + 0.02 for value in sent[:5]],
        "pre_clip_rad": [sent[0], sent[1], sent[2], sent[3], 2.40, sent[5]],
        "action_rad": {name: value for name, value in zip(SIM_JOINTS, sent)},
        "state_rad": {name: value - 0.03 for name, value in zip(SIM_JOINTS, sent)},
        "state_source": "servo_position_readback",
        "feedback_age_s": feedback_age_s, "feedback_scan_span_s": 0.2,
        "confirmation": "driver_sent", "controller_acknowledged": "unavailable",
        "execution_observed": "unavailable", "hardware_effect": "unverified",
    }


def _sim_stream(start_ns: int, episode_id: str, ticks: range, phase: str) -> list:
    out = []
    for tick in ticks:
        row = _sim_row(tick, start_ns, episode_id=episode_id, phase=phase)
        out.append((SIM_ROW_TOPIC, _Msg(data=json.dumps(row)), row["stamp_ns"]))
        ts = row["stamp_ns"]
        frame = np.full((HEIGHT, WIDTH, 3), 9, dtype=np.uint8)
        out.append((IMAGE_TOPIC, _Msg(header=_Header(ts), width=WIDTH, height=HEIGHT,
                                      encoding="bgr8", data=frame.tobytes()), ts))
    return out


def _simulated_session(tmp_path: Path, monkeypatch, rows_messages: list,
                       session_id: str = "ros_simulated6d",
                       manifest_episodes: list | None = None,
                       meta_overrides: dict | None = None) -> str:
    root = tmp_path / "upper" / "sessions" / session_id
    (root / "raw" / "bag").mkdir(parents=True)
    start = T0
    end = T0 + 600_000_000
    episodes = manifest_episodes if manifest_episodes is not None else [
        {"episode_id": "ep_one", "task": "offline grasp", "source": "manual_hybrid",
         "status": "saved", "started_time_ns": start, "ended_time_ns": end,
         "evaluation": {"success": "unknown", "labels": "manual_or_unknown"},
         "phases": [{"phase": "observe", "started_time_ns": start,
                     "ended_time_ns": start + 300_000_000},
                    {"phase": "grasp", "started_time_ns": start + 300_000_000,
                     "ended_time_ns": end}]}]
    metadata = {
        "source": "simulated", "simulated": True, "data_identity": "simulated_offline",
        "profile": "hardware_hybrid_manual_v1_offline6d", "runtime_mode": "simulated_hybrid_manual",
        "training_export": False, "export_schema": "arm_gripper_6dof_phase1",
        "clock_domain": "car_system_time", "clock_epoch": "clock-epoch-1",
        "camera_topic": IMAGE_TOPIC, "camera_profile": "wrist_original",
        "camera_extrinsics": {"verified": False}, "record_joint_state_topic": None,
        "base_velocity_topic": None, "teleop_state_topic": SIM_ROW_TOPIC,
        "profile_sha256": "cafe",
        "feedback_provenance": {"trusted": False, "available": True, "simulated": True,
                                "hardware_measurement": False, "joint_source_kind": "synthetic",
                                "max_state_age_s": 0.5},
        "features": {"fps": 14.0,
                     "image": {"name": "observation.images.wrist_simulated",
                               "topic": IMAGE_TOPIC},
                     "state": {"names": SIM_JOINTS, "units": ["rad"] * 6,
                               "source": "simulated_servo_position_readback"},
                     "action": {"names": SIM_JOINTS, "units": ["rad"] * 6,
                                "source": "simulated_gateway_sent_target_after_clamp",
                                "leader_reading_is_not_action": True}},
    }
    metadata.update(meta_overrides or {})
    (root / "manifest.json").write_text(json.dumps({
        "session_id": session_id, "status": "raw_ready", "ready_eligible": True,
        "profile": metadata["profile"], "task": "offline grasp", "episodes": episodes,
        "metadata": metadata, "clock_domain": metadata["clock_domain"],
        "clock_epoch": metadata["clock_epoch"]}, indent=2), encoding="utf-8")
    (root / "READY").write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
    # The exporter cross-checks every manifest phase against the car's append-only event
    # log, so the fixture has to write that log too: a contract test that skipped it would
    # be testing a session the production path could never have exported.
    with (root / "events.jsonl").open("w", encoding="utf-8") as handle:
        for index, item in enumerate(episodes):
            for phase_index, phase in enumerate(item.get("phases") or []):
                handle.write(json.dumps({
                    "kind": "phase_started", "event_id": f"ev{index}-{phase_index}-start",
                    "session_id": session_id, "episode_id": item["episode_id"],
                    "clock_domain": metadata["clock_domain"],
                    "time_ns": phase["started_time_ns"],
                    "payload": {"phase": phase["phase"]}}) + "\n")
                handle.write(json.dumps({
                    "kind": "phase_ended", "event_id": f"ev{index}-{phase_index}-end",
                    "session_id": session_id, "episode_id": item["episode_id"],
                    "clock_domain": metadata["clock_domain"],
                    "time_ns": phase["ended_time_ns"],
                    "payload": {"phase": phase["phase"]}}) + "\n")
    monkeypatch.setattr(ros_mobile, "UPPER_SESSIONS", tmp_path / "upper" / "sessions")
    monkeypatch.setattr(ros_mobile, "UPPER_EXPORTS", tmp_path / "upper" / "exports")
    monkeypatch.setattr(ros_mobile, "lerobot_dataset_root", lambda: tmp_path / "datasets")
    monkeypatch.setattr(ros_mobile, "_first_image_geometry",
                        lambda _dir, _topic: (HEIGHT, WIDTH, "bgr8"))
    monkeypatch.setattr(ros_mobile, "_open_bag_reader", lambda _path: _FakeReader(rows_messages))
    return session_id


def _sim_rows(start_ns: int, episode_id: str) -> list:
    return (_sim_stream(start_ns, episode_id, range(0, 3), "observe")
            + _sim_stream(start_ns, episode_id, range(3, 6), "grasp"))


def test_the_offline_contract_is_the_only_entry_that_accepts_a_simulated_session(tmp_path, monkeypatch):
    session_id = _simulated_session(tmp_path, monkeypatch, _sim_rows(T0, "ep_one"))
    with pytest.raises(Exception) as normal:
        ros_mobile.export_session(session_id)
    assert "hardware_teleop" in str(normal.value)
    report = ros_mobile.export_session(session_id, simulated_offline=True)
    assert report["export_contract"] == "simulated_offline_6d_v1"
    assert report["data_identity"] == "simulated_offline"
    assert report["training_eligible"] is False
    assert report["status"] == "validated"
    # One dataset frame per recorded row, at the measured row rate rather than the camera's.
    assert report["total_frames"] == 6
    assert report["fps"] == 10
    assert report["declared_camera_fps"] == 14.0


def test_the_simulated_dataset_labels_its_sources_and_refuses_to_claim_measurement(tmp_path, monkeypatch):
    import json as _json
    from lerobot.datasets import LeRobotDataset

    session_id = _simulated_session(tmp_path, monkeypatch, _sim_rows(T0, "ep_one"))
    report = ros_mobile.export_session(session_id, simulated_offline=True)
    root = Path(report["dataset_root"])
    dataset = LeRobotDataset(repo_id=report["repo_id"], root=root)
    assert dataset.features["action"]["names"] == ["joint1.target", "joint2.target",
                                                   "joint3.target", "joint4.target",
                                                   "joint5.target", "r_joint.target"]
    assert dataset.features["observation.state"]["info"]["source"] == \
        "simulated_servo_readback_recorded_by_the_control_process"
    assert dataset.features["action"]["info"]["source"] == \
        "car_side_sent_target_recorded_by_the_control_process"
    assert list(dataset.features["observation.state"]["info"]["units"]) == ["rad"] * 6
    identity = _json.loads((root / "meta" / "lelab_export_identity.json").read_text())
    assert identity["training_eligible"] is False
    assert identity["data_identity"] == "simulated_offline"
    assert identity["clock_epoch"] == "clock-epoch-1"
    phases = _json.loads((root / "meta" / "lelab_phase_index.json").read_text())
    slices = phases["episodes"][0]["phases"]
    assert [item["phase"] for item in slices] == ["observe", "grasp"]
    assert slices[0]["frame_from"] == 0 and slices[0]["frame_to_exclusive"] == 3
    assert slices[1]["frame_from"] == 3 and slices[1]["frame_to_exclusive"] == 6


def test_a_row_that_disagrees_with_the_manifest_phase_stops_the_export(tmp_path, monkeypatch):
    rows = _sim_stream(T0, "ep_one", range(0, 3), "observe") + \
        _sim_stream(T0, "ep_one", range(3, 6), "observe")   # grasp never arrives
    session_id = _simulated_session(tmp_path, monkeypatch, rows)
    with pytest.raises(Exception) as caught:
        ros_mobile.export_session(session_id, simulated_offline=True)
    assert "phase" in str(caught.value)


def test_an_uncontexted_row_is_refused_rather_than_placed_in_an_episode(tmp_path, monkeypatch):
    # The first row arrives before the agent ever opened a context for it.
    orphan = _sim_row(0, T0, episode_id="ep_one", phase="observe", context="missing")
    rows = [(SIM_ROW_TOPIC, _Msg(data=json.dumps(orphan)), orphan["stamp_ns"])] + \
        _sim_stream(T0, "ep_one", range(1, 3), "observe") + \
        _sim_stream(T0, "ep_one", range(3, 6), "grasp")
    session_id = _simulated_session(tmp_path, monkeypatch, rows)
    with pytest.raises(Exception) as caught:
        ros_mobile.export_session(session_id, simulated_offline=True)
    assert "context" in str(caught.value).lower()


def test_a_stale_row_feedback_is_refused_even_though_the_row_stamp_is_fresh(tmp_path, monkeypatch):
    rows = []
    for tick in range(6):
        row = _sim_row(tick, T0, episode_id="ep_one",
                       phase="observe" if tick < 3 else "grasp", feedback_age_s=0.9)
        rows.append((SIM_ROW_TOPIC, _Msg(data=json.dumps(row)), row["stamp_ns"]))
        rows.append((IMAGE_TOPIC, _Msg(header=_Header(row["stamp_ns"]), width=WIDTH, height=HEIGHT,
                                       encoding="bgr8",
                                       data=np.full((HEIGHT, WIDTH, 3), 9, np.uint8).tobytes()),
                     row["stamp_ns"]))
    session_id = _simulated_session(tmp_path, monkeypatch, rows)
    with pytest.raises(Exception) as caught:
        ros_mobile.export_session(session_id, simulated_offline=True)
    assert "stale position" in str(caught.value)


def test_the_contract_closes_when_a_single_identity_fact_is_missing(tmp_path, monkeypatch):
    for field, value in [("data_identity", None), ("simulated", False),
                         ("training_export", True), ("profile", "hardware-hybrid-manual-v1"),
                         ("clock_epoch", None)]:
        session_id = _simulated_session(tmp_path, monkeypatch, _sim_rows(T0, "ep_one"),
                                        session_id=f"ros_check_{field}",
                                        meta_overrides={field: value})
        with pytest.raises(Exception) as caught:
            ros_mobile.export_session(session_id, simulated_offline=True)
        assert "simulated_6d" in str(caught.value), f"{field} was not refused"
