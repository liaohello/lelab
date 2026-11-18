# 预注册实验协议（L3–L5）

Run：`20261118T070852Z-so101-lora-learning`。本文件在**任何 GPU 训练之前**写定；
指标、预算和停止条件不得在看到结果后放宽。

## 1. 模型与配方（冻结）

| 项 | 值 | 依据 |
| --- | --- | --- |
| 基座 | `lerobot/fastwam_base`，rev `385a4bd1…`，`model.safetensors` 12,041,526,464 B | 已哈希核对 |
| 适配 | LoRA r=16，alpha=32，dropout=0，targets 由 `FastWAMPolicy._get_default_peft_targets()` 决定 | `9f1b73c` + 本轮 tiny 闭环 |
| 新增模块 | action encoder / action head / proprio encoder（base 不含）全参训练并随 adapter 保存 | `STAGE5-MEMORY.md` §3.1 |
| 冻结 | `freeze_video_expert=true`、基座其余全部冻结 | 显存与契约 |
| 精度 | bf16，梯度检查点开，文本编码器在 CPU | 同上 |
| 优化器 | FastWAM preset：AdamW lr **1e-4**（`optimizer_lr`），不手调 | `configuration_fastwam.py` |
| 维度 | action=12，proprio=12 | 数据集 |
| 相机 | central / left_wrist / right_wrist，各 224×224，拼接宽 **672** | `set_dataset_feature_metadata` + 本轮 processor 检查 |
| 时间窗 | `num_video_frames=33`，`action_video_freq_ratio=4`（9 帧视频），`action_horizon=32`，`n_action_steps=32` | FastWAM 原生，不改 |
| 动作语义 | 绝对关节目标（度/百分比），`use_relative_actions=false` | 数据核验 |
| 统计 | train-only（100 集，74,131 帧），派生视图 `assets/so101_left_red_view` | `DATASET-READINESS.md` |
| 数据划分 | train 0–99 / dev 20 集（按审计 `role`，`family_crosses=0`） | 冻结 |
| batch | B=1（显存与稳定性优先） | 前一轮 16.12 GB 实测 |

## 2. 预算（GPU 墙钟，含加载/保存/评估）

| 阶段 | 更新数上限 | 预计 | 说明 |
| --- | --- | --- | --- |
| L3 短程 | 20 | ≈ 4 min（加载 140 s + 20×~1.5 s + 保存） | 验证真实数据链路与 adapter 冷启动 |
| L4 拟合 | 200 | ≈ 6 min | 2 集 × 4 个固定窗口 |
| L5 pilot | 500 | ≈ 15 min | 若 L4 达标才跑 |
| **合计上限** | **720 updates / ≤ 60 min** | | 期间不并行其它模型；超时即停并保存 |

实测单步（2 相机 224²）：forward 0.6–10 s（首步含编译），backward 0.6 s，step 0.1 s；
三相机宽 672 预计 1–2 s/update。加载 139 s、保存 ~170 s 计入预算。

## 3. 预注册指标与判定

评估窗口：从**指定 episode** 取固定锚点，锚点集合与噪声种子在跑前写死（见下）。

- L4（拟合，训练集）：
  - 数据：train episode `0` 与 `50`，各 4 个锚点（帧 0、1/4、1/2、3/4 处），共 8 个窗口。
  - 指标：固定噪声（seed 2026）下的 action MSE（归一化空间）与反归一化 MAE（度）。
  - 通过：8 窗口平均 action loss 相对 step0 **下降 ≥30%**，且反归一化 MAE 下降；
    左臂 6 维与两个夹爪**分别**报告。视频 loss 不参与判定。
