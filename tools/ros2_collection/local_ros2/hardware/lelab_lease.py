#!/usr/bin/env python3
"""Car-side control-lease helper for the LeLab chassis trial.

HTTP only: it never touches ROS, the serial port or the motors.  It exists so the
bring-up script does not have to repeat the acquire / begin_epoch / renew dance in
inline shell python.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:18110"


def _post(base, path, body, token=None, timeout=5.0):
    headers = {"content-type": "application/json"}
    if token:
        headers["X-LeLab-Control-Token"] = token
    request = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"{path} -> HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(f"{path} -> unreachable: {exc}")


def command_acquire(args):
    reply = _post(args.base, "/control/acquire",
                  {"client_id": args.client_id, "ttl_s": args.ttl})
    token = reply.get("token") or reply.get("control_token")
    if not token:
        raise SystemExit(f"acquire returned no token: {reply}")
    if args.token_file:
        with open(args.token_file, "w", encoding="utf-8") as handle:
            handle.write(token)
    print(f"acquire ok client={reply.get('client_id', args.client_id)} ttl_s={args.ttl}")
    if args.channels:
        begin = _post(args.base, "/hybrid/begin_epoch",
                      {"session_id": args.session_id,
                       "session_epoch": args.epoch or int(time.time()),
                       "calibration_bundle_hash": args.bundle,
                       "channels": args.channels.split(",")}, token)
        print(f"epoch ok session={args.session_id} channels={args.channels} "
              f"reply={json.dumps(begin, sort_keys=True)[:160]}")
    print(f"token={token}")


def command_renew(args):
    with open(args.token_file, encoding="utf-8") as handle:
        token = handle.read().strip()
    deadline = time.time() + args.seconds
    ok = fail = 0
    while time.time() < deadline:
        if args.stop_file and os.path.exists(args.stop_file):
            print("renew stop file present", flush=True)
            break
        try:
            _post(args.base, "/control/renew", {}, token, timeout=3.0)
            ok += 1
        except SystemExit as exc:
            fail += 1
            print(f"renew failed: {exc}", flush=True)
        time.sleep(args.interval)
    print(f"renew loop done ok={ok} fail={fail}", flush=True)


def command_status(args):
    try:
        with open(args.status_path, encoding="utf-8") as handle:
            status = json.load(handle)
    except (OSError, ValueError):
        print(f"no readable status at {args.status_path}")
        return
    base = status.get("base_gamepad") or {}
    print(f"outputs_enabled={status.get('outputs_enabled')} "
          f"paused={status.get('paused')} fault={status.get('latched_fault')}")
    print(f"base permitted={base.get('permitted')} armed={base.get('armed')} "
          f"reason={base.get('reason')} target={base.get('target_si')} "
          f"wheels={base.get('wheel_target_rps')} input_age={base.get('input_age_s')}")
    print(f"session={status.get('session_id')} bundle={str(status.get('calibration_bundle_hash'))[:12]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("LELAB_AGENT_URL", DEFAULT_BASE))
    sub = parser.add_subparsers(dest="command", required=True)

    acquire = sub.add_parser("acquire")
    acquire.add_argument("--client-id", default="lelab-chain-up")
    acquire.add_argument("--ttl", type=int, default=1800)
    acquire.add_argument("--token-file", default="/tmp/lelab_trial/token.txt")
    acquire.add_argument("--channels", default="base")
    acquire.add_argument("--session-id", default=None)
    acquire.add_argument("--epoch", type=int, default=None)
    acquire.add_argument("--bundle", default=None)
    acquire.set_defaults(func=command_acquire)

    renew = sub.add_parser("renew")
    renew.add_argument("--token-file", default="/tmp/lelab_trial/token.txt")
    renew.add_argument("--seconds", type=int, default=1800)
    renew.add_argument("--interval", type=float, default=10.0)
    renew.add_argument("--stop-file", default="/tmp/lelab_trial/stop_renew")
    renew.set_defaults(func=command_renew)

    status = sub.add_parser("status")
    status.add_argument("--status-path", default="/tmp/lelab_trial/car_control_status.json")
    status.set_defaults(func=command_status)

    args = parser.parse_args()
    if args.command == "acquire":
        if not args.session_id:
            args.session_id = f"trial-{time.strftime('%Y%m%dT%H%M%S')}"
        if not args.bundle:
            parser.error("acquire --channels needs --bundle")
    args.func(args)


if __name__ == "__main__":
    main()
