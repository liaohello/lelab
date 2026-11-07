# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Read-only inspection of a recorded LeRobot dataset (v3.0 layout).

Backs the `/dataset-viewer` page: per-episode metadata, camera video files and
down-sampled state/action series. Nothing here writes to the dataset.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_VIDEO_COL_RE = re.compile(
    r"^videos/(observation\.images\.[^/]+)/(chunk_index|file_index|from_timestamp|to_timestamp)$"
)
_MAX_POINTS = 1200


def _read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    return table.to_pylist()


def _episode_parquet_files(dataset_path: Path) -> list[Path]:
    return sorted((dataset_path / "meta" / "episodes").rglob("*.parquet"))


def _data_parquet_files(dataset_path: Path) -> list[Path]:
    return sorted((dataset_path / "data").rglob("*.parquet"))


def load_info(dataset_path: Path) -> dict[str, Any]:
    info_path = dataset_path / "meta" / "info.json"
    if not info_path.is_file():
        return {}
    try:
        return json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def camera_names(info: dict[str, Any]) -> list[str]:
    """Short camera names ("right", "up") from the dataset features."""
    names: list[str] = []
    for key in (info.get("features") or {}):
        if key.startswith("observation.images."):
            names.append(key.split(".")[-1])
    return names


def _phase_index(dataset_path: Path) -> dict[int, dict]:
    path = dataset_path / "meta" / "lelab_phase_index.json"
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("version") != 1:
            return {}
        return {int(item["episode_index"]): item for item in doc["episodes"]}
    except (OSError, ValueError, TypeError, KeyError):
        logger.warning("Invalid phase index: %s", path)
        return {}


#: Where the phase numbers in a slice came from.  Emitted with every slice so a
#: consumer can tell a car-clock boundary from any later resampling, and can see
#: that a phase is a *manual operator event*, not a detector output.
PHASE_SLICE_SOURCE = (
    "meta/lelab_phase_index.json <- car events.jsonl phase_started/phase_ended "
    "on the car clock; frames are ceil-aligned onto the exported fps grid; "
    "video offsets are relative to this episode's segment of the shared mp4"
)


