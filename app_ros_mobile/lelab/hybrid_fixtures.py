"""T3 Wave A offline fixtures. SIMULATED ONLY - never hardware evidence.

These objects exist so T4 (routes/recording) and T5 (page) can exercise the frozen
facade without a car, a serial port, a camera or a gamepad.  Every fixture is tagged
`simulated: True`; production wiring must inject real adapters through the coordinator
factory instead.  No fixture claims a measured state or an actuator acknowledgement.
"""
from __future__ import annotations

import time
from typing import Any

from .hybrid_calibration import JOINTS, KINDS, ROLES
from .hybrid_channel import (ARM_JOINTS, CHANNELS, CONFIRMATION_LEVELS, FEEDBACK_JOINTS,
                             FEEDBACK_SERVO_IDS, TRUSTED_SOURCE)
from .hybrid_facade import HybridPublicFacade
from .mobile_teleop import canonical_sha256

PROFILE = "hardware_hybrid_manual_v1"
MEASURED_AT = "2026-11-03T00:00:00Z"


class FixtureClock:
    def __init__(self, start_ns: int = 1_000_000_000) -> None:
        self.now = start_ns

    def __call__(self) -> int:
        return self.now

    def advance(self, ns: int) -> None:
        self.now += ns


class FixtureUpperLease:
    def __init__(self, log: list[str], *, fail: bool = False) -> None:
        self.log, self.fail, self.ok = log, fail, True

    def acquire(self, request: dict[str, Any]) -> str:
        self.log.append("upper:acquire")
        if self.fail:
            raise RuntimeError("upper lease busy")
        return "upper-token"

    def valid(self, token: str) -> bool:
        return self.ok and token == "upper-token"

    def renew(self, token: str) -> None:
        self.log.append("upper:renew")
        if not self.ok:
            raise RuntimeError("upper lease expired")

    def release(self, token: str, reason: str) -> None:
        self.log.append("upper:release")


class FixtureCarLease:
    """Second layer of the same hardware lease; stop() precedes any release."""

    def __init__(self, log: list[str], *, fail: bool = False) -> None:
        self.log, self.fail, self.ok = log, fail, True

    def acquire(self, request: dict[str, Any]) -> str:
        self.log.append("car:acquire")
        if self.fail:
            raise RuntimeError("car lease busy")
        return "car-token"

    def valid(self, token: str) -> bool:
        return self.ok and token == "car-token"

    def renew(self, token: str) -> None:
        self.log.append("car:renew")
        if not self.ok:
            raise RuntimeError("car lease expired")

    def release(self, token: str, reason: str) -> None:
        self.log.append("car:release")

    def stop(self, reason: str) -> None:
        self.log.append("car:stop")


