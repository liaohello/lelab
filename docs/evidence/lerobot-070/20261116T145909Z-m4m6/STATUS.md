# LeRobot 0.7.0 迁移续作状态（M4/M5）

生成：2026-11-16（America/Los_Angeles）。本目录只记录本次实际执行的命令与结果；
逐 ID 明细见 `results.json`，输入冻结见 `../20261116T014700Z-m0/`，前序 M1–M3 见
`../20261116T1010Z-m1m3/`。

## 1. 本次新增代码

| 文件 | 作用 |
| --- | --- |
| `app/lelab/utils/inference_070.py` | 0.7 远程推理适配器：operator-owned `DeploymentSpec`、服务端 YAML、remote-rollout argv、动作边界门禁、非阻塞观测桥 |
| `app/lelab/remote_inference.py` | 云端入口改为 `lerobot-policy-server` + Zenoh 回环 + `--inference.type=remote`；旧 `--wandb`/async 参数不再生成 |
| `app/lelab/jetson_policy.py` | Jetson 服务脚本改用 0.7 server；shadow/execute gRPC 客户端显式拒绝，只读 observe 保留 |
| `app/lelab/rollout.py`、`app_ros_mobile/lelab/rollout.py` | 请求新增 deployment/semantics/dims/refill/timeout；旧分块调参在 remote 模式明确拒绝 |
| `app_ros_mobile/lelab/remote_inference.py` | ROS 应用同一套 0.7 远程命令契约（单臂） |
| `app/frontend/.../InferenceModal.tsx`、`inferenceApi.ts` | 远程部署身份、单位语义、维度、超时表单；提交前门禁 |
| `app/pyproject.toml`、`app/uv.lock` | 增加 `remote` extra，锁文件含 eclipse-zenoh 1.10.2 |
| `app/deploy/package_lerobot070_candidate.py` | 候选包生成 + 解包/校验/篡改拒绝/恢复演练 |

## 2. 验证范围

- 应用完整测试：`589 passed / 0 failed / 1 skipped`。唯一 skip 是 `test_jetson_policy.py`
  模块级 gRPC/async_inference 依赖缺失；替代守卫用例在 `test_inference_070_jetson.py` 实际执行 4 项。
- 库本地定制：`54 passed`（follower 三层限幅、sampler、monitor overrides）。
- 0.7 策略与 parity：`48 passed / 1 failed / 3 skipped`。失败为 VAE 空构建在 4 GiB
  `RLIMIT_AS` 下 CPU allocator 失败，单测复现；CUDA 专用项 skip 不计通过。
- 0.7 远程推理：`161 passed / 2 skipped`；三个显式回环/断连/取消用例单独执行均 PASSED。
- ROS 应用：`812 passed / 10 failed`（详见日志）。其中 1 项是旧 CLI 断言，已按 0.7
  契约修正并单独复验 9/9 通过；其余 9 项为 avcodec 内存、线程上限、缺历史证据文件、
  estop 409 和 homing 断言，按失败记录，不写成通过。
- 前端：本机 tsc 通过；本机 Vite 因 undici Wasm 在 4 GiB 地址空间限制下无法启动，
  改在 g128（node v24，无该限制）以同一份源码和依赖构建，exit 0，产物已回传并确认包含新表单字段。
- 候选包：1678 文件；解包、逐文件哈希、篡改拒绝、恢复后复验全部通过。原工作区与原 `current` 未改动。

## 3. 状态分离

| 维度 | 状态 |
| --- | --- |
| 离线集成 | 完成（有上文列出的环境失败与未覆盖组合） |
| 真实 FastWAM | 未验收：无本地 GPU；远端 V100S 已被他人占用 23/32 GB；未找到对应权重 |
| 真实 FineART | 未验收：无 GPU、无对应权重 |
| 语言规划 | 未开始：只有任务字段透传与动作模式预检，不宣称 planner 已训练 |
| 实机 | 未验收：本次没有设备观测或运动授权 |
| 部署 | 未执行：只生成候选包并本地演练回退，未切换活动入口 |

## 4. 复验命令

```bash
R=/home/liaozhengyan/Desktop/lelab-lerobot070
P=$R/.venv/bin/python
B='OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1'
cd "$R/releases/sanitized-20261024-01/app" && env -u PYTHONPATH $B $P -m pytest tests -q
cd "$R/releases/sanitized-20261024-01/lerobot" && env -u PYTHONPATH $B $P -m pytest \
  tests/robots/test_so100_follower.py tests/datasets/test_sampler.py \
  tests/configs/test_training_monitor_overrides.py -q
cd "$R/releases/sanitized-20261024-01/lerobot" && env -u PYTHONPATH $B $P -m pytest \
  tests/remote_inference -q -ra
cd "$R/app_ros_mobile" && env -u PYTHONPATH $B $P -m pytest tests -q -ra
```

前端与候选包：

```bash
cd "$R/releases/sanitized-20261024-01/app/frontend"
node node_modules/typescript/bin/tsc -p tsconfig.app.json --noEmit
# Vite build 在有足够地址空间的 Node 主机执行，再将 dist/ 同步回该目录。
env -u PYTHONPATH $P "$R/releases/sanitized-20261024-01/app/deploy/package_lerobot070_candidate.py" \
  --worktree "$R" --output "$R/docs/evidence/lerobot-070/20261116T145909Z-m4m6"
```

## 5. 回退

| 目标 | 操作 |
| --- | --- |
| 仅回退本次 M4/M5 改动 | 使用本分支 `d7eef1d`（M0–M3 已完成点）作为基线重建分支；本次改动未自动合入 main |
| 回到 0.7 升级前 | `git -C "$R" reset --hard pre-lerobot070-port`（会丢弃双臂/监控/0.7 全部迁移） |
| 丢弃整个迁移分支 | `git -C /home/liaozhengyan/Desktop/lelab worktree remove "$R"`；原工作区和活动 `current` 从未改动 |
| 候选包不可用 | 不发布 candidate.tar.gz；活动 `current` 保持 `releases/sanitized-20261024-01` |
