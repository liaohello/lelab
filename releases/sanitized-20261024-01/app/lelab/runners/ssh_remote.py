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
"""Train on the SSH training server from the Jetson.

Unlike `HfCloudJobRunner` (a local subprocess that merely streams a remote
pod), this runner owns a *detached* process on the GPU host:

1. refuse to start unless the dataset was transferred and marked `.ready`
   (the same staged/verified path `lelab.remote_sync` writes);
2. create `~/Desktop/lelab_server/jobs/<job-id>` and write the normalized
   training config next to the run;
3. launch `lerobot-train` under `setsid` so it survives the SSH connection
   and can be re-attached after a Jetson reload;
4. poll `run.log` in bounded chunks and feed the lines through the same
   metric parser as local jobs, so loss/step/ETA appear in the Jetson UI;
5. keep checkpoints on the training server; only the resulting model is pulled
   back to the Jetson afterwards.

All remote commands are argv arrays (never a shell string built from user
input) and are executed through `sh -s --` with `shlex.quote`d arguments.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import posixpath
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue

from ..jobs import LogLine, TrainingMetrics, extract_wandb_run_url, parse_metrics_into
from ..remote_sync import TrainServerConfig, ssh_base_command
from ..train import TrainingRequest, build_training_command
from . import ssh_ops

logger = logging.getLogger(__name__)

DEFAULT_REMOTE_PYTHON = "python3"
_DEFAULT_POLL_INTERVAL = 2.0
_DEFAULT_SSH_TIMEOUT = 60.0
_MAX_LOG_CHUNK = 512 * 1024


_EXPAND_PATH_FUNCTION = r'''expand_remote_path() {
  value=$1
  case "$value" in
    "~") printf '%s\n' "$HOME" ;;
    "~/"*) printf '%s/%s\n' "$HOME" "${value#\~/}" ;;
    *) printf '%s\n' "$value" ;;
  esac
}
'''


class RemoteTrainingError(RuntimeError):
    """A human-readable failure of the remote training lifecycle."""


def _local_hf_token() -> str:
    """Token to hand to the remote trainer (empty when the user isn't logged in).

    Passing it through the launch script's stdin (not argv) keeps it out of the
    train server's process table; a logged-in user therefore gets private/gated
    model downloads on the remote host for free.
    """
    try:
        from huggingface_hub import get_token

        return get_token() or ""
    except Exception:
        return ""


def _timestamp() -> str:
    return datetime.now(tz=UTC).isoformat()


def _default_ssh(argv: list[str], input_text: str | None = None) -> tuple[int, str]:
    """Run one SSH invocation; never uses `shell=True`."""
    try:
        completed = subprocess.run(
            argv,
            # Keep LF newlines byte-for-byte. In text mode Windows translates
            # them to CRLF, and the remote /bin/sh then sees `set -eu\r`.
            input=input_text.encode("utf-8") if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=_DEFAULT_SSH_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RemoteTrainingError(f"SSH client not available: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RemoteTrainingError(f"SSH command timed out after {_DEFAULT_SSH_TIMEOUT:.0f}s") from exc
    output = completed.stdout.decode("utf-8", errors="replace") if completed.stdout else ""
    return completed.returncode, output


def _sha256_manifest_id(payload: str) -> str:
    import hashlib

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# mkdir -p "$1" and, when "$2" is set, `cat` the config JSON into it.
_PREPARE_SCRIPT = "set -eu\n" + _EXPAND_PATH_FUNCTION + r"""dir=$(expand_remote_path "$1")
mkdir -p -- "$dir"
"""

_LAUNCH_SCRIPT = "set -eu\n" + _EXPAND_PATH_FUNCTION + r"""dir=$(expand_remote_path "$1")
command=$2
root=$(expand_remote_path "$3")
proxy=$4
hf_endpoint=$5
hf_token=$6
gpu=$7
run_id=$8
attempt_id=$9
truncate_log=$10
cd "$dir"
export HF_LEROBOT_HOME="$root/datasets"
export HF_HOME="$root/models/huggingface"
export HF_HUB_DISABLE_XET=1
export HF_HUB_DOWNLOAD_TIMEOUT=30
export HF_HUB_ETAG_TIMEOUT=30
if [ -n "$gpu" ]; then
  export CUDA_VISIBLE_DEVICES="$gpu"
fi
if [ -n "$hf_endpoint" ]; then
  export HF_ENDPOINT="$hf_endpoint"
fi
if [ -n "$hf_token" ]; then
  export HF_TOKEN="$hf_token" HUGGING_FACE_HUB_TOKEN="$hf_token"
fi
if [ -n "$proxy" ]; then
  export HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy"
  export http_proxy="$proxy" https_proxy="$proxy"
  export NO_PROXY="127.0.0.1,localhost"
fi
for subdir in control metrics; do
  if [ ! -d "$subdir" ]; then mkdir "$subdir"; fi
done
rm -f control/pause.request control/pause.state.json
export LELAB_CONTROL_DIR="$dir/control"
export LELAB_RUN_ID="$run_id"
export LELAB_ATTEMPT_ID="$attempt_id"
export LELAB_EVENTS_PATH="$dir/metrics/worker-events.jsonl"
if [ "$truncate_log" = "1" ]; then
  : > run.log
else
  touch run.log
fi
rm -f run.exit
setsid nohup sh -c "$command; echo \$? > run.exit" > run.log 2>&1 &
echo $! > run.pid
"""

_STATE_SCRIPT = "set -eu\n" + _EXPAND_PATH_FUNCTION + r"""dir=$(expand_remote_path "$1")
cd "$dir"
pid=$(cat run.pid 2>/dev/null || true)
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)
  target=$(readlink -f "$dir" 2>/dev/null || printf '%s' "$dir")
  if [ -n "$cwd" ] && [ "$cwd" != "$target" ]; then
    printf 'gone %s\n' "$pid"
    exit 0
  fi
  printf 'running %s\n' "$pid"
