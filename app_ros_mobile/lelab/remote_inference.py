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
"""Cloud inference: the policy runs on the GPU training server.

Topology (see ``remote_server.md``)::

    Jetson                                      Train server (RTX 4090 x4)
    ------                                      --------------------------
    robot_client  --[127.0.0.1:28081]--SSH--->  PolicyServer 127.0.0.1:28081
      |                                            ^
      +-- follower bus + cameras                   +-- loads the policy from a
                                                       server-local path

The Jetson keeps the hardware lease and the safety limits; only the action
chunks come from the network. One cloud session owns the same singleton as
local inference, so teleoperation/recording/local inference exclude each
other through the existing mutexes.

Lifecycle of a session:

1. ``preparing_policy``   — resolve the UI policy ref on the train server
   (Hub refs are downloaded *there*, through the mirror/proxy, so the Jetson
   never needs to hold the weights);
2. ``starting_server``    — detached ``lerobot.async_inference.policy_server``
   under ``<train root>/policy_server/<stamp>`` (loopback only);
3. ``opening_tunnel``     — Jetson-local ``ssh -N -L 127.0.0.1:<port>:...``;
4. ``starting_client``    — ``lerobot.async_inference.robot_client`` on the
   Jetson, pointed at the tunnel;
5. ``running``            — until the duration elapses, the client exits, or
   the operator stops it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .remote_sync import TrainServerConfig, ssh_base_command
from .rollout import (
    InferenceRequest,
    _extract_error_from_log,
    _format_cameras_arg,
    _friendly_hint,
)
from .runners import ssh_ops
from .train_server import marker_payload
from .utils.config import setup_follower_calibration_file
from .utils.policy_refs import PolicyRefError, parse_policy_ref
from .utils.servers import train_policy_endpoint

logger = logging.getLogger(__name__)

POLICY_SERVER_MODULE = "lerobot.async_inference.policy_server"
ROBOT_CLIENT_MODULE = "lerobot.async_inference.robot_client"
POLICY_SERVER_DIRNAME = "policy_server"
SESSION_MARKER_NAME = ".lelab-session.json"

# lerobot prints this once the client has pushed policy instructions to the
# server and the control loop takes over; before that the client is still
# connecting the bus/cameras.
_CLIENT_ROLLOUT_MARKER = "Control loop thread starting"
_DEFAULT_SERVER_FPS = 30
_DEFAULT_INFERENCE_LATENCY = 1 / 30
_DEFAULT_OBS_QUEUE_TIMEOUT = 2.0
_TUNNEL_READY_TIMEOUT = 20.0


class RemoteInferenceError(RuntimeError):
    """A human-readable failure while preparing the cloud session."""


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------

_state_lock = threading.Lock()
_session: dict[str, Any] | None = None
_client_proc: subprocess.Popen | None = None
_tunnel_proc: subprocess.Popen | None = None
_duration_timer: threading.Timer | None = None


def remote_inference_active() -> bool:
    with _state_lock:
        return _session is not None and bool(_session.get("active"))


def remote_status_owns_route() -> bool:
    """True when ``/inference-status`` should report the cloud session.

    A finished cloud session keeps reporting its outcome (exit code, error,
    log path) until the next session starts, so the inference page can show
    the result instead of a bare "inactive".
    """
    with _state_lock:
        if _session is None:
            return False
        if _session.get("active"):
            return True
    from . import rollout as _rollout

    return not _rollout.inference_active


def _set_phase(phase: str) -> None:
    with _state_lock:
        if _session is not None:
            _session["phase"] = phase


def _train_config() -> TrainServerConfig:
    return TrainServerConfig.from_env()


def _remote_env(config: TrainServerConfig) -> dict[str, str]:
    """HF/proxy environment for remote lerobot processes."""
    env: dict[str, str] = {}
    endpoint = ssh_ops.remote_hf_endpoint() or os.environ.get("HF_ENDPOINT", "").strip()
    if endpoint:
        env["HF_ENDPOINT"] = endpoint
    proxy = ssh_ops.remote_proxy()
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
    token = ""
    with contextlib.suppress(Exception):
        from huggingface_hub import get_token

        token = get_token() or ""
    if token:
        env["HF_TOKEN"] = token
    env["HF_HUB_DISABLE_XET"] = "1"
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    env["HF_HOME"] = f"{config.root.rstrip('/')}/models/huggingface"
    # Keep a stalled mirror connection from hanging a session forever.
    env["HF_HUB_DOWNLOAD_TIMEOUT"] = "30"
    env["HF_HUB_ETAG_TIMEOUT"] = "30"
    env["LELAB_TRAIN_POLICY_STAGE"] = f"{config.root.rstrip('/')}/models/policies"
    return env


def _policy_server_root(config: TrainServerConfig) -> str:
    return f"{config.root.rstrip('/')}/{POLICY_SERVER_DIRNAME}"


# --------------------------------------------------------------------------
# Remote PolicyServer process
# --------------------------------------------------------------------------

_START_SERVER_SCRIPT = (
    "set -eu\n"
    + ssh_ops.EXPAND_PATH_FUNCTION
    + r"""session_dir=$(expand_remote_path "$1")
