"""Read-only observation API tests with a fake bus and fake cameras."""

from __future__ import annotations

import time

import numpy as np
import pytest
from pydantic import ValidationError

from lelab import observe as ob

_ReadOnlyFollowerArm = ob._ReadOnlyFollowerArm


class _FakeContract:
    state_names = ("a.pos", "b.pos")
    camera_keys = ("right",)

    @classmethod
    def from_checkpoint(cls, *_args, **_kwargs):
        return cls()

    def validate_observation(self, observation):
        assert set(self.state_names) <= set(observation)


class _FakeSource:
    instances: list[_FakeSource] = []

    def __init__(self, port, calibration, contract, *, cameras):
        self.port = port
        self.calibration = calibration
        self.cameras = cameras
        self.closed = False
        self.captures = 0
        _FakeSource.instances.append(self)

    def start(self):
        pass

    def capture(self):
        self.captures += 1
        return {
            "a.pos": 1.0,
            "b.pos": 2.0,
            "right": np.zeros((8, 8, 3), dtype=np.uint8),
        }

    def camera_ages_ms(self):
        return {"right": 3.0}

    def torque_state(self):
        return {"a": 0, "b": 0}

    def close(self):
        self.closed = True


class _FakeManager:
    def __init__(self):
        self.messages = []

    def broadcast_joint_data_sync(self, message):
        self.messages.append(message)


def test_observe_start_status_stop_never_sends_actions(monkeypatch):
    monkeypatch.setattr(ob, "PolicyContract", _FakeContract)
    monkeypatch.setattr(ob, "ReadOnlyFollowerSource", _FakeSource)
    ob.reset_observe_state()
    manager = _FakeManager()
    result = ob.handle_start_observe(
        ob.ObserveRequest(port="/dev/fake", calibration="/tmp/fake.json"),
        manager,
    )
    assert result["success"] is True
    deadline = time.monotonic() + 2.0
    while not manager.messages and time.monotonic() < deadline:
        time.sleep(0.02)
    status = ob.handle_observe_status()
    assert status["active"] is True
    assert status["state"] == {"a.pos": 1.0, "b.pos": 2.0}
    assert status["actions_sent"] == 0
    assert status["frames"] >= 1
    assert status["camera_ages_ms"]["right"] == 3.0
    assert ob.camera_frame("right")
    stopped = ob.handle_stop_observe()
    assert stopped["success"] is True
    assert ob.observe_active() is False
    assert _FakeSource.instances[-1].closed is True
    assert all(message["actions_sent"] == 0 for message in manager.messages)
    assert not any("action" in message for message in manager.messages)


def test_observe_stop_is_idempotent(monkeypatch):
    monkeypatch.setattr(ob, "PolicyContract", _FakeContract)
    monkeypatch.setattr(ob, "ReadOnlyFollowerSource", _FakeSource)
    ob.reset_observe_state()
    assert ob.handle_stop_observe()["success"] is True
    assert ob.handle_stop_observe()["already_stopped"] is True


class _FakeBimanualSource:
    instances: list[_FakeBimanualSource] = []

    def __init__(self, left_port, right_port, **kwargs):
        self.left_port = left_port
        self.right_port = right_port
        self.kwargs = kwargs
        self.closed = False
        self.captures = 0
        _FakeBimanualSource.instances.append(self)

    def start(self):
        pass

    def capture(self):
        self.captures += 1
        observation = {}
        for side, offset in (("left", 0), ("right", 100)):
            for index, motor in enumerate(
                (
                    "shoulder_pan",
                    "shoulder_lift",
                    "elbow_flex",
                    "wrist_flex",
                    "wrist_roll",
                    "gripper",
                )
            ):
                observation[f"{side}_{motor}.pos"] = float(offset + index)
        observation["front"] = np.zeros((8, 8, 3), dtype=np.uint8)
        return observation

    def camera_ages_ms(self):
        return {"front": 4.0}

    def torque_state(self):
        return {
            f"{side}_{motor}": 0
            for side in ("left", "right")
            for motor in _ReadOnlyFollowerArm.DEFAULT_MOTOR_IDS
        }

    def arms_summary(self):
        return {
            "left": {"port": self.left_port, "calibration": None, "calibrated": False},
            "right": {
                "port": self.right_port,
                "calibration": "/tmp/right.json",
                "calibrated": True,
            },
        }

    def close(self):
        self.closed = True


