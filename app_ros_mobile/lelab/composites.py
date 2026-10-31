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
"""Composite datasets: several sealed recordings trained on as one dataset.

The source datasets are never modified. A composite is a *derived* dataset that
LeRobot's own merge operation writes on the **training server** (the only place
it is ever built), under the deterministic id ``local/composite_<hash>``. The
hash covers the ordered member list, each member's metadata fingerprint and its
seal digest, so changing a source recording always produces a new composite
instead of silently reusing a stale one.

This module owns the Jetson-side planning (compatibility checks, hashing,
estimated size), the remote build lifecycle and the remote trash/restore. The
actual build runs :mod:`lelab.scripts.build_composite`, streamed over SSH so
the training server needs no LeLab install.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import posixpath
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .__version__ import __version__ as _lelab_version
from .dataset_storage import dataset_detail, dataset_summary, lerobot_dataset_root, resolve_dataset_path
from .remote_sync import TrainServerConfig, ssh_base_command
from .runners import ssh_ops

logger = logging.getLogger(__name__)

COMPOSITE_SCHEMA_VERSION = 1
COMPOSITE_NAMESPACE = "local"
COMPOSITE_PREFIX = "composite_"
COMPOSITE_HASH_LENGTH = 16
MIN_MEMBERS = 2
MAX_MEMBERS = 8
DEFAULT_STORAGE_FORMAT = "v3.0"

_LOG_TAIL_LINES = 60
_PROGRESS_PREFIX = "LELAB_PROGRESS "
_REMOTE_TIMEOUT = 120.0

WeightsMode = Literal["frames", "equal", "custom"]


class CompositeError(RuntimeError):
    """A human-readable composite-dataset failure."""


def composite_repo_id(composite_hash: str) -> str:
    return f"{COMPOSITE_NAMESPACE}/{COMPOSITE_PREFIX}{composite_hash}"


def is_composite_repo_id(repo_id: str) -> bool:
    namespace, _, name = repo_id.partition("/")
    return namespace == COMPOSITE_NAMESPACE and name.startswith(COMPOSITE_PREFIX)


def composite_hash_of(repo_id: str) -> str | None:
    if not is_composite_repo_id(repo_id):
        return None
    return repo_id.split("/", 1)[1][len(COMPOSITE_PREFIX) :]


def _canonical_digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_features(features: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Feature rows keyed by name, keeping only layout-relevant video info."""
    layout_keys = {
        "video.codec",
        "video.width",
        "video.height",
        "video.fps",
        "video.is_depth_map",
    }

    def normalized(row: dict[str, Any]) -> dict[str, Any]:
        info = row.get("info") if isinstance(row.get("info"), dict) else {}
        return {
            "dtype": row.get("dtype"),
            "shape": row.get("shape"),
            "names": row.get("names"),
            "info": {key: value for key, value in info.items() if key in layout_keys},
        }

    return {str(row.get("name")): normalized(row) for row in features}


