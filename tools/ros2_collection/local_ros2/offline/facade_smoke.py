#!/usr/bin/env python3
"""OFFLINE ONLY: exercise the production facade against the isolated Docker agent.

The leader and board are simulated. The agent must explicitly identify itself as
the isolated offline profile; this refuses hardware and does not open serial ports.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import sys
import time
import traceback
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "app_ros_mobile"))

from lelab.hybrid_calibration import HybridCalibrationStore  # noqa: E402
from lelab.hybrid_fixtures import calibration_assets_fixture  # noqa: E402
from lelab.hybrid_hardware import UpperLeaderLease  # noqa: E402
from lelab.hybrid_stream_hardware import (  # noqa: E402
    FrameCarControlLease, FrameStreamDriver, FrameStreamHardwareFacade, StreamHardwareObserver,
)
from lelab.mobile_teleop_coordinator import configure_hybrid_facade  # noqa: E402

AGENT = "http://127.0.0.1:" + os.environ.get("LROS_DOCKER_AGENT_PORT", "18130")
STREAM_PORT = int(os.environ.get("LROS_DOCKER_STREAM_PORT", "18122"))
RUNTIME = pathlib.Path(os.environ.get("LROS_WORK_DIR", str(ROOT / "work/ros2_collection/local_humble")))
WORK = RUNTIME / "facade-smoke"
STATE = RUNTIME / "docker/state"


def car_status():
    with urllib.request.urlopen(AGENT + "/status", timeout=3) as response:
        status = json.load(response)
    if status.get("profile") != "hardware_hybrid_manual_v1_offline6d" or status.get("simulated") is not True:
        raise RuntimeError("refusing_non_simulated_agent")
    return status


class SyntheticReader:
    """Device-boundary substitute: no USB and no hardware serial import."""

    def __init__(self):
        self.started = time.monotonic()
        self.reads = 0

    def snapshot(self):
        self.reads += 1
        x = 0.02 * math.sin((time.monotonic() - self.started) * 0.7)
        return {"connected": True, "healthy": True, "age_s": 0.001, "reads": self.reads,
                "positions_rad": {"shoulder_pan": x, "shoulder_lift": x, "elbow_flex": x,
                                  "wrist_flex": x, "wrist_roll": x},
                "gripper_percent": 50.0 + x, "source": "simulated_leader"}

    def close(self):
        return {"closed": True}


class SyntheticUpper(UpperLeaderLease):
    def _open_reader(self):
        return SyntheticReader()


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    store = HybridCalibrationStore(WORK / "calibration")
    assets = calibration_assets_fixture(measured_at_utc="2026-11-04T00:00:00Z")
    ids = {}
    for kind in ("leader", "follower", "mapping"):
        ids[kind] = assets[kind]["identity"]
        try:
            existing = store.load(kind, ids[kind]).data
        except FileNotFoundError:
            store.save(kind, ids[kind], assets[kind])
        else:
            if any(existing.get(key) != value for key, value in assets[kind].items()
                   if key not in {"version", "content_sha256"}):
                raise RuntimeError(f"simulated_calibration_changed:{kind}")
    upper = SyntheticUpper(agent_url=AGENT, store=store, leader_identity=ids["leader"])
    car = FrameCarControlLease(AGENT, expected_profile="hardware_hybrid_manual_v1_offline6d")
    observer = StreamHardwareObserver(upper=upper, agent_url=AGENT, store=store,
        leader_identity=ids["leader"], follower_identity=ids["follower"],
        mapping_identity=ids["mapping"], simulated_source=True)
    if sys.argv[1:] == ["--prepare"]:
        print(observer.calibration_readiness()["bundle_hash"])
        return 0
    initial = car_status()
    if (initial.get("control") or {}).get("active"):
        raise RuntimeError("offline_agent_already_has_a_lease")
    car.observer = observer
    facade = FrameStreamHardwareFacade(upper=upper, car=car, observer=observer, calibration_store=store)
    facade.bind_driver(FrameStreamDriver(car, observer, AGENT, host="127.0.0.1", port=STREAM_PORT))
    facade.simulated_context = {"simulated": True, "data_identity": "simulated_offline",
                                "training_eligible": False}
    facade = configure_hybrid_facade(facade=facade, replace=True)
    report = {"data_identity": "simulated_offline", "training_eligible": False,
              "agent_profile": initial["profile"], "checks": []}

    def check(name, ok, detail=None):
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}", flush=True)

    def board_count():
        p = STATE / "sim_board_owner.json"
        return json.loads(p.read_text()).get("outputs_received", 0)

    identity = None
    try:
        baseline = board_count()
        acquired = facade.acquire_session({"owner": "offline-production-facade"})
        identity = {"session_id": acquired["session_id"], "session_epoch": acquired["session_epoch"]}
        facade.preview(identity)
        time.sleep(0.25)
        check("preview_has_no_output", board_count() == baseline, {"before": baseline, "after": board_count()})
        for action in ("align", "arm", "engage"):
            facade.set_arm_state({"action": action, **identity})
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and facade._follow_commands < 8 and facade.state == "ready":
            time.sleep(0.05)
        follow = facade.status()
        link = follow.get("frame_link") or {}
        admitted = (link.get("counts") or {}).get("admitted", 0)
        check("facade_submitted_new_frames", facade._follow_commands >= 2,
              {"submitted": facade._follow_commands, "state": facade.state, "error": facade.last_error})
        check("production_agent_admitted", admitted >= 1, link.get("counts"))
        check("simulated_board_received", board_count() > baseline, {"before": baseline, "after": board_count()})
        facade.set_arm_state({"action": "release_clutch", **identity})
        time.sleep(0.3)
        stopped = board_count()
        time.sleep(0.3)
        check("no_new_targets_after_pause", board_count() == stopped, {"at_pause": stopped, "after": board_count()})
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "detail": str(exc),
                           "traceback": traceback.format_exc()}
        check("unhandled_exception", False, str(exc))
    finally:
        if identity is not None and facade.session_id is not None:
            try:
                facade.release_session("offline_smoke_finished", **identity)
            except Exception as exc:
                report["release_error"] = str(exc)
        final = car_status()
        check("car_disarmed", not (final.get("control") or {}).get("active") and
              not (final.get("licence") or {}).get("frame_outputs_enabled"))
        WORK.joinpath("report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return 0 if report["checks"] and all(item["ok"] for item in report["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
