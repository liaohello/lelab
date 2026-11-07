#!/usr/bin/env python3
"""OFFLINE ONLY: measure the real frame path, and say plainly what cannot be measured.

Three producers, one socket protocol, one car:

* ``facade_async`` - the production facade: its follow worker submits the newest frame
  and keeps going, which is what an operator actually runs;
* ``sync_frame`` - the same ``FrameLink`` and the same mapped sample, but one frame in
  flight: submit, wait for that frame's receipt, then submit the next;
* ``legacy_http`` - the production legacy factory. The offline profile has no real
  leader, board or serial device, so this is attempted and its concrete failure is
  recorded. It is never replaced by "frame plus ping" and never reported as a
  measurement of the old transport.

Timings are taken on one clock (this process' monotonic clock) and named after the
stage they actually bound: queued is the hand-off to the sender, written is after
``sendall`` returned, receipt_seen is when the reply arrived back here. The car's own
window age and publish intervals stay on the car's clock and are reported separately;
nothing here subtracts one machine's clock from another's, and a receipt is not proof
that a real servo moved.

Feedback freshness (0.5 s) and the frame TTL (200 ms) are the configured values. They
are not relaxed to make a rate target.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import threading
import time
import traceback

from facade_smoke import (AGENT, STATE, STREAM_PORT, SyntheticUpper, WORK, car_status,
                          calibration_assets_fixture, FrameCarControlLease,
                          HybridCalibrationStore, StreamHardwareObserver,
                          FrameStreamHardwareFacade, FrameStreamDriver,
                          configure_hybrid_facade)
from lelab.hybrid_hardware import build_hardware_facade

CAR_DOC = STATE / "car_control_status.json"
ARM_JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5")
MODES = ("facade_async", "sync_frame", "legacy_http")
TTL_MS = 200
FRESHNESS_S = 0.5


def read_doc(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def percentiles(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    def at(quantile: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(quantile * len(ordered)))], 3)
    return {"count": len(ordered), "p50_ms": at(0.50), "p95_ms": at(0.95),
            "p99_ms": at(0.99), "max_ms": round(ordered[-1], 3),
            "mean_ms": round(statistics.fmean(ordered), 3)}


def interval(values: list[list[int]]) -> dict:
    """Milliseconds between two columns of the same record, dropping absent pairs."""
    pairs = [row[1] - row[0] for row in values if row[0] and row[1] and row[1] >= row[0]]
    return percentiles([value / 1e6 for value in pairs])


class Instrumented:
    """Wrap one link's own send/receive calls and stamp each frame on this clock."""

    def __init__(self, link) -> None:
        self.link = link
        self.rows: list[dict] = []
        self._lock = threading.Lock()
        self._queued: dict[int, int] = {}
        self._written: dict[int, int] = {}

    def attach(self) -> None:
        client = self.link.client
        original_submit, original_send, original_note = (client.submit_frame, client.send,
                                                          client._note)

        def submit_frame(frame):
            seq = frame.get("frame_seq")
            with self._lock:
                self._queued[seq] = time.monotonic_ns()
            return original_submit(frame)

        def timed_send(message):
            started = time.monotonic_ns()
            result = original_send(message)
            if message.get("type") == "frame":
                seq = (message.get("frame_seq"),)[0]
                with self._lock:
                    self._written[seq] = time.monotonic_ns()
                    self.rows.append({"frame_seq": seq,
                                      "sample_seq": message.get("sample_seq"),
                                      "queued_ns": self._queued.pop(seq, 0),
                                      "written_ns": self._written.pop(seq, 0),
                                      "sendall_ms": round((time.monotonic_ns() - started)
                                                          / 1e6, 3),
                                      "receipt_seen_ns": 0, "state": None,
                                      "window_remaining_ms": None,
                                      "window_age_at_arrival_ms": None})
            return result

        def note(message):
            if message.get("type") == "receipt":
                arrived = time.monotonic_ns()
                with self._lock:
                    for row in reversed(self.rows):
                        if row["frame_seq"] == message.get("frame_seq") and not row["state"]:
                            row.update(receipt_seen_ns=arrived, state=message.get("state"),
                                       reason=message.get("reason"),
                                       window_remaining_ms=message.get("window_remaining_ms"),
                                       window_age_at_arrival_ms=message.get(
                                           "window_age_at_arrival_ms"))
                            break
            return original_note(message)

        client.submit_frame = submit_frame          # type: ignore[method-assign]
        client.send = timed_send                    # type: ignore[method-assign]
        client._note = note                         # type: ignore[method-assign]

    def summary(self, duration_s: float) -> dict:
        rows = self.rows
        return {
            "duration_s": round(duration_s, 2),
            "frames_offered": len(rows),
            "hz_offered": round(len(rows) / duration_s, 2) if duration_s else None,
            "receipts_seen": sum(1 for row in rows if row["state"]),
            "admitted": sum(1 for row in rows if row["state"] == "admitted"),
            "refused": sum(1 for row in rows if row["state"] == "refused"),
            "refusal_reasons": sorted({str(row.get("reason")) for row in rows
                                       if row["state"] == "refused"})[:8],
            "queued_to_written_ms": interval([[row["queued_ns"], row["written_ns"]]
                                              for row in rows]),
            "written_to_receipt_ms": interval([[row["written_ns"], row["receipt_seen_ns"]]
                                               for row in rows]),
            "queued_to_receipt_ms": interval([[row["queued_ns"], row["receipt_seen_ns"]]
                                              for row in rows]),
            "sendall_ms": percentiles([row["sendall_ms"] for row in rows]),
            "car_window_age_at_arrival_ms": percentiles(
                [row["window_age_at_arrival_ms"] for row in rows
                 if isinstance(row.get("window_age_at_arrival_ms"), (int, float))]),
            "car_window_remaining_ms": percentiles(
                [row["window_remaining_ms"] for row in rows
                 if isinstance(row.get("window_remaining_ms"), (int, float))]),
            "never_answered": sum(1 for row in rows if not row["state"]),
        }


