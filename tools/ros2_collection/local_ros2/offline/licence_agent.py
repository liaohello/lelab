#!/usr/bin/env python3
"""OFFLINE ONLY - stands in for the car-side agent's licence decisions.

It issues exactly the licence mirror that the agent will publish once the bridge is
wired into `agent.py`: same schema, same heartbeat cadence, same revocation rules,
so the control process can be exercised before the full agent stack is running.

The generated token is a test value; nothing here contacts hardware, and revoking or
exiting is the fault injection, not an accident.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

CAR_PKG = os.environ.get("CAR_PKG", "/repo/ros2_ws/src/lelab_mobile_agent")
if CAR_PKG not in sys.path:
    sys.path.insert(0, CAR_PKG)

from lelab_mobile_agent.licence_ipc import LicencePublisher, licence_record  # noqa: E402
from lelab_mobile_agent.teleop_stream import token_digest  # noqa: E402

MS_TO_S = 0.001


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default="/out/state/licence.sock")
    parser.add_argument("--session-id", default="offline-session")
    parser.add_argument("--session-epoch", default="1")
    parser.add_argument("--bundle", default="0" * 64)
    parser.add_argument("--token", default="offline-lease-token")
    parser.add_argument("--permits", default="arm,gripper")
    parser.add_argument("--outputs", default="false")
    parser.add_argument("--ttl-ms", type=int, default=300)
    parser.add_argument("--heartbeat-ms", type=int, default=100)
    parser.add_argument("--duration-s", type=float, default=0.0)
    parser.add_argument("--revoke-at-s", type=float, default=0.0)
    parser.add_argument("--exit-at-s", type=float, default=0.0)
    parser.add_argument("--status-path", default="/out/state/licence_agent_status.json")
    parser.add_argument("--generation-file", default="/out/state/licence_generation.txt")
    args = parser.parse_args()

    outputs = str(args.outputs).lower() in ("1", "true", "yes")
    granted = {name.strip(): True for name in args.permits.split(",") if name.strip()}
    permits = {"arm": "arm" in granted, "gripper": "gripper" in granted, "base": False}
    token_sha = token_digest(args.token)
    # Generations only move forward, so an issuer restart is seen by the control
    # process as the agent re-authorising (which is what clears a latched stop)
    # rather than as a replayed older licence.
    counter_path = Path(args.generation_file)
    try:
        generation = int(counter_path.read_text().strip()) + 1
    except (FileNotFoundError, ValueError):
        generation = 1
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    counter_path.write_text(str(generation) + "\n", encoding="utf-8")
    publisher = LicencePublisher(args.socket)
    started = time.monotonic()
    revoked = False
    exited = False
    sent = 0
    last_error = None

    def snapshot(state: str) -> None:
        path = Path(args.status_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {"data_identity": "simulated_offline", "state": state,
                    "session_id": args.session_id, "session_epoch": args.session_epoch,
                    "calibration_bundle_hash": args.bundle, "permits": permits,
                    "outputs_enabled": outputs and not revoked,
                    "control_generation": generation, "ttl_ms": args.ttl_ms,
                    "heartbeat_ms": args.heartbeat_ms, "sent": sent,
                    "uptime_s": round(time.monotonic() - started, 2),
                    "last_error": last_error, "token_sha256": token_sha}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
        os.replace(temporary, path)

    def handle(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle)
    while True:
        elapsed = time.monotonic() - started
        if args.duration_s and elapsed >= args.duration_s:
            break
        if args.exit_at_s and elapsed >= args.exit_at_s:
            exited = True          # no revoke message at all: the socket just dies
            break
        if args.revoke_at_s and elapsed >= args.revoke_at_s and not revoked:
            revoked = True
            generation += 1
            if publisher.revoke("operator_revoked"):
                snapshot("revoked")
            break
        if not publisher._sock and not publisher.connect():
            last_error = "control_process_socket_unavailable"
            snapshot("connecting")
            time.sleep(args.heartbeat_ms * MS_TO_S)
            continue
        record = licence_record(control_generation=generation, session_id=args.session_id,
                                session_epoch=args.session_epoch,
                                calibration_bundle_hash=args.bundle, permits=permits,
                                outputs_enabled=outputs and not revoked,
                                token_sha256=token_sha, ttl_ms=args.ttl_ms)
        if publisher.publish(**{key: record[key] for key in
                                ("control_generation", "session_id", "session_epoch",
                                 "calibration_bundle_hash", "permits", "outputs_enabled",
                                 "token_sha256", "ttl_ms")}):
            sent += 1
            snapshot("issuing")
        else:
            last_error = "licence_publish_failed"
        time.sleep(args.heartbeat_ms * MS_TO_S)

    if not exited:
        publisher.close()
    snapshot("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