python_bin=$(expand_remote_path "$2")
port=$3
fps=$4
latency=$5
obs_timeout=$6
hf_home=$(expand_remote_path "$7")
proxy=$8
hf_endpoint=$9
hf_token=${10}
mkdir -p -- "$session_dir"
cd "$session_dir"
export HF_HOME="$hf_home"
export HF_HUB_DISABLE_XET=1
if [ -n "$hf_endpoint" ]; then export HF_ENDPOINT="$hf_endpoint"; fi
if [ -n "$hf_token" ]; then export HF_TOKEN="$hf_token" HUGGING_FACE_HUB_TOKEN="$hf_token"; fi
if [ -n "$proxy" ]; then
  export HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy" http_proxy="$proxy" https_proxy="$proxy"
  export NO_PROXY="127.0.0.1,localhost"
fi
: > run.log
setsid nohup "$python_bin" -m lerobot.async_inference.policy_server \
  --host=127.0.0.1 --port="$port" --fps="$fps" \
  --inference_latency="$latency" --obs_queue_timeout="$obs_timeout" \
  > run.log 2>&1 &
echo $! > run.pid
cat run.pid
"""
)


def _start_remote_policy_server(
    config: TrainServerConfig,
    *,
    port: int,
    env: dict[str, str],
    stamp: str,
) -> tuple[int, str]:
    session_dir = f"{_policy_server_root(config)}/{stamp}"
    python = ssh_ops.remote_python()
    return_code, output = ssh_ops.run_script(
        config,
        _START_SERVER_SCRIPT,
        session_dir,
        python,
        str(port),
        str(_DEFAULT_SERVER_FPS),
        str(_DEFAULT_INFERENCE_LATENCY),
        str(_DEFAULT_OBS_QUEUE_TIMEOUT),
        env.get("HF_HOME", ""),
        env.get("HTTPS_PROXY", ""),
        env.get("HF_ENDPOINT", ""),
        env.get("HF_TOKEN", ""),
        timeout=30.0,
    )
    if return_code != 0:
        detail = " ".join(line.strip() for line in output.splitlines() if line.strip())[-400:]
        raise RemoteInferenceError(f"Could not start the PolicyServer on {config.endpoint}: {detail}")
    pid_text = output.strip().splitlines()[-1] if output.strip() else ""
    try:
        pid = int(pid_text)
    except ValueError as exc:
        raise RemoteInferenceError(f"PolicyServer start produced no pid: {output.strip()[-200:]}") from exc
    return pid, f"{session_dir}/run.log"


_WRITE_MARKER_SCRIPT = (
    "set -eu\n"
    + ssh_ops.EXPAND_PATH_FUNCTION
    + r"""dir=$(expand_remote_path "$1")
mkdir -p -- "$dir"
printf '%s\n' "$2" > "$dir/"""+ SESSION_MARKER_NAME + '"\n'
)


def _write_session_marker(config: TrainServerConfig, stamp: str, payload: str) -> None:
    """Publish the ownership marker next to the server's run.pid.

    Remote cleanup/authorisation reads this file, so it must exist even if the
    Jetson dies right after the server starts.
    """
    session_dir = f"{_policy_server_root(config)}/{stamp}"
    return_code, output = ssh_ops.run_script(
        config, _WRITE_MARKER_SCRIPT, session_dir, payload, timeout=30.0
    )
    if return_code != 0:
        detail = " ".join(line.strip() for line in output.splitlines() if line.strip())[-300:]
        raise RemoteInferenceError(f"Could not write the session marker on {config.endpoint}: {detail}")