elif [ -f run.exit ]; then
  printf 'exit %s %s\n' "$pid" "$(cat run.exit)"
else
  printf 'gone %s\n' "$pid"
fi
"""

_STOP_SCRIPT = "set -eu\n" + _EXPAND_PATH_FUNCTION + r"""dir=$(expand_remote_path "$1")
cd "$dir"
pid=$(cat run.pid 2>/dev/null || true)
if [ -z "$pid" ]; then
  exit 0
fi
cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)
target=$(readlink -f "$dir" 2>/dev/null || printf '%s' "$dir")
if [ -n "$cwd" ] && [ "$cwd" != "$target" ]; then
  echo "refusing to signal pid $pid: cwd $cwd does not belong to $target" >&2
  exit 4
fi
kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
"""

_PAUSE_SCRIPT = "set -eu\n" + _EXPAND_PATH_FUNCTION + r"""dir=$(expand_remote_path "$1")
cd "$dir"
pid=$(cat run.pid 2>/dev/null || true)
if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
  echo "training process is not running" >&2
  exit 5
fi
cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)
target=$(readlink -f "$dir" 2>/dev/null || printf '%s' "$dir")
if [ -n "$cwd" ] && [ "$cwd" != "$target" ]; then
  echo "refusing to pause pid $pid: cwd $cwd does not belong to $target" >&2
  exit 4
fi
mkdir -p control
printf '{"requested_at": %s, "pid": %s}\n' "$(date +%s)" "$pid" > control/pause.request.tmp
mv control/pause.request.tmp control/pause.request
"""

_READ_PAUSE_STATE_SCRIPT = "set -eu\n" + _EXPAND_PATH_FUNCTION + r"""dir=$(expand_remote_path "$1")
cd "$dir"
cat control/pause.state.json 2>/dev/null || true
"""

_VERIFY_PAUSE_SCRIPT = "set -eu\n" + _EXPAND_PATH_FUNCTION + r"""ckpt=$(expand_remote_path "$1")
interpreter=$2
"$interpreter" - "$ckpt" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
manifest_path = root / "lelab_pause.json"
if not manifest_path.is_file():
    print("pause manifest is missing")
    sys.exit(3)
