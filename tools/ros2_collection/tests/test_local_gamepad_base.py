import json
import sys
from pathlib import Path

import pytest

CAR = Path(__file__).resolve().parents[3] / "ros2_ws/src/lelab_mobile_agent"
sys.path.insert(0, str(CAR))
from lelab_mobile_agent.local_gamepad_base import (  # noqa: E402
    ZERO, LocalGamepadBase, hardware_trial_settings, make_offline_profile, offline_settings,
)


def fixture_profile():
    profile = json.loads((CAR / "config/hardware-hybrid-manual-v1-offline6d-dds.json").read_text())
    return make_offline_profile(profile)


def test_hardware_profiles_cannot_construct_simulated_base():
    profile = fixture_profile()
    profile["runtime"]["simulated"] = False
    with pytest.raises(ValueError, match="raw-only offline profile"):
        offline_settings(profile)


def test_deadman_timeout_and_epoch_change_require_a_new_neutral_edge():
    sent = []
    config = offline_settings(fixture_profile())
    config.pop("neutral_dwell_ms")
    config["deadman_button"] = 0
    for index, name in enumerate(("vx", "vy", "wz")):
        config["axes"][name].update(index=index, direction=1)
    base = LocalGamepadBase(config, sent.append)
    scope = ("recording", 1)
    ms = 1_000_000

    def sample(at, axes, button):
        base.ingest(axes, [button], now_ns=at * ms, source_ns=at * ms, ros_now_ns=at * ms)

    base.tick(1 * ms, scope, "drive")
    sample(5, [0, 0, 0], 1)
    base.tick(9 * ms, scope, "drive")
    assert not sent
    sample(10, [0, 0, 0], 0)
    sample(20, [0, 0, 0], 1)
    base.tick(20 * ms, scope, "drive")
    sample(100, [1, 0, 0], 1)
    base.tick(100 * ms, scope, "drive")
    assert sent[0] == ZERO
    assert sent[-1] == pytest.approx((0.024, 0, 0))
    base.tick(400 * ms, scope, "drive")
    assert sent[-1] == ZERO
    sample(410, [1, 0, 0], 1)
    base.tick(420 * ms, scope, "drive")
    assert sent[-1] == ZERO
    assert not base.armed

    sample(430, [0, 0, 0], 0)
    sample(440, [0, 0, 0], 1)
    base.tick(441 * ms, scope, "drive")
    sample(500, [0, 1, 0], 1)
    base.tick(501 * ms, scope, "drive")
    assert sent[-1][1] > 0
    base.tick(502 * ms, ("recording", 2), "drive")
    assert sent[-1] == ZERO
    assert not base.armed


def test_stick_only_requires_new_authorization_and_neutral_after_input_loss():
    sent = []
    base = LocalGamepadBase(offline_settings(fixture_profile()), sent.append)
    scope = ("recording", 1)
    ms = 1_000_000
    neutral, jog = [0, 0, 0], [1, -1, 1]

    def sample(at, axes):
        base.ingest(axes, [], now_ns=at * ms, source_ns=at * ms, ros_now_ns=at * ms)
        base.tick(at * ms, scope, "drive")

    base.tick(ms, scope, "drive")
    sample(10, jog)
    sample(60, jog)
    assert not sent
    sample(70, neutral)
    sample(260, neutral)
    assert not base.armed
    sample(320, neutral)
    assert base.armed
    sample(350, jog)
    sample(450, jog)
    assert sent[-1] == pytest.approx((0.03, -0.03, -0.05))
    sample(460, neutral)
    assert sent[-1] == ZERO
    sample(500, jog)
    sample(600, jog)
    base.tick(901 * ms, scope, "drive")          # 300 ms input gap elapses
    assert sent[-1] == ZERO
    sample(910, jog)
    sample(950, jog)
    assert sent[-1] == ZERO and not base.armed
    sample(960, neutral)
    sample(1210, neutral)
    sample(1220, jog)
    sample(1320, jog)
    assert sent[-1] == ZERO and not base.armed
    assert base.status(1320 * ms)["reauthorization_required"]
    scope = ("recording", 2)
    base.tick(1691 * ms, scope, "drive")
    sample(1700, neutral)
    sample(1950, neutral)
    sample(1960, jog)
    sample(2060, jog)
    assert sent[-1] != ZERO
    base.tick(2061 * ms, ("recording", 3), "drive")
    assert sent[-1] == ZERO and not base.armed


