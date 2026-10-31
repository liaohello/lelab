#!/usr/bin/env bash
# End-to-end container-inference bring-up (run ON the Jetson, no sudo needed).
#
#   bash ~/Desktop/lelab/deploy/jetson/run-container-inference-setup.sh
#
# 1. GPU smoke test with a local CUDA image (no pull),
# 2. pull nvcr.io/nvidia/pytorch:25.06-py3-igpu through the host mihomo,
# 3. build lelab-inference:local,
# 4. start it and verify sm_87 + matmul + ACT load (container logs),
# 5. optional synthetic probe (LELAB_RUN_PROBE=1) via the repo probe script.
set -uo pipefail

root=${LELAB_ROOT:-$HOME/Desktop/lelab}
log_dir=$root/logs
mkdir -p "$log_dir"
log=$log_dir/container-inference-setup.log

step() { echo; echo "=== $* $(date +%H:%M:%S) ==="; }
fail() { echo "!! $*" >&2; exit 1; }

step "1/5 容器 GPU 冒烟（本地 dustynv 镜像，不下载）"
if ! docker run --rm --network host --runtime nvidia dustynv/vllm:0.8.6-r36.4-cu128-24.04 \
      python3 -c "import torch;print('  torch',torch.__version__,'cuda',torch.cuda.is_available(),'cap',torch.cuda.get_device_capability() if torch.cuda.is_available() else None)"; then
  fail "nvidia runtime 仍不可用"
fi

step "2/5 拉取基础镜像 nvcr.io/nvidia/pytorch:25.06-py3-igpu"
if docker image inspect nvcr.io/nvidia/pytorch:25.06-py3-igpu >/dev/null 2>&1; then
  echo "  已存在，跳过"
else
  docker pull nvcr.io/nvidia/pytorch:25.06-py3-igpu || fail "镜像拉取失败（检查 daemon 代理 127.0.0.1:7892）"
fi

step "3/5 构建 lelab-inference:local"
cd "$root"
# L4T 内核没有 iptable_raw 表，BuildKit 的 RUN 步骤默认走 bridge 网络会失败；
# 我们的容器本就使用 host 网络，构建也统一走 host。
docker build --network host \
  -f deploy/jetson/Dockerfile.inference \
  -t lelab-inference:local . || fail "镜像构建失败"

step "4/5 启动容器并自检（sm_87 + matmul + PolicyServer）"
docker rm -f lelab-inference >/dev/null 2>&1 || true
docker run -d --name lelab-inference \
  --runtime nvidia --network host --restart unless-stopped --shm-size 2gb \
  --cap-drop ALL \
  -e HF_HOME=/cache/huggingface -e HF_LEROBOT_HOME=/cache/lerobot -e HF_HUB_DISABLE_XET=1 \
  -v "$root/models:/models:ro" \
  -v "$HOME/.cache/huggingface:/cache/huggingface" \
  -v "$log_dir:/logs" \
  lelab-inference:local || fail "容器启动失败"

for i in $(seq 1 40); do
  sleep 3
  if docker logs lelab-inference 2>&1 | grep -q "PolicyServer 127.0.0.1:8080"; then
    break
  fi
  if ! docker ps --format '{{.Names}}' | grep -q '^lelab-inference$'; then
    docker logs --tail 40 lelab-inference || true
    fail "容器退出（见上面日志）"
  fi
done
docker logs --tail 30 lelab-inference || true

step "5/5 无硬件合成推理（不接机械臂/相机）"
if [[ "${LELAB_RUN_PROBE:-0}" == "1" ]]; then
  "$root/app/.venv/bin/python" "$root/app/deploy/train/probe-cloud-policy.py" \
    --server 127.0.0.1:8080 --policy /models/act_so101 --policy-type act \
    --actions-per-chunk 16 || fail "合成推理探针失败"
else
  echo "  跳过（设 LELAB_RUN_PROBE=1 再跑）"
fi

echo
echo "容器推理后端就绪：127.0.0.1:8080（lelab-inference）"
