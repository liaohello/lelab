# FineART-VLA 真实资产与验证状态（FART-01）

主机 g128（V100S-32GB，驱动 535，torch 2.9.1+cu126，`HF_ENDPOINT=https://hf-mirror.com`）。
脚本：`fineart_inventory.py`（清单/预算/限速实测）、`parallel_fetch.py`（分块并行下载+哈希校验）、
`run_fineart.py`（四阶段真实验证）。全部状态按实际执行记录。

## 1. 资产清单（来自 Hub 元数据，不是猜的）

`lerobot/fineart_vla_base` 实际重定向到 **`lerobot/FineART-VLA-subtask-midtrained`**，
revision `b6cf4faf2630f971dd390bc32365e9d9c330da8e`，未门禁，15 个文件：

| 文件 | 字节 | 说明 |
| --- | --- | --- |
| `model.safetensors` | 9,354,050,752（8.71 GiB） | bf16 全量权重，SHA-256 `9e384c73…dd197` |
| `action_tokenizer/bpe_tokenizer/tokenizer.json` | 421,620 | **checkpoint 自带** FAST tokenizer |
| `action_tokenizer/processor_config.json` | 253 | `max_action_tokens=384`、`fast_skip_tokens=128` |
| `policy_preprocessor_step_3_normalizer_processor.safetensors` | 8,320 | 归一化统计 |
| `policy_postprocessor_step_0_unnormalizer_processor.safetensors` | 8,304 | 反归一化统计 |
| `config.json` / `policy_pre/postprocessor.json` / `train_config.json` | 3.9K/780/7.3K/9.2K | 策略与处理器配置 |

文本 tokenizer 不在 checkpoint 里，由配置声明为 **`google/paligemma-3b-pt-224`**
（revision `35e4f46485b4d07967e7e9935bc3786aad50687c`）。

类与注册名（静态核对）：`FineARTVLAPolicy`（`modeling_fineart_vla.py`）、
`FineARTVLAConfig(PI05Config)`、注册类型 `fineart_vla`；训练侧可选
`flashrt/flashrt-adarms-train@v1`、`flashrt/flashrt-flex-attention-train@v1` 内核。
旧 blocker 文档里的 `FineARTPolicy` 类名是错的，已按实际源码纠正。

## 2. 空间与带宽预算（实测）

| 项 | 值 | 来源 |
| --- | --- | --- |
| checkpoint 下载 | 9,354,537,811 B（8.72 GiB，含小文件） | Hub 元数据 |
| 文本 tokenizer | 4 个文件，约 4.4 MB | Hub 元数据（但 403） |
| FAST tokenizer | 698 KB，训练用；checkpoint 已自带 | Hub 元数据 |
| 单流镜像速度 | 1.16 MiB/s（本机 urllib）/ 2.46 MiB/s（g128 urllib）/ 4.7 MB/s（curl 25 s 采样） | `fineart-inventory*.json`、实测 |
| 16 路分块 | **32.5 MiB/s**，8.71 GiB 用 4 分 37 秒 | `parallel_fetch.py` 日志 |
| 下载后磁盘 | 29 GB → 28 GB 可用（盘 99–100% 占用） | `df` |

结论：**"镜像限速 1 MB/s，要 60 分钟"不是 FineART 的固有属性**，是单连接限速；
分块并行后吞吐 32.5 MiB/s。旧 blocker 把 FastWAM 的 UMT5 停滞外推到 FineART 是不成立的，
但新发现的真实阻碍是门禁（下节）。

## 3. 实际执行结果

| 阶段 | 状态 | 证据 |
| --- | --- | --- |
| `1_integrity` | **passed**（10.65 s） | 15 个文件大小全部匹配；`model.safetensors` SHA-256 = Hub LFS 摘要 `9e384c73…dd197` |
| `1b_tokenizer` | **blocked** | `google/paligemma-3b-pt-224` 的 `gated=manual`；文件请求 **403**；无接受许可的 token |
| `2_load` | **failed** | 权重确实从本地目录装载（`Loading weights from local directory`，147 s），随后 **processor 实例化失败**：`action_tokenizer_processor` 需要 `paligemma_tokenizer_name=google/paligemma-3b-pt-224`，离线/无权限取不到 → 策略对象构建不完整 |
| `3_action` | not_run | 依赖 2_load |
| `4_save_reload` | not_run | 依赖 3_action |

报告文件：`fineart-verify.json`、运行日志 `fineart-run.log`。

### 环境缺口（已修）

`action_tokenizer_processor` 需要 `scipy`；g128 的 venv 当初只装了部分 extra，缺 scipy。
`lerobot[fineart_vla]` → `lerobot[pi]` 已声明 `scipy>=1.14.0,<2.0.0`，因此这是**宿主环境缺口，不是打包缺陷**。
已装 `scipy 1.18.1` 后重跑，失败点从 scipy 前移到门禁 tokenizer —— 说明失败原因已定位到唯一的真实阻塞。

## 4. 明确未证明的部分

- **动作模式未验证**：没有产出任何 `[B, chunk, action_dim]` 动作张量，不能声称"FineART 能出动作"。
- **保存/重载未验证**：没有执行 `save_pretrained`（也没消耗额外 9 GB）。
- **语言规划未验证**：recipe/字幕监督、FAST token CE 都没有跑过。
- **SO-101 6/12 维适配未验证**：checkpoint 的 native 维度来自其自身 config，与 SO-101 无关。
- 8.71 GiB 权重的"装载成功"只代表 safetensors 能读入、模块能构造到处理器阶段，
  **不等于**策略可用。

## 5. 解除阻塞的具体条件

1. 在 Hugging Face 上接受 `google/paligemma-3b-pt-224` 的许可，并在主机配置
   `HF_TOKEN`（或把 tokenizer 文件放到本地目录后改 `config.json` 的
   `text_tokenizer_name` 指向本地路径 —— 需要一并改 `policy_preprocessor.json`
   里的 `paligemma_tokenizer_name`，否则处理器仍走 Hub）。
2. 磁盘：当前 28 GB 可用，够下载（已下完），但 `save_pretrained` 再要 9 GB；
   若同时保留 FastWAM（12 GB）需先腾出 ≥10 GB。
3. 复跑命令（已可直接执行）：

```bash
cd /home/lfy/lelab-rmr/closure && HF_HUB_OFFLINE=1 /home/lfy/lelab-rmr/.venv/bin/python run_fineart.py \
  --checkpoint-dir /home/lfy/lelab-rmr/fineart-ckpt \
  --tokenizer-dir /home/lfy/lelab-rmr/fineart-ckpt/_tokenizer \
  --manifest fineart-inventory-g128.json --report fineart-verify.json
```
