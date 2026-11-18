# FineART-VLA 真实验证（FART-01）— 已打通

主机 g128（V100S-32GB，torch 2.9.1+cu126），`HF_HOME=/home/lfy/lelab-rmr/hf-cache`，
`HF_HUB_OFFLINE=1`。脚本：`fineart_inventory.py`、`parallel_fetch.py`、`run_fineart.py`。
报告：`fineart-verify.json`，日志：`fineart-run.log`。

## 1. 结论

| 阶段 | 状态 | 关键数据 |
| --- | --- | --- |
| `1_integrity` | **passed**（10.8 s） | 15 个文件大小匹配；`model.safetensors` SHA-256 = Hub LFS `9e384c73…dd197` |
| `1b_tokenizer` | **passed** | PaliGemma tokenizer 由 ModelScope 镜像取得，`tokenizer.model` 与 HF LFS 同哈希（`8986bb4f…`），离线缓存可解析（GemmaTokenizer, vocab 257153） |
| `2_load` | **passed**（153.0 s） | `FineARTVLAPolicy` + `FineARTVLAConfig`，**4143.4M** 参数；pre/post processor 从 checkpoint 成功构建 |
| `3_action` | **passed**（1.08 s） | 真实指令 tokenize（`put the red cube into the box`）→ `predict_action_chunk` → **[1, 50, 14]**，finite |
| `4_save_reload` | **passed**（172.2 s） | `save_pretrained` → 重载 → 同 noise 同输入：**max_abs_diff=0.0，逐位相同**（容差 0.02） |

`exit_code=0`，`required_not_passed=[]`。

## 2. 关键事实（都是实测）

- `lerobot/fineart_vla_base` 重定向到 **`lerobot/FineART-VLA-subtask-midtrained`**，
  rev `b6cf4faf…`，8.71 GiB 单文件权重（`9,354,050,752` B）。
- 下载：单流 1.16–2.46 MiB/s，16 路分块 **32.5 MiB/s**，8.71 GiB / 4 分 37 秒，整文件哈希校验。
- 文本 tokenizer 是**独立且 gated** 的 `google/paligemma-3b-pt-224`：hf-mirror 403；
  ModelScope 副本字节一致（见 `MIRRORS.md`），仅用于无 token 环境验证，不替代许可证。
- checkpoint 自带 FAST tokenizer（`action_tokenizer/bpe_tokenizer`，
  `max_action_tokens=384`、`fast_skip_tokens=128`），因此训练侧不需要另拉
  `physical-intelligence/fast`。
- noise 必须在 **padded 空间**（`max_action_dim=32`）采样；用 native 14 维会在
  `action_in_proj` 报 `[50,14] X [32,1024]`。输出再由策略裁回 14 维。
- V100S 上 `torch.compile` 会对每个 denoise step 反复重编译，验证时用
  `TORCHDYNAMO_DISABLE=1`（不影响数值结论）。

## 3. 仍然没有证明的

- **语言规划/子任务监督**：只喂了一条真实 token 序列并出动作，没有评估 recipe、
  字幕或 FAST token CE；"能出动作"≠"语言条件正确"。
- **动作质量**：没有数据集、没有真机、没有基线对比；`[1,50,14]` 只证明管线与形状。
- **SO-101 6/12 维**：checkpoint 的 14 维来自它自己的 config，与 SO-101 无关，
  仍要显式改维度 + 微调 + 实机验收。
- **训练**：本轮只做推理与序列化；FineART 的训练显存/吞吐未测。

## 4. 复跑

```bash
cd /home/lfy/lelab-rmr/closure && HF_HOME=/home/lfy/lelab-rmr/hf-cache HF_HUB_OFFLINE=1 \
  TORCHDYNAMO_DISABLE=1 /home/lfy/lelab-rmr/.venv/bin/python run_fineart.py \
  --checkpoint-dir /home/lfy/lelab-rmr/fineart-ckpt \
  --tokenizer-dir /home/lfy/lelab-rmr/pg-tok \
  --manifest fineart-inventory-g128.json --report fineart-verify.json
```

前置：`scipy`（`lerobot[fineart_vla]`→`lerobot[pi]` 已声明）、`peft`（本机 0.21.3）、
tokenizer 缓存布局与 `HF_HOME` 指向（见 `MIRRORS.md`）。
