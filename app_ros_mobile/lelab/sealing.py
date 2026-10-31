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
"""Sealing: turn a finished recording into an immutable, content-addressed dataset.

A sealed dataset may be viewed, trained on, synced and verified. It may not be
recorded into, re-recorded, repaired, overwritten, trashed or deleted. The seal
is a JSON manifest under ``meta/.lelab-sealed.json`` listing every file with
its size and SHA-256, plus a single manifest digest that composite datasets and
training jobs record for reproducibility.

Hashing a multi-gigabyte dataset takes a while, so sealing runs in a background
thread (see :class:`SealManager`) and reports progress through
``GET /datasets/local/{repo_id}/seal``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .__version__ import __version__ as _lelab_version

logger = logging.getLogger(__name__)

SEAL_RELATIVE_PATH = Path("meta") / ".lelab-sealed.json"
SEAL_SCHEMA_VERSION = 1
_HASH_CHUNK_BYTES = 1024 * 1024


class SealError(RuntimeError):
    """Raised when a dataset cannot be sealed, unsealed or is not mutable."""


def _dataset_helpers():
    """Import path helpers lazily: dataset_storage imports this module."""
    from .dataset_storage import lerobot_dataset_root, resolve_dataset_path

    return lerobot_dataset_root, resolve_dataset_path


def seal_manifest_path(
    repo_id: str, dataset_root: str | os.PathLike[str] | None = None
) -> Path:
    _, resolve_dataset_path = _dataset_helpers()
    return resolve_dataset_path(repo_id, dataset_root) / SEAL_RELATIVE_PATH


def read_seal(
    repo_id: str, dataset_root: str | os.PathLike[str] | None = None
) -> dict[str, Any] | None:
    """Return the parsed seal manifest, or ``None`` when the dataset is unsealed."""
    path = seal_manifest_path(repo_id, dataset_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SealError(f"Seal manifest for {repo_id} is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("state") != "sealed":
        raise SealError(f"Seal manifest for {repo_id} is not a completed seal")
    return payload


def is_sealed(repo_id: str, dataset_root: str | os.PathLike[str] | None = None) -> bool:
    """True when the dataset carries a completed seal manifest."""
    try:
        return read_seal(repo_id, dataset_root) is not None
    except SealError:
        # A corrupt manifest must not silently unlock the dataset: treat it as
        # sealed so the operator has to unseal (or repair) explicitly.
        return True


def ensure_mutable(
    repo_id: str, dataset_root: str | os.PathLike[str] | None = None, *, action: str = "modify"
) -> None:
    """Refuse a mutating action on a sealed dataset."""
    if is_sealed(repo_id, dataset_root):
        raise SealError(
            f"Cannot {action} {repo_id}: the dataset is sealed (封存). "
            "Unseal it first if the data really has to change."
        )


def seal_status(
    repo_id: str, dataset_root: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Compact seal state for listings summarised without touching big files."""
    try:
        seal = read_seal(repo_id, dataset_root)
    except SealError as exc:
        return {"sealed": True, "seal_state": "corrupt", "seal_error": str(exc)}
    if seal is None:
        return {"sealed": False, "seal_state": "unsealed"}
    return {
        "sealed": True,
        "seal_state": "sealed",
        "sealed_at": seal.get("sealed_at"),
        "seal_manifest_sha256": seal.get("manifest_sha256"),
        "seal_file_count": seal.get("file_count"),
        "seal_size_bytes": seal.get("size_bytes"),
    }


