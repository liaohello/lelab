"""Jetson-hosted LeRobot PolicyServer with local observation and safety ownership.

The local host owns the serial bus and cameras.  The Jetson only receives
observations and returns action chunks.  Three modes are deliberately separate:

* ``observe``  -- read joints/cameras only; no gRPC channel and no action sink.
* ``shadow``   -- upload observations, receive and validate actions, never send.
* ``execute``  -- only after an explicit safety gate; actions are dispatched to
  a local sink that re-checks expiry and limits.

Action expiry uses the local monotonic clock and the observation association,
never the two machines' wall clocks.  A dropped session clears the queue and
requires a new handshake before any action can be accepted again.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import pickle  # nosec - LeRobot's own gRPC protocol uses pickle
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

logger = logging.getLogger(__name__)

MODE_OBSERVE = "observe"
MODE_SHADOW = "shadow"
MODE_EXECUTE = "execute"
_MODES = (MODE_OBSERVE, MODE_SHADOW, MODE_EXECUTE)

DEFAULT_STATE_NAMES = (
    "shoulder_pan.pos",
    "shoulder_lift.pos",
    "elbow_flex.pos",
    "wrist_flex.pos",
    "wrist_roll.pos",
    "gripper.pos",
)

DEFAULT_JETSON_HOST = "192.168.31.204"
DEFAULT_JETSON_PORT = 22
DEFAULT_JETSON_USER = "nvidia"
DEFAULT_POLICY_PORT = 28081
DEFAULT_JETSON_ROOT = "~/jetson-policy-server-20261031"
DEFAULT_JETSON_PYTHON = "~/Desktop/lelab/app/.venv/bin/python"
DEFAULT_CHECKPOINT = (
    "~/Desktop/lelab/models/"
    "act_local_red_bowl_20260919_152702_2026-09-19_19-55-49_6cf4e08d/"
    "checkpoints/010000/pretrained_model"
)
DEFAULT_POLICY_TYPE = "act"


class JetsonPolicyError(RuntimeError):
    """A configuration, transport, contract, or safety failure."""


# ---------------------------------------------------------------------------
# Configuration and checkpoint contract
# ---------------------------------------------------------------------------


def _expand_remote_path(value: str, user: str) -> str:
    """Expand a leading ``~`` for the remote account before shell quoting."""
    if value == "~":
        return f"/home/{user}"
    if value.startswith("~/"):
        return f"/home/{user}/{value[2:]}"
    return value


@dataclass(frozen=True, slots=True)
class JetsonConfig:
    host: str = DEFAULT_JETSON_HOST
    port: int = DEFAULT_JETSON_PORT
    user: str = DEFAULT_JETSON_USER
    root: str = DEFAULT_JETSON_ROOT
    ssh_key: str = ""
    policy_port: int = DEFAULT_POLICY_PORT
    python: str = DEFAULT_JETSON_PYTHON
    checkpoint: str = DEFAULT_CHECKPOINT
    policy_type: str = DEFAULT_POLICY_TYPE
    bind_host: str = "0.0.0.0"

    def __post_init__(self) -> None:
        for name in ("root", "python", "checkpoint"):
            object.__setattr__(
                self, name, _expand_remote_path(str(getattr(self, name)), self.user)
            )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> JetsonConfig:
        values = os.environ if env is None else env
        return cls(
            host=values.get("LELAB_JETSON_HOST", DEFAULT_JETSON_HOST),
            port=int(values.get("LELAB_JETSON_PORT", DEFAULT_JETSON_PORT)),
            user=values.get("LELAB_JETSON_USER", DEFAULT_JETSON_USER),
            root=values.get("LELAB_JETSON_ROOT", DEFAULT_JETSON_ROOT),
            ssh_key=values.get("LELAB_JETSON_SSH_KEY", ""),
            policy_port=int(values.get("LELAB_JETSON_POLICY_PORT", DEFAULT_POLICY_PORT)),
            python=values.get("LELAB_JETSON_PYTHON", DEFAULT_JETSON_PYTHON),
            checkpoint=values.get("LELAB_JETSON_CHECKPOINT", DEFAULT_CHECKPOINT),
            policy_type=values.get("LELAB_JETSON_POLICY_TYPE", DEFAULT_POLICY_TYPE),
            bind_host=values.get("LELAB_JETSON_BIND_HOST", "0.0.0.0"),
        )

    @property
    def endpoint(self) -> str:
        return f"{self.user}@{self.host}"

    @property
    def policy_endpoint(self) -> str:
        return f"{self.host}:{self.policy_port}"

    @property
    def source_dir(self) -> str:
        return f"{self.root.rstrip('/')}/lerobot/src"


def _sha256_files(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class PolicyContract:
    """The parts of a checkpoint that a runtime observation/action must match."""

    policy_type: str
    state_key: str
    state_dim: int
    state_names: tuple[str, ...]
    image_keys: tuple[str, ...]
    image_shapes_chw: dict[str, tuple[int, int, int]]
    action_key: str
    action_dim: int
    chunk_size: int
    n_action_steps: int
    normalization: dict[str, str]
    preprocessor_steps: tuple[str, ...]
    postprocessor_steps: tuple[str, ...]
    checkpoint_dir: Path
    checkpoint_sha256: str = ""

    @classmethod
    def from_checkpoint(
        cls,
        checkpoint_dir: str | Path,
        *,
        policy_type: str | None = None,
        state_names: tuple[str, ...] | None = None,
    ) -> PolicyContract:
        root = Path(checkpoint_dir).expanduser()
        config_path = root / "config.json"
        pre_path = root / "policy_preprocessor.json"
        post_path = root / "policy_postprocessor.json"
        for path in (config_path, pre_path, post_path):
            if not path.is_file():
                raise JetsonPolicyError(f"checkpoint metadata missing: {path}")

        config = json.loads(config_path.read_text(encoding="utf-8"))
        pre = json.loads(pre_path.read_text(encoding="utf-8"))
        post = json.loads(post_path.read_text(encoding="utf-8"))

        actual_type = str(config.get("type", ""))
        if policy_type and actual_type != policy_type:
            raise JetsonPolicyError(
                f"checkpoint type is {actual_type!r}, expected {policy_type!r}: {root}"
            )

        input_features = config.get("input_features") or {}
        output_features = config.get("output_features") or {}
        state_features = [
            (key, feature)
            for key, feature in input_features.items()
            if str(feature.get("type", "")).upper() in {"STATE", "ENV"}
        ]
        if len(state_features) != 1:
            raise JetsonPolicyError(
                f"expected exactly one state feature, found {[key for key, _ in state_features]}"
            )
        state_key, state_feature = state_features[0]
        state_shape = tuple(int(v) for v in state_feature.get("shape", ()))
        if len(state_shape) != 1:
            raise JetsonPolicyError(f"state shape must be 1-D, got {state_shape}")
        state_dim = state_shape[0]

        image_features = {
            key: tuple(int(v) for v in feature.get("shape", ()))
            for key, feature in input_features.items()
            if str(feature.get("type", "")).upper() == "VISUAL"
        }
        if not image_features:
            raise JetsonPolicyError("checkpoint has no visual observation")
        for key, shape in image_features.items():
            if len(shape) != 3 or shape[0] != 3:
                raise JetsonPolicyError(f"image feature {key} must be (C,H,W), got {shape}")

        action_features = [
            (key, feature)
            for key, feature in output_features.items()
            if str(feature.get("type", "")).upper() == "ACTION"
        ]
        if len(action_features) != 1:
            raise JetsonPolicyError("expected exactly one action feature")
        action_key, action_feature = action_features[0]
        action_shape = tuple(int(v) for v in action_feature.get("shape", ()))
        if len(action_shape) != 1:
            raise JetsonPolicyError(f"action shape must be 1-D, got {action_shape}")

        norm_map = dict(config.get("normalization_mapping") or {})
        required_norm = {"VISUAL", "STATE", "ACTION"}
        missing_norm = required_norm - set(norm_map)
        if missing_norm:
            raise JetsonPolicyError(f"normalization mapping missing {sorted(missing_norm)}")

        pre_steps = tuple(
            str(step.get("registry_name", "")) for step in (pre.get("steps") or [])
        )
        post_steps = tuple(
            str(step.get("registry_name", "")) for step in (post.get("steps") or [])
        )
        if "normalizer_processor" not in pre_steps:
            raise JetsonPolicyError("checkpoint preprocessor has no normalizer_processor")
        if "unnormalizer_processor" not in post_steps:
            raise JetsonPolicyError("checkpoint postprocessor has no unnormalizer_processor")

        names = tuple(state_names or DEFAULT_STATE_NAMES)
        if len(names) != state_dim:
            raise JetsonPolicyError(
                f"state names count {len(names)} != checkpoint state dim {state_dim}"
            )

        return cls(
            policy_type=actual_type,
            state_key=state_key,
            state_dim=state_dim,
            state_names=names,
            image_keys=tuple(image_features),
            image_shapes_chw=dict(image_features),
            action_key=action_key,
            action_dim=action_shape[0],
            chunk_size=int(config.get("chunk_size", 0)),
            n_action_steps=int(config.get("n_action_steps", 0)),
            normalization=norm_map,
            preprocessor_steps=pre_steps,
            postprocessor_steps=post_steps,
            checkpoint_dir=root,
            checkpoint_sha256=_sha256_files([config_path, pre_path, post_path]),
        )

    @property
    def camera_keys(self) -> tuple[str, ...]:
        """Raw camera names without the ``observation.images.`` prefix."""
        return tuple(key.removeprefix("observation.images.") for key in self.image_keys)

    def dataset_features(self) -> dict[str, dict[str, Any]]:
        """Features used by LeRobot's server-side ``build_dataset_frame``."""
        features: dict[str, dict[str, Any]] = {
            self.state_key: {
                "dtype": "float32",
                "shape": [self.state_dim],
                "names": list(self.state_names),
            }
        }
        for key, (channels, height, width) in self.image_shapes_chw.items():
            features[key] = {
                "dtype": "image",
                "shape": [height, width, channels],
                "names": ["height", "width", "channels"],
            }
        return features

    def validate_observation(self, observation: dict[str, Any]) -> None:
        missing = [name for name in self.state_names if name not in observation]
        if missing:
            raise JetsonPolicyError(f"observation missing state keys: {missing}")
        state = np.asarray(
            [observation[name] for name in self.state_names], dtype=np.float64
        )
        if state.shape != (self.state_dim,):
            raise JetsonPolicyError(
                f"state shape {state.shape} != checkpoint state dim {self.state_dim}"
            )
        if not np.all(np.isfinite(state)):
            raise JetsonPolicyError("observation state contains NaN/inf")
        for key, (channels, _height, _width) in self.image_shapes_chw.items():
            camera_key = key.removeprefix("observation.images.")
            if camera_key not in observation:
                raise JetsonPolicyError(f"observation missing camera {camera_key!r}")
            image = np.asarray(observation[camera_key])
            if image.ndim != 3 or image.shape[2] != channels:
                raise JetsonPolicyError(
                    f"camera {camera_key!r} shape {image.shape} is not (H,W,{channels})"
                )

    def validate_action(self, action: Any) -> np.ndarray:
        if hasattr(action, "detach"):
            action = action.detach().cpu().numpy()
        array = np.asarray(action, dtype=np.float64)
        if array.shape != (self.action_dim,):
            raise JetsonPolicyError(
                f"action shape {array.shape} != checkpoint action dim {self.action_dim}"
            )
        if not np.all(np.isfinite(array)):
            raise JetsonPolicyError("action contains NaN/inf")
        return array