_SERVER_STATE_SCRIPT = (
    "set -eu\n"
    + ssh_ops.EXPAND_PATH_FUNCTION
    + r"""session_dir=$(expand_remote_path "$1")
pid_file="$session_dir/run.pid"
if [ ! -f "$pid_file" ]; then exit 0; fi
pid=$(cat "$pid_file" 2>/dev/null || true)
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  printf 'running\t%s\n' "$pid"
else
  printf 'stopped\t%s\n' "$pid"
fi
"""
)


def handle_remote_policy_server_status() -> dict[str, Any]:
    """Whether a PolicyServer started by lelab is alive on the train server."""
    payload: dict[str, Any] = {
        "configured": False,
        "running": False,
        "pid": None,
        "log_path": None,
        "session_dir": None,
        "error": None,
    }
    try:
        config = _train_config()
    except Exception as exc:
        payload["error"] = str(exc)
        return payload
    payload["configured"] = True
    payload["endpoint"] = config.endpoint
    with _state_lock:
        stamp = _session.get("stamp") if _session is not None else None
    if not stamp:
        return payload
    session_dir = f"{_policy_server_root(config)}/{stamp}"
    payload["session_dir"] = session_dir
    payload["log_path"] = f"{session_dir}/run.log"
    return_code, output = ssh_ops.run_script(config, _SERVER_STATE_SCRIPT, session_dir, timeout=30.0)
    if return_code != 0:
        payload["error"] = output.strip()[-300:]
        return payload
    for line in output.splitlines():
        state, _, pid_text = line.partition("\t")
        if state == "running":
            payload["running"] = True
            with contextlib.suppress(ValueError):
                payload["pid"] = int(pid_text)
    return payload


def handle_stop_remote_policy_server() -> dict[str, Any]:
    """Stop our train-side PolicyServer(s), even after a Jetson restart.

    Falls back to a marker-scoped remote scan when the in-memory session is
    gone (API restarted), so leftover sessions can always be released from the
    UI. Foreign listeners are never candidates.
    """
    try:
        config = _train_config()
    except Exception as exc:
        return {"success": False, "status_code": 400, "message": str(exc)}
    with _state_lock:
        pid = _session.get("server_pid") if _session is not None else None
    if pid:
        ssh_ops.stop_remote_pid(config, int(pid))
        with _state_lock:
            if _session is not None:
                _session["server_pid"] = None
        return {"success": True, "message": f"PolicyServer {pid} stopped"}

    from .train_server import cleanup_our_policy_servers

    pids = cleanup_our_policy_servers(config)
    if not pids:
        return {"success": False, "status_code": 409, "message": "No cloud PolicyServer of ours is running"}
    return {"success": True, "message": f"Stopped leftover PolicyServer(s): {pids}"}


