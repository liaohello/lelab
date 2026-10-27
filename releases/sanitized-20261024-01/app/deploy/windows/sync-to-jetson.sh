#!/usr/bin/env bash
# One-shot deploy to the Jetson (run from the repo root on Windows/WSL).
#
#   bash deploy/windows/sync-to-jetson.sh [user@host]
#
# Syncs lelab/, tests/ and the built frontend, then restarts the API.
set -euo pipefail

target=${1:-${LELAB_JETSON_TARGET:-}}
[[ -n "$target" ]] || { echo "Pass user@host or set LELAB_JETSON_TARGET" >&2; exit 64; }
key=${LELAB_JETSON_KEY:-$HOME/.ssh/id_ed25519_lelab}
remote_root=${LELAB_JETSON_ROOT:-/home/nvidia/Desktop/lelab}
ssh_cmd=(ssh -i "$key" -o ConnectTimeout=8)
rsync_ssh="ssh -i $key -o ConnectTimeout=8"

[[ -f frontend/dist/index.html ]] || { echo "frontend/dist missing; run: (cd frontend && npm run build)" >&2; exit 66; }

echo "== target: $target ($remote_root)"
"${ssh_cmd[@]}" "$target" 'echo reachable'
rsync -az --exclude '__pycache__' --exclude '*.pyc' -e "$rsync_ssh" lelab/  "$target:$remote_root/app/lelab/"
rsync -az --exclude '__pycache__' -e "$rsync_ssh" tests/  "$target:$remote_root/app/tests/"
rsync -az --exclude '__pycache__' -e "$rsync_ssh" deploy/ "$target:$remote_root/deploy/"
rsync -az --delete -e "$rsync_ssh" frontend/dist/ "$target:$remote_root/app/frontend/dist/"
"${ssh_cmd[@]}" "$target" 'bash ~/Desktop/lelab/restart-api.sh'
echo "== deployed; run: bash deploy/train/acceptance-check.sh (on the Jetson)"
