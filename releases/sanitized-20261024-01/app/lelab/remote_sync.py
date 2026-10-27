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
"""Asynchronous, resumable transfer of a local dataset to a training server.

The Jetson remains the source of truth.  A transfer first writes to a stable
``datasets/.incoming`` directory and only replaces the training copy after
rsync exits successfully.  The remote finalisation script keeps the previous
copy below ``datasets/.previous`` and rolls it back if the final rename fails.

Authentication is intentionally delegated to OpenSSH (an SSH agent, the
default key, or ``LELAB_TRAIN_SSH_KEY``).  Passwords are never accepted by
this module and no shell is used on the Jetson side.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import os
import platform
import posixpath
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .dataset_storage import resolve_dataset_path
from .utils.servers import resolved_env

#: Phases in which a transfer owns the singleton and the dataset must not be
#: modified (records its `repo_id`) or started again.
ACTIVE_SYNC_PHASES = frozenset(
    {"queued", "checking", "preparing", "transferring", "finalizing", "cancelling"}
)
_ACTIVE_PHASES = ACTIVE_SYNC_PHASES
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_PROGRESS_RE = re.compile(r"^\s*(?P<bytes>[0-9][0-9,]*)\s+(?P<percent>[0-9]{1,3})%")
_TOTAL_RE = re.compile(r"Total file size:\s*(?P<bytes>[0-9][0-9,]*)\s+bytes")


class DatasetSyncError(RuntimeError):
    """A human-readable dataset transfer failure."""


class _SyncCancelledError(Exception):
    """Internal control-flow signal for a user-requested cancellation."""


@dataclass(frozen=True, slots=True)
class TrainServerConfig:
    """Non-secret SSH coordinates for the remote training server."""

    host: str
    port: int
    user: str
    root: str
    ssh_key: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TrainServerConfig:
        # Profile files supply deployment defaults; real environment variables
        # (and any explicit mapping passed by tests) always win.
        values = resolved_env(os.environ if env is None else env)
        host = values.get("LELAB_TRAIN_HOST", "").strip()
        user = values.get("LELAB_TRAIN_USER", "").strip()
        root = values.get("LELAB_TRAIN_ROOT", "").strip()
        raw_port = values.get("LELAB_TRAIN_PORT", "22").strip()
        raw_key = values.get("LELAB_TRAIN_SSH_KEY", "").strip()

        missing = [
            name
            for name, value in (
                ("LELAB_TRAIN_HOST", host),
                ("LELAB_TRAIN_USER", user),
                ("LELAB_TRAIN_ROOT", root),
            )
            if not value
        ]
        if missing:
            raise DatasetSyncError(f"Missing training-server configuration: {', '.join(missing)}")
        if not _HOST_RE.fullmatch(host) or host.startswith("-"):
            raise DatasetSyncError("LELAB_TRAIN_HOST contains unsupported characters")
        if not _USER_RE.fullmatch(user) or user.startswith("-"):
            raise DatasetSyncError("LELAB_TRAIN_USER is not a valid SSH user")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise DatasetSyncError("LELAB_TRAIN_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise DatasetSyncError("LELAB_TRAIN_PORT must be between 1 and 65535")

        normalized_root = _normalize_remote_root(root)
        ssh_key = Path(raw_key).expanduser().resolve(strict=False) if raw_key else None
        return cls(host=host, port=port, user=user, root=normalized_root, ssh_key=ssh_key)

    @property
    def endpoint(self) -> str:
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{self.user}@{host}"


@dataclass(frozen=True, slots=True)
class _SyncPlan:
    sync_id: str
    repo_id: str
    source: Path
    stage_key: str
    staging: str
    target: str
    previous: str
    check_command: list[str]
    prepare_command: list[str]
    rsync_command: list[str]
    finalize_command: list[str]

    def public_commands(self) -> dict[str, Any]:
        """Return an exact, JSON-safe dry-run description."""
        return {
            "check": list(self.check_command),
            "prepare": list(self.prepare_command),
            "prepare_stdin": _PREPARE_SCRIPT,
            "rsync": list(self.rsync_command),
            "finalize": list(self.finalize_command),
            "finalize_stdin": _FINALIZE_SCRIPT,
        }

    def public_paths(self) -> dict[str, str]:
        return {
            "source": str(self.source),
            "staging": self.staging,
            "target": self.target,
            "previous": self.previous,
        }


_PREPARE_SCRIPT = r"""set -eu
root=$1
stage_key=$2
case "$root" in
  "~") root=$HOME ;;
  "~/"*) root=$HOME/${root#\~/} ;;
esac
datasets=$root/datasets
staging=$datasets/.incoming/$stage_key
mkdir -p -- "$staging" "$datasets/.previous"
"""


_FINALIZE_SCRIPT = r"""set -eu
root=$1
repo_id=$2
sync_id=$3
stage_key=$4
case "$root" in
  "~") root=$HOME ;;
  "~/"*) root=$HOME/${root#\~/} ;;
esac
datasets=$root/datasets
staging=$datasets/.incoming/$stage_key
target=$datasets/$repo_id
previous_dir=$datasets/.previous/$repo_id
backup=$previous_dir/$sync_id

test -d "$staging"
test -f "$staging/meta/info.json"
mkdir -p -- "$(dirname "$target")" "$previous_dir"
test ! -e "$backup"
printf '%s\n' "$sync_id" > "$staging/.ready"

moved_old=0
rollback() {
  code=$?
  trap - EXIT HUP INT TERM
  if [ "$moved_old" -eq 1 ] && [ ! -e "$target" ] && [ -e "$backup" ]; then
    mv -- "$backup" "$target" || true
  fi
  exit "$code"
}
trap rollback EXIT HUP INT TERM

if [ -e "$target" ] || [ -L "$target" ]; then
  mv -- "$target" "$backup"
  moved_old=1
fi
mv -- "$staging" "$target"
moved_old=0
trap - EXIT HUP INT TERM
"""


def _normalize_remote_root(root: str) -> str:
    if root != root.strip() or any(character in root for character in ("\x00", "\n", "\r")):
        raise DatasetSyncError("LELAB_TRAIN_ROOT contains invalid characters")
    if root == "~":
        return root
    if root.startswith("~/"):
        prefix = "~/"
        suffix = root[2:]
    elif root.startswith("/"):
        prefix = "/"
        suffix = root[1:]
    else:
        raise DatasetSyncError("LELAB_TRAIN_ROOT must be an absolute path or start with '~/''")
    parts = suffix.split("/")
    if not suffix or any(not part or part in {".", ".."} for part in parts):
        raise DatasetSyncError("LELAB_TRAIN_ROOT must name a specific normalized directory")
    normalized = posixpath.join(prefix, *parts)
    return normalized if prefix == "/" else f"~/{'/'.join(parts)}"


def _remote_path_for_rsync(path: str) -> str:
    """Quote a remote POSIX path while retaining rsync's ``~`` expansion.

    rsync passes the destination through the remote shell, so the path must be
    quoted for that shell. A leading ``"$HOME"`` used to be emitted here, but
    rsync 3.2.7 quotes the whole argument first, which makes the remote shell
    receive the literal string ``"$HOME"/Desktop/...`` and mkdir fails with
    ``/home/<user>/"$HOME"/Desktop/...``. Plain ``~/...`` is expanded by rsync
    itself and was verified end-to-end against the training server.
    """
    if path == "~":
        return "~"
    if path.startswith("~/"):
        return f"~/{shlex.quote(path[2:])}"
    return shlex.quote(path)


def _ssh_base(config: TrainServerConfig) -> list[str]:
    return ssh_base_command(config)


def ssh_base_command(config: TrainServerConfig) -> list[str]:
    """Public form of the SSH argv prefix (used by the remote job runner too)."""
    command = [
        "ssh",
        "-p",
        str(config.port),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
    ]
    if config.ssh_key is not None:
        command.extend(["-i", str(config.ssh_key)])
    return command


def _remote_script_invocation(*arguments: str) -> str:
    return "sh -s -- " + " ".join(shlex.quote(argument) for argument in arguments)


def _make_plan(
    config: TrainServerConfig,
    repo_id: str,
    source: Path,
    sync_id: str,
) -> _SyncPlan:
    datasets = posixpath.join(config.root, "datasets")
    stage_key = hashlib.sha256(repo_id.encode("utf-8")).hexdigest()
    staging = posixpath.join(datasets, ".incoming", stage_key)
    target = posixpath.join(datasets, *repo_id.split("/"))
    previous = posixpath.join(datasets, ".previous", *repo_id.split("/"), sync_id)
    ssh_base = _ssh_base(config)
    check = [*ssh_base, config.endpoint, "true"]
    prepare = [
        *ssh_base,
        config.endpoint,
        _remote_script_invocation(config.root, stage_key),
    ]
    ssh_transport = shlex.join(ssh_base)
    destination = f"{config.endpoint}:{_remote_path_for_rsync(staging)}/"
    rsync = [
        "rsync",
        "-a",
        "--delete",
        "--partial",
        "--append-verify",
        "--info=progress2",
        "--stats",
        "--no-inc-recursive",
        "-e",
        ssh_transport,
        f"{source}/",
        destination,
    ]
    finalize = [
        *ssh_base,
        config.endpoint,
        _remote_script_invocation(config.root, repo_id, sync_id, stage_key),
    ]
    return _SyncPlan(
        sync_id=sync_id,
        repo_id=repo_id,
        source=source,
        stage_key=stage_key,
        staging=staging,
        target=target,
        previous=previous,
        check_command=check,
        prepare_command=prepare,
        rsync_command=rsync,
        finalize_command=finalize,
    )


def _timestamp() -> str:
    return datetime.now(tz=UTC).isoformat()


def _initial_status() -> dict[str, Any]:
    return {
        "phase": "idle",
        "id": None,
        "repo_id": None,
        "progress": 0.0,
        "transferred": 0,
        "total": None,
        "rate_bytes_per_second": None,
        "eta_seconds": None,
        "error": None,
        "started": None,
        "finished": None,
    }


def _error_tail(output: str, *, limit: int = 2000) -> str:
    compact = " ".join(line.strip() for line in output.splitlines() if line.strip())
    return compact[-limit:] if compact else "no diagnostic output"


class DatasetSyncManager:
    """Own at most one background Jetson-to-training-server transfer."""

    def __init__(
        self,
        *,
        dataset_root: str | os.PathLike[str] | None = None,
        config: TrainServerConfig | None = None,
    ) -> None:
        self._dataset_root = dataset_root
        self._config = config
        self._lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._status = _initial_status()
        self._rate_sample: tuple[float, int] | None = None
        # repo_id -> last finished transfer (kept in memory for the dashboard;
        # the authoritative record is the remote `.ready` marker).
        self._history: dict[str, dict[str, Any]] = {}

    def status(self) -> dict[str, Any]:
        """Return a detached JSON-serializable status snapshot."""
        with self._lock:
            return copy.deepcopy(self._status)

    def history(self, repo_id: str | None = None) -> dict[str, Any] | list[dict[str, Any]]:
        """Last known transfer state per dataset (or one dataset's record)."""
        with self._lock:
            if repo_id is not None:
                return copy.deepcopy(self._history.get(repo_id, {}))
            return copy.deepcopy(list(self._history.values()))

    def start(self, repo_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        """Validate and enqueue one transfer without waiting for network I/O.

        A dry run performs path/configuration validation and returns every
        subprocess argument plus the two fixed remote scripts, but launches no
        process and performs no connection check.
        """
        with self._lock:
            if self._status["phase"] in _ACTIVE_PHASES:
                raise DatasetSyncError("A dataset transfer is already running")

        try:
            source = resolve_dataset_path(repo_id, self._dataset_root)
        except ValueError as exc:
            raise DatasetSyncError(str(exc)) from exc
        if not source.is_dir() or not (source / "meta" / "info.json").is_file():
            raise DatasetSyncError(f"Local LeRobot dataset not found: {repo_id}")

        config = self._config if self._config is not None else TrainServerConfig.from_env()
        sync_id = uuid.uuid4().hex
        plan = _make_plan(config, repo_id, source, sync_id)
        started = _timestamp()
        state = {
            "phase": "dry_run" if dry_run else "queued",
            "id": sync_id,
            "repo_id": repo_id,
            "progress": 0.0,
            "transferred": 0,
            "total": None,
            "error": None,
            "started": started,
            "finished": started if dry_run else None,
        }
        if dry_run:
            state["commands"] = plan.public_commands()
            state["paths"] = plan.public_paths()

        with self._lock:
            # Repeat the check after filesystem/config validation so two
            # simultaneous callers cannot both reserve the singleton.
            if self._status["phase"] in _ACTIVE_PHASES:
                raise DatasetSyncError("A dataset transfer is already running")
            self._status = state
            if dry_run:
                return copy.deepcopy(state)
            self._cancel_event.clear()
            self._rate_sample = None
            self._thread = threading.Thread(
                target=self._run_sync,
                args=(config, plan),
                name=f"dataset-sync-{sync_id[:8]}",
                daemon=True,
            )
            self._thread.start()
            return copy.deepcopy(state)

    def cancel(self) -> dict[str, Any]:
        """Request cancellation and terminate the currently tracked process."""
        with self._lock:
            if self._status["phase"] not in _ACTIVE_PHASES:
                return copy.deepcopy(self._status)
            self._cancel_event.set()
            self._status["phase"] = "cancelling"
            process = self._process
            if process is not None and process.poll() is None:
                with contextlib.suppress(OSError):
                    process.terminate()
            return copy.deepcopy(self._status)

    def _set_status(self, **changes: Any) -> None:
        with self._lock:
            self._status.update(changes)

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise _SyncCancelledError

    def _track_process(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._process = process
            should_terminate = self._cancel_event.is_set()
        if should_terminate and process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()

    def _clear_process(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if self._process is process:
                self._process = None

    def _spawn(
        self,
        command: list[str],
        *,
        stdin: int | None = None,
    ) -> subprocess.Popen[str]:
        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        try:
            process = subprocess.Popen(
                command,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=environment,
            )
        except OSError as exc:
            raise DatasetSyncError(f"Could not start {command[0]}: {exc}") from exc
        self._track_process(process)
        return process

    def _capture_command(self, command: list[str], *, input_text: str | None = None) -> tuple[int, str]:
        process = self._spawn(command, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL)
        try:
            output, _ = process.communicate(input_text)
            return process.returncode, output or ""
        finally:
            self._clear_process(process)

    def _run_rsync(self, command: list[str]) -> tuple[int, str]:
        process = self._spawn(command, stdin=subprocess.DEVNULL)
        output_parts: list[str] = []
        pending = ""
        try:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                output_parts.append(chunk)
                pending += chunk
                records = re.split(r"[\r\n]+", pending)
                pending = records.pop()
                for record in records:
                    self._consume_rsync_record(record)
                self._check_cancelled()
            if pending:
                self._consume_rsync_record(pending)
            return_code = process.wait()
            return return_code, "".join(output_parts)
        finally:
            self._clear_process(process)

    def _consume_rsync_record(self, record: str) -> None:
        progress_match = _PROGRESS_RE.search(record)
        if progress_match:
            transferred = int(progress_match.group("bytes").replace(",", ""))
            progress = min(100.0, float(progress_match.group("percent")))
            now = time.monotonic()
            sample = self._rate_sample
            rate: float | None = None
            if sample is not None and now - sample[0] >= 0.5 and transferred >= sample[1]:
                rate = (transferred - sample[1]) / (now - sample[0])
                self._rate_sample = (now, transferred)
            elif sample is None:
                self._rate_sample = (now, transferred)
            with self._lock:
                previous_total = self._status.get("total")
                estimated_total = transferred if progress >= 100 else None
                if progress > 0 and progress < 100:
                    estimated_total = round(transferred * 100 / progress)
                if isinstance(previous_total, int) and estimated_total is not None:
                    estimated_total = max(previous_total, estimated_total)
                eta_seconds: float | None = None
                if rate and rate > 0 and isinstance(estimated_total, int) and estimated_total > transferred:
                    eta_seconds = round((estimated_total - transferred) / rate, 1)
                self._status.update(
                    progress=max(float(self._status.get("progress", 0.0)), progress),
                    transferred=max(int(self._status.get("transferred", 0)), transferred),
                    total=estimated_total if estimated_total is not None else previous_total,
                    rate_bytes_per_second=round(rate, 1) if rate else self._status.get("rate_bytes_per_second"),
                    eta_seconds=eta_seconds,
                )
        total_match = _TOTAL_RE.search(record)
        if total_match:
            self._set_status(total=int(total_match.group("bytes").replace(",", "")))

    def _preflight(self, config: TrainServerConfig) -> None:
        if platform.system() != "Linux":
            raise DatasetSyncError("Dataset sync must run on the Jetson/Linux host (rsync is not run on Windows)")
        missing_tools = [tool for tool in ("ssh", "rsync") if shutil.which(tool) is None]
        if missing_tools:
            raise DatasetSyncError(
                f"Missing required transfer tool(s): {', '.join(missing_tools)}. Install openssh-client and rsync."
            )
        if config.ssh_key is not None and not config.ssh_key.is_file():
            raise DatasetSyncError(f"LELAB_TRAIN_SSH_KEY does not exist: {config.ssh_key}")

    def _run_sync(self, config: TrainServerConfig, plan: _SyncPlan) -> None:
        try:
            self._set_status(phase="checking")
            self._preflight(config)
            self._check_cancelled()

            return_code, output = self._capture_command(plan.check_command)
            self._check_cancelled()
            if return_code != 0:
                raise DatasetSyncError(
                    f"Cannot connect to training server {config.endpoint}:{config.port}: {_error_tail(output)}"
                )

            self._set_status(phase="preparing")
            return_code, output = self._capture_command(plan.prepare_command, input_text=_PREPARE_SCRIPT)
            self._check_cancelled()
            if return_code != 0:
                raise DatasetSyncError(f"Could not prepare remote staging directory: {_error_tail(output)}")

            self._set_status(phase="transferring")
            return_code, output = self._run_rsync(plan.rsync_command)
            self._check_cancelled()
            if return_code != 0:
                raise DatasetSyncError(f"rsync failed with exit code {return_code}: {_error_tail(output)}")

            self._set_status(phase="finalizing")
            return_code, output = self._capture_command(plan.finalize_command, input_text=_FINALIZE_SCRIPT)
            self._check_cancelled()
            if return_code != 0:
                raise DatasetSyncError(f"Remote atomic activation failed: {_error_tail(output)}")

            with self._lock:
                transferred = int(self._status.get("transferred", 0))
                total = self._status.get("total")
                self._status.update(
                    phase="completed",
                    progress=100.0,
                    total=transferred if total is None else total,
                    finished=_timestamp(),
                )
        except _SyncCancelledError:
            self._set_status(phase="cancelled", error=None, finished=_timestamp())
        except Exception as exc:
            if self._cancel_event.is_set():
                self._set_status(phase="cancelled", error=None, finished=_timestamp())
            else:
                message = str(exc) if isinstance(exc, DatasetSyncError) else f"Unexpected sync error: {exc}"
                self._set_status(phase="failed", error=message, finished=_timestamp())
        finally:
            with self._lock:
                self._process = None
                self._history[plan.repo_id] = {
                    "repo_id": plan.repo_id,
                    "id": plan.sync_id,
                    "phase": self._status.get("phase"),
                    "progress": self._status.get("progress"),
                    "transferred": self._status.get("transferred"),
                    "total": self._status.get("total"),
                    "error": self._status.get("error"),
                    "finished": self._status.get("finished"),
                    "remote_path": plan.target,
                }


# Route handlers import this singleton; construction performs no environment,
# filesystem, network, or subprocess work.
dataset_sync_manager = DatasetSyncManager()


def get_dataset_sync_manager() -> DatasetSyncManager:
    """Return the process-wide dataset transfer manager."""
    return dataset_sync_manager