# --------------------------------------------------------------------------
# SSH tunnel
# --------------------------------------------------------------------------


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _wait_for_local_port(port: int, *, timeout: float, proc: subprocess.Popen | None = None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        if _port_open("127.0.0.1", port, 0.4):
            return True
        time.sleep(0.3)
    return False


def _open_tunnel(config: TrainServerConfig, *, local_port: int, remote_port: int) -> subprocess.Popen:
    argv = [
        *ssh_base_command(config),
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-N",
        "-T",
        "-L",
        f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
        config.endpoint,
    ]
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


# --------------------------------------------------------------------------
# Robot client (runs on the Jetson)
# --------------------------------------------------------------------------


def _build_client_command(
    request: InferenceRequest,
    *,
    follower_id: str,
    local_port: int,
    remote_policy_path: str,
) -> list[str]:
    from .safety import follower_safety_cli_args

    policy_type = (request.policy_type or "act").strip() or "act"
    cmd = [
        sys.executable,
        "-m",
        ROBOT_CLIENT_MODULE,
        "--robot.type=so101_follower",
        f"--robot.port={request.follower_port}",
        f"--robot.id={follower_id}",
        *follower_safety_cli_args(),
        f"--server_address=127.0.0.1:{local_port}",
        f"--policy_type={policy_type}",
        f"--pretrained_name_or_path={remote_policy_path}",
        f"--policy_device={request.remote_policy_device or 'cuda'}",
        "--client_device=cpu",
        f"--actions_per_chunk={request.actions_per_chunk}",
        f"--fps={request.fps}",
        f"--chunk_size_threshold={request.chunk_size_threshold}",
    ]
    if request.task:
        cmd.append(f"--task={request.task}")
    if request.cameras:
        cmd.append(f"--robot.cameras={_format_cameras_arg(request.cameras)}")
    return cmd


def _pump_client_stdout(proc: subprocess.Popen, log_handle) -> None:
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace")
            with contextlib.suppress(Exception):
                log_handle.write(line)
                log_handle.flush()
            if _CLIENT_ROLLOUT_MARKER in line:
                with _state_lock:
                    if _session is not None and _session.get("rollout_started_at") is None:
                        _session["rollout_started_at"] = time.time()
                logger.info("Cloud inference control loop started")
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Robot client log pump failed: %s", exc)
    finally:
        with contextlib.suppress(Exception):
            log_handle.close()


# --------------------------------------------------------------------------
# Public handlers
# --------------------------------------------------------------------------


def handle_start_remote_inference(request: InferenceRequest) -> dict[str, Any]:
    """Start a cloud inference session; returns a route-friendly result dict."""
    global _session, _client_proc, _tunnel_proc, _duration_timer

    from . import record as _record, rollout as _rollout, teleoperate as _teleoperate

    with _state_lock:
        if _teleoperate.teleoperation_active:
            return {
                "success": False,
                "status_code": 409,
                "message": "Teleoperation is currently active. Stop it first.",
            }
        if _record.recording_active:
            return {
                "success": False,
                "status_code": 409,
                "message": "Recording is currently active. Stop it first.",
            }
        if _session is not None and _session.get("active"):
            return {
                "success": False,
                "status_code": 409,
                "message": "A cloud inference session is already active. Stop it first.",
            }
        if _rollout.inference_active:
            return {
                "success": False,
                "status_code": 409,
                "message": "Local inference is currently active. Stop it first.",
            }
        # Claim the shared inference slot so a local start cannot race us.
        _rollout.inference_active = True
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())
        _session = {
            "active": True,
            "phase": "preparing_policy",
            "stamp": stamp,
            "policy_ref": request.policy_ref,
            "resolved_policy_path": None,
            "server_pid": None,
            "server_log_path": None,
            "client_log_path": None,
            "tunnel_ready": False,
            "started_at": time.time(),
            "rollout_started_at": None,
            "duration_s": request.duration_s,
            "ended_at": None,
            "exit_code": None,
            "outcome": None,
            "error": None,
        }

    try:
        config = _train_config()
    except Exception as exc:
        _fail_session(str(exc))
        return {"success": False, "status_code": 400, "message": str(exc)}

    try:
        parsed = parse_policy_ref(request.policy_ref)
    except PolicyRefError as exc:
        _fail_session(str(exc))
        return {"success": False, "status_code": 400, "message": str(exc)}

    try:
        follower_id = setup_follower_calibration_file(request.follower_config)

        env = _remote_env(config)
        _set_phase("preparing_policy")
        remote_policy_path = ssh_ops.stage_policy_on_server(
            config,
            parsed,
            python=ssh_ops.remote_python(),
            env=env,
            timeout=1800.0,
        )
        with _state_lock:
            if _session is not None:
                _session["resolved_policy_path"] = remote_policy_path

        _host, remote_port = train_policy_endpoint()
        _set_phase("starting_server")
        # Port 28081 is single-session by design: refuse to touch a foreign
        # listener, and hand our own leftovers to the operator's cleanup button.
        from .train_server import our_sessions, train_server_status

        live = train_server_status(force=True)
        port_state = (live.get("policy_port") or {}).get("state")
        if port_state == "foreign":
            raise RemoteInferenceError(
                f"Port {remote_port} on {config.endpoint} is held by a process this "
                "system does not own; close it or choose another host — LeLab never "
                "kills foreign processes."
            )
        if port_state == "ours":
            leftovers = our_sessions(live)
            detail = ", ".join(
                f"{s['stamp']} (pid {s['pid']}, GPU {s.get('gpu')}, {s.get('policy')})"
                for s in leftovers
            )
            raise RemoteInferenceError(
                f"A previous cloud session still holds port {remote_port}: {detail or 'unknown'}. "
                "Use '清理遗留会话' (System page) or the cleanup action to release it first."
            )
        server_pid, server_log = _start_remote_policy_server(
            config,
            port=remote_port,
            env=env,
            stamp=stamp,
        )
        _write_session_marker(
            config,
            stamp,
            marker_payload(
                "policy_server",
                stamp=stamp,
                pid=server_pid,
                port=remote_port,
                gpu=request.remote_gpu if isinstance(request.remote_gpu, int) else None,
                policy_ref=request.policy_ref,
                policy_path=remote_policy_path,
                log_path=server_log,
            ),
        )
        with _state_lock:
            if _session is not None:
                _session["server_pid"] = server_pid
                _session["server_log_path"] = server_log

        _set_phase("opening_tunnel")
        if _port_open("127.0.0.1", remote_port, 0.2):
            raise RemoteInferenceError(
                f"Local port {remote_port} is already in use on the Jetson; "
                "close the other tunnel/policy client first."
            )
        tunnel = _open_tunnel(config, local_port=remote_port, remote_port=remote_port)
        _tunnel_proc = tunnel
        if not _wait_for_local_port(remote_port, timeout=_TUNNEL_READY_TIMEOUT, proc=tunnel):
            raise RemoteInferenceError(
                f"SSH tunnel to {config.endpoint} did not come up on 127.0.0.1:{remote_port}"
            )
        with _state_lock:
            if _session is not None:
                _session["tunnel_ready"] = True

        _set_phase("starting_client")
        cmd = _build_client_command(
            request,
            follower_id=follower_id,
            local_port=remote_port,
            remote_policy_path=remote_policy_path,
        )
        log_dir = Path.home() / ".cache" / "huggingface" / "lerobot" / "inference_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"remote-{int(time.time())}.log"
        log_handle = log_path.open("w", buffering=1)
        client_env = os.environ.copy()
        client_env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=client_env,
        )
        _client_proc = proc
        threading.Thread(
            target=_pump_client_stdout,
            args=(proc, log_handle),
            name="remote-inference-log-pump",
            daemon=True,
        ).start()
        with _state_lock:
            if _session is not None:
                _session["client_log_path"] = str(log_path)
                _session["phase"] = "running"

        if request.duration_s and request.duration_s > 0:
            timer = threading.Timer(float(request.duration_s), _auto_stop, args=(proc.pid,))
            timer.daemon = True
            timer.start()
            _duration_timer = timer

        logger.info(
            "Cloud inference started: policy=%s server_pid=%s on %s",
            remote_policy_path,
            server_pid,
            config.endpoint,
        )
        return {
            "success": True,
            "message": "Cloud inference started",
            "log_path": str(log_path),
            "server_log_path": server_log,
            "remote_policy_path": remote_policy_path,
        }
    except Exception as exc:
        logger.exception("Failed to start cloud inference")
        _finish_session(exit_code=None, error=str(exc))
        _cleanup_processes(keep_server=False)
        with _state_lock:
            _rollout.inference_active = False
        return {"success": False, "status_code": 500, "message": f"Failed to start cloud inference: {exc}"}


