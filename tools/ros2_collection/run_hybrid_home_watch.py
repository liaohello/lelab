#!/usr/bin/env python3
"""Start and monitor one full hybrid homing run while another process heartbeats."""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path


def request(base: str, path: str, body: dict | None = None,
            *, timeout: float = 8.0) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode())


def selected(task: dict) -> dict:
    return {
        key: task.get(key)
        for key in (
            "state", "scope", "current_joint", "step_target_raw",
            "observed_raw", "progress_pct", "feedback_age_s",
            "feedback_sample_seq", "last_error",
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:18287/ros-mobile")
    parser.add_argument("--session-file", required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--poll", type=float, default=0.25)
    parser.add_argument("--jsonl", default="")
    args = parser.parse_args()
    identity = json.loads(Path(args.session_file).read_text(encoding="utf-8"))

    result = request(args.base, "/hybrid/home", {
        "action": "start",
        **identity,
    })
    task = result.get("homing") or {}
    print("HOME_START " + json.dumps(selected(task), sort_keys=True), flush=True)
    deadline = time.monotonic() + args.timeout
    output = None
    try:
        if args.jsonl:
            output = Path(args.jsonl).open("a", encoding="utf-8")
        while time.monotonic() < deadline:
            status = request(args.base, "/hybrid/status")
            task = status.get("homing") or {}
            row = selected(task)
            line = json.dumps(row, sort_keys=True)
            print("HOME " + line, flush=True)
            if output is not None:
                output.write(line + "\n")
                output.flush()
            if task.get("state") == "complete":
                print("HOME_COMPLETE", flush=True)
                return 0
            if task.get("state") in {"fault", "stopped", "cancelled"}:
                print("HOME_STOPPED " + str(task.get("last_error")), flush=True)
                return 1
            time.sleep(args.poll)

        request(args.base, "/hybrid/home", {
            "action": "cancel",
            **identity,
        })
        print("HOME_TIMEOUT_CANCELLED", flush=True)
        return 2
    finally:
        if output is not None:
            output.close()


if __name__ == "__main__":
    raise SystemExit(main())