class FixtureT1Driver:
    """T1 interface fixture: capabilities / read_snapshot / submit / stop.

    `submit` records intent only; outputs_enabled is always False in Wave A.
    """

    def __init__(self, log: list[str] | None = None, *, now_ns: int = 1_000_000_000) -> None:
        self.log = log if log is not None else []
        self.now_ns = now_ns
        self.outputs_enabled = False
        self.submitted: list[dict[str, Any]] = []

    def capabilities(self) -> dict[str, Any]:
        return {"simulated": True, "outputs_enabled": False,
                "base_topic": "/controller/cmd_vel", "base_units": ["m/s", "m/s", "rad/s"],
                "arm_joint_names": list(ARM_JOINTS), "gripper_joint_name": "r_joint",
                "gripper_servo_id": 10, "camera_capability": "unknown",
                "firmware_capability": "unknown", "firmware_watchdog_verified": False,
                "hardware_stop_effect": "unverified", "controller_acknowledged": "unavailable"}

    def read_snapshot(self) -> dict[str, Any]:
        return trusted_feedback_fixture(now_ns=self.now_ns, calibration_hash="fixture-bundle-hash")

    def submit(self, command: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(dict(command))
        self.log.append("driver:submit")
        return {"simulated": True, "driver_sent": False,
                "controller_acknowledged": "unavailable", "execution_observed": False}

    def stop(self, reason: str) -> dict[str, Any]:
        self.log.append("driver:stop")
        return {"simulated": True, "stopped": True, "reason": str(reason),
                "hardware_effect": "unverified"}


class FixtureObserver:
    """Server-side trusted observer fixture. It never invents *measured* feedback."""

    def __init__(self, *, bundle_hash: str = "fixture-bundle-hash") -> None:
        self.bundle_hash = bundle_hash
        self.facts: dict[str, Any] = {
            "leader_connected": True, "leader_healthy": True,
            "feedback_valid": True, "exclusive_control": True,
            "alignment_valid": True,
            "alignment_reference": {"leader_rad": [0.0] * 5, "follower_rad": [0.0] * 5},
            "devices": {"leader": {"role": "so101_leader", "simulated": True},
                        "follower": {"role": "landerpi_follower", "simulated": True},
                        "gamepad": {"status": "unknown", "simulated": True}},
            "feedback": trusted_feedback_fixture(now_ns=1_000_000_000, calibration_hash=bundle_hash),
            "video": {"available": False, "reason": "camera capability unknown", "frame_seq": None},
            "recording": {"status": "not_started", "simulated": True},
        }

    def calibration_readiness(self) -> dict[str, Any]:
        return {"ready": True, "bundle_hash": self.bundle_hash, "reasons": [],
                "simulated": True,
                "camera_capability": "unknown", "firmware_capability": "unknown"}

    def snapshot(self) -> dict[str, Any]:
        return dict(self.facts)


def trusted_feedback_fixture(*, now_ns: int = 1_000_000_000,
                             calibration_hash: str = "fixture-bundle-hash",
                             source_kind: str = TRUSTED_SOURCE) -> dict[str, Any]:
    """Six individually timestamped readbacks; a scan, never a simultaneous sample."""
    return {"simulated": True, "clock_domain": "monotonic", "source_kind": source_kind,
            "samples": [
                {"name": name, "servo_id": FEEDBACK_SERVO_IDS[name], "source_kind": source_kind,
                 "valid": True, "error": None, "raw_position": 500, "position_rad": 0.0,
                 "calibration_hash": calibration_hash, "sample_seq": 1,
                 "received_mono_ns": now_ns - index, "sample_time_ns": None,
                 # A simulation still has an explicit sample age.  Omitting it
                 # makes the browser correctly refuse to call the data fresh.
                 "age_ns": index, "age_s": index / 1_000_000_000}
                for index, name in enumerate(FEEDBACK_JOINTS)
            ]}


def calibration_assets_fixture(*, measured_at_utc: str = MEASURED_AT) -> dict[str, dict[str, Any]]:
    """Six role-correct, measured-looking assets for contract tests. SIMULATED."""
    limits = {name: [-2.09, 2.09] for name in JOINTS}
    assets: dict[str, dict[str, Any]] = {
        "base": {
            "schema_version": 1, "kind": "base", "role": ROLES["base"], "identity": "fixture-base-001",
            "measured": True, "measured_at_utc": measured_at_utc,
            "wheel_order": ["front_left", "front_right", "rear_left", "rear_right"],
            "signs": [1, -1, 1, -1], "counts_per_wheel_rev": 1000,
            "wheel_geometry": {"wheel_radius_m": 0.05, "track_width_m": 0.2, "wheelbase_m": 0.2},
            "kinematics_version": "mecanum_v1", "correction_model": {"scale": [1.0, 1.0, 1.0]},
            "imu_calibration_ref": "fixture-imu-001", "estimator_source": "wheel_odometry_estimate",
            "residuals": {"forward": 0.01}, "test_conditions": {"surface": "fixture"},
        },
        "follower": {
            "schema_version": 1, "kind": "follower", "role": ROLES["follower"],
            "identity": "fixture-follower-001", "measured": True,
            "measured_at_utc": measured_at_utc,
            "joint_order": list(JOINTS), "limits_rad": {name: list(pair) for name, pair in limits.items()},
            "soft_limits_rad": {name: [-2.0, 2.0] for name in JOINTS},
            "device_identity": {"control_board": "fixture-board-001",
                                "servo_ids": dict(FEEDBACK_SERVO_IDS)},
            "raw_to_rad": {"ticks_per_rev": 1000, "sweep_deg": 240.0, "zero_tick": 500},
            "source_kind": TRUSTED_SOURCE,
        },
        "leader": {
            "schema_version": 1, "kind": "leader", "role": ROLES["leader"],
            "identity": "fixture-leader-001", "measured": True,
            "measured_at_utc": measured_at_utc,
            "joint_order": ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex",
                            "wrist_roll", "gripper"],
            "device_identity": {"serial": "fixture-leader-serial",
                                "usb_by_id": "/dev/serial/by-id/fixture-so101-leader",
                                "lerobot_calibration": {
                                    name: {"id": index, "drive_mode": 0, "homing_offset": 0,
                                           "range_min": 0, "range_max": 4095}
                                    for index, name in enumerate(
                                        ["shoulder_pan", "shoulder_lift", "elbow_flex",
                                         "wrist_flex", "wrist_roll", "gripper"], start=1)}},
        },
        "mapping": {
            "schema_version": 1, "kind": "mapping", "role": ROLES["mapping"],
            "identity": "fixture-mapping-001", "measured": True,
            "measured_at_utc": measured_at_utc,
            "joint_order": list(JOINTS),
            "bound_identities": {"leader": "fixture-leader-001", "follower": "fixture-follower-001"},
            "joints": [{"leader": name, "follower": follower,
                        "direction": 1, "scale": 1.0, "wrap": False}
                       for name, follower in zip(
                           ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"],
                           ARM_JOINTS)],
            "gripper": {"leader": "gripper_percent", "follower": "r_joint",
                        "direction": 1, "scale": 1.0, "pulse_span_per_hundred_pct": 6.0,
                        "pulse_min": 0.0, "pulse_max": 999.0},
        },
        "gamepad": {
            "schema_version": 1, "kind": "gamepad", "role": ROLES["gamepad"],
            "identity": "fixture-gamepad-001", "measured": True,
            "measured_at_utc": measured_at_utc,
            "axes": {name: {"index": index, "min": -1.0, "center": 0.0, "max": 1.0,
                            "deadzone": 0.1}
                     for index, name in enumerate(("vx", "vy", "wz"))},
            "deadman_button": 0, "center": [0.0, 0.0, 0.0],
            "full_scale": 1.0, "speed_tiers": [0.3, 0.6, 1.0],
            "device_identity": {"vid": "fixture-vid", "pid": "fixture-pid",
                                "serial": "fixture-serial", "host": "pi"},
        },
        "camera": {
            "schema_version": 1, "kind": "camera", "role": ROLES["camera"],
            "identity": "fixture-camera-001", "measured": True,
            "measured_at_utc": measured_at_utc,
            "intrinsics": {"matrix": [500.0, 0.0, 320.0, 0.0, 500.0, 200.0, 0.0, 0.0, 1.0],
                           "distortion": [0.0, 0.0, 0.0, 0.0, 0.0],
                           "width": 640, "height": 400, "fps": 15},
            "extrinsics": {"parent_frame": "link4", "child_frame": "camera_optical_frame",
                           "translation_m": [0.0, 0.0, 0.0],
                           "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0],
                           "mount_chain": ["link4", "camera_connect_link"],
                           "T_base_arm": {"translation_m": [0.0, 0.0, 0.0]},
                           "T_arm_camera": {"translation_m": [0.0, 0.0, 0.0]}},
            "device_identity": {"serial": "fixture-camera-serial"},
        },
    }
    # The strict asset schema binds each layer to its own content hash and version.
    for asset in assets.values():
        digest = canonical_sha256(asset)
        asset["content_sha256"] = digest
        asset["version"] = digest[:12]
    return assets


