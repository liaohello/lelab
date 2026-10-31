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
"""Jetson-side model store: pull trained checkpoints home, keep them tidy.

Layout::

    <model_root>/<job_id>/model.json          # manifest (step, size, source)
    <model_root>/<job_id>/.ready              # atomic visibility marker
    <model_root>/<job_id>/checkpoints/<step>/pretrained_model/...
    <model_root>/.trash/<job_id>-<ts>/        # manual purge only (no expiry)

Transfers use rsync's resumable staging; correctness comes from a SHA-256
comparison against the training server before the directory becomes visible.
Retention keeps the newest ``LELAB_LOCAL_MODEL_KEEP`` (default 10) jobs and
*moves* older ones to the trash instead of deleting them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .remote_sync import TrainServerConfig
from .runners import ssh_ops

logger = logging.getLogger(__name__)

DEFAULT_KEEP_LOCAL_MODELS = 10
_SHA_CHUNK = 1024 * 1024


def model_root(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    configured = (values.get("LELAB_MODEL_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Desktop" / "lelab" / "models"


def keep_local_models(env: Mapping[str, str] | None = None) -> int:
    values = os.environ if env is None else env
    raw = (values.get("LELAB_LOCAL_MODEL_KEEP") or "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_KEEP_LOCAL_MODELS
    except ValueError:
        return DEFAULT_KEEP_LOCAL_MODELS


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_SHA_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


_REMOTE_SHA_SCRIPT = (
    "set -eu\n"
    + ssh_ops.EXPAND_PATH_FUNCTION
    + 'path=$(expand_remote_path "$1")\nsha256sum "$path" | awk \'{print $1}\'\n'
)


def _remote_sha256(config: TrainServerConfig, remote_file: str) -> str:
    _code, out = ssh_ops.run_script(config, _REMOTE_SHA_SCRIPT, remote_file, timeout=600.0)
    return out.strip().split()[0] if out.strip() else ""


def _dir_size_mb(path: Path) -> float:
    total = 0
    for file in path.rglob("*"):
        with contextlib.suppress(OSError):
            if file.is_file():
                total += file.stat().st_size
    return round(total / 1024 / 1024, 1)


def read_manifest(job_id: str, root: Path | None = None) -> dict[str, Any] | None:
    path = (root or model_root()) / job_id / "model.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def list_local_models(root: Path | None = None) -> list[dict[str, Any]]:
    """Local models, newest pull first, with on-disk size."""
    base = root or model_root()
    models: list[dict[str, Any]] = []
    if not base.is_dir():
        return models
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name == ".trash":
            continue
        manifest = read_manifest(entry.name, base)
        if manifest is None:
            continue
        models.append(
            {**manifest, "job_id": entry.name, "path": str(entry), "size_mb": _dir_size_mb(entry)}
        )
    models.sort(key=lambda item: float(item.get("pulled_at") or 0), reverse=True)
    return models


def list_trashed_models(root: Path | None = None) -> list[dict[str, Any]]:
    base = root or model_root()
    trash = base / ".trash"
    items: list[dict[str, Any]] = []
    if not trash.is_dir():
        return items
    for entry in sorted(trash.iterdir()):
        if not entry.is_dir():
            continue
        manifest = read_manifest(entry.name, trash) or {}
        items.append(
            {
                "job_id": manifest.get("job_id") or entry.name,
                "trash_name": entry.name,
                "step": manifest.get("step"),
                "size_mb": _dir_size_mb(entry),
                "trashed_at": manifest.get("trashed_at"),
                "path": str(entry),
            }
        )
    items.sort(key=lambda item: float(item.get("trashed_at") or 0), reverse=True)
    return items


def trash_model(job_id: str, *, root: Path | None = None) -> str:
    """Move a local model into ``.trash`` (recoverable until purged)."""
    base = root or model_root()
    source = base / job_id
    if not source.is_dir():
        raise FileNotFoundError(f"local model not found: {job_id}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    trash_dir = base / ".trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    target = trash_dir / f"{job_id}-{stamp}"
    manifest = read_manifest(job_id, base) or {}
    manifest["trashed_at"] = time.time()
    (source / "model.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    with contextlib.suppress(FileNotFoundError):
        (source / ".ready").unlink()
    shutil.move(str(source), str(target))
    return target.name


def restore_model(trash_name: str, *, root: Path | None = None) -> str:
    base = root or model_root()
    source = base / ".trash" / trash_name
    if not source.is_dir():
        raise FileNotFoundError(f"trash entry not found: {trash_name}")
    manifest = read_manifest(trash_name, base / ".trash") or {}
    job_id = str(manifest.get("job_id") or trash_name.rsplit("-", 2)[0])
    target = base / job_id
    if target.exists():
        raise FileExistsError(f"a local model named {job_id} already exists")
    manifest.pop("trashed_at", None)
    (source / "model.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    shutil.move(str(source), str(target))
    (target / ".ready").write_text("restored\n", encoding="utf-8")
    return job_id


def purge_model(trash_name: str, *, root: Path | None = None) -> None:
    """Permanently delete one trash entry (the only irreversible operation)."""
    target = (root or model_root()) / ".trash" / trash_name
    if not target.is_dir():
        raise FileNotFoundError(f"trash entry not found: {trash_name}")
    shutil.rmtree(target)


def enforce_retention(*, keep: int | None = None, root: Path | None = None) -> list[str]:
    """Move everything beyond the newest ``keep`` models into ``.trash``."""
    base = root or model_root()
    limit = keep if keep is not None else keep_local_models()
    models = list_local_models(base)
    moved: list[str] = []
    for model in models[limit:]:
        moved.append(trash_model(model["job_id"], root=base))
    return moved


class ModelPullState:
    """Single-slot progress record for the UI (one transfer at a time)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"phase": "idle"}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)


