#!/usr/bin/env bash
# L6: dev-set comparison — random action head (step0) vs the L5 adapter.
#
# Two separate prediction runs, same anchors, same noise seed, same stats; the
# scorer is a different process and never imports torch.
set -euo pipefail
C=/home/lfy/lelab-rmr/closure/so101-lora
V=/home/lfy/lelab-rmr/assets/so101_left_red_view
BASE=/home/lfy/lelab-rmr/fastwam-ckpt
PY=/home/lfy/lelab-rmr/.venv/bin/python
COMMON="--view $V --base-checkpoint $BASE --role dev"

export -n PYTHONPATH || true
export HF_HOME=/home/lfy/lelab-rmr/hf-cache
export HF_HUB_OFFLINE=1
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

echo "=== step0 reference (no adapter) $(date +%H:%M:%S) ==="
env -u PYTHONPATH $PY $C/collect_predictions.py $COMMON --label step0 \
  --out $C/preds-step0-dev.npz --report $C/collect-step0-dev.json

echo "=== L5 adapter $(date +%H:%M:%S) ==="
env -u PYTHONPATH $PY $C/collect_predictions.py $COMMON \
  --adapter /home/lfy/lelab-rmr/runs/so101-lora/l5/checkpoints/last/pretrained_model \
  --label l5 --out $C/preds-l5-dev.npz --report $C/collect-l5-dev.json

echo "=== score $(date +%H:%M:%S) ==="
$PY $C/score_predictions.py --preds $C/preds-l5-dev.npz \
  --train-stats $C/train_stats.json --reference $C/preds-step0-dev.npz \
  --label l5 --out $C/score-l5-dev.json
echo "=== done $(date +%H:%M:%S) ==="
