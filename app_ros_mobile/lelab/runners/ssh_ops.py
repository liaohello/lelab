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
"""Generic SSH primitives for the GPU training server.

Shared by :class:`~lelab.runners.ssh_remote.SshRemoteJobRunner` (training) and
:mod:`lelab.remote_inference` (cloud PolicyServer + policy staging). Remote
commands are always sent as ``sh -s -- <quoted args>`` with the script on
stdin, so no user value is ever interpolated into a remote shell string.

Environment knobs (all optional):

``LELAB_TRAIN_PYTHON``
    Interpreter used for remote lerobot entry points. Defaults to ``python3``.
``LELAB_TRAIN_PROXY``
    HTTP(S) proxy the remote process should use (normally the train server's
    loopback end of a reverse Mihomo tunnel, e.g. ``http://127.0.0.1:17890``).
``LELAB_TRAIN_HF_ENDPOINT``
    Hugging Face endpoint override for the remote process
    (``https://hf-mirror.com`` when the host cannot reach huggingface.co).
"""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import re
import shlex
import subprocess
import uuid
from collections.abc import Mapping
from pathlib import Path

from ..remote_sync import TrainServerConfig, ssh_base_command

logger = logging.getLogger(__name__)

DEFAULT_SSH_TIMEOUT = 60.0

# Remote shells do not expand a leading ``~/`` for values that arrive as
# arguments, so every script sources this helper and calls it.
EXPAND_PATH_FUNCTION = r'''expand_remote_path() {
  value=$1
  case "$value" in
    "~") printf '%s\n' "$HOME" ;;
    "~/"*) printf '%s/%s\n' "$HOME" "${value#\~/}" ;;
    *) printf '%s\n' "$value" ;;
  esac
}
'''


def default_ssh(
    argv: list[str],
    input_text: str | None = None,
    *,
    timeout: float = DEFAULT_SSH_TIMEOUT,
) -> tuple[int, str]:
    """Run one SSH invocation; never uses ``shell=True``."""
    try:
        completed = subprocess.run(
            argv,
            # Keep LF newlines byte-for-byte. In text mode Windows translates
            # them to CRLF, and the remote /bin/sh then sees `set -eu\r`.
            input=input_text.encode("utf-8") if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"SSH client not available: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"SSH command timed out after {timeout:.0f}s") from exc
    output = completed.stdout.decode("utf-8", errors="replace") if completed.stdout else ""
    return completed.returncode, output


def default_transfer(argv: list[str], *, timeout: float = 1800.0) -> tuple[int, str]:
    """Run a local file-transfer command without invoking a shell."""
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"File-transfer tool not available: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Policy transfer timed out after {timeout:.0f}s") from exc
    output = completed.stdout.decode("utf-8", errors="replace") if completed.stdout else ""
    return completed.returncode, output


def run_script(
    config: TrainServerConfig,
    script: str,
    *arguments: str,
    timeout: float = DEFAULT_SSH_TIMEOUT,
    ssh=default_ssh,
    check: bool = False,
) -> tuple[int, str]:
    """Run ``script`` on the train server with ``sh -s -- <arguments>``."""
    argv = [
        *ssh_base_command(config),
        config.endpoint,
        "sh -s -- " + " ".join(shlex.quote(argument) for argument in arguments),
    ]
    try:
        return ssh(argv, script, timeout=timeout)
    except TypeError:
        # Injected test doubles only accept (argv, input_text).
        return ssh(argv, script)


def run_script_checked(
    config: TrainServerConfig,
    script: str,
    *arguments: str,
    timeout: float = DEFAULT_SSH_TIMEOUT,
    ssh=default_ssh,
) -> str:
    return_code, output = run_script(config, script, *arguments, timeout=timeout, ssh=ssh)
    if return_code != 0:
        detail = " ".join(line.strip() for line in output.splitlines() if line.strip())[-600:]
        raise RuntimeError(f"Remote command failed on {config.endpoint} (exit {return_code}): {detail}")
    return output


def remote_python(env: Mapping[str, str] | None = None) -> str:
    """Interpreter used for remote lerobot runs (absolute path recommended)."""
    values = os.environ if env is None else env
    return (values.get("LELAB_TRAIN_PYTHON") or "").strip() or "python3"


