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
"""Read-only inspection helpers for locally stored LeRobot datasets."""

from __future__ import annotations

import contextlib
import copy
import json
import math
import os
import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .sealing import ensure_mutable, seal_status

_DEFAULT_ROOT = "~/.cache/huggingface/lerobot"
_MAX_METADATA_BYTES = 10 * 1024 * 1024
TRASH_DIR_NAME = ".trash"
_TRASH_METADATA_NAME = "trash.json"
_CACHE_TTL_SECONDS = 30.0

#: Process-wide cache for the expensive part of dataset listings: walking
#: every video file of every dataset. Detail views always bypass it so an open
#: dialog shows exact numbers; listings reuse a 30s-old size instead of
#: re-scanning terabytes on every poll.
_stats_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_stats_cache_lock = threading.Lock()


def _cached_directory_stats(path: Path, *, ttl_seconds: float = _CACHE_TTL_SECONDS) -> dict[str, Any]:
    key = str(path)
    now = time.monotonic()
    with _stats_cache_lock:
        cached = _stats_cache.get(key)
    if cached is not None and now - cached[0] <= ttl_seconds:
        return copy.deepcopy(cached[1])
    stats = _directory_stats(path)
    with _stats_cache_lock:
        _stats_cache[key] = (now, stats)
        # Bound the cache; listings create one entry per dataset.
        if len(_stats_cache) > 256:
            oldest = sorted(_stats_cache.items(), key=lambda item: item[1][0])[:64]
            for stale_key, _ in oldest:
                _stats_cache.pop(stale_key, None)
    return copy.deepcopy(stats)


def clear_stats_cache() -> None:
    """Drop cached directory sizes (tests and explicit refreshes)."""
    with _stats_cache_lock:
        _stats_cache.clear()


def _json_safe(value: Any) -> Any:
    """Normalize metadata to strict JSON values (notably replacing NaN/Inf)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def lerobot_dataset_root(dataset_root: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the configured local LeRobot dataset root."""
    value = dataset_root if dataset_root is not None else os.environ.get("HF_LEROBOT_HOME", _DEFAULT_ROOT)
    return Path(value).expanduser().resolve(strict=False)


