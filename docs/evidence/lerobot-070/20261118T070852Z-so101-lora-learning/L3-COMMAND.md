# L3–L5 命令账本（实际发出的命令，含失败的尝试）

远端：`g128`（`lfy@192.168.6.128`，经 `nc -X connect -x 222.20.98.74:1080` 的 gost 代理），
V100S-32GB，CUDA driver 535，torch 2.9.1+cu126，venv `/home/lfy/lelab-rmr/.venv`。
运行根：`/home/lfy/lelab-rmr/runs/so101-lora/`。所有训练走 `tmux`，日志重定向到 `l3.log`。

## 0. 训练 episode 列表（来自冻结划分）

```bash
mkdir -p /home/lfy/lelab-rmr/runs/so101-lora
python -c "import json;d=json.load(open('/home/lfy/lelab-rmr/assets/so101_left_red_view/splits.json'));print(','.join(str(i) for i in d['train']))" \
  > /home/lfy/lelab-rmr/runs/so101-lora/train_episodes.txt
```

## 1. 失败的三次尝试（保留，说明为什么要这样写）

| # | 失败原因（原文） | 处理 |
| --- | --- | --- |
| 1 | 传了 `--peft.lora_dropout=0.0` | `PeftConfig` 无该字段 → 去掉，dropout 用默认 0 |
| 2 | `--policy.path=...` | `FastWAMConfig` 解析报 `unexpected keyword`（`policy.path` 会把 ckpt 目录当 dataclass 展开）→ 改用 `--policy.type=fastwam --policy.pretrained_path=...` |
| 3 | 未关 `push_to_hub` | 训练结束会尝试上传 → 加 `--policy.push_to_hub=false` |

## 2. L3 最终发出的命令（唯一一条）

```bash
EPS=$(cat /home/lfy/lelab-rmr/runs/so101-lora/train_episodes.txt)
cd /home/lfy/lelab-rmr && env -u PYTHONPATH \
  OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 \
  HF_HOME=/home/lfy/lelab-rmr/hf-cache HF_HUB_OFFLINE=1 \
  /home/lfy/lelab-rmr/.venv/bin/python -m lerobot.scripts.lerobot_train \
  --policy.type=fastwam \
  --policy.pretrained_path=/home/lfy/lelab-rmr/fastwam-ckpt \
  --policy.device=cuda --policy.dtype=bfloat16 \
  --policy.action_dim=12 --policy.proprio_dim=12 \
  --policy.image_size=[224,672] \
  --policy.freeze_video_expert=true \
  --policy.action_horizon=32 --policy.n_action_steps=32 \
  --policy.use_peft=true --policy.push_to_hub=false \
  --peft.r=16 --peft.lora_alpha=32 \
  --dataset.repo_id=local/so101_scene1_left_red_v1 \
  --dataset.root=/home/lfy/lelab-rmr/assets/so101_left_red_view \
  --dataset.episodes=[$EPS] \
  --batch_size=1 --num_workers=0 \
  --steps=20 --save_freq=10 --log_freq=5 --seed=1000 \
  --output_dir=/home/lfy/lelab-rmr/runs/so101-lora/l3 --job_name=l3
```

`--dataset.episodes` 是 100 个训练集 episode（dev 20 集排除在外），来源是冻结划分，不是随手挑的。

## 3. 退出码

无。命令发出后 g128 失联（`BLOCKERS.md` B-1），没有拿到 `l3.log` 的任何回读，
因此 L3 的退出码与状态均为 **unknown**。

## 4. L4 / L5 的计划命令（**未发出**）

```bash
# L4：2 集 × 4 锚点，200 updates，lr 用 FastWAM preset 1e-4
... --dataset.episodes=[0,50] --steps=200 --save_freq=50 --seed=1001 \
    --output_dir=.../l4 --job_name=l4

# L5：100 集，500 updates，从 L4 的 adapter 续训（--policy.path=<l4 checkpoint>）
... --steps=500 --save_freq=100 --seed=1002 --output_dir=.../l5 --job_name=l5
```

L5 只有在 L4 达到 `EXPERIMENT.md` §3 的 ≥30% 下降后才发出；预算合计 720 updates / ≤60 min。

## 5. 评估（L5 / L6）—— 恢复后照抄，两步

预测与评分是分开的两个进程：`collect_predictions.py` 只负责用固定噪声产出 chunk，
`score_predictions.py` 完全不碰模型，只读 npz。这样"评分的代码"和"产出的代码"不可能互相掩护。

```bash
C=/home/lfy/lelab-rmr/closure/so101-lora
V=/home/lfy/lelab-rmr/assets/so101_left_red_view
BASE=/home/lfy/lelab-rmr/fastwam-ckpt

# (a) 随机动作头基线：不挂 adapter，dev 20 集 × 4 锚点
python $C/collect_predictions.py --view $V --base-checkpoint $BASE --role dev \
  --label step0 --out $C/preds-step0-dev.npz --report $C/collect-step0-dev.json

# (b) 训练后的 adapter（L3 只产 20 步，用作链路验证；L5 产物才是候选）
python $C/collect_predictions.py --view $V --base-checkpoint $BASE \
  --adapter /home/lfy/lelab-rmr/runs/so101-lora/l5/checkpoints/last/pretrained_model \
  --role dev --label l5 --out $C/preds-l5-dev.npz --report $C/collect-l5-dev.json

# (c) 评分（退出码 0=通过，1=未达门槛；两条命令共用同一 stats 与阈值）
python $C/score_predictions.py --preds $C/preds-l5-dev.npz \
  --train-stats $C/train_stats.json --reference $C/preds-step0-dev.npz \
  --label l5 --out $C/score-l5-dev.json
```

注意：
- `--adapter` 指向 `checkpoints/last/pretrained_model`（LeRobot 的保存目录），
  若该目录本身就是 adapter 目录（含 `adapter_config.json`），可以省掉 `--adapter`；
- dev 锚点在 `collect_predictions.py` 里按 episode 长度取 0 / 1/4 / 1/2 / 3/4，与训练集同一规则；
- 同一 episode 只建一个 `LeRobotDataset(episodes=[ep])`，索引 `i` 就是该集第 `i` 帧，
  脚本会断言 `episode_index` 与 `frame_index`，不依赖多集子集的枚举顺序。
