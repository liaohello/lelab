# 实际执行的命令与退出码

工作区 `/home/liaozhengyan/Desktop/lelab-lerobot070`，兼容环境主机 g128（`lfy@`）。
App 运行时在 g128 上跑，原因写在第 3 节；本机 `RLIMIT_AS=4 GiB` 会让 torch 在导入期 core dump。

## 1. 归档验收（本机）

```bash
# App
.venv/bin/python releases/sanitized-20261024-01/app/deploy/accept_candidate.py \
  --archive docs/evidence/lerobot-070/20261117T045658Z-verification-closure/lelab-app-070-20261117T160714Z/candidate.tar.gz \
  --profile app --dest acceptance-run/app3 --reuse-stack --reuse-env \
  --report docs/evidence/lerobot-070/20261120T025431Z-candidate-acceptance/app/acceptance.json \
  --trusted-dir docs/evidence/lerobot-070/20261117T045658Z-verification-closure/lelab-app-070-20261117T160714Z \
  --expect-commit 9f1b73cc9ff8cdf08ba689dc4cccd9a0b6b403dc --worktree .
# exit 1（env.import_ok=false：lerobot.datasets 缺失）

# ROS
.venv/bin/python .../accept_candidate.py --archive .../lelab-ros-070-20261117T160744Z/candidate.tar.gz \
  --profile ros --dest acceptance-run/ros --reuse-stack --skip-env \
  --report .../ros/acceptance.json --trusted-dir .../lelab-ros-070-20261117T160744Z \
  --expect-commit 9f1b73c... --worktree .
# exit 0（归档与篡改检查；env 未建，见 §4）
```

`--reuse-stack/--reuse-env` 是本轮新增的可恢复选项：第一次解包/建环境完成后，
重跑只做校验与探针，避免重复 16 分钟安装。首次构建记录在 `app3/env-prepare.log`：

```
$ uv sync --frozen --no-dev --no-install-project --python .../env/bin/python
exit=1  # 离线缓存缺 httpx2==2.16.2
$ uv sync --frozen --no-dev --no-install-project --python ... --default-index <tuna>
exit=0  # 16m48s，随后固定为 --reuse-env
```

## 2. App 运行时（g128）

```bash
scp -r acceptance-run/app3/stack  lfy@g128:/home/lfy/lelab-accept/app-archived
scp -r acceptance-run/app3-fixed  lfy@g128:/home/lfy/lelab-accept/app-restored   # 仅补回 28 文件

# 原样
ssh lfy@g128 '... python run_app_acceptance.py --stack .../app-archived --env .../.venv \
  --project app --port 8020 --health-timeout 60 --report runtime-archived.json --log server-archived.log'
# exit 1：6 passed / 1 failed（health），子进程 exit 1，ModuleNotFoundError: lerobot.datasets

# 补回 28 文件后
ssh lfy@g128 '... python run_app_acceptance.py --stack .../app-restored --env .../.venv \
  --project app --port 8020 --home /home/lfy/lelab-accept/home --health-timeout 180 \
  --report runtime-restored.json --log server-restored.log'
# exit 0：21/21
```

`--home` 是必要的：单臂远程请求会先要求
`~/.cache/huggingface/lerobot/calibration/robots/so_follower/so101_follower` 存在；
给服务一个隔离 HOME，测试不写用户的真实标定目录。

## 3. ROS 兼容环境（g128）

```bash
ssh lfy@g128 '... python run_pytest_suite.py --project .../ros-archived/payload/app_ros_mobile \
  --tree .../ros-archived/payload --env .../.venv --targets tests/test_t4_hybrid_recording.py \
  --label ros-archived-smoke ...'
# exit 2：1 collection error（lerobot.datasets）

ssh lfy@g128 '... run_pytest_suite.py --project .../ros-restored/payload/app_ros_mobile \
  --tree .../ros-restored/payload --env .../.venv \
  --targets tests/test_t4_hybrid_recording.py,tests/test_remote_inference.py,tests/test_policy_refs.py,tests/test_record.py,tests/test_t4_hybrid_9d_export.py,tests/test_hybrid_capture.py \
  --label ros-targeted ...'
# exit 1：85 tests, 84 passed, 1 failed（estop 闩锁）

ssh lfy@g128 '... run_pytest_suite.py --project .../ros-restored/payload/app_ros_mobile \
  --tree .../ros-restored/payload --env .../.venv --targets tests --label ros-full ...'
# exit 1：833 tests, 824 passed, 9 failed, 2 skipped, 82.8 s
```

PYTHONPATH 必须包含 `app_ros_mobile`、`lelab_mobile_agent`、`lerobot/src` 三者；
少了第二个会在 `from lelab_mobile_agent.recorder import SessionRecorder` 处收集失败。

## 4. 未做与原因

| 项 | 状态 | 原因 |
| --- | --- | --- |
| ROS 独立环境（`uv sync` ROS profile） | 未建 | 与 App 同一套 4 GB 依赖，仅多 `rosbags`；归档原样就因 D-1 无法 import，建环境不会改变结论。兼容回归使用 g128 既有 venv + `PYTHONPATH` 指向解包树，导入根已核对 |
| 目标 ROS 2 启动/节点/日志 | BLOCKED | 两台验收主机都没有 `rclpy`/`rosbag2_py`/`ros2`；见 `ros/target-environment.json` |
| 实机 | 未执行 | 无设备与运动授权 |

## 5. 回退演练（g128）

```bash
ssh lfy@g128 '... python rollback_drill.py \
  --old-dir  /home/lfy/lelab-accept/rel-old        `# 20261117T070209Z + 补回 28 文件` \
  --candidate-dir /home/lfy/lelab-accept/app-restored \
  --env /home/lfy/lelab-rmr/.venv --project app --launcher rollback_launcher.py \
  --root /home/lfy/lelab-accept/rollback-app --port 8021 \
  --report rollback-app.json --log rollback-app.log --health-deadline 180'
# exit 0：16/16
```

## 6. 本轮踩到并修正的自身问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `uv sync` 把 `lerobot.egg-info` 写进被校验的树 | `--no-install-project` 只覆盖根项目，不覆盖 editable 的路径依赖 | 加 `--no-install-package lerobot`；解包树恢复 pristine |
| `uv sync` 选了 Python 3.13 | uv 默认挑最高可用解释器，锁是按 3.12 解析的 | `--python sys.executable`（3.12.3） |
| 探针 `SyntaxError` | 单行 `def` 不能跟在 `;` 之后 | 改成三引号脚本 |
| `/` 判定 `text/html` 失败 | `dict(headers)` 保留原始大小写，取的是 `Content-Type` | 统一小写键 |
| `/training` 404 | SPA 回退只在 `Accept: text/html` 时生效（设计如此） | 导航语义请求 |
| 6 维双臂用例只拿到 422/400 且不是契约错误 | 未配置训练服务器时先抛 “Missing training-server configuration” | 设 `LELAB_TRAIN_HOST/PORT/USER/ROOT`（端口 1，立即拒连） |
| 单臂合法请求 500 “calibration not found” | 先校验标定文件，再走 SSH | `--home` 指向隔离目录并放占位文件 |
| 回退演练 `pointer_points_back_to_old` 失败 | 断言比较了字面量 `releases/old`，实际是绝对符号链接 | 比较解析后的目标 |
