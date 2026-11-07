#!/usr/bin/env python3
"""六 on the deployed path: the upper computer stops sending while the car agent stays alive.

需求六 forbids reading "the upper computer stopped sending" as "the car stopped". Locally that is not
a kill-the-agent test (`local_car_fault_injection.py` already covers that death): here the agent keeps
running, keeps its control lease and keeps recording, while the process that streams leader-arm
targets is frozen with SIGSTOP. What has to hold is that the car marks the arm stream expired on its
own monotonic clock, and that when the stream comes back it is a *new* target - never a burst of the
poses that queued up while the link was stuck.

Run `local_car_rehearsal.py` first (it feeds the synthetic feedback and camera topics), then:

    app_ros_mobile/.venv/bin/python tools/ros2_collection/local_car_upper_stream_stall.py \
        --root /tmp/lelab_car_rehearsal/car

Nothing here moves hardware; the follower is synthetic, so no arm can move even if a trajectory
leaves the gateway. Exits non-zero with a named reason if the car or the session misbehaves.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DRIVER = REPO / "tools" / "ros2_collection" / "local_teleop_session_drive.py"


def target_is_newer(before: dict | None, after: dict | None) -> bool:
    """Whether `after` is a later accepted command than `before`, not the same one again.

    The car's `control.teleop.last` block carries the target itself (`pre_clip_rad`,
    `gripper_percent`, `clutch`, `calibration_version`) and the monotonic `wall` stamp of when the
    gateway took it - there is no sequence number in it. Advancing `wall` *and* changing the payload
    is what shows the stream resumed with fresh commands instead of replaying the pose that was
    current when the upper computer froze.
    """
    if not before or not after:
        return False
    return after.get("wall", 0) > before.get("wall", 0) and after != before


def stream(url: str, timeout: float = 1.0) -> dict:
    """One read of the car's own report of the arm stream; a failed read is a sample too."""
    try:
        with urllib.request.urlopen(url + "/status", timeout=timeout) as response:
            body = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {"unreadable": str(exc)}
    teleop = ((body.get("control") or {}).get("teleop") or {})
    return {"stream": teleop.get("stream"), "last": teleop.get("last"),
            "active": (body.get("control") or {}).get("active"),
            "mode": (body.get("control") or {}).get("control_mode"),
            "hold_policy": teleop.get("hold_policy"),
            "torque_control_offered": teleop.get("torque_control_offered")}


def watch(url: str, seconds: float, phase: str, sink: list) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        sample = stream(url)
        sample.update(phase=phase, t=round(time.monotonic(), 3))
        sink.append(sample)
        time.sleep(0.2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/lelab_car_rehearsal/car")
    ap.add_argument("--follow-seconds", type=float, default=8.0,
                    help="leader-seconds per episode in the driver run")
    ap.add_argument("--stall-seconds", type=float, default=2.5)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    root = Path(args.root)
    marker = json.loads((root / "READY_FOR_DRIVER.json").read_text(encoding="utf-8"))
    url = f"http://127.0.0.1:{marker['port']}"
    with urllib.request.urlopen(url + "/health", timeout=3.0) as response:
        health = json.loads(response.read())
    if not health.get("ok"):
        raise SystemExit(f"the rehearsal agent is not healthy: {health}")

    report_path = root / "stalled_upper.json"  # inside this run's own root, never a shared path
    child = subprocess.Popen([sys.executable, str(DRIVER), "--root", str(root),
                              "--follow-seconds", str(args.follow_seconds),
                              "--out", str(report_path)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    samples: list[dict] = []
    phases: dict[str, Any] = {}
    live_before = None
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline and live_before is None:
        if child.poll() is not None:
            raise SystemExit(f"the driver exited before the arm stream ever went live: "
                             f"{child.stdout.read()[-400:]}")
        sample = stream(url)
        status = (sample.get("stream") or {})
        if status.get("live") and sample.get("active"):
            live_before = dict(sample, t=round(time.monotonic(), 3))
            samples.append(dict(live_before, phase="before_stall"))
        else:
            time.sleep(0.2)
    if live_before is None:
        raise SystemExit("the arm stream never became live - nothing was stalled")

    expired: list[dict] = []
    over_ttl: list[dict] = []
    os.kill(child.pid, signal.SIGSTOP)
    try:
        phases["stopped_signal_sent"] = {"child_pid": child.pid, "signal": "SIGSTOP",
                                         "state": Path(f"/proc/{child.pid}/stat").read_text()
                                         .split(") ", 1)[1].split()[0]}
        watch(url, args.stall_seconds, "frozen", samples)
        expired = [s for s in samples if s.get("phase") == "frozen"
                   and (s.get("stream") or {}).get("expected")
                   and not (s.get("stream") or {}).get("live")]
        over_ttl = [s for s in expired if (s["stream"].get("age_s") or 0) > (s["stream"].get("ttl_s") or 0)]
    finally:
        os.kill(child.pid, signal.SIGCONT)
    watch(url, 1.5, "resumed", samples)
    resumed_live = [s for s in samples if s.get("phase") == "resumed"
                    and (s.get("stream") or {}).get("live")]
    out, _ = child.communicate(timeout=180)

    driver_report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    frozen_last = next((s.get("last") for s in samples if s.get("phase") == "frozen"), None)
    newest_last = resumed_live[-1].get("last") if resumed_live else None
    session_dir = root / "sessions" / str(driver_report.get("manifest", {}).get("session_id", ""))
    manifest = {}
    if session_dir.is_dir():
        manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))

    expectations = {
        "car_marked_the_stream_expired_without_being_told": bool(expired) and bool(over_ttl),
        "car_kept_its_own_control_lease_while_the_upper_was_frozen": all(
            s.get("active") is True for s in samples if s.get("phase") == "frozen"),
        "no_new_target_while_frozen": len({json.dumps(s.get("last"), sort_keys=True, default=str)
                                           for s in samples if s.get("phase") == "frozen"}) <= 1,
        "stream_came_back_live_after_resume": bool(resumed_live),
        "resume_carries_a_newer_target_not_a_replayed_one": target_is_newer(frozen_last, newest_last),
        "the_car_never_offered_torque_off_as_a_stop": all(
            s.get("torque_control_offered") is False and s.get("hold_policy")
            for s in samples if s.get("phase") == "frozen"),
        "the_upper_side_session_still_finalised_cleanly": manifest.get("status") == "raw_ready"
                                                         and bool(manifest.get("episodes")),
        "driver_expectations_still_hold": driver_report.get("expectations", {}).get("failed") == [],
    }
    expectations["failed"] = [k for k, v in expectations.items() if v is False]

    out_json = {"agent_url": url, "marker": marker, "child": {"pid": child.pid, "returncode": child.returncode},
                "phases": phases, "expired_samples_while_frozen": expired[:4],
                "live_before_stall": live_before, "first_live_after_resume": (resumed_live or [None])[0],
                "sample_count": len(samples), "driver_report": driver_report.get("expectations"),
                "driver_state_after_end_episode": driver_report.get("after_end_episode_state"),
                "manifest": manifest and {"session_id": manifest["session_id"], "status": manifest["status"],
                                          "episodes": len(manifest["episodes"])},
                "driver_stdout_tail": out[-400:], "expectations": expectations}
    text = json.dumps(out_json, indent=2, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 1 if expectations["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
