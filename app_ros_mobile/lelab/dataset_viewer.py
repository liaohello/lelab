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
    fps = float(info.get("fps") or 30)
    for episode in episodes:
        episode["duration_s"] = round(episode["length"] / fps, 1) if fps else None
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


def episode_timeseries(dataset_path: Path, episode: int, *, max_points: int = _MAX_POINTS) -> dict[str, Any]:
    """State/action series for one episode, uniformly down-sampled."""
    rows: list[dict[str, Any]] = []
    want = int(episode)
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
        return {"episode": want, "points": 0, "t": [], "series": {}}
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
    return {
        "episode": want,
        "points": len(sampled),
        "total_frames": len(rows),
        "t": timestamps,
        "series": series,
    }
