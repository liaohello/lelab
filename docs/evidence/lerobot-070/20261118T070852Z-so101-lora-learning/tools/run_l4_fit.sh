#!/usr/bin/env bash
# L4's pre-registered fit check: episodes 0 and 50, 4 anchors each, fixed noise.
# EXPERIMENT.md §3 asks for a >=30% drop in action loss on these 8 windows
# relative to the untrained head. The training log only shows sampled-noise
# losses, so this is the registered measurement.
set -euo pipefail
C=/home/lfy/lelab-rmr/closure/so101-lora
V=/home/lfy/lelab-rmr/assets/so101_left_red_view
BASE=/home/lfy/lelab-rmr/fastwam-ckpt
PY=/home/lfy/lelab-rmr/.venv/bin/python
COMMON="--view $V --base-checkpoint $BASE --role train --episodes 0,50"

export HF_HOME=/home/lfy/lelab-rmr/hf-cache
export HF_HUB_OFFLINE=1
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

echo "=== step0 on fit episodes $(date +%H:%M:%S) ==="
env -u PYTHONPATH $PY $C/collect_predictions.py $COMMON --label fit-step0 \
  --out $C/preds-step0-fit.npz --report $C/collect-step0-fit.json

echo "=== L4 adapter on the same 8 windows $(date +%H:%M:%S) ==="
env -u PYTHONPATH $PY $C/collect_predictions.py $COMMON \
  --adapter /home/lfy/lelab-rmr/runs/so101-lora/l4/checkpoints/last/pretrained_model \
  --label fit-l4 --out $C/preds-l4-fit.npz --report $C/collect-l4-fit.json

echo "=== score $(date +%H:%M:%S) ==="
$PY $C/score_predictions.py --preds $C/preds-l4-fit.npz \
  --train-stats $C/train_stats.json --reference $C/preds-step0-fit.npz \
  --label fit-l4 --out $C/score-l4-fit.json
echo "=== done $(date +%H:%M:%S) ==="
