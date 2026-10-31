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
"""Standalone composite-dataset builder.

Reads a plan (see :mod:`lelab.composites`), verifies that every member on this
machine still matches the fingerprints the plan was computed from, merges them
with LeRobot's own ``lerobot_edit_dataset`` merge operation into a staging
directory, and publishes the result atomically with a ``.ready`` marker.

The file is deliberately dependency-free beyond LeRobot itself so the training
server can run it over SSH without a LeLab install: LeLab streams this source
as the interpreter's stdin (``python3 - <args>``) and reads the
``LELAB_PROGRESS {json}`` lines from stdout.

Exit codes: 2 = a member does not match the plan, 3 = not enough disk space,
4 = invalid plan, anything else = the merge failed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_MARGIN_BYTES = 1024 * 1024 * 1024
DEFAULT_SIZE_FACTOR = 1.15
STAGING_DIRNAME = ".composites_incoming"
MARKER_RELATIVE_PATH = Path("meta") / "lelab-composite.json"
_HASH_CHUNK_BYTES = 1024 * 1024

#: Feature-info keys that describe encoder settings rather than data layout.
#: Two recordings of the same task can differ here and still merge cleanly.
VIDEO_ENCODER_INFO_KEYS = (
    "video.crf",
    "video.gop",
    "video.pix_fmt",
    "video.preset",
    "video.extra_options",
    "video.threads",
)


class CompositeBuildError(RuntimeError):
    """A human-readable failure while building a composite dataset."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_feature(feature: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(feature))
    info = normalized.get("info")
    if isinstance(info, dict):
        for key in VIDEO_ENCODER_INFO_KEYS:
            info.pop(key, None)
        if not info:
            normalized.pop("info", None)
    return normalized


def meta_fingerprint(dataset_path: Path) -> dict[str, Any]:
    """Cheap identity of a dataset: metadata, stats and episode index only.

    Video and Parquet payloads are covered by the seal manifest instead; this
    fingerprint exists to catch a stale ``.ready`` copy on the training server.
    """
    info_path = dataset_path / "meta" / "info.json"
    if not info_path.is_file():
        raise CompositeBuildError(f"Not a LeRobot dataset: {dataset_path}")
    info = json.loads(info_path.read_text(encoding="utf-8"))

    stats_path = dataset_path / "meta" / "stats.json"
    stats_sha = _sha256_file(stats_path) if stats_path.is_file() else None

    episodes_dir = dataset_path / "meta" / "episodes"
    episodes_files = sorted(episodes_dir.rglob("*.parquet")) if episodes_dir.is_dir() else []
    episodes_sha = (
        _canonical_digest(
            [
                {"path": item.relative_to(dataset_path).as_posix(), "sha256": _sha256_file(item)}
                for item in episodes_files
            ]
        )
        if episodes_files
        else None
    )

    features = info.get("features") if isinstance(info.get("features"), dict) else {}
    return {
        "codebase_version": info.get("codebase_version"),
        "fps": info.get("fps"),
        "robot_type": info.get("robot_type"),
        "total_episodes": info.get("total_episodes"),
        "total_frames": info.get("total_frames"),
        "storage_format": info.get("storage_format"),
        "features": {name: _normalized_feature(value) for name, value in features.items()},
        "stats_sha256": stats_sha,
        "episodes_sha256": episodes_sha,
        "episodes_file_count": len(episodes_files),
    }


def meta_fingerprint_sha256(dataset_path: Path) -> str:
    return _canonical_digest(meta_fingerprint(dataset_path))


def verify_members(plan: dict[str, Any], datasets_root: Path) -> list[str]:
    """Return one message per member whose on-disk identity differs from the plan."""
    problems: list[str] = []
    for member in plan.get("members") or []:
        repo_id = str(member.get("repo_id") or "")
        expected = member.get("meta_fingerprint_sha256")
        dataset_path = datasets_root.joinpath(*repo_id.split("/"))
        if not repo_id:
            problems.append("plan contains a member without repo_id")
            continue
        if not dataset_path.is_dir():
            problems.append(f"{repo_id}: missing on this machine (sync it first)")
            continue
        try:
            actual = meta_fingerprint_sha256(dataset_path)
        except (OSError, ValueError, CompositeBuildError) as exc:
            problems.append(f"{repo_id}: unreadable ({exc})")
            continue
        if actual != expected:
            problems.append(
                f"{repo_id}: metadata differs from the sealed copy on the Jetson "
                f"(expected {str(expected)[:12]}, found {actual[:12]}) — re-sync it"
            )
    return problems


