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
"""Read-only view of the GPU training server, collected over one SSH call.

The query channel is SSH — never the inference port (28081). lerobot's gRPC
PolicyServer only exposes ``Ready / SendPolicyInstructions / SendObservations /
GetActions`` and ``Ready()`` calls ``_reset_server()`` internally, so probing it
would corrupt a running session.

The remote script prints section-delimited plain text (no ``jq``/JSON on the
remote host); :func:`parse_status` turns it into the payload the frontend
consumes. Everything here is read-only: the only commands that touch state are
``kill -0`` style liveness probes.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Mapping
from typing import Any

from .remote_sync import TrainServerConfig
from .runners import ssh_ops

logger = logging.getLogger(__name__)

#: The control-plane query costs one SSH round-trip; several pollers (System
#: page, Training GPU picker, Inference modal) ask for it at once, so results
#: are shared for a couple of seconds.
STATUS_TTL_SECONDS = 2.0
_QUERY_TIMEOUT = 30.0
_SECTION_PREFIX = "##"

# The remote script. Sections are printed in a fixed order; missing sections
# simply produce empty lists so a partially-degraded host still renders.
_STATUS_SCRIPT = (
    "set -u\n"
    + ssh_ops.EXPAND_PATH_FUNCTION
    + r"""root=$(expand_remote_path "$1")
jobs_dir="$root/jobs"
server_dir="$root/policy_server"

echo '##GPUS'
nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu,temperature.gpu,uuid \
  --format=csv,noheader,nounits 2>/dev/null || true

echo '##APPS'
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_memory \
    --format=csv,noheader,nounits 2>/dev/null | while IFS= read -r line; do
    # The job's run.pid is the `setsid sh -c ...` wrapper; the CUDA process is
    # its child, so attribute by process group instead of pid.
    app_pid=$(printf '%s' "$line" | awk -F, '{print $2}' | tr -d ' ')
    pgid=$(ps -o pgid= -p "$app_pid" 2>/dev/null | tr -d ' ')
    printf '%s\t%s\n' "$line" "$pgid"
  done
fi

echo '##LISTEN'
ss -ltnp 2>/dev/null | awk 'NR>1 {print $4 "\t" $6}' || true

