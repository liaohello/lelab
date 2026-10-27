#!/usr/bin/env bash
# One-time GPU-container setup for the Jetson (needs sudo).
#
#   sudo bash deploy/jetson/setup-container-runtime.sh
#
# 1. registers Docker's `nvidia` runtime (nvidia-container-toolkit is already
#    installed but daemon.json was empty),
# 2. points the Docker daemon at the Jetson-local mihomo so `docker pull` can
#    reach nvcr.io (the daemon, not the shell, performs the pull),
# 3. restarts Docker and proves GPU access with the smallest local image.
#
# Safe to re-run. No containers were running when this was written.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "请用 sudo 运行：sudo bash $0" >&2; exit 77; }

proxy=${MIHOMO_PROXY:-http://127.0.0.1:7892}

echo "== 1/4 注册 nvidia runtime =="
if command -v nvidia-ctk >/dev/null; then
  nvidia-ctk runtime configure --runtime=docker
else
  echo "nvidia-ctk 不存在，请先安装 nvidia-container-toolkit" >&2
  exit 69
fi

echo "== 2/4 Docker daemon 代理（仅用于拉镜像/装依赖）-> $proxy =="
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/proxy.conf <<EOF
[Service]
Environment="HTTP_PROXY=$proxy"
Environment="HTTPS_PROXY=$proxy"
Environment="NO_PROXY=localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8"
EOF

echo "== 3/4 重启 Docker（当前无运行中容器；LeLab API 跑在 venv，不受影响）=="
systemctl daemon-reload
systemctl restart docker
sleep 3
docker info 2>/dev/null | grep -E "Runtimes|Default Runtime" || true

echo "== 4/4 GPU 冒烟测试 =="
docker run --rm --runtime nvidia ubuntu:22.04 nvidia-smi -L
echo "GPU 容器已可用 ✅"