def _validated_phases(raw: Any, length: int) -> list[dict[str, Any]]:
    """Keep only phases whose frame window is inside the episode and ordered.

    Bad phase metadata is dropped with a warning rather than rendered as a
    plausible-looking but wrong slice.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    previous_to = 0
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            first = int(entry["frame_from"])
            stop = int(entry["frame_to_exclusive"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Dropping phase without frame bounds: %s", entry)
            continue
        if first < previous_to or stop < first or stop > length:
            logger.warning("Dropping inconsistent phase bounds: %s", entry)
            continue
        out.append({**entry, "frame_from": first, "frame_to_exclusive": stop,
                    "frames": max(0, stop - first)})
        previous_to = stop
    return out


def list_episodes(dataset_path: Path) -> dict[str, Any]:
    """Per-episode index/length/task plus the video file behind each camera."""
    info = load_info(dataset_path)
    cameras = camera_names(info)
    episodes: list[dict[str, Any]] = []
    for parquet in _episode_parquet_files(dataset_path):
        try:
            rows = _read_parquet_rows(parquet)
        except Exception as exc:  # pragma: no cover - corrupt file
            logger.warning("Could not read %s: %s", parquet, exc)
            continue
        chunk = parquet.parent.name.replace("chunk-", "")
        for row in rows:
            index = int(row.get("episode_index", len(episodes)))
            start = row.get("dataset_from_index")
            end = row.get("dataset_to_index")
            length = int(row.get("length") or (end - start if start is not None and end is not None else 0))
            task = row.get("tasks") or row.get("task") or ""
            if isinstance(task, list):
                task = task[0] if task else ""
            videos: dict[str, str] = {}
            video_ranges: dict[str, dict[str, float]] = {}
            for key, value in row.items():
                match = _VIDEO_COL_RE.match(str(key))
                if not match or value is None:
                    continue
                camera, kind = match.group(1).split(".")[-1], match.group(2)
                videos.setdefault(camera, "")
                if kind == "file_index":
                    videos[camera] = f"chunk-{chunk}/file-{int(value):03d}.mp4"
                elif kind == "from_timestamp":
                    video_ranges.setdefault(camera, {})["from_s"] = round(float(value), 3)
                elif kind == "to_timestamp":
                    video_ranges.setdefault(camera, {})["to_s"] = round(float(value), 3)
            episodes.append(
                {
                    "index": index,
                    "length": length,
                    "frames": length,
                    "task": str(task),
                    "videos": videos,
                    # v3 分块存储：多集共用同一个 mp4，靠这两个时间戳定位本集片段。
                    "video_ranges": video_ranges,
                    "cameras": [cam for cam in cameras if cam in videos],
                }
            )
    episodes.sort(key=lambda item: item["index"])
    phase_index = _phase_index(dataset_path)
    fps = float(info.get("fps") or 30)
    for episode in episodes:
        episode["duration_s"] = round(episode["length"] / fps, 1) if fps else None
        metadata = phase_index.get(episode["index"], {})
        episode["source_episode_id"] = metadata.get("episode_id")
        episode["phases"] = _validated_phases(metadata.get("phases", []), episode["length"])
        for phase in episode["phases"]:
            phase["source"] = PHASE_SLICE_SOURCE
            phase["video_ranges"] = {
                camera: {"from_s": round(bounds["from_s"] + float(phase.get("from_s") or 0), 3),
                         "to_s": round(bounds["from_s"] + float(phase.get("to_s") or 0), 3),
                         "within_episode_segment": True}
                for camera, bounds in episode["video_ranges"].items() if "from_s" in bounds
            }
        episode["phase_source"] = PHASE_SLICE_SOURCE if episode["phases"] else None
    return {
        "repo_id": dataset_path.name,
        "fps": fps,
        "robot_type": info.get("robot_type"),
        "total_episodes": info.get("total_episodes") or len(episodes),
        "total_frames": info.get("total_frames"),
        "cameras": cameras,
        "episodes": episodes,
    }


def video_path(dataset_path: Path, episode: int, camera: str) -> Path | None:
    """Locate the mp4 for one episode/camera, with a path-traversal guard."""
    if not camera.startswith("observation.images."):
        camera = f"observation.images.{camera}"
    if "/" in camera or ".." in camera:
        return None
    directory = dataset_path / "videos" / camera
    if not directory.is_dir():
        return None
    listing = list_episodes(dataset_path)
    entry = next((e for e in listing["episodes"] if e["index"] == int(episode)), None)
    if entry is None:
        return None
    relative = entry["videos"].get(camera.split(".")[-1])
    if not relative:
        # Fall back to the convention chunk-000/file-<episode>.mp4.
        candidate = directory / "chunk-000" / f"file-{int(episode):03d}.mp4"
        return candidate if candidate.is_file() else None
    target = directory / relative
    return target if target.is_file() else None


def episode_timeseries(dataset_path: Path, episode: int, *, max_points: int = _MAX_POINTS,
                       phase: str | None = None) -> dict[str, Any]:
    """State/action series for one episode, uniformly down-sampled.

    With ``phase`` the series is restricted to that phase's frame window and the
    response carries the slice provenance; without it the whole episode is used.
    """
    rows: list[dict[str, Any]] = []
    want = int(episode)
    slice_info: dict[str, Any] | None = None
    if phase is not None:
        slice_info = episode_phase_slice(dataset_path, want, phase)
        if not slice_info.get("available"):
            return {"episode": want, "points": 0, "t": [], "series": {},
                    "phase_slice": slice_info}
    for parquet in _data_parquet_files(dataset_path):
        try:
            data = _read_parquet_rows(parquet)
        except Exception as exc:  # pragma: no cover
            logger.warning("Could not read %s: %s", parquet, exc)
            continue
        rows.extend(row for row in data if int(row.get("episode_index", -1)) == want)
        if rows:
            break
    if not rows:
        return {"episode": want, "points": 0, "t": [], "series": {},
                "phase_slice": slice_info}
    episode_length = len(rows)
    if slice_info is not None:
        first = slice_info["frame_from"]
        stop = slice_info["frame_to_exclusive"]
        rows = [row for row in rows
                if first <= int(row.get("frame_index", -1)) < stop]
        if not rows:
            return {"episode": want, "points": 0, "t": [], "series": {},
                    "phase_slice": {**slice_info, "available": False,
                                    "reason": "phase_window_has_no_samples"}}
    rows.sort(key=lambda row: float(row.get("timestamp") or row.get("frame_index") or 0))
    step = max(1, len(rows) // max_points)
    sampled = rows[::step]

    def series_for(key: str) -> list[list[float]]:
        out: list[list[float]] = []
        for row in sampled:
            value = row.get(key)
            if value is None:
                continue
            out.append([float(v) for v in value])
        return out

    series: dict[str, Any] = {}
    for key, label in (("observation.state", "state"), ("action", "action")):
        values = series_for(key)
        if values:
            series[label] = values
    timestamps = [round(float(row.get("timestamp") or 0), 3) for row in sampled]
    payload = {
        "episode": want,
        "points": len(sampled),
        "total_frames": len(rows),
        "t": timestamps,
        "series": series,
        "phases": _validated_phases(
            _phase_index(dataset_path).get(want, {}).get("phases", []), episode_length),
    }
    if slice_info is not None:
        payload["phase_slice"] = slice_info
        payload["slice_source"] = PHASE_SLICE_SOURCE
    return payload


def episode_phase_slice(dataset_path: Path, episode: int, phase: str) -> dict[str, Any]:
    """Locate one manual phase inside an episode: frame window, seconds, video.

    Every number is returned together with its source so the caller cannot
    mistake a phase slice for a re-detected segment.
    """
    listing = list_episodes(dataset_path)
    entry = next((e for e in listing["episodes"] if e["index"] == int(episode)), None)
    if entry is None:
        return {"available": False, "reason": "episode_not_found",
                "episode": int(episode), "phase": phase, "source": PHASE_SLICE_SOURCE}
    phases = entry.get("phases") or []
    names = [p.get("phase") for p in phases]
    match = next((p for p in phases if p.get("phase") == phase), None)
    if match is None:
        return {"available": False, "reason": "phase_not_in_episode",
                "episode": int(episode), "phase": phase,
                "available_phases": names, "source": PHASE_SLICE_SOURCE}
    fps = float(listing.get("fps") or 30)
    first = int(match["frame_from"])
    stop = int(match["frame_to_exclusive"])
    return {
        "available": True,
        "episode": int(episode),
        "phase": phase,
        "available_phases": names,
        "frame_from": first,
        "frame_to_exclusive": stop,
        "frames": max(0, stop - first),
        "from_s": round(first / fps, 3) if fps else None,
        "to_s": round(stop / fps, 3) if fps else None,
        "episode_from_s": match.get("from_s"),
        "episode_to_s": match.get("to_s"),
        "video_ranges": match.get("video_ranges") or {},
        "videos": entry.get("videos") or {},
        "car_time_ns": {"started_time_ns": match.get("started_time_ns"),
                        "ended_time_ns": match.get("ended_time_ns")},
        "source": PHASE_SLICE_SOURCE,
        "interval": "half-open [frame_from, frame_to_exclusive)",
    }
