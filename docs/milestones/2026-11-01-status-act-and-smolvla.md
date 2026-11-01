# 2026-11-01 阶段状态快照：ACT 训练可用，SmolVLA 轮次不足

状态：**离线/仿真证据已归档，实机未验证。** 本文是当前阶段的一句话结论与证据索引，不替代
[so101_act_scene1 实验记录](../../work/so101_act_scene1/README.md) 与
[RESEARCH_PROGRESS](../RESEARCH_PROGRESS.md) 中的历史里程碑。

> 证据位置说明：`work/` 与 `plans/` 在 `.gitignore` 内，**不随仓库发布**。本文引用的
> `work/so101_act_scene1/**` 路径只在本地工作副本可读；公开仓库侧能看到的是本文的数字摘要，
> 不是原始日志。需要复核时请在原工作站查这些文件。

## 1. 一句话结论

| 模型 | 当前判定 | 依据 |
| --- | --- | --- |
| **ACT** | **训练成功，可作为当前基线策略**：同一 checkpoint 在 MuJoCo 闭环里 3 次尝试成功 2 次 | `work/so101_act_scene1/evidence/closed_loop_030000.json`（2026-10-30 实测） |
| **SmolVLA** | **训练轮次不足，当前成功次数少**：只有离线指标，**0 条闭环成功记录** | `docs/milestones/2026-10-25-smolvla-bimanual-10k-finetune.md`（2026-10-25 实测） |

两条都不是论文复现结论，也都不构成本仓库对外宣称的 benchmark 成功率。

## 2. ACT 证据（已实测）

| 项 | 实测值 | 来源 |
| --- | --- | --- |
| 训练 | 30,000 steps，1 h 49 min 正常退出；末步 `loss 0.033 / l1 0.031 / kld 0.000` | `evidence/train.log` |
| 数据 | `local/so101_scene1_single_random_v1`，64 集 / 45,031 帧 / 50 fps，12 维 state+action，三路 480×640 相机；train 52 / val 12 | `evidence/preflight.json`、`evidence/run_manifest.json` |
| checkpoint | 5k / 10k / 20k / 30k，各 592 MB，strict reload 通过 | `evidence/run_manifest.json` |
| 离线（first-10 原始单位 MAE，12 集 × 8 起点 = 96 窗口） | ACT **1.2608**；`state_copy` 0.8731；`train_mean` 19.8895 → **未同时优于两个基线** | `evidence/eval_step30000.json` |
| 离线（全 100 步 MAE） | ACT 1.3534，优于两个基线 | 同上 |
| 闭环（MuJoCo 50 Hz，chunk 100，物理成功谓词） | 10k **0/3**、20k **1/3**、30k **2/3** | `evidence/closed_loop_0{10000,20000,30000}.json` |
| 30k 逐条 | seed16000 红场景 成功 14.3 s；seed16002 红场景 成功 12.9 s；seed16001 蓝场景 `wrong_tray` 失败 | `evidence/closed_loop_030000.json` |
| 录像 | `evidence/../videos/030000/seed1600{0,1,2}_*.mp4` | 3 个 MP4 |

**因此“ACT 训练成功”准确表述为：链路正确 + 同一 checkpoint 在单一场景的闭环 3 次里成功 2 次，
且成功率随训练步数单调上升（0/3 → 1/3 → 2/3）。** 不包含：跨场景泛化、蓝集能力、语言选目标、
实机执行。离线 first-10 MAE 仍劣于 `state_copy` 基线，属于未过的门禁，不因闭环结果而改判。

## 3. SmolVLA 证据（已实测，离线）

| 项 | 实测值 | 来源 |
| --- | --- | --- |
| 训练 | 10,000 updates，exit 0，中途两次预注册 GPU 冲突暂停后恢复完成 | 里程碑文档 §run |
| 离线主指标 | scaled MAE **0.3105**（update 5,000 为 0.3324；更早 500-update pilot 为 0.4471） | 里程碑文档 §results |
| 收益饱和 | 8,000 → 10,000 updates 只再降 0.06% | 同上 |
| 原始 A 门禁 | **未通过**，未放宽 | 同上 §legacy gate |
| 闭环 / 实机 | **0 条记录**，没有仿真或实机成功案例 | 同上 §boundaries |

**因此“SmolVLA 训练轮次不够、当前成功次数较少”准确表述为：只跑到 10,000 updates，离线误差在
8,000 步后就基本不再下降，且从未进入闭环评测，所以现有成功记录为 0，而不是“成功率低但有几例”。**
把它和 ACT 直接比成功率是不成立的对照——两者的数据、相机、频率和任务都不同。

## 4. 数量级差距（为什么说“轮次不够”）

| 维度 | ACT | SmolVLA |
| --- | --- | --- |
| 训练步数 | 30,000 steps × batch 16 ≈ 48 万样本 ≈ 13 epoch | 10,000 updates |
| 数据 | 64 集同场景示范，50 fps | 双臂 bimanual 集，开发集划分 |
| 评测 | 离线 96 窗口 + 闭环 9 次尝试 | 仅离线 12 集验证 |
| 成功记录 | 3/9（其中 30k 为 2/3） | 0 |

结论：SmolVLA 要把“成功次数少”这件事变成可判断的问题，**先要补齐闭环评测入口**，
而不是继续加步数；在闭环入口存在之前加步数无法产出成功率。

## 5. 复现入口

```bash
# ACT 离线评估（服务器，checkpoint 已存在）
python eval_offline.py --checkpoint <run>/train/checkpoints/030000/pretrained_model \
  --dataset-root <dataset> --splits <splits.json> --out <run>/eval_step030000.json

# ACT 闭环评估（MuJoCo，本机或服务器）
python scripts/closed_loop_eval.py --checkpoint <run>/train/checkpoints/030000/pretrained_model ...
```

完整命令与门禁见 [so101_act_scene1/README.md](../../work/so101_act_scene1/README.md) 第 6 节。

## 6. 允许与禁止的表述

| 允许 | 禁止 |
| --- | --- |
| ACT 30k checkpoint 在单场景 MuJoCo 闭环 2/3 成功 | ACT 已具备通用抓取能力 / 已达论文水平 |
| ACT 离线 first-10 MAE 未过门禁，全段 MAE 过基线 | 省略 first-10 未过门禁，只报全段 MAE |
| SmolVLA 训练轮次不足且无闭环评测 | SmolVLA 成功率 0% 所以架构不行 |
| 以上均为仿真/离线结果，实机未验证 | 把仿真成功写成实机可用 |

## 7. 未验证项

- 未在真实 SO-101 上执行任何策略；闭环结果全部来自 MuJoCo。
- 未测 30k checkpoint 在不同 seed / 不同物体初始位姿下的稳健性（只有 3 个固定 seed）。
- 未做双臂 12 维策略：现有 ACT 是 scene1 单场景、单活动臂、12 维动作但只有一侧在动。
- 未评测 SmolVLA 的闭环表现，也没有可比的 12 维双臂 checkpoint。
- 本文件写作时未启动训练、未占用 GPU、未连接串口或相机。
