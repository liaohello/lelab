#!/usr/bin/env bash
# Stage a LeRobot policy checkpoint on the training server.
#
# The train host cannot reach huggingface.co directly; hf-mirror.com is used
# unless HF_ENDPOINT (or --endpoint) says otherwise. To use Mihomo instead,
# open deploy/windows/open-train-proxy-tunnel.ps1 on Windows and run this
# script with HTTP(S)_PROXY=http://127.0.0.1:17890.
#
#   bash fetch-pretrained-model.sh                        # l-e-n/act_so101 -> models/pretrained/act_so101
#   bash fetch-pretrained-model.sh --repo lerobot/act_aloha_sim_transfer_cube_human --dest models/pretrained/act_aloha
set -euo pipefail

repo=l-e-n/act_so101
dest=
endpoint=${HF_ENDPOINT:-https://hf-mirror.com}
project_root=${LELAB_TRAIN_ROOT:-$HOME/Desktop/lelab_server}
python=${LELAB_TRAIN_PYTHON:-$project_root/app/.venv/bin/python}

usage() {
  cat <<'EOF'
Usage: fetch-pretrained-model.sh [options]

  --repo REPO_ID        Hugging Face model repo (default: l-e-n/act_so101)
  --dest PATH           Destination dir (default: models/pretrained/<name>)
  --endpoint URL        HF endpoint (default: $HF_ENDPOINT or https://hf-mirror.com)
  --root PATH           Train-server root (default: $LELAB_TRAIN_ROOT or ~/Desktop/lelab_server)
  --python PATH         Interpreter with huggingface_hub installed
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo=$2; shift 2 ;;
    --dest) dest=$2; shift 2 ;;
    --endpoint) endpoint=$2; shift 2 ;;
    --root) project_root=$2; shift 2 ;;
    --python) python=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done

[[ "$repo" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || { echo "invalid --repo: $repo" >&2; exit 64; }
name=${repo##*/}
if [[ -z "$dest" ]]; then
  dest="$project_root/models/pretrained/$name"
elif [[ "$dest" != /* ]]; then
  dest="$project_root/$dest"
fi
[[ -x "$python" ]] || { echo "interpreter not found: $python" >&2; exit 69; }

mkdir -p "$dest"
echo "repo:     $repo"
echo "dest:     $dest"
echo "endpoint: $endpoint"
echo "python:   $python"

HF_ENDPOINT="$endpoint" HF_HUB_DISABLE_XET=1 "$python" - "$repo" "$dest" <<'PY'
import sys

from huggingface_hub import snapshot_download

repo_id, dest = sys.argv[1], sys.argv[2]
path = snapshot_download(repo_id=repo_id, repo_type="model", local_dir=dest)
print(f"staged: {path}")
PY
