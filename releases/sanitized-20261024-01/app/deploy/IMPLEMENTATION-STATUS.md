# Implementation status — Jetson primary / Windows standby / Train server

This page maps the agreed architecture to the code that implements it, so a
reviewer can check "what is actually wired" without reading every module.

| Area | Where | State |
| --- | --- | --- |
| LeRobot pinned to the local 0.6.2 tree | `pyproject.toml` `[tool.uv.sources]` → `../lerobot` (junction script: `deploy/windows/link-lerobot.ps1`) | done |
| Three-machine profiles | `deploy/config/servers.example.toml`, `deploy/env/{jetson,train,windows}.env.example` | done |
| Hardware control lock | `lelab/hardware_lease.py`, `/hardware/lease*`, `/emergency-stop`, watchdog on startup | done |
| System status API + stream | `lelab/system_monitor.py`, `GET /system/status`, `WS /ws/system-status` | done |
| Storage status API + stream | `lelab/dataset_storage.py`, `GET /storage/summary`, `WS /ws/storage-status` | done |
| System / storage pages | frontend routes `/system`, `/storage` (legacy `/system-storage` keeps both) | done |
| Dataset list + inspection | `GET /datasets/local`, `.../summary`, `.../schema`, `.../files`, `.../integrity` | done |
| Dataset trash / restore | `POST .../trash`, `POST .../restore`, `GET /datasets/local/trash` (`.trash`, never a hard delete) | done |
| Dataset sealing (封存) | `lelab/sealing.py`, `POST/GET /datasets/local/{id}/seal`, `/unseal`, `/seal/verify`; mutation guards in trash/delete/record/repair | done (tests; 真机待验收) |
| Composite datasets (multi-dataset training) | `lelab/composites.py` + `lelab/scripts/build_composite.py`, `POST /datasets/compatibility`, `POST /datasets/composite`, `GET /datasets/composite/status`, `GET /datasets/composites`, trash/restore | done (tests + 本地真实合并; 训练机真机待验收) |
| Weighted sampling across members | lerobot fork: `EpisodeAwareSampler(episode_weights=…)`, `DatasetConfig.sample_weights_file`, job-local `weights.json` | done (tests; fork 升级需重放) |
| Composite smoke script | `deploy/train/smoke-composite-training.sh` (封存→同步→预检→构建→多策略并行→封存复检) | 脚本完成（dry-run + 假 API 全流程验证） |
| Dataset sync to train server | `lelab/remote_sync.py`, `POST .../sync-train`, `GET .../sync-status`, `GET /datasets/sync-status`, `POST /datasets/sync-cancel` | done (dry run + staged rsync + atomic `.ready`) |
| Train-server reachability | `GET /system/train-server`, `POST /system/train-server/probe` | done |
| SSH training runner | `lelab/runners/ssh_remote.py`, `JobTarget.runner="ssh_remote"`, re-attach on restart | done |
| Fallback inference endpoint | `28081` in profiles, `deploy/jetson/open-train-inference-tunnel.sh` | done (needs on-device commissioning) |
| Cloud inference (Jetson robot client + train-side PolicyServer) | `lelab/remote_inference.py`, `POST /start-inference {target:"remote"}`, `GET /inference/remote/server-status`, `POST /inference/remote/server/stop` | done — gRPC probe passes (policy load 2s, 16-action chunk) |
| Remote checkpoints | `ssh_ops.list_remote_checkpoints`, `JobCheckpoint(source="remote")`, remote `policy-config` read | done |
| HF token / mirror / proxy propagation | `SshRemoteJobRunner` (HF_TOKEN via stdin), `LELAB_TRAIN_HF_ENDPOINT`, `LELAB_TRAIN_PROXY`, `deploy/windows/open-train-proxy-tunnel.ps1` | done |
| Model return + `active` switch | `deploy/data/pull-model-from-train.sh` (dry-run by default: staged rsync → remote SHA-256 manifest → verified move → atomic `active` symlink) | script done, UI button **todo** |
| Jetson commissioning | `deploy/README.md` §5/§10 | **todo** — requires the physical device |

## 2026-09-19 运维能力落地（M0–M7）

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| M0 | 前端 HF 登录（任意页面输入 token、三态显示、登出、`<HF_HOME>/token` 持久化） | 真机验证 |
| M1 | SSH 控制面 `lelab/train_server.py` + `GET /system/train-server/status` + System 实时卡片 | 真机验证 |
| M2 | 作业/会话 marker + 28081 三态（free/ours/foreign）+ 用户点击清理遗留会话 | 真机验证（模拟遗留会话被清） |
| M3 | 显存回收加固（client 自退即回收、Stop 直接杀、marker 兜底清理） | 真机验证 |
| M4 | 无上限并发训练 + GPU 手选/自动 + 显存门禁（训练 4096 / 推理 2048，可填） | 真机验证（双作业并发、GPU3 绑定、阈值拒绝、Stop 后显存回落） |
| M5 | `/models` 训练机权重：完整 loss 曲线、保留 3 个、真删中间权重、磁盘水位 | 真机验证（step5 删除释放 591MB） |
| M6 | 自动回传最终 checkpoint + 本机模型仓（保留 10 个、`.trash` 手动彻底删除）+ 远程模型库单 step 下载 | 后端真机验证；UI 待部署 |
| M7 | 远端软删除（`jobs/.trash`）、孤儿查询/接管、重启后可查可停可删 | 后端测试通过（391）；UI 待部署 |

遗留：M6/M7 的前端产物与 M7 后端尚未部署到 Jetson（设备离线）；Windows 数据同步按决定暂不做。

## Deliberate deviations from the draft plan

* **`tegrastats` sampling.** The plan suggested one long-running `tegrastats`
  collector. `lelab/system_monitor.py` instead runs
  `tegrastats --interval 100 --count 1` per refresh, so a crashed API can never
  leave a background telemetry process behind. Costs one short-lived process
  per second on a Jetson; revisit if that shows up in profiles.
* **Dataset size in listings.** `/storage/summary` reports `size_bytes: null`
  per dataset and keeps the aggregate exact; exact per-dataset sizes come from
  `/datasets/local/{id}/summary` with a 30-second cache, so a poll cannot walk
  multi-GB video trees.
* **Generic `/datasets/{id}/...` routes** exist as aliases of the
  `/datasets/local/{id}/...` routes; the local-prefixed form is canonical
  because `GET /datasets` also lists Hub datasets.

## Remaining work

1. **Model return to Jetson.** `deploy/data/pull-model-from-train.sh` performs
   the staged pull, verification and atomic `active` switch. What remains is
   the "部署到 Jetson" action on the training job page (the script can be
   invoked today; `lelab/runners/ssh_remote.py` already records `remote_dir`
   and `remote_host` on the job so the button knows where to pull from).
2. **On-device commissioning.** Follow `deploy/README.md` §10 (UI only →
   camera preview → serial discovery → calibration → 5° motion test →
   teleoperation → recording → sync → smoke training → inference).
3. **Windows takeover rehearsal.** Stop the Jetson services, drop the lease
   (`POST /hardware/lease/release {"force": true}`), move the USB devices, load
   the calibration backup, then start the Windows profile.
