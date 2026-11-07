#!/usr/bin/env python3
"""OFFLINE ONLY: does a real bag close still starve the lease and status path?

The leader and board are simulated; the agent must be the isolated offline profile.

A recording stop finalises the bag and hashes the session while holding the recorder
lock. ``/status`` used to wait behind that lock, which made a healthy lease read as
``lease_lost`` and kept ``/hybrid/stop`` waiting behind it. This measures the read
latency and the renewals across a real close, and counts the polls the agent answered
from its cached recording snapshot: a cache-served poll is direct proof that the
recorder lock was held while ``/status`` still answered.

Nothing here is physical stop evidence, and no number here is a hardware measurement.
"""
from __future__ import annotations

import json
import threading
import time
import traceback

from facade_smoke import (AGENT, STREAM_PORT, WORK, SyntheticUpper, car_status,
                          HybridCalibrationStore, calibration_assets_fixture,
                          FrameCarControlLease, StreamHardwareObserver,
                          FrameStreamHardwareFacade, FrameStreamDriver,
                          configure_hybrid_facade)
from lelab.hybrid_hardware import HardwareCompositionError, _http_json
from lelab.hybrid_recording import HttpCarClient, HybridRecordingController

# The freshness window the facade itself applies. Reporting against it is not a
# request to widen it; it is the number a lease check is already judged on.
FRESHNESS_S = 0.5
POLL_HZ = 50.0


def summarise(samples: list[float]) -> dict:
    if not samples:
        return {"count": 0}
    ordered = sorted(samples)
    def at(quantile: float) -> float:
        return ordered[min(len(ordered) - 1, int(quantile * len(ordered)))]
    return {"count": len(ordered), "p50_s": round(at(0.50), 4), "p95_s": round(at(0.95), 4),
            "max_s": round(ordered[-1], 4),
            "over_freshness_s": sum(1 for value in ordered if value > FRESHNESS_S)}


