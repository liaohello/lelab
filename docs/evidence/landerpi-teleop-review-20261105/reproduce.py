"""Offline reproductions for the 2026-11-05 teleop review.

No serial, ROS nodes, HTTP requests, or actuator outputs are used. Exit 0 means
the documented failure modes were reproduced, not that they were fixed.
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "app_ros_mobile"), str(ROOT / "ros2_ws/src/lelab_mobile_agent")]

from lelab import hybrid_hardware as hw
from lelab.hybrid_calibration import HybridCalibrationStore
from lelab.hybrid_fixtures import calibration_assets_fixture
from lelab.hybrid_session import HybridSession
from lelab.mobile_teleop import TeleopMapper


def main() -> None:
    report = {"kind": "offline_bug_reproduction", "actuator_output": False}
    mapping = {
        "joints": [
            {"leader": "a", "follower": "joint1", "direction": 1, "scale": 1},
            {"leader": "b", "follower": "joint2", "direction": 1, "scale": 1},
        ],
        "filter_alpha": 1.0,
        "max_step_rad_s": 0.5,
    }
    mapper = TeleopMapper(mapping, {"joint1": [-2, 2], "joint2": [-2, 2]})
    for name in ("joint1", "joint2"):
        mapper.joint_target(name, 0, 0, 0, 0.0)
    result = {
        name: mapper.joint_target(name, 1, 0, 0, 1.0)["target_rad"]
        for name in ("joint1", "joint2")
    }
    assert result == {"joint1": 0.5, "joint2": 0.0}
    report["mapper_shared_dt"] = result

    fresh_mapper = TeleopMapper(mapping, {"joint1": [-2, 2], "joint2": [-2, 2]})
    target = fresh_mapper.joint_target("joint1", 1, 0, 0, 0.1)["target_rad"]
    assert target == 1.0
    report["fresh_mapper_bypasses_rate_limit"] = {
        "target_after_0_1s": target,
        "allowed_delta_at_0_5rad_s": 0.05,
        "source_correspondence": "HardwareObserver._preview constructs a fresh mapper per snapshot",
    }

    with tempfile.TemporaryDirectory(prefix="lelab-review-") as tmp:
        store = HybridCalibrationStore(Path(tmp))
        assets = calibration_assets_fixture(measured_at_utc="2026-11-05T00:00:00Z")
        identities = {kind: assets[kind]["identity"] for kind in ("leader", "follower", "mapping")}
        for kind in identities:
            store.save(kind, identities[kind], assets[kind])
        wrong_mapping = copy.deepcopy(assets["mapping"])
        wrong_mapping["bound_identities"]["leader"] = "different-leader-device"
        store.save("mapping", identities["mapping"], wrong_mapping)
        observer = hw.HardwareObserver(
            upper=SimpleNamespace(reader=None), agent_url="http://not-used.invalid",
            store=store, leader_identity=identities["leader"],
            follower_identity=identities["follower"], mapping_identity=identities["mapping"],
        )
        ready = observer.calibration_readiness()["ready"]
        assert ready is True
        report["cross_identity_readiness"] = {
            "mapping_binds_leader": "different-leader-device",
            "loaded_leader": identities["leader"], "ready": ready,
        }
        store.save("mapping", identities["mapping"], assets["mapping"])

        samples = {
            name: {
                "servo_id": servo_id, "raw_position": 500,
                "source_kind": "command_cache", "valid": False,
                "age_s": 0.01, "sample_seq": 1, "sample_time_ns": 1,
            }
            for name, servo_id in zip(hw.JOINTS, (1, 2, 3, 4, 5, 10))
        }
        car = {
            "control": {"active": True, "follower_feedback": {
                "available": False, "reason": "scan_span_exceeded", "joint_samples": samples,
                "scan_span_s": 2.0, "stale_after_s": 0.5,
            }},
            "hybrid": {"driver_bound": True, "outputs_enabled": False},
        }
        observer.upper.reader = SimpleNamespace(snapshot=lambda: {
            "connected": True, "healthy": True, "positions_rad": {}, "gripper_percent": 0,
        })
        original_http = hw._http_json
        hw._http_json = lambda *args, **kwargs: car
        try:
            feedback = observer.snapshot()["feedback"]
        finally:
            hw._http_json = original_http
        assert feedback["valid"] is True
        report["observer_promotes_invalid_feedback"] = {
            "input_available": False, "input_source": "command_cache", "scan_span_s": 2.0,
            "output_valid": feedback["valid"],
            "first_sample_valid": feedback["samples"][0]["valid"],
            "first_sample_source": feedback["samples"][0]["source_kind"],
        }

    entered, proceed, stopped = threading.Event(), threading.Event(), threading.Event()
    errors = []

    class Observer:
        def calibration_readiness(self):
            return {"ready": True, "bundle_hash": "b"}

        def snapshot(self):
            entered.set()
            proceed.wait(2)
            return dict(leader_connected=True, leader_healthy=True,
                        feedback_valid=True, exclusive_control=True)

    lease = SimpleNamespace(valid=lambda token: True, stop=lambda reason: None)
    session = HybridSession(upper=lease, car=lease, observer=Observer())
    session.session_id, session.session_epoch, session.bundle_hash = "audit", 1, "b"
    session.upper_token, session.car_token, session.state = "upper", "car", "ready"

    def status_worker():
        try:
            session.status()
        except BaseException as exc:
            errors.append(type(exc).__name__)

    def stop_worker():
        try:
            session.emergency_stop("audit")
        except BaseException as exc:
            errors.append(type(exc).__name__)
        finally:
            stopped.set()

    status_thread = threading.Thread(target=status_worker)
    stop_thread = threading.Thread(target=stop_worker)
    status_thread.start()
    assert entered.wait(1)
    stop_thread.start()
    blocked = not stopped.wait(0.2)
    proceed.set()
    status_thread.join(2)
    stop_thread.join(2)
    assert blocked and stopped.is_set() and not errors
    report["estop_blocked_by_status_lock"] = {
        "blocked_for_at_least_s": 0.2,
        "finished_after_status_unblocked": stopped.is_set(), "thread_errors": errors,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
