# Copyright 2026 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""LeLab adapter for the LeRobot 0.7.0 remote inference stack.

LeRobot 0.7.0 retired ``lerobot.async_inference`` and its gRPC PolicyServer.
The replacement is a two-process Zenoh deployment:

* ``lerobot-policy-server`` loads one pinned checkpoint and owns the model;
* ``lerobot-rollout --inference.type=remote`` runs on the robot host and keeps
  the hardware lease, safety limits and action deadlines.

This module builds those two commands from validated LeLab requests. It never
starts a process, downloads weights or opens a device. Both ends bind a TCP
Zenoh endpoint on loopback, so the existing SSH tunnel remains the only way to
reach the training host.

The checkpoint-facing schema is operator-owned: a deployment name, an explicit
units/order semantics string and the feature shapes must be supplied. Nothing
here guesses component order from a policy family or silently reuses the old
``chunk_size_threshold`` / ``actions_per_chunk`` knobs, which have no 0.7
equivalent.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar

SERVER_MODULE = "lerobot.scripts.lerobot_policy_server"
ROLLOUT_MODULE = "lerobot.scripts.lerobot_rollout"
LEGACY_TOKENS = (
    "lerobot.async_inference",
    "lerobot.transport.services_pb2",
    "--server_address",
    "--policy_type",
    "--pretrained_name_or_path",
    "--inference_latency",
    "--obs_queue_timeout",
    "--chunk_size_threshold",
    "--actions_per_chunk",
)

_T = TypeVar("_T")


class Inference070Error(ValueError):
    """A remote-inference request cannot be expressed by the 0.7 contract."""


