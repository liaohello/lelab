#!/usr/bin/env python3
"""T5 follow capture: reuse the live session (following), keep it alive with a
2 s heartbeat, sample leader positions and follower readback at 5 Hz for the
requested window, then pause_all + release at the end. Read-only HTTP; no extra
motion command is sent (the follower tracks the leader the operator moves).

Usage: t5_follow_capture.py <out.jsonl> <duration_s>
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:18288/ros-mobile/hybrid"


def req(path, body=None, method="POST", timeout=8):
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


def main():
    out_path = sys.argv[1]
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 75.0
    _, s = req("/status", None, "GET")
    if s.get("state") != "ready" or not s.get("session_id"):
        json.dump({"status": "fail", "reason": "session_not_ready", "state": s.get("state")},
                  open(out_path.replace(".jsonl", ".json"), "w"), indent=2)
        print("NOT READY", s.get("state")); return 1
    ident = {"session_id": s["session_id"], "session_epoch": s["session_epoch"],
             "calibration_bundle_hash": s.get("calibration_bundle_hash")}
    print("CAPTURE SESSION", ident["session_id"], "duration", duration)

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

    t0 = time.time()
    n = 0
    with open(out_path, "w") as fh:
        while time.time() - t0 < duration:
            _, st = req("/status", None, "GET")
            fr = st.get("follow_runtime") or {}
            rec = {
                "t": round(time.time() - t0, 2),
                "state": st.get("state"), "outputs": st.get("outputs_enabled"),
                "leader": (st.get("leader") or {}).get("positions_rad"),
                "pose": {x.get("name"): x.get("raw_position")
                         for x in (((st.get("feedback") or {}).get("samples")) or [])},
                "follow": {k: fr.get(k) for k in ("following", "paused", "last_seq", "reason",
                                                  "target_rad", "error", "frames_sent")},
                "channels": {k: (v.get("permitted") if isinstance(v, dict) else v)
                             for k, v in (st.get("channels") or {}).items()},
                "last_error": st.get("last_error"), "hb_ok": hb["ok"], "hb_fail": hb["fail"],
            }
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            n += 1
            if st.get("state") not in ("ready",):
                break
            time.sleep(0.2)
    stop.set()
    cp, pr = req("/control", {"action": "pause_all", **ident})
    cr, rr = req("/session", {"action": "release", **ident, "reason": "t5_capture_done"})
    summary = {"samples": n, "duration": round(time.time() - t0, 1),
               "hb": hb, "pause_http": cp, "release_http": cr,
               "session": ident["session_id"]}
    json.dump(summary, open(out_path.replace(".jsonl", ".json"), "w"), indent=2)
    print("DONE", json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())