def remote_proxy(env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    return (values.get("LELAB_TRAIN_PROXY") or "").strip()


def remote_hf_endpoint(env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    return (values.get("LELAB_TRAIN_HF_ENDPOINT") or "").strip()


def _python_env_prelude(env: Mapping[str, str] | None) -> str:
    """Render ``os.environ`` assignments so secrets stay off the remote argv."""
    if not env:
        return ""
    lines = ["import os"]
    for key, value in sorted(env.items()):
        if value:
            lines.append(f"os.environ[{key!r}] = {value!r}")
    lines.append("")
    return "\n".join(lines) + "\n"


def run_python(
    config: TrainServerConfig,
    source: str,
    *arguments: str,
    python: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_SSH_TIMEOUT,
    ssh=default_ssh,
) -> tuple[int, str]:
    """Run inline Python on the train server with the script on stdin.

    ``env`` values (HF tokens, proxies, ...) are written into the script body
    rather than passed as arguments, so they never show up in the remote
    process table.
    """
    interpreter = (python or remote_python()).strip() or "python3"
    body = _python_env_prelude(env) + source
    argv = [
        *ssh_base_command(config),
        config.endpoint,
        f"{shlex.quote(interpreter)} - " + " ".join(shlex.quote(a) for a in arguments),
    ]
    return ssh(argv, body, timeout=timeout)


def read_remote_file(
    config: TrainServerConfig,
    path: str,
    *,
    max_bytes: int = 512 * 1024,
    timeout: float = DEFAULT_SSH_TIMEOUT,
    ssh=default_ssh,
) -> str | None:
    """Return a remote text file, or ``None`` when it does not exist."""
    script = (
        "set -eu\n"
        + EXPAND_PATH_FUNCTION
        + 'path=$(expand_remote_path "$1")\n'
        + 'if [ ! -f "$path" ]; then exit 3; fi\n'
        + f'head -c {int(max_bytes)} -- "$path"\n'
    )
    return_code, output = run_script(config, script, path, timeout=timeout, ssh=ssh)
    if return_code == 3:
        return None
    if return_code != 0:
        raise RuntimeError(f"Could not read {path} on {config.endpoint}: {output.strip()[-300:]}")
    return output


def remote_dir_exists(
    config: TrainServerConfig,
    path: str,
    *,
    timeout: float = DEFAULT_SSH_TIMEOUT,
    ssh=default_ssh,
) -> bool:
    script = "set -eu\n" + EXPAND_PATH_FUNCTION + 'dir=$(expand_remote_path "$1")\ntest -d "$dir"\n'
    return_code, _ = run_script(config, script, path, timeout=timeout, ssh=ssh)
    return return_code == 0


def remote_pid_alive(
    config: TrainServerConfig,
    pid: int,
    *,
    timeout: float = DEFAULT_SSH_TIMEOUT,
    ssh=default_ssh,
) -> bool:
    if pid <= 0:
        return False
    script = 'kill -0 "$1" 2>/dev/null\n'
    return_code, _ = run_script(config, script, str(pid), timeout=timeout, ssh=ssh)
    return return_code == 0


def stop_remote_pid(
    config: TrainServerConfig,
    pid: int,
    *,
    timeout: float = DEFAULT_SSH_TIMEOUT,
    ssh=default_ssh,
) -> None:
    """TERM the pid (its process group first), then KILL if it lingers."""
    if pid <= 0:
        return
    script = (
        "set -eu\n"
        'pid=$1\n'
        'if ! kill -0 "$pid" 2>/dev/null; then exit 0; fi\n'
        'kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true\n'
        "i=0\n"
        'while [ "$i" -lt 20 ]; do\n'
        '  kill -0 "$pid" 2>/dev/null || exit 0\n'
        "  i=$((i + 1))\n"
        "  sleep 0.5\n"
        "done\n"
        'kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true\n'
    )
    run_script(config, script, str(pid), timeout=timeout, ssh=ssh)


# --------------------------------------------------------------------------
# Checkpoints (training output) on the train server
# --------------------------------------------------------------------------

_LIST_CHECKPOINTS_SCRIPT = (
    "set -eu\n"
    + EXPAND_PATH_FUNCTION
    + r"""job_dir=$(expand_remote_path "$1")
# Training jobs keep lerobot's output under <job>/run/checkpoints; imported
# or hand-made model dirs may put checkpoints/ at the top level.
root="$job_dir/checkpoints"
[ -d "$root" ] || root="$job_dir/run/checkpoints"
[ -d "$root" ] || exit 0
for entry in "$root"/*; do
  [ -d "$entry/pretrained_model" ] || continue
  step=$(basename "$entry")
  case "$step" in
    *[!0-9]*) continue ;;
  esac
  printf '%s\t%s\n' "$step" "$entry/pretrained_model"
done
"""
)


def list_remote_checkpoints(
    config: TrainServerConfig,
    run_dir: str,
    *,
    timeout: float = DEFAULT_SSH_TIMEOUT,
    ssh=default_ssh,
) -> list[tuple[int, str]]:
    """Return ``[(step, remote_pretrained_model_dir), ...]`` ascending."""
    return_code, output = run_script(config, _LIST_CHECKPOINTS_SCRIPT, run_dir, timeout=timeout, ssh=ssh)
    if return_code != 0:
        detail = " ".join(line.strip() for line in output.splitlines() if line.strip())[-300:]
        raise RuntimeError(f"Could not list checkpoints in {run_dir}: {detail}")
    entries: list[tuple[int, str]] = []
    for line in output.splitlines():
        step_text, _, path = line.partition("\t")
        if not path:
            continue
        try:
            step = int(step_text)
        except ValueError:
            continue
        entries.append((step, path))
    entries.sort(key=lambda item: item[0])
    return entries


# --------------------------------------------------------------------------
# Hub policy staging (resolve a UI ref into a server-local directory)
# --------------------------------------------------------------------------

# Hub refs are staged with an explicit `local_dir` rather than the shared hub
# cache: the mirror serves the plain-file path reliably, while the hub-cache
# blob path has been observed stalling at 0 bytes behind a mirror redirect.
_RESOLVE_POLICY_SOURCE = r'''
import json
import os
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

ref = json.loads(sys.argv[1])
kind = ref["kind"]

if kind == "local":
    path = Path(ref["path"]).expanduser()
    if not path.is_dir():
        sys.exit(f"policy directory not found on the training server: {path}")
    print("POLICY_PATH=" + str(path))
    sys.exit(0)

stage_root = Path(
    os.environ.get("LELAB_TRAIN_POLICY_STAGE", str(Path.home() / ".cache" / "lelab" / "policies"))
).expanduser()
slug = ref["repo_id"].replace("/", "--")
dest = stage_root / slug
dest.mkdir(parents=True, exist_ok=True)

allow_patterns = None
if kind == "hub_tree":
    allow_patterns = [f"checkpoints/{ref['step_dir']}/pretrained_model/*"]

snapshot_download(
    repo_id=ref["repo_id"],
    repo_type="model",
    allow_patterns=allow_patterns,
    local_dir=str(dest),
)

path = dest
if kind == "hub_tree":
    path = dest / "checkpoints" / ref["step_dir"] / "pretrained_model"
if not path.is_dir():
    sys.exit(f"policy tree missing after download: {path}")

print("POLICY_PATH=" + str(path))
'''


_PREPARE_LOCAL_POLICY_STAGE = (
    "set -eu\n"
    + EXPAND_PATH_FUNCTION
    + r'''stage_root=$(expand_remote_path "$1")
slug=$2
fingerprint=$3
target="$stage_root/$slug"
marker="$target/.lelab-policy-fingerprint"
if [ -d "$target" ] && [ -f "$marker" ] && [ "$(cat "$marker")" = "$fingerprint" ]; then
  printf 'POLICY_PATH=%s\n' "$target"
  exit 0
fi
incoming="$stage_root/.incoming/$slug-$4"
mkdir -p -- "$incoming"
printf 'STAGING_PATH=%s\n' "$incoming"
'''
)


_FINALIZE_LOCAL_POLICY_STAGE = (
    "set -eu\n"
    + EXPAND_PATH_FUNCTION
    + r'''stage_root=$(expand_remote_path "$1")
slug=$2
fingerprint=$3
incoming=$4
target="$stage_root/$slug"
case "$incoming" in
  "$stage_root/.incoming/"*) ;;
  *) printf 'invalid policy staging path: %s\n' "$incoming" >&2; exit 4 ;;
esac
[ -d "$incoming" ] || { printf 'policy staging directory disappeared: %s\n' "$incoming" >&2; exit 5; }
[ -n "$(find "$incoming" -mindepth 1 -maxdepth 1 -print -quit)" ] || {
  printf 'policy staging directory is empty: %s\n' "$incoming" >&2; exit 6;
}
printf '%s\n' "$fingerprint" > "$incoming/.lelab-policy-fingerprint"
if [ -e "$target" ]; then
  rm -rf -- "$incoming"
else
  mv -- "$incoming" "$target"
fi
printf 'POLICY_PATH=%s\n' "$target"
'''
)


def _local_policy_fingerprint(source: Path) -> str:
    """Return a stable, inexpensive fingerprint for an immutable checkpoint."""
    digest = hashlib.sha256()
    digest.update(str(source.resolve()).encode("utf-8", errors="surrogatepass"))
    for entry in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
        relative = entry.relative_to(source).as_posix()
        stat = entry.stat()
        digest.update(relative.encode("utf-8", errors="surrogatepass"))
        digest.update(f"\0{stat.st_size}\0{stat.st_mtime_ns}\0".encode())
    return digest.hexdigest()


def _local_policy_slug(source: Path, fingerprint: str) -> str:
    useful_parts = [part for part in source.parts[-5:] if part not in {"/", "\\"}]
    label = "--".join(useful_parts)
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-.") or "policy"
    return f"local--{label[:88]}--{fingerprint[:16]}"


def _stage_local_policy_directory(
    config: TrainServerConfig,
    source: Path,
    *,
    env: Mapping[str, str] | None,
    timeout: float,
    ssh,
    transfer,
) -> str:
    """Copy a Jetson-local checkpoint to an atomic server-side stage."""
    fingerprint = _local_policy_fingerprint(source)
    slug = _local_policy_slug(source, fingerprint)
    stage_root = (env or {}).get(
        "LELAB_TRAIN_POLICY_STAGE", posixpath.join(config.root, "models", "policies")
    )
    nonce = uuid.uuid4().hex
    return_code, output = run_script(
        config,
        _PREPARE_LOCAL_POLICY_STAGE,
        stage_root,
        slug,
        fingerprint,
        nonce,
        timeout=timeout,
        ssh=ssh,
    )
    if return_code != 0:
        detail = " ".join(line.strip() for line in output.splitlines() if line.strip())[-500:]
        raise RuntimeError(f"Could not prepare policy staging on {config.endpoint}: {detail}")
    ready = re.search(r"^POLICY_PATH=(.+)$", output, flags=re.MULTILINE)
    if ready:
        return ready.group(1).strip()
    staging_match = re.search(r"^STAGING_PATH=(.+)$", output, flags=re.MULTILINE)
    if not staging_match:
        raise RuntimeError(f"Policy staging produced no destination: {output.strip()[-300:]}")
    staging = staging_match.group(1).strip()

    ssh_transport = shlex.join(ssh_base_command(config))
    destination = f"{config.endpoint}:{shlex.quote(staging)}/"
    command = [
        "rsync",
        "-a",
        "--partial",
        "--append-verify",
        "--info=stats2",
        "-e",
        ssh_transport,
        f"{source}/",
        destination,
    ]
    try:
        transfer_code, transfer_output = transfer(command, timeout=timeout)
    except TypeError:
        transfer_code, transfer_output = transfer(command)
    if transfer_code != 0:
        detail = " ".join(line.strip() for line in transfer_output.splitlines() if line.strip())[-500:]
        raise RuntimeError(
            f"Could not copy local policy to {config.endpoint} (rsync exit {transfer_code}): {detail}"
        )

    return_code, output = run_script(
        config,
        _FINALIZE_LOCAL_POLICY_STAGE,
        stage_root,
        slug,
        fingerprint,
        staging,
        timeout=timeout,
        ssh=ssh,
    )
    if return_code != 0:
        detail = " ".join(line.strip() for line in output.splitlines() if line.strip())[-500:]
        raise RuntimeError(f"Could not finalize policy staging on {config.endpoint}: {detail}")
    match = re.search(r"^POLICY_PATH=(.+)$", output, flags=re.MULTILINE)
    if match:
        return match.group(1).strip()
    raise RuntimeError(f"Policy finalization produced no path: {output.strip()[-300:]}")


def stage_policy_on_server(
    config: TrainServerConfig,
    policy_ref,
    *,
    python: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 1800.0,
    ssh=default_ssh,
    transfer=default_transfer,
) -> str:
    """Make ``policy_ref`` resolvable on the train server; return its path.

    Hub refs are downloaded there (through the configured mirror/proxy).
    A path that exists on this Jetson is synchronized to an atomic server-side
    stage; a path that only exists on the server is checked there. The returned
    path is what the robot client sends to the PolicyServer as
    ``pretrained_name_or_path``.
    """
    import json as _json

    payload = {"kind": policy_ref.kind}
    if policy_ref.kind == "local":
        local_path = Path(policy_ref.path).expanduser()
        if local_path.is_dir():
            return _stage_local_policy_directory(
                config,
                local_path,
                env=env,
                timeout=timeout,
                ssh=ssh,
                transfer=transfer,
            )
        payload["path"] = policy_ref.path
    else:
        payload["repo_id"] = policy_ref.repo_id
        payload["step_dir"] = policy_ref.step_dir
    return_code, output = run_python(
        config,
        _RESOLVE_POLICY_SOURCE,
        _json.dumps(payload),
        python=python,
        env=env,
        timeout=timeout,
        ssh=ssh,
    )
    if return_code != 0:
        detail = " ".join(line.strip() for line in output.splitlines() if line.strip())[-500:]
        raise RuntimeError(f"Could not stage policy {policy_ref.raw!r} on {config.endpoint}: {detail}")
    # Progress bars (tqdm) write to stderr, which the SSH call merges into the
    # same stream; match the marker anywhere instead of anchoring at line
    # start. HF_HUB_DISABLE_PROGRESS_BARS keeps the output clean anyway.
    match = re.search(r"^POLICY_PATH=(.+)$", output, flags=re.MULTILINE)
    if match:
        return match.group(1).strip()
    raise RuntimeError(f"Policy staging produced no path: {output.strip()[-300:]}")


# --------------------------------------------------------------------------
# Job lifecycle (soft delete)
# --------------------------------------------------------------------------

# Exit codes: 3 = no marker / not ours, 4 = directory missing.
_SOFT_DELETE_SCRIPT = (
    "set -eu\n"
    + EXPAND_PATH_FUNCTION
    + r"""root=$(expand_remote_path "$1")
job_id=$2
instance=$3
jobs_dir="$root/jobs"
job="$jobs_dir/$job_id"
if [ ! -d "$job" ]; then exit 4; fi
marker="$job/.lelab-job.json"
if [ ! -f "$marker" ]; then exit 3; fi
case "$(cat "$marker")" in
  *"\"instance_id\": \"$instance\""*) ;;
  *) exit 3 ;;
esac
trash_dir="$jobs_dir/.trash"
mkdir -p "$trash_dir"
stamp=$(date +%Y%m%d-%H%M%S)
target="$trash_dir/$job_id-$stamp"
mv -- "$job" "$target"
printf '%s\n' "$(basename "$target")"
"""
)


class RemoteJobNotOursError(PermissionError):
    """The remote job directory is missing or belongs to another instance."""


def soft_delete_remote_job(
    config: TrainServerConfig,
    job_id: str,
    *,
    instance_id: str,
    timeout: float = 120.0,
    ssh=default_ssh,
) -> str:
    """Move ``jobs/<job_id>`` into ``jobs/.trash/<job_id>-<stamp>``.

    Only directories carrying our ownership marker are touched; everything
    else raises :class:`RemoteJobNotOursError` so the API can answer 403.
    Returns the trash entry name.
    """
    return_code, output = run_script(
        config, _SOFT_DELETE_SCRIPT, config.root, job_id, instance_id, timeout=timeout, ssh=ssh
    )
    if return_code == 0:
        return output.strip().splitlines()[-1] if output.strip() else ""
    if return_code in (3, 4):
        raise RemoteJobNotOursError(
            f"远端作业 {job_id} 不存在或不属于本系统（exit {return_code}），拒绝删除"
        )
    detail = " ".join(line.strip() for line in output.splitlines() if line.strip())[-300:]
    raise RuntimeError(f"远端软删除失败（exit {return_code}）：{detail}")
