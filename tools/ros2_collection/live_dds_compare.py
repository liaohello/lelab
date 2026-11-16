#!/usr/bin/env python3
"""T3 dual-host DDS vs HTTP feedback comparison (read-only, no command publish)."""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app_ros_mobile"))
from lelab.hybrid_dds_feedback import (  # noqa: E402
    ClockMapping, DdsFeedbackClient, feedback_from_snapshot,
)

JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]


def http_status(url):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/status", timeout=2.0) as r:
            return json.loads(r.read())
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}:{exc}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--agent-url", required=True)
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--calibration-hash", default="")
    ap.add_argument("--transport-bound", type=float, default=0.15)
    ap.add_argument("--stale", type=float, default=0.5)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    client = DdsFeedbackClient(args.socket, timeout_s=1.0)
    mapping = ClockMapping()
    rows = []
    started = time.monotonic()
    while time.monotonic() - started < args.duration:
        snap = client.read()
        dds = feedback_from_snapshot(snap or {}, mapping,
                                     expected_calibration_hash=args.calibration_hash,
                                     stale_after_s=args.stale,
                                     transport_bound_s=args.transport_bound)
        http = http_status(args.agent_url)
        hfb = (http.get("control") or {}).get("follower_feedback") or {}
        http_cal = ((hfb.get("joint_samples") or {}).get("joint1") or {}).get("calibration_hash")
        dds_seq = dds.get("scan_sequence")
        http_seq = hfb.get("scan_sequence")
        dds_owner = dds.get("publisher_instance")
        http_owner = hfb.get("publisher_instance")
        same_scan = (dds_seq is not None and dds_seq == http_seq
                     and dds_owner == http_owner)
        raw_diff = {}
        if same_scan:
            hs = hfb.get("joint_samples") or {}
            ds = dds.get("joint_samples") or {}
            for name in JOINTS:
                a = (hs.get(name) or {}).get("raw_position")
                b = (ds.get(name) or {}).get("raw_position")
                raw_diff[name] = (None if a is None or b is None else int(b) - int(a))
        rows.append({
            "t_mono_ns": time.monotonic_ns(),
            "dds_available": dds.get("available"), "dds_reason": dds.get("reason"),
            "dds_age_bound_s": dds.get("age_bound_s"),
            "dds_owner": dds_owner, "dds_seq": dds_seq,
            "dds_calibration": dds.get("calibration_hash"),
            "http_available": hfb.get("available"), "http_reason": hfb.get("reason"),
            "http_owner": http_owner, "http_seq": http_seq,
            "http_calibration": http_cal,
            "same_scan": same_scan, "raw_diff_dds_minus_http": raw_diff,
        })
        time.sleep(0.2)
    client.close()
    with (out / "feedback-compare.jsonl").open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    matched = [r for r in rows if r["same_scan"]]
    diffs = [abs(v) for r in matched for v in r["raw_diff_dds_minus_http"].values()
             if v is not None]
    ages = [r["dds_age_bound_s"] for r in rows if isinstance(r["dds_age_bound_s"], (int, float))]
    summary = {
        "id": "T3", "hardware_or_simulated": "hardware",
        "duration_s": round(time.monotonic() - started, 2),
        "samples": len(rows), "matched_same_owner_seq": len(matched),
        "max_abs_raw_diff_ticks": max(diffs) if diffs else None,
        "dds_available_count": sum(1 for r in rows if r["dds_available"]),
        "dds_age_bound_max_s": max(ages) if ages else None,
        "dds_age_bound_p95_s": sorted(ages)[int(len(ages) * 0.95)] if ages else None,
        "calibration_match_count": sum(1 for r in matched
                                       if r["dds_calibration"] == r["http_calibration"]),
        "owners": sorted({r["dds_owner"] for r in rows if r["dds_owner"] is not None}),
        "transport_bound_s": args.transport_bound, "stale_gate_s": args.stale,
        "age_source": "car_publish_age_plus_bounded_transport",
    }
    (out / "compare-summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    (out / "clock-mapping.json").write_text(json.dumps(mapping.state(), indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
