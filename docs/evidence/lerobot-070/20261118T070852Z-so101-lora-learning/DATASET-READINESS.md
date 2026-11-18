# SO-101 数据就绪报告（LEARN-DAT-01/02/03）

Run：`20261118T070852Z-so101-lora-learning`。所有数字来自本轮只读核验，命令与退出码见文末。

## 1. 结论

**唯一契约完整、可用的候选**：`local/so101_scene1_left_red_v1`（WSB 采集线
`scene1_single_random_left_red_v1`），实体路径
`/home/lfy/so101-wsb/runs/formal_v1/dataset`（g128，1.2 GB）。

- 120 集 / 89,194 帧 / **50 fps** / 12 维 state+action / 三相机（central、left_wrist、right_wrist）480×640。
- 原始划分 **100 train / 20 validation**，`family_crosses=0`（119 个 scene family，一个 2 集 family 整体归入同一侧），
  划分在**任何归一化统计之前**冻结（协议原文）。
- 训练统计已有权威副本：`train split only`、100 集 / **74,131 帧**；
  本轮按 episode 重算，帧数**完全一致**，逐维均值最大差 0.024°、标准差最大差 0.0033（float 累加噪声级）。
- 完整性：data parquet 与三路视频**全部存在**，`sum(episode.length)=89,194` 与声明一致。
- 时间轴：每集 dt ∈ [0.0199995, 0.0200005] s（50 fps），单调；三路视频 50 fps、窗口 14.4 s/720 帧。
- 动作语义：**绝对关节目标**（度 / 夹爪百分比）。窗口首帧 `action[0]` 与 `state[0]` 平均绝对差 **0.2032°**，
  符合"从当前状态施加的目标"而非增量。
- 加载器实测（CPU）：`action [2,32,12]`、`state [2,12]`、每相机 `[2,9,3,480,640]`、uint8、
  `action_is_pad`/`*_is_pad` 齐备；末尾窗口 padding 正确、窗口中段无 padding。
- 处理器实测：三相机按名排序后各 224×224、拼接宽 **672**；state/action 归一化有限，
  normalize→denormalize 往返最大误差 **1.9e-6**；提示词包含数据集指令原文。

## 2. 指纹与来源

| 项 | 值 |
| --- | --- |
| repo_id | `local/so101_scene1_left_red_v1` |
| 数据路径（g128） | `/home/lfy/so101-wsb/runs/formal_v1/dataset` |
| 采集清单 | `…/runs/formal_v1/collection_manifest.json`（127 次尝试、120 成功、`stop_reason` 见清单） |
| 划分/审计 | `/home/lfy/so101-wsb/evidence/audit_formal_v1.json`（`split`、`episodes[].role`、`norm_scope`、`norm_stats`） |
| 协议 | `/home/lfy/so101-wsb/scene1_single_random_left_red_protocol.json`（`status=frozen_after_pilot`） |
| 证书 | `scene1_left_red_single_tray_certificate_v1.json`，SHA-256 `dc774b85…597b` |
| `meta/info.json` | `9aa1b086…96b17` |
| `meta/stats.json`（源，覆盖 120 集） | `bad0213b…1dd2` |
| `meta/tasks.parquet` | `c0f26803…cd93` |
| `meta/episodes/chunk-000/file-000.parquet` | `3f27d84d…c510` |
| 任务文本 | `Put the red block into the red tray.`（单任务） |
| robot_type | `so101_dual_arm_mujoco`（MuJoCo 仿真，**不是实机**） |

## 3. 使用边界（写进实验协议，不能扩大）

- **单侧活动**：红块→红托盘，只有**左臂**执行；右臂 6 维基本静止
  （右肩 std 0.000/0.101，右夹爪 std 0.004）。12 维是接口宽度，不是双臂协同示范。
- **仿真**：MuJoCo 物体与相机渲染，不等于实机视觉/动力学。
- **动作是绝对目标**：微调与评估都按绝对关节目标处理，不做相对/增量变换
  （`use_relative_actions=false`）。
- **开发集不是测试集**：20 集是采集时冻结的 validation，本轮只称"开发集"。
- 统计数据：源 `meta/stats.json` 覆盖全部 120 集（含开发集），本轮**不使用**它训练；
  训练/评估统一用重算的 train-only 统计（`train_stats.json`），并另建派生视图
  `…/assets/so101_left_red_view`（源目录未改）。

## 4. 复跑命令（g128，CPU，只读）

```bash
cd /home/lfy/lelab-rmr/closure/so101-lora
# 完整性 + 划分 + 统计（约 40 s）
/home/lfy/lelab-rmr/.venv/bin/python verify_dataset.py \
  --dataset /home/lfy/so101-wsb/runs/formal_v1/dataset \
  --audit /home/lfy/so101-wsb/evidence/audit_formal_v1.json \
  --manifest /home/lfy/so101-wsb/runs/formal_v1/collection_manifest.json \
  --out inventory.json --stats-out train_stats.json --decode-episode 0
# 加载器形状/padding（9 项断言）
/home/lfy/lelab-rmr/.venv/bin/python prepare_and_check.py --source …/dataset \
  --stats train_stats.json --audit … --manifest … \
  --view /home/lfy/lelab-rmr/assets/so101_left_red_view --out loader-check.json
# 处理器/归一化/提示词（8 项断言）
/home/lfy/lelab-rmr/.venv/bin/python check_processor.py \
  --view /home/lfy/lelab-rmr/assets/so101_left_red_view --out processor-check.json
```

退出码：三次运行均 0；`loader-check.json` 9/9、`processor-check.json` 8/8 通过。

## 5. 本轮踩到并修掉的两个度量错误

1. 第一版按 `audit.episodes` 的**列表顺序**当 episode 序号，训练帧数比声明多 208 →
   改为 `manifest.successes_rows[i].attempt_id → audit[attempt_id].role` 映射，帧数精确等于 74,131。
2. 第一版统计循环把整个 parquet（全部 120 集）按 100 集重复累加，得到 8,919,400 帧、
   均值偏 4.99° → 改为一次读取 + episode 掩码，帧数与逐维均值均对齐。
   （两次错误都保留在本文，避免把"看起来对"当证据。）