class _ReadOnlyFakeBus:
    def __init__(self, port):
        self.port = port
        self.motors = list(_ReadOnlyFollowerArm.DEFAULT_MOTOR_IDS)
        self.is_connected = False
        self.events: list[str] = []

    def connect(self):
        self.is_connected = True

    def sync_read(self, register, normalize=True):
        assert register == "Present_Position"
        self.events.append(f"sync_read:normalize={normalize}")
        offset = 10 if "left" in self.port else 20
        return {name: float(offset + index) for index, name in enumerate(self.motors)}

    def read(self, register, motor, normalize=False):
        assert register == "Torque_Enable"
        return 0

    def disconnect(self, disable_torque=True):
        self.events.append(f"disconnect:{disable_torque}")
        self.is_connected = False

    def write(self, *args, **kwargs):
        self.events.append("real-write")

    def sync_write(self, *args, **kwargs):
        self.events.append("real-sync-write")

    def configure_motors(self, *args, **kwargs):
        self.events.append("real-configure")

    def enable_torque(self, *args, **kwargs):
        self.events.append("real-enable-torque")

    def disable_torque(self, *args, **kwargs):
        self.events.append("real-disable-torque")

    def write_calibration(self, *args, **kwargs):
        self.events.append("real-calibration-write")

    def set_half_turn_homings(self, *args, **kwargs):
        self.events.append("real-half-turn")

    def record_ranges_of_motion(self, *args, **kwargs):
        self.events.append("real-range")

    def setup_motor(self, *args, **kwargs):
        self.events.append("real-setup")


class _ReadOnlyFakeCamera:
    def __init__(self):
        self.released = False

    def read(self):
        return True, np.zeros((8, 8, 3), dtype=np.uint8)

    def release(self):
        self.released = True


def test_bimanual_observe_reports_twelve_values_and_no_actions(monkeypatch):
    monkeypatch.setattr(ob, "ReadOnlyBimanualFollowerSource", _FakeBimanualSource)
    ob.reset_observe_state()
    manager = _FakeManager()
    result = ob.handle_start_bimanual_observe(
        ob.BimanualObserveRequest(
            left_port="/dev/left-fake",
            right_port="/dev/right-fake",
            cameras={"front": {"index": 0, "width": 640, "height": 480, "fps": 30}},
        ),
        manager,
    )
    assert result["success"] is True
    deadline = time.monotonic() + 2.0
    while not manager.messages and time.monotonic() < deadline:
        time.sleep(0.02)

    status = ob.handle_observe_status()
    assert status["mode"] == "bimanual"
    assert len(status["state"]) == 12
    assert set(status["arms"]) == {"left", "right"}
    assert status["bindings"]["right"]["calibrated"] is True
    assert status["actions_sent"] == 0
    assert ob.camera_frame("front")

    stopped = ob.handle_stop_observe()
    assert stopped["success"] is True
    assert _FakeBimanualSource.instances[-1].closed is True
    assert all(message["mode"] == "bimanual" for message in manager.messages)
    assert all(message["actions_sent"] == 0 for message in manager.messages)


def test_bimanual_source_guards_both_buses_and_releases_cameras() -> None:
    cameras = [{"key": "front", "index": 0}]
    source = ob.ReadOnlyBimanualFollowerSource(
        "/dev/left-fake",
        "/dev/right-fake",
        cameras=cameras,
        bus_factory=lambda port, calibration: _ReadOnlyFakeBus(port),
        camera_factory=lambda spec: _ReadOnlyFakeCamera(),
    )
    source.start()
    observation = source.capture()
    assert len(observation) == 13
    assert observation["left_shoulder_pan.pos"] == 10.0
    assert observation["right_gripper.pos"] == 25.0
    assert source.arms["left"]._bus.events[-1] == "sync_read:normalize=False"
    assert source.arms["right"]._bus.events[-1] == "sync_read:normalize=False"
    assert source.forbidden_writes == ()
    assert all(
        event not in bus.events
        for bus in (source.arms["left"]._bus, source.arms["right"]._bus)
        for event in ("real-write", "real-sync-write", "real-disable-torque")
    )

    with pytest.raises(ob.JetsonPolicyError, match="motor write"):
        source.arms["right"]._bus.write("shoulder_pan", 1)
    assert source.forbidden_writes == ("right:write",)

    source.close()
    assert source.arms["left"]._bus is None
    assert source.arms["right"]._bus is None


def test_bimanual_observe_rejects_same_port() -> None:
    with pytest.raises(ValidationError, match="different serial ports"):
        ob.BimanualObserveRequest(left_port="/dev/same", right_port="/dev/same")
