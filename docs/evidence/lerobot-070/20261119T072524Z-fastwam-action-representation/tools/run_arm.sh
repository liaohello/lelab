#!/usr/bin/env bash
# One arm of the absolute-vs-relative comparison.
#
# usage: run_arm.sh <a|b> <updates> <outdir>
#
# Both arms share base checkpoint, seed, sampling order, dataset split, camera
# layout, optimizer preset, schedule, update count and inference noise. The only
# difference is the action representation:
#   a: absolute actions, normalisation stats from the absolute view
#   b: actions relative to the current pose (joints only), normalisation stats
#      from the relative view; grippers stay absolute percentages
set -euo pipefail

ARM="$1"; UPDATES="$2"; OUT="$3"
case "$ARM" in
  a) VIEW=/home/lfy/lelab-rmr/assets/so101_left_red_view; REL=false ;;
  b) VIEW=/home/lfy/lelab-rmr/assets/so101_left_red_rel;  REL=true  ;;
  *) echo "arm must be a or b" >&2; exit 64 ;;
esac

EPS=$(cat /home/lfy/lelab-rmr/runs/so101-lora/train_episodes.txt)
EXTRA=()
if [ "$REL" = true ]; then
  EXTRA=(
    --policy.use_relative_actions=true
    '--policy.relative_exclude_joints=["gripper"]'
  )
fi

cd /home/lfy/lelab-rmr
exec env -u PYTHONPATH \
  OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 \
  PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  HF_HOME=/home/lfy/lelab-rmr/hf-cache HF_HUB_OFFLINE=1 \
  .venv/bin/python -m lerobot.scripts.lerobot_train \
  --policy.type=fastwam \
  --policy.pretrained_path=/home/lfy/lelab-rmr/fastwam-ckpt \
  --policy.device=cuda \
  --policy.dtype=bfloat16 \
  --policy.text_encoder_device=cpu \
  --policy.action_dim=12 \
  --policy.proprio_dim=12 \
  '--policy.image_size=[224,672]' \
  --policy.freeze_video_expert=true \
  --policy.action_horizon=32 \
  --policy.n_action_steps=32 \
  --policy.use_peft=true \
  --policy.push_to_hub=false \
  "${EXTRA[@]}" \
  --peft.r=16 \
  --peft.lora_alpha=32 \
  --dataset.repo_id=local/so101_scene1_left_red_v1 \
  --dataset.root="$VIEW" \
  "--dataset.episodes=[$EPS]" \
  --batch_size=1 \
  --num_workers=0 \
  --steps="$UPDATES" \
  --save_freq=50 \
  --log_freq=10 \
  --seed=1003 \
  --output_dir="$OUT" \
  --job_name="ab-$ARM"