# ---------------------------------------------------------------------------
# SSH lifecycle for the Jetson PolicyServer
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServerStatus:
    pid: int | None
    alive: bool
    listening: bool
    log_path: str
    marker: dict[str, Any] = field(default_factory=dict)
    log_tail: str = ""


class JetsonPolicyServer:
    """Start/stop exactly one PolicyServer in the isolated Jetson directory."""

    def __init__(self, config: JetsonConfig, *, ssh_runner: Callable[..., Any] | None = None):
        self.config = config
        self._ssh_runner = ssh_runner or self._ssh
        self._started_pid: int | None = None

    def _ssh_argv(self) -> list[str]:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
        if self.config.ssh_key:
            argv += ["-i", str(Path(self.config.ssh_key).expanduser())]
        argv += ["-p", str(self.config.port), self.config.endpoint]
        return argv

    def _ssh(self, script: str, *args: str, timeout: float = 30.0) -> tuple[int, str]:
        command = "sh -s -- " + " ".join(_shell_quote(a) for a in args)
        completed = subprocess.run(
            [*self._ssh_argv(), command],
            input=script.encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout.decode("utf-8", errors="replace")

    def _run(self, script: str, *args: str, timeout: float = 30.0) -> str:
        code, output = self._ssh_runner(script, *args, timeout=timeout)
        if code != 0:
            raise JetsonPolicyError(
                f"remote command failed on {self.config.endpoint} (exit {code}): "
                f"{' '.join(output.splitlines())[-500:]}"
            )
        return output

    def sync_source(self, local_source: str | Path, *, delete: bool = True) -> dict[str, Any]:
        """Mirror the local LeRobot ``src`` tree and return a checksum manifest."""
        local = Path(local_source).expanduser().resolve()
        if not (local / "lerobot").is_dir():
            raise JetsonPolicyError(f"local LeRobot source not found: {local}")
        remote = self.config.source_dir
        self._run(
            'set -eu\nroot=$1\nmkdir -p "$root/lerobot"\n',
            self.config.root,
        )
        rsync = [
            "rsync",
            "-az",
            "--checksum",
            *(["--delete"] if delete else []),
            "--exclude",
            "__pycache__",
            "--exclude",
            "*.pyc",
            "-e",
            " ".join(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    *(
                        ["-i", str(Path(self.config.ssh_key).expanduser())]
                        if self.config.ssh_key
                        else []
                    ),
                    "-p",
                    str(self.config.port),
                ]
            ),
            f"{local}/",
            f"{self.config.endpoint}:{remote}/",
        ]
        completed = subprocess.run(
            rsync, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=300
        )
        if completed.returncode != 0:
            raise JetsonPolicyError(
                "rsync failed: "
                + completed.stdout.decode("utf-8", errors="replace")[-800:]
            )
        manifest = self.source_manifest(remote=True)
        return {"remote": remote, "files": len(manifest), "sha256": _manifest_digest(manifest)}

    def source_manifest(self, *, remote: bool) -> dict[str, str]:
        if remote:
            script = (
                "set -eu\n"
                "dir=$1\n"
                'cd "$dir"\n'
                "find lerobot -type f -name '*.py' -print0 | sort -z | "
                "xargs -0 sha256sum\n"
            )
            output = self._run(script, self.config.source_dir)
        else:
            root = Path(__file__).resolve().parents[2] / "lerobot" / "src"
            entries = []
            for path in sorted((root / "lerobot").rglob("*.py")):
                entries.append(
                    f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
                    f"{path.relative_to(root).as_posix()}"
                )
            output = "\n".join(entries)
        manifest: dict[str, str] = {}
        for line in output.splitlines():
            parts = line.split()
            if len(parts) == 2 and len(parts[0]) == 64:
                manifest[parts[1]] = parts[0]
        if not manifest:
            raise JetsonPolicyError("empty source manifest")
        return manifest

    def runtime_probe(self) -> dict[str, Any]:
        script = (
            "set -eu\n"
            "src=$1\n"
            "python=$2\n"
            'export PYTHONPATH="$src"\n'
            '"$python" - <<\'PY\'\n'
            "import json, lerobot, torch, grpc\n"
            "print(json.dumps({\n"
            "  'lerobot_file': lerobot.__file__,\n"
            "  'lerobot_version': getattr(lerobot, '__version__', None),\n"
            "  'torch': torch.__version__,\n"
            "  'cuda_available': torch.cuda.is_available(),\n"
            "  'cuda': torch.version.cuda,\n"
            "  'grpc': grpc.__version__,\n"
            "}))\n"
            "PY\n"
        )
        output = self._run(script, self.config.source_dir, self.config.python)
        payload = json.loads(output.strip().splitlines()[-1])
        if not str(payload["lerobot_file"]).startswith(self.config.source_dir):
            raise JetsonPolicyError(
                "Jetson imported LeRobot from outside the isolated source: "
                f"{payload['lerobot_file']}"
            )
        return payload

    def start(self, *, timeout_s: float = 30.0) -> dict[str, Any]:
        script = r"""set -eu
root=$1
python=$2
bind_host=$3
port=$4
src=$5
checkpoint=$6
policy_type=$7
mkdir -p "$root/run" "$root/logs"
pidfile="$root/run/policy_server.pid"
marker="$root/run/session.json"
if [ -f "$pidfile" ]; then
  old=$(cat "$pidfile" 2>/dev/null || true)
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
    echo "ALREADY_RUNNING $old"
    exit 0
  fi
fi
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$port$"; then
  echo "PORT_IN_USE $port"
  exit 3
fi
cd "$root"
export PYTHONPATH="$src"
export HF_HOME="$root/hf"
export HF_HUB_DISABLE_XET=1
: > "$root/logs/policy_server.log"
setsid nohup "$python" -m lerobot.async_inference.policy_server \
  --host="$bind_host" --port="$port" --fps=30 \
  --inference_latency=0.0333333333 --obs_queue_timeout=2 \
  > "$root/logs/policy_server.log" 2>&1 &
pid=$!
echo "$pid" > "$pidfile"
printf '{"pid":%s,"port":%s,"policy_type":"%s","checkpoint":"%s","source":"%s","bind_host":"%s","started_at":%s}\n' \
  "$pid" "$port" "$policy_type" "$checkpoint" "$src" "$bind_host" "$(date +%s)" > "$marker"
echo "STARTED $pid"
"""
        code, output = self._ssh_runner(
            script,
            self.config.root,
            self.config.python,
            self.config.bind_host,
            str(self.config.policy_port),
            self.config.source_dir,
            self.config.checkpoint,
            self.config.policy_type,
            timeout=60.0,
        )
        text = output.strip()
        if "ALREADY_RUNNING" in text or "STARTED" in text:
            self._started_pid = int(text.split()[-1])
        else:
            raise JetsonPolicyError(
                f"PolicyServer did not start (exit {code}): {' '.join(text.splitlines())[-600:]}"
            )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._tcp_open():
                status = self.status()
                if status.alive:
                    return {
                        "pid": status.pid,
                        "endpoint": self.config.policy_endpoint,
                        "log": status.log_path,
                        "already_running": "ALREADY_RUNNING" in text,
                    }
            time.sleep(0.25)
        log = self.log_tail(80)
        raise JetsonPolicyError(
            f"PolicyServer port did not become reachable at {self.config.policy_endpoint}; "
            f"log tail: {log[-600:]}"
        )

    def _tcp_open(self, timeout: float = 0.5) -> bool:
        try:
            with socket.create_connection(
                (self.config.host, self.config.policy_port), timeout=timeout
            ):
                return True
        except OSError:
            return False

    def status(self) -> ServerStatus:
        script = r"""set -eu
root=$1
pidfile="$root/run/policy_server.pid"
marker="$root/run/session.json"
pid=""
alive=false
if [ -f "$pidfile" ]; then
  pid=$(cat "$pidfile" 2>/dev/null || true)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then alive=true; fi
fi
printf 'PID=%s\nALIVE=%s\n' "$pid" "$alive"
if [ -f "$marker" ]; then cat "$marker"; fi
"""
        output = self._run(script, self.config.root)
        pid: int | None = None
        alive = False
        marker: dict[str, Any] = {}
        for line in output.splitlines():
            if line.startswith("PID="):
                value = line.partition("=")[2].strip()
                pid = int(value) if value.isdigit() else None
            elif line.startswith("ALIVE="):
                alive = line.partition("=")[2].strip() == "true"
            elif line.startswith("{"):
                with contextlib.suppress(json.JSONDecodeError):
                    marker = json.loads(line)
        return ServerStatus(
            pid=pid,
            alive=alive,
            listening=self._tcp_open() if alive else False,
            log_path=f"{self.config.root.rstrip('/')}/logs/policy_server.log",
            marker=marker,
            log_tail=self.log_tail(30) if alive else "",
        )

    def log_tail(self, lines: int = 80) -> str:
        script = (
            "set -eu\nroot=$1\nn=$2\n"
            'test -f "$root/logs/policy_server.log" || exit 0\n'
            'tail -n "$n" "$root/logs/policy_server.log"\n'
        )
        try:
            return self._run(script, self.config.root, str(lines))
        except JetsonPolicyError:
            return ""

    def stop(self, *, timeout_s: float = 10.0) -> dict[str, Any]:
        script = r"""set -eu
root=$1
pidfile="$root/run/policy_server.pid"
marker="$root/run/session.json"
if [ ! -f "$pidfile" ]; then echo "ALREADY_STOPPED"; exit 0; fi
pid=$(cat "$pidfile" 2>/dev/null || true)
if [ -z "$pid" ]; then rm -f "$pidfile"; echo "ALREADY_STOPPED"; exit 0; fi
if ! kill -0 "$pid" 2>/dev/null; then rm -f "$pidfile"; echo "ALREADY_STOPPED"; exit 0; fi
cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
case "$cmd" in *"lerobot.async_inference.policy_server"*) ;; *) echo "NOT_OURS $pid $cmd"; exit 4;; esac
case "$cwd" in "$root"|"$root"/*) ;; *) echo "NOT_OURS_CWD $pid $cwd"; exit 5;; esac
kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
i=0
while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 40 ]; do sleep 0.25; i=$((i+1)); done
if kill -0 "$pid" 2>/dev/null; then kill -KILL -"$pid" 2>/dev/null || true; fi
rm -f "$pidfile" "$marker"
echo "STOPPED $pid"
"""
        code, output = self._ssh_runner(script, self.config.root, timeout=timeout_s + 5)
        if code != 0:
            raise JetsonPolicyError(
                f"refusing to stop a process that is not ours (exit {code}): "
                f"{' '.join(output.splitlines())[-400:]}"
            )
        return {"stopped": True, "output": output.strip()}


