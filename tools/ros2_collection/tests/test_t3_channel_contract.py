"""T3 Wave A car-side contract tests. SIMULATED ONLY, no ROS graph or hardware.

These exercise the ROS-free hybrid gateway / protocol boundary on Python 3.12;
they are NOT evidence of Humble/Python 3.10 runtime, firmware behaviour, physical
stopping or actuator acknowledgement.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "ros2_ws/src/lelab_mobile_agent"), str(ROOT / "app_ros_mobile")]

from lelab_mobile_agent.contract import HybridCommandGate, hybrid_feedback_gate  # noqa: E402
from lelab_mobile_agent.hybrid_gateway import HybridGateway, HybridGatewayError  # noqa: E402

JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]
TARGETS = {
    "base": {"vx_m_s": 0.1, "vy_m_s": 0.0, "wz_rad_s": 0.0},
    "arm": {"joint_names": JOINTS[:5], "positions_rad": [0.0] * 5},
    "gripper": {"joint_name": "r_joint", "position_rad": 0.0},
}


class Clock:
    def __init__(self, now: int = 1_000_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class T1DriverFixture:
    """Explicit T1 interface fixture; submit never actuates."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.fail_submit = False
        self.fail_stop = False

    def capabilities(self):
        return {"simulated": True, "outputs_enabled": False,
                "arm_joint_names": JOINTS[:5], "gripper_joint_name": "r_joint",
                "gripper_servo_id": 10, "camera_capability": "unknown",
                "firmware_capability": "unknown", "hardware_stop_effect": "unverified"}

    def read_snapshot(self):
        return {"simulated": True, "samples": []}

    def submit(self, command):
        self.log.append("submit")
        if self.fail_submit:
            raise RuntimeError("simulated driver failure")
        return {"simulated": True, "controller_acknowledged": "unavailable"}

    def stop(self, reason):
        self.log.append("stop")
        if self.fail_stop:
            raise RuntimeError("simulated stop failure")
        return {"simulated": True, "hardware_effect": "unverified"}


def command(channel: str, seq: int = 1, *, epoch: int = 7) -> dict:
    out = {"protocol_version": 1, "session_id": "s", "session_epoch": epoch,
           "channel": channel, "source": "gamepad" if channel == "base" else "so101_leader",
           "seq": seq, "sample_seq": seq, "ttl_ms": 100,
           "calibration_bundle_hash": "h", "target": copy.deepcopy(TARGETS[channel])}
    if channel != "base":
        out["clutch"] = True
    return out


def gateway(clock: Clock | None = None, driver: T1DriverFixture | None = None) -> HybridGateway:
    return HybridGateway({"profile": "hardware_hybrid_manual_v1"},
                         session_id="s", session_epoch=7, bundle_hash="h",
                         max_ttl_ms=200, clock_ns=clock or Clock(), driver=driver)


def feedback(now_ns: int = 1_000_000_000, *, source: str = "servo_position_readback") -> list[dict]:
    return [{"name": name, "servo_id": servo_id, "source_kind": source, "valid": True,
             "raw_position": 500, "position_rad": 0.0, "calibration_hash": "h",
             "sample_seq": 1, "received_mono_ns": now_ns, "clock_domain": "monotonic"}
            for name, servo_id in zip(JOINTS, [1, 2, 3, 4, 5, 10])]


def test_default_gateway_has_no_output_and_reports_unknown_capabilities():
    g = gateway()
    assert g.outputs_enabled is False
    caps = g.capabilities()
    assert caps["available"] is False and caps["actuator_outputs"] is False
    assert caps["camera_capability"] == "unknown" and caps["firmware_capability"] == "unknown"
    assert caps["hardware_stop_effect"] == "unverified"
    assert caps["controller_acknowledged"] == "unavailable"
    assert g.status()["outputs_enabled"] is False
    # Importing the gateway must not drag ROS into a plain-Python process.
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys, lelab_mobile_agent.hybrid_gateway as m; "
         "print('rclpy' in sys.modules, m.HybridGateway({}).outputs_enabled)"],
        capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "ros2_ws/src/lelab_mobile_agent")})
    assert probe.stdout.strip() == "False False"
    json.dumps(g.status(), allow_nan=False)