@dataclass(frozen=True)
class DeploymentSpec:
    """Operator-declared identity and schema of one remote model deployment."""

    deployment: str
    semantics: str
    checkpoint: str
    device: str = "cuda"
    port: int = 7447
    action_fps: float = 30.0
    action_deadline_s: float = 5.0
    idle_timeout_s: float = 10.0
    state_dim: int = 6
    action_dim: int = 6
    state_names: tuple[str, ...] = ()
    camera_shapes: Mapping[str, tuple[int, int, int]] = field(default_factory=dict)
    robot_type: str = "so101_follower"
    revision: str | None = None

    def __post_init__(self) -> None:
        for name in ("deployment", "semantics", "checkpoint", "device"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise Inference070Error(f"{name} must be a nonempty string")
        if not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise Inference070Error("port must be a TCP port")
        for name in ("action_fps", "action_deadline_s", "idle_timeout_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise Inference070Error(f"{name} must be finite and positive")
        for name in ("state_dim", "action_dim"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise Inference070Error(f"{name} must be a positive integer")
        if self.state_names and len(self.state_names) != self.state_dim:
            raise Inference070Error("state_names length does not match state_dim")

    @property
    def endpoint(self) -> str:
        """The Zenoh direct endpoint used on both sides of the SSH tunnel."""
        return f"tcp/127.0.0.1:{self.port}"

    def server_config(self) -> dict[str, Any]:
        """A dict accepted by ``lerobot.remote_inference.ServerConfig``.

        JSON is a subset of YAML, so the same dict can also be written as the
        server's ``--config_path`` file without another serialization contract.
        """
        state_names = list(self.state_names) or [f"joint_{i}" for i in range(self.state_dim)]
        state_feature: dict[str, Any] = {
            "name": "observation.state",
            "shape": [self.state_dim],
            "dtype": "float32",
            "names": state_names,
            "semantics": self.semantics,
        }
        features: list[dict[str, Any]] = [state_feature]
        for name, shape in sorted(self.camera_shapes.items()):
            if len(shape) != 3 or shape[-1] != 3:
                raise Inference070Error(f"camera {name!r} must use HWC RGB shape, got {shape}")
            features.append(
                {
                    "name": f"observation.images.{name}",
                    "shape": list(shape),
                    "dtype": "uint8",
                    "kind": "rgb",
                    "semantics": f"rgb-{name}-v1",
                }
            )
        return {
            "deployment": self.deployment,
            "model": {
                "repo_or_path": self.checkpoint,
                "revision": self.revision,
                "device": self.device,
            },
            "execution": {
                "supported_modes": ["chunk"],
                "action_fps": self.action_fps,
                "action_deadline_s": self.action_deadline_s,
                "idle_timeout_s": self.idle_timeout_s,
            },
            "language": {"enabled": False},
            "zenoh": {"mode": "peer", "listen_endpoints": [self.endpoint]},
            "semantics": self.semantics,
            "robot_type": self.robot_type,
            "features": features,
            "action_feature": {
                "name": "action",
                "shape": [self.action_dim],
                "dtype": "float32",
                "names": state_names[: self.action_dim],
                "semantics": self.semantics,
            },
        }

    def write_config(self, path: str | Path) -> Path:
        """Write the operator-owned server config; parent directories are created."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.server_config(), indent=2, sort_keys=True) + "\n")
        return target


def server_command(python: str, config_path: str | Path) -> list[str]:
    """Command line for the 0.7 policy server (run on the model host)."""
    return [python, "-m", SERVER_MODULE, f"--config_path={config_path}"]


def rollout_command(
    python: str,
    *,
    spec: DeploymentSpec,
    request: Any,
    robot_args: list[str],
) -> list[str]:
    """Command line for the 0.7 remote rollout (run on the robot host).

    ``robot_args`` is supplied by the existing LeLab safety/robot builders so
    limits and camera identities are not reconstructed here.
    """
    refill = float(getattr(request, "remote_refill_seconds", 0.5))
    action_timeout = float(getattr(request, "remote_action_timeout_s", 5.0))
    for name, value in (("remote_refill_seconds", refill), ("remote_action_timeout_s", action_timeout)):
        if not math.isfinite(value) or value <= 0:
            raise Inference070Error(f"{name} must be finite and positive")
    cmd = [
        python,
        "-m",
        ROLLOUT_MODULE,
        "--inference.type=remote",
        f"--inference.endpoint={spec.endpoint}",
        f"--inference.deployment={spec.deployment}",
        f"--inference.mode=chunk",
        f"--inference.chunk_merge=aligned",
        f"--inference.semantics={spec.semantics}",
        f"--inference.refill_seconds={refill}",
        f"--inference.action_timeout_s={action_timeout}",
        *robot_args,
        f"--fps={request.fps}",
    ]
    task = (getattr(request, "task", "") or "").strip()
    if task:
        cmd.append(f"--task={task}")
    assert_no_legacy_tokens(cmd)
    return cmd


def assert_no_legacy_tokens(argv: list[str]) -> None:
    """Fail before spawn if a retired 0.6 flag or module slipped into a command."""
    joined = " ".join(argv)
    found = [token for token in LEGACY_TOKENS if token in joined]
    if found:
        raise Inference070Error(f"retired async-inference tokens in command: {found}")


def action_is_dispatchable(action: Any, *, width: int, capture_age_s: float, ttl_s: float) -> bool:
    """Final app-side gate before a remote action reaches the hardware.

    The 0.7 engine already enforces generation, session identity and deadlines;
    this is the last local check at the ``send_action`` boundary. It refuses
    wrong width, non-finite values and actions older than the local TTL. It
    never clips, reorders or repairs an action.
    """
    import numpy as np

    array = np.asarray(action)
    if array.shape != (width,) or array.dtype.kind not in "fiu":
        return False
    if not np.isfinite(array).all():
        return False
    if ttl_s <= 0 or capture_age_s < 0 or capture_age_s > ttl_s:
        return False
    return True


class LatestObservationSender:
    """Non-blocking, drop-oldest bridge used by LeLab's own control loops.

    LeRobot 0.7's remote engine owns transport-level observation submission,
    but LeLab still needs a stdlib-only bridge between a camera/joint callback
    and the engine. Only the newest sample is kept; a ``must_go`` sample is not
    replaced by a later ordinary sample. Failures are counted, never retried
    forever, and ``stop()`` always ends the worker thread.
    """

    def __init__(self, send: Callable[[Any], bool], *, ttl_s: float | None = None) -> None:
        self._send = send
        self._ttl_s = ttl_s
        self._pending: Any = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.submitted = 0
        self.sent = 0
        self.dropped = 0
        self.failed = 0

    @property
    def stats(self) -> Any:
        """Counters in the shape used by the former fork helper."""
        return SimpleNamespace(
            submitted=self.submitted,
            sent=self.sent,
            dropped=self.dropped,
            failed=self.failed,
        )

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="lelab-latest-observation", daemon=True)
        self._thread.start()

    def submit(self, observation: Any, *, must_go: bool = False) -> None:
        must_go = bool(must_go or getattr(observation, "must_go", False))
        with self._lock:
            self.submitted += 1
            previous = self._pending
            if previous is not None and getattr(previous[1], "must_go", False) and not must_go:
                self.dropped += 1
                return
            if previous is not None:
                self.dropped += 1
            self._pending = (time.monotonic(), observation, must_go)
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=0.1)
            self._wake.clear()
            while True:
                with self._lock:
                    item = self._pending
                    self._pending = None
                if item is None:
                    break
                captured, observation, _ = item
                if self._ttl_s is not None and time.monotonic() - captured > self._ttl_s:
                    continue
                try:
                    ok = bool(self._send(observation))
                except Exception:  # noqa: BLE001 - a transport failure must not kill the loop
                    ok = False
                if ok:
                    self.sent += 1
                else:
                    self.failed += 1
