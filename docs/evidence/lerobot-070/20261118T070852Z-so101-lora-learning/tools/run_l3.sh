#!/usr/bin/env bash
# L3 short real-data run (20 updates). Frozen recipe: EXPERIMENT.md §1.
#
# Attempt 1 died with a CUDA OOM while loading because `text_encoder_device`
# defaulted to the GPU, so UMT5-XXT sat next to the 12 GB DiT. The frozen recipe
# says "text encoder on CPU"; the argv simply lacked the flag.
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
  --steps=20 \
  --save_freq=10 \
  --log_freq=5 \
  --seed=1000 \
  --output_dir=/home/lfy/lelab-rmr/runs/so101-lora/l3 \
  --job_name=l3
