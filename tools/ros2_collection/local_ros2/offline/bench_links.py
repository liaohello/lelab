#!/usr/bin/env python3
"""Compare the three control-link shapes against one live runtime.

Same simulated input, same car process, same board.  What changes is the link:

  sync_legacy   three synchronous exchanges per control cycle, which is the shape the
                upper computer has today (GET /status, then one request per channel);
  sync_frame    one synchronous exchange per cycle (one whole frame, one receipt);
  stream        one whole frame per cycle with asynchronous receipts and a bounded,
                latest-wins sender.

The synchronous modes measure how much of a cycle is consumed by waiting for a reply.
This is a transport comparison only: no hardware, and the production HTTP agent is not
part of it, so the legacy column is the request count replayed on this socket rather
than a measurement of the deployed /hybrid/apply route.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "app_ros_mobile"))

from lelab.teleop_frame_v1 import frame_v1  # noqa: E402
from lelab.teleop_stream import StreamClient, StreamError, read_message  # noqa: E402

ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"]
STATE = REPO / "work/ros2_collection/local_humble/state"


def read_state(name: str) -> dict:
    path = STATE / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "p99": None, "max": None, "count": 0}
    ordered = sorted(values)
    def at(fraction: float) -> float:
        return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]
    return {"p50": round(at(0.50), 2), "p95": round(at(0.95), 2),
            "p99": round(at(0.99), 2), "max": round(ordered[-1], 2), "count": len(ordered)}


def command(channel: str, seq: int, sample_seq: int, value: float) -> dict:
    if channel == "arm":
        target = {"joint_names": ARM_JOINTS, "positions_rad": [value] * 5}
    else:
        target = {"joint_name": "r_joint", "position_rad": value}
    return {"protocol_version": 1, "session_id": BENCH_SESSION, "session_epoch": "1",
            "channel": channel, "source": "so101_leader", "seq": seq,
            "sample_seq": sample_seq, "ttl_ms": 150,
            "calibration_bundle_hash": BUNDLE, "target": target,
            "clutch": True, "clutch_label": "engaged"}


BUNDLE = ""


def build(frame_seq: int, window_id, stream_epoch, angle: float) -> dict:
    return frame_v1(session_id=BENCH_SESSION, session_epoch="1",
                    calibration_bundle_hash=BUNDLE, frame_seq=frame_seq,
                    stream_epoch=stream_epoch, sample_seq=frame_seq,
                    freshness_window_id=window_id,
                    commands={"arm": command("arm", frame_seq, frame_seq, angle),
                              "gripper": command("gripper", frame_seq, frame_seq, angle / 4)})


BENCH_SESSION = "offline-session"


def run_mode(mode: str, marker: dict, seconds: float, hz: float) -> dict:
    global BUNDLE
    BUNDLE = marker["calibration_bundle_hash"]
    client = StreamClient(port=marker["stream_port"], timeout_s=1.0)
    welcome = client.connect(lease_token=marker["lease_token"], session_id=BENCH_SESSION,
                             session_epoch="1")
    client.start_writer()
    epoch = client.stream_epoch
    control_before = read_state("car_control_status.json")
    board_before = read_state("sim_board_owner.json").get("outputs_received", 0)
    started = time.monotonic()
    deadline = started + seconds
    period = 1.0 / hz
    rtt_ms: list[float] = []
    attempts = 0
    admitted = 0
    refused: dict[str, int] = {}
    tick = 0
    frame_seq = 10_000 if mode != "stream" else 1
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        target_time = started + tick * period
        tick += 1
        if now < target_time:
            time.sleep(target_time - now)
            now = time.monotonic()
        window = client.current_window
        if not window or window.get("window_remaining_ms") is None:
            client.pump(0.02)
            continue
        attempts += 1
        angle = 0.05 * (frame_seq % 20)
        if mode == "stream":
            client.submit_frame(build(frame_seq, window["window_id"], epoch, angle))
            frame_seq += 1
            client.pump(0.0)
            for receipt in client.drain_receipts():
                if receipt.get("state") == "admitted":
                    admitted += 1
                    sent_at = receipt.pop("_sent_at", None)
                    if sent_at:
                        rtt_ms.append((time.monotonic() - sent_at) * 1000)
                else:
                    refused[str(receipt.get("reason"))] = \
                        refused.get(str(receipt.get("reason")), 0) + 1
            continue
        # Synchronous modes pay for the reply before the next cycle starts.
        sent_at = time.monotonic()
        client.send({"type": "frame", **build(frame_seq, window["window_id"], epoch, angle)})
        frame_seq += 1
        reply = _await(client, timeout=0.5)
        rtt_ms.append((time.monotonic() - sent_at) * 1000)
        if reply and reply.get("state") == "admitted":
            admitted += 1
        elif reply:
            refused[str(reply.get("reason"))] = refused.get(str(reply.get("reason")), 0) + 1
        if mode == "sync_legacy":
            # the two channels used to be separate requests, plus a status probe
            for _ in range(2):
                probe_at = time.monotonic()
                client.send({"type": "ping"})
                _await_type(client, "pong", timeout=0.5)
                rtt_ms.append((time.monotonic() - probe_at) * 1000)
    elapsed = time.monotonic() - started
    client.close()

    control_after = read_state("car_control_status.json")
    board_after = read_state("sim_board_owner.json").get("outputs_received", 0)
    emitted_delta = (control_after.get("counters", {}).get("frames_emitted", 0)
                     - control_before.get("counters", {}).get("frames_emitted", 0))
    return {
        "mode": mode,
        "seconds": round(elapsed, 2),
        "cycles_attempted": attempts,
        "admitted": admitted,
        "refusals": refused,
        "effective_new_frame_hz": round(admitted / max(0.001, elapsed), 2),
        "emitted_frames": emitted_delta,
        "effective_emit_hz": round(emitted_delta / max(0.001, elapsed), 2),
        "board_messages": board_after - board_before,
        "round_trip_ms": percentiles(rtt_ms),
        "emit_intervals_ms": control_after.get("emit_intervals", {}),
        "control_counters": control_after.get("counters", {}),
        "stream_counters": control_after.get("stream", {}).get("counters", {}),
        "sender_stats": client.stats(),
    }


def _await(client: StreamClient, timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            message = client.recv(0.2)
        except StreamError as error:
            if error.reason in ("read_timeout", "closed"):
                return None
            raise
        if message.get("type") == "receipt":
            return message
    return None


def _await_type(client: StreamClient, kind: str, timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            message = client.recv(0.2)
        except StreamError as error:
            if error.reason in ("read_timeout", "closed"):
                return None
            raise
        if message.get("type") == kind:
            return message
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--hz", type=float, default=10.0)
    parser.add_argument("--modes", default="sync_legacy,sync_frame,stream")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    marker = json.loads((REPO / "work/ros2_collection/local_humble/run/offline_stack.json")
                        .read_text(encoding="utf-8"))
    rounds = []
    for repeat in range(args.repeats):
        for mode in args.modes.split(","):
            print(f"[bench] repeat {repeat + 1} mode {mode} for {args.seconds}s", flush=True)
            result = run_mode(mode.strip(), marker, args.seconds, args.hz)
            result["repeat"] = repeat + 1
            rounds.append(result)
            print(json.dumps({k: result[k] for k in
                              ("mode", "effective_new_frame_hz", "effective_emit_hz",
                               "round_trip_ms", "admitted", "refusals")}, default=str), flush=True)
    document = {"data_identity": "simulated_offline", "training_eligible": False,
                "note": ("sync_* modes replay the request counts of the current upper "
                         "computer on this socket; the production HTTP agent is not "
                         "running in this measurement"),
                "target_hz": args.hz, "seconds_per_round": args.seconds,
                "repeats": args.repeats, "rounds": rounds,
                "summary_by_mode": {}}
    for mode in args.modes.split(","):
        selected = [round_item for round_item in rounds if round_item["mode"] == mode.strip()]
        if not selected:
            continue
        document["summary_by_mode"][mode.strip()] = {
            "rounds": len(selected),
            "effective_new_frame_hz": [item["effective_new_frame_hz"] for item in selected],
            "effective_emit_hz": [item["effective_emit_hz"] for item in selected],
            "round_trip_p95_ms": [item["round_trip_ms"]["p95"] for item in selected],
            "refusals": {key: sum(item["refusals"].get(key, 0) for item in selected)
                         for key in sorted({k for item in selected for k in item["refusals"]})},
        }
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                                     encoding="utf-8")
    print(json.dumps(document["summary_by_mode"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