@dataclass(frozen=True, slots=True)
class CompositePlan:
    """Everything the builder and the API need to describe one composite."""

    repo_ids: tuple[str, ...]
    members: tuple[dict[str, Any], ...]
    blockers: tuple[dict[str, Any], ...]
    warnings: tuple[dict[str, Any], ...]
    estimated_size_bytes: int
    weights_mode: WeightsMode = "frames"
    weights: tuple[float, ...] | None = None
    composite_hash: str | None = None

    @property
    def ok(self) -> bool:
        return not self.blockers

    @property
    def composite_repo_id(self) -> str | None:
        return composite_repo_id(self.composite_hash) if self.composite_hash else None

    def to_report(
        self, *, cached: bool | None = None, remote_state: str | None = None
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            "ok": self.ok,
            "repo_ids": list(self.repo_ids),
            "members": [dict(member) for member in self.members],
            "blockers": [dict(blocker) for blocker in self.blockers],
            "warnings": [dict(warning) for warning in self.warnings],
            "estimated_size_bytes": self.estimated_size_bytes,
            "composite_hash": self.composite_hash,
            "composite_repo_id": self.composite_repo_id,
            "weights_mode": self.weights_mode,
            "weights": list(self.weights) if self.weights is not None else None,
        }
        if cached is not None:
            report["cached"] = cached
        if remote_state is not None:
            report["remote_state"] = remote_state
        return report

    def to_plan_payload(self) -> dict[str, Any]:
        """The JSON contract consumed by ``lelab.scripts.build_composite``."""
        if self.composite_hash is None or self.composite_repo_id is None:
            raise CompositeError("the plan is not buildable: it still has blockers")
        return {
            "schema_version": COMPOSITE_SCHEMA_VERSION,
            "composite_hash": self.composite_hash,
            "composite_repo_id": self.composite_repo_id,
            "repo_ids": list(self.repo_ids),
            "members": [
                {
                    "repo_id": member["repo_id"],
                    "meta_fingerprint_sha256": member["meta_fingerprint_sha256"],
                    "seal_manifest_sha256": member.get("seal_manifest_sha256"),
                    "total_episodes": member.get("total_episodes"),
                    "total_frames": member.get("total_frames"),
                }
                for member in self.members
            ],
            "merge": {"concatenate_videos": False, "concatenate_data": False},
            "estimated_size_bytes": self.estimated_size_bytes,
            "weights": {
                "mode": self.weights_mode,
                "values": list(self.weights) if self.weights is not None else None,
            },
            "created_by": {"tool": "lelab", "version": _lelab_version},
        }


def _blocker(code: str, message: str, repo_ids: list[str] | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "repo_ids": repo_ids or []}


