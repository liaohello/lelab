#!/usr/bin/env python3
"""Cloud-inference plumbing probe: no robot, no cameras, no motion.

Starts nothing itself — it expects a PolicyServer reachable at
``--server 127.0.0.1:28081`` (the LeLab cloud session opens the SSH tunnel and
the server; ``lelab.remote_inference`` can also be driven directly). It then
performs exactly the gRPC sequence the robot client does:

1. ``Ready``                     — client registration;
2. ``SendPolicyInstructions``    — server loads the policy from its local path;
3. ``SendObservations``          — one synthetic observation (zeros) with the
                                    features the ACT checkpoint expects;
4. ``GetActions``                — receive one action chunk and print it.

Use it to verify GPU inference and the tunnel without risking arm motion:

    python deploy/train/probe-cloud-policy.py \
        --server 127.0.0.1:28081 \
        --policy /home/trainer/Desktop/lelab_server/models/pretrained/act_so101 \
        --policy-type act
"""

from __future__ import annotations

import argparse
import pickle  # nosec - same pickled payload the robot client sends
import time

import grpc
import torch

from lerobot.transport import services_pb2, services_pb2_grpc
from lerobot.transport.utils import grpc_channel_options, send_bytes_in_chunks


def build_features(state_dim: int, cameras: list[str], height: int, width: int) -> dict:
    """Dataset-style feature spec, i.e. what the robot client sends.

    Shape mirrors ``hw_to_dataset_features(robot.observation_features, "observation")``:
    joints are ``float32`` vectors, cameras are ``(H, W, 3)`` images.
    """
    features = {
        "observation.state": {
            "dtype": "float32",
            "shape": (state_dim,),
            "names": [f"joint_{i}" for i in range(state_dim)],
        }
    }
    for name in cameras:
        features[f"observation.images.{name}"] = {
            "dtype": "image",
            "shape": (height, width, 3),
            "names": ["height", "width", "channels"],
        }
    return features


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="127.0.0.1:28081")
    parser.add_argument("--policy", required=True, help="pretrained_model dir on the server")
    parser.add_argument("--policy-type", default="act")
    parser.add_argument("--state-dim", type=int, default=6)
    parser.add_argument("--cameras", default="wrist,front")
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--actions-per-chunk", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    from lerobot.async_inference.helpers import RemotePolicyConfig, TimedObservation

    cameras = [name for name in args.cameras.split(",") if name]
    features = build_features(args.state_dim, cameras, args.height, args.width)
    channel = grpc.insecure_channel(args.server, options=grpc_channel_options(initial_backoff="0.5s"))
    stub = services_pb2_grpc.AsyncInferenceStub(channel)

    print(f"[1/4] Ready -> {args.server}")
    stub.Ready(services_pb2.Empty(), timeout=args.timeout)

    print(f"[2/4] SendPolicyInstructions (loads {args.policy} on the server)")
    spec = RemotePolicyConfig(
        policy_type=args.policy_type,
        pretrained_name_or_path=args.policy,
        lerobot_features=features,
        actions_per_chunk=args.actions_per_chunk,
        device="cuda",
    )
    t0 = time.time()
    stub.SendPolicyInstructions(
        services_pb2.PolicySetup(data=pickle.dumps(spec)),  # nosec
        timeout=args.timeout,
    )
    print(f"      policy loaded in {time.time() - t0:.1f}s")

    # Raw keys are what the robot client would send: one entry per motor plus
    # one per camera (short name). `build_dataset_frame` maps them onto the
    # dataset feature names above.
    raw: dict[str, object] = {f"joint_{i}": 0.0 for i in range(args.state_dim)}
    for name in cameras:
        raw[name] = torch.zeros((args.height, args.width, 3), dtype=torch.uint8)
    observation = TimedObservation(
        timestamp=time.time(),
        timestep=0,
        observation=raw,
        must_go=True,
    )

    print("[3/4] SendObservations (one synthetic frame)")
    stub.SendObservations(
        send_bytes_in_chunks(
            pickle.dumps(observation),  # nosec
            services_pb2.Observation,
            log_prefix="probe",
            silent=True,
        ),
        timeout=args.timeout,
    )

    print("[4/4] GetActions")
    t0 = time.time()
    actions = stub.GetActions(services_pb2.Empty(), timeout=args.timeout)
    chunk = pickle.loads(actions.data)  # nosec
    first = chunk[0]
    action_tensor = first.get_action() if hasattr(first, "get_action") else first
    print(f"      chunk received in {time.time() - t0:.2f}s")
    print(f"      actions={len(chunk)} first_action={action_tensor.tolist()}")
    print("CLOUD INFERENCE PROBE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
