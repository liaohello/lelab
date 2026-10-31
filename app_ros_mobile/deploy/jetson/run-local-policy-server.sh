#!/usr/bin/env bash
# Run the preferred Jetson-local PolicyServer without granting robot/camera device access.
set -euo pipefail

project_root=${LELAB_PROJECT_ROOT:-/home/nvidia/Desktop/lelab}
shared=${LELAB_SHARED_ROOT:-${project_root}/shared}
image=${LELAB_JETSON_IMAGE:-lelab-jetson:local}
host=${LEROBOT_LOCAL_POLICY_HOST:-127.0.0.1}
port=${LEROBOT_LOCAL_POLICY_PORT:-8080}
fps=${LEROBOT_POLICY_FPS:-30}
latency=${LEROBOT_POLICY_INFERENCE_LATENCY:-0.033}
timeout=${LEROBOT_POLICY_OBS_QUEUE_TIMEOUT:-1}

[[ "$host" == "127.0.0.1" ]] || { echo "local PolicyServer must bind to 127.0.0.1" >&2; exit 64; }
for number in "$port" "$fps"; do
  [[ "$number" =~ ^[0-9]+$ ]] || { echo "invalid numeric configuration: $number" >&2; exit 64; }
done
[[ -r "$shared/config/container-image.approved" ]] || { echo "container image is not approved" >&2; exit 78; }
approved_image=$(sed -n 's/^image=//p' "$shared/config/container-image.approved" | head -n 1)
[[ "$approved_image" == "$image" ]] || { echo "configured image does not match approval" >&2; exit 78; }

exec docker run --rm --name lerobot-local-policy \
  --runtime nvidia \
  --network host \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=1g \
  --mount "type=bind,src=${shared},dst=${shared}" \
  --env "HF_HOME=${HF_HOME:-${shared}/huggingface}" \
  --env "HF_LEROBOT_HOME=${HF_LEROBOT_HOME:-${shared}/lerobot}" \
  --entrypoint /opt/lelab-venv/bin/python \
  "$image" -m lerobot.async_inference.policy_server \
  --host="$host" \
  --port="$port" \
  --fps="$fps" \
  --inference_latency="$latency" \
  --obs_queue_timeout="$timeout"
