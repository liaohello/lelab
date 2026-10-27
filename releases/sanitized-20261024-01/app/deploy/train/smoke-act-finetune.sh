#!/usr/bin/env bash
# CLI fallback for the UI's "train on the train server" button: a short ACT
# fine-tune of the synthetic dataset, using the same environment the SSH
# runner sets (HF_HOME/HF_LEROBOT_HOME under the train-server root).
#
#   bash smoke-act-finetune.sh                     # 50 steps, batch 2
#   bash smoke-act-finetune.sh --steps 200 --batch-size 4
#
# The UI path is: sync the dataset (POST /datasets/local/<id>/sync-train) then
# POST /jobs/training with target.runner=ssh_remote.
set -euo pipefail

dataset=lelab-test/act-so101-smoke
policy_path=
steps=50
batch_size=2
job_name=direct-act-smoke
project_root=${LELAB_TRAIN_ROOT:-$HOME/Desktop/lelab_server}
python=${LELAB_TRAIN_PYTHON:-$project_root/app/.venv/bin/python}
endpoint=${HF_ENDPOINT:-https://hf-mirror.com}

usage() {
  cat <<'EOF'
Usage: smoke-act-finetune.sh [options]

  --dataset REPO_ID     Fake dataset repo id (default: lelab-test/act-so101-smoke)
  --policy-path PATH    Pretrained ACT dir to fine-tune (default: models/pretrained/act_so101)
  --steps N             Training steps (default: 50)
  --batch-size N        Batch size (default: 2)
  --job-name NAME       Output directory name under jobs/ (default: direct-act-smoke)
  --root PATH           Train-server root
  --python PATH         Interpreter with lerobot installed
  --endpoint URL        HF endpoint used when the policy is a Hub repo id
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset) dataset=$2; shift 2 ;;
    --policy-path) policy_path=$2; shift 2 ;;
    --steps) steps=$2; shift 2 ;;
    --batch-size) batch_size=$2; shift 2 ;;
    --job-name) job_name=$2; shift 2 ;;
    --root) project_root=$2; shift 2 ;;
    --python) python=$2; shift 2 ;;
    --endpoint) endpoint=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done

[[ -z "$policy_path" ]] && policy_path="$project_root/models/pretrained/act_so101"
dataset_dir="$project_root/datasets/$dataset"
[[ -f "$dataset_dir/.ready" ]] || { echo "dataset not synced: $dataset_dir/.ready missing" >&2; exit 66; }
[[ -d "$policy_path" ]] || { echo "pretrained policy dir not found: $policy_path" >&2; exit 66; }
[[ -x "$python" ]] || { echo "interpreter not found: $python" >&2; exit 69; }

run_dir="$project_root/jobs/$job_name/run"
mkdir -p "$run_dir"

export HF_HOME="$project_root/models/huggingface"
export HF_LEROBOT_HOME="$project_root/datasets"
export HF_ENDPOINT="$endpoint"
export HF_HUB_DISABLE_XET=1

echo "dataset:    $dataset"
echo "policy:     $policy_path"
echo "steps:      $steps (batch $batch_size)"
echo "output:     $run_dir"

exec "$python" -m lerobot.scripts.lerobot_train \
  --dataset.repo_id="$dataset" \
  --dataset.root="$dataset_dir" \
  --policy.path="$policy_path" \
  --steps="$steps" \
  --batch_size="$batch_size" \
  --num_workers=0 \
  --log_freq=1 \
  --save_freq="$steps" \
  --save_checkpoint=true \
  --policy.device=cuda \
  --policy.push_to_hub=false \
  --wandb.enable=false \
  --output_dir="$run_dir"
