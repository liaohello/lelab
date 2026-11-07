"""T4: the car contract and the upper-host schema must not drift.

The car (Python 3.10 / Humble) and the upper host (Python 3.12) each carry their own
copy of the recording vocabulary on purpose -- the car must not import from the web
app, and the host must not import rclpy.  This test is the seam that keeps the two
copies identical, and it also pins the *rejection* behaviour: a stale epoch, an
expired command, a replayed sequence, a command-echo "state" and a future
timestamp must fail on both sides with the same machine token.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ros2_ws/src/lelab_mobile_agent"))

from lelab_mobile_agent import recording_contract as car  # noqa: E402

from lelab import hybrid_export_schema as host  # noqa: E402

GOLDEN = {
    "protocol_version": 1,
    "phases": ("drive", "observe", "grasp", "transport", "release"),
    "manual_results": ("success", "failure", "unknown"),
    "schema_names": ("arm_gripper_6dof_phase1", "mobile_hybrid_manual_9d_v1"),
    "state_joints": ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"),
    "confirmation_levels": ("requested", "gateway_accepted", "driver_sent",
                            "controller_acknowledged", "execution_observed"),
    "trusted_joint_source": "servo_position_readback",
    "trusted_base_sources": ("wheel_odometry_estimate", "imu_fused_estimate"),
    "forbidden_state_sources": ("command_cache", "command_integrated_estimate",
                                "model_placeholder", "synthetic"),
    "action_9d_fields": ("base_vx_cmd", "base_vy_cmd", "base_wz_cmd", "joint1_target",
                         "joint2_target", "joint3_target", "joint4_target", "joint5_target",
                         "r_joint_target"),
    "state_9d_fields": ("base_vx_est", "base_vy_est", "base_wz_est", "joint1_measured",
                        "joint2_measured", "joint3_measured", "joint4_measured",
                        "joint5_measured", "r_joint_measured"),
}


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_the_two_copies_agree_on_every_pinned_value(key):
    expected = GOLDEN[key]
    assert car.CONTRACT[key] == host.CONTRACT[key]
    if isinstance(expected, tuple):
        assert tuple(car.CONTRACT[key]) == expected
        assert tuple(host.CONTRACT[key]) == expected
    else:
        assert car.CONTRACT[key] == expected


def test_the_whole_contract_dicts_are_identical():
    assert car.CONTRACT == host.CONTRACT


def test_column_order_is_what_the_exporter_uses():
    """Action is command-first, state is estimate-first: swapping them is silent corruption."""
    assert host.ACTION_9D_FIELDS[:3] == ("base_vx_cmd", "base_vy_cmd", "base_wz_cmd")
    assert host.STATE_9D_FIELDS[:3] == ("base_vx_est", "base_vy_est", "base_wz_est")
    assert host.ACTION_9D_FIELDS[3:] == tuple(f"{j}_target" for j in host.STATE_JOINTS)
    assert host.STATE_9D_FIELDS[3:] == tuple(f"{j}_measured" for j in host.STATE_JOINTS)
    assert len(set(host.ACTION_9D_FIELDS)) == 9 and len(set(host.STATE_9D_FIELDS)) == 9


# ---------------------------------------------------------------------------
# Same input, same refusal on both sides
# ---------------------------------------------------------------------------
def _joint(name="joint1", **overrides):
    sample = {
        "name": name, "servo_id": 1, "raw_position": 500, "position_rad": 0.0,
        "source_kind": "servo_position_readback", "sample_seq": 1, "sample_time_ns": 1_000,
        "age_s": 0.01, "valid": True, "calibration_hash": "sha256:abc",
    }
    sample.update(overrides)
    return sample


def test_a_command_echo_is_not_a_measured_joint_on_either_side():
    with pytest.raises(car.ContractError) as car_error:
        car.validate_joint_sample(_joint(source_kind="command_cache"),
                                  expected_name="joint1", calibration_hash="sha256:abc",
                                  max_age_s=0.2)
    with pytest.raises(host.SampleRejected) as host_error:
        host.check_joint_state([_joint(source_kind="command_cache")] + [
            _joint(name, sample_time_ns=1_000) for name in host.STATE_JOINTS[1:]],
            frame_time_ns=1_050, calibration_hash="sha256:abc", max_age_s=0.2)
    assert car_error.value.reason == "untrusted_joint_source"
    assert host_error.value.reason == "forbidden_state_source"


def test_an_out_of_range_raw_position_is_preserved_and_rejected_not_clamped():
    with pytest.raises(car.ContractError, match="0..1000"):
        car.validate_joint_sample(_joint(raw_position=1200), expected_name="joint1",
                                  calibration_hash="sha256:abc", max_age_s=0.2)
    with pytest.raises(host.SampleRejected, match="0..1000"):
        host.check_joint_state([_joint(raw_position=1200)] + [
            _joint(name) for name in host.STATE_JOINTS[1:]],
            frame_time_ns=1_050, calibration_hash="sha256:abc", max_age_s=0.2)


def test_a_stale_joint_is_rejected_with_the_same_token():
    # The car reports ``age_s``; the host derives age from ``sample_time_ns``. Both
    # describe the same stale sample and both must refuse it.
    stale = _joint(sample_time_ns=0, age_s=0.5)
    with pytest.raises(car.ContractError, match="exceeds"):
        car.validate_joint_sample(stale, expected_name="joint1", calibration_hash="sha256:abc",
                                  max_age_s=0.2)
    with pytest.raises(host.SampleRejected, match="exceeds"):
        host.check_joint_state([stale] + [_joint(name) for name in host.STATE_JOINTS[1:]],
                               frame_time_ns=1_000_000_000, calibration_hash="sha256:abc",
                               max_age_s=0.2)


def test_a_future_sample_is_never_used_as_current_state():
    future = _joint(sample_time_ns=2_000, age_s=-0.5)
    with pytest.raises(car.ContractError):
        car.validate_joint_sample(future, expected_name="joint1", calibration_hash="sha256:abc",
                                  max_age_s=0.2)
    with pytest.raises(host.SampleRejected, match="newer than the frame"):
        host.check_joint_state([future] + [_joint(name) for name in host.STATE_JOINTS[1:]],
                               frame_time_ns=1_050, calibration_hash="sha256:abc", max_age_s=0.2)


def test_execution_cannot_be_claimed_from_a_uart_write():
    forged = {"driver_sent": True, "execution_observed": True}
    with pytest.raises(car.ContractError) as car_error:
        car.normalize_command_status(forged, channel="arm", source="so101_leader")
    with pytest.raises(host.SampleRejected) as host_error:
        host.command_status_view(forged)
    assert car_error.value.reason == "unproven_execution"
    assert host_error.value.reason == "unproven_execution"


def test_a_missing_ack_reports_unavailable_rather_than_success():
    status = car.normalize_command_status({"driver_sent": True}, channel="arm",
                                         source="so101_leader")
    assert status["highest_confirmed"] == "driver_sent"
    assert status["controller_acknowledged"] == "unavailable"
    assert status["execution_observed"] == "unavailable"
    assert "does not imply" in status["ack_note"]
    view = host.command_status_view({"driver_sent": True})
    assert view["highest_confirmed"] == "driver_sent"
    assert view["controller_acknowledged"] == "unavailable"


# ---------------------------------------------------------------------------
# Event-level session rules
# ---------------------------------------------------------------------------
def _event(**overrides):
    base = {
        "event_id": "e1", "kind": "phase_started", "protocol_version": 1,
        "session_id": "s1", "session_epoch": 7, "episode_id": "ep", "time_ns": 1_000,
        "clock_domain": "car", "clock_epoch": "ce1", "payload": {"phase": "observe"},
        "source": "manual",
    }
    base.update(overrides)
    return base


def test_an_old_session_epoch_is_refused():
    with pytest.raises(car.ContractError) as error:
        car.validate_event(_event(session_epoch=6), session_id="s1", session_epoch=7,
                           clock_domain="car", clock_epoch="ce1", now_time_ns=1_100,
                           max_age_ns=10**9)
    assert error.value.reason == "stale_session_epoch"


def test_another_sessions_event_is_refused():
    with pytest.raises(car.ContractError, match="another session"):
        car.validate_event(_event(session_id="other"), session_id="s1", session_epoch=7,
                           clock_domain="car", clock_epoch="ce1", now_time_ns=1_100,
                           max_age_ns=10**9)


def test_no_interpolation_across_a_clock_epoch():
    with pytest.raises(car.ContractError) as error:
        car.validate_event(_event(clock_epoch="ce2"), session_id="s1", session_epoch=7,
                           clock_domain="car", clock_epoch="ce1", now_time_ns=1_100,
                           max_age_ns=10**9)
    assert error.value.reason == "cross_clock_epoch"


def test_an_expired_event_is_refused():
    with pytest.raises(car.ContractError) as error:
        car.validate_event(_event(time_ns=1_000), session_id="s1", session_epoch=7,
                           clock_domain="car", clock_epoch="ce1", now_time_ns=10**10,
                           max_age_ns=10**9)
    assert error.value.reason == "expired"


def test_a_future_event_is_refused():
    with pytest.raises(car.ContractError) as error:
        car.validate_event(_event(time_ns=5_000), session_id="s1", session_epoch=7,
                           clock_domain="car", clock_epoch="ce1", now_time_ns=1_000,
                           max_age_ns=10**9)
    assert error.value.reason == "future_event"


def test_an_untrusted_event_source_is_refused():
    with pytest.raises(car.ContractError) as error:
        car.validate_event(_event(source="browser"), session_id="s1", session_epoch=7,
                           clock_domain="car", clock_epoch="ce1", now_time_ns=1_100,
                           max_age_ns=10**9)
    assert error.value.reason == "untrusted_source"


def test_phase_boundaries_must_increase_and_not_repeat():
    with pytest.raises(car.ContractError, match="strictly increase"):
        car.validate_phase("grasp", time_ns=100, episode_started_ns=50,
                           previous_started_ns=100, previous_phase="observe")
    with pytest.raises(car.ContractError, match="ambiguous"):
        car.validate_phase("observe", time_ns=200, episode_started_ns=50,
                           previous_started_ns=100, previous_phase="observe")
    with pytest.raises(car.ContractError, match="predates"):
        car.validate_phase("observe", time_ns=10, episode_started_ns=50,
                           previous_started_ns=None, previous_phase=None)


def test_manual_result_never_becomes_simulator_truth():
    assert car.normalize_manual_result(None) == "unknown"
    assert car.normalize_manual_result(True) == "success"
    assert car.normalize_manual_result("failed") == "failure"
    assert host.manual_result("") == "unknown"
    assert host.manual_result(False) == "failure"
    with pytest.raises(car.ContractError):
        car.normalize_manual_result("unverified")


# ---------------------------------------------------------------------------
# Row builder: the one function that can turn a bag frame into a 9D row
# ---------------------------------------------------------------------------
def _command(channel: str, seq: int, *, time_ns: int = 1_000, ttl_ms: int = 200,
             source: str | None = None, target=None):
    return {
        "seq": seq, "issued_time_ns": time_ns, "ttl_ms": ttl_ms,
        "source": source or ("gamepad" if channel == "base" else "so101_leader"),
        "target": target if target is not None else (
            [0.1, 0.0, 0.2] if channel == "base" else
            ([0.1, 0.2, 0.3, 0.4, 0.5] if channel == "arm" else [0.25])),
    }


def _joints(time_ns: int = 1_000):
    return [{"name": name, "position_rad": 0.01 * i, "raw_position": 500 + i,
             "source_kind": "servo_position_readback", "sample_time_ns": time_ns,
             "valid": True, "calibration_hash": "sha256:abc"}
            for i, name in enumerate(host.STATE_JOINTS)]


def _base(time_ns: int = 1_000, **overrides):
    sample = {"vx_m_s": 0.05, "vy_m_s": 0.0, "wz_rad_s": 0.02,
              "source_kind": "wheel_odometry_estimate", "sample_time_ns": time_ns,
              "covariance": [1e-4, 1e-4, 2e-4], "valid": True, "calibration_hash": "sha256:abc"}
    sample.update(overrides)
    return sample


def _row(**overrides):
    kwargs = {
        "frame_time_ns": 1_050,
        "commands": {"base": _command("base", 1), "arm": _command("arm", 1),
                     "gripper": _command("gripper", 1)},
        "base_estimate": _base(),
        "joints": _joints(),
        "calibration_hash": "sha256:abc",
        "max_state_age_s": 0.2,
    }
    kwargs.update(overrides)
    return host.build_9d_row(**kwargs)


def test_a_complete_sample_builds_a_nine_dimension_row():
    row, last_seq = _row()
    assert row["action"] == [0.1, 0.0, 0.2, 0.1, 0.2, 0.3, 0.4, 0.5, 0.25]
    assert row["state"] == [0.05, 0.0, 0.02, 0.0, 0.01, 0.02, 0.03, 0.04, 0.05]
    assert row["provenance"]["state_joint_source"] == "servo_position_readback"
    assert row["provenance"]["action_sources"] == {"base": "gamepad", "arm": "so101_leader",
                                                   "gripper": "so101_leader"}
    assert last_seq == {("base", "gamepad"): 1, ("arm", "so101_leader"): 1,
                        ("gripper", "so101_leader"): 1}


def test_a_missing_channel_rejects_instead_of_zero_filling():
    with pytest.raises(host.SampleRejected) as error:
        _row(commands={"base": None, "arm": _command("arm", 1),
                       "gripper": _command("gripper", 1)})
    assert error.value.reason == "missing_command"


def test_a_replay_rejects_but_a_latest_wins_gap_is_legal():
    with pytest.raises(host.SampleRejected) as error:
        _row(last_seq={("arm", "so101_leader"): 1})
    assert error.value.reason == "command_replayed"
    with pytest.raises(host.SampleRejected) as error:
        _row(last_seq={("arm", "so101_leader"): 5})
    assert error.value.reason == "command_out_of_order"
    assert _row(last_seq={("arm", "so101_leader"): 0})[0]["action"][3] == 0.1


def test_an_expired_command_rejects():
    with pytest.raises(host.SampleRejected) as error:
        _row(frame_time_ns=2_000_000,
             commands={"base": _command("base", 1, time_ns=0, ttl_ms=1),
                       "arm": _command("arm", 1), "gripper": _command("gripper", 1)})
    assert error.value.reason == "command_expired"


def test_a_command_from_the_future_rejects():
    with pytest.raises(host.SampleRejected) as error:
        _row(commands={"base": _command("base", 1, time_ns=2_000),
                       "arm": _command("arm", 1), "gripper": _command("gripper", 1)})
    assert error.value.reason == "command_from_future"


def test_a_command_integrated_estimate_is_not_a_state():
    with pytest.raises(host.SampleRejected) as error:
        _row(base_estimate=_base(source_kind="command_integrated_estimate"))
    assert error.value.reason == "forbidden_state_source"


def test_wrong_joint_order_and_missing_joints_reject():
    shuffled = _joints()
    shuffled[0], shuffled[1] = shuffled[1], shuffled[0]
    with pytest.raises(host.SampleRejected) as error:
        _row(joints=shuffled)
    assert error.value.reason == "wrong_joint_order"
    with pytest.raises(host.SampleRejected) as error:
        _row(joints=_joints()[:5])
    assert error.value.reason == "wrong_joint_count"


def test_a_wrong_action_shape_rejects():
    with pytest.raises(host.SampleRejected) as error:
        _row(commands={"base": _command("base", 1), "arm": _command("arm", 1, target=[0.1]),
                       "gripper": _command("gripper", 1)})
    assert error.value.reason == "wrong_action_shape"


def test_phase_timeline_rules():
    phases = [{"phase": "observe", "started_time_ns": 0, "ended_time_ns": 50},
              {"phase": "grasp", "started_time_ns": 50, "ended_time_ns": 100}]
    assert [p["phase"] for p in host.check_phase_timeline(
        phases, episode_start_ns=0, episode_end_ns=100)] == ["observe", "grasp"]
    with pytest.raises(host.SampleRejected) as error:
        host.check_phase_timeline(
            [{"phase": "observe", "started_time_ns": 0, "ended_time_ns": 60},
             {"phase": "grasp", "started_time_ns": 50, "ended_time_ns": 100}],
            episode_start_ns=0, episode_end_ns=100)
    assert error.value.reason == "phase_overlap"
    with pytest.raises(host.SampleRejected) as error:
        host.check_phase_timeline([{"phase": "observe", "started_time_ns": 0}],
                                  episode_start_ns=0, episode_end_ns=100)
    assert error.value.reason == "unterminated_phase"


def test_slices_are_half_open_and_carry_their_source():
    slices = host.phase_slices(
        [{"phase": "observe", "started_time_ns": 0, "ended_time_ns": 800_000_000}],
        aligned_start_ns=0, frame_count=15, fps=10)
    assert slices[0]["frame_from"] == 0 and slices[0]["frame_to_exclusive"] == 8
    assert "half-open" in slices[0]["source"]
