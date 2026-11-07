#!/usr/bin/env python3
"""Drive a production car-agent session over HTTP for the offline closed loop.

This is the piece that replaces the offline licence fixture: every permission change
goes through the agent's own routes (`/control/acquire`, `/hybrid/begin_epoch`,
`/hybrid/enable_output`, `/hybrid/stop`, `/control/release`, `/control/estop`,
`/control/clear_estop`), so what the control process obeys is the same authority the
car would obey.  The lease token is read from the agent's response and written only
into a 0600 marker file; it is never printed.

SIMULATED ONLY: the devices behind the agent are simulated and the profile pins
`data_identity=simulated_offline`.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

MARKER_KEYS = ("stream_port", "session_id", "session_epoch", "calibration_bundle_hash",
               "lease_token", "agent_url")


def call(agent_url: str, path: str, body: dict | None = None, token: str | None = None,
         timeout: float = 5.0) -> dict:
    """One agent request.  Raises with the agent's own reason string on a refusal."""
    request = urllib.request.Request(agent_url + path, method="POST" if body is not None else "GET")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-LeLab-Control-Token", token)
    data = None if body is None else json.dumps(body).encode("utf-8")
    try:
        with urllib.request.urlopen(request, data=data, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            detail = {"raw": str(error)}
        raise RuntimeError(f"{path} -> HTTP {error.code}: "
                           f"{json.dumps(detail, default=str)[:400]}") from error
    return json.loads(payload) if payload.strip().startswith("{") else {"raw": payload}


def write_marker(path: pathlib.Path, marker: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)          # the lease token must not be world-readable
    temporary.replace(path)


def wait_for_feedback(agent_url: str, joints: int = 6, timeout_s: float = 8.0) -> dict:
    """Wait until the car reports a complete readback, the way an operator previews first.

    Enabling output is refused unless six valid readbacks are in hand.  Right after a
    stack start the board has not produced a full scan yet, so arming immediately would
    fail for a reason that has nothing to do with what is being tested.
    """
    deadline = time.monotonic() + timeout_s
    last = {}
    while time.monotonic() < deadline:
        status = call(agent_url, "/status")
        feedback = (status.get("control") or {}).get("follower_feedback") or {}
        last = {"available": feedback.get("available"),
                "samples": len(feedback.get("joint_samples") or {}),
                "reason": feedback.get("reason")}
        if feedback.get("available") and len(feedback.get("joint_samples") or {}) >= joints:
            last["waited_s"] = round(timeout_s - (deadline - time.monotonic()), 2)
            return last
        time.sleep(0.25)
    last["timeout_s"] = timeout_s
    return last


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-url", required=True)
    parser.add_argument("--stream-port", type=int, required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--bundle", default="")
    parser.add_argument("--channels", default="arm,gripper")
    # The client id identifies the holder of the lease; re-acquiring under a different
    # name is a refusal, not a retry, so it is read from the marker when present.
    parser.add_argument("--client-id", default="")
    parser.add_argument("--session-id", default="",
                        help="recording session id; defaults to offline-<client-id>")
    parser.add_argument("--ttl-s", type=float, default=300.0)
    parser.add_argument("--control-status", default="",
                        help="the control process status file, to detect a latched stop")
    parser.add_argument("--action", default="start",
                        choices=["start", "enable", "stop", "release", "estop", "clear",
                                 "status", "renew", "assert-legacy-refused",
                                 "assert-single-publisher"])
    parser.add_argument("--expect-refusal", default="", help="route expected to answer 409")
    args = parser.parse_args()
    marker_path = pathlib.Path(args.marker)
    marker = json.loads(marker_path.read_text()) if marker_path.exists() else {}
    client_id = args.client_id or marker.get("client_id") or "offline-acceptance"
    session_id = args.session_id or marker.get("session_id") or f"offline-{client_id}"

    if args.action == "status":
        print(json.dumps(call(args.agent_url, "/status"), sort_keys=True)[:600])
        return 0

    def acquire_lease():
        """A fresh or continued lease.  Refuses while an emergency stop is latched."""
        return call(args.agent_url, "/control/acquire",
                    {"client_id": client_id, "ttl_s": args.ttl_s})

    if args.action in ("start", "enable"):
        acquired = acquire_lease()
        token = acquired["token"]
    else:
        acquired = {}
        # Recovery and teardown must work from the recorded token: acquiring is itself
        # refused while a stop is latched, and clearing the stop is exactly what an
        # operator needs to do at that moment.
        token = marker.get("lease_token") or acquire_lease()["token"]

    if args.action == "assert-legacy-refused":
        failures = []
        routes = [r for r in (args.expect_refusal or "/hybrid/apply").split(",") if r]
        for route in routes:
            body = {"command": {}} if route == "/hybrid/apply" else {
                "positions": [0.0] * 5}
            try:
                call(args.agent_url, route, body, token=token)
                failures.append(f"{route}: ACCEPTED")
            except RuntimeError as error:
                text = str(error)
                if "frame_transport_active" in text:
                    print(f"refused as expected: {route} -> "
                          f"{text[text.index('frame_transport_active'):][:80]}")
                else:
                    failures.append(f"{route}: wrong reason {text[:120]}")
        if failures:
            print("FAIL: " + " | ".join(failures), file=sys.stderr)
            return 1
        return 0

    if args.action in ("start", "enable"):
        summary_extra = {}
        # A stop latched by an earlier run stays latched - that is the point of latching -
        # so re-arming has to begin with the explicit clearance, which carries no
        # permission and still needs the fresh epoch binding and enable step below.
        if args.control_status:
            try:
                control = json.loads(pathlib.Path(args.control_status).read_text())
            except (OSError, ValueError):
                control = {}
            if control.get("latched_fault"):
                cleared = call(args.agent_url, "/control/clear_estop", {"token": token}, token=token)
                summary_extra["cleared_latched_stop"] = {
                    "was": control.get("latched_fault"), "agent": cleared}
                time.sleep(0.4)              # let the clearance reach the mirror
        bundle = args.bundle or marker.get("calibration_bundle_hash") or ("a" * 64)
        epoch = int(acquired.get("session_epoch") or 0)
        epoch = epoch if 0 <= epoch < 2 ** 53 else 1
        session = {"session_id": session_id, "session_epoch": epoch,
                   "calibration_bundle_hash": bundle}
        begun = call(args.agent_url, "/hybrid/begin_epoch",
                     {**session, "channels": [c for c in args.channels.split(",") if c]},
                     token=token)
        marker.update({"stream_port": args.stream_port, "agent_url": args.agent_url,
                       "lease_token": token, "session_id": session["session_id"],
                       "session_epoch": session["session_epoch"],
                       "client_id": client_id,
                       "calibration_bundle_hash": bundle,
                       "data_identity": "simulated_offline", "training_eligible": False})
        write_marker(marker_path, marker)
        summary = {**summary_extra, "begun": {key: begun.get(key) for key in
                            ("session_id", "session_epoch", "channels", "outputs_enabled",
                             "driver_bound", "simulated")},
                   "lease": {"session_epoch": acquired.get("session_epoch"),
                             "expires_in_s": acquired.get("expires_in_s")},
                   "marker": str(marker_path)}
        if args.action == "enable":
            summary["feedback_ready"] = wait_for_feedback(args.agent_url)
            try:
                enabled = call(args.agent_url, "/hybrid/enable_output", session, token=token)
                summary["enabled"] = {key: enabled.get(key) for key in
                                      ("enabled", "outputs_enabled", "emitter",
                                       "frame_stream_connected", "simulated")}
            except RuntimeError as error:
                summary["enable_refused"] = str(error)[:200]
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
        return 0

    if args.action == "renew":
        # The upper computer renews periodically in production; the offline harness has
        # to hold the lease the same way, or the mirror would expire mid-phase.
        print(json.dumps(call(args.agent_url, "/control/renew", {"token": token}, token=token),
                         sort_keys=True))
        return 0

    if args.action in ("stop", "estop", "clear", "release"):
        session = {"session_id": marker.get("session_id"),
                   "session_epoch": marker.get("session_epoch"),
                   "calibration_bundle_hash": marker.get("calibration_bundle_hash")}
        outcome = {}
        if args.action in ("stop", "release"):
            try:
                outcome["hybrid_stop"] = call(args.agent_url, "/hybrid/stop",
                                              {**session, "reason": f"offline_{args.action}"},
                                              token=token)
            except RuntimeError as error:
                outcome["hybrid_stop_error"] = str(error)[:160]
        if args.action == "estop":
            outcome["estop"] = call(args.agent_url, "/control/estop", {"reason": "offline_rehearsal"},
                                    token=token)
        if args.action == "clear":
            outcome["clear_estop"] = call(args.agent_url, "/control/clear_estop", {"token": token},
                                          token=token)
        if args.action == "release":
            try:
                outcome["released"] = call(args.agent_url, "/control/release", {"token": token},
                                           token=token)
            except RuntimeError as error:
                outcome["release_error"] = str(error)[:160]
            marker.pop("lease_token", None)
            write_marker(marker_path, marker)
        print(json.dumps(outcome, indent=2, sort_keys=True, default=str)[:900])
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