def resolve_dataset_path(
    repo_id: str,
    dataset_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve a repo id under the local cache and reject path traversal.

    Repo ids use Hugging Face's forward-slash notation (for example
    ``team/pick-cube``). Backslashes, absolute paths, ``.``/``..`` components,
    drive separators and control characters are rejected on every platform so
    the same request is safe on Windows and Jetson Linux.
    """
    if not isinstance(repo_id, str) or not repo_id.strip():
        raise ValueError("repo_id must be a non-empty string")
    if repo_id != repo_id.strip() or "\\" in repo_id or "\x00" in repo_id:
        raise ValueError("repo_id contains an invalid path character")

    parts = repo_id.split("/")
    if any(
        not part
        or part in {".", ".."}
        or ":" in part
        or any(ord(character) < 32 for character in part)
        for part in parts
    ):
        raise ValueError("repo_id must contain only safe relative path components")

    root = lerobot_dataset_root(dataset_root)
    candidate = root.joinpath(*parts).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("repo_id resolves outside the dataset root") from exc
    return candidate


def _iso_mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    except OSError:
        return None


def _directory_stats(path: Path) -> dict[str, Any]:
    size_bytes = 0
    file_count = 0
    directory_count = 0
    errors: list[str] = []
    if not path.is_dir():
        return {
            "size_bytes": 0,
            "file_count": 0,
            "directory_count": 0,
            "last_modified": _iso_mtime(path),
            "errors": [],
        }

    def onerror(error: OSError) -> None:
        errors.append(str(error))

    for current, directory_names, file_names in os.walk(path, topdown=True, onerror=onerror, followlinks=False):
        current_path = Path(current)
        # Never account for, or descend through, a symlink target outside the
        # dataset. os.walk already avoids traversal, and filtering makes that
        # behavior explicit across Python/platform versions.
        directory_names[:] = [name for name in directory_names if not (current_path / name).is_symlink()]
        directory_count += len(directory_names)
        for name in file_names:
            file_path = current_path / name
            try:
                if file_path.is_symlink():
                    continue
                size_bytes += file_path.stat().st_size
                file_count += 1
            except OSError as exc:
                errors.append(str(exc))
    return {
        "size_bytes": size_bytes,
        "file_count": file_count,
        "directory_count": directory_count,
        "last_modified": _iso_mtime(path),
        "errors": errors[:20],
    }


def _read_metadata(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        if path.stat().st_size > _MAX_METADATA_BYTES:
            return None, f"metadata exceeds {_MAX_METADATA_BYTES} bytes"
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(value, dict):
        return None, "meta/info.json must contain a JSON object"
    return _json_safe(value), None


def _storage_templates(metadata: dict[str, Any]) -> dict[str, str | None]:
    explicit_keys = {
        "data_path",
        "video_path",
        "image_path",
        "episodes_path",
        "episodes_stats_path",
        "tasks_path",
    }
    return {
        str(key): value
        for key, value in metadata.items()
        if isinstance(key, str)
        and (key in explicit_keys or key.endswith("_path"))
        and (isinstance(value, str) or value is None)
    }


def _feature_rows(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    features = metadata.get("features")
    if not isinstance(features, dict):
        return []
    rows: list[dict[str, Any]] = []
    for name, raw in features.items():
        feature = raw if isinstance(raw, dict) else {}
        dtype = feature.get("dtype")
        storage = "video" if dtype == "video" else "image" if dtype == "image" else "parquet"
        rows.append(
            {
                "name": str(name),
                "dtype": dtype if isinstance(dtype, str) else None,
                "shape": feature.get("shape") if isinstance(feature.get("shape"), list) else None,
                "names": feature.get("names") if isinstance(feature.get("names"), list) else None,
                "storage": storage,
                "info": feature.get("info") if isinstance(feature.get("info"), dict) else None,
            }
        )
    return rows


def _file_node(path: Path, root: Path, *, depth: int, state: dict[str, Any]) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix() if path != root else "."
    try:
        is_symlink = path.is_symlink()
        is_directory = path.is_dir() and not is_symlink
        stat = path.lstat()
    except OSError as exc:
        state["errors"].append(str(exc))
        return {"name": path.name, "path": relative, "type": "unavailable", "size_bytes": None}

    if is_symlink:
        return {"name": path.name, "path": relative, "type": "symlink", "size_bytes": stat.st_size}
    if not is_directory:
        return {"name": path.name, "path": relative, "type": "file", "size_bytes": stat.st_size}

    node: dict[str, Any] = {
        "name": path.name or str(path),
        "path": relative,
        "type": "directory",
        "children": [],
    }
    if depth >= state["max_depth"]:
        state["truncated"] = True
        node["truncated"] = True
        return node
    try:
        children = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
    except OSError as exc:
        state["errors"].append(str(exc))
        node["error"] = str(exc)
        return node
    for child in children:
        if state["entry_count"] >= state["max_entries"]:
            state["truncated"] = True
            node["truncated"] = True
            break
        state["entry_count"] += 1
        node["children"].append(_file_node(child, root, depth=depth + 1, state=state))
    return node


def dataset_file_tree(dataset_path: Path, *, max_entries: int = 500, max_depth: int = 5) -> dict[str, Any]:
    """Return a bounded recursive file tree without following symlinks."""
    if max_entries < 1 or max_depth < 0:
        raise ValueError("max_entries must be positive and max_depth must be non-negative")
    state: dict[str, Any] = {
        "max_entries": max_entries,
        "max_depth": max_depth,
        "entry_count": 0,
        "truncated": False,
        "errors": [],
    }
    tree = _file_node(dataset_path, dataset_path, depth=0, state=state)
    return {
        "tree": tree,
        "entry_count": state["entry_count"],
        "max_entries": max_entries,
        "max_depth": max_depth,
        "truncated": state["truncated"],
        "errors": state["errors"][:20],
    }


def _find_parquet_files(path: Path, *, limit: int) -> list[Path]:
    found: list[Path] = []
    for current, directory_names, file_names in os.walk(path, topdown=True, followlinks=False):
        current_path = Path(current)
        directory_names[:] = sorted(
            name for name in directory_names if not (current_path / name).is_symlink()
        )
        for name in sorted(file_names):
            if name.lower().endswith(".parquet"):
                found.append(current_path / name)
                if len(found) >= limit:
                    return found
    return found


def _load_parquet_module() -> Any | None:
    try:
        import pyarrow.parquet as parquet
    except (ImportError, OSError):
        return None
    return parquet


def parquet_schema(dataset_path: Path, *, file_limit: int = 8) -> dict[str, Any]:
    """Inspect a representative local Parquet schema when pyarrow is present."""
    if file_limit < 1:
        raise ValueError("file_limit must be positive")
    files = _find_parquet_files(dataset_path, limit=file_limit)
    if not files:
        return {"status": "not_found", "reason": "No Parquet files found", "files": [], "fields": []}
    parquet = _load_parquet_module()
    if parquet is None:
        return {
            "status": "unavailable",
            "reason": "pyarrow is not installed or could not be loaded",
            "files": [path.relative_to(dataset_path).as_posix() for path in files],
            "fields": [],
        }
    errors: list[str] = []
    for path in files:
        try:
            schema = parquet.ParquetFile(path).schema_arrow
            fields = [
                {"name": field.name, "type": str(field.type), "nullable": bool(field.nullable)}
                for field in schema
            ]
            return {
                "status": "available",
                "reason": None,
                "source_file": path.relative_to(dataset_path).as_posix(),
                "files": [item.relative_to(dataset_path).as_posix() for item in files],
                "fields": fields,
                "schema": str(schema),
                "errors": errors,
            }
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"{path.relative_to(dataset_path).as_posix()}: {exc}")
    return {
        "status": "error",
        "reason": "Unable to read the sampled Parquet files",
        "files": [path.relative_to(dataset_path).as_posix() for path in files],
        "fields": [],
        "errors": errors,
    }


def dataset_disk_capacity(dataset_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Return only filesystem capacity for the dataset root.

    This intentionally avoids walking dataset files, so it is safe to call on
    the latency-sensitive recording start path even when videos occupy many
    terabytes.
    """
    root = lerobot_dataset_root(dataset_root)
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
        return {
            "probe_path": str(probe),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "percent": round((usage.used / usage.total) * 100, 2) if usage.total else None,
        }
    except OSError:
        return {
            "probe_path": str(probe),
            "total_bytes": None,
            "used_bytes": None,
            "free_bytes": None,
            "percent": None,
        }