- L5（开发集）：
  - 数据：dev 20 集，每集 4 个锚点（同规则），episode 等权。
  - 主指标：以 train std 缩放的动作 MAE（12 维等权，另单列左臂 6 维与夹爪）。
  - 基线：同头初始化 **step0**、`train_mean` 常量、`state_copy`（绝对目标基线）。
  - 通过：优于**全部三个基线**，且相对最优基线改善 **≥25%**；低方差通道（std<1°）
    的缩放 MAE 不低于 0.5（防止靠静态维度刷分）。
  - 只有同时满足 L4 与 L5 才可写"离线开发候选通过"；否则如实写"能拟合/未达门槛"。

## 4. 停止条件（硬）

1. 任一损失出现 NaN/Inf → 立即停止，保存现场，判 failed。
2. L4 到 100 updates 时 action loss 相对 step0 下降 <10% → 停止并诊断，不加步数。
3. L5 到 250 updates 时仍不优于 step0 → 停止，判"未达门槛"，不加步数。
4. 总更新数或墙钟超预算 → 停止，保存可恢复状态；**不另起 run 绕过上限**。
5. 任何 shape/dtype/维度不匹配 → 立即失败，不做 `strict=False` 兜底。

## 5. 评估实现约束

- 推理只用当前帧观测（`n_obs_steps=1`）+ 指令；**不得**把未来帧或目标 action 放进输入。
- 固定 `noise`（显式张量）以避免采样随机性；同一锚点对 step0 / 微调 / 基线用同一噪声。
- 反归一化用训练时的 postprocessor；基线在同一空间、同一单位下比较。
- 报告：每 episode 指标、总体、左臂/右臂、两个夹爪、首步与全 horizon 分开。

## 6. 产物

- adapter（`adapter_model.safetensors` + `adapter_config.json`）、`modules_to_save` 权重、
  policy config、pre/post processor、train-only stats、训练日志、评估 JSON。
- 权重与数据留在 g128 运行目录，不入 Git；本目录只放脱敏报告与脚本。

## 7. 修正案 1 —— 指标定义的补全（写于**任何**训练结果存在之前）

时间：`2026-11-18T15:0xZ`，此时 L3 的状态是 **unknown**（g128 失联，见 `BLOCKERS.md`），
没有任何 step 级 loss、没有 adapter、没有预测结果。此修正只把 §3 里一处**无法执行**的写法
换成可执行且更严的判定，不放宽任何门槛。

### 7.1 原写法的问题

§3 写"低方差通道（std<1°）的缩放 MAE 不低于 0.5（防止靠静态维度刷分）"。用 train-only
统计核对后发现两件事：

- 第 6 维与第 10 维的 std **正好是 0.0**（`train_stats.json`），缩放 MAE 是 0/0，不是"0.5"能描述的；
- "不低于 0.5"的方向与该句的目的相反：静态维度越准，缩放误差越小，取值只会更低。

### 7.2 采纳的判定（更严，见 `tools/score_predictions.py::verdict`）

12 维中 std≥1° 的只有第 0–5 维（18.0–31.6°，左臂），第 6–11 维 std 为 0.0–0.12°。因此：

1. **主判定** `mae_scaled_active`：std≥1° 维度上的 `mean|err|/std`，必须比**最优基线**好 **≥25%**；
2. **次判定** `mae_raw_all`：全部 12 维的原始 MAE（度，等权），同样要 **≥25%**；
3. **静态维度不得被牺牲**：静态维原始 MAE ≤ 最优基线的 **1.5 倍**。

三条同时成立才算通过。基线固定为 `zero` / `train_mean` / `state_copy` / `step0`（随机动作头），
全部在同一锚点、同一噪声（seed 2026）、同一反归一化下计算。

### 7.3 与 §3 的关系

- 预算、停止条件、锚点规则、噪声种子、数据划分**完全不变**；
- 判定从"一个数"变成"三条同时满足"，比原写法更难通过；
- 该修正与 `score_predictions.py` 的 9 条 tiny/fake 自测一起固化
  （`test_score_logic.py`：完美预测通过、保持姿态不通过、只赢静态维不通过、
  NaN 不通过、形状不符报错、全 padding 切片为 NaN、std=0 维度被排除）。
