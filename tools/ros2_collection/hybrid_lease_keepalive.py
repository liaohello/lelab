#!/usr/bin/env python3
"""Acquire and keep one hybrid session alive for live hardware testing."""
from __future__ import annotations

import argparse
import json
import signal
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def request(base: str, path: str, body: dict | None = None,
            *, timeout: float = 5.0) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode())


def session_body(status: dict) -> dict:
    return {
        "session_id": status["session_id"],
        "session_epoch": status["session_epoch"],
        "calibration_bundle_hash": status["calibration_bundle_hash"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:18287/ros-mobile")
    parser.add_argument("--owner", default="codex-live-keepalive")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--session-file", required=True)
    args = parser.parse_args()

    stop = threading.Event()

    def on_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    request(args.base, "/hybrid/probe-leader", {})
    status = request(args.base, "/hybrid/status")
    if status.get("session_id"):
        raise RuntimeError("hybrid session already active")
    status = request(args.base, "/hybrid/session", {
        "action": "acquire",
        "owner": args.owner,
    }, timeout=15.0)
    if status.get("state") != "ready" or not status.get("session_id"):
        raise RuntimeError(f"session_not_ready:{status.get('state')}:{status.get('last_error')}")
    body = session_body(status)
    Path(args.session_file).write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("SESSION " + json.dumps(body, sort_keys=True), flush=True)

    failures = 0
    while not stop.wait(args.interval):
        try:
            out = request(args.base, "/hybrid/heartbeat", body, timeout=4.0)
            if out.get("state") not in (None, "ready"):
                raise RuntimeError(f"heartbeat_state:{out.get('state')}")
            failures = 0
            print(f"HEARTBEAT {time.time():.3f}", flush=True)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"HEARTBEAT_ERROR {failures} {type(exc).__name__}:{exc}", flush=True)
            if failures >= 3:
                print("HEARTBEAT_LOST", flush=True)
                return 2

    try:
        request(args.base, "/hybrid/session", {
            "action": "release",
            **body,
            "reason": "operator_keepalive_stopped",
        })
        print("RELEASED", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"RELEASE_ERROR {type(exc).__name__}:{exc}", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