def test_channel_permit_is_required_and_independent():
    g = gateway()
    with pytest.raises(HybridGatewayError, match="channel_not_permitted"):
        g.submit(command("base"))
    g.grant("base")
    assert g.submit(command("base"))["accepted"] is True
    with pytest.raises(HybridGatewayError, match="channel_not_permitted"):
        g.submit(command("arm"))
    g.grant("arm", "gripper")
    assert set(g.permitted) == {"base", "arm", "gripper"}
    g.revoke("base")
    assert "base" not in g.permitted


@pytest.mark.parametrize("field,value", [
    ("protocol_version", 2), ("session_epoch", 6), ("session_id", "old"),
    ("source", "maintenance"), ("channel", "unknown"),
    ("calibration_bundle_hash", "old"), ("ttl_ms", 0), ("ttl_ms", 500),
    ("seq", True), ("sample_seq", -1), ("received_mono_ns", 5),
    ("intent", {"deadman": True}), ("deadline_mono_ns", 9),
])
def test_bad_or_old_frames_are_rejected_without_consuming_seq(field, value):
    g = gateway()
    g.grant("base", "arm", "gripper")
    bad = command("base")
    bad[field] = value
    with pytest.raises(HybridGatewayError):
        g.submit(bad)
    # A valid frame with the original seq must still be accepted.
    assert g.submit(command("base"))["seq"] == 1


@pytest.mark.parametrize("channel,broken", [
    ("base", {"vx_m_s": float("nan"), "vy_m_s": 0.0, "wz_rad_s": 0.0}),
    ("base", {"vx_m_s": 0.1, "vy_m_s": 0.0, "wz_rad_s": 0.0, "extra": 1}),
    ("arm", {"joint_names": ["joint1"] * 5, "positions_rad": [0.0] * 4}),
    ("gripper", {"joint_name": "gripper_base_joint", "position_rad": 0.0}),
])
def test_missing_or_wrong_target_never_enters_the_queue(channel, broken):
    g = gateway()
    g.grant(channel)
    bad = command(channel)
    bad["target"] = broken
    with pytest.raises(HybridGatewayError):
        g.submit(bad)
    assert g.status()["pending"] == {}


def test_seq_gap_is_latest_wins_and_replay_retires_gate():
    clock = Clock()
    g = gateway(clock)
    g.grant("base")
    g.submit(command("base", 1))
    assert g.submit(command("base", 3))["seq"] == 3
    with pytest.raises(HybridGatewayError, match="stale_seq"):
        g.submit(command("base", 3))
    assert g.status()["retired"] is True
    # A replay discards the queued intent before any epoch rollover.
    assert g.status()["pending"] == {}
    rolled = g.begin_epoch(8)
    assert rolled["queue_dropped"] == 0
    assert g.status()["retired"] is False
    assert g.status()["pending"] == {}
    assert g.permitted == set()
    with pytest.raises(HybridGatewayError, match="session_mismatch"):
        g.submit(command("base", 1, epoch=7))


def test_expired_command_never_dispatches_and_forward_only_carries_remaining_ttl():
    clock = Clock()
    g = gateway(clock)
    g.grant("arm")
    g.submit(command("arm"))
    clock.now += 40_000_000
    popped = g.dispatch("arm")
    assert popped["driver_sent"] is False
    assert popped["outputs_enabled"] is False
    forwarded = g.forward(popped["command"])
    assert forwarded["ttl_ms"] == 60
    assert "deadline_mono_ns" not in forwarded
    assert "received_mono_ns" not in forwarded
    clock.now += 60_000_000
    with pytest.raises(HybridGatewayError, match="expired"):
        g.forward(popped["command"])


def test_stop_clears_intents_before_driver_and_rollback_reports_failure():
    clock = Clock()
    driver = T1DriverFixture()
    g = gateway(clock, driver)
    g.grant("base")
    g.submit(command("base"))
    stopped = g.stop("operator")
    assert stopped["queue_dropped"] == 1
    assert stopped["outputs_enabled"] is False
    assert driver.log == ["stop"]
    assert g.permitted == set()
    assert g.status()["pending"] == {}

    driver.fail_stop = True
    failed = g.rollback("simulated_stop_failure")
    assert failed["driver_stopped"] is False
    assert g.last_error == "simulated_stop_failure"
    assert g.status()["retired"] is False  # fresh gate after rollback


