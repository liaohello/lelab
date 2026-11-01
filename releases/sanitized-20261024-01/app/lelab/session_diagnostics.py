"""Sidecar diagnostics for LeLab recording sessions.

The standard LeRobot dataset timestamp is ``frame_index / fps`` by design.  It
is kept unchanged.  Real monotonic sample times, per-camera frame ages, cycle
overruns and both the raw demonstration target and the safety-clamped command
are written to a JSONL sidecar outside the dataset directory, keyed by
episode/frame index.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .rig import (
    JOINT_NAMES,
    MOTOR_TO_URDF,
    SIDE_JOINTS,
    bimanual_snapshot_from_observation,
)


def _default_base() -> Path:
    return Path(
        os.getenv(
            "LELAB_SESSION_DIAGNOSTICS_DIR",
            str(
                Path(os.getenv("HF_LEROBOT_HOME", "~/.cache/huggingface/lerobot")).expanduser()
                / "lelab_sessions"
            ),
        )
    ).expanduser()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def camera_health(robot: Any, configured_names: list[str]) -> list[dict[str, Any]]:
    """Inspect camera state without opening a device or consuming a frame."""

    cameras = getattr(robot, "cameras", {}) or {}
    now = time.perf_counter()
    health: list[dict[str, Any]] = []
    for name in configured_names:
        camera = cameras.get(name)
        if camera is None:
            health.append(
                {
                    "name": name,
                    "connected": False,
                    "age_ms": None,
                    "stale": True,
                    "error": "camera not present in the connected robot",
                }
            )
            continue
        connected = bool(getattr(camera, "is_connected", False))
        timestamp = getattr(camera, "latest_timestamp", None)
        age_s = now - timestamp if timestamp is not None else None
        configured_fps = getattr(camera, "fps", None) or 30
        stale_after = max(1.0, 3.0 / float(configured_fps))
        health.append(
            {
                "name": name,
                "connected": connected,
                "age_ms": None if age_s is None else round(age_s * 1000.0, 2),
                "stale": (not connected) or age_s is None or age_s > stale_after,
                "error": None if connected else "camera disconnected",
            }
        )
    return health


def _action_sides(action: dict[str, Any] | None) -> dict[str, dict[str, float]]:
    sides: dict[str, dict[str, float]] = {"left": {}, "right": {}}
    for key, value in (action or {}).items():
        for side in ("left", "right"):
            prefix = f"{side}_"
            if key.startswith(prefix):
                sides[side][key[len(prefix) :].removesuffix(".pos")] = float(value)
    return sides


def build_snapshot(
    payload: dict[str, Any],
    *,
    phase: str,
    mode: str,
    configured_cameras: list[str],
    robot: Any,
) -> dict[str, Any]:
    """Build one same-cycle UI snapshot; never substitute zeros for missing data."""

    observation = payload.get("observation") or {}
    errors: list[str] = []
    if mode == "bimanual":
        arms, errors = bimanual_snapshot_from_observation(observation)
        actions = {
            "raw_target": _action_sides(payload.get("raw_action")),
            "sent_target": _action_sides(payload.get("sent_action")),
        }
    else:
        import math

        joints: dict[str, float] = {}
        degrees: dict[str, float] = {}
        for motor, urdf_name in MOTOR_TO_URDF.items():
            key = f"{motor}.pos"
            if key not in observation:
                errors.append(f"missing {key}")
                continue
            value = float(observation[key])
            degrees[motor] = value
            joints[urdf_name] = value * math.pi / 180.0
        arms = {"single": {"joints_rad": joints, "joints_deg": degrees}}
        actions = {
            "raw_target": _action_sides(payload.get("raw_action")),
            "sent_target": _action_sides(payload.get("sent_action")),
        }

    return {
        "mode": mode,
        "phase": phase,
        "sequence": None,
        "monotonic_s": time.monotonic(),
        "cycle_started_monotonic_s": payload.get("cycle_started_monotonic_s"),
        "recorded": bool(payload.get("recorded")),
        "frame_index": payload.get("frame_index"),
        "episode_index": payload.get("episode_index"),
        "standard_timestamp_s": payload.get("standard_timestamp_s"),
        "arms": arms,
        "actions": actions,
        "errors": errors,
        "camera_health": camera_health(robot, configured_cameras),
        "timings_ms": payload.get("timings_ms", {}),
    }


class SessionDiagnostics:
    """Per-session JSONL + manifest writer, outside the standard dataset tree."""

    def __init__(
        self,
        *,
        dataset_repo_id: str,
        dataset_root: str | Path,
        mode: str,
        metadata: dict[str, Any],
        fps: int,
        base_dir: str | Path | None = None,
    ):
        safe_name = dataset_repo_id.replace("/", "__").replace(" ", "_")
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        base = Path(base_dir) if base_dir is not None else _default_base()
        self.root = base / safe_name / f"{stamp}_{os.getpid()}"
        self.root.mkdir(parents=True, exist_ok=False)
        self.frames_path = self.root / "frames.jsonl"
        self.manifest_path = self.root / "session.json"
        self._lock = threading.Lock()
        self._closed = False
        self._frame_handle = self.frames_path.open("w", encoding="utf-8")
        self._cycles_total = 0
        self._cycles_recorded = 0
        self._reset_cycles = 0
        self._write_manifest(
            {
                "schema": "lelab.bimanual.diagnostics.v1",
                "dataset_repo_id": dataset_repo_id,
                "dataset_root": str(dataset_root),
                "mode": mode,
                "fps": fps,
                "joint_names": list(JOINT_NAMES if mode == "bimanual" else SIDE_JOINTS),
                "sync": metadata.get("sync"),
                "action_semantics": metadata.get("action_semantics"),
                "safety": metadata.get("safety"),
                "devices": metadata.get("devices"),
                "standard_timestamp": "frame_index / fps (LeRobot writer)",
                "diagnostic_time_source": "time.perf_counter()/time.monotonic() on the control host",
                "created_at": time.time(),
                "status": "recording",
            }
        )

    @property
    def path(self) -> str:
        return str(self.root)

    def _write_manifest(self, payload: dict[str, Any]) -> None:
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_jsonable(payload), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.manifest_path)

    def write_cycle(self, payload: dict[str, Any], snapshot: dict[str, Any]) -> None:
        if self._closed:
            return
        with self._lock:
            self._cycles_total += 1
            if not payload.get("recorded"):
                self._reset_cycles += 1
                return
            self._cycles_recorded += 1
            record = {
                "episode_index": payload.get("episode_index"),
                "frame_index": payload.get("frame_index"),
                "standard_timestamp_s": payload.get("standard_timestamp_s"),
                "cycle_started_monotonic_s": payload.get("cycle_started_monotonic_s"),
                "cycle_started_wall_s": payload.get("cycle_started_wall_s"),
                "elapsed_s": payload.get("elapsed_s"),
                "raw_action": _action_sides(payload.get("raw_action")),
                "sent_action": _action_sides(payload.get("sent_action")),
                "camera_health": snapshot.get("camera_health"),
                "timings_ms": payload.get("timings_ms"),
                "snapshot_errors": snapshot.get("errors"),
            }
            self._frame_handle.write(json.dumps(_jsonable(record), sort_keys=True) + "\n")

    def close(self, summary: dict[str, Any]) -> None:
        if self._closed:
            return
        with self._lock:
            self._closed = True
            self._frame_handle.flush()
            self._frame_handle.close()
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            payload.update(
                {
                    "status": summary.get("status", "completed"),
                    "finished_at": time.time(),
                    "cycles_total": self._cycles_total,
                    "cycles_recorded": self._cycles_recorded,
                    "reset_cycles": self._reset_cycles,
                    "summary": summary,
                }
            )
            self._write_manifest(payload)
