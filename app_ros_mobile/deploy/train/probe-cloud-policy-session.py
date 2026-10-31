#!/usr/bin/env python3
"""Hardware-free rehearsal of a cloud inference session.

Runs the exact LeLab machinery (`lelab.remote_inference`) — stage policy on the
train server, start the loopback PolicyServer, open the SSH tunnel — but talks
to it with `probe-cloud-policy.py` (synthetic observation, no robot client)
instead of the real robot. Use it to verify a checkpoint, the mirror/proxy
setup or the tunnel without any arm motion.

    # on the Jetson, from the app checkout
    set -a && . ~/Desktop/lelab/shared/config/jetson.env && set +a
    .venv/bin/python ~/Desktop/lelab/deploy/train/probe-cloud-policy-session.py \
        --policy-ref l-e-n/act_so101@root
    .venv/bin/python .../probe-cloud-policy-session.py \
        --policy-ref ~/Desktop/lelab_server/jobs/<job>/run/checkpoints/000050/pretrained_model
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from lelab import remote_inference as ri
from lelab.remote_sync import TrainServerConfig
from lelab.runners import ssh_ops
from lelab.utils.policy_refs import parse_policy_ref

PROBE = Path(__file__).with_name("probe-cloud-policy.py")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--policy-ref",
        required=True,
        help="absolute path on the train server, or 'repo@checkpoints/<step>' / 'repo@root'",
    )
    parser.add_argument("--policy-type", default="act")
    parser.add_argument("--actions-per-chunk", type=int, default=16)
    parser.add_argument(
        "--cameras",
        default="wrist,front",
        help="comma-separated camera feature names from the policy config",
    )
    parser.add_argument("--state-dim", type=int, default=6)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--stamp", default="probe-session")
    parser.add_argument("--stage-timeout", type=float, default=900.0)
    args = parser.parse_args()

    cfg = TrainServerConfig.from_env()
    env = ri._remote_env(cfg)

    t0 = time.time()
    policy_path = ssh_ops.stage_policy_on_server(
        cfg,
        parse_policy_ref(args.policy_ref),
        python=ssh_ops.remote_python(),
        env=env,
        timeout=args.stage_timeout,
    )
    print(f"[stage] {time.time() - t0:5.1f}s -> {policy_path}")

    pid, log = ri._start_remote_policy_server(cfg, port=28081, env=env, stamp=args.stamp)
    print(f"[server] pid {pid}, log {log}")

    tunnel = None
    rc = 1
    try:
        _host, port = ri.train_policy_endpoint()
        tunnel = ri._open_tunnel(cfg, local_port=port, remote_port=port)
        if not ri._wait_for_local_port(port, timeout=20.0, proc=tunnel):
            raise RuntimeError(f"tunnel to {cfg.endpoint} did not come up on 127.0.0.1:{port}")
        print(f"[tunnel] 127.0.0.1:{port} -> {cfg.endpoint}:{port}")
        rc = subprocess.call(
            [
                sys.executable,
                str(PROBE),
                "--server",
                f"127.0.0.1:{port}",
                "--policy",
                policy_path,
                "--policy-type",
                args.policy_type,
                "--actions-per-chunk",
                str(args.actions_per_chunk),
                "--cameras",
                args.cameras,
                "--state-dim",
                str(args.state_dim),
                "--height",
                str(args.height),
                "--width",
                str(args.width),
            ]
        )
    finally:
        if tunnel is not None:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except Exception:
                tunnel.kill()
        ssh_ops.stop_remote_pid(cfg, pid)
        _code, out = ssh_ops.run_script(
            cfg, 'kill -0 "$1" 2>/dev/null && echo ALIVE || echo STOPPED\n', str(pid)
        )
        print("[cleanup] server " + out.strip())
        _code, out = ssh_ops.run_script(
            cfg, ssh_ops.EXPAND_PATH_FUNCTION + 'path=$(expand_remote_path "$1")\ntail -n 6 "$path"\n', log
        )
        print("[server log tail]\n" + out.strip()[-1200:])
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
