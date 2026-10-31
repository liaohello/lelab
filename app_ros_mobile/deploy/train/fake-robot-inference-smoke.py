#!/usr/bin/env python3
"""Cloud-inference smoke test: the real robot client against a fake SO-101.

It uses the exact LeLab session machinery (stage the policy on the train
server, start the loopback PolicyServer, open the SSH tunnel) and then runs the
real ``lerobot.async_inference.robot_client`` against a synthetic 6-joint arm
with two 640x480 cameras. Observation uploads therefore cross the same
WiFi/SSH path as a live session, but no hardware moves.

Use it after client-side changes to check the sustained control-loop rate and
the observation->chunk latency (``[PERF]`` lines):

    set -a && . ~/Desktop/lelab/shared/config/jetson.env && set +a
    .venv/bin/python deploy/train/fake-robot-inference-smoke.py \
        --policy-ref ~/Desktop/lelab_server/models/policies/<staged-policy> \
        --duration 45

The script prints the diagnostics report for the client log it just produced.
"""

from __future__ import annotations

import argparse
import contextlib
import threading
import time
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import numpy as np

from lelab import remote_inference as ri
from lelab.remote_sync import TrainServerConfig
from lelab.runners import ssh_ops
from lelab.train_server import marker_payload
from lelab.utils import inference_diag
from lelab.utils.policy_refs import parse_policy_ref
from lerobot.robots import Robot, RobotConfig

MOTORS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")


@RobotConfig.register_subclass("lelab_fake_so101")
@dataclass
class FakeSO101Config(RobotConfig):
    """Synthetic arm that mimics an SO-101 follower's features."""

    # Median start pose of the red-bowl demonstrations (degrees).
    state_deg: tuple[float, ...] = (-2.6, -102.2, 91.9, 74.5, -0.4, 1.0)
    camera_shapes: dict[str, tuple[int, int]] = field(
        default_factory=lambda: {"up": (480, 640), "right": (480, 640)}
    )
    image_seed: int = 7
    max_step_deg: float = 1.5

    def __post_init__(self) -> None:
        if len(self.state_deg) != len(MOTORS):
            raise ValueError(f"state_deg needs {len(MOTORS)} values, got {len(self.state_deg)}")


class FakeSO101(Robot):
    """Fake follower: noisy images, first-order joint tracking, no serial bus."""

    config_class = FakeSO101Config
    name = "lelab_fake_so101"

    def __init__(self, config: FakeSO101Config) -> None:
        super().__init__(config)
        self.config = config
        self._is_connected = False
        self._state = dict(zip(MOTORS, config.state_deg, strict=True))
        rng = np.random.default_rng(config.image_seed)
        # One fixed noise frame per camera: the pickle/upload cost matches a real
        # 640x480 RGB observation while the CPU stays free for the control loop.
        self._images = {
            name: rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
            for name, (height, width) in config.camera_shapes.items()
        }

    @property
    def _motors_ft(self) -> dict[str, type]:
        return {f"{motor}.pos": float for motor in MOTORS}

    @cached_property
    def observation_features(self) -> dict[str, type | tuple]:
        return {
            **self._motors_ft,
            **{name: (height, width, 3) for name, (height, width) in self.config.camera_shapes.items()},
        }

    @cached_property
    def action_features(self) -> dict[str, type]:
        return self._motors_ft

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def is_calibrated(self) -> bool:
        return True

    def connect(self, calibrate: bool = True) -> None:
        self._is_connected = True

    def calibrate(self) -> None:
        pass

    def configure(self) -> None:
        pass

    def disconnect(self) -> None:
        self._is_connected = False

    def get_observation(self) -> dict:
        observation = {f"{motor}.pos": float(value) for motor, value in self._state.items()}
        observation.update(self._images)
        return observation

    def send_action(self, action: dict) -> dict:
        for motor in MOTORS:
            key = f"{motor}.pos"
            if key not in action:
                continue
            current = self._state[motor]
            delta = max(-self.config.max_step_deg, min(self.config.max_step_deg, action[key] - current))
            self._state[motor] = current + delta
        return {f"{motor}.pos": self._state[motor] for motor in MOTORS}


