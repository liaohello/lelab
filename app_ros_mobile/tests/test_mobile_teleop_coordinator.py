"""Coordinator-level guards that only the wizard sequence exposes (no hardware, no car)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lelab.mobile_teleop_coordinator import (BridgeError, CoordinatorConfig,
                                             MobileTeleopCoordinator)


class FakeLeader:
    def __init__(self, port: str):
        self.config = SimpleNamespace(port=port)
        self.close_calls = 0

    def snapshot(self) -> dict:
        return {"connected": True, "healthy": True, "reason": "ok", "reads": 12, "age_s": 0.02,
                "port_claim": "flock+tioxcl", "port": self.config.port}

    def close(self) -> dict:
        self.close_calls += 1
        return {"closed": True}

    def start(self) -> None:
        pass


def coordinator(port: str) -> tuple[MobileTeleopCoordinator, FakeLeader]:
    leader = FakeLeader(port)
    coord = MobileTeleopCoordinator(CoordinatorConfig(), {}, store=object(), leader_reader=leader,
                                    transport=lambda *a, **k: {"ok": True}, lease_manager=object())
    return coord, leader


class FakeLeases:
    def status(self) -> dict:
        return {"state": "held", "task": "teleoperation", "owner": OWNER, "expires_in": 55.0}


class FakeStore:
    def readiness(self, leader: str, follower: str, mapping: str) -> dict:
        return {"ready": True, "calibration_version": "abc123"}


OWNER = "test-operator"


def acquired_status(shape: str, *, feedback_available: bool = True,
                    client_id: str = OWNER, chassis_locked: bool = True) -> MobileTeleopCoordinator:
    """A coordinator that already holds both leases, reading the given car /status shape."""
    feedback = {"available": feedback_available, "reason": None if feedback_available else "no_ack",
                "source": "test", "positions_rad": {"joint1": 0.0}, "age_s": 0.02,
                "missing_joints": [] if feedback_available else ["joint1"]}
    control = {"active": True, "client_id": client_id, "mode": "leader_arm", "expires_in_s": 28.0,
               "estop_latched": False, "last_reject": None, "leader_arm_available": True}
    teleop = {"enabled": True, "state_topic": "/tf", "last": None, "chassis_locked": chassis_locked}
    if shape == "nested":  # what the deployed agent actually returns
        control = {**control, "follower_feedback": feedback, "teleop": teleop}
        status = {"read_only": False, "control": control}
    else:  # the flat shape the earlier stub used
        status = {"read_only": False, "control": control, "follower_feedback": feedback,
                  "teleop": teleop}
    coord = MobileTeleopCoordinator(CoordinatorConfig(), {}, store=FakeStore(),
                                    leader_reader=FakeLeader("/dev/serial/by-id/usb-1a86_A-if00"),
                                    transport=lambda *a, **k: status, lease_manager=FakeLeases())
    coord.identities = {"leader": "so101-leader-A", "follower": "car_A", "mapping": "map_A"}
    coord.token, coord.upper_token, coord.session_owner = "car-token", "upper-token", OWNER
    return coord


def test_gate_reads_feedback_and_teleop_from_the_deployed_nested_control_block():
    """Regression: reading these fields only at the top level left every gate closed on the real car."""
    gate = acquired_status("nested")._gate()
    assert gate.car_lease_valid and gate.exclusive_control
    assert gate.follower_feedback_valid and gate.chassis_locked and gate.upper_lease_valid
    assert gate.missing("following") == []


def test_gate_still_accepts_a_flat_status_shape():
    gate = acquired_status("flat")._gate()
    assert gate.follower_feedback_valid and gate.chassis_locked


def test_a_missing_nested_feedback_is_the_only_follower_blocker():
    missing = acquired_status("nested", feedback_available=False)._gate().missing("following")
    assert missing == ["follower_feedback_missing_or_stale"]


def test_control_held_by_another_client_is_not_exclusive():
    gate = acquired_status("nested", client_id="someone-else")._gate()
    assert "control_not_exclusive" in gate.missing("following")


def test_status_reports_the_observed_chassis_state_not_the_profile_default():
    """The page must not render a config default as the car's real chassis state."""
    coord = acquired_status("nested", chassis_locked=False)
    coord.profile = {"teleop": {"chassis_locked": True}}
    assert coord.status()["chassis_locked"] is None, "nothing observed from the car yet"

    coord._gate()
    out = coord.status()
    assert out["chassis_locked"] is False
    assert out["chassis_locked_profile"] is True
    assert out["gate_age_s"] is not None
    assert "chassis_must_stay_locked_in_phase_1" in out["missing_for_following"]


def test_acquire_reopens_nothing_when_the_same_leader_is_already_live():
    """The wizard opens the serial first; acquire must reuse it instead of 409-ing the operator."""
    coord, leader = coordinator("/dev/serial/by-id/usb-1a86_A-if00")
    out = coord.open_leader("/dev/serial/by-id/usb-1a86_A-if00",
                            leader_identity="so101-leader-A")
    assert coord.leader is leader and leader.close_calls == 0
    assert out["leader"]["connected"] is True
    assert coord.identities["leader"] == "so101-leader-A"


def test_switching_to_a_different_leader_port_is_refused():
    coord, leader = coordinator("/dev/serial/by-id/usb-1a86_A-if00")
    with pytest.raises(BridgeError) as exc:
        coord.open_leader("/dev/serial/by-id/usb-1a86_B-if00", leader_identity="so101-leader-B")
    assert exc.value.status == 409
    assert "release the serial" in exc.value.detail
    assert coord.leader is leader and leader.close_calls == 0, "a refused switch must not drop the live reader"