def _auto_stop(expected_pid: int) -> None:
    """Timer callback: stop the client once its duration elapsed."""
    with _state_lock:
        active_pid = _client_proc.pid if _client_proc is not None else None
    if active_pid == expected_pid:
        logger.info("Cloud inference duration elapsed; stopping session")
        handle_stop_remote_inference()


def _fail_session(message: str) -> None:
    from . import rollout as _rollout

    _finish_session(exit_code=None, error=message)
    with _state_lock:
        _rollout.inference_active = False


def _finish_session(*, exit_code: int | None, error: str | None) -> None:
    with _state_lock:
        if _session is None:
            return
        _session["active"] = False
        _session["ended_at"] = time.time()
        if exit_code is not None:
            _session["exit_code"] = exit_code
        if error:
            _session["error"] = error
            _session["outcome"] = "failed"
        elif _session.get("outcome") is None:
            _session["outcome"] = "ok"


def _cleanup_processes(*, keep_server: bool) -> None:
    """Stop the client/tunnel (and optionally the server) of the last session."""
    global _client_proc, _tunnel_proc, _duration_timer
    timer = _duration_timer
    _duration_timer = None
    if timer is not None:
        timer.cancel()

    proc = _client_proc
    _client_proc = None
    if proc is not None:
        with contextlib.suppress(Exception):
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    tunnel = _tunnel_proc
    _tunnel_proc = None
    if tunnel is not None:
        with contextlib.suppress(Exception):
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel.kill()
                tunnel.wait(timeout=5)

    if keep_server:
        return
    with _state_lock:
        pid = _session.get("server_pid") if _session else None
    if not pid:
        return
    try:
        config = _train_config()
        ssh_ops.stop_remote_pid(config, int(pid), timeout=40.0)
    except Exception as exc:  # pragma: no cover - network failure path
        logger.warning("Could not stop remote PolicyServer %s: %s", pid, exc)
    with _state_lock:
        if _session is not None:
            _session["server_pid"] = None