def _shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def _manifest_digest(manifest: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, checksum in sorted(manifest.items()):
        digest.update(f"{checksum}  {name}\n".encode())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Observation sources
# ---------------------------------------------------------------------------


class ObservationSource(Protocol):
    def start(self) -> None: ...
    def capture(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


class SyntheticObservationSource:
    """Deterministic observation source for protocol and protection tests."""

    def __init__(
        self,
        contract: PolicyContract,
        *,
        fps: float = 10.0,
        size: tuple[int, int] = (480, 640),
    ):
        self.contract = contract
        self.fps = fps
        self.size = size
        self._stop = threading.Event()
        self._seq = 0
        self.closed = False

    def start(self) -> None:
        self.closed = False

    def capture(self) -> dict[str, Any]:
        if self.closed:
            raise JetsonPolicyError("synthetic source is closed")
        t = time.monotonic()
        observation: dict[str, Any] = {
            name: math.sin(t + i) for i, name in enumerate(self.contract.state_names)
        }
        height, width = self.size
        for i, key in enumerate(self.contract.camera_keys):
            image = np.zeros((height, width, 3), dtype=np.uint8)
            image[:, :, i % 3] = 30 + 10 * (self._seq % 20)
            observation[key] = image
        self._seq += 1
        return observation

    def close(self) -> None:
        self.closed = True


class ReadOnlyFollowerSource:
    """Read SO-101 positions and cameras without configuring motors or torque.

    ``FeetechMotorsBus.connect()`` only opens the port and pings the motors.
    Every write/configuration method is replaced with a guard after construction
    so an accidental future call fails loudly instead of moving the arm.
    """

    FORBIDDEN = (
        "write",
        "sync_write",
        "configure_motors",
        "enable_torque",
        "disable_torque",
        "write_calibration",
        "set_half_turn_homings",
        "record_ranges_of_motion",
        "setup_motor",
    )

    def __init__(
        self,
        port: str,
        calibration_path: str | Path,
        contract: PolicyContract,
        *,
        cameras: list[dict[str, Any]] | None = None,
        bus_factory: Callable[..., Any] | None = None,
        camera_factory: Callable[..., Any] | None = None,
    ):
        self.port = port
        self.calibration_path = Path(calibration_path).expanduser()
        self.contract = contract
        self.cameras = cameras or []
        self._bus_factory = bus_factory
        self._camera_factory = camera_factory
        self._bus: Any = None
        self._captures: dict[str, Any] = {}
        self._capture_mono: dict[str, float] = {}
        self._writes: list[str] = []
        self._bus_lock = threading.Lock()
        self._stop = threading.Event()

    def _build_bus(self) -> Any:
        if self._bus_factory is not None:
            bus = self._bus_factory(self.port, self.calibration_path)
        else:
            from lerobot.motors import Motor, MotorCalibration, MotorNormMode
            from lerobot.motors.feetech import FeetechMotorsBus

            raw = json.loads(self.calibration_path.read_text(encoding="utf-8"))
            motors = {
                name: Motor(
                    int(entry["id"]),
                    "sts3215",
                    MotorNormMode.RANGE_0_100 if name == "gripper" else MotorNormMode.DEGREES,
                )
                for name, entry in raw.items()
            }
            calibration = {
                name: MotorCalibration(
                    id=int(entry["id"]),
                    drive_mode=int(entry.get("drive_mode", 0)),
                    homing_offset=int(entry["homing_offset"]),
                    range_min=int(entry["range_min"]),
                    range_max=int(entry["range_max"]),
                )
                for name, entry in raw.items()
            }
            bus = FeetechMotorsBus(
                port=self.port, motors=motors, calibration=calibration
            )
        for name in self.FORBIDDEN:
            if hasattr(bus, name):
                setattr(bus, name, self._forbidden(name))
        return bus

    def _forbidden(self, name: str) -> Callable[..., Any]:
        def guard(*args: Any, **kwargs: Any) -> Any:
            self._writes.append(name)
            raise JetsonPolicyError(
                f"read-only observation source attempted motor write: {name}"
            )

        return guard

    def _build_camera(self, spec: dict[str, Any]) -> Any:
        if self._camera_factory is not None:
            return self._camera_factory(spec)
        import cv2

        capture = cv2.VideoCapture(int(spec["index"]))
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(spec.get("width", 640)))
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(spec.get("height", 480)))
        capture.set(cv2.CAP_PROP_FPS, int(spec.get("fps", 30)))
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not capture.isOpened():
            raise JetsonPolicyError(f"camera {spec.get('key')} index {spec.get('index')} did not open")
        return capture

    def start(self) -> None:
        if self._bus is not None and getattr(self._bus, "is_connected", False):
            return
        self._bus = self._build_bus()
        self._bus.connect()
        for spec in self.cameras:
            key = str(spec["key"])
            self._captures[key] = self._build_camera(spec)
            for _ in range(5):
                self._captures[key].read()
        logger.info(
            "read-only follower source opened port=%s calibration=%s cameras=%s",
            self.port,
            self.calibration_path,
            list(self._captures),
        )

    def capture(self) -> dict[str, Any]:
        if self._bus is None:
            raise JetsonPolicyError("read-only source is not started")
        with self._bus_lock:
            values = self._bus.sync_read("Present_Position")
        observation: dict[str, Any] = {}
        for name in self.contract.state_names:
            motor = name.removesuffix(".pos")
            if motor not in values:
                raise JetsonPolicyError(f"bus did not return {motor!r}")
            observation[name] = float(values[motor])
        now = time.monotonic()
        for key, capture in self._captures.items():
            ok, frame = capture.read()
            if not ok or frame is None:
                raise JetsonPolicyError(f"camera {key!r} produced no frame")
            observation[key] = frame
            self._capture_mono[key] = now
        return observation

    def camera_ages_ms(self) -> dict[str, float]:
        now = time.monotonic()
        return {
            key: (now - stamp) * 1000.0 for key, stamp in self._capture_mono.items()
        }

    def torque_state(self) -> dict[str, int]:
        """Read-only snapshot of each motor's Torque_Enable register."""
        if self._bus is None:
            return {}
        with self._bus_lock:
            return {
                motor: int(self._bus.read("Torque_Enable", motor, normalize=False))
                for motor in self._bus.motors
            }

    @property
    def forbidden_writes(self) -> tuple[str, ...]:
        return tuple(self._writes)

    def close(self) -> None:
        self._stop.set()
        for capture in self._captures.values():
            with _Suppress():
                capture.release()
        self._captures.clear()
        if self._bus is not None:
            # disable_torque=False: closing the port must not write to the motors.
            with _Suppress():
                self._bus.disconnect(disable_torque=False)
            self._bus = None


