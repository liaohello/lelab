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
"""Tests for lelab.teleoperate — request schema and status handlers."""

from __future__ import annotations

import math
from pathlib import Path

import pytest


MOTOR_POS_KEYS = [f"{name}.pos" for name in
                  ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")]


def engage_clutch(teleop, monkeypatch, leader_ref=None, follower_ref=None) -> dict:
    """Pretend the operator already engaged the clutch, with the reference pair given.

    The loop starts disengaged by design, so a test about the follow loop rather than about
    the gate has to say so. reset_clutch is made inert because a session's own bring-up would
    otherwise wipe this back to the default before the loop runs.
    """
    leader_ref = {k: 0.0 for k in MOTOR_POS_KEYS} if leader_ref is None else leader_ref
    follower_ref = dict(leader_ref) if follower_ref is None else follower_ref
    monkeypatch.setattr(teleop, "reset_clutch", lambda: None)
    teleop.clutch.update({"engage_requested": False, "engaged": True, "refusal": None,
                          "alignment": {"leader": leader_ref, "follower": follower_ref}})
    return teleop.clutch["alignment"]

def test_teleoperate_request_rejects_missing_fields() -> None:
    from pydantic import ValidationError

    from lelab.teleoperate import TeleoperateRequest

    with pytest.raises(ValidationError):
        TeleoperateRequest()


def test_handle_teleoperation_status_returns_dict() -> None:
    from lelab.teleoperate import handle_teleoperation_status

    result = handle_teleoperation_status()
    assert isinstance(result, dict)
    # The page shows which arms the live session is driving; with no session that has to be
    # explicitly absent rather than silently missing, or a stale picker selection would read
    # as the active pair.
    assert result["teleoperation_active"] is False
    assert result["session"] is None


def test_handle_get_joint_positions_returns_dict_when_idle() -> None:
    from lelab.teleoperate import handle_get_joint_positions

    result = handle_get_joint_positions()
    assert isinstance(result, dict)


def test_a_partial_reading_is_not_padded_with_zeros() -> None:
    """FakeRobot answers two of six motors. That used to come back as a six-joint
    pose with four zeros in it, which the chart and the 3D view drew as if measured.
    A missing motor is now the caller's problem to show, not ours to invent."""
    from lelab.teleoperate import get_joint_positions_from_robot, read_joint_side
    from tests.mocks import FakeRobot

    robot = FakeRobot()
    robot.connect()

    assert get_joint_positions_from_robot(robot) is None

    side = read_joint_side(lambda: __import__("lelab.teleoperate", fromlist=["x"])
                           .joints_from_motor_reading(robot.get_observation()), "follower")
    assert side["status"] == "partial"
    assert side["joints"] is None
    assert "wrist_flex" in side["missing"]


def test_a_failed_read_is_reported_as_failed() -> None:
    from lelab.teleoperate import read_joint_side

    class _DeadBus:
        def get_observation(self):
            raise RuntimeError("sync_read timed out")

    side = read_joint_side(lambda: __import__("lelab.teleoperate", fromlist=["x"])
                           .joints_from_motor_reading(_DeadBus().get_observation()), "follower")

    assert side["status"] == "read_failed"
    assert side["joints"] is None
    assert "timed out" in side["error"]


def test_get_joint_positions_maps_degrees_to_radians_without_correction() -> None:
    """Every joint maps its Present_Position (degrees around the calibration
    center) straight to radians, deg * pi/180, with no per-joint offset.
    shoulder_lift/elbow_flex used to be run through a correction table; a
    calibration that would have triggered it is supplied here to prove the
    offset is gone and they now map like every other joint."""
    import math

    from lelab.teleoperate import get_joint_positions_from_robot

    class _Cal:
        # The removed correction derived its offset from range_min/range_max.
        range_min = 0
        range_max = 4095

    class _Robot:
        calibration = {"shoulder_lift": _Cal(), "elbow_flex": _Cal()}

        def get_observation(self):
            return {
                "shoulder_pan.pos": 0.0,
                "shoulder_lift.pos": 90.0,
                "elbow_flex.pos": -45.0,
                "wrist_flex.pos": 30.0,
                "wrist_roll.pos": 12.0,
                "gripper.pos": 50.0,
            }

    positions = get_joint_positions_from_robot(_Robot())

    assert positions["Rotation"] == pytest.approx(0.0)
    assert positions["Pitch"] == pytest.approx(math.radians(90.0))
    assert positions["Elbow"] == pytest.approx(math.radians(-45.0))
    assert positions["Wrist_Pitch"] == pytest.approx(math.radians(30.0))
    assert positions["Wrist_Roll"] == pytest.approx(math.radians(12.0))
    assert positions["Jaw"] == pytest.approx(math.radians(50.0))


def test_start_teleoperation_reports_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device that fails to connect must make the start handler return
    success=False (so the UI surfaces the error and doesn't navigate to an
    empty teleop screen) and reset state so a retry isn't blocked. Previously
    the connect ran in a worker thread and the handler always claimed success.
    """
    import lelab.teleoperate as teleop

    monkeypatch.setattr(teleop, "teleoperation_active", False)
    monkeypatch.setattr(teleop, "setup_calibration_files", lambda leader, follower: ("leader", "follower"))

    class _Bus:
        def connect(self) -> None:
            raise RuntimeError("serial port unavailable")

    class _Device:
        def __init__(self, config) -> None:
            self.bus = _Bus()
            self.cameras: dict = {}
            self.disconnected = False

        def disconnect(self) -> None:
            self.disconnected = True

    monkeypatch.setattr(teleop, "SO101Follower", _Device)
    monkeypatch.setattr(teleop, "SO101Leader", _Device)

    request = teleop.TeleoperateRequest(
        leader_port="COM_LEADER",
        follower_port="COM_FOLLOWER",
        leader_config="leader",
        follower_config="follower",
    )
    result = teleop.handle_start_teleoperation(request)

    assert result["success"] is False
    # The message must name the arm that failed (the follower connects first).
    assert "follower" in result["message"].lower()
    assert "COM_FOLLOWER" in result["message"]
    # State must be reset so the next attempt isn't blocked by the mutex.
    assert teleop.teleoperation_active is False


def test_start_teleoperation_injects_local_safety_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config construction is tested with a failing fake bus, so no serial
    device is opened and no worker/control loop can start."""
    import lelab.teleoperate as teleop

    monkeypatch.setattr(teleop, "teleoperation_active", False)
    monkeypatch.setattr(teleop, "setup_calibration_files", lambda leader, follower: ("leader", "follower"))
    monkeypatch.setenv("LELAB_MAX_RELATIVE_TARGET", "2.5")
    monkeypatch.setenv("LELAB_MAX_POSITION_DEVIATION", "9")
    captured: dict = {}

    def follower_config(**kwargs):
        captured.update(kwargs)
        return object()

    class _FailingBus:
        def connect(self) -> None:
            raise RuntimeError("expected test failure")

    class _Follower:
        def __init__(self, config) -> None:
            self.bus = _FailingBus()
            self.cameras = {}

        def disconnect(self) -> None:
            pass

    monkeypatch.setattr(teleop, "SO101FollowerConfig", follower_config)
    monkeypatch.setattr(teleop, "SO101Follower", _Follower)

    request = teleop.TeleoperateRequest(
        leader_port="unused",
        follower_port="unused",
        leader_config="leader",
        follower_config="follower",
    )
    result = teleop.handle_start_teleoperation(request)

    assert result["success"] is False
    assert captured["max_relative_target"] == 2.5
    assert captured["max_position_deviation"] == 9.0


def test_start_teleoperation_disconnects_follower_when_leader_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The partial-connect path: if the follower connects but the leader then
    fails, the follower must be disconnected so its serial port is released.
    """
    import lelab.teleoperate as teleop

    monkeypatch.setattr(teleop, "teleoperation_active", False)
    monkeypatch.setattr(teleop, "setup_calibration_files", lambda leader, follower: ("leader", "follower"))

    class _OkBus:
        def connect(self) -> None:
            pass

    class _FailingBus:
        def connect(self) -> None:
            raise RuntimeError("leader offline")

    class _Follower:
        def __init__(self, config) -> None:
            self.bus = _OkBus()
            self.cameras: dict = {}
            self.disconnected = False

        def disconnect(self) -> None:
            self.disconnected = True

    class _Leader:
        def __init__(self, config) -> None:
            self.bus = _FailingBus()
            self.disconnected = False

        def disconnect(self) -> None:
            self.disconnected = True

    created: dict = {}
    monkeypatch.setattr(
        teleop, "SO101Follower", lambda config: created.setdefault("follower", _Follower(config))
    )
    monkeypatch.setattr(teleop, "SO101Leader", lambda config: created.setdefault("leader", _Leader(config)))

    request = teleop.TeleoperateRequest(
        leader_port="COM_LEADER",
        follower_port="COM_FOLLOWER",
        leader_config="leader",
        follower_config="follower",
    )
    result = teleop.handle_start_teleoperation(request)

    assert result["success"] is False
    assert "leader" in result["message"].lower()
    # The already-connected follower must have been cleaned up.
    assert created["follower"].disconnected is True
    assert teleop.teleoperation_active is False


JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]


def _bus_ids():
    return {name: index + 1 for index, name in enumerate(JOINTS)}


def test_start_teleoperation_refuses_another_arms_calibration_before_opening_a_port(
    monkeypatch: pytest.MonkeyPatch, tmp_lerobot_home,
) -> None:
    """Every arm here uses ids 1..6, so lerobot's bus matches motors by *name* and would
    write the follower's calibration onto the leader with no complaint — smooth, plausible,
    wrong angles. The refusal has to land while both ports are still closed.
    """
    import json

    import lelab.teleoperate as teleop
    from lelab import arm_bindings
    from lelab.utils import config as cfg

    leader_port = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149723-if00"
    follower_port = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149339-if00"
    payload = {name: {"id": index + 1, "drive_mode": 0, "homing_offset": -1000 + index,
                      "range_min": 800, "range_max": 3200} for index, name in enumerate(JOINTS)}
    for directory, name in ((cfg.LEADER_CONFIG_PATH, "leader_rig"), (cfg.FOLLOWER_CONFIG_PATH, "follower_rig")):
        Path(directory).mkdir(parents=True, exist_ok=True)
        Path(directory, f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    bindings = tmp_lerobot_home / "lelab" / "arm_bindings.json"
    monkeypatch.setenv("LELAB_ARM_BINDINGS", str(bindings))
    # Measured evidence: this port is the follower...
    arm_bindings.record(follower_port, "robot", "follower_rig", payload, path=bindings)
    # ...and the request hands it to the leader side.
    monkeypatch.setattr(teleop, "teleoperation_active", False)
    monkeypatch.setattr(teleop, "setup_calibration_files",
                        lambda leader, follower: ("leader_rig", "follower_rig"))

    opened: list[str] = []
    written: list[str] = []

    class _Bus:
        motors = {name: type("M", (), {"id": id_})() for name, id_ in _bus_ids().items()}

        def connect(self) -> None:
            opened.append(self.name)

        def write_calibration(self, calibration) -> None:
            written.append(self.name)

    def _device(kind):
        class _Device:
            def __init__(self, config) -> None:
                self.bus = type("B", (), {"name": kind, "connect": _Bus.connect,
                                          "write_calibration": _Bus.write_calibration,
                                          "motors": _Bus.motors})()
                self.cameras: dict = {}

            def configure(self) -> None:
                pass

            def disconnect(self) -> None:
                pass

        return _Device

    monkeypatch.setattr(teleop, "SO101Follower", _device("follower"))
    monkeypatch.setattr(teleop, "SO101Leader", _device("leader"))

    request = teleop.TeleoperateRequest(
        leader_port=follower_port,
        follower_port=leader_port,
        leader_config="leader_rig",
        follower_config="follower_rig",
    )
    result = teleop.handle_start_teleoperation(request)

    assert result["success"] is False
    assert "Refusing to start teleoperation" in result["message"]
    assert follower_port in result["message"]
    assert "last calibrated as the follower" in result["message"]
    # Nothing reached the hardware: no port opened, no calibration written to a board.
    assert opened == []
    assert written == []
    assert teleop.teleoperation_active is False


def test_broadcast_carries_the_leader_reading_that_drove_the_follower(
    monkeypatch: pytest.MonkeyPatch, tmp_lerobot_home,
) -> None:
    """The workbench draws leader-vs-follower curves, so the same socket has to carry both
    sides. The leader values must come from the reading that was actually sent to the
    follower — not a second bus poll, which would double the serial load and could show a
    position the arm never commanded.
    """
    import json

    import lelab.teleoperate as teleop
    from lelab import arm_bindings
    from lelab.utils import config as cfg

    monkeypatch.setenv("LELAB_ARM_BINDINGS", str(tmp_lerobot_home / "lelab" / "arm_bindings.json"))
    # Real calibration files on disk, but no binding table entries: an arm calibrated
    # before this guard existed must still start, with a warning the page can show.
    payload = {name: {"id": index + 1, "drive_mode": 0, "homing_offset": -1000 + index,
                      "range_min": 800, "range_max": 3200} for index, name in enumerate(JOINTS)}
    for directory, name in ((cfg.LEADER_CONFIG_PATH, "leader_rig"), (cfg.FOLLOWER_CONFIG_PATH, "follower_rig")):
        Path(directory).mkdir(parents=True, exist_ok=True)
        Path(directory, f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    leader_port = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149652-if00"
    follower_port = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B91045575-if00"

    class _Ledger:
        def __init__(self) -> None:
            self.actions: list[dict] = []
            self.leader_reads = 0

    ledger = _Ledger()

    def _bus():
        return type("B", (), {"motors": {}, "connect": lambda self: None,
                              "write_calibration": lambda self, calibration, cache=True: None})()

    class _Leader:
        def __init__(self, config) -> None:
            self.bus = _bus()
            self.calibration: dict = {}

        def get_action(self):
            # Each read moves every joint one degree on, so a stale or re-polled value is
            # distinguishable from the one that was sent.
            ledger.leader_reads += 1
            return {f"{name}.pos": float(ledger.leader_reads + 10 * index)
                    for index, name in enumerate(JOINTS)}

        def configure(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

    class _Follower:
        def __init__(self, config) -> None:
            self.bus = _bus()
            self.calibration: dict = {}
            self.cameras: dict = {}

        def send_action(self, action):
            ledger.actions.append(dict(action))

        def get_observation(self):
            return {f"{name}.pos": 0.0 for name in JOINTS}

        def configure(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

    monkeypatch.setattr(teleop, "teleoperation_active", False)
    monkeypatch.setattr(teleop, "setup_calibration_files", lambda leader, follower: ("leader_rig", "follower_rig"))
    monkeypatch.setattr(teleop, "SO101Follower", _Follower)
    monkeypatch.setattr(teleop, "SO101Leader", _Leader)

    sent: list[dict] = []

    sessions: list = []

    class _Manager:
        active_connections = ["client"]

        def broadcast_joint_data_sync(self, data) -> None:
            sent.append(json.loads(json.dumps(data)))
            # What the status endpoint would report at this moment: the session must already
            # know the ports and calibration files it is driving.
            sessions.append(teleop.handle_teleoperation_status()["session"])

    # Run the worker inline and end the loop on its first sleep, so exactly one
    # iteration — and one broadcast — happens under the test.
    class _InlineThread:
        def __init__(self, target, *args, **kwargs) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

        def is_alive(self) -> bool:
            return False

        def join(self, timeout=None) -> None:
            pass

    def _stop_after_one_iteration(_seconds) -> None:
        teleop.teleoperation_active = False

    monkeypatch.setattr(teleop.threading, "Thread", _InlineThread)
    engage_clutch(teleop, monkeypatch)
    monkeypatch.setattr(teleop.time, "sleep", _stop_after_one_iteration)

    request = teleop.TeleoperateRequest(
        leader_port=leader_port, follower_port=follower_port,
        leader_config="leader_rig", follower_config="follower_rig",
    )
    result = teleop.handle_start_teleoperation(request, _Manager())

    assert result["success"] is True, result["message"]
    assert len(sent) == 1, sent
    frame = sent[0]
    assert set(frame) == {"type", "joints", "leader", "follower_status", "leader_status",
                          "follower_missing", "leader_missing", "timestamp", "preview",
                          "target_sent", "target", "clutch"}
    assert frame["preview"] is False and frame["target_sent"] is True
    assert frame["clutch"]["engaged"] is True and frame["clutch"]["aligned"] is True
    # The identity alignment this test pre-seeded makes the relative target equal the leader
    # reading, so the target block and the leader block must agree.
    assert frame["target"]["Rotation"] == pytest.approx(frame["leader"]["Rotation"])
    assert frame["follower_status"] == frame["leader_status"] == "measured"
    assert frame["joints"] is not None and frame["leader"] is not None
    # The leader block is the reading that was actually sent, converted — the broadcast
    # must not poll the leader bus a second time.
    assert ledger.leader_reads == len(ledger.actions) == 1
    assert frame["leader"]["Rotation"] == pytest.approx(math.radians(1.0))
    assert frame["leader"]["Pitch"] == pytest.approx(math.radians(11.0))
    assert frame["joints"]["Rotation"] == 0.0
    assert len(result["identity_warnings"]) == 2
    assert all("no recorded measurement" in w for w in result["identity_warnings"])
    assert sessions[0] is not None and sessions[0]["leader_port"] == leader_port
    assert sessions[0]["follower_port"] == follower_port
    assert sessions[0]["leader_config"] == "leader_rig"
    # Teardown clears it: a finished session must not keep presenting its old arms.
    assert teleop.teleoperation_session is None



def test_a_dead_follower_bus_still_streams_the_leader_without_faking_a_pose(
    monkeypatch: pytest.MonkeyPatch, tmp_lerobot_home
) -> None:
    """The failure mode that matters on a real bench: the follower bus dies mid-session.

    The old code broadcast a six-joint zero pose every frame, so the page showed the arm
    snapping to zero and the follow error became |target - 0|. The frame now carries
    joints=None with follower_status=read_failed, keeps the leader reading, and the loop
    keeps running — a lost reading is a visible state, not a fake one.
    """
    import json

    import lelab.teleoperate as teleop
    from lelab.utils import config as cfg

    monkeypatch.setenv("LELAB_ARM_BINDINGS", str(tmp_lerobot_home / "lelab" / "arm_bindings.json"))
    for directory in (cfg.LEADER_CONFIG_PATH, cfg.FOLLOWER_CONFIG_PATH):
        Path(directory).mkdir(parents=True, exist_ok=True)

    sent: list[dict] = []

    class _Bus:
        motors = {}

        def connect(self) -> None:
            pass

        def write_calibration(self, calibration, cache=True) -> None:
            pass

    class _Leader:
        def __init__(self, config) -> None:
            self.bus = _Bus()
            self.calibration: dict = {}

        def get_action(self):
            return {f"{name}.pos": 12.5 for name in
                    ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]}

        def configure(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

    class _Follower:
        def __init__(self, config) -> None:
            self.bus = _Bus()
            self.calibration: dict = {}
            self.cameras: dict = {}
            self.sent = 0

        def send_action(self, action):
            self.sent += 1

        def get_observation(self):
            raise RuntimeError("follower bus unplugged")

        def configure(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

    follower = {}

    def _make_follower(config):
        return follower.setdefault("device", _Follower(config))

    monkeypatch.setattr(teleop, "teleoperation_active", False)
    monkeypatch.setattr(teleop, "setup_calibration_files", lambda leader, follower_: ("a", "b"))
    monkeypatch.setattr(teleop, "SO101Follower", _make_follower)
    monkeypatch.setattr(teleop, "SO101Leader", lambda config: _Leader(config))

    class _Manager:
        active_connections = ["client"]

        def broadcast_joint_data_sync(self, data) -> None:
            sent.append(json.loads(json.dumps(data)))

    class _InlineThread:
        def __init__(self, target, *args, **kwargs) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

        def is_alive(self) -> bool:
            return False

        def join(self, timeout=None) -> None:
            pass

    def _stop_after_one_iteration(_seconds) -> None:
        teleop.teleoperation_active = False

    monkeypatch.setattr(teleop.threading, "Thread", _InlineThread)
    engage_clutch(teleop, monkeypatch)
    monkeypatch.setattr(teleop.time, "sleep", _stop_after_one_iteration)

    result = teleop.handle_start_teleoperation(
        teleop.TeleoperateRequest(leader_port="/dev/serial/by-id/lead", follower_port="/dev/serial/by-id/foll",
                                  leader_config="a", follower_config="b"),
        _Manager(),
    )

    assert result["success"] is True, result["message"]
    frame = sent[0]
    assert frame["joints"] is None
    assert frame["follower_status"] == "read_failed"
    assert frame["leader_status"] == "measured"
    assert frame["leader"]["Rotation"] == pytest.approx(12.5 * math.pi / 180.0)
    assert follower["device"].sent == 1, "the follow loop keeps sending while the read is broken"


def test_the_session_stops_itself_when_the_follower_stops_answering(
    monkeypatch: pytest.MonkeyPatch, tmp_lerobot_home
) -> None:
    """需求三/六：反馈读不到就不许继续跟随。

    The follow loop is local, so an unplugged follower bus does not by itself make the
    arm move wrongly — but continuing to command while every measured frame fails is
    flying blind. After the configured number of consecutive unreadable frames the
    worker ends the session (and its own teardown disconnects both arms), and the
    status endpoint says why.
    """
    import lelab.teleoperate as teleop
    from lelab.utils import config as cfg

    monkeypatch.setenv("LELAB_ARM_BINDINGS", str(tmp_lerobot_home / "lelab" / "arm_bindings.json"))
    monkeypatch.setenv("LELAB_MAX_FOLLOWER_READ_FAILURES", "3")
    for directory in (cfg.LEADER_CONFIG_PATH, cfg.FOLLOWER_CONFIG_PATH):
        Path(directory).mkdir(parents=True, exist_ok=True)

    class _Bus:
        motors = {}

        def connect(self) -> None:
            pass

        def write_calibration(self, calibration, cache=True) -> None:
            pass

    class _Leader:
        def __init__(self, config) -> None:
            self.bus = _Bus()
            self.calibration: dict = {}

        def get_action(self):
            return {f"{name}.pos": 5.0 for name in
                    ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]}

        def configure(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

    follower: dict = {}

    class _Follower:
        def __init__(self, config) -> None:
            self.bus = _Bus()
            self.calibration: dict = {}
            self.cameras: dict = {}
            self.sent = 0
            self.disconnected = False

        def send_action(self, action):
            self.sent += 1

        def get_observation(self):
            raise RuntimeError("follower unplugged")

        def configure(self) -> None:
            pass

        def disconnect(self) -> None:
            self.disconnected = True

    monkeypatch.setattr(teleop, "teleoperation_active", False)
    monkeypatch.setattr(teleop, "stop_reason", None)
    monkeypatch.setattr(teleop, "setup_calibration_files", lambda leader, follower_: ("a", "b"))
    monkeypatch.setattr(teleop, "SO101Follower", lambda config: follower.setdefault("d", _Follower(config)))
    monkeypatch.setattr(teleop, "SO101Leader", lambda config: _Leader(config))

    # A clock that advances past the 20 FPS throttle on every call, so each iteration
    # really attempts a follower read, and a hard cap so a broken guard cannot hang.
    clock = {"t": 1000.0}
    ticks = {"n": 0}

    def fake_time() -> float:
        clock["t"] += 0.1
        return clock["t"]

    def fake_sleep(_seconds) -> None:
        ticks["n"] += 1
        if ticks["n"] > 20:
            teleop.teleoperation_active = False

    class _InlineThread:
        def __init__(self, target, *args, **kwargs) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

        def is_alive(self) -> bool:
            return False

        def join(self, timeout=None) -> None:
            pass

    monkeypatch.setattr(teleop.time, "time", fake_time)
    engage_clutch(teleop, monkeypatch)
    monkeypatch.setattr(teleop.time, "sleep", fake_sleep)
    monkeypatch.setattr(teleop.threading, "Thread", _InlineThread)

    result = teleop.handle_start_teleoperation(
        teleop.TeleoperateRequest(leader_port="/dev/serial/by-id/lead", follower_port="/dev/serial/by-id/foll",
                                  leader_config="a", follower_config="b"),
        None,
    )

    assert result["success"] is True
    status = teleop.handle_teleoperation_status()
    assert status["teleoperation_active"] is False
    assert status["stop_reason"] is not None
    assert "follower_feedback_unavailable" in status["stop_reason"]
    assert follower["d"].sent == 3, "commands stop once the feedback is declared unavailable"
    assert follower["d"].disconnected is True, "the worker's own teardown released the port"


def test_a_recovered_follower_read_does_not_end_the_session(
    monkeypatch: pytest.MonkeyPatch, tmp_lerobot_home
) -> None:
    """A couple of blips on a 1 kHz bus is normal; only a streak of them is flying blind.

    Same harness as the stop test, but the follower answers again after two failures, so
    the streak resets and the session is ended only by the test's own iteration cap.
    """
    import lelab.teleoperate as teleop
    from lelab.utils import config as cfg

    monkeypatch.setenv("LELAB_ARM_BINDINGS", str(tmp_lerobot_home / "lelab" / "arm_bindings.json"))
    monkeypatch.setenv("LELAB_MAX_FOLLOWER_READ_FAILURES", "3")
    for directory in (cfg.LEADER_CONFIG_PATH, cfg.FOLLOWER_CONFIG_PATH):
        Path(directory).mkdir(parents=True, exist_ok=True)

    class _Bus:
        motors = {}

        def connect(self) -> None:
            pass

        def write_calibration(self, calibration, cache=True) -> None:
            pass

    class _Leader:
        def __init__(self, config) -> None:
            self.bus = _Bus()
            self.calibration: dict = {}

        def get_action(self):
            return {"shoulder_pan.pos": 5.0, "shoulder_lift.pos": 5.0, "elbow_flex.pos": 5.0,
                    "wrist_flex.pos": 5.0, "wrist_roll.pos": 5.0, "gripper.pos": 5.0}

        def configure(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

    follower: dict = {}

    class _Follower:
        def __init__(self, config) -> None:
            self.bus = _Bus()
            self.calibration: dict = {}
            self.cameras: dict = {}
            self.sent = 0
            self.attempts = 0

        def send_action(self, action):
            self.sent += 1

        def get_observation(self):
            self.attempts += 1
            if self.attempts <= 2:
                raise RuntimeError("transient bus error")
            return {f"{name}.pos": 5.0 for name in
                    ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]}

        def configure(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

    monkeypatch.setattr(teleop, "teleoperation_active", False)
    monkeypatch.setattr(teleop, "stop_reason", None)
    monkeypatch.setattr(teleop, "setup_calibration_files", lambda leader, follower_: ("a", "b"))
    monkeypatch.setattr(teleop, "SO101Follower", lambda config: follower.setdefault("d", _Follower(config)))
    monkeypatch.setattr(teleop, "SO101Leader", lambda config: _Leader(config))

    clock = {"t": 1000.0}
    ticks = {"n": 0}

    def fake_time() -> float:
        clock["t"] += 0.1
        return clock["t"]

    def fake_sleep(_seconds) -> None:
        ticks["n"] += 1
        if ticks["n"] > 8:
            teleop.teleoperation_active = False

    class _InlineThread:
        def __init__(self, target, *args, **kwargs) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

        def is_alive(self) -> bool:
            return False

        def join(self, timeout=None) -> None:
            pass

    monkeypatch.setattr(teleop.time, "time", fake_time)
    engage_clutch(teleop, monkeypatch)
    monkeypatch.setattr(teleop.time, "sleep", fake_sleep)
    monkeypatch.setattr(teleop.threading, "Thread", _InlineThread)

    result = teleop.handle_start_teleoperation(
        teleop.TeleoperateRequest(leader_port="/dev/serial/by-id/lead", follower_port="/dev/serial/by-id/foll",
                                  leader_config="a", follower_config="b"),
        None,
    )

    assert result["success"] is True
    status = teleop.handle_teleoperation_status()
    assert status["stop_reason"] is None, status["stop_reason"]
    assert follower["d"].sent == 9, f"the session should run to the test cap, sent={follower['d'].sent}"


def test_the_failure_limit_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    from lelab.teleoperate import max_follower_read_failures

    monkeypatch.setenv("LELAB_MAX_FOLLOWER_READ_FAILURES", "2")
    assert max_follower_read_failures() == 2
    monkeypatch.setenv("LELAB_MAX_FOLLOWER_READ_FAILURES", "not-a-number")
    assert max_follower_read_failures() == 10, "a bad value must not disable the guard"
    monkeypatch.setenv("LELAB_MAX_FOLLOWER_READ_FAILURES", "-5")
    assert max_follower_read_failures() == 1, "a non-positive limit would stop every session instantly"


class _Bus:
    """The bus shape handle_start_teleoperation() drives before the loop: connect, then
    write_calibration. A fake that skips it never reaches send_action()."""

    motors = {}

    def connect(self) -> None:
        pass

    def write_calibration(self, calibration, cache=True) -> None:
        pass


def _run_one_session_with(monkeypatch, tmp_lerobot_home, leader=None, follower_factory=None,
                          preview=False, failure_limit="1000", engage=True, ticks=1,
                          engage_after=None, leader_ref=None, follower_ref=None,
                          engage_in_request=False):
    """Start a session with the loop running inline, so a hardware failure inside it is
    exercised through the real worker code rather than a re-implementation.

    Returns (status, devices, broadcast frames): the frames are what the page would see.
    """
    import lelab.teleoperate as teleop
    from lelab.utils import config as cfg

    monkeypatch.setenv("LELAB_ARM_BINDINGS", str(tmp_lerobot_home / "lelab" / "arm_bindings.json"))
    monkeypatch.setenv("LELAB_MAX_FOLLOWER_READ_FAILURES", failure_limit)
    for directory in (cfg.LEADER_CONFIG_PATH, cfg.FOLLOWER_CONFIG_PATH):
        Path(directory).mkdir(parents=True, exist_ok=True)

    full = {f"{name}.pos": 5.0 for name in
            ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]}

    class _Leader:
        def __init__(self, config) -> None:
            self.bus = _Bus()
            self.calibration: dict = {}

        def get_action(self):
            return leader() if leader else dict(full)

        def configure(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

    class _Follower:
        def __init__(self, config) -> None:
            self.bus = _Bus()
            self.calibration: dict = {}
            self.cameras: dict = {}
            self.sent = 0
            self.configured = 0

        def send_action(self, action):
            self.sent += 1

        def get_observation(self):
            return dict(full)

        def configure(self) -> None:
            self.configured += 1

        def disconnect(self) -> None:
            pass

    made: dict = {}
    monkeypatch.setattr(teleop, "teleoperation_active", False)
    monkeypatch.setattr(teleop, "stop_reason", None)
    monkeypatch.setattr(teleop, "setup_calibration_files", lambda a, b: ("a", "b"))
    monkeypatch.setattr(teleop, "SO101Follower",
                        lambda config: made.setdefault("f", (follower_factory or _Follower)(config)))
    monkeypatch.setattr(teleop, "SO101Leader", lambda config: made.setdefault("l", _Leader(config)))

    class _InlineThread:
        def __init__(self, target, *args, **kwargs) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

        def is_alive(self) -> bool:
            return False

        def join(self, timeout=None) -> None:
            pass

    tick_count = {"n": 0}

    def _sleep(_seconds) -> None:
        tick_count["n"] += 1
        if engage_after is not None and tick_count["n"] == engage_after:
            teleop.handle_teleop_clutch(True)
        if tick_count["n"] >= ticks:
            teleop.teleoperation_active = False

    monkeypatch.setattr(teleop.threading, "Thread", _InlineThread)
    monkeypatch.setattr(teleop.time, "sleep", _sleep)

    frames: list[dict] = []

    class _Manager:
        active_connections = [object()]

        def broadcast_joint_data_sync(self, data) -> None:
            frames.append(data)

    if engage_in_request:
        monkeypatch.setattr(teleop, "reset_clutch", lambda: None)
        teleop.clutch.update({"engage_requested": False, "engaged": False,
                              "alignment": None, "refusal": None})
    elif engage:
        engage_clutch(teleop, monkeypatch, leader_ref=leader_ref, follower_ref=follower_ref)
    else:
        monkeypatch.setattr(teleop, "reset_clutch", lambda: None)
        teleop.clutch.update({"engage_requested": False, "engaged": False,
                              "alignment": None, "refusal": None})

    teleop.handle_start_teleoperation(
        teleop.TeleoperateRequest(leader_port="/dev/serial/by-id/lead", follower_port="/dev/serial/by-id/foll",
                                  leader_config="a", follower_config="b", preview=preview,
                                  engage=engage_in_request),
        _Manager(),
    )
    return teleop.handle_teleoperation_status(), made, frames


def test_a_dead_leader_bus_names_the_leader(
    monkeypatch: pytest.MonkeyPatch, tmp_lerobot_home
) -> None:
    def _read():
        raise RuntimeError("leader usb unplugged")

    status, made, _frames = _run_one_session_with(monkeypatch, tmp_lerobot_home, leader=_read)

    assert status["teleoperation_active"] is False
    assert status["stop_reason"].startswith("leader_read_failed")
    assert "leader usb unplugged" in status["stop_reason"]
    assert "f" not in made or made["f"].sent == 0


def test_a_follower_that_refuses_the_command_names_the_follower(
    monkeypatch: pytest.MonkeyPatch, tmp_lerobot_home
) -> None:
    class _Follower:
        def __init__(self, config) -> None:
            self.bus = _Bus()
            self.calibration: dict = {}
            self.cameras: dict = {}
            self.sent = 0

        def send_action(self, action):
            self.sent += 1
            raise RuntimeError("goal write rejected")

        def get_observation(self):
            return {}

        def configure(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

    status, made, _frames = _run_one_session_with(monkeypatch, tmp_lerobot_home,
                                                  follower_factory=lambda config: _Follower(config))

    assert status["stop_reason"].startswith("follower_send_failed")
    assert "goal write rejected" in status["stop_reason"]
    assert made["f"].sent == 1, "the reason must come from the write the follower actually refused"


def test_a_preview_session_writes_nothing_to_the_follower(
    monkeypatch: pytest.MonkeyPatch, tmp_lerobot_home
) -> None:
    status, made, frames = _run_one_session_with(monkeypatch, tmp_lerobot_home, preview=True)

    assert made["f"].sent == 0, "a preview must not reach the bus at all"
    assert made["f"].configured == 0, "configure() is what puts torque on the follower"
    assert status["stop_reason"] is None
    assert frames, "the page still needs the mapped target to preview against"
    assert frames[0]["preview"] is True and frames[0]["target_sent"] is False
    assert frames[0]["leader"], "the leader reading is the target being previewed"
    assert frames[0]["joints"], "and the follower's measured feedback is still read"


def test_a_following_session_still_configures_and_sends(
    monkeypatch: pytest.MonkeyPatch, tmp_lerobot_home
) -> None:
    _status, made, frames = _run_one_session_with(monkeypatch, tmp_lerobot_home)

    assert made["f"].configured == 1 and made["f"].sent == 1
    assert frames[0]["preview"] is False and frames[0]["target_sent"] is True


def test_a_preview_session_does_not_end_itself_when_the_follower_goes_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_lerobot_home
) -> None:
    """Without measured feedback the rule is "no following"; a preview sends nothing, so
    the same outage is a gap on screen rather than a reason to tear the session down."""
    class _SilentFollower:
        def __init__(self, config) -> None:
            self.bus = _Bus()
            self.calibration: dict = {}
            self.cameras: dict = {}
            self.sent = 0
            self.configured = 0

        def send_action(self, action):
            self.sent += 1

        def get_observation(self):
            raise RuntimeError("bus offline")

        def configure(self) -> None:
            self.configured += 1

        def disconnect(self) -> None:
            pass

    status, made, frames = _run_one_session_with(
        monkeypatch, tmp_lerobot_home, preview=True, failure_limit="1",
        follower_factory=lambda config: _SilentFollower(config))

    assert status["stop_reason"] is None
    assert frames[0]["follower_status"] == "read_failed" and frames[0]["joints"] is None
    assert made["f"].sent == 0


def _disengaged_clutch(teleop) -> None:
    teleop.clutch.update({"engage_requested": False, "engaged": False,
                          "alignment": None, "refusal": None})


def test_a_started_session_sends_nothing_until_the_clutch_is_engaged(
    monkeypatch: pytest.MonkeyPatch, tmp_lerobot_home
) -> None:
    """The operator has to ask for following; opening a session is not that request."""
    status, made, frames = _run_one_session_with(monkeypatch, tmp_lerobot_home, engage=False)

    assert made["f"].sent == 0, "a live session that was never engaged must not write a goal"
    assert status["stop_reason"] is None
    assert frames[0]["clutch"] == {"engage_requested": False, "engaged": False,
                                   "aligned": False, "refusal": None}
    assert frames[0]["target_sent"] is False


def test_engaging_captures_the_reference_pair_and_then_sends_relative_targets(
    monkeypatch: pytest.MonkeyPatch, tmp_lerobot_home
) -> None:
    """Iteration 1 measures, the request is honoured on it, iteration 2 sends follower_ref + delta."""
    made_seen: dict = {}

    def _follower(config):
        arm = _RecordingFollower(config)
        made_seen["arm"] = arm
        return arm

    status, made, frames = _run_one_session_with(
        monkeypatch, tmp_lerobot_home, engage=False, ticks=2, engage_after=1,
        follower_factory=_follower)

    assert status["clutch"]["engaged"] is True and status["clutch"]["aligned"] is True
    assert made["f"].sent == 1, made_seen["arm"].last
    assert made_seen["arm"].last == {k: 2.0 for k in MOTOR_POS_KEYS}, \
        "the leader has not moved since the reference, so the target is the follower's own pose"


def test_engaging_is_refused_while_the_follower_cannot_be_measured(
    monkeypatch: pytest.MonkeyPatch, tmp_lerobot_home
) -> None:
    class _BlindFollower:
        def __init__(self, config) -> None:
            self.bus = _Bus()
            self.calibration: dict = {}
            self.cameras: dict = {}
            self.sent = 0
            self.configured = 0

        def send_action(self, action):
            self.sent += 1

        def get_observation(self):
            raise RuntimeError("bus offline")

        def configure(self) -> None:
            self.configured += 1

        def disconnect(self) -> None:
            pass

    status, made, _frames = _run_one_session_with(
        monkeypatch, tmp_lerobot_home, engage=False, ticks=3, engage_after=1,
        follower_factory=lambda config: _BlindFollower(config))

    assert made["f"].sent == 0
    assert status["clutch"]["engaged"] is False
    assert status["clutch"]["refusal"].startswith("follower_feedback_not_measured")


def test_releasing_the_clutch_clears_the_reference_so_reengaging_realines(
    monkeypatch: pytest.MonkeyPatch, tmp_lerobot_home
) -> None:
    import lelab.teleoperate as teleop

    teleop.teleoperation_active = True
    teleop.teleoperation_session = {"preview": False}
    teleop.clutch.update({"engage_requested": False, "engaged": True, "refusal": None,
                          "alignment": {"leader": {k: 10.0 for k in MOTOR_POS_KEYS},
                                        "follower": {k: 3.0 for k in MOTOR_POS_KEYS}}})

    released = teleop.handle_teleop_clutch(False)
    assert released["engaged"] is False and released["aligned"] is False
    assert teleop.clutch["alignment"] is None, "resuming from the old reference would replay it"

    refused = teleop.handle_teleop_clutch(True)
    assert refused["success"] is True and refused["engage_requested"] is True

    teleop.teleoperation_session = {"preview": True}
    preview = teleop.handle_teleop_clutch(True)
    assert preview["success"] is False and preview["refusal"] == "preview_session_sends_nothing"

    teleop.teleoperation_active = False
    assert teleop.handle_teleop_clutch(True)["success"] is False


class _RecordingFollower:
    """A follower that remembers the goal it was last given, so a relative target can be read back."""

    def __init__(self, config) -> None:
        self.bus = _Bus()
        self.calibration: dict = {}
        self.cameras: dict = {}
        self.sent = 0
        self.configured = 0
        self.last: dict | None = None

    def send_action(self, action):
        self.sent += 1
        self.last = dict(action)

    def get_observation(self):
        return {k: 2.0 for k in MOTOR_POS_KEYS}

    def configure(self) -> None:
        self.configured += 1

    def disconnect(self) -> None:
        pass


def test_a_following_request_engages_on_its_first_measured_frame(
    monkeypatch: pytest.MonkeyPatch, tmp_lerobot_home
) -> None:
    """The operator asked to follow when starting the session, so no separate clutch call is needed."""
    _status, made, frames = _run_one_session_with(
        monkeypatch, tmp_lerobot_home, engage=False, engage_in_request=True, ticks=2)

    assert made["f"].sent == 1
    assert frames[0]["target_sent"] is False, "the first frame only measures; the reference comes from it"