class Bench:
    def __init__(self) -> None:
        self.report: dict = {"agent": AGENT, "ttl_ms": TTL_MS, "freshness_s": FRESHNESS_S,
                             "modes": {}, "data_identity": "simulated_offline",
                             "hardware_effect": "not_tested_simulated_board_only",
                             "checks": []}

    def build(self) -> None:
        status = car_status()
        if (status.get("control") or {}).get("active"):
            raise RuntimeError("offline_agent_already_has_a_lease")
        store = HybridCalibrationStore(WORK / "calibration")
        assets = calibration_assets_fixture(measured_at_utc="2026-11-04T00:00:00Z")
        ids = {kind: assets[kind]["identity"] for kind in ("leader", "follower", "mapping")}
        upper = SyntheticUpper(agent_url=AGENT, store=store, leader_identity=ids["leader"])
        car = FrameCarControlLease(AGENT,
                                   expected_profile="hardware_hybrid_manual_v1_offline6d")
        observer = StreamHardwareObserver(
            upper=upper, agent_url=AGENT, store=store, leader_identity=ids["leader"],
            follower_identity=ids["follower"], mapping_identity=ids["mapping"],
            simulated_source=True)
        car.observer = observer
        facade = FrameStreamHardwareFacade(upper=upper, car=car, observer=observer,
                                           calibration_store=store)
        driver = FrameStreamDriver(car, observer, AGENT, host="127.0.0.1", port=STREAM_PORT)
        facade.bind_driver(driver)
        facade.simulated_context = {"simulated": True, "data_identity": "simulated_offline",
                                    "training_eligible": False}
        self.facade = configure_hybrid_facade(facade=facade, replace=True)
        self.driver, self.observer, self.car = driver, observer, car

    def open(self) -> dict:
        acquired = self.facade.acquire_session({"owner": "offline-bench"})
        identity = {"session_id": acquired["session_id"],
                    "session_epoch": acquired["session_epoch"]}
        self.facade.preview(identity)
        for action in ("align", "arm", "engage"):
            self.facade.set_arm_state({"action": action, **identity})
        return identity

    def release(self) -> None:
        try:
            if self.facade.session_id:
                self.facade.release_session("offline_bench_finished",
                                            session_id=self.facade.session_id,
                                            session_epoch=self.facade.session_epoch)
        except Exception as exc:  # noqa: BLE001
            self.report["release_error"] = f"{type(exc).__name__}:{str(exc)[:200]}"

    # -- the two frame producers -------------------------------------------
    def run_async(self, duration_s: float) -> dict:
        identity = self.open()
        link = self.driver.link
        probe = Instrumented(link)
        probe.attach()
        car_before = read_doc(CAR_DOC).get("counters") or {}
        started = time.monotonic()
        time.sleep(duration_s)
        elapsed = time.monotonic() - started
        after = read_doc(CAR_DOC)
        counters = after.get("counters") or {}
        out = probe.summary(elapsed)
        out.update({
            "producer": "facade follow worker (newest frame wins, pipelined)",
            "facade_state": self.facade.state,
            "follow_error": getattr(self.facade, "_follow_error", None),
            "car_published_delta": (counters.get("frames_emitted", 0)
                                    - car_before.get("frames_emitted", 0)),
            "car_hz_published": round(((counters.get("frames_emitted", 0)
                                       - car_before.get("frames_emitted", 0)) / elapsed), 2),
            "car_effective_emit_hz": after.get("effective_emit_hz"),
            "car_emit_intervals_ms": after.get("emit_intervals"),
            "car_window_issues_delta": (counters.get("window_issues", 0)
                                        - car_before.get("window_issues", 0)),
            "car_hold_frames": counters.get("hold_frames"),
            "car_licences_lost": counters.get("licences_lost"),
        })
        out["raw_rows"] = probe.rows
        self.release()
        return out

    def run_sync(self, duration_s: float, cadence_s: float = 0.1) -> dict:
        """Same link, same mapped sample, one frame in flight at a time."""
        identity = self.open()
        # The facade's own worker would pipeline behind this loop and the comparison
        # would measure nothing; stop it, and say so in the report.
        self.facade._stop_follow_worker()
        worker_running = bool(getattr(self.facade, "_follow_thread", None)
                              and self.facade._follow_thread.is_alive())
        link = self.driver.link
        probe = Instrumented(link)
        probe.attach()
        car_before = read_doc(CAR_DOC).get("counters") or {}
        started = time.monotonic()
        sent = 0
        deadline = started + duration_s
        errors: list[str] = []
        while time.monotonic() < deadline:
            tick_started = time.monotonic()
            try:
                facts = self.observer.snapshot()
                preview = self.observer.map_sample(facts)
                targets = {item.get("joint"): item for item in preview.get("targets") or []}
                gripper = preview.get("gripper") or {}
                if preview.get("available") is not True or set(targets) != set(ARM_JOINTS):
                    errors.append(f"preview_unavailable:{preview.get('reason')}")
                else:
                    result = link.send_snapshot(
                        arm_positions=[float(targets[name]["target_rad"])
                                       for name in ARM_JOINTS],
                        gripper_rad=float(gripper["target_rad"]),
                        sample_seq=int((facts.get("leader") or {}).get("reads") or sent + 1),
                        leader_rad=[float(((facts.get("leader") or {})
                                           .get("positions_rad") or {}).get(name) or 0.0)
                                    for name in ("shoulder_pan", "shoulder_lift", "elbow_flex",
                                                 "wrist_flex", "wrist_roll")],
                        pre_clip_rad=[float(targets[name]["pre_clip_rad"])
                                      for name in ARM_JOINTS],
                        clutch=True)
                    if result.get("sent"):
                        sent += 1
                        wanted = result.get("frame_seq")
                        stop_waiting = time.monotonic() + 1.5
                        while time.monotonic() < stop_waiting:
                            if any(row["frame_seq"] == wanted and row["state"]
                                   for row in probe.rows):
                                break
                            # Stopping the follow worker also stops the thing that
                            # normally drains the socket, so this loop has to collect
                            # itself - otherwise the "reply delay" it measures is the
                            # drain cadence, not the link.
                            link.collect()
                            time.sleep(0.002)
            except Exception as exc:  # noqa: BLE001 - a refused frame is a finding
                errors.append(f"{type(exc).__name__}:{str(exc)[:160]}")
            time.sleep(max(0.0, cadence_s - (time.monotonic() - tick_started)))
        elapsed = time.monotonic() - started
        after = read_doc(CAR_DOC)
        counters = after.get("counters") or {}
        out = probe.summary(elapsed)
        out.update({
            "producer": "synchronous stop-and-wait on the same FrameLink",
            "facade_follow_worker_stopped": not worker_running,
            "facade_state": self.facade.state,
            "errors": errors[:6],
            "car_published_delta": (counters.get("frames_emitted", 0)
                                    - car_before.get("frames_emitted", 0)),
            "car_hz_published": round(((counters.get("frames_emitted", 0)
                                       - car_before.get("frames_emitted", 0)) / elapsed), 2),
            "car_effective_emit_hz": after.get("effective_emit_hz"),
            "car_emit_intervals_ms": after.get("emit_intervals"),
            "car_hold_frames": counters.get("hold_frames"),
        })
        out["raw_rows"] = probe.rows
        self.release()
        return out

    def run_legacy(self) -> dict:
        """Attempt the production legacy transport; record the concrete refusal."""
        os.environ["LELAB_HYBRID_TRANSPORT"] = "legacy_http"
        attempt: dict = {"transport": "legacy_http", "runnable_offline": False}
        try:
            facade = build_hardware_facade(AGENT)
            acquired = facade.acquire_session({"owner": "offline-bench-legacy"})
            attempt["acquired"] = {"session_id": acquired.get("session_id")}
            attempt["note"] = ("the legacy facade opened a session; see the engagement "
                               "result before calling anything measured")
            facade.release_session("offline_bench_legacy_finished", **{
                key: acquired[key] for key in ("session_id", "session_epoch")
                if key in acquired})
        except Exception as exc:  # noqa: BLE001 - the failure is the deliverable
            attempt["error"] = {"type": type(exc).__name__, "detail": str(exc)[:300]}
        finally:
            os.environ["LELAB_HYBRID_TRANSPORT"] = "frame_stream_v1"
        attempt["verdict"] = ("N/A - no legacy HTTP transport measurement exists for the "
                             "offline6d simulated profile"
                             if not attempt["runnable_offline"] else "attempted")
        return attempt

    # -- driver -------------------------------------------------------------
    def run(self, screen_s: float, compare_s: float, repeats: int) -> None:
        self.build()
        self.report["screen"] = {}
        self.report["screen"]["facade_async"] = self.run_async(screen_s)
        self.report["screen"]["sync_frame"] = self.run_sync(screen_s)
        self.report["screen"]["legacy_http"] = self.run_legacy()
        comparison: list[dict] = []
        producers = (("facade_async", self.run_async), ("sync_frame", self.run_sync))
        for round_number in range(1, repeats + 1):
            for mode, produce in producers:
                result = produce(compare_s)
                raw = result.pop("raw_rows", [])
                path = WORK / f"bench-{mode}-round{round_number}.jsonl"
                with path.open("w", encoding="utf-8") as handle:
                    for row in raw:
                        handle.write(json.dumps(row) + "\n")
                result["raw_file"] = str(path)
                result["round"] = round_number
                comparison.append(result)
            self.report["comparison"] = comparison
            self.write()
        async_rows = [row for row in comparison if row["producer"].startswith("facade")]
        sync_rows = [row for row in comparison if not row["producer"].startswith("facade")]
        self.report["comparison_summary"] = {
            "mode": ("facade_async vs sync_frame, same session inputs, same car, "
                     "TTL and freshness unchanged"),
            "facade_async_hz_published": [row["car_hz_published"] for row in async_rows],
            "sync_frame_hz_published": [row["car_hz_published"] for row in sync_rows],
            "facade_async_queued_to_receipt_p95_ms": [
                row["queued_to_receipt_ms"].get("p95_ms") for row in async_rows],
            "sync_frame_queued_to_receipt_p95_ms": [
                row["queued_to_receipt_ms"].get("p95_ms") for row in sync_rows],
            "note": ("published rate is counted on the car, reply delay on this host; "
                     "neither is a hardware measurement")}
        self.write()

    def write(self) -> None:
        WORK.mkdir(parents=True, exist_ok=True)
        (WORK / "bench-frame-path.json").write_text(
            json.dumps(self.report, indent=2, default=str) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screen-s", type=float, default=5.0)
    parser.add_argument("--compare-s", type=float, default=30.0)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    bench = Bench()
    try:
        bench.run(args.screen_s, args.compare_s, args.repeats)
    except Exception as exc:  # noqa: BLE001
        bench.report["error"] = {"type": type(exc).__name__, "detail": str(exc)[:400],
                                 "traceback": traceback.format_exc()[-2000:]}
        bench.write()
        print(f"ERROR {type(exc).__name__}: {exc}", flush=True)
        return 1
    if args.out:
        pathlib.Path(args.out).write_text(
            (WORK / "bench-frame-path.json").read_text(), encoding="utf-8")
    print(f"report: {WORK / 'bench-frame-path.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
