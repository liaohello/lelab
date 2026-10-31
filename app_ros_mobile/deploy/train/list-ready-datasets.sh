#!/usr/bin/env bash
# Read-only inventory of datasets that completed checksum verification and atomic publication.
set -euo pipefail

dataset_root=${LELAB_DATASET_ROOT:-/home/trainer/Desktop/lelab_server/shared/datasets}
if [[ $# -gt 1 || ( $# -eq 1 && "$1" == -* ) ]]; then
  echo "Usage: $0 [DATASET_ROOT]" >&2
  exit 64
fi
[[ $# -eq 0 ]] || dataset_root=$1
[[ -d "$dataset_root" ]] || { echo "dataset root not found: $dataset_root" >&2; exit 66; }

while IFS= read -r -d '' ready_file; do
  dataset_dir=${ready_file%/.ready}
  repo_id=${dataset_dir#"$dataset_root"/}
  printf '%s\t%s\n' "$repo_id" "$dataset_dir"
  sed 's/^/  /' "$ready_file"
  if [[ -f "$dataset_dir/meta/lelab-composite.json" ]]; then
    python3 - "$dataset_dir/meta/lelab-composite.json" <<'PY' || true
import json, sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
members = payload.get("repo_ids") or []
weights = (payload.get("weights") or {}).get("mode", "frames")
built = payload.get("built_at")
size = payload.get("size_bytes")
print(f"  composite: members={len(members)} weights={weights} built_at={built} size={size}")
for member in members:
    print(f"    - {member}")
PY
  fi
done < <(
  find "$dataset_root" -type f -name .ready \
    ! -path "$dataset_root/.incoming/*" \
    ! -path "$dataset_root/.trash/*" \
    -print0 | LC_ALL=C sort -z
)
