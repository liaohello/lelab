#!/usr/bin/env bash
# Runtime wrapper used by lelab-api.service. It grants access only to explicitly listed devices.
set -euo pipefail

runtime_mode=${LELAB_RUNTIME_MODE:-container}
project_root=${LELAB_PROJECT_ROOT:-/home/nvidia/Desktop/lelab}
current=${LELAB_CURRENT:-${project_root}/current}
shared=${LELAB_SHARED_ROOT:-${project_root}/shared}
bind_host=${LELAB_BIND_HOST:-127.0.0.1}
port=${LELAB_PORT:-8000}

[[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )) || {
  echo "Invalid LELAB_PORT: $port" >&2
  exit 64
}
[[ "$bind_host" == "127.0.0.1" || "$bind_host" == "0.0.0.0" ]] || {
  echo "LELAB_BIND_HOST must be 127.0.0.1 or 0.0.0.0" >&2
  exit 64
}
[[ -d "$current" ]] || { echo "Current release not found: $current" >&2; exit 66; }
mkdir -p "$shared"

if [[ "$runtime_mode" == "native" ]]; then
  venv=${LELAB_NATIVE_VENV:-${shared}/venv}
  [[ -x "$venv/bin/python" ]] || { echo "Native venv missing: $venv" >&2; exit 66; }
  cd "$current"
  exec "$venv/bin/python" -m uvicorn lelab.server:app --host "$bind_host" --port "$port"
fi

[[ "$runtime_mode" == "container" ]] || { echo "Unknown LELAB_RUNTIME_MODE: $runtime_mode" >&2; exit 64; }
command -v docker >/dev/null 2>&1 || { echo "docker is required for container mode" >&2; exit 69; }

image=${LELAB_JETSON_IMAGE:-lelab-jetson:local}
approval_file=${shared}/config/container-image.approved
[[ -r "$approval_file" ]] || {
  echo "Container compatibility approval missing: $approval_file" >&2
  echo "Run the audit, verify NVIDIA's JetPack matrix, then use bootstrap.sh --container-compatible." >&2
  exit 78
}
approved_image=$(sed -n 's/^image=//p' "$approval_file" | head -n 1)
[[ "$approved_image" == "$image" ]] || {
  echo "Configured image '$image' does not match approved image '$approved_image'" >&2
  exit 78
}

docker_args=(
  run --rm --name lelab-api
  --runtime nvidia
  --network host
  --read-only
  --tmpfs /tmp:rw,nosuid,nodev,size=1g
  --mount "type=bind,src=${current},dst=/opt/src/lelab,readonly"
  --mount "type=bind,src=${shared},dst=${shared}"
  --workdir /opt/src/lelab
  --env "HF_HOME=${HF_HOME:-${shared}/huggingface}"
  --env "HF_LEROBOT_HOME=${HF_LEROBOT_HOME:-${shared}/lerobot}"
  --env "HF_LEROBOT_CALIBRATION=${HF_LEROBOT_CALIBRATION:-${shared}/calibration}"
  --env "LELAB_DATASET_ROOT=${LELAB_DATASET_ROOT:-${shared}/datasets}"
  --env "LELAB_OUTPUT_ROOT=${LELAB_OUTPUT_ROOT:-${shared}/outputs}"
)

append_devices() {
  local csv=$1
  local kind=$2
  local device
  IFS=',' read -r -a devices <<<"$csv"
  for device in "${devices[@]}"; do
    [[ -n "$device" ]] || continue
    [[ "$device" =~ ^/dev/[A-Za-z0-9._/+:-]+$ ]] || {
      echo "Invalid $kind device path: $device" >&2
      exit 64
    }
    [[ -e "$device" ]] || { echo "$kind device does not exist: $device" >&2; exit 66; }
    docker_args+=(--device "$device:$device")
  done
}

append_devices "${LELAB_SERIAL_DEVICES:-}" "serial"
append_devices "${LELAB_VIDEO_DEVICES:-}" "video"

exec docker "${docker_args[@]}" "$image" lelab.server:app --host "$bind_host" --port "$port"
