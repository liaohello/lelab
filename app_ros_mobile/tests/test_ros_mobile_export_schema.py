"""Schema-driven export: the teleop layout is arm+gripper only and never zero-fills the chassis."""
from __future__ import annotations

import json

from pathlib import Path

import pytest

from lelab import ros_mobile

TELEOP_META = {
    "source": "hardware_teleop_arm_gripper",
    "runtime_mode": "hardware_teleop",
    "training_export": True,
    "export_schema": "arm_gripper_6dof_phase1",
    "camera_topic": "/ascamera/camera_publisher/rgb0/image",
    "record_joint_state_topic": "/controller_manager/joint_states",
    "base_velocity_topic": None,
    "teleop_state_topic": "/lelab/recording/teleop",
    "features": {
        "fps": 14.74,
        "image": {"name": "observation.images.wrist_real",
                  "topic": "/ascamera/camera_publisher/rgb0/image"},
        "state": {"names": ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"],
                  "units": ["rad"] * 6, "source": "measured_follower_joint_states"},
        "action": {"names": ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"],
                   "units": ["rad"] * 6, "source": "gateway_sent_target_after_clamp",
                   "leader_reading_is_not_action": True},
    },
}

SIM_FEATURES_META = {
    "source": "simulation",
    "training_export": True,
    "camera_topic": "/lelab/wrist/rgb/image_raw",
    "record_joint_state_topic": "/joint_states",
}


def test_simulation_schema_keeps_the_published_nine_dimension_layout():
    schema = ros_mobile._export_schema(SIM_FEATURES_META)
    assert schema["kind"] == "mobile_9dof_sim"
    assert schema["include_base"] is True
    assert schema["action_form"] == "nine_dof_events"
    assert ros_mobile._state_names(schema) == [
        "base_vx", "base_vy", "base_wz", "joint1.position", "joint2.position",
        "joint3.position", "joint4.position", "joint5.position", "gripper_base.position"]
    assert ros_mobile._action_names(schema)[-3:] == ["joint4.target", "joint5.target", "gripper_base.target"]
    assert len(ros_mobile._state_names(schema)) == schema["dims"] == 9


def test_teleop_schema_is_six_dimension_arm_and_gripper_only():
    schema = ros_mobile._export_schema(TELEOP_META)
    assert schema["kind"] == "arm_gripper_6dof_phase1"
    assert schema["include_base"] is False
    assert schema["action_form"] == "six_dof_commands"
    assert schema["dims"] == 6
    assert schema["fps"] == pytest.approx(14.74)
    assert schema["image_name"] == "observation.images.wrist_real"
    names = ros_mobile._state_names(schema)
    assert names == ["joint1.position", "joint2.position", "joint3.position", "joint4.position",
                     "joint5.position", "r_joint.position"]
    assert not any(n.startswith("base_") for n in names), "locked chassis must not be zero-filled"
    assert schema["state_source"] == "measured_follower_joint_states"
    assert schema["action_source"] == "gateway_sent_target_after_clamp"
    assert schema["requires_manual_label"] is True


def _cmd(component, seq, target, stamp, **extra):
    event = {"kind": "command", "component": component, "seq": seq, "target": target,
             "time_ns": stamp, "received_ns": stamp + 5_000_000}
    event.update(extra)
    event["_time_ns"] = stamp
    event["_received_ns"] = stamp + 5_000_000
    return event