class StatusPoller(threading.Thread):
    """Read /status on a fixed cadence and keep every elapsed time."""

    def __init__(self, interval_s: float = 1.0 / POLL_HZ) -> None:
        super().__init__(name="status-poller", daemon=True)
        self.interval_s = interval_s
        self.samples: list[float] = []
        self.cache_served = 0
        self.errors: list[str] = []
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            started = time.monotonic()
            try:
                status = _http_json(AGENT, "/status", timeout=FRESHNESS_S)
            except HardwareCompositionError as exc:
                self.errors.append(str(exc)[:160])
            else:
                elapsed = time.monotonic() - started
                self.samples.append(elapsed)
                if (status.get("recording") or {}).get("recording_status_cached") is True:
                    self.cache_served += 1
            self._halt.wait(max(0.0, self.interval_s - (time.monotonic() - started)))

    def stop(self) -> list[float]:
        self._halt.set()
        self.join(3.0)
        return list(self.samples)


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
    facade = FrameStreamHardwareFacade(upper=upper, car=car, observer=observer,
                                       calibration_store=store)
    facade.bind_driver(FrameStreamDriver(car, observer, AGENT, host="127.0.0.1", port=STREAM_PORT))
    facade.simulated_context = {"simulated": True, "data_identity": "simulated_offline",
                                "training_eligible": False}
    facade = configure_hybrid_facade(facade=facade, replace=True)
    client = HttpCarClient(lambda path, body: _http_json(
        AGENT, path, method="POST", body=body, token=car.token, timeout=10.0))
    recording = HybridRecordingController(facade, client, offline_6d=True)
    facade.bind_recording_controller(recording, replace=True)

    report: dict = {"data_identity": "simulated_offline", "training_eligible": False,
                    "freshness_window_s": FRESHNESS_S, "checks": [], "phases": {}}

    def check(name: str, condition, detail=None) -> None:
        report["checks"].append({"name": name, "ok": bool(condition), "detail": detail})
        print(f"{'PASS' if condition else 'FAIL'} {name}: {detail}", flush=True)
        if not condition:
            raise RuntimeError(name)

    identity = None
    poller = None
    try:
        acquired = facade.acquire_session({"owner": "offline-convoy"})
        identity = {"session_id": acquired["session_id"], "session_epoch": acquired["session_epoch"]}

        baseline = StatusPoller()
        baseline.start()
        time.sleep(2.0)
        report["phases"]["idle"] = summarise(baseline.stop())
        check("idle_status_under_freshness",
              report["phases"]["idle"].get("over_freshness_s") == 0, report["phases"]["idle"])

        facade.preview(identity)
        for action in ("align", "arm", "engage"):
            facade.set_arm_state({"action": action, **identity})
        started = recording.dispatch("start_session", {**identity,
            "profile": "hardware_hybrid_manual_v1_offline6d", "task": "convoy probe"})
        report["recording_id"] = started.get("car_recording_id")
        check("recording_started", bool(report["recording_id"]), report["recording_id"])
        recording.dispatch("start_episode", {**identity, "episode_id": "convoy_ep_1"})
        recording.dispatch("set_phase", {**identity, "phase": "observe"})
        time.sleep(1.5)
        frames = facade._follow_commands
        check("frames_followed", frames > 0, {"frames": frames, "state": facade.state,
                                             "error": facade.last_error})

        recording.dispatch("end_episode", {**identity, "success": "unknown"})

        # The measured window: a real bag finalisation and session hashing, with the
        # lease being renewed on the same cadence the operator page uses.
        poller = StatusPoller()
        poller.start()
        renewals = {"attempted": 0, "ok": 0, "errors": []}

        def renew_while_stopping() -> None:
            for _ in range(4):
                renewals["attempted"] += 1
                try:
                    facade.heartbeat()
                except Exception as exc:  # noqa: BLE001 - the failure is the finding
                    renewals["errors"].append(f"{type(exc).__name__}:{str(exc)[:160]}")
                else:
                    renewals["ok"] += 1
                time.sleep(0.4)

        renew_thread = threading.Thread(target=renew_while_stopping, daemon=True)
        renew_started = time.monotonic()
        renew_thread.start()
        stopped = recording.dispatch("stop_session", {**identity, "reason": "offline_convoy"})
        renew_thread.join(20.0)
        close_s = round(time.monotonic() - renew_started, 3)
        report["phases"]["stop_session"] = {
            "close_duration_s": close_s, "summary": summarise(poller.stop()),
            "cache_served_polls": poller.cache_served,
            "poll_errors": poller.errors[:5], "renewals": renewals}
        measured = report["phases"]["stop_session"]["summary"]
        # A poll that timed out never lands in the latency samples, so it has to be
        # counted as a violation instead of quietly disappearing from the distribution.
        check("no_status_poll_timed_out_during_close", not poller.errors, poller.errors[:5])
        check("status_under_close_under_freshness",
              measured.get("over_freshness_s") == 0 and measured.get("max_s", 9) < FRESHNESS_S,
              measured)
        check("renewals_during_close_all_succeeded",
              renewals["attempted"] > 0 and renewals["ok"] == renewals["attempted"], renewals)
        check("locked_window_was_observed", poller.cache_served > 0,
              {"cache_served_polls": poller.cache_served,
               "detail": "0 means the close never held the lock when a poll arrived; "
                         "that run proves nothing about the convoy"})
        check("no_lease_lost_during_close", facade.state == "ready" and not facade.last_error,
              {"state": facade.state, "last_error": facade.last_error,
               "car_reason": car.last_reason, "follow_error": facade._follow_error})
        report["diagnostics"] = facade.status()["diagnostics"]
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "detail": str(exc),
                           "traceback": traceback.format_exc(), "facade_state": facade.state,
                           "last_error": facade.last_error, "car_reason": car.last_reason}
        try:
            report["error"]["diagnostics"] = facade.status().get("diagnostics")
        except Exception as status_exc:  # noqa: BLE001 - the first failure is the finding
            report["error"]["diagnostics_error"] = f"{type(status_exc).__name__}:{status_exc}"
        print(f"FAIL: {type(exc).__name__}: {exc}", flush=True)
    finally:
        if poller is not None:
            poller.stop()
        if identity and recording.status_value in ("active", "invalid"):
            try:
                recording.stop_session({**identity, "reason": "offline_convoy_cleanup"})
            except Exception as exc:  # noqa: BLE001
                report["recording_cleanup_error"] = f"{type(exc).__name__}:{exc}"
        if identity and facade.session_id:
            try:
                facade.release_session("offline_convoy_finished", **identity)
            except Exception as exc:  # noqa: BLE001
                report["release_error"] = f"{type(exc).__name__}:{exc}"
        final = car_status()
        report["car_disarmed"] = not (final.get("control") or {}).get("active") and not (
            (final.get("licence") or {}).get("frame_outputs_enabled"))
        report["failed"] = [entry["name"] for entry in report["checks"] if not entry["ok"]]
        WORK.mkdir(parents=True, exist_ok=True)
        (WORK / "status-convoy.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"report: {WORK / 'status-convoy.json'}", flush=True)
    return 1 if (report.get("error") or report.get("release_error")
                 or report.get("failed") or not report.get("car_disarmed")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
