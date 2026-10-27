#!/usr/bin/env bash
# Stage, checksum, and atomically publish one Jetson dataset to the train host.
# Default mode is dry-run. Authentication is SSH-key-only; this script accepts no password.
set -euo pipefail

apply=0
replace=0
repo_id=""
source_path=""
dataset_root=${LELAB_DATASET_ROOT:-/home/nvidia/Desktop/lelab/shared/datasets}
remote_host=${LELAB_TRAIN_HOST:-}
remote_user=${LELAB_TRAIN_USER:-}
remote_port=${LELAB_TRAIN_PORT:-22}
remote_root=${LELAB_TRAIN_DATASET_ROOT:-}
env_file=""

usage() {
  cat <<'EOF'
Usage: upload-dataset-to-train.sh --repo-id OWNER/NAME [options]

Nothing is transferred unless --apply is present.

  --apply                    transfer, verify, and atomically publish
  --replace                  preserve an existing target under .trash before replacement
  --repo-id OWNER/NAME       logical LeRobot dataset id (required)
  --source PATH              dataset directory (default: $LELAB_DATASET_ROOT/OWNER/NAME)
  --dataset-root PATH        local Jetson dataset root
  --remote-host HOST         default: 192.0.2.10
  --remote-user USER         default: trainer
  --remote-port PORT         default: 22
  --remote-root PATH         default train dataset root
  --env-file PATH            source a trusted deployment environment file

The train host must already be present in known_hosts. Passwords are never read or stored.
EOF
}