try:
    manifest = json.loads(manifest_path.read_text())
except Exception as exc:  # noqa: BLE001
    print(f"pause manifest is unreadable: {exc}")
    sys.exit(3)
if not manifest.get("complete"):
    print("pause manifest is incomplete")
    sys.exit(3)
for record in manifest.get("required_files") or []:
    path = root / str(record.get("path"))
    if not path.is_file():
        print(f"missing {record.get('path')}")
        sys.exit(3)
    if path.stat().st_size != int(record.get("size", -1)):
        print(f"size mismatch for {record.get('path')}")
        sys.exit(3)
    expected = record.get("sha256")
    if expected:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            print(f"sha256 mismatch for {record.get('path')}")
            sys.exit(3)
print("ok")
PY
"""


def verify_remote_pause_checkpoint(
    config: TrainServerConfig,
    checkpoint_dir: str,
    *,
    remote_python: str = DEFAULT_REMOTE_PYTHON,
    ssh: Callable[[list[str], str | None], tuple[int, str]] | None = None,
) -> tuple[bool, str | None, dict | None]:
    """Read and verify a pause manifest on the training server.

    Hash verification runs on the server, so the large optimizer/model files
    never move across the link. Returns ``(ok, reason, manifest)``.
    """
    import json as _json

    manifest: dict | None = None
    try:
        text = ssh_ops.read_remote_file(
            config, posixpath.join(checkpoint_dir, "lelab_pause.json"), ssh=ssh
        )
        if text:
            payload = _json.loads(text)
            if isinstance(payload, dict):
                manifest = payload
    except Exception as exc:  # noqa: BLE001
        return False, f"could not read pause manifest: {exc}", None
    if manifest is None:
        return False, "pause manifest is missing or unreadable", None
    return_code, output = ssh_ops.run_script(
        config,
        _VERIFY_PAUSE_SCRIPT,
        checkpoint_dir,
        remote_python or DEFAULT_REMOTE_PYTHON,
        ssh=ssh,
    )
    if return_code != 0 or "ok" not in output.splitlines():
        detail = " ".join(line.strip() for line in output.splitlines() if line.strip())[-400:]
        return False, detail or f"remote verification failed (exit {return_code})", manifest
    return True, None, manifest

_TAIL_SCRIPT = "set -eu\n" + _EXPAND_PATH_FUNCTION + r"""dir=$(expand_remote_path "$1")
offset=$2
cd "$dir"
tail -c "+${offset}" run.log 2>/dev/null || true
"""


class SshRemoteJobRunner:
    """`JobRunner` implementation for one detached training on the GPU host."""

    def __init__(
        self,
        config: TrainServerConfig,
        job_id: str,
        metrics: TrainingMetrics,
        *,
        log_file_path: Path | None = None,
        remote_python: str = DEFAULT_REMOTE_PYTHON,
        remote_proxy: str | None = None,
        remote_hf_endpoint: str | None = None,
        remote_hf_token: str | None = None,
        remote_gpu: int | None = None,
        min_free_vram_mb: int | None = None,
        ssh: Callable[[list[str], str | None], tuple[int, str]] | None = None,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        event_sink: Callable[[str, float], None] | None = None,
        run_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        self.config = config
        self.job_id = job_id
        self.remote_python = remote_python
        self.remote_proxy = remote_proxy if remote_proxy is not None else os.environ.get("LELAB_TRAIN_PROXY", "")
        self.remote_hf_endpoint = (
            remote_hf_endpoint if remote_hf_endpoint is not None else os.environ.get("LELAB_TRAIN_HF_ENDPOINT", "")
        )
        self.remote_hf_token = remote_hf_token if remote_hf_token is not None else _local_hf_token()
        self.remote_gpu = remote_gpu
        self.min_free_vram_mb = min_free_vram_mb
        self._metrics = metrics
        self._ssh = ssh or _default_ssh
        self._poll_interval = max(0.2, float(poll_interval))
        self._log_file_path = log_file_path
        self._log_file = None
        self._log_queue: Queue[LogLine] = Queue()
        self._log_offset = 0
        self._partial = ""
        self._wandb_run_url: str | None = None
        self._remote_pid: int | None = None
        self._exit_code: int | None = None
        self._finished = False
        self._stop_event = threading.Event()
        self._stop_requested = False
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._remote_dir = posixpath.join(config.root, "jobs", job_id)
        self._event_sink = event_sink
        self._run_id = run_id
        self._attempt_id = attempt_id
        self._log_offset_path = (
            log_file_path.parent / "remote-log.offset" if log_file_path is not None else None
        )

    supports_pause = True

    @property
    def remote_events_path(self) -> str:
        return posixpath.join(self._remote_dir, "metrics", "worker-events.jsonl")

    def _persist_log_offset(self) -> None:
        if self._log_offset_path is None:
            return
        try:
            self._log_offset_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._log_offset_path.with_suffix(".tmp")
            tmp.write_text(str(int(self._log_offset)), encoding="utf-8")
            os.replace(tmp, self._log_offset_path)
        except OSError:
            pass

    def _read_log_offset(self) -> int | None:
        if self._log_offset_path is None:
            return None
        try:
            return int(self._log_offset_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def ingest_remote_events(self) -> None:
        """Replay structured events written while LeLab was disconnected.

        The worker's append-only events file is authoritative; each line goes
        through the event sink (deduped by the store), not the log panel, so a
        restart does not duplicate log entries.
        """
        if self._event_sink is None:
            return
        try:
            text = ssh_ops.read_remote_file(
                self.config, self.remote_events_path, max_bytes=8 * 1024 * 1024
            )
        except Exception as exc:
            logger.warning("Could not read remote events for %s: %s", self.job_id, exc)
            return
        if not text:
            return
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                self._event_sink(line, time.time())
            except Exception as exc:
                logger.warning("Remote event replay failed: %s", exc)

    @property
    def remote_control_dir(self) -> str:
        return posixpath.join(self._remote_dir, "control")

    def request_pause(self) -> None:
        """Place a bounded control marker beside the job on the GPU host.

        The remote script verifies that the recorded PID's cwd is this job's
        directory (PID-reuse / foreign-process guard) before writing anything.
        """
        return_code, output = self._run_script(_PAUSE_SCRIPT, self.remote_dir, check=False)
        if return_code != 0:
            detail = " ".join(line.strip() for line in output.splitlines() if line.strip())[-400:]
            raise RemoteTrainingError(
                f"Could not request pause on {self.config.endpoint} (exit {return_code}): "
                f"{detail or 'no output'}"
            )

    def read_pause_state(self) -> dict | None:
        try:
            return_code, output = self._run_script(
                _READ_PAUSE_STATE_SCRIPT, self.remote_dir, check=False
            )
        except Exception:
            return None
        if return_code != 0 or not output.strip():
            return None
        try:
            payload = json.loads(output.strip().splitlines()[-1])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    # ------------------------------------------------------------- topology
    @property
    def remote_dir(self) -> str:
        return self._remote_dir

    @property
    def remote_output_dir(self) -> str:
        return posixpath.join(self.remote_dir, "run")

    @property
    def remote_log_path(self) -> str:
        return posixpath.join(self.remote_dir, "run.log")

    def dataset_dir(self, repo_id: str) -> str:
        return posixpath.join(self.config.root, "datasets", *repo_id.split("/"))

    # --------------------------------------------------------------- ssh io
    def _run_script(
        self,
        script: str,
        *arguments: str,
        check: bool = True,
    ) -> tuple[int, str]:
        """Run `script` on the server with `sh -s -- <arguments>`."""
        argv = [
            *ssh_base_command(self.config),
            self.config.endpoint,
            "sh -s -- " + " ".join(shlex.quote(argument) for argument in arguments),
        ]
        return_code, output = self._ssh(argv, script)
        if check and return_code != 0:
            detail = " ".join(line.strip() for line in output.splitlines() if line.strip())[-600:]
            raise RemoteTrainingError(
                f"Remote command failed on {self.config.endpoint} (exit {return_code}): {detail}"
            )
        return return_code, output

    def _upload_text(self, path: str, content: str) -> None:
        """Stream `content` into `path` on the train server via SSH stdin."""
        script = (
            "set -eu\n"
            + _EXPAND_PATH_FUNCTION
            + 'path=$(expand_remote_path "$1")\nmkdir -p -- "$(dirname "$path")"\ncat > "$path"\n'
        )
        command = "sh -c " + shlex.quote(script) + " sh " + shlex.quote(path)
        argv = [*ssh_base_command(self.config), self.config.endpoint, command]
        return_code, output = self._ssh(argv, content)
        if return_code != 0:
            raise RemoteTrainingError(f"Could not write {path}: {output.strip()[-300:]}")

    # ---------------------------------------------------------------- start
    def _ensure_dataset_ready(self, repo_id: str) -> None:
        dataset_dir = self.dataset_dir(repo_id)
        script = (
            "set -eu\n"
            + _EXPAND_PATH_FUNCTION
            # Exit codes: 3 = no `.ready` marker, anything else (255 from ssh,
            # 127 from a missing shell, ...) is a transport/tooling failure and
            # must not be reported to the UI as "dataset not synchronized".
            + 'dir=$(expand_remote_path "$1")\n'
            + 'if [ ! -f "$dir/.ready" ]; then exit 3; fi\n'
        )
        return_code, output = self._run_script(script, dataset_dir, check=False)
        if return_code == 3:
            raise RemoteTrainingError(
                f"Dataset {repo_id} is not synchronized to {self.config.endpoint} "
                "(missing .ready). Run the sync action first."
            )
        if return_code != 0:
            detail = " ".join(line.strip() for line in output.splitlines() if line.strip())[-400:]
            raise RemoteTrainingError(
                f"Could not check the dataset on {self.config.endpoint} "
                f"(ssh exit {return_code}): {detail or 'no output'}"
            )

    def _upload_weights_file(self, config: TrainingRequest) -> None:
        """Write the per-episode sampling weights next to the training config.

        The launch script runs LeRobot with the remote job directory as its
        working directory, so the command can reference ``weights.json``
        directly without leaking an absolute path into the staged config.
        """
        weights = config.dataset_episode_weights
        if not weights:
            raise RemoteTrainingError(
                f"dataset_weights_mode={config.dataset_weights_mode!r} but no episode weights "
                "were resolved; restart the job from the training page"
            )
        self._ensure_weighted_sampling_supported()
        payload = {
            "mode": config.dataset_weights_mode,
            "members": list(config.dataset_ids),
            "member_weights": list(config.dataset_weights or []),
            "episode_weights": [float(weight) for weight in weights],
        }
        self._upload_text(
            posixpath.join(self.remote_dir, "weights.json"),
            json.dumps(payload, indent=2, sort_keys=True),
        )

    def _ensure_weighted_sampling_supported(self) -> None:
        """Fail fast when the remote lerobot predates weighted sampling or explicit eval splits."""
        source = (
            "import sys\n"
            "from lerobot.configs.default import DatasetConfig\n"
            "fields = getattr(DatasetConfig, '__dataclass_fields__', {})\n"
            "missing = [n for n in ('sample_weights_file', 'eval_episodes') if n not in fields]\n"
            "sys.stderr.write(','.join(missing))\n"
            "sys.exit(0 if not missing else 3)\n"
        )
        interpreter = (self.remote_python or DEFAULT_REMOTE_PYTHON).strip() or DEFAULT_REMOTE_PYTHON
        argv = [
            *ssh_base_command(self.config),
            self.config.endpoint,
            f"{shlex.quote(interpreter)} -",
        ]
        return_code, output = self._ssh(argv, source)
        if return_code == 3:
            missing = output.strip() or "sample_weights_file"
            raise RemoteTrainingError(
                f"训练机（{self.config.endpoint}）上的 lerobot 缺少 DatasetConfig 字段：{missing}。"
                "请先同步本地 lerobot fork，或改用 dataset_weights_mode='frames' 并去掉显式验证集。"
            )
        if return_code != 0:
            detail = " ".join(line.strip() for line in output.splitlines() if line.strip())[-300:]
            raise RemoteTrainingError(
                f"无法检查训练机的加权采样支持（ssh exit {return_code}）：{detail or 'no output'}"
            )

    def start(self, job_id: str, config: TrainingRequest, output_dir: str) -> None:
        """Prepare the remote job directory, write the config and launch."""
        del output_dir  # the remote tree owns the run directory
        if self._thread is not None:
            raise RemoteTrainingError("Runner already started")
        self._ensure_dataset_ready(config.dataset_repo_id)
        if config.dataset_weights_mode != "frames":
            self._upload_weights_file(config)

        self._run_script(_PREPARE_SCRIPT, self.remote_dir)

        payload = {
            "job_id": job_id,
            "created_at": _timestamp(),
            "source": "jetson",
            "remote_output_dir": self.remote_output_dir,
            "dataset_dir": self.dataset_dir(config.dataset_repo_id),
            "request": config.model_dump(mode="json"),
        }
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        payload["config_sha256"] = _sha256_manifest_id(serialized)
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        self._upload_text(posixpath.join(self.remote_dir, "train_config.json"), serialized)

        # Ownership marker: the remote cleanup/delete paths only ever touch
        # directories that carry our instance id.
        from ..train_server import marker_payload

        self._upload_text(
            posixpath.join(self.remote_dir, ".lelab-job.json"),
            marker_payload(
                "train",
                job_id=job_id,
                dataset=config.dataset_repo_id,
                datasets=list(config.dataset_ids) if len(config.dataset_ids) > 1 else None,
                composite_hash=config.dataset_composite_hash,
                weights_mode=config.dataset_weights_mode,
                steps=config.steps,
                batch_size=config.batch_size,
                gpu=self.remote_gpu,
                min_free_vram_mb=self.min_free_vram_mb,
            ),
        )

        command = build_training_command(
            config,
            # LeRobot writes checkpoints under <output_dir>/checkpoints; the
            # `run` subdirectory keeps job metadata and logs separate.
            # _LAUNCH_SCRIPT first changes into the remote job directory, so
            # a relative output avoids quoted "~/" paths that cannot expand.
            "run",
            self.remote_python,
        )
        command_text = " ".join(shlex.quote(part) for part in command)
        is_resume = bool(config.resume_config_path)
        # Capture the old log size BEFORE launching: on resume the new process
        # appends, and measuring afterwards could skip its first events.
        try:
            resume_offset = self._remote_log_size() if is_resume else 0
        except RemoteTrainingError:
            resume_offset = 0
        self._run_script(
            _LAUNCH_SCRIPT,
            self.remote_dir,
            command_text,
            self.config.root,
            self.remote_proxy,
            self.remote_hf_endpoint,
            self.remote_hf_token,
            "" if self.remote_gpu is None else str(self.remote_gpu),
            self._run_id or job_id,
            self._attempt_id or "",
            "0" if is_resume else "1",
        )
        self._log_offset = resume_offset
        self._persist_log_offset()
        state = self._fetch_state()
        if state.get("pid") is not None:
            self._remote_pid = state["pid"]
        logger.info(
            "Launched remote job %s on %s (pid %s, dir %s)",
            job_id,
            self.config.endpoint,
            self._remote_pid,
            self.remote_dir,
        )
        # Best-effort replay of the durable event sidecar (covers a resume
        # whose first lines landed before the tail offset was established).
        self.ingest_remote_events()
        self._start_poller()

    def reattach(self, *, remote_dir: str | None = None) -> None:
        """Re-attach to a job that kept running across a Jetson reload."""
        if remote_dir:
            self._remote_dir = remote_dir
        # Resume from the last consumed byte, not the current EOF: lines written
        # while LeLab was down must still be ingested. Fall back to EOF only
        # when no persisted offset exists.
        persisted = self._read_log_offset()
        try:
            size = self._remote_log_size()
        except RemoteTrainingError:
            size = 0
        if persisted is None or persisted > size:
            self._log_offset = size
        else:
            self._log_offset = persisted
        self.ingest_remote_events()
        state = self._fetch_state()
        self._remote_pid = state.get("pid")
        if state.get("exit_code") is not None:
            self._exit_code = state["exit_code"]
            self._finished = True
            return
        self._start_poller()

    def _remote_log_size(self) -> int:
        script = (
            "set -eu\n"
            + _EXPAND_PATH_FUNCTION
            + 'dir=$(expand_remote_path "$1")\ncd "$dir"\nwc -c < run.log 2>/dev/null || echo 0\n'
        )
        _code, output = self._run_script(script, self.remote_dir)
        try:
            return int(output.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 0

    # ------------------------------------------------------------- lifecycle
    def _fetch_state(self) -> dict[str, int | None]:
        return_code, output = self._run_script(_STATE_SCRIPT, self.remote_dir, check=False)
        if return_code != 0:
            return {"pid": None, "running": False, "exit_code": None}
        first = output.strip().splitlines()[-1].split() if output.strip() else []
        if not first:
            return {"pid": None, "running": False, "exit_code": None}
        kind = first[0]
        pid = int(first[1]) if len(first) > 1 and first[1].isdigit() else None
        if kind == "running":
            return {"pid": pid, "running": True, "exit_code": None}
        if kind == "exit" and len(first) > 2 and first[2].lstrip("-").isdigit():
            return {"pid": pid, "running": False, "exit_code": int(first[2])}
        return {"pid": pid, "running": False, "exit_code": None}

    def is_running(self) -> bool:
        if self._finished:
            return False
        state = self._fetch_state()
        if state.get("running"):
            if state.get("pid") is not None:
                self._remote_pid = state["pid"]
            return True
        if state.get("exit_code") is not None:
            self._exit_code = state["exit_code"]
        self._finished = True
        self._drain_remote_log(final=True)
        return False

    def returncode(self) -> int | None:
        return self._exit_code

    @property
    def stop_requested(self) -> bool:
        """True when the operator stopped this run (final state: interrupted)."""
        return self._stop_requested

    def pid(self) -> int | None:
        return self._remote_pid

    def stop(self) -> None:
        """Terminate the remote process group (TERM first, then KILL)."""
        if self._finished:
            return
        self._stop_event.set()
        self._stop_requested = True
        stop_rc, stop_out = self._run_script(_STOP_SCRIPT, self.remote_dir, check=False)
        if stop_rc == 4:
            self._stop_requested = False
            detail = " ".join(line.strip() for line in stop_out.splitlines() if line.strip())[-300:]
            raise RemoteTrainingError(f"拒绝终止不属于该任务的远端进程：{detail}")
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            state = self._fetch_state()
            if not state.get("running"):
                self._exit_code = state.get("exit_code") if state.get("exit_code") is not None else -15
                self._finished = True
                return
            time.sleep(0.5)
        kill_script = (
            "set -eu\n"
            + _EXPAND_PATH_FUNCTION
            + 'dir=$(expand_remote_path "$1")\ncd "$dir"\npid=$(cat run.pid 2>/dev/null || true)\n'
            'cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)\n'
            'target=$(readlink -f "$dir" 2>/dev/null || printf \'%s\' "$dir")\n'
            'if [ -n "$cwd" ] && [ "$cwd" != "$target" ]; then exit 4; fi\n'
            '[ -n "$pid" ] && kill -KILL "-$pid" 2>/dev/null || true\n'
        )
        self._run_script(kill_script, self.remote_dir, check=False)
        self._exit_code = -9
        self._finished = True

    def wandb_run_url(self) -> str | None:
        return self._wandb_run_url

    def stream_log_lines(self) -> list[LogLine]:
        out: list[LogLine] = []
        while True:
            try:
                out.append(self._log_queue.get_nowait())
            except Empty:
                break
        return out

    # ------------------------------------------------------------- log tail
    def _drain_remote_log(self, *, final: bool = False) -> None:
        script = (
            "set -eu\n"
            + _EXPAND_PATH_FUNCTION
            + 'dir=$(expand_remote_path "$1")\ncd "$dir"\ntail -c "+$2" run.log 2>/dev/null || true\n'
        )
        return_code, chunk = self._run_script(
            script, self.remote_dir, str(self._log_offset + 1), check=False
        )
        if return_code != 0 or not chunk:
            return
        if len(chunk) > _MAX_LOG_CHUNK:
            # Never let a chatty run (e.g. a re-attach after hours) block the
            # watchdog: skip ahead and keep the most recent chunk.
            chunk = chunk[-_MAX_LOG_CHUNK:]
            with contextlib.suppress(RemoteTrainingError):
                self._log_offset = max(0, self._remote_log_size() - len(chunk))
        else:
            self._log_offset += len(chunk.encode("utf-8", errors="replace"))
        self._persist_log_offset()
        text = self._partial + chunk
        lines = text.split("\n")
        self._partial = lines.pop()
        self._ingest(lines)
        if final and self._partial.strip():
            self._ingest([self._partial])
            self._partial = ""

    def _ingest(self, lines: list[str]) -> None:
        for line in lines:
            stripped = line.rstrip("\r")
            if not stripped.strip():
                continue
            parse_metrics_into(stripped, self._metrics)
            if self._wandb_run_url is None:
                url = extract_wandb_run_url(stripped)
                if url is not None:
                    self._wandb_run_url = url
            timestamp = time.time()
            if self._event_sink is not None:
                try:
                    self._event_sink(stripped, timestamp)
                except Exception as exc:  # monitoring must never stop training
                    logger.warning("Metric event sink failed: %s", exc)
            log_line = LogLine(timestamp=timestamp, message=stripped)
            self._append_log(log_line)
            if self._log_queue.qsize() >= 1000:
                with contextlib.suppress(Empty):
                    self._log_queue.get_nowait()
            self._log_queue.put(log_line)

    def _append_log(self, log_line: LogLine) -> None:
        if self._log_file_path is None:
            return
        try:
            self._log_file_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_file_path.open("a", encoding="utf-8") as stream:
                stream.write(log_line.model_dump_json() + "\n")
        except OSError as exc:  # best-effort persistence
            logger.warning("Could not append to %s: %s", self._log_file_path, exc)

    def _start_poller(self) -> None:
        self._thread = threading.Thread(
            target=self._poll_loop, name=f"ssh-job-{self.job_id}-log", daemon=True
        )
        self._thread.start()

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._drain_remote_log()
            except Exception:  # a transient SSH failure must not kill the tailer
                logger.exception("Log tail failed for remote job %s", self.job_id)
            with self._state_lock:
                if self._finished:
                    break
            if self._stop_event.wait(self._poll_interval):
                break