def _iter_dataset_files(path: Path, seal_path: Path) -> list[Path]:
    """Every regular file that belongs to the dataset payload, sorted.

    Skipped: the seal manifest itself and root-level dot files (``.ready`` is a
    transfer marker, not dataset content).
    """
    files: list[Path] = []
    for current_root, directory_names, file_names in os.walk(path):
        current = Path(current_root)
        if current == path:
            directory_names[:] = [name for name in directory_names if not name.startswith(".")]
        for name in file_names:
            if current == path and name.startswith("."):
                continue
            candidate = current / name
            if candidate == seal_path or candidate.is_symlink() or not candidate.is_file():
                continue
            files.append(candidate)
    files.sort(key=lambda item: item.relative_to(path).as_posix())
    return files


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_digest(entries: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry["size"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(entry["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_seal_manifest(
    repo_id: str,
    dataset_root: str | os.PathLike[str] | None = None,
    *,
    cancel: threading.Event | None = None,
    progress=None,
) -> dict[str, Any]:
    """Hash every payload file and return the seal manifest payload.

    ``progress(files_done, files_total, bytes_done, bytes_total)`` is called
    after each file; ``cancel`` aborts between files.
    """
    from .dataset_storage import dataset_detail

    root, resolve_dataset_path = _dataset_helpers()
    del root
    path = resolve_dataset_path(repo_id, dataset_root)
    if not path.is_dir() or not (path / "meta" / "info.json").is_file():
        raise FileNotFoundError(f"Local LeRobot dataset not found: {repo_id}")

    seal_path = path / SEAL_RELATIVE_PATH
    files = _iter_dataset_files(path, seal_path)
    if not files:
        raise SealError(f"Dataset {repo_id} has no files to seal")

    detail = dataset_detail(repo_id, dataset_root, tree_max_entries=1, tree_max_depth=1)
    fmt = detail.get("format") or {}
    total_bytes = sum(item.stat().st_size for item in files)
    entries: list[dict[str, Any]] = []
    bytes_done = 0
    for index, item in enumerate(files, start=1):
        if cancel is not None and cancel.is_set():
            raise SealError(f"Sealing {repo_id} was cancelled")
        relative = item.relative_to(path).as_posix()
        size = item.stat().st_size
        digest = _sha256_file(item)
        entries.append({"path": relative, "size": size, "sha256": digest})
        bytes_done += size
        if progress is not None:
            progress(index, len(files), bytes_done, total_bytes)

    return {
        "schema_version": SEAL_SCHEMA_VERSION,
        "state": "sealed",
        "repo_id": repo_id,
        "sealed_at": datetime.now(tz=UTC).isoformat(),
        "root": str(path),
        "codebase_version": fmt.get("codebase_version"),
        "robot_type": fmt.get("robot_type"),
        "fps": fmt.get("fps"),
        "total_episodes": fmt.get("total_episodes"),
        "total_frames": fmt.get("total_frames"),
        "file_count": len(entries),
        "size_bytes": total_bytes,
        "manifest_sha256": _manifest_digest(entries),
        "files": entries,
        "tool": {"name": "lelab", "version": _lelab_version},
    }


def write_seal_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Atomically publish a seal manifest next to the dataset metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def unseal_dataset(
    repo_id: str, dataset_root: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Remove the seal manifest, making the dataset mutable again."""
    path = seal_manifest_path(repo_id, dataset_root)
    if not path.is_file():
        return {"repo_id": repo_id, "sealed": False, "state": "unsealed"}
    try:
        path.unlink()
    except OSError as exc:
        raise SealError(f"Could not unseal {repo_id}: {exc}") from exc
    get_seal_manager().forget(repo_id)
    logger.info("Unsealed dataset %s", repo_id)
    return {"repo_id": repo_id, "sealed": False, "state": "unsealed"}


def verify_seal(
    repo_id: str, dataset_root: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Recompute the manifest and compare it with the recorded seal."""
    seal = read_seal(repo_id, dataset_root)
    if seal is None:
        raise SealError(f"Dataset {repo_id} is not sealed")
    current = build_seal_manifest(repo_id, dataset_root)
    expected = {entry["path"]: entry for entry in seal.get("files") or []}
    actual = {entry["path"]: entry for entry in current["files"]}
    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual))
    changed = sorted(
        name
        for name in set(actual) & set(expected)
        if actual[name]["sha256"] != expected[name]["sha256"]
        or actual[name]["size"] != expected[name]["size"]
    )
    ok = not added and not removed and not changed
    return {
        "repo_id": repo_id,
        "ok": ok,
        "manifest_sha256": seal.get("manifest_sha256"),
        "computed_sha256": current["manifest_sha256"],
        "added": added,
        "removed": removed,
        "changed": changed,
    }


class SealManager:
    """Process-wide background sealer, mirroring the dataset sync manager."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}

    # ------------------------------------------------------------------ state
    def status(self, repo_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(repo_id)
        if job is not None:
            return dict(job)
        from .dataset_storage import dataset_summary

        try:
            summary = dataset_summary(repo_id)
        except (FileNotFoundError, ValueError):
            return {"repo_id": repo_id, "state": "unknown"}
        return {
            "repo_id": repo_id,
            "state": "sealed" if summary.get("sealed") else "unsealed",
            "sealed_at": summary.get("sealed_at"),
            "manifest_sha256": summary.get("seal_manifest_sha256"),
            "file_count": summary.get("seal_file_count"),
            "size_bytes": summary.get("seal_size_bytes"),
        }

    def active(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(job) for job in self._jobs.values() if job.get("state") == "sealing"]

    # ------------------------------------------------------------------ start
    def start(
        self,
        repo_id: str,
        dataset_root: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        from .dataset_storage import dataset_integrity, dataset_summary

        with self._lock:
            existing = self._jobs.get(repo_id)
            if existing is not None and existing.get("state") == "sealing":
                return dict(existing)

        summary = dataset_summary(repo_id, dataset_root)
        if summary.get("sealed"):
            return {
                "repo_id": repo_id,
                "state": "sealed",
                "sealed_at": summary.get("sealed_at"),
                "manifest_sha256": summary.get("seal_manifest_sha256"),
            }
        integrity = dataset_integrity(repo_id, dataset_root)
        if integrity.get("status") == "error":
            failing = [
                entry["detail"]
                for entry in integrity.get("checks", [])
                if entry.get("status") == "error"
            ]
            raise SealError(
                f"Cannot seal {repo_id}: integrity check failed ({'; '.join(failing)})"
            )

        cancel = threading.Event()
        job: dict[str, Any] = {
            "repo_id": repo_id,
            "state": "sealing",
            "started_at": time.time(),
            "files_done": 0,
            "files_total": summary.get("file_count"),
            "bytes_done": 0,
            "bytes_total": summary.get("size_bytes"),
            "progress": 0.0,
            "error": None,
        }
        with self._lock:
            self._jobs[repo_id] = job
            self._cancel[repo_id] = cancel
        thread = threading.Thread(
            target=self._run,
            args=(repo_id, dataset_root, job, cancel),
            name=f"seal-{repo_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[repo_id] = thread
        thread.start()
        return dict(job)

    def cancel(self, repo_id: str) -> dict[str, Any]:
        with self._lock:
            cancel = self._cancel.get(repo_id)
            job = self._jobs.get(repo_id)
        if cancel is None or job is None or job.get("state") != "sealing":
            return self.status(repo_id)
        cancel.set()
        return {"repo_id": repo_id, "state": "cancelling"}

    def forget(self, repo_id: str) -> None:
        """Drop in-memory state so the next status read comes from disk."""
        with self._lock:
            self._jobs.pop(repo_id, None)
            self._threads.pop(repo_id, None)
            self._cancel.pop(repo_id, None)

    # ------------------------------------------------------------------- work
    def _run(
        self,
        repo_id: str,
        dataset_root: str | os.PathLike[str] | None,
        job: dict[str, Any],
        cancel: threading.Event,
    ) -> None:
        def progress(files_done: int, files_total: int, bytes_done: int, bytes_total: int) -> None:
            with self._lock:
                job.update(
                    {
                        "files_done": files_done,
                        "files_total": files_total,
                        "bytes_done": bytes_done,
                        "bytes_total": bytes_total,
                        "progress": round(bytes_done / bytes_total, 4) if bytes_total else 0.0,
                    }
                )

        try:
            manifest = build_seal_manifest(
                repo_id, dataset_root, cancel=cancel, progress=progress
            )
            write_seal_manifest(seal_manifest_path(repo_id, dataset_root), manifest)
        except Exception as exc:  # noqa: BLE001 - surfaced through the status payload
            logger.exception("Sealing %s failed", repo_id)
            with self._lock:
                job.update(
                    {
                        "state": "cancelled" if cancel.is_set() else "failed",
                        "error": str(exc),
                        "finished_at": time.time(),
                    }
                )
            return
        with self._lock:
            job.update(
                {
                    "state": "sealed",
                    "finished_at": time.time(),
                    "progress": 1.0,
                    "manifest_sha256": manifest["manifest_sha256"],
                    "sealed_at": manifest["sealed_at"],
                    "file_count": manifest["file_count"],
                    "size_bytes": manifest["size_bytes"],
                }
            )
        logger.info(
            "Sealed %s (%s files, %s bytes)", repo_id, manifest["file_count"], manifest["size_bytes"]
        )


seal_manager = SealManager()


def get_seal_manager() -> SealManager:
    """Return the process-wide sealer used by the API routes."""
    return seal_manager
