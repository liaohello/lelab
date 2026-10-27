#!/usr/bin/env bash
# Prepare Jetson directories and optional runtime pieces. Default mode is a dry-run plan.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
deploy_dir=$(cd -- "$script_dir/.." && pwd)
apply=0
install_packages=0
add_device_groups=0
pull_base_image=0
container_compatible=0
install_service=0
enable_service=0
proxy_url=""
project_root=/home/nvidia/Desktop/lelab
app_image=lelab-jetson:local
base_image=nvcr.io/nvidia/pytorch:25.04-py3-igpu

usage() {
  cat <<'EOF'
Usage: bootstrap.sh [options]

Nothing is changed unless --apply is present.

  --apply                       perform the selected changes
  --project-root PATH           default: /home/nvidia/Desktop/lelab
  --proxy-url URL               optional Jetson-only download proxy
  --install-packages            apt install audit/build utilities
  --add-device-groups           add current user to dialout, video, docker
  --pull-base-image             pull the candidate NVIDIA base image
  --container-compatible        explicit acknowledgement after JetPack matrix review
  --app-image IMAGE             image recorded as approved (default: lelab-jetson:local)
  --install-service             install unit and an environment template (does not start it)
  --enable-service              enable/start the unit; implies --install-service

The base image is only a candidate. --pull-base-image requires both --apply and
--container-compatible. Building the local app image is a separate reviewed step.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) apply=1; shift ;;
    --project-root) [[ $# -ge 2 ]] || { echo "missing value" >&2; exit 64; }; project_root=$2; shift 2 ;;
    --proxy-url) [[ $# -ge 2 ]] || { echo "missing value" >&2; exit 64; }; proxy_url=$2; shift 2 ;;
    --install-packages) install_packages=1; shift ;;
    --add-device-groups) add_device_groups=1; shift ;;
    --pull-base-image) pull_base_image=1; shift ;;
    --container-compatible) container_compatible=1; shift ;;
    --app-image) [[ $# -ge 2 ]] || { echo "missing value" >&2; exit 64; }; app_image=$2; shift 2 ;;
    --install-service) install_service=1; shift ;;
    --enable-service) enable_service=1; install_service=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done

[[ "$project_root" == /* ]] || { echo "--project-root must be absolute" >&2; exit 64; }
[[ "$app_image" =~ ^[A-Za-z0-9._/:@-]+$ ]] || { echo "invalid --app-image" >&2; exit 64; }
if [[ -n "$proxy_url" && ! "$proxy_url" =~ ^https?://[A-Za-z0-9._:-]+/?$ ]]; then
  echo "invalid --proxy-url" >&2
  exit 64
fi
if (( pull_base_image )) && (( ! container_compatible )); then
  echo "--pull-base-image requires --container-compatible after a manual compatibility review" >&2
  exit 64
fi

shared=${project_root}/shared
env_target=${shared}/config/jetson.env

cat <<EOF
Jetson bootstrap plan
  mode:                $([[ $apply -eq 1 ]] && printf APPLY || printf DRY-RUN)
  project root:        $project_root
  persistent dirs:     shared/{datasets,calibration,outputs,huggingface,lerobot,models,config}
  install packages:    $install_packages
  add device groups:   $add_device_groups
  pull candidate base: $pull_base_image ($base_image)
  image approval:      $container_compatible ($app_image)
  install service:     $install_service
  enable service:      $enable_service
  proxy:               ${proxy_url:-disabled}
EOF

if (( ! apply )); then
  echo
  echo "Dry-run only. Re-run with --apply and only the reviewed option flags."
  exit 0
fi

if [[ -n "$proxy_url" ]]; then
  export HTTP_PROXY=$proxy_url HTTPS_PROXY=$proxy_url http_proxy=$proxy_url https_proxy=$proxy_url
fi

install -d -m 0750 \
  "$project_root" \
  "$project_root/releases" \
  "$project_root/.incoming" \
  "$shared/datasets" \
  "$shared/calibration" \
  "$shared/outputs" \
  "$shared/huggingface" \
  "$shared/lerobot" \
  "$shared/models" \
  "$shared/config"

if [[ ! -e "$env_target" ]]; then
  install -m 0600 "$deploy_dir/env/jetson.env.example" "$env_target"
  echo "Created $env_target from the credential-free template; review it before service start."
else
  echo "Preserved existing $env_target"
fi

if (( install_packages )); then
  sudo -E apt-get update
  sudo -E apt-get install -y --no-install-recommends curl ffmpeg git jq rsync v4l-utils
fi

if (( add_device_groups )); then
  sudo usermod -aG dialout,video,docker "$(id -un)"
  echo "Group membership changed; log out and back in before accessing devices."
fi

if (( pull_base_image )); then
  command -v docker >/dev/null 2>&1 || { echo "docker not found" >&2; exit 69; }
  echo "Compatibility acknowledgement recorded by $(id -un) at $(date -u +%FT%TZ)"
  echo "Docker daemon proxy configuration is separate; shell proxy variables may not affect docker pull."
  docker pull "$base_image"
fi

if (( container_compatible )); then
  {
    printf 'image=%s\n' "$app_image"
    printf 'base_image=%s\n' "$base_image"
    printf 'approved_by=%s\n' "$(id -un)"
    printf 'approved_at=%s\n' "$(date -u +%FT%TZ)"
    printf 'note=%s\n' 'Operator confirmed JetPack/L4T compatibility against NVIDIA documentation.'
  } >"$shared/config/container-image.approved"
  chmod 0600 "$shared/config/container-image.approved"
fi

if (( install_service )); then
  [[ "$project_root" == "/home/nvidia/Desktop/lelab" ]] || {
    echo "The supplied systemd unit is pinned to /home/nvidia/Desktop/lelab; customize a copy for $project_root" >&2
    exit 78
  }
  sudo install -m 0644 "$deploy_dir/systemd/lelab-api.service" /etc/systemd/system/lelab-api.service
  sudo install -m 0644 "$deploy_dir/systemd/lerobot-local-policy-server.service" \
    /etc/systemd/system/lerobot-local-policy-server.service
  sudo systemctl daemon-reload
  echo "Installed LeLab API and local PolicyServer units without starting them."
fi

if (( enable_service )); then
  [[ -L "$project_root/current" ]] || { echo "$project_root/current release symlink is missing" >&2; exit 66; }
  sudo systemctl enable --now lelab-api.service lerobot-local-policy-server.service
fi

echo "Jetson bootstrap completed. Re-run deploy/jetson/audit.sh and inspect the environment file."
