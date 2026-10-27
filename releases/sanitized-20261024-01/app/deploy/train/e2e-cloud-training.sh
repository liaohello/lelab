#!/usr/bin/env bash
# End-to-end smoke test of the "train on the train server" button, driven
# through the same HTTP endpoints the frontend uses.
#
# Run it ON the Jetson (it talks to the local LeLab API on 127.0.0.1:8000):
#
#   bash deploy/train/e2e-cloud-training.sh
#   bash deploy/train/e2e-cloud-training.sh --steps 200 --batch-size 4
#
# Steps: health -> dataset sync (staged rsync + .ready) -> POST /jobs/training
# with target.runner=ssh_remote -> poll metrics until the job finishes.
set -euo pipefail

api=${LELAB_API:-http://127.0.0.1:8000}
dataset=${LELAB_SMOKE_DATASET:-lelab-test/act-so101-smoke}
policy=${LELAB_SMOKE_POLICY:-/home/trainer/Desktop/lelab_server/models/pretrained/act_so101}
steps=50
batch_size=2
timeout_s=${LELAB_SMOKE_TIMEOUT:-1800}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api) api=$2; shift 2 ;;
    --dataset) dataset=$2; shift 2 ;;
    --policy) policy=$2; shift 2 ;;
    --steps) steps=$2; shift 2 ;;
    --batch-size) batch_size=$2; shift 2 ;;
    --timeout) timeout_s=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

json() { python3 -c "import json,sys;d=json.load(sys.stdin);print(eval(\"d$1\"))"; }

echo "== 1/4 LeLab health =="
curl -sf "$api/health" >/dev/null
curl -s "$api/system/train-server"; echo

echo "== 2/4 sync dataset $dataset =="
curl -sf -X POST "$api/datasets/local/$dataset/sync-train" >/dev/null
deadline=$((SECONDS + 900))
while :; do
  status=$(curl -sf "$api/datasets/sync-status")
  phase=$(printf '%s' "$status" | json "['active']['phase']")
  echo "  phase=$phase $(printf '%s' "$status" | json "['active']['progress']")%"
  case "$phase" in
    completed) break ;;
    failed|cancelled) echo "$status" >&2; exit 1 ;;
  esac
  (( SECONDS > deadline )) && { echo "sync timed out" >&2; exit 1; }
  sleep 3
done

echo "== 3/4 start ACT fine-tune on the training server =="
payload=$(python3 - "$dataset" "$policy" "$steps" "$batch_size" <<'PY'
import json
import sys

dataset, policy, steps, batch = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
print(
    json.dumps(
        {
            "config": {
                "dataset_repo_id": dataset,
                "policy_type": "act",
                "policy_pretrained_path": policy,
                "steps": steps,
                "batch_size": batch,
                "num_workers": 0,
                "log_freq": 1,
                "save_freq": steps,
                "save_checkpoint": True,
                "policy_device": "cuda",
                "output_dir": "outputs/train",
            },
            "target": {"runner": "ssh_remote"},
        }
    )
)
PY
)
job=$(curl -sf -X POST "$api/jobs/training" -H 'Content-Type: application/json' -d "$payload")
job_id=$(printf '%s' "$job" | json "['id']")
echo "  job=$job_id runner=$(printf '%s' "$job" | json "['runner']")"

echo "== 4/4 watch until the job finishes =="
deadline=$((SECONDS + timeout_s))
while :; do
  record=$(curl -sf "$api/jobs/$job_id")
  state=$(printf '%s' "$record" | json "['state']")
  step=$(printf '%s' "$record" | json "['metrics']['current_step']")
  loss=$(printf '%s' "$record" | json "['metrics']['current_loss']")
  echo "  state=$state step=$step loss=$loss"
  case "$state" in
    done) break ;;
    failed|interrupted)
      printf '%s' "$record" | json "['error_message']" >&2
      exit 1
      ;;
  esac
  (( SECONDS > deadline )) && { echo "training timed out" >&2; exit 1; }
  sleep 5
done

echo
echo "checkpoints:"
curl -sf "$api/jobs/$job_id/checkpoints"; echo
echo "SMOKE TEST PASSED ($job_id)"
