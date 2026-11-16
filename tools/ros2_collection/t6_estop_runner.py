#!/usr/bin/env python3
"""T6 static-hold fault test: acquire->preview->align->arm->engage (hold), then
operator estop -> clear estop -> re-engage, sampling pose/output/frames at each
stage to distinguish 'publish stopped' from 'mechanically held' and to detect droop.

Production endpoints only; no safety gate bypassed. Runs on the upper host.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:18288/ros-mobile/hybrid"
PROFILE = "hardware_hybrid_follow_v1_dds"
OWNER = "t6-estop"


def req(path, body=None, method="POST", timeout=10, headers=None):
    data = None if body is None else json.dumps(body).encode()
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read(1_000_000) or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read(8000) or b"{}")
        except ValueError:
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}:{e}"}


def pose(s):
    return {x.get("name"): x.get("raw_position")
            for x in (((s.get("feedback") or {}).get("samples")) or [])}


def snap(stage, extra=None):
    _, s = req("/status", None, "GET")
    rec = {"stage": stage, "state": s.get("state"), "outputs": s.get("outputs_enabled"),
           "arm_state": s.get("arm_state"),
           "estop": (s.get("control") or {}).get("estop_latched"),
           "pose": pose(s), "last_error": s.get("last_error")}
    if extra:
        rec.update(extra)
    return rec


def acquire_engage(trace):
    code, acq = req("/session", {"action": "acquire", "profile": PROFILE, "owner": OWNER})
    if code != 200:
        trace.append({"stage": "acquire_fail", "http": code, "resp": acq}); return None
    ident = {"session_id": acq["session_id"], "session_epoch": acq["session_epoch"],
             "calibration_bundle_hash": acq["calibration_bundle_hash"]}
    req("/control", {"action": "preview", **ident}); time.sleep(0.15)
    req("/control", {"action": "align", **ident}); time.sleep(0.2)
    req("/control", {"action": "arm", **ident}); time.sleep(0.2)
    ce, _ = req("/control", {"action": "engage", **ident})
    trace.append({"stage": "engaged", "engage_http": ce, "session": ident["session_id"]})
    return ident


def main():
    out_path = sys.argv[1]
    trace = []
    stop = threading.Event()
    ident_box = {}

    def heartbeat():
        while not stop.is_set():
            ident = ident_box.get("ident")
            if ident:
                req("/heartbeat", ident)
            stop.wait(2.0)

    threading.Thread(target=heartbeat, daemon=True).start()

    ident = acquire_engage(trace)
    if not ident:
        json.dump({"status": "fail", "trace": trace}, open(out_path, "w"), indent=2)
        stop.set(); return 1
    ident_box["ident"] = ident
    time.sleep(1.5)
    base = snap("baseline"); trace.append(base)

    # --- operator estop ---
    ce, er = req("/control", {"action": "estop", "reason": "t6-operator-estop", **ident})
    trace.append({"stage": "estop_sent", "http": ce, "resp": {k: er.get(k) for k in ("estop", "reason")} if isinstance(er, dict) else er})
    for i in range(6):
        time.sleep(0.5); trace.append(snap(f"after_estop_{i}"))
    # --- clear estop ---
    cc, cr = req("/clear-estop", {}, "POST")
    trace.append({"stage": "clear_sent", "http": cc,
                  "resp": {k: cr.get(k) for k in ("estop", "confirmed_state", "motion_granted", "authorised_by", "last_error")} if isinstance(cr, dict) else cr})
    for i in range(3):
        time.sleep(0.5); trace.append(snap(f"after_clear_{i}"))
    # --- re-engage (explicit) ---
    _, s = req("/status", None, "GET")
    if s.get("state") != "ready":
        ident = acquire_engage(trace) or ident
        ident_box["ident"] = ident
    else:
        req("/control", {"action": "arm", **ident}); time.sleep(0.2)
        req("/control", {"action": "engage", **ident}); time.sleep(0.2)
    for i in range(4):
        time.sleep(0.5); trace.append(snap(f"after_reengage_{i}"))

    # --- release ---
    cr2, rr = req("/session", {"action": "release", **ident, "reason": "t6_done"})
    trace.append({"stage": "released", "http": cr2})
    stop.set()
    doc = {"status": "pass", "trace": trace}
    json.dump(doc, open(out_path, "w"), indent=2)
    print("DONE trace=", len(trace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())