# Load an environment file first when explicitly requested, without accepting positional data from it.
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  if [[ "${args[$i]}" == "--env-file" ]]; then
    (( i + 1 < ${#args[@]} )) || { echo "--env-file requires a value" >&2; exit 64; }
    env_file=${args[$((i + 1))]}
    [[ -r "$env_file" ]] || { echo "cannot read env file: $env_file" >&2; exit 66; }
    # shellcheck disable=SC1090
    . "$env_file"
    dataset_root=${LELAB_DATASET_ROOT:-$dataset_root}
  fi
done

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) apply=1; shift ;;
    --replace) replace=1; shift ;;
    --repo-id) [[ $# -ge 2 ]] || { echo "missing value" >&2; exit 64; }; repo_id=$2; shift 2 ;;
    --source) [[ $# -ge 2 ]] || { echo "missing value" >&2; exit 64; }; source_path=$2; shift 2 ;;
    --dataset-root) [[ $# -ge 2 ]] || { echo "missing value" >&2; exit 64; }; dataset_root=$2; shift 2 ;;
    --remote-host) [[ $# -ge 2 ]] || { echo "missing value" >&2; exit 64; }; remote_host=$2; shift 2 ;;
    --remote-user) [[ $# -ge 2 ]] || { echo "missing value" >&2; exit 64; }; remote_user=$2; shift 2 ;;
    --remote-port) [[ $# -ge 2 ]] || { echo "missing value" >&2; exit 64; }; remote_port=$2; shift 2 ;;
    --remote-root) [[ $# -ge 2 ]] || { echo "missing value" >&2; exit 64; }; remote_root=$2; shift 2 ;;
    --env-file) shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done

[[ "$repo_id" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || {
  echo "--repo-id must be OWNER/NAME using letters, digits, dot, underscore, or dash" >&2
  exit 64
}
[[ "$remote_host" =~ ^[A-Za-z0-9.-]+$ ]] || { echo "invalid remote host" >&2; exit 64; }
[[ "$remote_user" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "invalid remote user" >&2; exit 64; }
[[ "$remote_port" =~ ^[0-9]+$ ]] && (( remote_port >= 1 && remote_port <= 65535 )) || {
  echo "invalid remote port" >&2
  exit 64
}
[[ "$remote_root" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
  echo "remote root must be an absolute path without spaces" >&2
  exit 64
}

if [[ -z "$source_path" ]]; then
  source_path=${dataset_root%/}/${repo_id}
fi
source_path=${source_path%/}

printf 'Dataset transfer plan\n'
printf '  mode:         %s\n' "$([[ $apply -eq 1 ]] && printf APPLY || printf DRY-RUN)"
printf '  source:       %s\n' "$source_path"
printf '  repo id:      %s\n' "$repo_id"
printf '  destination:  %s@%s:%s/%s\n' "$remote_user" "$remote_host" "$remote_root" "$repo_id"
printf '  ssh port:     %s\n' "$remote_port"
printf '  replace:      %s (existing data is moved to .trash, never deleted)\n' "$replace"

if (( ! apply )); then
  echo "Dry-run only. Re-run with --apply after reviewing the paths."
  exit 0
fi

[[ -d "$source_path" ]] || { echo "dataset directory not found: $source_path" >&2; exit 66; }
[[ -f "$source_path/meta/info.json" ]] || {
  echo "missing LeRobot metadata: $source_path/meta/info.json" >&2
  exit 65
}
for tool in ssh scp rsync sha256sum find sort python3; do
  command -v "$tool" >/dev/null 2>&1 || { echo "required tool not found: $tool" >&2; exit 69; }
done

safe_repo=${repo_id//\//__}
transfer_id=${safe_repo}-$(date -u +%Y%m%dT%H%M%SZ)-$$
remote_stage=${remote_root}/.incoming/${transfer_id}
remote_final=${remote_root}/${repo_id}
tmp_dir=$(mktemp -d)
trap 'rm -rf -- "$tmp_dir"' EXIT
checksums=${tmp_dir}/checksums.sha256
metadata=${tmp_dir}/transfer.json

(
  cd "$source_path"
  find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum
) >"$checksums"

file_count=$(wc -l <"$checksums" | tr -d ' ')
total_bytes=$(python3 - "$source_path" <<'PY'
import os
import sys

total = 0
for base, _, files in os.walk(sys.argv[1]):
    for name in files:
        total += os.path.getsize(os.path.join(base, name))
print(total)
PY
)
checksum_manifest_sha256=$(sha256sum "$checksums" | awk '{print $1}')
python3 - "$metadata" "$repo_id" "$transfer_id" "$file_count" "$total_bytes" "$checksum_manifest_sha256" <<'PY'
import datetime
import json
import socket
import sys

path, repo_id, transfer_id, file_count, total_bytes, manifest_sha = sys.argv[1:]
document = {
    "schema_version": 1,
    "repo_id": repo_id,
    "transfer_id": transfer_id,
    "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
    "source_host": socket.gethostname(),
    "file_count": int(file_count),
    "total_bytes": int(total_bytes),
    "checksum_algorithm": "sha256",
    "checksum_manifest_sha256": manifest_sha,
}
with open(path, "w", encoding="utf-8") as stream:
    json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
    stream.write("\n")
PY

ssh_target=${remote_user}@${remote_host}
ssh_options=(-T -p "$remote_port" -o BatchMode=yes -o StrictHostKeyChecking=yes)
remote_prepare='set -euo pipefail
root=$1
repo=$2
stage=$3
replace=$4
final="$root/$repo"
if [[ -e "$final" && "$replace" != 1 ]]; then
  echo "target already exists: $final (use --replace to preserve it under .trash)" >&2
  exit 73
fi
mkdir -p "$stage/payload/.transfer" "$root/.trash"
printf "%s\n" "$stage"'
printf '%s\n' "$remote_prepare" | ssh "${ssh_options[@]}" "$ssh_target" \
  bash -s -- "$remote_root" "$repo_id" "$remote_stage" "$replace"

rsync_ssh="ssh -p $remote_port -o BatchMode=yes -o StrictHostKeyChecking=yes"
rsync -a --partial --checksum --human-readable --info=progress2 \
  -e "$rsync_ssh" -- "$source_path/" "$ssh_target:$remote_stage/payload/"
scp -P "$remote_port" -o BatchMode=yes -o StrictHostKeyChecking=yes -- \
  "$checksums" "$metadata" "$ssh_target:$remote_stage/payload/.transfer/"

remote_finalize='set -euo pipefail
root=$1
repo=$2
stage=$3
replace=$4
transfer_id=$5
expected_manifest_sha=$6
payload="$stage/payload"
final="$root/$repo"
cd "$payload"
actual_manifest_sha=$(sha256sum .transfer/checksums.sha256 | awk "{print \$1}")
[[ "$actual_manifest_sha" == "$expected_manifest_sha" ]] || { echo "manifest checksum mismatch" >&2; exit 65; }
sha256sum -c .transfer/checksums.sha256
if [[ -e "$final" ]]; then
  [[ "$replace" == 1 ]] || { echo "target appeared during transfer" >&2; exit 73; }
  trash="$root/.trash/$transfer_id/$repo"
  mkdir -p "$(dirname "$trash")"
  mv -- "$final" "$trash"
fi
mkdir -p "$(dirname "$final")"
printf "{\"schema_version\":1,\"transfer_id\":\"%s\",\"verified_at\":\"%s\",\"checksum_manifest_sha256\":\"%s\"}\n" \
  "$transfer_id" "$(date -u +%FT%TZ)" "$expected_manifest_sha" >.ready.tmp
mv -- .ready.tmp .ready
mv -- "$payload" "$final"
rmdir -- "$stage"
printf "READY %s\n" "$final"'
printf '%s\n' "$remote_finalize" | ssh "${ssh_options[@]}" "$ssh_target" \
  bash -s -- "$remote_root" "$repo_id" "$remote_stage" "$replace" "$transfer_id" \
  "$checksum_manifest_sha256"

echo "Dataset verified and published atomically: $ssh_target:$remote_final"