def test_resolve_action_holds_arm_and_gripper_separately():
    schema = ros_mobile._export_schema(TELEOP_META)
    arms = [_cmd("arm", 3, [0.1, 0.2, 0.3, 0.4, 0.5], 1_000_000_000,
                 pre_clip=[0.11, 0.2, 0.3, 0.4, 0.5],
                 leader_rad={"shoulder_pan": 0.1}, clutch=True, calibration_version="a|b|c",
                 clipped=["joint1"])]
    grippers = [_cmd("gripper", 4, [0.9], 900_000_000, leader_gripper_percent=42.0, clutch=True)]
    data = {"arms": arms, "grippers": grippers, "events": arms + grippers}
    action, stamp, received, extra = ros_mobile._resolve_action(schema, data, 1_200_000_000, [])
    assert action == [0.1, 0.2, 0.3, 0.4, 0.5, 0.9]
    assert stamp == 900_000_000, "the age gate must use the older of the two command streams"
    assert received == 900_000_000 + 5_000_000
    assert extra["clutch"] is True
    assert extra["calibration_version"] == "a|b|c"
    assert extra["pre_clip_rad"] == [0.11, 0.2, 0.3, 0.4, 0.5]
    assert extra["leader_rad"] == {"shoulder_pan": 0.1}
    assert extra["clipped_joints"] == ["joint1"]
    assert extra["arm_action_age_s"] == pytest.approx(0.2)
    assert extra["gripper_action_age_s"] == pytest.approx(0.3)
    assert extra["action_semantics"]["executed"].startswith("inferred")


def test_resolve_action_refuses_when_one_command_stream_is_missing():
    schema = ros_mobile._export_schema(TELEOP_META)
    arms = [_cmd("arm", 1, [0.0] * 5, 1_000)]
    data = {"arms": arms, "grippers": [], "events": arms}
    action, stamp, received, extra = ros_mobile._resolve_action(schema, data, 1_100, [])
    assert action is None, "no gripper command means no defined action, not a zero"


def test_resolve_action_sim_path_uses_the_nine_dimension_event_vector():
    schema = ros_mobile._export_schema(SIM_FEATURES_META)
    events = [_cmd("arm", 1, [0.0] * 9, 500, action=[1.0] + [0.0] * 8)]
    action, stamp, received, extra = ros_mobile._resolve_action(schema, {"events": events}, 700, events)
    assert action == [1.0] + [0.0] * 8
    assert stamp == 500 and extra == {}


class _Msg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Header:
    def __init__(self, stamp_ns):
        self.stamp = _Msg(sec=stamp_ns // 10**9, nanosec=stamp_ns % 10**9)


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


class _Conn:
    msgtype = "fake"

    def __init__(self, topic):
        self.topic = topic


def test_read_bag_routes_teleop_commands_and_survives_a_missing_base_topic():
    schema = ros_mobile._export_schema(TELEOP_META)
    image = _Msg(header=_Header(1_000), width=640, height=400, encoding="bgr8", data=b"\x00" * 3)
    joints = _Msg(header=_Header(1_050), name=["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"],
                  position=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
    arm_event = _Msg(data=json.dumps({"kind": "command", "component": "arm", "seq": 1, "time_ns": 1_100,
                                      "target": [0.1, 0.2, 0.3, 0.4, 0.5], "action": [0.0] * 9}))
    gripper_event = _Msg(data=json.dumps({"kind": "command", "component": "gripper", "seq": 2,
                                          "time_ns": 1_120, "target": [0.9]}))
    other_event = _Msg(data=json.dumps({"kind": "episode_started", "time_ns": 1_090}))
    messages = [("/lelab/events", other_event, 1_091),
                ("/lelab/events", arm_event, 1_101),
                ("/lelab/events", gripper_event, 1_121),
                ("/controller_manager/joint_states", joints, 1_060),
                ("/ascamera/camera_publisher/rgb0/image", image, 1_010)]
    original = ros_mobile._open_bag_reader
    ros_mobile._open_bag_reader = lambda _path: _FakeReader(messages)
    try:
        data = ros_mobile._read_bag(Path("unused"), 1_000, 2_000,
                                    "/ascamera/camera_publisher/rgb0/image",
                                    "/controller_manager/joint_states", None, schema)
    finally:
        ros_mobile._open_bag_reader = original
    assert [e["component"] for e in data["arms"]] == ["arm"]
    assert [e["component"] for e in data["grippers"]] == ["gripper"]
    assert len(data["events"]) == 2, "non-command events are ignored"
    assert data["bases"] == []
    assert data["states"][0]["names"][0] == "joint1"
    assert data["images"][0]["stamp"] == 1_000
