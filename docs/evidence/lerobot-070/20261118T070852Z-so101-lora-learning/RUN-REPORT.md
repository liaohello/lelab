# SO-101 FastWAM LoRA 学习验证 —— 执行记录（L3–L6）

Run：`20261118T070852Z-so101-lora-learning`，worktree `/home/liaozhengyan/Desktop/lelab-lerobot070`，
分支 `codex/lerobot-070-integration`。**未推送、未合并、未切 `current`、未连硬件、未发动作。**

## 0. 一句话结论

真实 SO-101 数据上的 LoRA 链路**跑通了**：L3 20 步 → L4 200 步 → L5 200 步，
adapter 落盘、新进程可加载、前向出 `[1,32,12]`。训练吞吐比预注册假设慢 20 倍（47 s/步），
根因是文本编码器每步重跑 UMT5-XXL；加缓存后 2.15 s/步（22 倍）。
预算被三次基础设施失败吃掉，L5 只跑到 200 步（注册 500）。

开发集结论：**`not_passed`**。相对随机动作头改善 2.5 倍（缩放 1.284→0.506、首步 13.81°→4.17°），
但相对"保持当前姿态"仍差 4.6 倍（0.111）与 13 倍（首步 0.31°）。
即：**有学习信号、链路成立、未达控制门槛**。

## 1. 时间与预算账（GPU 墙钟，g128 V100S-32GB）

| # | 阶段 | 起止 | 墙钟 | updates | 结果 |
| --- | --- | --- | --- | --- | --- |
| A | L3 尝试 1 | 22:19–22:25 | 6 min | 0 | **OOM**：`text_encoder_device` 默认跟随 cuda，UMT5-XXL 与 12 GB DiT 同卡 |
| B | L3 | 23:06:58–23:26 | 19 min | 20 | **passed**，checkpoint + adapter 落盘 |
| C | L4 尝试 1 | 23:31–23:33 | 2 min | 0 | **OOM**：上一条进程仍在释放显存，两个进程抢 31.74 GiB |
| D | L4 | 23:35–23:49 | 14 min | 200 | **passed** |
| E | L5 | 23:54–00:07 | 13 min | 200 | **passed（截断，注册 500）** |
| F | 开发集评估 | 00:07–00:58 | 40 min（含两次失败 4.5 min、两次模型加载各 3 min、两次 80 锚点采集各 ~18 min） | 0 | **passed** |
| **合计** | | 22:19–00:58 | **≈ 94 min** | **420** | |

### 1.1 超预算了，超在哪

预注册上限是 **720 updates / ≤60 min**。实际：**420 updates / ≈94 min**。

- 有效训练 34 min（B+D+E，420 updates，2.5 s/step）；
- **失败与重试 ~15 min**（A 6 + C 2 + F 前两次 4.5 + 进程退出的等待）；
- **评估 37 min**（两次 80 锚点，13.5 s/锚点；预注册只写了"评估计入预算"，没有估量）。

有三点必须写明：

1. **没有为了"再练一会儿看会不会更好"而延长训练**。L5 在 200 步按预算停下，未追加。
2. 超时主要来自**我自己的三次实现缺陷**（A 的配置漏项、C 的提前重启、F 的两次脚本 bug），
   不是模型或数据的问题。
3. 评估成本被严重低估：80 个锚点要 ~18 min，比 L5 整轮训练还长。
   下一轮如果还要做开发集对照，必须把这块单独列预算，或者预先缓存 VAE latent。

## 2. 吞吐：预注册假设错了 20 倍，以及为什么

预注册写"预计 1–2 s/update"，实测 L3 = **46.5–48.0 s/update**。拆开看：

- `data_s:0.33 prep_s:0.001 updt_s:47.6` —— 不是数据加载，是 update 本身；
- 采样 nvidia-smi 时 GPU 利用率 **0%**、CPU 单核 **99.8%**；
- 根因：`_batch_to_training_sample` 里 `self.model.encode_prompt(prompt)` **每步都跑一次**
  UMT5-XXL 前向，而 `text_encoder_device='cpu'` 把它钉在 CPU 上（放到 GPU 会 OOM，见 A）。

