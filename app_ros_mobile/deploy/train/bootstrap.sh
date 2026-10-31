#!/usr/bin/env bash
# Prepare the training server. Default mode is a dry-run plan.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
deploy_dir=$(cd -- "$script_dir/.." && pwd)
apply=0
install_packages=0
sync_dependencies=0
install_service=0
enable_service=0
project_root=/home/trainer/Desktop/lelab_server

usage() {
  cat <<'EOF'
Usage: bootstrap.sh [options]

Nothing is changed unless --apply is present.

  --apply                 perform selected changes
  --project-root PATH     default: /home/trainer/Desktop/lelab_server
  --install-packages      apt install build/transfer utilities
  --sync-dependencies     run uv sync in the deployed current release
  --install-service       install loopback-only PolicyServer unit; do not start
  --enable-service        enable/start PolicyServer; implies --install-service

Do not configure the private 192.0.2.23:7890 proxy on this public host.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) apply=1; shift ;;
    --project-root) [[ $# -ge 2 ]] || { echo "missing value" >&2; exit 64; }; project_root=$2; shift 2 ;;
    --install-packages) install_packages=1; shift ;;
    --sync-dependencies) sync_dependencies=1; shift ;;
    --install-service) install_service=1; shift ;;
    --enable-service) enable_service=1; install_service=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done

[[ "$project_root" == /* ]] || { echo "--project-root must be absolute" >&2; exit 64; }
if [[ "${HTTP_PROXY:-}" == *"192.0.2.23:7890"* || "${HTTPS_PROXY:-}" == *"192.0.2.23:7890"* ]]; then
  echo "Refusing to use the private LAN proxy on the public train host" >&2
  exit 64
fi

shared=${project_root}/shared
env_target=${shared}/config/train.env
cat <<EOF
Train bootstrap plan
  mode:              $([[ $apply -eq 1 ]] && printf APPLY || printf DRY-RUN)
  project root:      $project_root
  persistent dirs:   shared/{datasets,models,outputs,huggingface,lerobot,config}
  install packages:  $install_packages
  sync dependencies: $sync_dependencies
  install service:   $install_service
  enable service:    $enable_service
  PolicyServer:      127.0.0.1:28081
EOF

if (( ! apply )); then
  echo
  echo "Dry-run only. Re-run with --apply and only the reviewed option flags."
  exit 0
fi

install -d -m 0750 \
  "$project_root" \
  "$project_root/releases" \
  "$project_root/.incoming" \
  "$shared/datasets" \
  "$shared/datasets/.incoming" \
  "$shared/datasets/.trash" \
  "$shared/models" \
  "$shared/outputs" \
  "$shared/huggingface" \
  "$shared/lerobot" \
  "$shared/config"

if [[ ! -e "$env_target" ]]; then
  install -m 0600 "$deploy_dir/env/train.env.example" "$env_target"
  echo "Created $env_target from the credential-free template; review it before service start."
else
  echo "Preserved existing $env_target"
fi

if (( install_packages )); then
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends build-essential curl ffmpeg git jq rsync
fi

if (( sync_dependencies )); then
  command -v uv >/dev/null 2>&1 || { echo "uv not found; install a reviewed uv release first" >&2; exit 69; }
  [[ -L "$project_root/current" ]] || { echo "$project_root/current release symlink is missing" >&2; exit 66; }
  uv sync --project "$project_root/current"
fi

if (( install_service )); then
  [[ "$project_root" == "/home/trainer/Desktop/lelab_server" ]] || {
    echo "The supplied unit is pinned to /home/trainer/Desktop/lelab_server; customize a copy" >&2
    exit 78
  }
  sudo install -m 0644 "$deploy_dir/systemd/lerobot-policy-server.service" \
    /etc/systemd/system/lerobot-policy-server.service
  sudo systemctl daemon-reload
  echo "Installed lerobot-policy-server.service without starting it."
fi

if (( enable_service )); then
  [[ -x "$project_root/current/.venv/bin/python" ]] || {
    echo "Expected venv missing: $project_root/current/.venv" >&2
    exit 66
  }
  sudo systemctl enable --now lerobot-policy-server.service
fi

echo "Train bootstrap completed. Re-run deploy/train/audit.sh before training or inference."
