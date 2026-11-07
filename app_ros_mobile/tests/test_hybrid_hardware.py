from __future__ import annotations

import time
from types import SimpleNamespace

from lelab import hybrid_hardware
from lelab.hybrid_calibration import HybridCalibrationStore
from lelab.hybrid_fixtures import calibration_assets_fixture


class Reader:
    def snapshot(self):
        return {
            "connected": True,
            "healthy": True,
            "reason": "ok",
            "positions_rad": {
                "shoulder_pan": 0.1,
                "shoulder_lift": -0.2,
                "elbow_flex": 0.3,
                "wrist_flex": 0.4,
                "wrist_roll": 0.5,
            },
            "gripper_percent": 42.0,
            "source": "leader:/dev/serial/by-id/test",
        }


def _layers(tmp_path):
    assets = calibration_assets_fixture(measured_at_utc="2026-11-04T00:00:00Z")
    store = HybridCalibrationStore(tmp_path)
    identities = {}
    for kind in ("leader", "follower", "mapping"):
        identities[kind] = assets[kind]["identity"]
        store.save(kind, identities[kind], assets[kind])
    return store, identities


def _car_status():
    age = 0.02
    raw = {"joint1": 500, "joint2": 728, "joint3": 124,
           "joint4": 99, "joint5": 499, "r_joint": 150}
    ids = {"joint1": 1, "joint2": 2, "joint3": 3,
           "joint4": 4, "joint5": 5, "r_joint": 10}
    samples = {
        name: {
            "name": name, "servo_id": ids[name], "raw_position": value,
            "source_kind": "servo_position_readback", "sample_seq": 3,
            "age_s": age, "received_mono_ns": 1, "sample_time_ns": 2,
            "valid": True, "error": None,
        }
        for name, value in raw.items()
    }
    return {
        "control": {"active": True, "follower_feedback": {
            "available": True, "reason": "ok", "joint_samples": samples,
            "scan_span_s": 0.04, "stale_after_s": 0.5,
        }},
        "hybrid": {"driver_bound": True, "outputs_enabled": False},
        "sensors": {"rgb": {
            "topic": "/ascamera/camera_publisher/rgb0/image",
            "stale": False, "preview_seq": 12,
            "source_stamp_ns": time.time_ns(), "age_s": 0.03,
        }},
    }


def test_three_layer_observer_is_real_data_only_and_preview_never_outputs(tmp_path, monkeypatch):
    store, identities = _layers(tmp_path)
    upper = SimpleNamespace(reader=Reader())
    observer = hybrid_hardware.HardwareObserver(
        upper=upper,
        agent_url="http://car.invalid",
        store=store,
        leader_identity=identities["leader"],
        follower_identity=identities["follower"],
        mapping_identity=identities["mapping"],
    )
    readiness = observer.calibration_readiness()
    assert readiness["ready"] is True
    assert readiness["layout"] == "so101_three_layer"
    assert readiness["bundle_hash"]

    monkeypatch.setattr(hybrid_hardware, "_http_json", lambda *args, **kwargs: _car_status())
    facts = observer.snapshot()
    assert facts["leader_healthy"] is True
    assert facts["feedback_valid"] is True
    assert facts["exclusive_control"] is True
    assert facts["outputs_enabled"] is False
    assert facts["feedback"]["servo_pulses"]["r_joint"] == 150
    assert facts["feedback"]["clock_domain"] == "upper_receipt_monotonic"
    assert set(facts["feedback"]["positions_rad"]) == set(hybrid_hardware.JOINTS)
    assert facts["feedback"]["scan_span_ns"] == 40_000_000

    reference = observer.capture_reference()
    assert reference["follower_gripper_raw"] == 150
    facts = observer.snapshot()
    assert facts["preview"]["available"] is True
    assert facts["preview"]["motion_commands_sent"] == 0
    assert facts["preview"]["outputs_enabled"] is False

    facade = hybrid_hardware.HardwarePreviewFacade(
        upper=upper, car=object(), observer=observer, calibration_store=store,
    )
    facade.bind_driver(hybrid_hardware.ReadbackOnlyDriver(observer))
    caps = facade.capabilities()
    assert caps["available"] is True
    assert caps["preview_available"] is True
    assert caps["hardware_verified"] is False
    assert caps["outputs_enabled"] is False
    assert "preview" in caps["actions"]
    assert "enable_base" not in caps["actions"]
    public_status = facade.status()
    assert public_status["hardware_probe"]["feedback_valid"] is True
    assert public_status["feedback"]["positions_rad"]["joint2"] > 0
    assert public_status["calibrations"]["ready"] is True


def test_missing_three_layers_keeps_preview_closed(tmp_path):
    store = HybridCalibrationStore(tmp_path)
    observer = hybrid_hardware.HardwareObserver(
        upper=SimpleNamespace(reader=None),
        agent_url="http://car.invalid",
        store=store,
        leader_identity="missing-leader",
        follower_identity="missing-follower",
        mapping_identity="missing-mapping",
    )
    assert observer.calibration_readiness()["ready"] is False
    facade = hybrid_hardware.HardwarePreviewFacade(
        upper=SimpleNamespace(), car=object(), observer=observer, calibration_store=store,
    )
    facade.bind_driver(hybrid_hardware.ReadbackOnlyDriver(observer))
    caps = facade.capabilities()
    assert caps["available"] is True
    assert caps["preview_available"] is False
    assert "save" in caps["actions"]
    assert "preview" not in caps["actions"]
    assert "enable_base" not in caps["actions"]


def test_observer_never_promotes_invalid_feedback(tmp_path, monkeypatch):
    store, identities = _layers(tmp_path)
    observer = hybrid_hardware.HardwareObserver(
        upper=SimpleNamespace(reader=Reader()),
        agent_url="http://car.invalid",
        store=store,
        leader_identity=identities["leader"],
        follower_identity=identities["follower"],
        mapping_identity=identities["mapping"],
    )
    status = _car_status()
    feedback = status["control"]["follower_feedback"]
    feedback.update({"available": False, "reason": "scan_span_exceeded",
                     "scan_span_s": 2.0})
    for sample in feedback["joint_samples"].values():
        sample.update({"source_kind": "command_cache", "valid": False,
                       "error": "untrusted_source"})
    monkeypatch.setattr(hybrid_hardware, "_http_json", lambda *args, **kwargs: status)

    facts = observer.snapshot()

    assert facts["feedback"]["valid"] is False
    assert facts["feedback_valid"] is False
    assert all(item["valid"] is False for item in facts["feedback"]["samples"])
    assert all(item["source_kind"] == "command_cache"
               for item in facts["feedback"]["samples"])
    assert "untrusted_source" in facts["feedback"]["samples"][0]["error"]


def test_readiness_rejects_mapping_bound_to_another_leader(tmp_path):
    store, identities = _layers(tmp_path)
    mapping = store.load("mapping", identities["mapping"]).data
    mapping["bound_identities"]["leader"] = "different-leader-device"
    store.save("mapping", identities["mapping"], mapping)
    observer = hybrid_hardware.HardwareObserver(
        upper=SimpleNamespace(reader=Reader()),
        agent_url="http://car.invalid",
        store=store,
        leader_identity=identities["leader"],
        follower_identity=identities["follower"],
        mapping_identity=identities["mapping"],
    )

    readiness = observer.calibration_readiness()

    assert readiness["ready"] is False
    assert any("mapping binds leader" in reason for reason in readiness["reasons"])
