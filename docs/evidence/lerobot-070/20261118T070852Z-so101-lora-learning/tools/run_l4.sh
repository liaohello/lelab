#!/usr/bin/env bash
# L4 small-sample fit (200 updates on two episodes, EXPERIMENT.md §2/§3).
#
# Runs on the same frozen recipe as L3 plus the prompt-cache change in
# `_batch_to_training_sample` (numerically identical: the frozen UMT5 encoding of
# a constant instruction is reused instead of recomputed every step, which was
# 47.6 s of the 48 s step on CPU).
set -euo pipefail
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
  --peft.r=16 \
  --peft.lora_alpha=32 \
  --dataset.repo_id=local/so101_scene1_left_red_v1 \
  --dataset.root=/home/lfy/lelab-rmr/assets/so101_left_red_view \
  '--dataset.episodes=[0,50]' \
  --batch_size=1 \
  --num_workers=0 \
  --steps=200 \
  --save_freq=50 \
  --log_freq=10 \
  --seed=1001 \
  --output_dir=/home/lfy/lelab-rmr/runs/so101-lora/l4 \
  --job_name=l4
