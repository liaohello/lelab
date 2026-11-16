#!/usr/bin/env python3
"""T7: production two-episode observe/grasp recording over the real DDS link.

Requires --operator-present. Heartbeat starts before any motion. Each set_phase
pauses output; the runner re-engages (preview->align->arm->engage) and verifies
per-ID torque before each motion window. Failure paths pause before release.

Usage: t7_record_runner.py --operator-present <out.json> [observe_s] [grasp_s] [task]
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
OWNER = "t7-record"


def req(path, body=None, method="POST", timeout=12):
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
    args = sys.argv[1:]
    if not args or args[0] != "--operator-present":
        print("REFUSED: pass --operator-present to confirm physical presence at the leader",
              flush=True)
        return 2
    args = args[1:]
    if not args:
        print("usage: t7_record_runner.py --operator-present <out.json> [observe_s] [grasp_s] [task]",
              flush=True)
        return 2
    out_path = args[0]
    obs = float(args[1]) if len(args) > 1 else 12.0
    grs = float(args[2]) if len(args) > 2 else 12.0
    task = args[3] if len(args) > 3 else "mobile_arm_gripper_teleop"
    steps = []
    ident = None
    stop = threading.Event()

    def rec(name, code, doc, extra=None):
        steps.append({"step": name, "http": code,
                      "doc": doc if not isinstance(doc, dict) else
                      {k: doc.get(k) for k in list(doc)[:20]},
                      **({"extra": extra} if extra else {})})
        print(f"[{name}] http={code}", flush=True)

    def fail(reason):
        if ident:
            c, pz = req("/control", {"action": "pause_all", **ident})
            rec("fail_pause", c, pz)
            c, rl = req("/session", {"action": "release", **ident, "reason": reason})
            rec("fail_release", c, rl)
        stop.set()
        json.dump({"status": "fail", "reason": reason, "steps": steps},
                  open(out_path, "w"), indent=2)
        print("FAIL", reason, flush=True)
        return 1

    def hb():
        while not stop.is_set():
            req("/heartbeat", ident)
            stop.wait(2.0)

    def reengage(label):
        """preview -> align -> arm -> engage after a pause; verify per-ID torque."""
        c, _ = req("/control", {"action": "preview", **ident})
        if c != 200:
            rec(f"{label}_preview", c, _)
            return False
        time.sleep(0.15)
        c, a = req("/control", {"action": "align", **ident})
        rec(f"{label}_align", c, a)
        if c != 200:
            return False
        c, a = req("/control", {"action": "arm", **ident})
        rec(f"{label}_arm", c, a)
        if c != 200:
            return False
        time.sleep(0.2)
        c, e = req("/control", {"action": "engage", **ident})
        rec(f"{label}_engage", c, e)
        if c != 200:
            return False
        deadline = time.time() + 15.0
        tor = {}
        while time.time() < deadline:
            _, s = req("/status", None, "GET")
            tor = ((s.get("homing") or {}).get("torque") or {})
            state = tor.get("state")
            if state == "verified":
                rec(f"{label}_torque", 200, tor)
                return True
            if state == "failed":
                rec(f"{label}_torque", 200, tor)
                return False
            time.sleep(0.5)
        rec(f"{label}_torque", 200, {"state": "timeout", "last": tor})
        return False

    def home_and_verify():
        """Load per-ID torque by running the bounded safe-pose homing in this session.

        The control only runs _progress_homing_torque while a homing motion is set, so
        plain engage leaves torque 'idle'. Homing loads+verifies torque and also puts the
        arm at the known safe pose before recording."""
        c, h = req("/home", {"action": "start", **ident})
        rec("home_start", c, h)
        if c != 200:
            return False
        deadline = time.time() + 180.0
        while time.time() < deadline:
            _, hs = req("/home", None, "GET")
            _, s = req("/status", None, "GET")
            st = hs.get("state")
            tor = ((s.get("homing") or {}).get("torque") or {}).get("state")
            if st == "complete" and tor == "verified":
                rec("home_complete", 200, {"state": st, "torque": tor})
                return True
            if st in ("fault", "cancelled") or tor == "failed":
                rec("home_failed", 200, {"state": st, "torque": tor, "err": hs.get("last_error")})
                return False
            time.sleep(1.0)
        rec("home_timeout", 200, {})
        return False

    # 1) acquire
    c, acq = req("/session", {"action": "acquire", "profile": PROFILE, "owner": OWNER})
    rec("acquire", c, acq)
    if c != 200:
        json.dump({"status": "fail", "steps": steps}, open(out_path, "w"), indent=2)
        return 1
    ident = {"session_id": acq["session_id"], "session_epoch": acq["session_epoch"],
             "calibration_bundle_hash": acq["calibration_bundle_hash"]}

    # 2) heartbeat before any motion
    threading.Thread(target=hb, daemon=True).start()

    # 3) load torque via bounded safe-pose homing (per-ID verified), then engage
    if not home_and_verify():
        return fail("homing_failed")
    if not reengage("init"):
        return fail("init_engage_failed")

    # 4) start recording session
    c, rs = req("/recording", {"action": "start_session", **ident, "task": task})
    rec("rec_start_session", c, rs)
    if c != 200:
        return fail("rec_start_session_failed")

    def do_episode(n):
        c, se = req("/recording", {"action": "start_episode", **ident})
        rec(f"ep{n}_start", c, se)
        if c != 200:
            return False
        c, ph = req("/recording", {"action": "set_phase", "phase": "observe", **ident})
        rec(f"ep{n}_observe_phase", c, ph)
        if c != 200:
            return False
        if not reengage(f"ep{n}_observe"):
            return False
        time.sleep(obs)
        c, pg = req("/recording", {"action": "set_phase", "phase": "grasp", **ident})
        rec(f"ep{n}_grasp_phase", c, pg)
        if c != 200:
            return False
        if not reengage(f"ep{n}_grasp"):
            return False
        time.sleep(grs)
        c, ee = req("/recording", {"action": "end_episode", **ident})
        rec(f"ep{n}_end", c, ee)
        return c == 200

    ok1 = do_episode(1)
    if not ok1:
        return fail("episode1_failed")

    # Between episodes: re-align and re-engage explicitly
    if not reengage("inter_episode"):
        return fail("inter_episode_engage_failed")

    ok2 = do_episode(2)
    if not ok2:
        return fail("episode2_failed")

    # 5) stop session (pauses internally), then explicit pause + release
    c, ss = req("/recording", {"action": "stop_session", **ident})
    rec("rec_stop_session", c, ss)
    c, pz = req("/control", {"action": "pause_all", **ident})
    rec("pause", c, pz)
    c, rl = req("/session", {"action": "release", **ident, "reason": "t7_record_done"})
    rec("release", c, rl)
    stop.set()

    _, s = req("/status", None, "GET")
    doc = {"status": "pass" if (ok1 and ok2 and c == 200) else "fail",
           "episodes_ok": {"ep1": ok1, "ep2": ok2},
           "observe_s": obs, "grasp_s": grs, "task": task, "task_result": "unknown",
           "final_state": s.get("state"), "final_pose": pose(s), "steps": steps}
    json.dump(doc, open(out_path, "w"), indent=2)
    print("DONE", doc["status"], "ep1", ok1, "ep2", ok2, flush=True)
    return 0 if doc["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())