def _run_client(policy_path: str, port: int, args: argparse.Namespace) -> Path:
    # The DEBUG log file is created when the client module is imported.
    before = set(Path("logs").glob("robot_client_*.log"))

    from lerobot.async_inference.configs import RobotClientConfig
    from lerobot.async_inference.robot_client import RobotClient

    config = RobotClientConfig(
        robot=FakeSO101Config(id="lelab-fake-so101"),
        server_address=f"127.0.0.1:{port}",
        policy_type=args.policy_type,
        pretrained_name_or_path=policy_path,
        actions_per_chunk=args.actions_per_chunk,
        chunk_size_threshold=args.chunk_size_threshold,
        fps=args.fps,
        task=args.task,
        policy_device="cuda",
        client_device="cpu",
    )
    print(
        f"[client] fake SO-101: 6 joints, {len(config.robot.camera_shapes)} cameras, "
        f"actions_per_chunk={args.actions_per_chunk}, fps={args.fps}"
    )
    client = RobotClient(config)

    # The remote gRPC server binds its port a moment before it starts serving;
    # keep retrying so a reset during that window is not fatal.
    deadline = time.time() + args.startup_timeout
    while True:
        if client.start():
            break
        if time.time() >= deadline:
            raise RuntimeError("robot client could not reach the PolicyServer")
        time.sleep(2.0)
    receiver = threading.Thread(target=client.receive_actions, daemon=True, name="action-receiver")
    receiver.start()
    timer = threading.Timer(args.duration, client.shutdown_event.set)
    timer.daemon = True
    timer.start()
    started = time.time()
    try:
        client.control_loop(task=config.task)
    finally:
        timer.cancel()
        client.stop()
        receiver.join(timeout=5.0)
    print(f"[client] control loop ran {time.time() - started:.1f}s")

    new_logs = sorted(set(Path("logs").glob("robot_client_*.log")) - before)
    if not new_logs:
        raise RuntimeError("robot client did not write a DEBUG log")
    return new_logs[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-ref", required=True, help="server path or hub ref to the checkpoint")
    parser.add_argument("--duration", type=float, default=45.0)
    parser.add_argument("--policy-type", default="act")
    parser.add_argument("--actions-per-chunk", type=int, default=50)
    parser.add_argument("--chunk-size-threshold", type=float, default=0.5)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--task", default="pick up the red bowl")
    parser.add_argument("--stamp", default="")
    parser.add_argument("--startup-timeout", type=float, default=60.0)
    args = parser.parse_args()

    cfg: TrainServerConfig = ri._train_config()
    env = ri._remote_env(cfg)
    stamp = args.stamp or time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())

    print(f"[stage] resolving {args.policy_ref} on {cfg.endpoint}")
    policy_path = ssh_ops.stage_policy_on_server(
        cfg, parse_policy_ref(args.policy_ref), python=ssh_ops.remote_python(), env=env, timeout=900.0
    )
    print(f"[stage] remote policy: {policy_path}")

    _host, port = ri.train_policy_endpoint()
    pid, log = ri._start_remote_policy_server(cfg, port=port, env=env, stamp=stamp)
    ri._write_session_marker(
        cfg,
        stamp,
        marker_payload(
            "policy_server",
            stamp=stamp,
            pid=pid,
            port=port,
            gpu=None,
            policy_ref=args.policy_ref,
            policy_path=policy_path,
            log_path=log,
        ),
    )
    print(f"[server] pid {pid}, log {log}")

    tunnel = None
    client_log: Path | None = None
    try:
        tunnel = ri._open_tunnel(cfg, local_port=port, remote_port=port)
        if not ri._wait_for_local_port(port, timeout=20.0, proc=tunnel):
            raise RuntimeError(f"tunnel to {cfg.endpoint} did not come up on 127.0.0.1:{port}")
        print(f"[tunnel] 127.0.0.1:{port} -> {cfg.endpoint}:{port}")
        client_log = _run_client(policy_path, port, args)
    finally:
        if tunnel is not None:
            with contextlib.suppress(Exception):
                tunnel.terminate()
                tunnel.wait(timeout=5)
        ssh_ops.stop_remote_pid(cfg, pid)
        _code, out = ssh_ops.run_script(
            cfg, 'kill -0 "$1" 2>/dev/null && echo ALIVE || echo STOPPED\n', str(pid)
        )
        print(f"[cleanup] server {out.strip() or 'unknown'}")

    if client_log is not None:
        print("\n" + inference_diag.render_text(inference_diag.build_report(client_log, None)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
