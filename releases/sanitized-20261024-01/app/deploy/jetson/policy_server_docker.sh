#!/usr/bin/env bash
# Start/stop the isolated Jetson PolicyServer container.
#
# The container is named explicitly and only that name is ever stopped.  It
# runs on the host network so the local workstation can reach the LAN port;
# the gRPC protocol is unencrypted, so keep this on the trusted LAN.
set -euo pipefail

HOST="${LELAB_JETSON_HOST:-192.168.31.204}"
SSH_PORT="${LELAB_JETSON_PORT:-22}"
USER_NAME="${LELAB_JETSON_USER:-nvidia}"
KEY="${LELAB_JETSON_SSH_KEY:-$HOME/.ssh/lelab-jetson}"
ROOT="${LELAB_JETSON_ROOT:-/home/nvidia/jetson-policy-server-20261031}"
POLICY_PORT="${LELAB_JETSON_POLICY_PORT:-28081}"
IMAGE="${LELAB_JETSON_POLICY_IMAGE:-lelab-policy-server:20261031}"
NAME="${LELAB_JETSON_POLICY_NAME:-lelab-policy-server-20261031}"
CHECKPOINT="${LELAB_JETSON_CHECKPOINT:-/home/nvidia/Desktop/lelab/models/act_local_red_bowl_20260919_152702_2026-09-19_19-55-49_6cf4e08d/checkpoints/010000/pretrained_model}"
MODELS_ROOT="${LELAB_JETSON_MODELS_ROOT:-/home/nvidia/Desktop/lelab/models}"

SSH=(ssh -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes -p "$SSH_PORT" "$USER_NAME@$HOST")

usage() {
  echo "usage: $0 {start|stop|status}" >&2
  exit 2
}

[ $# -eq 1 ] || usage

case "$1" in
  start)
    "${SSH[@]}" bash -s -- "$ROOT" "$POLICY_PORT" "$IMAGE" "$NAME" "$CHECKPOINT" "$MODELS_ROOT" <<'REMOTE'
set -eu
root=$1
port=$2
image=$3
name=$4
checkpoint=$5
models_root=$6
mkdir -p "$root/logs"
if docker inspect "$name" >/dev/null 2>&1; then
  if [ "$(docker inspect -f '{{.State.Running}}' "$name")" = "true" ]; then
    echo "ALREADY_RUNNING $(docker inspect -f '{{.Id}}' "$name" | cut -c1-12)"
    exit 0
  fi
  docker rm "$name" >/dev/null
fi
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$port$"; then
  echo "PORT_IN_USE $port"
  exit 3
fi
cid=$(docker run -d --name "$name" --restart=no --network host --ipc=host \
  --runtime nvidia \
  -v "$root:$root" \
  -v "$models_root:$models_root:ro" \
  -e PYTHONPATH="$root/lerobot/src" \
  -w "$root" \
  --entrypoint python "$image" \
  -m lerobot.async_inference.policy_server \
    --host=0.0.0.0 --port="$port" --fps=30 \
    --inference_latency=0.0333333333 --obs_queue_timeout=2)
printf '{"container":%s,"container_name":"%s","image":"%s","checkpoint":"%s","port":%s,"started_at":%s}\n' \
  "\"$cid\"" "$name" "$image" "$checkpoint" "$port" "$(date +%s)" > "$root/logs/container.json"
for i in $(seq 1 60); do
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$port$"; then
    echo "STARTED $cid"
    exit 0
  fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" != "true" ]; then
    docker logs --tail 40 "$name" 2>&1 || true
    echo "CONTAINER_EXITED"
    exit 4
  fi
  sleep 0.5
done
echo "PORT_TIMEOUT"
exit 5
REMOTE
    ;;
  stop)
    "${SSH[@]}" bash -s -- "$NAME" <<'REMOTE'
set -eu
name=$1
if ! docker inspect "$name" >/dev/null 2>&1; then
  echo "ALREADY_STOPPED"
  exit 0
fi
docker stop "$name" >/dev/null
docker rm "$name" >/dev/null
echo "STOPPED $name"
REMOTE
    ;;
  status)
    "${SSH[@]}" bash -s -- "$NAME" "$POLICY_PORT" <<'REMOTE'
set -eu
name=$1
port=$2
if ! docker inspect "$name" >/dev/null 2>&1; then
  echo "STATE=absent"
else
  echo "STATE=$(docker inspect -f '{{.State.Status}}' "$name")"
  echo "CONTAINER=$(docker inspect -f '{{.Id}}' "$name" | cut -c1-12)"
  echo "IMAGE=$(docker inspect -f '{{.Config.Image}}' "$name")"
  echo "STARTED=$(docker inspect -f '{{.State.StartedAt}}' "$name")"
  echo "LOG_TAIL_BEGIN"
  docker logs --tail 20 "$name" 2>&1 || true
  echo "LOG_TAIL_END"
fi
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$port$"; then
  echo "LISTENING=$port"
else
  echo "LISTENING=none"
fi
REMOTE
    ;;
  *)
    usage
    ;;
esac