def handle_stop_remote_inference(*, stop_server: bool = True) -> dict[str, Any]:
    """Stop the client + tunnel, and the train-side server unless disabled."""
    from . import rollout as _rollout

    with _state_lock:
        if _session is None or not _session.get("active"):
            return {"success": False, "status_code": 409, "message": "No cloud inference is active"}
        _session["phase"] = "stopping"

    _cleanup_processes(keep_server=not stop_server)
    _finish_session(exit_code=0, error=None)
    with _state_lock:
        _rollout.inference_active = False
    return {"success": True, "message": "Cloud inference stopped"}


def handle_remote_inference_status() -> dict[str, Any]:
    """Status payload for the shared ``/inference-status`` route."""
    with _state_lock:
        session = dict(_session) if _session is not None else None
        if session is not None and session.get("active") and _client_proc is not None:
            return_code = _client_proc.poll()
            if return_code is not None:
                session["active"] = False
                session["ended_at"] = time.time()
                session["exit_code"] = return_code
                if _session is not None:
                    _session["active"] = False
                    _session["ended_at"] = session["ended_at"]
                    _session["exit_code"] = return_code
                    _session["outcome"] = "failed" if return_code else "ok"

    if session is None:
        return {
            "inference_active": False,
            "target": None,
            "started_at": None,
            "exited": False,
            "exit_code": None,
        }

    if not session.get("active"):
        from . import rollout as _rollout

        # A finished client is useless without its server: release the GPU
        # memory immediately instead of leaving an orphan PolicyServer behind.
        _cleanup_processes(keep_server=False)
        with _state_lock:
            _rollout.inference_active = False

    error = session.get("error")
    if not session.get("active") and error is None and session.get("exit_code"):
        error = _extract_error_from_log(session.get("client_log_path"))
    started_at = session.get("started_at")
    rollout_started_at = session.get("rollout_started_at")
    return {
        "inference_active": bool(session.get("active")),
        "target": "remote",
        "phase": session.get("phase"),
        "started_at": started_at,
        "rollout_started_at": rollout_started_at,
        "elapsed_s": (time.time() - started_at) if started_at else 0.0,
        "rollout_elapsed_s": (time.time() - rollout_started_at) if rollout_started_at else 0.0,
        "duration_s": session.get("duration_s"),
        "policy_ref": session.get("policy_ref"),
        "resolved_policy_path": session.get("resolved_policy_path"),
        "log_path": session.get("client_log_path"),
        "server_log_path": session.get("server_log_path"),
        "server_pid": session.get("server_pid"),
        "tunnel_ready": session.get("tunnel_ready"),
        "exited": not session.get("active"),
        "exit_code": session.get("exit_code"),
        "outcome": session.get("outcome"),
        "error": error,
        "hint": _friendly_hint(error),
    }


def reset_session_for_tests() -> None:
    """Drop all session state (unit tests only)."""
    global _session, _client_proc, _tunnel_proc, _duration_timer
    with _state_lock:
        _session = None
        _client_proc = None
        _tunnel_proc = None
        _duration_timer = None
