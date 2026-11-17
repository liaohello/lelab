# FastWAM stage5 内存剖析（K4 / VFY-04）

一个模型、一个进程、显式阶段：`budget → forward → backward → clip → optimizer_step`，
每阶段先把 JSON 原子落盘再进入下一阶段，所以 OOM 也会留下"死在哪一步、峰值多少"。
脚本：`stage5_profile.py`。主机 g128（V100S-32GB，实测 `total 34.08 GB`）。

## 1. 两个必须分开的配置

`FastWAMConfig.freeze_video_expert` 默认 **False**，checkpoint 的 `config.json` 没有覆盖它，
所以"按 checkpoint 原样"训练时 6020.7M 全部可训练；SO-101 微调才需要显式冻结 ~5B 视频专家。
把这两种混在一起谈"显存够不够"是错的。

| 配置 | 可训练参数 | 训练态下界（bf16 权重 + bf16 梯度 + fp32 Adam 两矩） | 结论 |
| --- | --- | --- | --- |
| `freeze_video_expert=false`（checkpoint 原样） | 6020.7M | 2.04×6 → 12.04 + 12.04 + 48.17 = **72.3 GB** | 34 GB 与 40 GB 都放不下，不做必败试跑 |
| `freeze_video_expert=true`（SO-101 微调用） | 1020.9M | 2.04 + 2.04 + 8.17 = **12.25 GB** | 加上常驻的 12.04 GB 冻结权重 = 24.3 GB，34 GB 卡上仅剩约 9.8 GB 给激活 |

历史证据（`20261116T171907Z-release-model-readiness/fastwam-verification.json`）里的 OOM 是
**未冻结**配置：PyTorch 已分配 30.15 GiB、总容量 31.74 GiB 后失败。这和上表 72.3 GB 的下界一致，
不是"差一点点"，而是少 38 GB。

## 2. 本轮实际观测

`freeze_video_expert=true` 的加载阶段（`fastwam-stage5-freeze.json`）：

| 阶段 | allocated | reserved | peak_allocated | 说明 |
| --- | --- | --- | --- | --- |
| start | 0.0 | 0.0 | 0.0 | 设备 34.08 GB，空闲 33.75 GB |
| loaded | 13.48 GB | — | 25.52 GB | 6020.7M 参数全部 bf16 常驻；峰值含加载期临时 fp32 缓冲 |
| budget | 13.48 GB | — | 25.52 GB | trainable 1020.9M，训练态 12.25 GB |

`reserved` 在加载后被缓存分配器占着，`mem_get_info` 的 "free"（7.91 GB）因此**不能**当作
torch 还能用的量 —— 第一版脚本用 `free` 判定，错误地给出了 blocked。已改为与设备总量比较，
并把 `allocated/reserved/peak` 三者都记录，让判据可复核。

## 3. 1 次真实更新：已完成（冻结配置）

`fastwam-stage5-freeze.json`，B=1、native horizon 32、224×224×2 相机、state 8、
`freeze_video_expert=true`、gradient checkpointing、文本编码器在 CPU：

| 阶段 | allocated | reserved | peak_allocated | 说明 |
| --- | --- | --- | --- | --- |
| loaded | 13.48 GB | 25.85 GB | 25.52 GB | 6020.7M 参数 bf16 常驻 |
| train_mode | 13.48 GB | 25.85 GB | 13.48 GB | 进入训练模式，峰值重置 |
| forward[0] | 16.10 GB | — | 16.13 GB | 激活约 +2.6 GB |
| backward[0] | 15.55 GB | — | 16.16 GB | 反向峰值与正向接近 |
| clip[0] | 15.55 GB | — | 16.16 GB | 梯度有限 |
| optimizer_step[0] | 19.63 GB | — | **21.67 GB** | AdamW 状态 + 参数更新 |
| done | 19.63 GB | — | 21.67 GB | `grad_finite=true`、`optimizer_state_finite=true` |

结论：**该配置在 34.08 GB 的 V100S 上可以完成 1 次更新，峰值 21.67 GB**（余量约 12 GB）。
这不能外推到更长序列/更大 batch/更多相机，也不能外推到未冻结配置（见下）。

### 3.1 一个必须标注的资产事实

`lerobot/fastwam_base` 的 `model.safetensors` 共 1645 个张量，**不含**
`model.mot.mixtures.action.action_encoder.*`、`model.mot.mixtures.action.head.*`、
`model.proprio_encoder.*`；加载时会新建并随机初始化这些层（日志里的
`Missing key(s) when loading model: {...}` 就是它们）。也就是说这个 base 是**骨干**，
不是带训练好动作头的策略。因此：

- 本次更新的**显存数字有效**（形状决定占用，与初值无关）；
- 本次更新的**行为/收敛无意义**，不能当作 FastWAM 已训练成功的证据；
- 上一轮"离线动作 finite、[1,7]"同样只证明管线通，不证明动作质量。

### 3.2 资产重建过程（可复现）

冻结的 VAE 与 UMT5 来自 `Wan-AI/Wan2.2-TI2V-5B-Diffusers`，文本 tokenizer 来自
`google/umt5-xxl`，二者都不在 `model.safetensors` 里，且缓存快照曾被清理。本次用
Hub LFS 摘要确认残留 blob 就是权重本体
（`98d0e81591a412f6284188bb7d5120b8477f187c753b4791ceeec92af08fd631`，
12,041,526,464 B），把它软链回本地 checkpoint 目录，并补 `config.json`、
`policy_pre/postprocessor.json` 后重跑；冻结组件在联网（镜像）状态下重新解析。

## 4. 明确不成立的推断

- **"32 GB OOM，所以 40 GB 够"** —— 不成立。未冻结配置需要 72.3 GB 训练态；冻结配置的缺口
  在激活，实测峰值尚未取得，40 GB 只是比 34 GB 多 6 GB，不能保证覆盖激活。
- **"freeze 之后只剩 2 GB 训练态"** —— 不成立。2.04 GB 是**可训练权重**本身，训练态还要
  梯度与 Adam 两矩，合计 12.25 GB。
- **"加载成功 = 能训练"** —— 不成立。加载只证明权重可读、模块可构造；更新需要额外 12.25 GB
  且要真正跑完 forward/backward/step。