def test_driver_submit_failure_and_cancellation_roll_back():
    clock = Clock()
    driver = T1DriverFixture()
    g = gateway(clock, driver)
    g.grant("arm")
    g.submit(command("arm"))
    # Outputs are enabled only inside this contract test; nothing physical is driven.
    g.outputs_enabled = True
    driver.fail_submit = True
    result = g.dispatch("arm")
    assert result["driver_sent"] is False and result["error"] == "RuntimeError"
    assert g.outputs_enabled is False and g.permitted == set()

    class CancelDriver(T1DriverFixture):
        def submit(self, command):
            raise KeyboardInterrupt()

    g2 = gateway(clock, CancelDriver())
    g2.grant("arm")
    g2.submit(command("arm"))
    g2.outputs_enabled = True
    with pytest.raises(KeyboardInterrupt):
        g2.dispatch("arm")
    assert g2.outputs_enabled is False and g2.permitted == set()


def test_trusted_feedback_only_and_never_repairs_bad_samples():
    g = gateway()
    good = feedback()
    assert g.read_feedback(good, now_ns=1_000_000_000)["trusted"] is True
    for source in ("command_cache", "model_placeholder", "synthetic", "vendor_joint_states"):
        assert g.read_feedback({"source_kind": source, "samples": good},
                               now_ns=1_000_000_000)["trusted"] is False
    for change in (lambda s: s.pop(),
                   lambda s: s[0].update(position_rad=float("inf")),
                   lambda s: s[0].update(raw_position=1001),
                   lambda s: s[0].update(received_mono_ns=0),
                   lambda s: s[0].update(servo_id=99),
                   lambda s: s[0].update(calibration_hash="other")):
        altered = copy.deepcopy(good)
        change(altered)
        assert not g.read_feedback(altered, now_ns=1_000_000_000)["trusted"]
    assert altered != good  # the gate must not silently clamp or repair inputs


def test_shared_contract_gate_rejects_local_clock_fields_and_wrong_source():
    gate = HybridCommandGate("s", 7, "h", 200, Clock())
    c = command("arm")
    c["received_mono_ns"] = 1
    with pytest.raises(Exception) as exc:
        gate.receive(c, {"arm"})
    assert "local_clock_fields_forbidden" in str(exc.value)
    c = command("arm")
    c["source"] = "maintenance"
    with pytest.raises(Exception):
        gate.receive(c, {"arm"})
    c = command("arm")
    c["intent"] = {"deadman": True}
    with pytest.raises(Exception, match="unexpected_envelope_field"):
        gate.receive(c, {"arm"})


def test_hybrid_profile_is_no_output_and_both_copies_are_identical():
    source = ROOT / "configs/ros2_collection/hardware-hybrid-manual-v1.json"
    packaged = ROOT / "ros2_ws/src/lelab_mobile_agent/config/hardware-hybrid-manual-v1.json"
    assert source.read_bytes() == packaged.read_bytes()
    profile = json.loads(source.read_text())
    assert profile["profile"] == "hardware_hybrid_manual_v1"
    assert profile["runtime"]["actuator_outputs"] is False
    assert profile["teleop"]["chassis_locked"] is True
    assert profile["safety"]["grasp_base_locked"] is True
    assert profile["safety"]["command_cache_is_measured"] is False
    assert profile["runtime"]["firmware_capability"] == "unknown"
    assert profile["training_export"]["enabled"] is False
    # Static guard: the legacy actuator endpoints must be refused for this profile.
    agent_source = (ROOT / "ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/agent.py").read_text()
    assert "hybrid_no_output" in agent_source
    assert "/control/arm" in agent_source


def test_six_servo_feedback_gate_from_shared_contract():
    samples = feedback(now_ns=1000)
    assert hybrid_feedback_gate(samples, "h", 1000, 100)["valid"] is True
    cache = copy.deepcopy(samples)
    cache[0]["source_kind"] = "command_cache"
    assert not hybrid_feedback_gate(cache, "h", 1000, 100)["valid"]