def _emit(payload: dict[str, Any], progress_cb=None) -> None:
    if progress_cb is not None:
        progress_cb(payload)
        return
    sys.stdout.write("LELAB_PROGRESS " + json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _run_merge(
    plan: dict[str, Any],
    staging_dir: Path,
    datasets_root: Path,
    *,
    python_executable: str,
    progress_cb=None,
    log_cb=None,
) -> None:
    merge = plan.get("merge") or {}
    command = [
        python_executable,
        "-m",
        "lerobot.scripts.lerobot_edit_dataset",
        "--operation.type",
        "merge",
        "--new_repo_id",
        str(plan["composite_repo_id"]),
        "--new_root",
        str(staging_dir),
        "--operation.repo_ids",
        json.dumps(list(plan["repo_ids"])),
        "--operation.concatenate_videos",
        "true" if merge.get("concatenate_videos", False) else "false",
        "--operation.concatenate_data",
        "true" if merge.get("concatenate_data", False) else "false",
    ]
    environment = {
        **os.environ,
        "HF_LEROBOT_HOME": str(datasets_root),
        "PYTHONUNBUFFERED": "1",
    }
    process = subprocess.Popen(
        command,
        cwd=str(datasets_root),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip("\n")
        if log_cb is not None:
            log_cb(line)
        else:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
    return_code = process.wait()
    if return_code != 0:
        raise CompositeBuildError(
            f"lerobot merge failed with exit code {return_code}; see the log above"
        )


def publish(
    plan: dict[str, Any],
    staging_dir: Path,
    datasets_root: Path,
    *,
    build_id: str,
) -> Path:
    """Atomically move a finished staging directory into place and mark it ready."""
    final_dir = datasets_root.joinpath(*str(plan["composite_repo_id"]).split("/"))
    ready = final_dir / ".ready"
    if ready.is_file():
        shutil.rmtree(staging_dir, ignore_errors=True)
        return final_dir

    size_bytes = 0
    file_count = 0
    for current_root, _dirs, files in os.walk(staging_dir):
        for name in files:
            try:
                size_bytes += (Path(current_root) / name).stat().st_size
            except OSError:
                continue
            file_count += 1

    marker = staging_dir / MARKER_RELATIVE_PATH
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                **plan,
                "build_id": build_id,
                "built_at": time.time(),
                "size_bytes": size_bytes,
                "file_count": file_count,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    if final_dir.exists():
        # A previous build died before writing `.ready`; its output is garbage.
        shutil.rmtree(final_dir, ignore_errors=True)
    os.replace(staging_dir, final_dir)
    (final_dir / ".ready").write_text(build_id + "\n", encoding="utf-8")
    return final_dir


def build(
    plan: dict[str, Any],
    datasets_root: Path,
    *,
    build_id: str = "manual",
    python_executable: str | None = None,
    progress_cb=None,
    log_cb=None,
    skip_disk_check: bool = False,
) -> dict[str, Any]:
    """Verify, merge and publish one composite dataset. Returns a summary."""
    if not plan.get("repo_ids") or len(plan["repo_ids"]) < 2:
        raise CompositeBuildError("a composite needs at least two member datasets")
    if not plan.get("composite_repo_id"):
        raise CompositeBuildError("plan is missing composite_repo_id")

    interpreter = python_executable or sys.executable or "python3"
    datasets_root = Path(datasets_root).expanduser()
    final_dir = datasets_root.joinpath(*str(plan["composite_repo_id"]).split("/"))
    if (final_dir / ".ready").is_file():
        _emit({"phase": "ready", "progress": 1.0, "repo_id": plan["composite_repo_id"]}, progress_cb)
        return {"composite_repo_id": plan["composite_repo_id"], "cached": True, "path": str(final_dir)}

    _emit({"phase": "verifying", "progress": 0.02}, progress_cb)
    problems = verify_members(plan, datasets_root)
    if problems:
        raise CompositeBuildError("; ".join(problems))

    if not skip_disk_check:
        estimate = int(plan.get("estimated_size_bytes") or 0)
        required = int(estimate * DEFAULT_SIZE_FACTOR) + DEFAULT_MARGIN_BYTES
        free = shutil.disk_usage(datasets_root).free
        if estimate and free < required:
            raise CompositeBuildError(
                "not enough free space for the composite: "
                f"need ~{required / 1e9:.1f} GB, have {free / 1e9:.1f} GB "
                f"({plan['composite_repo_id']})"
            )

    staging_root = datasets_root / STAGING_DIRNAME
    staging_root.mkdir(parents=True, exist_ok=True)
    staging_dir = staging_root / f"{plan['composite_hash']}-{build_id}"
    shutil.rmtree(staging_dir, ignore_errors=True)

    _emit({"phase": "merging", "progress": 0.1}, progress_cb)
    try:
        _run_merge(
            plan,
            staging_dir,
            datasets_root,
            python_executable=interpreter,
            progress_cb=progress_cb,
            log_cb=log_cb,
        )
        _emit({"phase": "publishing", "progress": 0.95}, progress_cb)
        published = publish(plan, staging_dir, datasets_root, build_id=build_id)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    _emit({"phase": "ready", "progress": 1.0, "repo_id": plan["composite_repo_id"]}, progress_cb)
    return {
        "composite_repo_id": plan["composite_repo_id"],
        "cached": False,
        "path": str(published),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a LeLab composite dataset.")
    parser.add_argument("plan", nargs="?", help="path to the plan JSON, or '-' for stdin")
    parser.add_argument(
        "--plan-json",
        help="base64-encoded plan JSON; used when LeLab streams this script over SSH",
    )
    parser.add_argument("--datasets-root", required=True)
    parser.add_argument("--build-id", default="manual")
    parser.add_argument("--python", dest="python_executable", default=None)
    args = parser.parse_args(argv)

    if args.plan_json:
        plan = json.loads(base64.b64decode(args.plan_json).decode("utf-8"))
    elif args.plan == "-":
        plan = json.load(sys.stdin)
    elif args.plan:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    else:
        parser.error("either a plan path or --plan-json is required")

    try:
        build(
            plan,
            Path(args.datasets_root),
            build_id=args.build_id,
            python_executable=args.python_executable,
        )
    except CompositeBuildError as exc:
        print(f"composite build failed: {exc}", file=sys.stderr, flush=True)
        return 2 if "differs from the sealed copy" in str(exc) else 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main() in tests
    raise SystemExit(main())