def member_fingerprint(
    repo_id: str, dataset_root: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Fingerprint a *local* member with the same algorithm the builder uses."""
    from .scripts.build_composite import meta_fingerprint, meta_fingerprint_sha256

    path = resolve_dataset_path(repo_id, dataset_root)
    fingerprint = meta_fingerprint(path)
    fingerprint["meta_fingerprint_sha256"] = meta_fingerprint_sha256(path)
    fingerprint["path"] = str(path)
    return fingerprint


def _normalize_weights(
    repo_ids: list[str],
    weights_mode: WeightsMode,
    weights: list[float] | None,
) -> tuple[list[float] | None, list[dict[str, Any]]]:
    """Return (normalized weights, blockers) for the requested sampling mode."""
    count = len(repo_ids)
    if weights_mode == "frames":
        return None, []
    if weights_mode == "equal":
        return [round(1.0 / count, 8)] * count, []
    if weights_mode != "custom":
        return None, [_blocker("weights-mode", f"unknown weights mode: {weights_mode!r}")]
    if weights is None or len(weights) != count:
        received = 0 if weights is None else len(weights)
        return None, [
            _blocker("weights-length", f"自定义权重需要 {count} 个数值（每个数据集一个），收到 {received} 个")
        ]
    if any(weight is None or float(weight) <= 0 for weight in weights):
        return None, [_blocker("weights-value", "自定义权重必须全部大于 0")]
    total = sum(float(weight) for weight in weights)
    return [round(float(weight) / total, 8) for weight in weights], []


def plan_composite(
    repo_ids: list[str],
    dataset_root: str | os.PathLike[str] | None = None,
    *,
    weights_mode: WeightsMode = "frames",
    weights: list[float] | None = None,
) -> CompositePlan:
    """Check compatibility and build the deterministic plan for a composite."""
    members: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    ordered = [str(repo_id).strip() for repo_id in repo_ids if str(repo_id).strip()]
    if len(ordered) < MIN_MEMBERS:
        blockers.append(_blocker("member-count", "至少需要选择 2 个数据集"))
    if len(ordered) > MAX_MEMBERS:
        blockers.append(_blocker("member-count", f"一次最多组合 {MAX_MEMBERS} 个数据集"))
    duplicates = sorted({repo_id for repo_id in ordered if ordered.count(repo_id) > 1})
    if duplicates:
        blockers.append(_blocker("duplicate", f"数据集重复选择：{', '.join(duplicates)}", duplicates))

    normalized_weights, weight_blockers = _normalize_weights(ordered, weights_mode, weights)
    blockers.extend(weight_blockers)

    detail: dict[str, dict[str, Any]] = {}
    fingerprints: dict[str, dict[str, Any]] = {}
    for repo_id in ordered:
        try:
            summary = dict(dataset_summary(repo_id, dataset_root))
        except (FileNotFoundError, ValueError, OSError) as exc:
            blockers.append(
                _blocker(
                    "not-local",
                    f"{repo_id}: 本地不存在或不是 LeRobot 数据集"
                    f"（Hub 数据集需先下载到本地）：{exc}",
                    [repo_id],
                )
            )
            continue
        try:
            summary["features"] = dataset_detail(
                repo_id, dataset_root, tree_max_entries=1, tree_max_depth=1
            )["format"]["features"]
        except (FileNotFoundError, ValueError, OSError):
            summary["features"] = []
        detail[repo_id] = summary
        if not summary.get("finalized"):
            blockers.append(
                _blocker("not-finalized", f"{repo_id}: 录制未完成（缺少 episode 索引）", [repo_id])
            )
        if not summary.get("sealed"):
            blockers.append(
                _blocker("not-sealed", f"{repo_id}: 尚未封存，封存后才能参与组合", [repo_id])
            )
        try:
            fingerprints[repo_id] = member_fingerprint(repo_id, dataset_root)
        except Exception as exc:  # noqa: BLE001 - reported as a blocker
            blockers.append(_blocker("fingerprint", f"{repo_id}: 无法计算指纹（{exc}）", [repo_id]))

    if detail:
        first_id = next(iter(detail))
        reference = detail[first_id]
        for repo_id, member in detail.items():
            if repo_id == first_id:
                continue
            if member.get("codebase_version") != reference.get("codebase_version"):
                blockers.append(
                    _blocker(
                        "codebase-version",
                        f"{repo_id}: 数据格式版本 {member.get('codebase_version')} 与 "
                        f"{first_id} 的 {reference.get('codebase_version')} 不一致",
                        [repo_id, first_id],
                    )
                )
            if member.get("fps") != reference.get("fps"):
                blockers.append(
                    _blocker(
                        "fps",
                        f"{repo_id}: fps {member.get('fps')} 与 {first_id} 的 "
                        f"{reference.get('fps')} 不一致（本期不做重采样）",
                        [repo_id, first_id],
                    )
                )
            if member.get("robot_type") != reference.get("robot_type"):
                blockers.append(
                    _blocker(
                        "robot-type",
                        f"{repo_id}: 机械臂类型 {member.get('robot_type')} 与 {first_id} 的 "
                        f"{reference.get('robot_type')} 不一致",
                        [repo_id, first_id],
                    )
                )
            storage_format = member.get("storage_format")
            if storage_format != reference.get("storage_format") or (
                storage_format is not None and storage_format != DEFAULT_STORAGE_FORMAT
            ):
                blockers.append(
                    _blocker(
                        "storage-format",
                        f"{repo_id}: 存储格式 {storage_format} 不受支持"
                        "（需与其它成员一致且为默认格式）",
                        [repo_id, first_id],
                    )
                )

    if detail:
        first_id = next(iter(detail))
        reference_features = _normalized_features(detail[first_id].get("features") or [])
        reference_keys = set(reference_features)
        for repo_id, member in detail.items():
            if repo_id == first_id:
                continue
            features = _normalized_features(member.get("features") or [])
            member_keys = set(features)
            if reference_keys != member_keys:
                missing = sorted(reference_keys - member_keys)
                extra = sorted(member_keys - reference_keys)
                parts = []
                if missing:
                    parts.append(f"缺少 {', '.join(missing)}")
                if extra:
                    parts.append(f"多出 {', '.join(extra)}")
                blockers.append(
                    _blocker(
                        "feature-keys",
                        f"{repo_id}: 字段与 {first_id} 不一致（{'；'.join(parts)}）；"
                        "摄像头命名不同必须重新录制而不是重命名",
                        [repo_id, first_id],
                    )
                )
                continue
            for key in sorted(reference_keys):
                expected = reference_features[key]
                actual = features[key]
                if (
                    expected["dtype"] != actual["dtype"]
                    or expected["shape"] != actual["shape"]
                    or expected["names"] != actual["names"]
                ):
                    blockers.append(
                        _blocker(
                            "feature-shape",
                            f"{repo_id}: 字段 {key} 的 dtype/shape/关节顺序与 {first_id} 不一致",
                            [repo_id, first_id],
                        )
                    )
                if expected.get("info") != actual.get("info"):
                    blockers.append(
                        _blocker(
                            "camera-info",
                            f"{repo_id}: 摄像头 {key} 的分辨率/编码/fps 与 {first_id} 不一致",
                            [repo_id, first_id],
                        )
                    )

    for repo_id in ordered:
        member = detail.get(repo_id)
        fingerprint = fingerprints.get(repo_id)
        if member is None:
            continue
        members.append(
            {
                "repo_id": repo_id,
                "codebase_version": member.get("codebase_version"),
                "robot_type": member.get("robot_type"),
                "fps": member.get("fps"),
                "total_episodes": member.get("total_episodes"),
                "total_frames": member.get("total_frames"),
                "duration_seconds": member.get("duration_seconds"),
                "size_bytes": member.get("size_bytes"),
                "storage_format": member.get("storage_format"),
                "cameras": member.get("cameras") or [],
                "sealed": bool(member.get("sealed")),
                "sealed_at": member.get("sealed_at"),
                "seal_manifest_sha256": member.get("seal_manifest_sha256"),
                "meta_fingerprint_sha256": (fingerprint or {}).get("meta_fingerprint_sha256"),
            }
        )

    estimated = sum(int(member.get("size_bytes") or 0) for member in members)
    composite_hash = None
    if not blockers:
        composite_hash = _canonical_digest(
            {
                "schema_version": COMPOSITE_SCHEMA_VERSION,
                "merge": {"concatenate_videos": False, "concatenate_data": False},
                "members": [
                    {
                        "repo_id": member["repo_id"],
                        "meta_fingerprint_sha256": member["meta_fingerprint_sha256"],
                        "seal_manifest_sha256": member["seal_manifest_sha256"],
                    }
                    for member in members
                ],
            }
        )[:COMPOSITE_HASH_LENGTH]

    return CompositePlan(
        repo_ids=tuple(ordered),
        members=tuple(members),
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        estimated_size_bytes=estimated,
        weights_mode=weights_mode,
        weights=tuple(normalized_weights) if normalized_weights is not None else None,
        composite_hash=composite_hash,
    )


# ---------------------------------------------------------------------------
# Remote helpers (the composite only ever lives on the training server)
# ---------------------------------------------------------------------------


def train_server_config() -> TrainServerConfig:
    try:
        return TrainServerConfig.from_env()
    except Exception as exc:  # noqa: BLE001 - DatasetSyncError and friends
        raise CompositeError(f"训练机未配置：{exc}") from exc


def _datasets_root(config: TrainServerConfig) -> str:
    return posixpath.join(config.root, "datasets")


def remote_status(repo_id: str, *, config: TrainServerConfig | None = None) -> dict[str, Any]:
    """Read a composite's marker and ``.ready`` state from the training server."""
    config = config or train_server_config()
    dataset_dir = posixpath.join(_datasets_root(config), *repo_id.split("/"))
    ready = ssh_ops.read_remote_file(config, posixpath.join(dataset_dir, ".ready"), max_bytes=256)
    if ready is None:
        return {"state": "missing", "repo_id": repo_id}
    marker = ssh_ops.read_remote_file(
        config, posixpath.join(dataset_dir, "meta", "lelab-composite.json")
    )
    manifest: dict[str, Any] | None = None
    if marker:
        with contextlib.suppress(ValueError):
            manifest = json.loads(marker)
    return {
        "state": "ready",
        "repo_id": repo_id,
        "build_id": ready.strip(),
        "manifest": manifest,
    }


_LIST_SOURCE = """
import json, os, sys
from pathlib import Path

datasets = Path(os.path.expanduser(sys.argv[1])) / "datasets"
items = []
for marker in sorted(datasets.glob("local/composite_*/meta/lelab-composite.json")):
    dataset = marker.parent.parent
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    items.append(
        {
            "repo_id": payload.get("composite_repo_id")
            or str(dataset.relative_to(datasets)).replace(os.sep, "/"),
            "hash": payload.get("composite_hash"),
            "members": payload.get("repo_ids") or [],
            "built_at": payload.get("built_at"),
            "size_bytes": payload.get("size_bytes"),
            "file_count": payload.get("file_count"),
            "estimated_size_bytes": payload.get("estimated_size_bytes"),
            "weights_mode": (payload.get("weights") or {}).get("mode", "frames"),
            "ready": (dataset / ".ready").is_file(),
        }
    )
print(json.dumps(items))
"""


def list_remote_composites(*, config: TrainServerConfig | None = None) -> list[dict[str, Any]]:
    """List every composite stored on the training server."""
    config = config or train_server_config()
    return_code, output = ssh_ops.run_python(config, _LIST_SOURCE, config.root)
    if return_code != 0:
        raise CompositeError(
            f"无法读取训练机上的组合数据集（ssh exit {return_code}）：{output.strip()[-300:]}"
        )
    try:
        payload = json.loads(output.strip().splitlines()[-1]) if output.strip() else []
    except (ValueError, IndexError) as exc:
        raise CompositeError(f"训练机返回了无法解析的组合数据集列表：{output.strip()[-300:]}") from exc
    return payload if isinstance(payload, list) else []


_TRASH_SCRIPT = (
    "set -eu\n"
    + ssh_ops.EXPAND_PATH_FUNCTION
    + r"""datasets=$(expand_remote_path "$1")
repo_id=$2
entry=$3
payload=$4
target=$datasets/$repo_id
trash=$datasets/.trash
test -f "$target/.ready" || exit 3
mkdir -p -- "$trash"
test ! -e "$trash/$entry" || exit 4
mv -- "$target" "$trash/$entry"
printf '%s' "$payload" > "$trash/$entry/lelab-trash.json"
"""
)

_RESTORE_SCRIPT = (
    "set -eu\n"
    + ssh_ops.EXPAND_PATH_FUNCTION
    + r"""datasets=$(expand_remote_path "$1")
repo_id=$2
entry=$3
test -d "$datasets/.trash/$entry" || exit 3
test ! -e "$datasets/$repo_id" || exit 4
mkdir -p -- "$(dirname "$datasets/$repo_id")"
mv -- "$datasets/.trash/$entry" "$datasets/$repo_id"
rm -f -- "$datasets/$repo_id/lelab-trash.json"
"""
)

_LIST_TRASH_SOURCE = """
import json, os, sys
from pathlib import Path

datasets = Path(os.path.expanduser(sys.argv[1])) / "datasets"
entries = []
for marker in sorted((datasets / ".trash").glob("*/lelab-trash.json")):
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    payload.setdefault("entry", marker.parent.name)
    entries.append(payload)
print(json.dumps(entries))
"""


def trash_remote_composite(
    repo_id: str, *, config: TrainServerConfig | None = None
) -> dict[str, Any]:
    """Move a composite on the training server into its ``.trash`` (recoverable)."""
    if not is_composite_repo_id(repo_id):
        raise CompositeError(f"{repo_id} is not a composite dataset id")
    config = config or train_server_config()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    entry = f"{stamp}-{repo_id.replace('/', '__')}"
    payload = json.dumps(
        {
            "repo_id": repo_id,
            "entry": entry,
            "trashed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    return_code, output = ssh_ops.run_script(
        config, _TRASH_SCRIPT, _datasets_root(config), repo_id, entry, payload, timeout=_REMOTE_TIMEOUT
    )
    if return_code == 3:
        raise CompositeError(f"{repo_id} 在训练机上不存在或未就绪")
    if return_code == 4:
        raise CompositeError(f"回收站条目已存在：{entry}")
    if return_code != 0:
        raise CompositeError(f"移入回收站失败（ssh exit {return_code}）：{output.strip()[-300:]}")
    return {"repo_id": repo_id, "entry": entry, "trashed_at": json.loads(payload)["trashed_at"]}


def list_remote_composite_trash(
    *, config: TrainServerConfig | None = None
) -> list[dict[str, Any]]:
    config = config or train_server_config()
    return_code, output = ssh_ops.run_python(config, _LIST_TRASH_SOURCE, config.root)
    if return_code != 0:
        raise CompositeError(f"无法读取训练机回收站（ssh exit {return_code}）")
    try:
        payload = json.loads(output.strip().splitlines()[-1]) if output.strip() else []
    except (ValueError, IndexError) as exc:
        raise CompositeError("训练机返回了无法解析的回收站列表") from exc
    return payload if isinstance(payload, list) else []


def restore_remote_composite(
    repo_id: str, entry: str, *, config: TrainServerConfig | None = None
) -> dict[str, Any]:
    if "/" in entry or "\\" in entry or entry.startswith("."):
        raise CompositeError("trash entry must be a plain directory name")
    config = config or train_server_config()
    return_code, output = ssh_ops.run_script(
        config, _RESTORE_SCRIPT, _datasets_root(config), repo_id, entry, timeout=_REMOTE_TIMEOUT
    )
    if return_code == 3:
        raise CompositeError(f"回收站条目不存在：{entry}")
    if return_code == 4:
        raise CompositeError(f"{repo_id} 已存在于训练机，无法恢复")
    if return_code != 0:
        raise CompositeError(f"恢复失败（ssh exit {return_code}）：{output.strip()[-300:]}")
    return {"repo_id": repo_id, "entry": entry, "restored": True}


# ---------------------------------------------------------------------------
# Build lifecycle
# ---------------------------------------------------------------------------


def _builder_source() -> str:
    from .scripts import build_composite

    return Path(build_composite.__file__).read_text(encoding="utf-8")


class CompositeBuildManager:
    """One background build per composite hash, mirroring the dataset sync manager."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancel: dict[str, threading.Event] = {}

    # ------------------------------------------------------------------ state
    def status(self, key: str) -> dict[str, Any]:
        """Status by composite hash or composite repo id."""
        composite_hash = composite_hash_of(key) or key
        with self._lock:
            job = self._jobs.get(composite_hash)
        return dict(job) if job is not None else {"composite_hash": composite_hash, "state": "unknown"}

    def active(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(job)
                for job in self._jobs.values()
                if job.get("state") in {"verifying", "merging", "publishing"}
            ]

    # ------------------------------------------------------------------ start
    def start(
        self,
        plan: CompositePlan,
        *,
        runner: str = "ssh_remote",
        config: TrainServerConfig | None = None,
        python: str | None = None,
    ) -> dict[str, Any]:
        if not plan.ok or plan.composite_hash is None:
            raise CompositeError("组合计划存在阻塞项，无法构建")
        key = plan.composite_hash
        with self._lock:
            existing = self._jobs.get(key)
            if existing is not None and existing.get("state") in {
                "verifying",
                "merging",
                "publishing",
            }:
                return dict(existing)

        build_id = uuid.uuid4().hex[:12]
        job: dict[str, Any] = {
            "composite_hash": key,
            "composite_repo_id": plan.composite_repo_id,
            "repo_ids": list(plan.repo_ids),
            "runner": runner,
            "build_id": build_id,
            "state": "verifying",
            "phase": "verifying",
            "progress": 0.0,
            "started_at": time.time(),
            "finished_at": None,
            "error": None,
            "log_tail": [],
            "cached": False,
        }
        cancel = threading.Event()
        with self._lock:
            self._jobs[key] = job
            self._cancel[key] = cancel
        thread = threading.Thread(
            target=self._run,
            args=(key, plan, runner, config, python, job, cancel, build_id),
            name=f"composite-{key}",
            daemon=True,
        )
        with self._lock:
            self._threads[key] = thread
        thread.start()
        return dict(job)

    def cancel(self, key: str) -> dict[str, Any]:
        composite_hash = composite_hash_of(key) or key
        with self._lock:
            job = self._jobs.get(composite_hash)
            process = self._processes.get(composite_hash)
            cancel = self._cancel.get(composite_hash)
        if job is None or job.get("state") not in {"verifying", "merging", "publishing"}:
            return self.status(composite_hash)
        if cancel is not None:
            cancel.set()
        if process is not None:
            with contextlib.suppress(Exception):
                process.terminate()
        return {"composite_hash": composite_hash, "state": "cancelling"}

    # ------------------------------------------------------------------- work
    def _append_log(self, job: dict[str, Any], line: str) -> None:
        if not line:
            return
        with self._lock:
            tail: list[str] = job.setdefault("log_tail", [])
            tail.append(line)
            del tail[:-_LOG_TAIL_LINES]

    def _apply_progress(self, job: dict[str, Any], line: str) -> bool:
        if not line.startswith(_PROGRESS_PREFIX):
            return False
        try:
            payload = json.loads(line[len(_PROGRESS_PREFIX) :])
        except ValueError:
            return False
        with self._lock:
            if isinstance(payload.get("phase"), str):
                job["phase"] = payload["phase"]
            if isinstance(payload.get("progress"), (int, float)):
                job["progress"] = float(payload["progress"])
        return True

    def _run(
        self,
        key: str,
        plan: CompositePlan,
        runner: str,
        config: TrainServerConfig | None,
        python: str | None,
        job: dict[str, Any],
        cancel: threading.Event,
        build_id: str,
    ) -> None:
        try:
            if runner == "ssh_remote":
                self._run_remote(key, plan, config, python, job, cancel, build_id)
            elif runner == "local":
                self._run_local(key, plan, job, cancel, build_id)
            else:
                raise CompositeError(f"unsupported composite build runner: {runner!r}")
        except Exception as exc:  # noqa: BLE001 - surfaced through the status payload
            logger.exception("Composite build %s failed", key)
            with self._lock:
                job.update(
                    {
                        "state": "cancelled" if cancel.is_set() else "failed",
                        "phase": "cancelled" if cancel.is_set() else "failed",
                        "error": str(exc),
                        "finished_at": time.time(),
                    }
                )
            return
        with self._lock:
            job.update(
                {"state": "ready", "phase": "ready", "progress": 1.0, "finished_at": time.time()}
            )
        logger.info("Composite %s is ready on %s", plan.composite_repo_id, runner)

    def _run_local(
        self,
        key: str,
        plan: CompositePlan,
        job: dict[str, Any],
        cancel: threading.Event,
        build_id: str,
    ) -> None:
        """Local build: used by tests and the standalone CLI, never by the API."""
        from .scripts import build_composite

        def progress(payload: dict[str, Any]) -> None:
            if cancel.is_set():
                raise CompositeError("build cancelled")
            with self._lock:
                job["phase"] = payload.get("phase", job.get("phase"))
                job["progress"] = payload.get("progress", job.get("progress"))

        result = build_composite.build(
            plan.to_plan_payload(),
            lerobot_dataset_root(),
            build_id=build_id,
            progress_cb=progress,
            log_cb=lambda line: self._append_log(job, line),
        )
        with self._lock:
            job["cached"] = bool(result.get("cached"))

    def _run_remote(
        self,
        key: str,
        plan: CompositePlan,
        config: TrainServerConfig | None,
        python: str | None,
        job: dict[str, Any],
        cancel: threading.Event,
        build_id: str,
    ) -> None:
        config = config or train_server_config()
        interpreter = (python or ssh_ops.remote_python()).strip() or "python3"
        payload = plan.to_plan_payload()
        source = _builder_source()
        plan_b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        remote_command = (
            f"{shlex.quote(interpreter)} - "
            f"--plan-json {shlex.quote(plan_b64)} "
            f"--datasets-root {shlex.quote(posixpath.join(config.root, 'datasets'))} "
            f"--build-id {shlex.quote(build_id)} --python {shlex.quote(interpreter)}"
        )
        command = [*ssh_base_command(config), config.endpoint, remote_command]
        logger.info("Building composite %s on %s", plan.composite_repo_id, config.endpoint)
        # Binary streams keep the streamed Python source byte-identical on
        # Windows (text mode would rewrite LF to CRLF on the way to the remote).
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        with self._lock:
            self._processes[key] = process
        try:
            assert process.stdin is not None
            process.stdin.write(source.encode("utf-8"))
            process.stdin.close()
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not self._apply_progress(job, line):
                    self._append_log(job, line)
            return_code = process.wait()
        finally:
            with self._lock:
                self._processes.pop(key, None)
        if cancel.is_set():
            raise CompositeError("build cancelled")
        if return_code != 0:
            tail = " | ".join((job.get("log_tail") or [])[-3:])
            raise CompositeError(f"训练机构建失败（ssh exit {return_code}）：{tail or 'no output'}")


composite_build_manager = CompositeBuildManager()


def get_composite_manager() -> CompositeBuildManager:
    """Return the process-wide composite build manager."""
    return composite_build_manager
