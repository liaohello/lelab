#!/usr/bin/env bash
# Validate the GPU stack, then run the loopback-only PolicyServer.
set -euo pipefail

host=${LEROBOT_POLICY_HOST:-127.0.0.1}
port=${LEROBOT_POLICY_PORT:-8080}
fps=${LEROBOT_POLICY_FPS:-30}
latency=${LEROBOT_POLICY_INFERENCE_LATENCY:-0.033}
obs_timeout=${LEROBOT_POLICY_OBS_QUEUE_TIMEOUT:-1}

[[ "$host" == "127.0.0.1" ]] || { echo "PolicyServer 必须绑 loopback（当前 $host）" >&2; exit 64; }

echo "[entrypoint] torch/CUDA 自检"
python3 - <<'PY'
import torch
print(f"  torch {torch.__version__} | cuda_available={torch.cuda.is_available()}")
if torch.cuda.is_available():
    cap = torch.cuda.get_device_capability()
    name = torch.cuda.get_device_name(0)
    print(f"  device={name} capability=sm_{cap[0]}{cap[1]}")
    x = torch.randn(1024, 1024, device="cuda")
    print(f"  matmul ok: {(x @ x).sum().item():.3f}")
    if (cap[0], cap[1]) != (8, 7):
        print("  WARNING: 期望 sm_87")
else:
    raise SystemExit("CUDA 不可用：容器未拿到 GPU（检查 --runtime nvidia）")
PY

echo "[entrypoint] PolicyServer $host:$port (fps=$fps)"
exec python3 -m lerobot.async_inference.policy_server \
  --host="$host" --port="$port" --fps="$fps" \
  --inference_latency="$latency" --obs_queue_timeout="$obs_timeout"
