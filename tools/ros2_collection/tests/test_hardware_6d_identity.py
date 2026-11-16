"""The hardware 6D export identity must fail closed and never relabel simulation."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "app_ros_mobile")]

from lelab import hybrid_export_schema as hs  # noqa: E402


def hardware_meta(**overrides):
    meta = {
        "simulated": False,
        "data_identity": "hardware",
        "profile": "hardware_hybrid_follow_v1_dds",
        "runtime_mode": "hardware_hybrid_manual",
        "export_schema": hs.SCHEMA_ARM_GRIPPER_6DOF,
        "training_export": True,
        "clock_domain": "car_system_time",
        "clock_epoch": "epoch-1",
        "calibration_bundle_hash": "b" * 64,
        "feedback_provenance": {"hardware_measurement": True, "max_state_age_s": 0.5},
        "feedback_limits": {
            "version": "limits-v1",
            "sha256": "c" * 64,
            "binding_status": "device_bound",
            "device_identity": {
                "kind": "landerpi_bus_servo_follower",
                "host_serial": "host-1",
            },
            "ranges": {
                name: [-50 if name == "joint3" else 0, 1000]
                for name in ("joint1", "joint2", "joint3",
                             "joint4", "joint5", "r_joint")
            },
        },
    }
    meta.update(overrides)
    return meta


def test_a_qualifying_hardware_session_gets_a_training_eligible_identity():
    identity = hs.hardware_6d_identity(hardware_meta())
    assert identity["data_identity"] == "hardware"
    assert identity["training_eligible"] is True
    assert identity["contract"] == hs.HARDWARE_6D_CONTRACT
    assert identity["max_state_age_s"] == 0.5
    assert identity["clock_epoch"] == "epoch-1"
    assert identity["sampling"] == "event_paced_teleop_rows"


def test_requested_calibration_alias_is_accepted_only_when_provenance_matches():
    meta = hardware_meta(
        calibration_bundle_hash=None,
        requested_calibration_bundle_hash="b" * 64,
        feedback_provenance={
            "hardware_measurement": True,
            "max_state_age_s": 0.5,
            "calibration_hash": "b" * 64,
        },
    )
    assert hs.hardware_6d_identity(meta)["calibration_bundle_hash"] == "b" * 64

    meta["requested_calibration_bundle_hash"] = "d" * 64
    with pytest.raises(hs.SampleRejected) as caught:
        hs.hardware_6d_identity(meta)
    assert caught.value.reason == "hardware_6d_calibration_mismatch"


@pytest.mark.parametrize("overrides,reason", [
    ({"simulated": True}, "hardware_6d_rejects_simulation"),
    ({"data_identity": "simulated_offline"}, "hardware_6d_identity_missing"),
    ({"profile": "hardware_hybrid_manual_v1_offline6d"}, "hardware_6d_profile"),
    ({"runtime_mode": "simulated_hybrid_manual"}, "hardware_6d_runtime_mode"),
    ({"export_schema": "mobile_hybrid_manual_9d_v1"}, "hardware_6d_schema"),
    ({"training_export": False}, "hardware_6d_training_switch"),
    ({"feedback_provenance": {"hardware_measurement": False, "max_state_age_s": 0.5}},
     "hardware_6d_not_measured"),
    ({"feedback_provenance": {"hardware_measurement": True, "max_state_age_s": 1.0}},
     "hardware_6d_state_age_unbounded"),
    ({"clock_epoch": None}, "hardware_6d_clock_identity"),
    ({"calibration_bundle_hash": None}, "hardware_6d_calibration_missing"),
    ({"feedback_limits": None}, "hardware_6d_feedback_limits_missing"),
    ({"feedback_limits": {"version": "v", "sha256": "c" * 64,
                          "binding_status": "legacy_profile_only_upgrade_on_save",
                          "device_identity": {}}},
     "hardware_6d_feedback_limits_binding"),
])
def test_the_hardware_identity_rejects_unqualified_sessions(overrides, reason):
    with pytest.raises(hs.SampleRejected) as caught:
        hs.hardware_6d_identity(hardware_meta(**overrides))
    assert caught.value.reason == reason


def test_the_simulated_contract_still_rejects_hardware_measurement():
    meta = {
        "simulated": True, "data_identity": hs.SIMULATED_DATA_SOURCE,
        "profile": "hardware_hybrid_manual_v1_offline6d",
        "runtime_mode": "simulated_hybrid_manual",
        "export_schema": hs.SCHEMA_ARM_GRIPPER_6DOF,
        "training_export": False,
        "clock_domain": "car_system_time", "clock_epoch": "e",
        "feedback_provenance": {"hardware_measurement": True, "max_state_age_s": 0.5},
    }
    with pytest.raises(hs.SampleRejected) as caught:
        hs.simulated_6d_identity(meta)
    assert caught.value.reason == "simulated_6d_claims_hardware_measurement"
