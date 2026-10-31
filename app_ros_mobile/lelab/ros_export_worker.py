"""Standalone LeRobot export worker.

Runs in its own process group so a large export never blocks the FastAPI event
loop and can be cancelled by the upper host without leaving the API wedged.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: python -m lelab.ros_export_worker SESSION_ID INCLUDE_FAILED RESULT_PATH", file=sys.stderr)
        return 2
    session_id, include_failed_raw, result_path_raw = sys.argv[1:]
    result_path = Path(result_path_raw)
    try:
        from lelab.ros_mobile import export_session

        report = export_session(session_id, include_failed_raw == "1")
        result = {"status": "completed", "report": report}
    except BaseException as exc:  # noqa: BLE001 - worker must report every failure
        detail = getattr(exc, "detail", None) or str(exc) or type(exc).__name__
        result = {
            "status": "failed",
            "error": detail,
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(limit=8),
        }
        print(result["traceback"], file=sys.stderr)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
