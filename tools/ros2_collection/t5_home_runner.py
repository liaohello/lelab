#!/usr/bin/env python3
"""T5 Phase A: acquire -> preview -> align -> bounded safe-pose homing, with an
upper-layer heartbeat so the car control token does not expire while the in-app
browser tab is backgrounded. Production endpoints only; no safety gate bypassed.

Runs on the upper host (HTTP to the FastAPI app on 127.0.0.1:18288).
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
OWNER = "t5-phase-a"


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
    out_path, trace_path = sys.argv[1], sys.argv[2]
    trace = []

    code, acq = req("/session", {"action": "acquire", "profile": PROFILE, "owner": OWNER})
    if code != 200 or not acq.get("session_id"):
        json.dump({"phase": "A", "status": "fail", "stage": "acquire",
                   "http": code, "resp": acq}, open(out_path, "w"), indent=2)
        print("ACQUIRE FAILED", code, json.dumps(acq)[:300]); return 1
    ident = {"session_id": acq["session_id"], "session_epoch": acq["session_epoch"],
             "calibration_bundle_hash": acq["calibration_bundle_hash"]}
    print("ACQUIRED", ident["session_id"], "epoch", ident["session_epoch"])

    req("/control", {"action": "preview", **ident})
    time.sleep(0.15)
    cp, prev = req("/control", {"action": "align", **ident})
    print("PREVIEW/ALIGN", cp)

    stop = threading.Event()
    hb_state = {"ok": 0, "fail": 0, "last": None}

    def hb():
        while not stop.is_set():
            c, d = req("/heartbeat", ident)
            if c == 200:
                hb_state["ok"] += 1
            else:
                hb_state["fail"] += 1
                hb_state["last"] = {"http": c, "resp": d}
            stop.wait(2.0)

    threading.Thread(target=hb, daemon=True).start()

    _, s0 = req("/status", None, "GET")
    start_pose = pose(s0)
    print("START POSE", start_pose)

    ch, home = req("/home", {"action": "start", **ident})
    print("HOME START", ch, json.dumps(home)[:200])

    t0 = time.time()
    final = {}
    while time.time() - t0 < 150.0:
        _, h = req("/home", None, "GET")
        _, s = req("/status", None, "GET")
        tor = ((s.get("homing") or {}).get("torque") or {})
        rec = {"t": round(time.time() - t0, 1), "home": h.get("state"),
               "prog": h.get("progress_pct"), "joint": h.get("current_joint"),
               "observed": h.get("observed_raw"), "step": h.get("step_target_raw"),
               "torque_state": tor.get("state"), "torque_results": tor.get("results"),
               "home_err": h.get("last_error"), "outputs_enabled": s.get("outputs_enabled"),
               "session_state": s.get("state"), "pose": pose(s),
               "hb_ok": hb_state["ok"], "hb_fail": hb_state["fail"]}
        trace.append(rec)
        print(json.dumps({k: rec[k] for k in ("t", "home", "prog", "joint", "torque_state", "session_state", "outputs_enabled")}))
        final = rec
        if h.get("state") in ("complete", "fault", "cancelled"):
            break
        time.sleep(1.0)
    stop.set()

    _, sf = req("/status", None, "GET")
    doc = {"phase": "A", "status": "pass" if final.get("home") == "complete" else "fail",
           "session": ident, "profile": PROFILE, "owner": OWNER,
           "start_pose": start_pose, "final_pose": pose(sf),
           "final_home": final.get("home"), "home_error": final.get("home_err"),
           "torque_state": final.get("torque_state"),
           "torque_results": final.get("torque_results"),
           "outputs_enabled_after": sf.get("outputs_enabled"),
           "session_state_after": sf.get("state"),
           "heartbeat": {"ok": hb_state["ok"], "fail": hb_state["fail"], "last": hb_state["last"]},
           "trace_len": len(trace)}
    json.dump(doc, open(out_path, "w"), indent=2)
    with open(trace_path, "w") as fh:
        for r in trace:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    print("RESULT", doc["status"], "final_home", doc["final_home"],
          "torque", doc["torque_state"], "final_pose", doc["final_pose"])
    return 0 if doc["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())