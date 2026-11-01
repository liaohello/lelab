#!/usr/bin/env python3
"""Run observation-only or shadow inference against the Jetson PolicyServer.

Examples::

    # Read-only local hardware: no gRPC channel, no action sink.
    python deploy/jetson/run_shadow.py --mode observe --source follower --duration 10

    # Shadow: upload real observations, receive actions, never execute.
    python deploy/jetson/run_shadow.py --mode shadow --source follower --duration 60

This script never exposes execute mode.  Execute mode is only reachable through
the gated ``JetsonShadowClient`` API and is not part of this entry point.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from lelab import jetson_policy as jp


def _parse_cameras(value: str) -> list[dict]:
    cameras = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, index = item.partition("=")
        if not name or not index:
            raise SystemExit(f"camera must be name=index, got {item!r}")
        cameras.append(
            {
                "key": name,
                "index": int(index),
                "width": 640,
                "height": 480,
                "fps": 30,
            }
        )
    return cameras


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=(jp.MODE_OBSERVE, jp.MODE_SHADOW), required=True)
    parser.add_argument("--source", choices=("synthetic", "follower"), default="follower")
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--fps", type=float, default=5.0)
    parser.add_argument("--ttl-ms", type=float, default=5000.0)
    parser.add_argument("--startup-ttl-ms", type=float, default=15000.0)
    parser.add_argument("--task", default="")
    parser.add_argument("--cameras", default="right=0,up=2")
    parser.add_argument(
        "--port",
        default="/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B91045575-if00",
    )
    parser.add_argument(
        "--calibration",
        default=str(
            Path.home()
            / ".local/share/lelab-jetson-20261031/calibration/my_robot.json"
        ),
    )
    parser.add_argument(
        "--checkpoint-dir",
        default=str(
            Path.home() / ".local/share/lelab-jetson-20261031/checkpoints"
        ),
        help="local copy of the checkpoint metadata used for contract validation",
    )
    parser.add_argument(
        "--remote-checkpoint",
        default=os.environ.get("LELAB_JETSON_CHECKPOINT", jp.DEFAULT_CHECKPOINT),
        help="checkpoint path as seen by the Jetson PolicyServer",
    )
    parser.add_argument("--host", default=os.environ.get("LELAB_JETSON_HOST", jp.DEFAULT_JETSON_HOST))
    parser.add_argument(
        "--policy-port",
        type=int,
        default=int(os.environ.get("LELAB_JETSON_POLICY_PORT", jp.DEFAULT_POLICY_PORT)),
    )
    parser.add_argument("--metrics", default="")
    parser.add_argument("--observe-warmup-s", type=float, default=1.0)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    contract = jp.PolicyContract.from_checkpoint(args.checkpoint_dir)
    if args.source == "synthetic":
        source: jp.ObservationSource = jp.SyntheticObservationSource(
            contract, fps=args.fps
        )
    else:
        source = jp.ReadOnlyFollowerSource(
            args.port,
            args.calibration,
            contract,
            cameras=_parse_cameras(args.cameras),
        )

    metrics = Path(args.metrics) if args.metrics else (
        Path.home()
        / ".local/share/lelab-jetson-20261031/logs"
        / f"{args.mode}-{args.source}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    )
    config = jp.JetsonConfig(
        host=args.host,
        policy_port=args.policy_port,
        checkpoint=args.remote_checkpoint,
    )
    client = jp.JetsonShadowClient(
        config,
        contract,
        source,
        mode=args.mode,
        fps=args.fps,
        ttl_ms=args.ttl_ms,
        startup_ttl_ms=args.startup_ttl_ms,
        duration_s=args.duration,
        task=args.task,
        metrics_path=metrics,
    )
    print(
        json.dumps(
            {
                "mode": args.mode,
                "source": args.source,
                "contract": {
                    "policy_type": contract.policy_type,
                    "state_dim": contract.state_dim,
                    "action_dim": contract.action_dim,
                    "image_keys": list(contract.image_keys),
                    "sha256": contract.checkpoint_sha256,
                },
                "policy_endpoint": config.policy_endpoint,
                "metrics": str(metrics),
            },
            indent=2,
        ),
        flush=True,
    )

    started = time.monotonic()
    try:
        if isinstance(source, jp.ReadOnlyFollowerSource):
            source.start()
            print(
                json.dumps(
                    {
                        "source": "read_only_follower",
                        "port": source.port,
                        "calibration": str(source.calibration_path),
                        "torque_before": source.torque_state(),
                    }
                ),
                flush=True,
            )
        client.start()
        if args.mode == jp.MODE_OBSERVE and args.observe_warmup_s > 0:
            time.sleep(min(args.observe_warmup_s, args.duration))
        while time.monotonic() - started < args.duration:
            if client.fault:
                break
            time.sleep(0.1)
            status = client.status()
            if status["observations_captured"]:
                print(json.dumps(status), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        result = client.stop()
        if isinstance(source, jp.ReadOnlyFollowerSource):
            verifier = jp.ReadOnlyFollowerSource(
                source.port, source.calibration_path, contract, cameras=[]
            )
            verifier.start()
            print(json.dumps({"torque_after": verifier.torque_state()}), flush=True)
            verifier.close()
        summary_path = metrics.with_name(f"{metrics.stem}-summary.json")
        summary = (
            json.loads(summary_path.read_text())
            if summary_path.is_file()
            else {"metrics": client.metrics.summary()}
        )
        print(json.dumps({"stop": result, "summary": summary}, indent=2), flush=True)
    return 0 if client.fault is None else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