class _Suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return True


# ---------------------------------------------------------------------------
# Local action sink and execution gate
# ---------------------------------------------------------------------------


class ActionSink(Protocol):
    def send(self, action: np.ndarray) -> None: ...


class ExecutionNotAllowedError(JetsonPolicyError):
    """Raised when execute mode is requested without the full safety gate."""


REQUIRED_EXECUTE_GATES = (
    "calibration_verified",
    "contract_verified",
    "shadow_verified",
    "stop_verified",
    "site_authorized",
)


@dataclass(frozen=True, slots=True)
class ExecuteGate:
    calibration_verified: bool = False
    contract_verified: bool = False
    shadow_verified: bool = False
    stop_verified: bool = False
    site_authorized: bool = False

    def missing(self) -> list[str]:
        return [name for name in REQUIRED_EXECUTE_GATES if not getattr(self, name)]

    def require(self) -> None:
        missing = self.missing()
        if missing:
            raise ExecutionNotAllowedError(
                "execute mode is blocked; missing gates: " + ", ".join(missing)
            )


@dataclass(slots=True)
class _QueuedAction:
    action: np.ndarray
    expires_mono: float
    observation_seq: int
    index: int


class BoundedActionSink:
    """Local safety wrapper: TTL, shape/finite checks, and optional deltas."""

    def __init__(
        self,
        sink: ActionSink,
        contract: PolicyContract,
        *,
        ttl_ms: float,
        max_abs: float | None = None,
        max_delta: float | None = None,
    ):
        self._sink = sink
        self._contract = contract
        self._ttl_ms = ttl_ms
        self._max_abs = max_abs
        self._max_delta = max_delta
        self._last: np.ndarray | None = None
        self.sent = 0
        self.rejected = 0

    def validate(self, action: np.ndarray) -> str | None:
        action = self._contract.validate_action(action)
        if self._max_abs is not None and np.any(np.abs(action) > self._max_abs):
            return f"action exceeds max_abs={self._max_abs}"
        if (
            self._last is not None
            and self._max_delta is not None
            and float(np.max(np.abs(action - self._last))) > self._max_delta
        ):
            return f"action jump exceeds max_delta={self._max_delta}"
        return None

    def send(self, action: np.ndarray, *, expires_mono: float | None = None) -> None:
        if expires_mono is not None and time.monotonic() > expires_mono:
            self.rejected += 1
            raise JetsonPolicyError("action expired before dispatch")
        reason = self.validate(action)
        if reason:
            self.rejected += 1
            raise JetsonPolicyError(reason)
        self._sink.send(action)
        self._last = np.asarray(action, dtype=np.float64).copy()
        self.sent += 1