def storage_summary(dataset_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Return capacity and local-dataset totals for the configured root."""
    root = lerobot_dataset_root(dataset_root)
    disk = dataset_disk_capacity(root)

    dataset_items: dict[str, dict[str, Any]] = {}
    if root.is_dir():
        for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            directory_names[:] = [
                name
                for name in directory_names
                if not name.startswith(".") and not (current_path / name).is_symlink()
            ]
            if current_path.name == "meta" and "info.json" in file_names:
                dataset_path = current_path.parent
                try:
                    repo_id = dataset_path.relative_to(root).as_posix()
                except ValueError:
                    continue
                if is_hidden_path(dataset_path, root):
                    directory_names[:] = []
                    continue
                metadata, metadata_error = _read_metadata(current_path / "info.json")
                metadata = metadata or {}
                dataset_items[repo_id] = {
                    "repo_id": repo_id,
                    "codebase_version": metadata.get("codebase_version"),
                    "robot_type": metadata.get("robot_type"),
                    "fps": metadata.get("fps"),
                    "total_episodes": metadata.get("total_episodes"),
                    "total_frames": metadata.get("total_frames"),
                    "camera_count": len(_camera_features(metadata)),
                    "duration_seconds": (
                        round(float(metadata["total_frames"]) / float(metadata["fps"]), 3)
                        if isinstance(metadata.get("total_frames"), (int, float))
                        and isinstance(metadata.get("fps"), (int, float))
                        and metadata["fps"]
                        else None
                    ),
                    "finalized": _episodes_index_present(dataset_path),
                    # Sizes stay None here even though detail views compute
                    # exact values: the storage summary must not walk every
                    # video of every dataset on each poll. Use
                    # /datasets/local/{id}/summary for exact numbers.
                    "size_bytes": None,
                    "last_modified": _iso_mtime(dataset_path),
                    "metadata_error": metadata_error,
                }
                directory_names[:] = []

    root_stats = _directory_stats(root)
    sorted_items = [dataset_items[key] for key in sorted(dataset_items)]
    return {
        "root": str(root),
        "exists": root.is_dir(),
        "disk": disk,
        "dataset_storage": root_stats,
        "dataset_count": len(sorted_items),
        "datasets": [item["repo_id"] for item in sorted_items],
        "dataset_items": sorted_items,
    }


def dataset_detail(
    repo_id: str,
    dataset_root: str | os.PathLike[str] | None = None,
    *,
    tree_max_entries: int = 500,
    tree_max_depth: int = 5,
    parquet_file_limit: int = 8,
) -> dict[str, Any]:
    """Return local metadata, storage layout, file tree and Parquet schema.

    Raises:
        ValueError: If ``repo_id`` is unsafe.
        FileNotFoundError: If the repo id is safe but does not exist locally or
            is not a LeRobot dataset (``meta/info.json`` is absent).
    """
    path = resolve_dataset_path(repo_id, dataset_root)
    info_path = path / "meta" / "info.json"
    if not path.is_dir() or not info_path.is_file():
        raise FileNotFoundError(f"Local LeRobot dataset not found: {repo_id}")

    metadata, metadata_error = _read_metadata(info_path)
    metadata = metadata or {}
    errors = [metadata_error] if metadata_error else []
    return {
        "repo_id": repo_id,
        "path": str(path),
        "storage": _directory_stats(path),
        "format": {
            "codebase_version": metadata.get("codebase_version"),
            "robot_type": metadata.get("robot_type"),
            "fps": metadata.get("fps"),
            "total_episodes": metadata.get("total_episodes"),
            "total_frames": metadata.get("total_frames"),
            "total_tasks": metadata.get("total_tasks"),
            "total_videos": metadata.get("total_videos"),
            "chunks_size": metadata.get("chunks_size"),
            "storage_templates": _storage_templates(metadata),
            "features": _feature_rows(metadata),
        },
        "metadata": metadata if not metadata_error else None,
        "metadata_error": metadata_error,
        "file_tree": dataset_file_tree(path, max_entries=tree_max_entries, max_depth=tree_max_depth),
        "parquet_schema": parquet_schema(path, file_limit=parquet_file_limit),
        "errors": errors,
    }


# --------------------------------------------------------------------------
# Listing / integrity helpers
# --------------------------------------------------------------------------

_CAMERA_DTYPES = {"video", "image"}


def is_hidden_path(path: Path, root: Path) -> bool:
    """True when any component below `root` starts with a dot (e.g. `.trash`)."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part.startswith(".") for part in parts)


def _camera_features(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    features = metadata.get("features")
    if not isinstance(features, dict):
        return []
    cameras: list[dict[str, Any]] = []
    for name, raw in features.items():
        feature = raw if isinstance(raw, dict) else {}
        if feature.get("dtype") not in _CAMERA_DTYPES:
            continue
        info = feature.get("info") if isinstance(feature.get("info"), dict) else {}
        cameras.append(
            {
                "name": str(name),
                "dtype": feature.get("dtype"),
                "shape": feature.get("shape") if isinstance(feature.get("shape"), list) else None,
                "video_codec": info.get("video.codec") or info.get("video_codec"),
                "video_fps": info.get("video.fps") or info.get("fps"),
                "video_width": info.get("video.width") or info.get("width"),
                "video_height": info.get("video.height") or info.get("height"),
            }
        )
    return cameras


def _episodes_index_present(dataset_path: Path) -> bool:
    """LeRobot 3.x writes meta/episodes/**; 2.x wrote meta/episodes.jsonl."""
    meta = dataset_path / "meta"
    if (meta / "episodes.jsonl").is_file():
        return True
    episodes_dir = meta / "episodes"
    if not episodes_dir.is_dir():
        return False
    for _current, _dirs, files in os.walk(episodes_dir):
        if any(name.endswith(".parquet") for name in files):
            return True
    return False


def _last_data_mtime(dataset_path: Path) -> str | None:
    """Newest mtime under data/ (fallback: the dataset directory itself)."""
    newest: float | None = None
    data_dir = dataset_path / "data"
    for current, _dirs, files in os.walk(data_dir):
        if not files:
            continue
        for name in files:
            try:
                mtime = (Path(current) / name).stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime
    if newest is None:
        return _iso_mtime(dataset_path)
    return datetime.fromtimestamp(newest, tz=UTC).isoformat()


def dataset_summary(
    repo_id: str,
    dataset_root: str | os.PathLike[str] | None = None,
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Compact, list-friendly dataset description.

    Uses the cached directory walk by default: dataset listings are polled by
    the dashboard, and re-walking multi-GB video trees on every poll would
    dominate the Jetson's I/O.
    """
    path = resolve_dataset_path(repo_id, dataset_root)
    info_path = path / "meta" / "info.json"
    if not path.is_dir() or not info_path.is_file():
        raise FileNotFoundError(f"Local LeRobot dataset not found: {repo_id}")

    metadata, metadata_error = _read_metadata(info_path)
    metadata = metadata or {}
    stats = (
        _cached_directory_stats(path) if use_cache else _directory_stats(path)
    )
    fps = metadata.get("fps")
    total_frames = metadata.get("total_frames")
    duration_seconds: float | None = None
    if isinstance(fps, (int, float)) and fps and isinstance(total_frames, (int, float)):
        duration_seconds = round(float(total_frames) / float(fps), 3)
    cameras = _camera_features(metadata)
    episodes_index = _episodes_index_present(path)
    return {
        "repo_id": repo_id,
        "path": str(path),
        "size_bytes": stats.get("size_bytes"),
        "file_count": stats.get("file_count"),
        "directory_count": stats.get("directory_count"),
        "codebase_version": metadata.get("codebase_version"),
        "robot_type": metadata.get("robot_type"),
        "fps": fps,
        "total_episodes": metadata.get("total_episodes"),
        "total_frames": total_frames,
        "total_tasks": metadata.get("total_tasks"),
        "total_videos": metadata.get("total_videos"),
        "duration_seconds": duration_seconds,
        "camera_count": len(cameras),
        "cameras": cameras,
        "last_modified": stats.get("last_modified"),
        "last_recorded_at": _last_data_mtime(path),
        "finalized": bool(episodes_index),
        "episodes_index": episodes_index,
        "storage_format": metadata.get("storage_format"),
        "metadata_error": metadata_error,
        "scan_errors": stats.get("errors", []),
        **seal_status(repo_id, dataset_root),
    }


def dataset_integrity(
    repo_id: str,
    dataset_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Structural checks that never decode video frames.

    A local dataset must (a) parse, (b) have an episode index — without it
    LeRobot treats the dataset as "not downloaded yet" and tries the Hub — and
    (c) actually contain the data files its metadata promises.
    """
    path = resolve_dataset_path(repo_id, dataset_root)
    if not path.is_dir() or not (path / "meta" / "info.json").is_file():
        raise FileNotFoundError(f"Local LeRobot dataset not found: {repo_id}")

    metadata, metadata_error = _read_metadata(path / "meta" / "info.json")
    metadata = metadata or {}
    checks: list[dict[str, Any]] = []

    def check(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    if metadata_error:
        check("metadata", "error", f"meta/info.json is unreadable: {metadata_error}")
    else:
        missing = [
            key
            for key in ("codebase_version", "fps", "robot_type", "features")
            if metadata.get(key) in (None, {})
        ]
        check(
            "metadata",
            "warning" if missing else "ok",
            f"missing keys: {', '.join(missing)}" if missing else "meta/info.json parsed",
        )

    templates = _storage_templates(metadata)
    check(
        "path-templates",
        "ok" if templates else "warning",
        ", ".join(sorted(templates)) if templates else "no *_path templates in meta/info.json",
    )

    if _episodes_index_present(path):
        check("episodes-index", "ok", "episode index present")
    else:
        check(
            "episodes-index",
            "error",
            "no meta/episodes index; the recording was never finalized (run dataset repair)",
        )

    data_files: list[Path] = []
    if (path / "data").is_dir():
        data_files = [
            item
            for item in (path / "data").rglob("*.parquet")
            if item.is_file() and not item.is_symlink()
        ]
    check(
        "data",
        "ok" if data_files else "error",
        f"{len(data_files)} Parquet file(s)",
    )

    expected_episodes = metadata.get("total_episodes")
    if isinstance(expected_episodes, int) and expected_episodes > 0 and not data_files:
        check("episode-count", "error", f"metadata claims {expected_episodes} episodes but data/ is empty")
    elif isinstance(expected_episodes, int):
        check("episode-count", "ok" if expected_episodes > 0 else "warning", f"{expected_episodes} episode(s)")

    cameras = _camera_features(metadata)
    if cameras:
        video_files = (
            [item for item in (path / "videos").rglob("*") if item.is_file()]
            if (path / "videos").is_dir()
            else []
        )
        check(
            "videos",
            "ok" if video_files else "warning",
            f"{len(video_files)} media file(s) for {len(cameras)} camera feature(s)",
        )

    statuses = {entry["status"] for entry in checks}
    overall = "error" if "error" in statuses else "warning" if "warning" in statuses else "ok"
    return {
        "repo_id": repo_id,
        "path": str(path),
        "status": overall,
        "checks": checks,
        "data_file_count": len(data_files),
        "metadata_error": metadata_error,
    }


# --------------------------------------------------------------------------
# Trash / restore (never a permanent delete from the API)
# --------------------------------------------------------------------------


def _trash_root(dataset_root: str | os.PathLike[str] | None = None) -> Path:
    return lerobot_dataset_root(dataset_root) / TRASH_DIR_NAME


def _read_trash_metadata(entry: Path) -> dict[str, Any]:
    metadata, error = _read_metadata(entry / _TRASH_METADATA_NAME)
    if metadata is None:
        return {"repo_id": None, "error": error}
    return metadata


def trash_dataset(
    repo_id: str,
    dataset_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Move a dataset into `<root>/.trash/<timestamp>-<name>` (recoverable)."""
    root = lerobot_dataset_root(dataset_root)
    path = resolve_dataset_path(repo_id, dataset_root)
    ensure_mutable(repo_id, dataset_root, action="move to trash")
    if path == root or not path.is_dir() or not (path / "meta" / "info.json").is_file():
        raise FileNotFoundError(f"Local LeRobot dataset not found: {repo_id}")

    trash_root = _trash_root(dataset_root)
    trash_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base_name = f"{stamp}-{repo_id.replace('/', '__')}"
    entry = trash_root / base_name
    suffix = 1
    while entry.exists():
        entry = trash_root / f"{base_name}-{suffix}"
        suffix += 1

    size_bytes = _directory_stats(path).get("size_bytes")
    payload = {
        "repo_id": repo_id,
        "trashed_at": datetime.now(UTC).isoformat(),
        "original_path": str(path),
        "size_bytes": size_bytes,
        "entry": entry.name,
    }
    try:
        path.rename(entry)
    except OSError as exc:
        raise ValueError(f"Could not move {repo_id} to trash: {exc}") from exc
    # The move succeeded; metadata is a convenience, not a requirement.
    with contextlib.suppress(OSError):
        (entry / _TRASH_METADATA_NAME).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with _stats_cache_lock:
        _stats_cache.pop(str(path), None)
    return payload


def list_trash(dataset_root: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """List recoverable datasets, newest first."""
    trash_root = _trash_root(dataset_root)
    if not trash_root.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for entry in trash_root.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        meta = _read_trash_metadata(entry)
        entries.append(
            {
                "entry": entry.name,
                "repo_id": meta.get("repo_id"),
                "trashed_at": meta.get("trashed_at") or _iso_mtime(entry),
                "size_bytes": meta.get("size_bytes"),
                "path": str(entry),
                "metadata_error": meta.get("error"),
            }
        )
    entries.sort(key=lambda item: item.get("trashed_at") or "", reverse=True)
    return entries


def restore_dataset(
    repo_id: str,
    dataset_root: str | os.PathLike[str] | None = None,
    *,
    entry: str | None = None,
) -> dict[str, Any]:
    """Restore the newest trash entry for `repo_id` (or an explicit entry)."""
    trash_root = _trash_root(dataset_root)
    target = resolve_dataset_path(repo_id, dataset_root)
    if target.exists():
        raise ValueError(f"Refusing to restore {repo_id}: a dataset already exists at {target}")

    if entry is not None:
        if "/" in entry or "\\" in entry or entry.startswith("."):
            raise ValueError("trash entry must be a plain directory name")
        source = trash_root / entry
        if not source.is_dir():
            raise FileNotFoundError(f"Trash entry not found: {entry}")
    else:
        candidates = [item for item in list_trash(dataset_root) if item.get("repo_id") == repo_id]
        if not candidates:
            raise FileNotFoundError(f"No trashed dataset for {repo_id}")
        source = trash_root / candidates[0]["entry"]

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        metadata_path = source / _TRASH_METADATA_NAME
        if metadata_path.is_file():
            metadata_path.unlink()
        source.rename(target)
    except OSError as exc:
        raise ValueError(f"Could not restore {repo_id}: {exc}") from exc
    return {"repo_id": repo_id, "restored_to": str(target), "entry": source.name}


# Readable aliases for route handlers without coupling this module to FastAPI.
get_storage_summary = storage_summary
get_dataset_detail = dataset_detail
dataset_storage_summary = storage_summary