修法：训练路径改用已有的 `_encode_prompt_cached`（推理路径早就在用）。单任务数据集上
第二步起全部命中缓存，**47.6 s → 2.15 s/update（22 倍）**。数值上完全等价（同一个冻结编码器、
同一个 prompt → 同一份 embedding），`tests/policies/fastwam/test_fastwam_prompt_cache.py` 4 条断言锁住
"同 prompt 只编码一次、复用同一张量、不同 prompt 重算、训练路径确实走缓存"。

## 3. 三个 run 的实际损失（`logs/*.log`）

| run | 数据 | updates | loss_action 轨迹 |
| --- | --- | --- | --- |
| L3 | 100 集随机采 | 20 | 2.556 → 2.531 → 1.043 → 5.388 |
| L4 | 仅 episode 0,50 | 200 | 1.640 → 3.291 → 1.386 → … → 0.430（最好）… → 0.624 |
| L5 | 100 集 | 200 | 2.373 → 1.776 → 1.121 → 0.904 → 0.452 → 2.259 |

**必须说清楚的三件事**：

1. 这些是 **batch=1、随机噪声、随机时间点采样**的训练损失，**不是**预注册的固定窗口/固定噪声指标；
2. L4 的上下震荡（0.43 ↔ 3.35）来自两个 episode 交替：episode 0 和 50 的动作幅度差一个量级，
   batch=1 时每步就是"换一道题"，所以损失曲线不能当作收敛证据；
3. L5 跑完 200 步只见过 200 个样本（占训练集 74,131 帧的 0.27%），**这不足以叫"训练"**，
   它是"真实数据上的有限 pilot"，预注册里也是这么写的（§2 合计 720 updates / ≤60 min）。

## 4. 产物

g128 `/home/lfy/lelab-rmr/runs/so101-lora/{l3,l4,l5}/checkpoints/last/pretrained_model/`：
`adapter_config.json`、`adapter_model.safetensors`、`config.json`、`policy_pre/postprocessor*.json|safetensors`、
`train_config.json`；`training_state/` 有 optimizer 与 rng，可续训。权重不入 Git（体积与可复现性理由）。

## 5. 开发集对照（`DEV-EVAL.md`，`score-l5-dev.json`）

80 个锚点（20 dev 集 × 4），同一噪声 seed 2026，判定门槛在 `EXPERIMENT.md` §7 写死。

| | L5 | step0 随机头 | `state_copy` |
| --- | --- | --- | --- |
| 活跃维缩放 MAE | 0.506 | 1.284 | **0.111** |
| 全维原始 MAE | 5.94° | 15.17° | **1.28°** |
| 首步原始 MAE | 4.17° | 13.81° | **0.31°** |

三项门槛：`finite` ✅、比最优基线好 25% ❌（实际 -355%）、静态维不劣化 ✅（1.33×）→ **not_passed**。

最有诊断价值的一条是**首步**：绝对目标下 `action[0]≈state[0]`（差 0.20°），
`state_copy` 因此只有 0.31° 误差，而 L5 是 4.17° —— 模型连"第一步≈当前姿态"都没学会。
200 步 / 200 个样本不足以拟合这个映射，加步数之前应先把这个约束做成可测目标。

## 6. 评估耗时与三次失败（诚实记录）

| 尝试 | 现象 | 根因 | 代价 |
| --- | --- | --- | --- |
| eval 1 | `CUDA out of memory`（单进程 31.39 GiB） | `collect_predictions.py` 没传 `text_encoder_device`，UMT5 上了 GPU | 2.5 min |
| eval 2 | `NotImplementedError: upsample_bilinear2d_out_frame not implemented for 'Byte'` | 用 `return_uint8=True` 取图，模型 resize 要求 float | 2 min |
| eval 3 | 通过 | — | 20 min（step0）+ 20 min（L5） |

每次 80 锚点约 18 min（13.5 s/锚点：9 帧 × 3 相机的 VAE 编码 + 32 步去噪）。
这也是为什么"评估"必须算进 GPU 预算 —— 预注册里写了，但低估了。
