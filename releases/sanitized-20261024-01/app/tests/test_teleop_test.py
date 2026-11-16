"""Single-arm teleoperation test evidence: identity, telemetry and confirmation.

No serial port is opened here; the follow loop runs against fake devices.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def rig_root(tmp_path, monkeypatch):
    """Point robot records, calibration dirs and results at a scratch root."""

    from lelab.utils import config

    robots = tmp_path / "robots"
    robots.mkdir()
    leader_dir = tmp_path / "calibration" / "teleoperators" / "so_leader"
    follower_dir = tmp_path / "calibration" / "robots" / "so_follower"
    leader_dir.mkdir(parents=True)
    follower_dir.mkdir(parents=True)

    monkeypatch.setattr(config, "ROBOTS_PATH", str(robots))
    monkeypatch.setattr(config, "LEADER_CONFIG_PATH", str(leader_dir))
    monkeypatch.setattr(config, "FOLLOWER_CONFIG_PATH", str(follower_dir))
    monkeypatch.setattr(config, "TELEOP_TEST_DIR", str(tmp_path / "teleop-tests"))

    (leader_dir / "left_rig.json").write_text(json.dumps({"shoulder_pan": {"id": 1}}))
    (follower_dir / "left_rig.json").write_text(json.dumps({"shoulder_pan": {"id": 1}}))
    record = {
        "name": "left_rig",
        "leader_port": "/dev/serial/by-id/fake-leader",
        "follower_port": "/dev/serial/by-id/fake-follower",
        "leader_config": "left_rig.json",
        "follower_config": "left_rig.json",
        "cameras": [],
    }
    (robots / "left_rig.json").write_text(json.dumps(record))
    return tmp_path


def test_resolve_pair_reads_the_saved_record(rig_root) -> None:
    from lelab import teleop_test

    pair, identity = teleop_test.resolve_pair("left")

    assert pair == {
        "leader_port": "/dev/serial/by-id/fake-leader",
        "follower_port": "/dev/serial/by-id/fake-follower",
        "leader_config": "left_rig.json",
        "follower_config": "left_rig.json",
    }
    assert identity["record"] == "left_rig"
    assert identity["record_complete"] is True


def test_resolve_pair_refuses_an_incomplete_record(rig_root) -> None:
    from lelab import teleop_test

    follower = rig_root / "calibration" / "robots" / "so_follower" / "left_rig.json"
    follower.unlink()

    with pytest.raises(ValueError, match="还不能用于单臂遥操作测试"):
        teleop_test.resolve_pair("left")


def test_stored_pass_goes_stale_when_the_identity_changes(rig_root) -> None:
    from lelab import teleop_test

    first = teleop_test.build_identity("left")
    teleop_test.store_result("left", first, passed=True, note="follows smoothly")

    assert teleop_test.evaluate(first)["state"] == "passed"

    (rig_root / "calibration" / "robots" / "so_follower" / "left_rig.json").write_text(
        json.dumps({"shoulder_pan": {"id": 1}, "gripper": {"id": 6}})
    )
    second = teleop_test.build_identity("left")

    outcome = teleop_test.evaluate(second)
    assert outcome["state"] == "stale"
    assert outcome["entry"]["passed"] is True
    assert second["fingerprint"] != first["fingerprint"]


def test_telemetry_counts_clamps_and_delta() -> None:
    from lelab import teleop_test

    telemetry = teleop_test.new_telemetry(
        {
            "side": "left",
            "record": "left_rig",
            "fingerprint": "x",
            "safety": {"max_relative_target": 5.0},
        }
    )
    raw = {"shoulder_pan.pos": 10.0, "gripper.pos": 40.0}
    sent = {"shoulder_pan.pos": 7.0, "gripper.pos": 40.0}
    measured = {"shoulder_pan.pos": 6.8, "gripper.pos": 33.0}

    teleop_test.update_step(telemetry, raw, sent, measured)
    teleop_test.update_step(telemetry, raw, raw, measured)

    snapshot = teleop_test.telemetry_snapshot(telemetry)
    assert snapshot["steps"] == 2
    assert snapshot["clamped_steps"] == 1
    pan = snapshot["joints"]["shoulder_pan.pos"]
    assert pan["clamp_count"] == 1
    assert pan["max_clamp"] == pytest.approx(3.0)
    # delta compares the last sent target with the last measured position, so
    # the un-clamped second step (10 -> 6.8) is the largest tracking gap.
    assert pan["delta"] == pytest.approx(3.2)
    assert pan["max_delta"] == pytest.approx(3.2)
    assert snapshot["joints"]["gripper.pos"]["clamp_count"] == 0


class _FakeBus:
    def connect(self) -> None:
        pass

    def write_calibration(self, calibration) -> None:
        pass


class _FakeArm:
    """Leader yields raw targets; the follower answers with a 3-degree clamp."""

    MOTORS = (
        "shoulder_pan",
        "shoulder_lift",
        "elbow_flex",
        "wrist_flex",
        "wrist_roll",
        "gripper",
    )

    def __init__(self, config, *, clamps: bool) -> None:
        self.bus = _FakeBus()
        self.calibration: dict = {}
        self.cameras: dict = {}
        self.clamps = clamps
        self.disconnected = False

    def configure(self) -> None:
        pass

    def get_action(self) -> dict[str, float]:
        return {f"{motor}.pos": 20.0 for motor in self.MOTORS}

    def send_action(self, action: dict[str, float]) -> dict[str, float]:
        if not self.clamps:
            return dict(action)
        return {key: value - 3.0 for key, value in action.items()}

    def get_observation(self) -> dict[str, float]:
        return {f"{motor}.pos": 17.5 for motor in self.MOTORS}

    def disconnect(self, *args, **kwargs) -> None:
        self.disconnected = True


def test_side_start_resolves_record_and_records_telemetry(rig_root, monkeypatch) -> None:
    import time

    from lelab import teleoperate as teleop

    monkeypatch.setattr(teleop, "teleoperation_active", False)
    monkeypatch.setattr(teleop, "teleoperation_thread", None)
    monkeypatch.setattr(teleop, "_test_telemetry", None)
    monkeypatch.setattr(
        teleop, "setup_calibration_files", lambda leader, follower: (leader, follower)
    )
    monkeypatch.setattr(
        teleop,
        "SO101Follower",
        lambda config: _FakeArm(config, clamps=True),
    )
    monkeypatch.setattr(teleop, "SO101Leader", lambda config: _FakeArm(config, clamps=False))

    result = teleop.handle_start_teleoperation(teleop.TeleoperateRequest(side="left"))
    assert result["success"] is True, result

    deadline = time.monotonic() + 2.0
    while teleop.handle_teleoperation_status()["test"]["steps"] < 2:
        if time.monotonic() > deadline:
            pytest.fail("follow loop produced no telemetry")

    status = teleop.handle_teleoperation_status()
    assert status["teleoperation_active"] is True
    snapshot = status["test"]
    assert snapshot["side"] == "left"
    assert snapshot["safety"]
    assert snapshot["joints"]["shoulder_pan.pos"]["clamp_count"] >= 1

    teleop.handle_stop_teleoperation()

    after = teleop.handle_teleoperation_status()
    assert after["teleoperation_active"] is False
    # The stopped session keeps its telemetry so the operator can still confirm.
    assert after["test"]["steps"] == snapshot["steps"]

    confirmed = teleop.handle_confirm_teleop_test("left", passed=True, note="no jitter")
    assert confirmed["success"] is True
    assert confirmed["test"]["state"] == "passed"
    assert confirmed["result"]["summary"]["steps"] == snapshot["steps"]
    recorded = json.loads(
        (rig_root / "teleop-tests" / "v1.json").read_text(encoding="utf-8")
    )
    assert recorded["results"]["left"]["note"] == "no jitter"


def test_side_start_blocks_until_the_record_is_complete(rig_root, monkeypatch) -> None:
    from lelab import teleoperate as teleop

    (rig_root / "calibration" / "teleoperators" / "so_leader" / "left_rig.json").unlink()
    monkeypatch.setattr(teleop, "teleoperation_active", False)

    result = teleop.handle_start_teleoperation(teleop.TeleoperateRequest(side="left"))

    assert result["success"] is False
    assert "单臂遥操作测试" in result["message"]
    assert teleop.teleoperation_active is False


def test_side_request_rejects_explicit_ports() -> None:
    from lelab.teleoperate import TeleoperateRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TeleoperateRequest(side="left", leader_port="/dev/a")

    TeleoperateRequest(side="right").model_dump()


def test_readiness_reports_both_sides(rig_root) -> None:
    from lelab import teleop_test

    payload = teleop_test.readiness()

    assert payload["sides"]["left"]["available"] is True
    assert payload["sides"]["left"]["test"]["state"] == "untested"
    assert payload["sides"]["right"]["available"] is False
    assert Path(payload["result_path"]).parent.name == "teleop-tests"
