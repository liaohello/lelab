#!/usr/bin/env bash
# L5 budget-capped pilot: the full 100-episode train split, 200 updates.
#
# The pre-registered plan was 500 updates; at the measured 2.51 s/update plus a
# 3 min load and 2 min save, 500 would not fit the remaining wall-clock cap, so
# this runs the largest count that does. The cap is recorded, not silently
# raised (EXPERIMENT.md §4.4). It starts from the base checkpoint rather than
# resuming L4's adapter because `--policy.pretrained_path` expects a
# `model.safetensors` and the PEFT checkpoint directory only holds
# `adapter_model.safetensors`.
set -euo pipefail
cd /home/lfy/lelab-rmr
EPS=$(cat runs/so101-lora/train_episodes.txt)
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
  "--dataset.episodes=[$EPS]" \
  --batch_size=1 \
  --num_workers=0 \
  --steps=200 \
  --save_freq=100 \
  --log_freq=20 \
  --seed=1002 \
  --output_dir=/home/lfy/lelab-rmr/runs/so101-lora/l5 \
  --job_name=l5