def test_stale_stamp_or_malformed_unused_axis_cannot_refresh_input():
    base = LocalGamepadBase(offline_settings(fixture_profile()), lambda value: None)
    base.ingest([0, 0, 0], [0], now_ns=1_000_000_000, source_ns=1, ros_now_ns=1_000_000_000)
    assert base.sample is None
    base.ingest([0, 0, 0, float("nan")], [1], now_ns=1, source_ns=1, ros_now_ns=1)
    assert base.sample is None


def test_command_odometry_integrates_lateral_motion_and_stops_with_zero_target():
    base = LocalGamepadBase(offline_settings(fixture_profile()), lambda value: None)
    base.set_odom_episode(("recording", "first", "clock"))
    base._dispatch((0.1, -0.1, 0.0), 1_000_000_000)
    base.stop("stick_neutral", 1_200_000_000)
    status = base.status(2_000_000_000)
    estimate = status["command_odometry"]
    assert estimate["source_kind"] == "command_integrated_estimate"
    assert estimate["pose_xy_yaw"] == pytest.approx((0.02, -0.02, 0.0))
    assert status["state_si"] is None

    base.set_odom_episode(("recording", "second", "clock"))
    assert base.status(2_000_000_000)["command_odometry"]["pose_xy_yaw"] == ZERO


def trial_profile():
    return json.loads((CAR / "config/hardware-hybrid-base-trial-v1.json").read_text())


def test_hardware_trial_profile_is_gated_and_not_offline():
    profile = trial_profile()
    assert hardware_trial_settings(profile)["limits"]["vx"] <= 0.2
    with pytest.raises(ValueError, match="raw-only offline profile"):
        offline_settings(profile)
    profile["teleop"]["chassis_locked"] = False
    with pytest.raises(ValueError, match="chassis-trial profile"):
        hardware_trial_settings(profile)


def test_hardware_trial_base_keeps_outputs_unverified():
    base = LocalGamepadBase(hardware_trial_settings(trial_profile()),
                            lambda target: None, mode="hardware_trial")
    status = base.status(1_000_000_000)
    assert status["data_identity"] == "hardware"
    assert status["output_mode"] == "hardware_set_motor"
    assert status["physical_stop"] == "unverified"
    assert status["training_eligible"] is False


def test_hardware_trial_requires_neutral_before_any_dispatch():
    sent = []
    base = LocalGamepadBase(hardware_trial_settings(trial_profile()),
                            sent.append, mode="hardware_trial")
    scope = ("session", 1, "bundle", 1, "base_trial")
    ms = 1_000_000

    def sample(at, vx):
        base.ingest([0.0, vx, 0.0], [], now_ns=at * ms, source_ns=at * ms, ros_now_ns=at * ms)

    base.tick(1 * ms, scope, "base_trial_waiting_unlock")
    sample(5, 1.0)           # stick already deflected: no neutral edge yet
    base.tick(6 * ms, scope, "base_trial_waiting_unlock")
    assert not sent
    sample(10, 0.0)          # centred on the captured neutral
    base.tick(10 * ms, scope, "base_trial_waiting_unlock")
    sample(300, 0.0)
    base.tick(300 * ms, scope, "base_trial_waiting_unlock")
    assert base.armed and sent[-1] == ZERO
    # Idle and armed still repeats a zero target once a second, so a dropped frame or a
    # skipped tick cannot leave an older non-zero target standing on the board.
    for at in (500, 700, 900, 1100, 1300):
        sample(at, 0.0)
        base.tick(at * ms, scope, "base_trial_waiting_unlock")
    assert base.armed and len(sent) >= 2 and sent[-1] == ZERO
    sample(1400, -1.0)       # axis 1 negative is +vx on this transmitter
    base.tick(1400 * ms, scope, "base_trial_waiting_unlock")
    sample(1420, -1.0)
    base.tick(1420 * ms, scope, "base_trial_waiting_unlock")
    assert sent[-1][0] > 0