# ---------------------------------------------------------------------------
# Client: observation upload, action receipt, expiry, faults
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ClientMetrics:
    observations_captured: int = 0
    observations_sent: int = 0
    chunks_received: int = 0
    chunks_rejected: int = 0
    actions_received: int = 0
    actions_dispatched: int = 0
    upload_ms: list[float] = field(default_factory=list)
    roundtrip_ms: list[float] = field(default_factory=list)
    action_age_ms: list[float] = field(default_factory=list)
    capture_ms: list[float] = field(default_factory=list)
    faults: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        def pct(values: list[float], q: float) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
            return round(ordered[idx], 2)

        return {
            "observations_captured": self.observations_captured,
            "observations_sent": self.observations_sent,
            "chunks_received": self.chunks_received,
            "chunks_rejected": self.chunks_rejected,
            "actions_received": self.actions_received,
            "actions_dispatched": self.actions_dispatched,
            "upload_ms": {
                "p50": pct(self.upload_ms, 0.5),
                "p90": pct(self.upload_ms, 0.9),
                "p99": pct(self.upload_ms, 0.99),
            },
            "roundtrip_ms": {
                "p50": pct(self.roundtrip_ms, 0.5),
                "p90": pct(self.roundtrip_ms, 0.9),
                "p99": pct(self.roundtrip_ms, 0.99),
            },
            "action_age_ms": {
                "p50": pct(self.action_age_ms, 0.5),
                "p90": pct(self.action_age_ms, 0.9),
                "p99": pct(self.action_age_ms, 0.99),
            },
            "capture_ms": {
                "p50": pct(self.capture_ms, 0.5),
                "p90": pct(self.capture_ms, 0.9),
                "p99": pct(self.capture_ms, 0.99),
            },
            "faults": list(self.faults),
        }


