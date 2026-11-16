"""Cheap ROS status snapshot for the existing WebSocket surface.

The page must not poll ``/status`` to learn whether DDS feedback is fresh.  This
module reads the observer's local cache (and the bridge's typed snapshot when
DDS is selected) and returns one small document.  It never asks the car for
anything and never constructs a second hardware adapter.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any


class _RateTracker:
    def __init__(self, history: int = 16) -> None:
        self.samples: deque[tuple[float, int]] = deque(maxlen=max(2, history))

    def observe(self, sequence: int | None, now_s: float | None = None) -> float | None:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            return None
        now = time.monotonic() if now_s is None else float(now_s)
        if self.samples and now <= self.samples[-1][0]:
            return None
        self.samples.append((now, sequence))
        if len(self.samples) < 2:
            return None
        first_t, first_seq = self.samples[0]
        last_t, last_seq = self.samples[-1]
        elapsed = last_t - first_t
        delta = last_seq - first_seq
        if elapsed <= 0 or delta < 0:
            return None
        return round(delta / elapsed, 2)


_RATE = _RateTracker()


def _first_failure(health: dict[str, Any], feedback: dict[str, Any]) -> dict[str, Any] | None:
    candidate = health.get("first_failure") if isinstance(health, dict) else None
    if isinstance(candidate, dict) and candidate.get("present"):
        return candidate
    candidate = health.get("consumer_first_failure_reason") if isinstance(health, dict) else None
    if candidate:
        return {"present": True, "reason": candidate,
                "at_mono_ns": health.get("consumer_first_failure_mono_ns")}
    candidate = feedback.get("first_failure") if isinstance(feedback, dict) else None
    return candidate if isinstance(candidate, dict) else None


def _recovery(health: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(health, dict):
        return None
    phase = health.get("last_recovery_phase")
    if not phase:
        return None
    return {"phase": phase, "ok": health.get("last_recovery_ok"),
            "quiet_s": health.get("last_recovery_quiet_s"),
            "duration_ms": health.get("last_recovery_duration_ms"),
            "at_mono_ns": health.get("last_recovery_mono_ns")}


def collect_ros_status() -> dict[str, Any]:
    from .ros_mobile import get_hybrid_facade

    try:
        facade = get_hybrid_facade()
    except Exception as error:  # noqa: BLE001 - the page renders unavailable
        return {"available": False, "reason": f"facade_unavailable:{type(error).__name__}",
                "timestamp": time.time()}
    observer = getattr(facade, "observer", None)
    cache: dict[str, Any] = {}
    if observer is not None and callable(getattr(observer, "cache_state", None)):
        try:
            cache = observer.cache_state() or {}
        except Exception as error:  # noqa: BLE001
            cache = {"last_error": f"{type(error).__name__}:{error}"}
    facts = getattr(facade, "_facts", None)
    facts = facts if isinstance(facts, dict) else {}
    feedback = facts.get("feedback") if isinstance(facts.get("feedback"), dict) else {}
    snapshot = getattr(observer, "dds_snapshot", None) if observer is not None else None
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    health = snapshot.get("health") if isinstance(snapshot.get("health"), dict) else {}
    owner_health = feedback.get("owner_health") if isinstance(feedback.get("owner_health"), dict) else {}
    if not health:
        health = owner_health
    window = snapshot.get("window") if isinstance(snapshot.get("window"), dict) else {}
    receipt = snapshot.get("receipt") if isinstance(snapshot.get("receipt"), dict) else None
    sequence = feedback.get("scan_sequence")
    if not isinstance(sequence, int):
        sequence = health.get("last_success_seq") if isinstance(health, dict) else None
    rate = _RATE.observe(sequence if isinstance(sequence, int) else None)
    owner_age = health.get("last_success_age_s") if isinstance(health, dict) else None
    source_age = feedback.get("oldest_age_s")
    if not isinstance(source_age, (int, float)) or isinstance(source_age, bool):
        source_age = owner_age if isinstance(owner_age, (int, float)) else None
    session_id = getattr(facade, "session_id", None)
    return {
        "available": True,
        "timestamp": time.time(),
        "session_id": session_id,
        "session_epoch": getattr(facade, "session_epoch", None),
        "state": getattr(facade, "state", None),
        "arm_state": getattr(facade, "arm_state", None),
        "transport": getattr(facade, "transport", None),
        "feedback_source": cache.get("feedback_source"),
        "feedback_valid": bool(feedback.get("valid", False)),
        "feedback_reason": feedback.get("reason") or health.get("stale"),
        "source_scan_age_s": source_age,
        "scan_age_bound_s": feedback.get("age_bound_s"),
        "cache_age_s": cache.get("cache_age_s"),
        "sample_rate_hz": rate,
        "scan_span_s": feedback.get("scan_span_s"),
        "scan_sequence": sequence,
        "publisher_instance": feedback.get("publisher_instance")
                              or health.get("owner_instance"),
        "recovery": _recovery(health),
        "first_failure": _first_failure(health, feedback),
        "last_failure": health.get("last_failure") if isinstance(health, dict) else None,
        "owner_health": {
            "scans_ok": health.get("scans_ok"),
            "scans_failed": health.get("scans_failed"),
            "consecutive_failures": health.get("consecutive_failures"),
            "max_scan_interval_s": health.get("max_scan_interval_s"),
            "stale": health.get("stale"),
        } if health else None,
        "consumer": {
            "failed_scans": health.get("consumer_failed_scans")
                            if isinstance(health, dict)
                            else (feedback.get("consumer") or {}).get("failed_scans"),
            "last_error": health.get("consumer_last_error") if isinstance(health, dict)
                          else (feedback.get("consumer") or {}).get("last_error"),
        },
        "clock_mapping": cache.get("clock_mapping"),
        "dds": cache.get("dds"),
        "source_comparison": cache.get("comparison"),
        "window": {
            "window_id": window.get("window_id"),
            "stream_epoch": window.get("stream_epoch"),
            "remaining_ms": window.get("remaining_ms_conservative"),
            "command_transport_ready": window.get("command_transport_ready"),
        } if window else None,
        "stop_receipt": receipt if receipt and receipt.get("state") == "stopped" else None,
        "last_receipt": receipt,
        "last_stop": getattr(facade, "_last_stop", None),
        "estop_latched": bool(getattr(facade, "estop_latched", False)),
        "last_error": getattr(facade, "last_error", None),
    }
