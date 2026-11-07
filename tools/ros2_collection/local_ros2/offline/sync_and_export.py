#!/usr/bin/env python3
"""OFFLINE ONLY - run the production sync and the restricted offline six-dimensional export.

Both calls are the ones the upper computer itself uses: `sync_session` fetches the archive
from the car agent over HTTP, verifies every file's SHA256, checks READY against the
manifest hash, and only then publishes the copy; `export_session` is the exporter's own
function, reached through the restricted simulated contract rather than a fixture.  Nothing
here reimplements either step.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "app_ros_mobile"))

from lelab import ros_mobile  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--skip-export", action="store_true")
    args = parser.parse_args()

    out = {"session_id": args.session_id, "data_identity": "simulated_offline",
           "training_eligible": False,
           "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    failures = []
    try:
        out["sync"] = ros_mobile.sync_session(args.session_id)
    except Exception as error:  # noqa: BLE001 - the report has to carry the real refusal
        out["sync_error"] = f"{type(error).__name__}: {getattr(error, 'detail', None) or error}"
        failures.append("sync")

    if not args.skip_export and "sync" in out:
        try:
            report = ros_mobile.export_session(args.session_id, simulated_offline=True)
            out["export"] = {key: report.get(key) for key in (
                "status", "repo_id", "dataset_root", "fps", "declared_camera_fps",
                "total_frames", "total_episodes", "data_identity", "training_eligible",
                "export_contract", "sampling")}
            out["export"]["episodes"] = [
                {"episode_id": item["episode_id"], "frames": item["frames"],
                 "success": item.get("success"),
                 "phases": [[phase["phase"], phase["frame_from"], phase["frame_to_exclusive"]]
                            for phase in item["phases"]]}
                for item in report.get("episodes", [])]
            out["export"]["warnings"] = report.get("warnings")
        except Exception as error:  # noqa: BLE001
            detail = str(getattr(error, "detail", None) or error)
            # Re-running the command must not silently overwrite a published dataset: the
            # exporter refuses, and this reports that as already-exported rather than as a
            # failure of the chain.
            if "dataset already exists" in detail:
                out["export"] = {"status": "already_exported", "detail": detail[:300]}
            else:
                out["export_error"] = f"{type(error).__name__}: {detail[:400]}"
                failures.append("export")

    out["failed"] = failures
    path = pathlib.Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, sort_keys=True, default=str) + "\n",
                    encoding="utf-8")
    print(json.dumps({"sync_status": (out.get("sync") or {}).get("status"),
                      "sync_verified_files": (out.get("sync") or {}).get("verified_files"),
                      "export": out.get("export") or out.get("export_error"),
                      "sync_error": out.get("sync_error")}, indent=1, default=str)[:1500])
    print(f"report: {path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
