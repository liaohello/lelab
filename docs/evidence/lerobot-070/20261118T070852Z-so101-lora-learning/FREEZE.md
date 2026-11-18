# L0 冻结记录

Run：`20261118T070852Z-so101-lora-learning`（UTC 2026-11-18T07:08:52Z）

## 1. 源码与工作区

| 项 | 值 |
| --- | --- |
| 分支 | `codex/lerobot-070-integration` |
| HEAD | `1b9fb123db8af25fedeaca3d116525c1a18ed42a`（执行前重新核对，与任务书一致） |
| 未提交差异 | 仅 `docs/evidence/lerobot-070/20261116T171907Z-release-model-readiness/ui_requests.jsonl`（活动预览日志），**未 reset、未纳入本轮提交** |
| `app` / `current` / `lerobot` | 相对链接，解析到 `releases/sanitized-20261024-01/{app,/,lerobot}`；本轮未切换 |
| 解释器 | `/home/liaozhengyan/Desktop/lelab-lerobot070/.venv/bin/python`（3.12.3），`env -u PYTHONPATH` 下 `lerobot` 解析到本 worktree 的 `releases/.../lerobot/src` |
| 关键源码 SHA-256 | `modeling_fastwam.py 26923a42…f04e`、`factory.py 0d6c3fd0…5284`、`app/lelab/train.py dccc71c0…5867`、`pretrained.py f9445f5d…a843` |

## 2. 资产与主机

| 项 | 值 |
| --- | --- |
| g128 GPU | V100S-32GB；开工时空闲（4 MiB 占用），无他人计算进程；`uptime` 负载 3.7 来自 CPU 任务 |
| g128 磁盘 | 261 GB 可用（93%），足以容纳 adapter 与评估输出 |
| 他人进程 | 只读观察；`lfy` 下另有 `bili2text` CPU 进程（非本任务），未停止 |
| 既有预览 | `127.0.0.1:8137`（本机）与 g128 的 `:8138` harness 均为**只读保留**；本轮未停止、未改写其日志 |
| 模型资产 | `lerobot/fastwam_base`（12,041,526,464 B，sha256 `98d0e815…631`）、Wan2.2 组件、umt5 tokenizer 均在 g128 缓存 |
| 数据 | `/home/lfy/so101-wsb/runs/formal_v1/dataset`（只读使用） |

## 3. 本轮新建

- 证据目录 `docs/evidence/lerobot-070/20261118T070852Z-so101-lora-learning/`。
- g128 运行目录 `/home/lfy/lelab-rmr/closure/so101-lora/`（脚本、JSON、日志）。
- g128 派生资产 `/home/lfy/lelab-rmr/assets/so101_left_red_view`（symlink 数据 + train-only stats；
  源数据集未改）。

## 4. 状态索引修正（不重跑已通过模型）

- `../20261117T045658Z-verification-closure/RELEASE-READINESS.md`：删除"FineART 不能出动作"的过时表述，
  改为"只证明加载/动作管线/序列化"。
- 同目录 `case-ledger.json`：`FART-01` blocked → **passed**；`UI-DOM-01` 31 → **37** 条；
  `PKG-01` 指向本轮新候选（app 1682 / ros 1763）。历史失败与被取代的候选目录保留原样。
