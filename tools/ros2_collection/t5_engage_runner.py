#!/usr/bin/env python3
"""T5: acquire -> preview -> align -> arm -> engage, with heartbeat, and report
whether output enabling loads/verifies per-ID torque and whether the pose jumps.
No motion is commanded here; the follow worker only tracks leader deltas, so with
a static leader the target equals the reference (no jump).

Production endpoints only. Runs on the upper host (HTTP to 127.0.0.1:18288).
Mode arg: 'arm_engage' (default) or 'pause_release'.
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
OWNER = "t5-run"


def req(path, body=None, method="POST", timeout=10):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
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


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "arm_engage"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/t5_engage.json"
    ident = None

    # If a session already exists (e.g. left from another runner), reuse it.
    _, s = req("/status", None, "GET")
    if s.get("state") == "ready" and s.get("session_id"):
        ident = {"session_id": s["session_id"], "session_epoch": s["session_epoch"],
                 "calibration_bundle_hash": s.get("calibration_bundle_hash")}
        print("REUSE SESSION", ident["session_id"])
    else:
        code, acq = req("/session", {"action": "acquire", "profile": PROFILE, "owner": OWNER})
        if code != 200 or not acq.get("session_id"):
            json.dump({"status": "fail", "stage": "acquire", "http": code, "resp": acq},
                      open(out_path, "w"), indent=2)
            print("ACQUIRE FAILED", code, json.dumps(acq)[:300]); return 1
        ident = {"session_id": acq["session_id"], "session_epoch": acq["session_epoch"],
                 "calibration_bundle_hash": acq["calibration_bundle_hash"]}
        req("/control", {"action": "preview", **ident})
        time.sleep(0.15)
        cp, pv = req("/control", {"action": "align", **ident})
        print("PREVIEW/ALIGN", cp)

    stop = threading.Event()
    hb = {"ok": 0, "fail": 0, "last": None}

    def heartbeat():
        while not stop.is_set():
            c, d = req("/heartbeat", ident)
            if c == 200:
                hb["ok"] += 1
            else:
                hb["fail"] += 1
                hb["last"] = {"http": c, "resp": d}
            stop.wait(2.0)

    threading.Thread(target=heartbeat, daemon=True).start()

    _, s0 = req("/status", None, "GET")
    start_pose = pose(s0)
    print("START POSE", start_pose)

    if mode == "pause_release":
        cp, pr = req("/control", {"action": "pause_all", **ident})
        cr, rr = req("/session", {"action": "release", **ident, "reason": "t5_done"})
        stop.set()
        doc = {"mode": mode, "pause": {"http": cp, "resp": pr}, "release": {"http": cr, "resp": rr}}
        json.dump(doc, open(out_path, "w"), indent=2)
        print("PAUSE/RELEASE", cp, cr); return 0

    # arm then engage
    ca, ra = req("/control", {"action": "arm", **ident})
    print("ARM", ca, json.dumps(ra.get("arm_state") if isinstance(ra, dict) else ra)[:120])
    time.sleep(0.3)
    ce, re_ = req("/control", {"action": "engage", **ident})
    print("ENGAGE", ce, json.dumps(re_)[:200] if ce != 200 else "ok")

    trace = []
    t0 = time.time()
    while time.time() - t0 < 15.0:
        _, s = req("/status", None, "GET")
        tor = ((s.get("homing") or {}).get("torque") or {})
        rec = {"t": round(time.time() - t0, 1), "state": s.get("state"),
               "arm_state": s.get("arm_state"), "outputs": s.get("outputs_enabled"),
               "torque_state": tor.get("state"), "torque_results": tor.get("results"),
               "pose": pose(s), "last_error": s.get("last_error"),
               "missing": s.get("missing_for_following"),
               "channels": {k: (v.get("permitted") if isinstance(v, dict) else v)
                            for k, v in (s.get("channels") or {}).items()},
               "hb_ok": hb["ok"], "hb_fail": hb["fail"]}
        trace.append(rec)
        time.sleep(1.0)
    stop.set()
    doc = {"mode": mode, "session": ident, "arm_http": ca, "engage_http": ce,
           "start_pose": start_pose, "trace": trace,
           "final": trace[-1] if trace else None}
    json.dump(doc, open(out_path, "w"), indent=2)
    print("FINAL", json.dumps({k: (trace[-1][k] if trace else None)
                               for k in ("state", "arm_state", "outputs", "torque_state", "missing", "last_error")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())