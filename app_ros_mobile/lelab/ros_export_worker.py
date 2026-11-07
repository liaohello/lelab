"""Standalone LeRobot export worker.

Runs in its own process group so a large export never blocks the FastAPI event
loop and can be cancelled by the upper host without leaving the API wedged.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path


def _write_rejection(session_id: str, exc: BaseException, result: dict) -> str | None:
    """Persist why an export refused, next to the session it refused.

    A rejected export is evidence too: the operator needs to see that the run was
    refused for a source/provenance/phase reason rather than silently producing
    nothing.  Failure to write is reported in the result, never swallowed.
    """
    try:
        import lelab.ros_mobile as ros_mobile

        safe = session_id.replace("/", "_").replace("..", "_")
        directory = Path(ros_mobile.UPPER_EXPORTS) / safe
        directory.mkdir(parents=True, exist_ok=True)
        detail = getattr(exc, "detail", None) or str(exc) or type(exc).__name__
        payload = {
            "session_id": session_id,
            "status": "rejected",
            "error_type": type(exc).__name__,
            "detail": detail,
            "reason": _reason_token(detail),
            "wall_time": time.time(),
            "job_status": result.get("status"),
        }
        path = directory / "export_rejected.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)
    except Exception as write_exc:  # noqa: BLE001 - must not mask the original failure
        return f"unwritten:{type(write_exc).__name__}"


def _reason_token(detail: str) -> str:
    text = str(detail).lower()
    for token in ("feedback_provenance", "calibration_hash", "covariance", "max_state_age_s",
                  "missing_required_phase", "phase", "expired", "zero fill", "untrusted",
                  "forbidden", "read-only", "training_export", "runtime_mode", "cmd"):
        if token in text:
            return token
    return "other"


def main() -> int:
    # The fourth argument is optional so an older invocation still means the hardware path.
    if len(sys.argv) not in (4, 5):
        print("usage: python -m lelab.ros_export_worker SESSION_ID INCLUDE_FAILED RESULT_PATH "
              "[SIMULATED_OFFLINE_6D]", file=sys.stderr)
        return 2
    session_id, include_failed_raw, result_path_raw = sys.argv[1:4]
    simulated_offline = len(sys.argv) > 4 and sys.argv[4] == "1"
    result_path = Path(result_path_raw)
    try:
        from lelab.ros_mobile import export_session

        report = export_session(session_id, include_failed_raw == "1",
                                simulated_offline=simulated_offline)
        result = {"status": "completed", "report": report}
    except BaseException as exc:  # noqa: BLE001 - worker must report every failure
        detail = getattr(exc, "detail", None) or str(exc) or type(exc).__name__
        result = {
            "status": "failed",
            "error": detail,
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(limit=8),
        }
        result["rejection_report"] = _write_rejection(session_id, exc, result)
        print(result["traceback"], file=sys.stderr)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
