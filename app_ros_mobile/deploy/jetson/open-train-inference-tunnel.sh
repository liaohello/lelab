#!/usr/bin/env bash
# Forward Jetson localhost:28081 to the train host's loopback-only PolicyServer.
set -euo pipefail

apply=0
train_host=${LELAB_TRAIN_HOST:-}
train_user=${LELAB_TRAIN_USER:-}
ssh_port=${LELAB_TRAIN_PORT:-22}
local_port=28081
remote_port=28081

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) apply=1; shift ;;
    --train-host) train_host=$2; shift 2 ;;
    --train-user) train_user=$2; shift 2 ;;
    --ssh-port) ssh_port=$2; shift 2 ;;
    --local-port) local_port=$2; shift 2 ;;
    --remote-port) remote_port=$2; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--apply] [--local-port 28081] [--remote-port 28081]"
      exit 0
      ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

[[ "$train_host" =~ ^[A-Za-z0-9.-]+$ ]] || { echo "invalid train host" >&2; exit 64; }
[[ "$train_user" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "invalid train user" >&2; exit 64; }
for port in "$ssh_port" "$local_port" "$remote_port"; do
  [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )) || {
    echo "invalid port: $port" >&2
    exit 64
  }
done

echo "Tunnel plan: 127.0.0.1:$local_port -> $train_user@$train_host:$ssh_port -> 127.0.0.1:$remote_port"
echo "RobotClient endpoint after connection: 127.0.0.1:$local_port"
if (( ! apply )); then
  echo "Dry-run only. Re-run with --apply; Ctrl+C closes the tunnel."
  exit 0
fi

exec ssh -N -T \
  -p "$ssh_port" \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L "127.0.0.1:${local_port}:127.0.0.1:${remote_port}" \
  "$train_user@$train_host"
