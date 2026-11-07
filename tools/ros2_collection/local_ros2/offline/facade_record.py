#!/usr/bin/env python3
"""SIMULATED ONLY: production frame facade + recording controller + real ROS bag."""
from __future__ import annotations

import json
import time
import traceback

from facade_smoke import (AGENT, STREAM_PORT, WORK, SyntheticUpper, car_status,
                          HybridCalibrationStore, calibration_assets_fixture,
                          FrameCarControlLease, StreamHardwareObserver,
                          FrameStreamHardwareFacade, FrameStreamDriver,
                          configure_hybrid_facade)
from lelab.hybrid_hardware import _http_json
from lelab.hybrid_recording import HttpCarClient, HybridRecordingController


def main() -> int:
    status = car_status()  # refuses any non-simulated agent before opening a lease
    if (status.get("control") or {}).get("active"):
        raise RuntimeError("offline_agent_already_has_a_lease")
    store = HybridCalibrationStore(WORK / "calibration")
    assets = calibration_assets_fixture(measured_at_utc="2026-11-04T00:00:00Z")
    ids = {kind: assets[kind]["identity"] for kind in ("leader", "follower", "mapping")}
    upper = SyntheticUpper(agent_url=AGENT, store=store, leader_identity=ids["leader"])
    car = FrameCarControlLease(AGENT, expected_profile="hardware_hybrid_manual_v1_offline6d")
    observer = StreamHardwareObserver(upper=upper, agent_url=AGENT, store=store,
        leader_identity=ids["leader"], follower_identity=ids["follower"],
        mapping_identity=ids["mapping"], simulated_source=True)
    car.observer = observer
    facade = FrameStreamHardwareFacade(upper=upper, car=car, observer=observer, calibration_store=store)
    facade.bind_driver(FrameStreamDriver(car, observer, AGENT, host="127.0.0.1", port=STREAM_PORT))
    facade.simulated_context = {"simulated": True, "data_identity": "simulated_offline",
                                "training_eligible": False}
    facade = configure_hybrid_facade(facade=facade, replace=True)
    client = HttpCarClient(lambda path, body: _http_json(
        AGENT, path, method="POST", body=body, token=car.token, timeout=10.0))
    recording = HybridRecordingController(facade, client, offline_6d=True)
    facade.bind_recording_controller(recording, replace=True)
    report = {"data_identity": "simulated_offline", "training_eligible": False,
              "checks": [], "recording_id": None}
    identity = None

    def check(name, condition, detail=None):
        report["checks"].append({"name": name, "ok": bool(condition), "detail": detail})
        print(f"{'PASS' if condition else 'FAIL'} {name}: {detail}", flush=True)
        if not condition:
            raise RuntimeError(name)

    try:
        acquired = facade.acquire_session({"owner": "offline-facade-recording"})
        identity = {"session_id": acquired["session_id"], "session_epoch": acquired["session_epoch"]}
        facade.preview(identity)
        started = recording.dispatch("start_session", {**identity,
            "profile": "hardware_hybrid_manual_v1_offline6d", "task": "observe then grasp"})
        report["recording_id"] = started.get("car_recording_id")
        check("recording_started", bool(report["recording_id"]), report["recording_id"])
        for index in (1, 2):
            recording.dispatch("start_episode", {**identity, "episode_id": f"facade_ep_{index}"})
            recording.dispatch("set_phase", {**identity, "phase": "observe"})
            facade.preview(identity)
            for action in ("align", "arm", "engage"):
                facade.set_arm_state({"action": action, **identity})
            before = facade._follow_commands
            time.sleep(1.5)
            # The production controller's frame facade owns the phase barrier.
            recording.dispatch("set_phase", {**identity, "phase": "grasp"})
            facade.preview(identity)
            for action in ("align", "arm", "engage"):
                facade.set_arm_state({"action": action, **identity})
            time.sleep(1.5)
            check(f"episode_{index}_frames", facade._follow_commands > before,
                  {"frames": facade._follow_commands - before, "state": facade.state,
                   "error": facade.last_error})
            ended = recording.dispatch("end_episode", {**identity, "success": "unknown"})
            check(f"episode_{index}_paused", ended.get("stopped_first") is True,
                  {"arm_state": facade.arm_state, "state": facade.state,
                   "error": facade.last_error, "follow_error": facade._follow_error})
        stopped = recording.dispatch("stop_session", identity)
        check("recording_stopped", stopped.get("stopped_first") is True,
              {"episodes": len(stopped.get("episodes") or [])})
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "detail": str(exc),
                           "traceback": traceback.format_exc(),
                           "facade_state": facade.state, "arm_state": facade.arm_state,
                           "last_error": facade.last_error, "follow_error": facade._follow_error}
        print(f"FAIL: {type(exc).__name__}: {exc}", flush=True)
    finally:
        if identity and recording.status_value in ("active", "invalid"):
            try:
                recording.stop_session({"reason": "offline_record_cleanup"})
            except Exception as exc:
                report["recording_cleanup_error"] = f"{type(exc).__name__}: {exc}"
        if identity and facade.session_id:
            try:
                facade.release_session("offline_record_finished", **identity)
            except Exception as exc:
                report["release_error"] = f"{type(exc).__name__}: {exc}"
        final = car_status()
        report["car_disarmed"] = not (final.get("control") or {}).get("active") and not (
            final.get("licence") or {}).get("frame_outputs_enabled")
        WORK.mkdir(parents=True, exist_ok=True)
        (WORK / "record-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 1 if report.get("error") or report.get("release_error") or not report["car_disarmed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
