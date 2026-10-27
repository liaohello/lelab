#!/usr/bin/env bash
# Pull a trained policy from the training server onto the Jetson.
#
# Dry-run by default; pass --apply to transfer. The transfer is staged, so a
# dropped SSH session leaves a resumable partial directory instead of a
# half-written active model:
#
#   1. rsync --partial --append-verify into <models_root>/.incoming/<job>/<stamp>
#   2. verify every file against the remote SHA-256 manifest
#   3. move the verified tree to <models_root>/<policy>/<version>
#   4. atomically repoint <models_root>/<policy>/active
#   5. keep the previous versions for rollback
#
# Credentials never appear here: SSH uses the agent/key, and the train-server
# coordinates come from the profile (LELAB_TRAIN_* or
# deploy/config/servers.example.toml).
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: pull-model-from-train.sh --job-id ID [options]

  --job-id ID          Remote job id under <train_root>/models (required)
  --policy POLICY      Local policy folder name (default: meta policy from the job)
  --version VERSION    Local version folder (default: job id + timestamp)
  --models-root DIR    Local model root (default: $HOME/Desktop/lelab/models)
  --remote-source DIR  Explicit remote source directory (default: <train_root>/models/ID)
  --host HOST          Train server host (default: $LELAB_TRAIN_HOST)
  --port PORT          SSH port (default: $LELAB_TRAIN_PORT or 22)
  --user USER          SSH user (default: $LELAB_TRAIN_USER)
  --train-root DIR     Remote project root (default: $LELAB_TRAIN_ROOT)
  --ssh-key FILE       Private key for the deployment user (default: $LELAB_TRAIN_SSH_KEY)
  --apply              Perform the transfer (default: dry run)
  -h, --help           This text
USAGE
}

job_id=""
policy=""
version=""
models_root="${LELAB_MODEL_ROOT:-$HOME/Desktop/lelab/models}"
remote_source=""
host="${LELAB_TRAIN_HOST:-}"
port="${LELAB_TRAIN_PORT:-22}"
user="${LELAB_TRAIN_USER:-}"
train_root="${LELAB_TRAIN_ROOT:-}"
ssh_key="${LELAB_TRAIN_SSH_KEY:-}"
apply=0

while [ $# -gt 0 ]; do
  case "$1" in
    --job-id) job_id=$2; shift 2 ;;
    --policy) policy=$2; shift 2 ;;
    --version) version=$2; shift 2 ;;
    --models-root) models_root=$2; shift 2 ;;
    --remote-source) remote_source=$2; shift 2 ;;
    --host) host=$2; shift 2 ;;
    --port) port=$2; shift 2 ;;
    --user) user=$2; shift 2 ;;
    --train-root) train_root=$2; shift 2 ;;
    --ssh-key) ssh_key=$2; shift 2 ;;
    --apply) apply=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for required in job_id host user train_root; do
  value=${!required}
  if [ -z "$value" ]; then
    echo "Missing required value: $required (see --help or LELAB_TRAIN_*)" >&2
    exit 2
  fi
done

if [ "$(uname -s)" != "Linux" ]; then
  echo "This transfer must run on the Jetson/Linux host (rsync + sha256sum)." >&2
  exit 1
fi
for tool in ssh rsync sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Missing tool: $tool" >&2; exit 1; }
done

remote_source=${remote_source:-"$train_root/models/$job_id"}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
staging_root="$models_root/.incoming/$job_id/$stamp"
version=${version:-"$job_id-$stamp"}

ssh_args=(-p "$port" -o BatchMode=yes -o ConnectTimeout=10)
[ -n "$ssh_key" ] && ssh_args+=(-i "$ssh_key")
endpoint="$user@$host"

# `~` and `~/...` are expanded by the remote shell, so pass them quoted.
remote_quote() {
  case "$1" in
    "~") printf '"$HOME"' ;;
    "~/"*) printf '"$HOME"/%q' "${1#\~/}" ;;
    *) printf '%q' "$1" ;;
  esac
}
remote_q=$(remote_quote "$remote_source")

echo "Source : $endpoint:$remote_source"
echo "Target : $models_root/<policy>/$version (previous versions are kept)"
echo "Staging: $staging_root"

if [ "$apply" -eq 0 ]; then
  echo
  echo "Dry run. Commands that would run:"
  echo "  ssh ${ssh_args[*]} $endpoint test -d $remote_q"
  echo "  rsync -a --partial --append-verify -e 'ssh ${ssh_args[*]}' $endpoint:$remote_q/ $staging_root/"
  echo "  ssh ${ssh_args[*]} $endpoint 'cd $remote_source && find . -type f -print0 | sort -z | xargs -0 sha256sum' > manifest"
  echo "  (verify, then rename $staging_root -> <policy>/$version and repoint active)"
  exit 0
fi

ssh "${ssh_args[@]}" "$endpoint" "test -d $remote_q" || {
  echo "Remote model directory not found: $remote_source" >&2
  exit 1
}

mkdir -p "$staging_root" "$(dirname "$staging_root")"
rsync -a --partial --append-verify --info=progress2 \
  -e "ssh $(printf '%q ' "${ssh_args[@]}")" \
  "$endpoint:$(printf '%q' "$remote_source")/" "$staging_root/"

manifest="$staging_root/.manifest.sha256"
ssh "${ssh_args[@]}" "$endpoint" \
  "cd $(printf '%q' "$remote_source") && find . -type f ! -name '.manifest.sha256' -print0 | sort -z | xargs -0 sha256sum" \
  > "$manifest"

( cd "$staging_root" && sha256sum --check --quiet "$manifest" ) || {
  echo "Checksum verification failed; keeping $staging_root for inspection." >&2
  exit 1
}

# policy name: explicit flag, else the job's config.json `type`, else "policy".
if [ -z "$policy" ]; then
  policy=$(python3 - "$staging_root" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
for candidate in (root / "config.json", root / "pretrained_model" / "config.json"):
    if candidate.is_file():
        try:
            print(json.loads(candidate.read_text()).get("type") or "policy")
        except Exception:
            print("policy")
        break
else:
    print("policy")
PY
)
fi

final_dir="$models_root/$policy/$version"
mkdir -p "$models_root/$policy"
if [ -e "$final_dir" ]; then
  echo "Refusing to overwrite existing version directory: $final_dir" >&2
  exit 1
fi
mv "$staging_root" "$final_dir"

# Atomic `active` switch: create the new link next to it, then rename over.
ln -sfn "$version" "$models_root/$policy/.active.tmp"
mv -Tf "$models_root/$policy/.active.tmp" "$models_root/$policy/active"

echo "Deployed: $final_dir"
echo "Active  : $models_root/$policy/active -> $version"