_PULL_STATE = ModelPullState()


def pull_state() -> dict[str, Any]:
    return _PULL_STATE.snapshot()


def _ssh_transport(config: TrainServerConfig) -> str:
    parts = ["ssh", "-p", str(config.port), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if config.ssh_key is not None:
        parts.extend(["-i", str(config.ssh_key)])
    return " ".join(parts)


def pull_model(
    config: TrainServerConfig,
    *,
    job_id: str,
    remote_pretrained_dir: str,
    step: int | None = None,
    root: Path | None = None,
    timeout: float = 1800.0,
) -> dict[str, Any]:
    """Copy one checkpoint home with SHA-256 verification + atomic publish."""
    base = root or model_root()
    step_value = int(step) if step is not None else 0
    incoming = base / f".{job_id}.incoming"
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(incoming)
    incoming.mkdir(parents=True, exist_ok=True)
    _PULL_STATE.update(phase="transferring", job_id=job_id, step=step_value, started_at=time.time())
    rsync = [
        "rsync",
        "-a",
        "--partial",
        "--info=progress2",
        "-e",
        _ssh_transport(config),
        f"{config.endpoint}:{remote_pretrained_dir.rstrip('/')}/",
        f"{incoming}/",
    ]
    try:
        completed = subprocess.run(rsync, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _PULL_STATE.update(phase="failed", error=f"传输超时（{timeout:.0f}s）")
        raise TimeoutError(f"pull timed out after {timeout:.0f}s") from exc
    if completed.returncode != 0:
        detail = completed.stdout.decode("utf-8", errors="replace")[-300:]
        _PULL_STATE.update(phase="failed", error=detail)
        raise RuntimeError(f"rsync failed: {detail}")

    weights = incoming / "model.safetensors"
    if weights.is_file():
        _PULL_STATE.update(phase="verifying")
        local_sha = sha256_file(weights)
        remote_sha = _remote_sha256(config, f"{remote_pretrained_dir.rstrip('/')}/model.safetensors")
        if remote_sha and local_sha != remote_sha:
            _PULL_STATE.update(phase="failed", error="SHA-256 校验不一致")
            raise RuntimeError("SHA-256 mismatch after transfer; not publishing")

    target = base / job_id
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(target)
    pretrained_target = target / "checkpoints" / f"{step_value:06d}" / "pretrained_model"
    pretrained_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(incoming), str(pretrained_target))
    manifest = {
        "job_id": job_id,
        "step": step_value,
        "source": config.endpoint,
        "remote_path": remote_pretrained_dir,
        "pulled_at": time.time(),
        "size_mb": _dir_size_mb(target),
    }
    (target / "model.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    (target / ".ready").write_text("ok\n", encoding="utf-8")
    moved = enforce_retention(keep=keep_local_models(), root=base)
    _PULL_STATE.update(phase="completed", finished_at=time.time(), moved_to_trash=moved)
    return {**manifest, "path": str(target), "moved_to_trash": moved}
