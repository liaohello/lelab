#!/usr/bin/env bash
# Run the existing OpenPI model and LIBERO image in an isolated, owned workspace.
set -euo pipefail

root=${LELAB_LIBERO_ROOT:-"$HOME/Desktop/lelab_server/libero"}
if [[ -f "$root/libero.env" ]]; then
  # Credential-free image pins and paths written by this deployment.
  source "$root/libero.env"
fi
instance=${LELAB_INSTANCE_ID:-windows-standby}
gpu=${LELAB_LIBERO_GPU:-2}
server_image=${LELAB_LIBERO_SERVER_IMAGE:-openpi-server:latest}
client_image=${LELAB_LIBERO_CLIENT_IMAGE:-openpi-client:latest}
assets=${LELAB_LIBERO_ASSETS:-/data/openpi/openpi_deploy_package/openpi_assets}
checkpoint=${LELAB_LIBERO_CHECKPOINT:-"$assets/openpi-assets/checkpoints/pi05_libero"}
network=lelab-libero
server=lelab-libero-policy
mode=${1:-status}
if (($#)); then shift; fi

python3 - "$root" "$instance" <<'PY'
import json, sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()
marker=root / '.lelab-libero.json'
if not marker.is_file() or json.loads(marker.read_text()).get('instance_id') != sys.argv[2]:
    raise SystemExit('LIBERO workspace marker is missing or belongs to another instance')
PY

owned_container() {
  [[ $(docker inspect --format '{{index .Config.Labels "lelab.instance_id"}}' "$1") == "$instance" ]] \
    || { echo "Refusing to change container owned by another instance: $1" >&2; exit 1; }
}

client_run() {
  docker run --rm --name "$1" \
    --label "lelab.instance_id=$instance" --label lelab.component=libero \
    --network "$2" --gpus "device=$gpu" --shm-size=2g \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics \
    -e MUJOCO_GL=egl -e PYOPENGL_PLATFORM=egl -e PYNPUT_BACKEND=dummy \
    -e HOME=/workspace/home -e NUMBA_CACHE_DIR=/workspace/cache/numba \
    -v "$root:/workspace" --entrypoint python "$client_image" "${@:3}"
}

case "$mode" in
  probe)
    client_run lelab-libero-env-probe none /workspace/scripts/probe_libero.py \
      --output /workspace/results/environment "$@"
    ;;
  start)
    if docker container inspect "$server" >/dev/null 2>&1; then
      owned_container "$server"
      [[ $(docker inspect --format '{{.State.Running}}' "$server") == true ]] \
        || { echo "Owned server is stopped; run stop before starting a new session" >&2; exit 1; }
      echo "Policy server already running"
      exit 0
    fi
    if docker network inspect "$network" >/dev/null 2>&1; then
      [[ $(docker network inspect --format '{{index .Labels "lelab.instance_id"}}' "$network") == "$instance" ]] \
        || { echo "Network belongs to another instance" >&2; exit 1; }
    else
      docker network create --internal --label "lelab.instance_id=$instance" \
        --label lelab.component=libero "$network" >/dev/null
    fi
    [[ -d "$checkpoint/params" && -f "$checkpoint/assets/physical-intelligence/libero/norm_stats.json" ]] \
      || { echo "Incomplete pi05_libero checkpoint: $checkpoint" >&2; exit 1; }
    mkdir -p "$root/cache/big_vision" "$root/home" "$root/results"
    if [[ ! -f "$root/cache/big_vision/paligemma_tokenizer.model" ]]; then
      cp "$assets/big_vision/paligemma_tokenizer.model" "$root/cache/big_vision/"
    fi
    free_mb=$(nvidia-smi -i "$gpu" --query-gpu=memory.free --format=csv,noheader,nounits)
    (( free_mb >= 18000 )) || { echo "GPU $gpu has less than 18 GB free" >&2; exit 1; }
    docker run -d --name "$server" \
      --label "lelab.instance_id=$instance" --label lelab.component=libero \
      --network "$network" --gpus "device=$gpu" --shm-size=2g \
      -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
      -e XLA_PYTHON_CLIENT_PREALLOCATE=false -e OPENPI_DATA_HOME=/workspace/cache \
      -e HOME=/workspace/home \
      -v "$checkpoint:/checkpoint:ro" -v "$root:/workspace" \
      -w /opt/openpi --entrypoint /opt/openpi/.venv/bin/python "$server_image" \
      scripts/serve_policy.py --env LIBERO --port 8000 \
      policy:checkpoint --policy.config pi05_libero --policy.dir /checkpoint
    ;;
  eval)
    owned_container "$server"
    [[ $(docker inspect --format '{{.State.Running}}' "$server") == true ]] \
      || { echo "Policy server is not running" >&2; exit 1; }
    docker logs "$server" 2>&1 | grep -F 'server listening' >/dev/null \
      || { echo "Policy server is still loading; inspect docker logs before evaluating" >&2; exit 1; }
    result=${LELAB_LIBERO_RESULT:-"eval-$(date -u +%Y%m%dT%H%M%SZ)"}
    [[ "$result" =~ ^[A-Za-z0-9._-]+$ && "$result" != . && "$result" != .. ]] \
      || { echo "Invalid result directory name" >&2; exit 1; }
    if [[ -e "$root/results/$result/evaluation.json" ]]; then
      echo "Result already exists; choose another LELAB_LIBERO_RESULT" >&2
      exit 1
    fi
    client_run "lelab-libero-eval-$result" "$network" \
      /workspace/scripts/evaluate_openpi_libero.py \
      --output "/workspace/results/$result" "$@"
    ;;
  stop)
    if docker container inspect "$server" >/dev/null 2>&1; then
      owned_container "$server"
      docker logs "$server" >"$root/results/policy-server-last.log" 2>&1
      docker stop -t 20 "$server" >/dev/null
      docker rm "$server" >/dev/null
    fi
    ;;
  status)
    docker ps -a --filter "label=lelab.instance_id=$instance" \
      --filter label=lelab.component=libero --format '{{.Names}} {{.Status}} {{.Image}}'
    nvidia-smi -i "$gpu" --query-gpu=index,memory.free,utilization.gpu --format=csv,noheader
    ;;
  *) echo "Usage: $0 {probe|start|eval|stop|status} [evaluation arguments]" >&2; exit 2 ;;
esac