def rig_bundle_fixture(assets: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    assets = assets or calibration_assets_fixture()
    return {"schema_version": 1, "kind": "rig_bundle", "simulated": True,
            "profile": PROFILE,
            "assets": {kind: {"identity": assets[kind]["identity"],
                              "sha256": canonical_sha256(assets[kind])} for kind in KINDS}}


def calibration_document_fixture() -> dict[str, Any]:
    """Canonical schema/example deliverable (also written to the T3 evidence dir)."""
    assets = calibration_assets_fixture()
    return {"simulated": True, "schema_version": 1, "profile": PROFILE,
            "kinds": list(KINDS), "roles": dict(ROLES), "bundle_kind": "rig_bundle",
            "joint_order": list(JOINTS), "trusted_feedback_source": TRUSTED_SOURCE,
            "assets": assets, "bundle": rig_bundle_fixture(assets)}


def status_fixture() -> dict[str, Any]:
    """Frozen status shape from the parallel plan section 3.3. SIMULATED."""
    return {
        "simulated": True, "state": "disconnected", "session_id": None, "session_epoch": None,
        "profile": PROFILE,
        "leases": {"upper": {"held": False, "simulated": True},
                   "car": {"held": False, "simulated": True}},
        "channels": {name: {"enabled": False, "permitted": False, "state": "locked" if name == "base" else "idle",
                            "seq": None, "pending": None} for name in CHANNELS},
        "devices": {"leader": {"role": "so101_leader", "status": "unknown"},
                    "follower": {"role": "landerpi_follower", "status": "unknown"},
                    "gamepad": {"status": "unknown"}},
        "calibrations": {"ready": False, "reasons": ["calibration_provider_unavailable"],
                         "camera_capability": "unknown", "firmware_capability": "unknown"},
        "feedback": {"valid": False, "trusted": False, "reason": "no_trusted_readback",
                     "source_kind": None},
        "video": {"available": False, "reason": "camera capability unknown", "frame_seq": None},
        "recording": {"status": "not_started"},
        "last_error": None, "missing_for_following": ["session_not_ready"],
        "outputs_enabled": False, "phase": "grasp",
        "confirmation_levels": list(CONFIRMATION_LEVELS),
    }


def capabilities_fixture() -> dict[str, Any]:
    """Frozen capabilities payload; unknown hardware capabilities are explicit."""
    return {"simulated": True, "schema_version": 1, "profile": PROFILE,
            "available": False, "outputs_enabled": False, "hardware_verified": False,
            "calibration": {"kinds": list(KINDS), "roles": dict(ROLES),
                            "bundle_kind": "rig_bundle", "camera_capability": "unknown",
                            "firmware_capability": "unknown"},
            "channels": {name: {"permitted": False, "source": ("gamepad" if name == "base"
                                                               else "so101_leader")} for name in CHANNELS},
            "trusted_feedback_sources": [TRUSTED_SOURCE],
            "rejected_feedback_sources": ["command_cache", "model_placeholder", "synthetic",
                                          "vendor_joint_states"],
            "firmware_capability": "unknown", "camera_capability": "unknown",
            "firmware_watchdog_verified": False, "hardware_stop_effect": "unverified",
            "controller_acknowledged": "unavailable",
            "reason": "wave_a_no_hardware_adapters_bound"}


def build_fixture_facade() -> HybridPublicFacade:
    """Return a fully wired, explicitly simulated facade for offline tests.

    The returned object has `simulated_context` with the clock, call log and T1 driver.
    No default production path installs these fixtures.
    """
    log: list[str] = []
    clock = FixtureClock()
    driver = FixtureT1Driver(log, now_ns=clock.now)
    observer = FixtureObserver()
    facade = HybridPublicFacade(upper=FixtureUpperLease(log), car=FixtureCarLease(log),
                                observer=observer, clock_ns=clock,
                                gamepad_deadzone=0.1, gamepad_timeout_ns=300_000_000)
    facade.bind_driver(driver)
    facade.simulated_context = {"simulated": True, "log": log, "clock": clock,
                                "driver": driver, "observer": observer,
                                "status_fixture": status_fixture(),
                                "capabilities_fixture": capabilities_fixture()}
    return facade


def gamepad_sample(seq: int, now_ns: int, deadman: bool,
                   axes: list[float] | None = None,
                   *, source: str = "gamepad", identity: str = "fixture-gamepad-001") -> dict[str, Any]:
    return {"simulated": True, "source": source, "device_identity": identity,
            "seq": seq, "received_mono_ns": now_ns, "deadman": deadman,
            "axes": list(axes or [0.0, 0.0, 0.0])}
