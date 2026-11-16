# LeRobot 0.7.0 迁移执行状态

生成：2026-11-16（America/Los_Angeles）。本文只写本次实际跑过的结果；机读明细见
`results.json`，M0 输入冻结见 `../20261116T014700Z-m0/`。

## 1. 工作分支与路径

| 项 | 值 |
| --- | --- |
| worktree | `/home/liaozhengyan/Desktop/lelab-lerobot070` |
| 分支 | `codex/lerobot-070-integration` |
| HEAD | `71c5754` |
| 基点 | `47d1754`（原仓库 `codex/ros2-mobile-collection`） |
| 原工作区 | 未修改；活动 `current` 仍指向 `releases/sanitized-20261024-01` |
| 受控入口 | `app` / `lerobot` / `current` 三个链接均解析到本 worktree 内部 |
| 解释器 | `<worktree>/.venv/bin/python`（CPython 3.12.3） |
| 上游来源 | `/home/liaozhengyan/Desktop/lerobot-0.7.0`，1354 文件哈希见 M0 manifest |

## 2. 提交序列

| commit | 内容 |
| --- | --- |
| `ba767e7` | M0：冻结输入、记录 0.6.2 基线 |
| `f66f6eb` / `a41dac4` | M1：重放 `feat/local-bimanual`（805357d、7461286） |
| `a5f4e36` | M1：重放 `codex/training-monitor-review`（3cd1840） |
| `d96abfa` | M2：以完整 0.7.0 替换库基线（tag `pre-lerobot070-port` = a5f4e36 可回退） |
| `24d206e` | M2：SO-101 三层限幅迁回 |
| `684698d` | M2：双臂 + 录制扩展迁回、应用版本约束升到 0.7.0 |
| `71c5754` | M3：FastWAM / FineART-VLA 配置、依赖预检、CLI、tracking、维度门禁 |

## 3. 分项状态

| 维度 | 状态 | 依据 |
| --- | --- | --- |
| 离线集成 | **基本完成，有 4 个未接线点** | app 563 passed / 0 failed / 1 skipped / 2 collection error；库 39 passed；0.7.0 契约 20 passed |
| 真实 FastWAM | **未验收** | 无 GPU；未加载真实权重，仅有配置/CLI 层证据 |
| 真实 FineART | **未验收** | 同上；语言规划（subtask/VQA/autosteer）完全未跑 |
| 语言规划 | **未开始** | 仅把两个 policy 标为 `requires_task` |
| 实机 | **未验收** | 本次无设备与运动授权，未开串口/相机 |
| 部署 | **未执行** | 无候选包；前端生产构建被宿主内存阻塞 |

## 4. 本次真正验证到的行为

1. **三层限幅顺序**（TC-SAF-01/02）：`max_position_deviation` → `calibration_range_margin`
   → `max_relative_target`，数值对齐测试文档（100→10→8），被外力推到 25 时单步返回 23，
   原点在重连前不漂移，双保险仍只读一次串口。
2. **加权采样**（TC-DAT-01）：`episode_weights` 按 episode 权重有放回抽帧，4000 抽样比例落在
   2.6–3.4，同 (seed, epoch) 可复现，`start_index` 续接等于原序列后缀。
3. **双臂契约**：0.7.0 的 `BiSOFollower` 会覆盖子臂 id、不前缀相机键；已改回“子配置 id 优先 +
   碰撞键加 `left_`/`right_` 前缀 + 传播两档限幅”，双臂配置/录制 14 项测试通过。
4. **录制扩展**：`record_loop` 的 `cycle_callback`、`save_sent_action`、
   `events["stop_recording"]` 快停全部迁回，真实 writer + MP4 解码的双臂用例通过。
5. **CLI 契约**（TC-CLI-01）：把生成的 argv 直接交给真实 `TrainPipelineConfig` 解析，
   act / smolvla / fastwam / fineart_vla / pi05 × tracking 开/关 全部成功，
   **任何组合都不再出现 `--wandb.*`**，改走 `--trackio.*`（entity/notes/run_id/mode 只上报不映射）。
6. **FastWAM 维度门禁**（TC-POL-04）：不传 `policy_action_dim` / `policy_proprio_dim` 直接拒绝，
   这两个参数传给其它 policy 也拒绝；声明 12 而绑定 feature 为 6 时由 `validate_features` 报错。