class JetsonShadowClient:
    """Observation uploader with explicit observe/shadow/execute boundaries."""

    def __init__(
        self,
        config: JetsonConfig,
        contract: PolicyContract,
        source: ObservationSource,
        *,
        mode: str = MODE_OBSERVE,
        fps: float = 5.0,
        ttl_ms: float = 250.0,
        duration_s: float = 10.0,
        task: str = "",
        execute_gate: ExecuteGate | None = None,
        action_sink: BoundedActionSink | None = None,
        stub_factory: Callable[[], Any] | None = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
        metrics_path: str | Path | None = None,
        starvation_ms: float = 1000.0,
        startup_ttl_ms: float = 5000.0,
    ):
        if mode not in _MODES:
            raise ValueError(f"mode must be one of {_MODES}, got {mode!r}")
        if mode == MODE_EXECUTE:
            (execute_gate or ExecuteGate()).require()
            if action_sink is None:
                raise ExecutionNotAllowedError("execute mode requires a bounded action sink")
        self.config = config
        self.contract = contract
        self.source = source
        self.mode = mode
        self.fps = float(fps)
        self.ttl_ms = float(ttl_ms)
        self.duration_s = float(duration_s)
        self.task = task
        self.action_sink = action_sink
        self._stub_factory = stub_factory
        self._now = monotonic_fn
        self._metrics_path = Path(metrics_path) if metrics_path else None
        self._starvation_ms = float(starvation_ms)
        self._startup_ttl_ms = float(startup_ttl_ms)

        self.epoch = 0
        self._generation = 0
        self.fault: str | None = None
        self.phase = "init"
        self.metrics = ClientMetrics()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._stub: Any = None
        self._channel: Any = None
        self._source_started = False
        self._pending: dict[int, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._last_sent_seq = -1
        self._last_chunk_seq = -1
        self._queue: deque[_QueuedAction] = deque()
        self._queue_lock = threading.Lock()
        self._last_chunk_mono: float | None = None
        self._first_chunk_after_load: float | None = None
        self._session_started_mono: float | None = None
        self._run_started_mono: float | None = None
        self._server_actions_per_chunk = int(contract.n_action_steps or contract.chunk_size or 1)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> dict[str, Any]:
        if self.phase not in {"init", "stopped"}:
            raise JetsonPolicyError(f"client cannot start from phase {self.phase!r}")
        self._stop.clear()
        self.fault = None
        self._generation += 1
        generation = self._generation
        self.phase = "starting_source"
        self.source.start()
        self._source_started = True
        if self.mode == MODE_OBSERVE:
            self.phase = "observing"
            self._run_started_mono = self._now()
            thread = threading.Thread(
                target=self._observe_loop,
                args=(generation,),
                name="jetson-observe",
                daemon=True,
            )
            self._threads = [thread]
            thread.start()
            return self.status()
        if self.mode == MODE_EXECUTE:
            # The local safety wrapper is constructed by the caller; it must
            # already contain the gate.  No local robot is created here.
            logger.info("execute mode enabled behind local safety gates")
        self.phase = "connecting"
        self._connect()
        self._run_started_mono = self._now()
        self._session_started_mono = self._run_started_mono
        self._last_chunk_mono = None
        self._threads = [
            threading.Thread(
                target=self._observation_loop,
                args=(generation,),
                name="jetson-observation",
                daemon=True,
            ),
            threading.Thread(
                target=self._action_loop,
                args=(generation,),
                name="jetson-actions",
                daemon=True,
            ),
            threading.Thread(
                target=self._watchdog_loop,
                args=(generation,),
                name="jetson-watchdog",
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()
        self.phase = "running"
        return self.status()

    def _connect(self) -> None:
        if self._stub_factory is not None:
            self._stub = self._stub_factory()
        else:
            import grpc

            from lerobot.transport import services_pb2_grpc
            from lerobot.transport.utils import grpc_channel_options

            self._channel = grpc.insecure_channel(
                self.config.policy_endpoint, grpc_channel_options()
            )
            self._stub = services_pb2_grpc.AsyncInferenceStub(self._channel)
        import grpc

        from lerobot.async_inference.helpers import RemotePolicyConfig
        from lerobot.transport import services_pb2

        try:
            self._stub.Ready(services_pb2.Empty())
            policy_config = RemotePolicyConfig(
                policy_type=self.contract.policy_type,
                pretrained_name_or_path=self.config.checkpoint,
                lerobot_features=self.contract.dataset_features(),
                actions_per_chunk=self._server_actions_per_chunk,
                device="cuda",
            )
            self.phase = "loading_policy"
            started = self._now()
            self._stub.SendPolicyInstructions(
                services_pb2.PolicySetup(data=pickle.dumps(policy_config))
            )
            self._policy_load_seconds = self._now() - started
        except grpc.RpcError as exc:
            self._fault(f"handshake_failed: {exc.code().name if hasattr(exc, 'code') else exc}")
            raise JetsonPolicyError(self.fault) from exc

    def _observe_loop(self, generation: int) -> None:
        period = 1.0 / max(self.fps, 0.1)
        while not self._stop.is_set() and generation == self._generation:
            started = self._now()
            try:
                observation = self.source.capture()
                self.contract.validate_observation(observation)
            except Exception as exc:  # noqa: BLE001
                if generation == self._generation:
                    self._fault(f"observation_failed: {exc}")
                return
            self.metrics.observations_captured += 1
            self._last_sent_seq = self.metrics.observations_captured - 1
            self._write_metric(
                {"type": "observation", "mode": "observe", "seq": self._last_sent_seq,
                 "state": [observation[n] for n in self.contract.state_names]}
            )
            time.sleep(max(0.0, period - (self._now() - started)))

    def _observation_loop(self, generation: int) -> None:
        from lerobot.async_inference.helpers import TimedObservation

        period = 1.0 / max(self.fps, 0.1)
        while not self._stop.is_set() and generation == self._generation:
            started = self._now()
            try:
                capture_started = self._now()
                observation = self.source.capture()
                capture_ms = (self._now() - capture_started) * 1000.0
                self.contract.validate_observation(observation)
            except Exception as exc:  # noqa: BLE001
                if generation == self._generation:
                    self._fault(f"observation_failed: {exc}")
                return
            observation["task"] = self.task
            self.metrics.observations_captured += 1
            with self._pending_lock:
                seq = self.metrics.observations_captured - 1
                self._pending[seq] = {
                    "capture_mono": capture_started,
                    "capture_ms": capture_ms,
                    "sent_mono": None,
                }
                # Keep only the recent association table.
                for old in [k for k in self._pending if k < seq - 256]:
                    self._pending.pop(old, None)
            if generation != self._generation:
                return
            timed = TimedObservation(
                timestamp=time.time(),
                timestep=seq,
                observation=observation,
                must_go=True,
            )
            upload_started = self._now()
            try:
                self._send_observation(timed)
            except Exception as exc:  # noqa: BLE001
                if generation == self._generation:
                    self._fault(f"observation_upload_failed: {exc}")
                return
            upload_ms = (self._now() - upload_started) * 1000.0
            with self._pending_lock:
                pending = self._pending.get(seq)
                if pending is not None:
                    pending["sent_mono"] = upload_started
            self.metrics.observations_sent += 1
            self.metrics.upload_ms.append(upload_ms)
            self.metrics.capture_ms.append(capture_ms)
            self._last_sent_seq = seq
            self._write_metric(
                {
                    "type": "observation",
                    "mode": self.mode,
                    "seq": seq,
                    "capture_ms": round(capture_ms, 2),
                    "camera_ages_ms": (
                        self.source.camera_ages_ms()
                        if hasattr(self.source, "camera_ages_ms")
                        else None
                    ),
                    "state": [
                        observation[name] for name in self.contract.state_names
                    ],
                }
            )
            time.sleep(max(0.0, period - (self._now() - started)))

    def _send_observation(self, timed: Any) -> None:
        from lerobot.transport import services_pb2
        from lerobot.transport.utils import send_bytes_in_chunks

        payload = pickle.dumps(timed)
        iterator = send_bytes_in_chunks(
            payload, services_pb2.Observation, log_prefix="[lelab] observation", silent=True
        )
        self._stub.SendObservations(iterator)

    def _action_loop(self, generation: int) -> None:
        import grpc

        from lerobot.transport import services_pb2

        while not self._stop.is_set() and generation == self._generation:
            epoch = self.epoch
            try:
                response = self._stub.GetActions(services_pb2.Empty())
            except grpc.RpcError as exc:
                if not self._stop.is_set() and generation == self._generation:
                    self._fault(
                        f"server_disconnected: {exc.code().name if hasattr(exc, 'code') else exc}"
                    )
                return
            except Exception as exc:  # noqa: BLE001
                if not self._stop.is_set() and generation == self._generation:
                    self._fault(f"action_receive_failed: {exc}")
                return
            if generation != self._generation:
                return
            if not getattr(response, "data", b""):
                continue
            received_mono = self._now()
            try:
                chunk = pickle.loads(response.data)  # nosec
                self._handle_chunk(chunk, epoch=epoch, received_mono=received_mono)
            except JetsonPolicyError as exc:
                if generation == self._generation:
                    self._fault(str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                if generation == self._generation:
                    self._fault(f"action_chunk_invalid: {exc}")
                return

    def _handle_chunk(
        self,
        chunk: list[Any],
        *,
        epoch: int | None = None,
        received_mono: float | None = None,
    ) -> None:
        if epoch is not None and epoch != self.epoch:
            self.metrics.chunks_rejected += 1
            return
        if self._stop.is_set():
            return
        received_mono = self._now() if received_mono is None else received_mono
        if not chunk:
            return
        try:
            first_seq = int(chunk[0].get_timestep())
        except Exception as exc:  # noqa: BLE001
            raise JetsonPolicyError("action chunk has no timestep") from exc
        with self._pending_lock:
            association = self._pending.get(first_seq)
        if association is None:
            self.metrics.chunks_rejected += 1
            self._write_metric(
                {"type": "chunk_rejected", "reason": "unknown_observation", "seq": first_seq}
            )
            return
        if first_seq <= self._last_chunk_seq:
            self.metrics.chunks_rejected += 1
            self._write_metric(
                {"type": "chunk_rejected", "reason": "out_of_order", "seq": first_seq,
                 "last": self._last_chunk_seq}
            )
            return
        sent_mono = association.get("sent_mono")
        if sent_mono is None:
            self.metrics.chunks_rejected += 1
            self._write_metric(
                {"type": "chunk_rejected", "reason": "observation_not_associated", "seq": first_seq}
            )
            return
        action_age_ms = (received_mono - association["capture_mono"]) * 1000.0
        ttl_ms = self._startup_ttl_ms if self._first_chunk_after_load is None else self.ttl_ms
        if action_age_ms > ttl_ms:
            self.metrics.chunks_rejected += 1
            self._write_metric(
                {"type": "chunk_rejected", "reason": "expired", "seq": first_seq,
                 "age_ms": round(action_age_ms, 2), "ttl_ms": ttl_ms}
            )
            raise JetsonPolicyError(
                f"action chunk for observation #{first_seq} expired "
                f"({action_age_ms:.1f}ms > {ttl_ms:.1f}ms)"
            )
        validated: list[np.ndarray] = []
        for action in chunk:
            validated.append(self.contract.validate_action(action.get_action()))
        self._last_chunk_seq = first_seq
        self.metrics.chunks_received += 1
        self.metrics.actions_received += len(validated)
        self.metrics.roundtrip_ms.append((received_mono - sent_mono) * 1000.0)
        self.metrics.action_age_ms.append(action_age_ms)
        if self._first_chunk_after_load is None:
            self._first_chunk_after_load = received_mono
        self._last_chunk_mono = received_mono
        self._write_metric(
            {
                "type": "chunk",
                "seq": first_seq,
                "count": len(validated),
                "age_ms": round(action_age_ms, 2),
                "roundtrip_ms": round((received_mono - sent_mono) * 1000.0, 2),
                "first_action": validated[0].round(4).tolist(),
                "mode": self.mode,
            }
        )
        if self.mode == MODE_SHADOW:
            # Display/record only.  No sink exists in this mode.
            return
        if self.mode == MODE_EXECUTE and self.action_sink is not None:
            self._enqueue_actions(validated, first_seq, association["capture_mono"])

    def _enqueue_actions(
        self, actions: list[np.ndarray], first_seq: int, capture_mono: float
    ) -> None:
        ttl_s = self.ttl_ms / 1000.0
        with self._queue_lock:
            self._queue.clear()
            for index, action in enumerate(actions):
                self._queue.append(
                    _QueuedAction(
                        action=action,
                        expires_mono=capture_mono + ttl_s + index / max(self.fps, 0.1),
                        observation_seq=first_seq + index,
                        index=index,
                    )
                )

    def _watchdog_loop(self, generation: int) -> None:
        period = 1.0 / max(self.fps, 0.1)
        while not self._stop.is_set() and generation == self._generation:
            if self.mode == MODE_EXECUTE and self.action_sink is not None:
                self._drain_one()
            last = self._last_chunk_mono
            if (
                generation == self._generation
                and self.mode == MODE_EXECUTE
                and last is not None
                and (self._now() - last) * 1000.0 > self._starvation_ms
            ):
                self._fault(
                    f"action_starvation: no chunk for {self._starvation_ms:.0f}ms"
                )
                return
            time.sleep(period)

    def _drain_one(self) -> None:
        with self._queue_lock:
            if not self._queue:
                return
            item = self._queue[0]
            if self._now() > item.expires_mono:
                self._queue.clear()
                self._fault("queued_action_expired")
                return
            self._queue.popleft()
        try:
            self.action_sink.send(item.action, expires_mono=item.expires_mono)
            self.metrics.actions_dispatched += 1
        except Exception as exc:  # noqa: BLE001
            self._fault(f"action_rejected: {exc}")

    def _fault(self, reason: str) -> None:
        if self._stop.is_set():
            return
        with self._lock:
            if self.fault is not None:
                return
            self.fault = reason
            self.phase = "fault"
            self.metrics.faults.append(reason)
            with self._queue_lock:
                self._queue.clear()
        logger.error("Jetson policy fault: %s", reason)
        self._write_metric({"type": "fault", "reason": reason})
        self._stop.set()

    def status(self) -> dict[str, Any]:
        with self._pending_lock:
            pending = len(self._pending)
        with self._queue_lock:
            queued = len(self._queue)
        return {
            "mode": self.mode,
            "phase": self.phase,
            "fault": self.fault,
            "epoch": self.epoch,
            "observations_captured": self.metrics.observations_captured,
            "observations_sent": self.metrics.observations_sent,
            "chunks_received": self.metrics.chunks_received,
            "chunks_rejected": self.metrics.chunks_rejected,
            "actions_received": self.metrics.actions_received,
            "actions_dispatched": self.metrics.actions_dispatched,
            "pending_observations": pending,
            "queued_actions": queued,
            "ttl_ms": self.ttl_ms,
            "fps": self.fps,
            "policy_endpoint": self.config.policy_endpoint,
        }

    def restart_session(self) -> dict[str, Any]:
        """Open a new session after a fault; the old queue is never reused."""
        if self.mode == MODE_OBSERVE:
            raise JetsonPolicyError("observe mode has no remote session")
        if self.fault is None and self.phase == "running":
            raise JetsonPolicyError("restart_session is only for a stopped/faulted client")
        self._stop.set()
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=2.0)
        with self._queue_lock:
            self._queue.clear()
        with self._pending_lock:
            self._pending.clear()
        if self._channel is not None:
            with _Suppress():
                self._channel.close()
            self._channel = None
        self.epoch += 1
        self._generation += 1
        generation = self._generation
        self.fault = None
        self.phase = "connecting"
        self._last_sent_seq = -1
        self._last_chunk_seq = -1
        self._first_chunk_after_load = None
        self._last_chunk_mono = None
        self._stop.clear()
        self._connect()
        self._run_started_mono = self._now()
        self._session_started_mono = self._run_started_mono
        self._threads = [
            threading.Thread(
                target=self._observation_loop,
                args=(generation,),
                name="jetson-observation",
                daemon=True,
            ),
            threading.Thread(
                target=self._action_loop,
                args=(generation,),
                name="jetson-actions",
                daemon=True,
            ),
            threading.Thread(
                target=self._watchdog_loop,
                args=(generation,),
                name="jetson-watchdog",
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()
        self.phase = "running"
        return self.status()

    def stop(self) -> dict[str, Any]:
        already = self._stop.is_set()
        self._stop.set()
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=2.0)
        with self._queue_lock:
            self._queue.clear()
        if self._channel is not None:
            with _Suppress():
                self._channel.close()
            self._channel = None
        if self._source_started:
            with _Suppress():
                self.source.close()
            self._source_started = False
        if self.phase != "fault":
            self.phase = "stopped"
        self._write_summary()
        return {"stopped": True, "already_stopped": already, "status": self.status()}

    def _write_metric(self, record: dict[str, Any]) -> None:
        if self._metrics_path is None:
            return
        record = {"mono": self._now(), "wall": time.time(), **record}
        self._metrics_path.parent.mkdir(parents=True, exist_ok=True)
        with self._metrics_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def _write_summary(self) -> None:
        if self._metrics_path is None:
            return
        summary_path = self._metrics_path.with_name(
            f"{self._metrics_path.stem}-summary.json"
        )
        payload = {
            "mode": self.mode,
            "phase": self.phase,
            "fault": self.fault,
            "contract": {
                "policy_type": self.contract.policy_type,
                "state_dim": self.contract.state_dim,
                "action_dim": self.contract.action_dim,
                "image_keys": list(self.contract.image_keys),
                "normalization": self.contract.normalization,
                "checkpoint_sha256": self.contract.checkpoint_sha256,
            },
            "elapsed_s": (
                self._now() - self._run_started_mono if self._run_started_mono else 0.0
            ),
            "first_chunk_after_load_s": (
                self._first_chunk_after_load - self._session_started_mono
                if self._first_chunk_after_load and self._session_started_mono
                else None
            ),
            "metrics": self.metrics.summary(),
        }
        summary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Jetson PolicyServer lifecycle and contract")
    parser.add_argument("--host", default=os.environ.get("LELAB_JETSON_HOST", DEFAULT_JETSON_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LELAB_JETSON_PORT", DEFAULT_JETSON_PORT)))
    parser.add_argument("--user", default=os.environ.get("LELAB_JETSON_USER", DEFAULT_JETSON_USER))
    parser.add_argument("--root", default=os.environ.get("LELAB_JETSON_ROOT", DEFAULT_JETSON_ROOT))
    parser.add_argument("--ssh-key", default=os.environ.get("LELAB_JETSON_SSH_KEY", ""))
    parser.add_argument(
        "--policy-port", type=int, default=int(os.environ.get("LELAB_JETSON_POLICY_PORT", DEFAULT_POLICY_PORT))
    )
    parser.add_argument("--python", default=os.environ.get("LELAB_JETSON_PYTHON", DEFAULT_JETSON_PYTHON))
    parser.add_argument(
        "--checkpoint", default=os.environ.get("LELAB_JETSON_CHECKPOINT", DEFAULT_CHECKPOINT)
    )
    parser.add_argument("--policy-type", default=os.environ.get("LELAB_JETSON_POLICY_TYPE", DEFAULT_POLICY_TYPE))
    parser.add_argument("--local-source", default="")
    parser.add_argument("--contract-dir", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync")
    sub.add_parser("probe")
    sub.add_parser("start")
    sub.add_parser("status")
    sub.add_parser("stop")
    sub.add_parser("contract")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command in {"start", "stop", "status"}:
        script = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "jetson"
            / "policy_server_docker.sh"
        )
        if not script.is_file():
            raise SystemExit(f"container lifecycle script not found: {script}")
        return subprocess.run(["bash", str(script), args.command], check=False).returncode

    config = JetsonConfig(
        host=args.host,
        port=args.port,
        user=args.user,
        root=args.root,
        ssh_key=args.ssh_key,
        policy_port=args.policy_port,
        python=args.python,
        checkpoint=args.checkpoint,
        policy_type=args.policy_type,
    )
    server = JetsonPolicyServer(config)
    if args.command == "sync":
        if not args.local_source:
            raise SystemExit("--local-source is required for sync")
        print(json.dumps(server.sync_source(args.local_source), indent=2))
    elif args.command == "probe":
        print(json.dumps(server.runtime_probe(), indent=2))
    elif args.command == "contract":
        path = args.contract_dir or args.checkpoint
        contract = PolicyContract.from_checkpoint(path, policy_type=args.policy_type)
        print(
            json.dumps(
                {
                    "policy_type": contract.policy_type,
                    "state_dim": contract.state_dim,
                    "action_dim": contract.action_dim,
                    "image_keys": list(contract.image_keys),
                    "chunk_size": contract.chunk_size,
                    "n_action_steps": contract.n_action_steps,
                    "normalization": contract.normalization,
                    "preprocessor_steps": list(contract.preprocessor_steps),
                    "postprocessor_steps": list(contract.postprocessor_steps),
                    "checkpoint_sha256": contract.checkpoint_sha256,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