echo '##JOBS'
if [ -d "$jobs_dir" ]; then
  for job in "$jobs_dir"/*; do
    [ -d "$job" ] || continue
    id=$(basename "$job")
    marker=''
    [ -f "$job/.lelab-job.json" ] && marker=$(tr -d '\n' < "$job/.lelab-job.json")
    pid=$(cat "$job/run.pid" 2>/dev/null || true)
    exit_code=$(cat "$job/run.exit" 2>/dev/null || true)
    alive=0
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then alive=1; fi
    ckpt_count=0
    ckpt_bytes=0
    ckpt_steps=''
    if [ -d "$job/run/checkpoints" ]; then
      for ck in "$job/run/checkpoints"/*; do
        [ -d "$ck/pretrained_model" ] || continue
        step=$(basename "$ck")
        case "$step" in *[!0-9]*) continue ;; esac
        size=$(du -sm "$ck" 2>/dev/null | awk '{print $1}')
        [ -n "$size" ] || size=0
        ckpt_count=$((ckpt_count + 1))
        ckpt_bytes=$((ckpt_bytes + size))
        ckpt_steps="$ckpt_steps $step:$size"
      done
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$id" "$pid" "$exit_code" "$alive" "$ckpt_count" "$ckpt_bytes" "$ckpt_steps" "$marker"
  done
fi

echo '##SESSIONS'
if [ -d "$server_dir" ]; then
  for session in "$server_dir"/*; do
    [ -d "$session" ] || continue
    stamp=$(basename "$session")
    marker=''
    [ -f "$session/.lelab-session.json" ] && marker=$(tr -d '\n' < "$session/.lelab-session.json")
    pid=$(cat "$session/run.pid" 2>/dev/null || true)
    alive=0
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then alive=1; fi
    log_tail=''
    if [ -f "$session/run.log" ]; then
      # Newlines become SOH (\001) so the whole session stays on one line for
      # the section parser; it is turned back into '\n' on the Jetson.
      log_tail=$(tail -n 60 "$session/run.log" 2>/dev/null | tr '\n' '\001' || true)
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' "$stamp" "$pid" "$alive" "$marker" "$log_tail"
  done
fi

echo '##DISK'
df -Pk "$root" 2>/dev/null | awk 'NR==2 {printf "%s %s %s\n", $2, $3, $4}' || true
echo '##WEIGHTS'
for kind in pretrained policies; do
  dir="$root/models/$kind"
  [ -d "$dir" ] || continue
  for entry in "$dir"/*; do
    [ -d "$entry" ] || continue
    size=$(du -sm "$entry" 2>/dev/null | awk '{print $1}')
    [ -n "$size" ] || size=0
    printf '%s\t%s\t%s\n' "$kind" "$(basename "$entry")" "$size"
  done
done
echo '##SIZES'
if [ -d "$jobs_dir" ]; then du -sm "$jobs_dir" 2>/dev/null | awk '{print $1}'; fi
if [ -d "$server_dir" ]; then du -sm "$server_dir" 2>/dev/null | awk '{print $1}'; fi
echo '##END'
"""
)


def _parse_sections(output: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in output.splitlines():
        line = raw.rstrip("\r")
        if line.startswith(_SECTION_PREFIX):
            current = line[len(_SECTION_PREFIX) :].strip()
            sections[current] = []
            continue
        if current is not None and line != "":
            sections[current].append(line)
    return sections


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return default


def _parse_checkpoints(spec: str) -> list[dict[str, Any]]:
    """Parse " 000100:210 000200:212" into sorted checkpoint dicts."""
    checkpoints: list[dict[str, Any]] = []
    for token in spec.split():
        step_text, _, size_text = token.partition(":")
        try:
            step = int(step_text)
        except ValueError:
            continue
        checkpoints.append({"step": step, "size_mb": _to_int(size_text)})
    checkpoints.sort(key=lambda item: item["step"])
    return checkpoints


def _is_ours(marker_text: str, instance_id: str) -> bool:
    """A job/session belongs to us when its marker names our instance."""
    if not marker_text.strip():
        return False
    try:
        import json

        marker = json.loads(marker_text)
    except Exception:
        return False
    if not isinstance(marker, dict):
        return False
    return str(marker.get("instance_id", "")).strip() == instance_id


def parse_status(
    output: str,
    *,
    instance_id: str,
    policy_port: int = 28081,
    fetched_at: float | None = None,
) -> dict[str, Any]:
    """Turn the remote script's output into the frontend payload."""
    sections = _parse_sections(output)
    our_pids: set[int] = set()

    gpus: list[dict[str, Any]] = []
    uuid_to_index: dict[str, int] = {}
    for line in sections.get("GPUS", []):
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            continue
        total = _to_int(parts[2])
        used = _to_int(parts[3])
        uuid = parts[6] if len(parts) > 6 else ""
        if uuid:
            uuid_to_index[uuid] = _to_int(parts[0])
        gpus.append(
            {
                "index": _to_int(parts[0]),
                "name": parts[1],
                "mem_total_mb": total,
                "mem_used_mb": used,
                "mem_free_mb": max(0, total - used),
                "util_percent": _to_int(parts[4]),
                "temp_c": _to_int(parts[5]),
                "our_jobs": [],
                "foreign_procs": 0,
            }
        )
    gpus.sort(key=lambda gpu: gpu["index"])

    apps: list[dict[str, Any]] = []
    for line in sections.get("APPS", []):
        app_part, _, pgid_text = line.partition("\t")
        parts = [part.strip() for part in app_part.split(",")]
        if len(parts) >= 4:
            uuid, pid_text, name, mem_text = parts[0], parts[1], parts[2], parts[3]
        elif len(parts) == 3:  # older nvidia-smi without gpu_uuid
            uuid, (pid_text, name, mem_text) = "", (parts[0], parts[1], parts[2])
        else:
            continue
        apps.append(
            {
                "pid": _to_int(pid_text),
                "name": name,
                "mem_mb": _to_int(mem_text),
                "gpu_index": uuid_to_index.get(uuid),
                "pgid": _to_int(pgid_text) or None,
                "ours": False,
            }
        )

    port_state = {"port": policy_port, "state": "free", "pid": None}
    for entry in sections.get("LISTEN", []):
        address, _, process = entry.partition("\t")
        if not address.strip().endswith(f":{policy_port}"):
            continue
        # `ss -ltnp` only shows process info for our own sockets; a listener we
        # cannot attribute is treated as foreign (never killed).
        pid_match = re.search(r"pid=(\d+)", process)
        port_state = {
            "port": policy_port,
            "state": "occupied",
            "pid": int(pid_match.group(1)) if pid_match else None,
        }
        break

    jobs: list[dict[str, Any]] = []
    for line in sections.get("JOBS", []):
        parts = (line.split("\t") + [""] * 8)[:8]
        job_id, pid_text, exit_text, alive_text, count_text, size_text, steps, marker = parts
        ours = _is_ours(marker, instance_id)
        if alive_text.strip() == "1":
            pid_value = _to_int(pid_text)
            if ours and pid_value:
                our_pids.add(pid_value)
        if alive_text.strip() == "1":
            state = "running"
        elif exit_text.strip() != "":
            state = "exited"
        else:
            state = "lost"
        jobs.append(
            {
                "id": job_id,
                "pid": _to_int(pid_text) or None,
                "exit_code": _to_int(exit_text) if exit_text.strip() != "" else None,
                "state": state,
                "gpu": None,
                "mem_mb": None,
                "checkpoint_count": _to_int(count_text),
                "checkpoint_mb": _to_int(size_text),
                "checkpoints": _parse_checkpoints(steps),
                "ours": ours,
            }
        )
    jobs.sort(key=lambda item: item["id"])

    sessions: list[dict[str, Any]] = []
    for line in sections.get("SESSIONS", []):
        parts = (line.split("\t") + [""] * 5)[:5]
        stamp, pid_text, alive_text, marker, log_tail = parts
        log_tail = log_tail.replace("\x01", "\n")
        ours = _is_ours(marker, instance_id)
        pid_value = _to_int(pid_text) or None
        if ours and alive_text.strip() == "1" and pid_value:
            our_pids.add(pid_value)
        marker_data: dict[str, Any] = {}
        if marker.strip():
            import json

            try:
                marker_data = json.loads(marker)
            except Exception:
                marker_data = {}
        state = "running" if alive_text.strip() == "1" else "stopped"
        sessions.append(
            {
                "stamp": stamp,
                "pid": pid_value,
                "state": state,
                "gpu": marker_data.get("gpu"),
                "port": marker_data.get("port", policy_port),
                "policy": marker_data.get("policy_ref"),
                "ours": ours,
                "client_connected": "connected and ready" in log_tail,
                "last_action": "Action chunk" in log_tail,
            }
        )

    # Attribute the 28081 listener: ours (one of our session pids) or foreign.
    if port_state["state"] == "occupied":
        our_pids_on_port = {s["pid"] for s in sessions if s["ours"] and s["pid"]}
        if port_state["pid"] and port_state["pid"] in our_pids_on_port:
            port_state["state"] = "ours"
        elif port_state["pid"] is None and any(s["ours"] and s["state"] == "running" for s in sessions):
            # Socket owned by another user: no pid visible, but one of our
            # sessions is alive — most likely that one.
            port_state["state"] = "ours"
            port_state["pid"] = next(
                (s["pid"] for s in sessions if s["ours"] and s["state"] == "running"), None
            )
        else:
            port_state["state"] = "foreign"

    # Map measured VRAM back onto our jobs and their GPU.
    for app in apps:
        app["ours"] = app["pid"] in our_pids or (app["pgid"] is not None and app["pgid"] in our_pids)
        for job in jobs:
            if app["ours"] and job["pid"] is not None and job["pid"] in {app["pid"], app["pgid"]}:
                job["mem_mb"] = app["mem_mb"]
                if job["gpu"] is None:
                    job["gpu"] = app["gpu_index"]
        for gpu in gpus:
            if app["gpu_index"] is not None and app["gpu_index"] != gpu["index"]:
                continue
            if app["ours"]:
                gpu["our_jobs"].append({"pid": app["pid"], "mem_mb": app["mem_mb"]})
            elif app["gpu_index"] is not None or len(gpus) <= 1:
                # Without a uuid (old nvidia-smi) a foreign process cannot be
                # attributed; counting it on every GPU would be misleading.
                gpu["foreign_procs"] += 1

    disk: dict[str, Any] = {"total_gb": None, "used_gb": None, "free_gb": None, "free_percent": None}
    for line in sections.get("DISK", []):
        parts = line.split()
        if len(parts) >= 3:
            total_kb, used_kb, free_kb = (_to_int(part) for part in parts[:3])
            total_gb = round(total_kb / 1024 / 1024, 1)
            disk = {
                "total_gb": total_gb,
                "used_gb": round(used_kb / 1024 / 1024, 1),
                "free_gb": round(free_kb / 1024 / 1024, 1),
                "free_percent": round(free_kb / total_kb * 100, 1) if total_kb else None,
            }

    sizes = [_to_int(line) for line in sections.get("SIZES", [])]
    jobs_mb = sizes[0] if len(sizes) > 0 else 0
    policy_mb = sizes[1] if len(sizes) > 1 else 0
    staged_weights = []
    for line in sections.get("WEIGHTS", []):
        parts = (line.split("\t") + [""] * 3)[:3]
        kind, name, size_text = parts
        if not name:
            continue
        staged_weights.append(
            {
                "kind": kind,
                "name": name,
                "size_mb": _to_int(size_text),
                "remote_path": f"{kind}/{name}",
            }
        )
    staged_weights.sort(key=lambda item: (item["kind"], item["name"]))

    return {
        "ssh_ok": True,
        "fetched_at": fetched_at if fetched_at is not None else time.time(),
        "policy_port": port_state,
        "gpus": gpus,
        "compute_apps": apps,
        "jobs": jobs,
        "sessions": sessions,
        "disk": disk,
        "usage": {"jobs_gb": round(jobs_mb / 1024, 2), "policy_server_gb": round(policy_mb / 1024, 2)},
        "staged_weights": staged_weights,
        "error": None,
    }


class TrainServerStatusCache:
    """TTL cache shared by every endpoint that needs the control plane."""

    def __init__(self, ttl_seconds: float = STATUS_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._payload: dict[str, Any] | None = None
        self._expires_at = 0.0

    def get(self, *, force: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if not force and self._payload is not None and now < self._expires_at:
                return self._payload
        payload = self._collect()
        with self._lock:
            self._payload = payload
            self._expires_at = time.time() + self._ttl
        return payload

    def invalidate(self) -> None:
        with self._lock:
            self._expires_at = 0.0

    def _collect(self) -> dict[str, Any]:
        try:
            config = TrainServerConfig.from_env()
        except Exception as exc:
            return _offline_payload(str(exc))
        try:
            instance_id = instance_id_from_env()
            _host, port = _policy_port()
            return_code, output = ssh_ops.run_script(
                config,
                _STATUS_SCRIPT,
                config.root,
                timeout=_QUERY_TIMEOUT,
            )
        except Exception as exc:
            return _offline_payload(f"{type(exc).__name__}: {exc}")
        if return_code != 0:
            detail = " ".join(line.strip() for line in output.splitlines() if line.strip())[-300:]
            return _offline_payload(f"ssh exit {return_code}: {detail or 'no output'}")
        payload = parse_status(output, instance_id=instance_id, policy_port=port)
        payload["endpoint"] = config.endpoint
        return payload


def _offline_payload(message: str) -> dict[str, Any]:
    return {
        "ssh_ok": False,
        "fetched_at": time.time(),
        "policy_port": {"port": None, "state": "unknown", "pid": None},
        "gpus": [],
        "compute_apps": [],
        "jobs": [],
        "sessions": [],
        "disk": {"total_gb": None, "used_gb": None, "free_gb": None, "free_percent": None},
        "usage": {"jobs_gb": None, "policy_server_gb": None},
        "error": message,
    }


def _policy_port() -> tuple[str, int]:
    from .utils.servers import train_policy_endpoint

    return train_policy_endpoint()


def instance_id_from_env(env: Mapping[str, str] | None = None) -> str:
    """Stable identity written into remote markers ("who started this")."""
    import os

    values = os.environ if env is None else env
    configured = (values.get("LELAB_INSTANCE_ID") or "").strip()
    if configured:
        return configured
    return f"{values.get('LELAB_ROLE', 'lelab').strip() or 'lelab'}-local"


def our_sessions(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Sessions in a status payload that were started by this instance."""
    return [session for session in payload.get("sessions", []) if session.get("ours")]


def marker_payload(kind: str, **fields: Any) -> str:
    """Serialize an ownership marker (``.lelab-job.json`` / ``.lelab-session.json``).

    The marker is the authorization primitive for stop/delete/pull: a remote
    directory without a matching ``instance_id`` is never touched.
    """
    import json

    body = {"kind": kind, "instance_id": instance_id_from_env(), "created_at": time.time()}
    body.update({key: value for key, value in fields.items() if value is not None})
    return json.dumps(body, sort_keys=True)


_CLEANUP_SCRIPT = (
    "set -u\n"
    + ssh_ops.EXPAND_PATH_FUNCTION
    + r"""root=$(expand_remote_path "$1")
instance=$2
server_dir="$root/policy_server"
killed=''
[ -d "$server_dir" ] || exit 0
for session in "$server_dir"/*; do
  [ -d "$session" ] || continue
  marker="$session/.lelab-session.json"
  [ -f "$marker" ] || continue
  case "$(cat "$marker")" in
    *"\"instance_id\": \"$instance\""*) ;;
    *) continue ;;
  esac
  pid=$(cat "$session/run.pid" 2>/dev/null || true)
  [ -n "$pid" ] || continue
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    killed="$killed $pid"
  fi
done
printf '%s\n' "$killed"
"""
)


def cleanup_our_policy_servers(config: TrainServerConfig, *, timeout: float = 60.0) -> list[int]:
    """TERM every PolicyServer this instance started, even after a restart.

    Deliberately marker-scoped: other users' processes on the shared host are
    never candidates, no matter what holds port 28081.
    """
    _code, out = ssh_ops.run_script(
        config,
        _CLEANUP_SCRIPT,
        config.root,
        instance_id_from_env(),
        timeout=timeout,
    )
    pids: list[int] = []
    for token in out.replace("\n", " ").split():
        if token.isdigit():
            pids.append(int(token))
    invalidate_train_server_status()
    return pids


# --------------------------------------------------------------------------
# Checkpoint retention (weights management page)
# --------------------------------------------------------------------------

DEFAULT_KEEP_CHECKPOINTS = 3


def prune_plan(
    jobs: list[dict[str, Any]],
    *,
    keep_n: int = DEFAULT_KEEP_CHECKPOINTS,
    job_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Decide which checkpoints are reclaimable (pure function, unit-tested).

    Keeps the newest ``keep_n`` per job regardless of size; a job with fewer
    checkpoints is untouched. Only our jobs are candidates.
    """
    keep_n = max(1, int(keep_n))
    wanted = set(job_ids) if job_ids else None
    plan: list[dict[str, Any]] = []
    for job in jobs:
        if not job.get("ours"):
            continue
        if wanted is not None and job.get("id") not in wanted:
            continue
        checkpoints = sorted(job.get("checkpoints") or [], key=lambda ck: int(ck["step"]))
        reclaimable = checkpoints[:-keep_n] if len(checkpoints) > keep_n else []
        for ckpt in reclaimable:
            plan.append(
                {
                    "job_id": job["id"],
                    "step": int(ckpt["step"]),
                    "size_mb": int(ckpt.get("size_mb") or 0),
                }
            )
    return plan


_CHECKPOINT_SCRIPT = (
    "set -u\n"
    + ssh_ops.EXPAND_PATH_FUNCTION
    + r"""root=$(expand_remote_path "$1")
instance=$2
mode=$3
keep_n=$4
targets=$5
job_filter=$6
jobs_dir="$root/jobs"
[ -d "$jobs_dir" ] || exit 0
for job in "$jobs_dir"/*; do
  [ -d "$job" ] || continue
  job_id=$(basename "$job")
  marker="$job/.lelab-job.json"
  [ -f "$marker" ] || continue
  case "$(cat "$marker")" in
    *"\"instance_id\": \"$instance\""*) ;;
    *) continue ;;
  esac
  if [ -n "$job_filter" ]; then
    case " $job_filter " in
      *" $job_id "*) ;;
      *) continue ;;
    esac
  fi
  ckpt_dir="$job/run/checkpoints"
  [ -d "$ckpt_dir" ] || continue
  steps=$(for entry in "$ckpt_dir"/*; do
    [ -d "$entry/pretrained_model" ] || continue
    step=$(basename "$entry")
    case "$step" in *[!0-9]*) continue ;; esac
    printf '%s\n' "$step"
  done | sort -n)
  [ -n "$steps" ] || continue
  newest=$(printf '%s\n' "$steps" | tail -n 1)
  total=$(printf '%s\n' "$steps" | wc -l)
  keep_from=$((total - keep_n + 1))
  [ "$keep_from" -lt 1 ] && keep_from=1
  index=0
  for step in $steps; do
    index=$((index + 1))
    size=$(du -sm "$ckpt_dir/$step" 2>/dev/null | awk '{print $1}')
    [ -n "$size" ] || size=0
    action=keep
    if [ "$mode" = "prune" ]; then
      [ "$index" -lt "$keep_from" ] && action=delete
    else
      case " $targets " in
        *" $step "*) action=delete ;;
      esac
    fi
    if [ "$action" = "delete" ] && [ "$step" = "$newest" ]; then
      action=keep
    fi
    if [ "$action" = "delete" ]; then
      rm -rf -- "$ckpt_dir/$step"
      printf 'deleted\t%s\t%s\t%s\n' "$job_id" "$step" "$size"
    else
      printf 'kept\t%s\t%s\t%s\n' "$job_id" "$step" "$size"
    fi
  done
done
"""
)


def apply_checkpoint_plan(
    config: TrainServerConfig,
    *,
    mode: str,
    keep_n: int = DEFAULT_KEEP_CHECKPOINTS,
    targets: list[int] | None = None,
    job_ids: list[str] | None = None,
    timeout: float = 300.0,
) -> list[dict[str, Any]]:
    """Hard-delete checkpoints (marker-scoped, newest always kept)."""
    if mode not in {"prune", "delete"}:
        raise ValueError(f"unknown checkpoint mode: {mode}")
    target_text = " ".join(str(step) for step in (targets or []))
    filter_text = " ".join(job_ids or [])
    return_code, output = ssh_ops.run_script(
        config,
        _CHECKPOINT_SCRIPT,
        config.root,
        instance_id_from_env(),
        mode,
        str(max(1, int(keep_n))),
        target_text,
        filter_text,
        timeout=timeout,
    )
    if return_code != 0:
        detail = " ".join(line.strip() for line in output.splitlines() if line.strip())[-300:]
        raise RuntimeError(f"Checkpoint cleanup failed on {config.endpoint}: {detail}")
    results: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        action, job_id, step_text, size_text = parts
        results.append(
            {"action": action, "job_id": job_id, "step": _to_int(step_text), "size_mb": _to_int(size_text)}
        )
    return results


_CACHE = TrainServerStatusCache()


def train_server_status(*, force: bool = False) -> dict[str, Any]:
    """Cached control-plane snapshot for the API layer."""
    return _CACHE.get(force=force)


def invalidate_train_server_status() -> None:
    _CACHE.invalidate()