7. **依赖预检**（TC-ENV-03）：`POLICY_EXTRAS` 增加 `fastwam → lerobot[fastwam]`（探针 diffusers）
   与 `fineart_vla → lerobot[fineart_vla]`（探针 transformers）。

## 5. 剩余阻碍（按影响排序）

| # | 阻碍 | 影响 | 下一步 |
| --- | --- | --- | --- |
| 1 | `lelab_monitor.py` + `training_metrics` 未迁回；`tests/test_pause_resume_state.py` 无法收集 | TC-CKP-01～04 全挂，暂停/续训不可用 | 把 659 行的 `utils/lelab_monitor.py` 移植到 0.7.0 的新保存/恢复阶段（`checkpoint_format`、Trackio、EMA 位置都变了） |
| 2 | `async_inference` 在 0.7.0 已删除；`tests/test_observation_sender.py` 按路径加载失败 | 远程推理（TC-INF-01～04）不可用 | 改写为 0.7.0 `remote_inference` + Zenoh，旧 `--server_address` 参数族全部作废 |
| 3 | `DatasetConfig.sample_weights_file` / `eval_episodes` 未迁回 | 组合数据集权重与显式验证集切分丢失（TC-DAT-02 半挂） | 在 0.7.0 的 `DatasetConfig` 上重加，并在 `datasets/factory.py` 读到 sampler |
| 4 | 宿主内存不足（可用 ~1 GB），`vite build` 报 `WebAssembly.instantiate: Out of memory` | 生产 `dist/` 仍是改动前的包，TC-UI-02 半挂、TC-REL-01 阻塞 | 释放内存后重跑 `vite build`，再核对 dist 是否含新 policy 选项 |
| 5 | 无 GPU、无权重 | 真实 FastWAM/FineART 与语言规划无法验收 | 需要对应资源；CPU 上只能做 tiny/fake |

## 6. 环境限制（会影响复现者）

- `ulimit -u` 软硬均为 4096，本机常驻线程已接近上限：**所有 Python 命令必须串行**，
  且必须带 `OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1`，
  否则 `import lelab.server` 会因为 OpenBLAS 无法建线程而段错误（M0 已记录）。
- `torchcodec` 装得上但加载不了（缺 `libavutil`），lerobot 自动回退 `pyav`。
- 已验证的基线（0.6.2）失败项：app 1 个陈旧断言、库 8 个 fixture 漂移；两者均已在本分支修掉。

## 7. 重跑

```bash
export R=/home/liaozhengyan/Desktop/lelab-lerobot070
export PY="$R/.venv/bin/python"
export BLAS="OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1"

env -u PYTHONPATH $BLAS "$PY" -c 'import lerobot,importlib.metadata as m;print(m.version("lerobot"))'

cd "$R/app" && env -u PYTHONPATH $BLAS "$PY" -m pytest tests -q --continue-on-collection-errors
cd "$R/app" && env -u PYTHONPATH $BLAS "$PY" -m pytest tests/test_train_cli_contract_070.py -q
cd "$R/lerobot" && env -u PYTHONPATH $BLAS "$PY" -m pytest \
  tests/robots/test_so100_follower.py tests/datasets/test_sampler.py -q
cd "$R/app/frontend" && node node_modules/typescript/bin/tsc -p tsconfig.app.json --noEmit
```

`node_modules` 目前是指向 `/home/liaozhengyan/Desktop/lelab-bimanual/app/frontend/node_modules`
的符号链接（package.json 完全相同），不属于提交内容。

## 8. 回退

| 目标 | 操作 |
| --- | --- |
| 回到 0.7.0 之前的分支态（含双臂 + 监控，库仍是 0.6.2） | `git -C $R reset --hard pre-lerobot070-port` |
| 只回退 0.7.0 基线替换 | `git -C $R revert d96abfa`（会产生大量冲突，建议用上面的 tag 而非 revert） |
| 丢弃整个迁移分支 | 删除 worktree：`git -C /home/liaozhengyan/Desktop/lelab worktree remove $R`；原仓库与活动 `current` 从未被改动 |
| 恢复共享环境 | 迁移使用独立 `.venv`，原有 `/home/liaozhengyan/Desktop/lelab-bimanual/.venv` 未被写入 |
