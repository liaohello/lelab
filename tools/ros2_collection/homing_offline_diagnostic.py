#!/usr/bin/env python3
"""OFFLINE ONLY - continuous homing diagnostic over the local Humble rig.

This drives the production upper-side facade and frame link against the offline
Docker/Humble car stack (production agent + standalone control process + simulated
board owner). Only the SO-101 leader edge is simulated here; the car side is the
real control process, and every topic is under /lelab_offline.

Sequence: read-only preflight -> new session -> bounded heartbeat -> only_joint
homing (complete) -> only_joint start+cancel -> release. The heartbeat stops on
task end or its own total deadline, so a hung diagnostic cannot renew leases
forever. Nothing here opens a serial port, and every artefact is stamped
data_identity=simulated_offline.

Run it through tools/ros2_collection/homing_offline_diagnostic.sh, which starts an
isolated rig whose control-process bundle matches the calibration fixture.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "app_ros_mobile")]

from lelab.hybrid_calibration import HybridCalibrationStore  # noqa: E402
from lelab.hybrid_fixtures import calibration_assets_fixture  # noqa: E402
from lelab.hybrid_hardware import _http_json  # noqa: E402
from lelab import hybrid_stream_hardware as stream  # noqa: E402

IDENTITIES = {"leader": "fixture-leader-001",
              "follower": "fixture-follower-001",
              "mapping": "fixture-mapping-001"}
JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint")
ARM_JOINTS = JOINTS[:5]
DATA_IDENTITY = "simulated_offline"


class SimulatedLeader:
    """Six named positions that move smoothly; never a hardware reading."""

    def __init__(self) -> None:
        self.reads = 0

    def snapshot(self) -> dict[str, Any]:
        self.reads += 1
        phase = self.reads / 30.0
        positions = {
            "shoulder_pan": 0.1 * math.sin(phase),
            "shoulder_lift": -0.2 + 0.05 * math.cos(phase),
            "elbow_flex": 0.3,
            "wrist_flex": 0.4,
            "wrist_roll": 0.5,
        }
        return {"connected": True, "healthy": True, "age_s": 0.001,
                "reads": self.reads, "positions_rad": positions,
                "gripper_percent": 42.0, "source": "simulated_leader_offline",
                "simulated": True}


class SimulatedUpperLease:
    """Duck-typed stand-in for UpperLeaderLease; no serial device exists."""

    def __init__(self) -> None:
        self.reader = SimulatedLeader()
        self.active = False
        self.renewals = 0

    def acquire(self, _request: dict[str, Any]) -> str:
        if self.active:
            raise RuntimeError("upper_lease_already_held")
        self.active = True
        return "offline-simulated-upper-token"

    def valid(self, token: str) -> bool:
        return self.active and token == "offline-simulated-upper-token"

    def renew(self, token: str) -> bool:
        if self.valid(token):
            self.renewals += 1
            return True
        return False

    def release(self, token: str, _reason: str) -> None:
        if token == "offline-simulated-upper-token":
            self.active = False

    def probe(self) -> dict[str, Any]:
        return {"simulated": True, "healthy": True}


def prepare_store(root: Path) -> tuple[HybridCalibrationStore, dict[str, str]]:
    """Install the fixture layers once; re-saving would bump versions and the bundle.

    The wrapper wipes and recreates this scratch root before computing the bundle,
    starts the rig with that bundle, and then the diagnostic reuses the same stored
    versions. The follower asset is given the per-joint raw conversion
    ``plan_home_step`` requires (the browser fixture only carries the aggregate).
    """
    assets = calibration_assets_fixture(measured_at_utc="2026-11-06T00:00:00Z")
    conversion = dict(assets["follower"].get("raw_to_rad") or {})
    for name in JOINTS:
        conversion[name] = {"zero_tick": 500, "centre_raw": 500,
                            "direction": -1.0 if name == "r_joint" else 1.0,
                            "radians_per_tick": math.pi / 750.0}
    assets["follower"]["raw_to_rad"] = conversion
    store = HybridCalibrationStore(root)
    identities = {}
    for kind in ("leader", "follower", "mapping"):
        identities[kind] = str(assets[kind]["identity"])
        try:
            store.load(kind, identities[kind])
        except Exception:  # noqa: BLE001 - absent layer is the only case we create
            store.save(kind, identities[kind], assets[kind])
    return store, identities


def build_facade(agent_url: str, frame_port: int, store: HybridCalibrationStore,
                 identities: dict[str, str]):
    upper = SimulatedUpperLease()
    car = stream.FrameCarControlLease(
        agent_url, expected_profile="hardware_hybrid_homing_v1_offline")
    observer = stream.StreamHardwareObserver(
        upper=upper, agent_url=agent_url, store=store,
        leader_identity=identities["leader"],
        follower_identity=identities["follower"],
        mapping_identity=identities["mapping"],
        simulated_source=True)
    car.observer = observer
    facade = stream.FrameStreamHardwareFacade(
        upper=upper, car=car, observer=observer, calibration_store=store)
    facade.bind_driver(stream.FrameStreamDriver(
        car, observer, agent_url, host="127.0.0.1", port=frame_port))
    facade.hardware_identities = dict(identities)
    return facade, observer, upper


def wait_until(predicate, timeout_s: float, period_s: float = 0.05):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(period_s)
    return None


def frame_process(status: dict[str, Any]) -> dict[str, Any]:
    return (((status.get("licence") or {}).get("bridge") or {}).get("frame_process") or {})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-url", default="http://127.0.0.1:18131")
    parser.add_argument("--frame-port", type=int, default=18123)
    parser.add_argument("--calibration-root", required=True)
    parser.add_argument("--bundle", default="",
                        help="bundle the rig was started with (bin/docker LROS_DOCKER_BUNDLE)")
    parser.add_argument("--print-bundle", action="store_true",
                        help="print the facade bundle for this calibration root and exit")
    parser.add_argument("--report", default="")
    parser.add_argument("--only-joint", default="joint1")
    parser.add_argument("--phase-timeout-s", type=float, default=25.0)
    parser.add_argument("--heartbeat-total-s", type=float, default=90.0)
    args = parser.parse_args()

    root = Path(args.calibration_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    store, identities = prepare_store(root)
    if args.print_bundle:
        probe, _observer, _upper = build_facade(args.agent_url, args.frame_port,
                                               store, identities)
        readiness = probe.observer.calibration_readiness()
        print(json.dumps({"ready": readiness.get("ready"),
                          "bundle_hash": readiness.get("bundle_hash"),
                          "reasons": readiness.get("reasons")}, sort_keys=True))
        return 0 if readiness.get("ready") else 2
    if not args.bundle:
        print("--bundle is required (run --print-bundle first)", file=sys.stderr)
        return 2
    facade, observer, upper = build_facade(args.agent_url, args.frame_port, store, identities)
    report: dict[str, Any] = {
        "data_identity": DATA_IDENTITY, "simulated": True,
        "training_eligible": False, "hardware_effect": "simulated_only",
        "agent_url": args.agent_url, "frame_port": args.frame_port,
        "phases": {}, "timeline": [], "failures": [],
    }

    def fail(stage: str, error: str) -> None:
        report["failures"].append({"stage": stage, "error": str(error)})

    heartbeat_stop = threading.Event()
    heartbeat_state = {"renewals": 0, "failures": 0, "deadline_hit": False,
                       "last_error": None}
    timeline_stop = threading.Event()

    def heartbeat_loop() -> None:
        deadline = time.monotonic() + max(1.0, args.heartbeat_total_s)
        while not heartbeat_stop.wait(1.0):
            if time.monotonic() > deadline:
                heartbeat_state["deadline_hit"] = True
                return
            try:
                facade.heartbeat()
                heartbeat_state["renewals"] += 1
            except Exception as exc:  # noqa: BLE001 - a lost renewal is reported, not retried
                heartbeat_state["failures"] += 1
                heartbeat_state["last_error"] = f"{type(exc).__name__}:{exc}"
                return

    def timeline_loop() -> None:
        while not timeline_stop.wait(0.1):
            try:
                status = facade.status()
            except Exception as exc:  # noqa: BLE001
                report["timeline"].append({"t": round(time.monotonic(), 3),
                                           "status_error": f"{type(exc).__name__}:{exc}"})
                continue
            homing = status.get("homing") or {}
            report["timeline"].append({
                "t": round(time.monotonic(), 3),
                "state": status.get("state"),
                "outputs_enabled": status.get("outputs_enabled"),
                "homing_state": homing.get("state"),
                "current_joint": homing.get("current_joint"),
                "step_target_raw": homing.get("step_target_raw"),
                "observed_raw": homing.get("observed_raw"),
                "feedback_age_s": homing.get("feedback_age_s"),
                "feedback_sample_seq": homing.get("feedback_sample_seq"),
                "publisher_instance": homing.get("feedback_publisher_instance"),
                "torque_state": (homing.get("torque") or {}).get("state"),
                "last_error": homing.get("last_error"),
            })

    heartbeat_thread = threading.Thread(target=heartbeat_loop, name="diag-heartbeat",
                                        daemon=True)
    timeline_thread = threading.Thread(target=timeline_loop, name="diag-timeline",
                                       daemon=True)
    exit_code = 0
    try:
        # ---- Phase 0: read-only preflight (no torque, no session) -----------
        preflight: dict[str, Any] = {}
        initial: dict[str, int] = {}
        try:
            # A freshly started rig needs a bounded moment before the agent's licence
            # bridge has read the control process status file once.
            deadline = time.monotonic() + 8.0
            status: dict[str, Any] = {}
            process: dict[str, Any] = {}
            while time.monotonic() < deadline:
                status = _http_json(args.agent_url, "/status", timeout=3.0)
                process = frame_process(status)
                age = process.get("age_s")
                if (process.get("homing_torque_state") is not None
                        and isinstance(age, (int, float)) and 0 <= age <= 1.0):
                    break
                time.sleep(0.2)
            feedback = ((status.get("control") or {}).get("follower_feedback") or {})
            samples = feedback.get("joint_samples") or {}
            initial = {name: int(samples[name]["raw_position"]) for name in JOINTS
                       if isinstance(samples.get(name), dict)
                       and isinstance(samples[name].get("raw_position"), (int, float))}
            preflight = {
                "simulated": status.get("simulated"),
                "data_source": status.get("data_source"),
                "control_active": (status.get("control") or {}).get("active"),
                "outputs_enabled": (status.get("licence") or {}).get("frame_outputs_enabled"),
                "feedback_available": feedback.get("available"),
                "feedback_reason": feedback.get("reason"),
                "publisher_instance": feedback.get("publisher_instance"),
                "publisher_generation": feedback.get("publisher_generation"),
                "samples": len(samples),
                "oldest_age_s": feedback.get("oldest_age_s"),
                "torque_state": process.get("homing_torque_state"),
                "torque_publish_count": process.get("torque_publish_count"),
                "process_age_s": process.get("age_s"),
            }
            assert preflight["simulated"] is True, "rig is not declared simulated"
            assert preflight["control_active"] is False, "a control lease is already active"
            assert preflight["outputs_enabled"] is False, "outputs are already enabled"
            assert len(samples) == 6 and len(initial) == 6, "six servo samples are not present"
            assert type(preflight["publisher_instance"]) is int, "feedback has no owner instance"
            oldest = preflight["oldest_age_s"]
            assert isinstance(oldest, (int, float)) and 0 <= oldest <= 0.5, \
                f"feedback is stale pre-session ({oldest})"
            # Before a session the samples are intentionally untrusted (the bundle is
            # not bound yet); the preflight only proves the publisher is alive.
            assert preflight["torque_state"] == "idle", "preflight unexpectedly moved torque"
            assert preflight["torque_publish_count"] in (0, None), \
                "preflight wrote torque"
        except Exception as exc:  # noqa: BLE001
            fail("preflight", exc)
            raise
        report["phases"]["preflight"] = {"ok": True, **preflight}

        # ---- Phase 1: new session -----------------------------------------
        acquired = facade.acquire_session({"owner": "offline-homing-diagnostic"})
        identity = {"session_id": acquired.get("session_id"),
                    "session_epoch": acquired.get("session_epoch"),
                    "calibration_bundle_hash": acquired.get("calibration_bundle_hash")}
        if identity["calibration_bundle_hash"] != args.bundle:
            fail("session", "bundle_mismatch:"
                 f"facade={identity['calibration_bundle_hash']} rig={args.bundle}")
            raise SystemExit(3)
        if acquired.get("state") != "ready":
            fail("session", f"state={acquired.get('state')}")
            raise SystemExit(3)
        report["phases"]["session"] = {
            "ok": True, "session_id": identity["session_id"],
            "session_epoch": identity["session_epoch"],
            "bundle_matches_rig": True,
        }
        heartbeat_thread.start()
        timeline_thread.start()

        # ---- Phase 2: only_joint homing to completion ----------------------
        facade.start_home({**identity, "action": "start",
                           "only_joint": args.only_joint})
        terminal = wait_until(
            lambda: (facade.status().get("homing") or {}).get("state")
            in {"complete", "fault", "cancelled", "stopped"},
            args.phase_timeout_s)
        status = facade.status()
        homing = status.get("homing") or {}
        torque = homing.get("torque") or {}
        if terminal is not True or homing.get("state") != "complete":
            fail("homing_complete", str(homing.get("last_error") or homing.get("state")))
            raise SystemExit(4)
        observed = homing.get("observed_raw") or {}
        target = homing.get("targets_raw", {}).get(args.only_joint)
        if not isinstance(target, int) or abs(int(observed.get(args.only_joint, -9999)) - target) > 2:
            fail("homing_complete", f"target_not_reached:{observed}")
            raise SystemExit(4)
        if torque.get("state") != "verified":
            fail("homing_complete", f"torque_not_verified:{torque}")
            raise SystemExit(4)
        for name in JOINTS:
            if name == args.only_joint:
                continue
            if abs(int(observed.get(name, 0)) - int(initial.get(name, 0))) > 10:
                fail("homing_complete", f"unselected_axis_moved:{name}")
                raise SystemExit(4)
        report["phases"]["homing_complete"] = {
            "ok": True, "only_joint": args.only_joint,
            "initial_raw": initial, "observed_raw": observed,
            "torque": torque, "outputs_enabled_after": status.get("outputs_enabled"),
        }

        # ---- Phase 3: only_joint start then explicit cancel ----------------
        facade.start_home({**identity, "action": "start", "only_joint": "r_joint"})
        cancelled = facade.cancel_home({**identity, "action": "cancel"})
        home_after_cancel = cancelled.get("homing") or {}
        if home_after_cancel.get("state") != "cancelled":
            fail("homing_cancel", f"state={home_after_cancel.get('state')}")
            raise SystemExit(4)
        if cancelled.get("outputs_enabled") is not False:
            fail("homing_cancel", "outputs still enabled after cancel")
            raise SystemExit(4)
        report["phases"]["homing_cancel"] = {
            "ok": True, "state": home_after_cancel.get("state"),
            "outputs_enabled": cancelled.get("outputs_enabled"),
        }

        # ---- Phase 4: task end stops renewal, then release -----------------
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=3.0)
        if heartbeat_thread.is_alive():
            fail("release", "heartbeat thread did not stop")
            raise SystemExit(4)
        timeline_stop.set()
        timeline_thread.join(timeout=3.0)
        facade.release_session("offline_diagnostic_complete",
                               session_id=identity["session_id"],
                               session_epoch=identity["session_epoch"])
        if upper.active:
            fail("release", "simulated upper lease was not released")
            raise SystemExit(4)
        time.sleep(1.2)
        final = _http_json(args.agent_url, "/status", timeout=3.0)
        final_control = (final.get("control") or {}).get("active")
        final_outputs = (final.get("licence") or {}).get("frame_outputs_enabled")
        if final_control is not False or final_outputs is not False:
            fail("release", f"car still active after release: control={final_control} "
                            f"outputs={final_outputs}")
            raise SystemExit(4)
        report["phases"]["release"] = {
            "ok": True, "heartbeat_renewals": heartbeat_state["renewals"],
            "heartbeat_failures": heartbeat_state["failures"],
            "heartbeat_deadline_hit": heartbeat_state["deadline_hit"],
            "car_control_active": final_control, "car_outputs_enabled": final_outputs,
            "simulated_upper_renewals": upper.renewals,
        }
        report["status"] = "passed"
    except SystemExit as exc:
        exit_code = int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001
        fail("unexpected", f"{type(exc).__name__}:{exc}")
        exit_code = 5
    finally:
        heartbeat_stop.set()
        timeline_stop.set()
        for thread in (heartbeat_thread, timeline_thread):
            if thread.is_alive():
                thread.join(timeout=2.0)
        try:
            if facade.session_id is not None:
                facade.release_session("offline_diagnostic_cleanup",
                                       session_id=facade.session_id,
                                       session_epoch=facade.session_epoch)
        except Exception as exc:  # noqa: BLE001
            fail("cleanup", f"{type(exc).__name__}:{exc}")
        observer.stop_cache()

    if "status" not in report:
        report["status"] = "failed"
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    print(text)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    return 0 if report["status"] == "passed" else (exit_code or 1)


if __name__ == "__main__":
    raise SystemExit(main())