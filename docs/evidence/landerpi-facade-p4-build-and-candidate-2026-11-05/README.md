# P4 隔离构建、契约回归与版本化候选包

工作日期 2026-11-05（用户时区 America/Los_Angeles），实测 2026-11-06 UTC。
范围：交接文档第 5 节 P4。所有构建都在本任务已有隔离环境里执行；未安装全局依赖，
未烧录固件，未部署，未推送，未切换 `current`。HEAD 仍是 `e4312f0`。

## 1. 受影响契约回归（只跑改动与风险相关的文件）

| 位置 | 命令 | 结果 |
| --- | --- | --- |
| 上位机 Python 3.12 | `app_ros_mobile/.venv/bin/python -m pytest app_ros_mobile/tests/test_hybrid_hardware.py test_hybrid_stream_hardware.py test_integration_upper.py test_t4_hybrid_recording.py test_t4_hybrid_9d_export.py tools/ros2_collection/tests/test_teleop_stream.py test_teleop_frame_v1.py test_container_ops_stop.py test_agent_exclusivity_guard.py -q` | **136 passed, 9 skipped，rc=0**（`reports/pytest-upper-3.12.log`） |
| 车端 Python 3.10 / Humble | 容器内 `python3 -m pytest` 九个车端契约文件（frame_v1 / stream / car_control_process / licence_ipc / frame_arbitration / licence_bridge / recorder_faults / hybrid_frame_link / container_ops_stop） | **121 passed, 31 skipped，rc=0**（`reports/pytest-car-3.10-humble.log`） |
| 前端类型检查 | `PATH=$PWD/work/tools/node/bin:$PATH ./node_modules/.bin/tsc --noEmit -p tsconfig.app.json`（node v22.20.0） | **rc=0，无输出错误**。文档里记的两条历史 TS2551（`src/pages/Training.tsx:438`）现在不再出现，未改动该产品文件 |
| 前端正式构建 | `npm run build`（vite 8.0.16） | **rc=0**，产物 `dist/assets/index-h0UoGB8…` 见下：`dist/assets/index-h0UoIB98.js` + `index-DxwouSln.css`，`dist/index.html` 引用一致。本轮没有修改前端源码，所以哈希与上一轮一致；日志 `reports/frontend-build.log` |
| 静态检查 | `git diff --check`、`compileall -q`（lelab + offline + tests） | 均通过（whitespace 警告只有历史 CRLF 的 `vite.config.ts`）。`ruff` 在本机两个环境都未安装，未执行 |

31 项车端 skip 与 9 项上位 skip 全部是 `skipUnless(HAS_RCLPY)` 一类的环境门，不是被跳过的失败。

## 2. 历史失败独立裁决（没有靠删断言通过）

1. **torque guard** `tools/ros2_collection/tests/test_agent_exclusivity_guard.py:172`
   `test_the_deployed_agent_never_writes_torque_state` —— 改动前实跑 **1 failed, 11 passed, 8 skipped**。
   命中项逐个核到源码：`set_torque(`（agent.py:249）、`_calibration_set_torque(`（agent.py:781、250）、
   `follower_calibration_torque(`（agent.py:877、3261）、`["torque"]`（agent.py:913）。
   它们全部属于**从臂标定作业**这条显式授权路径，并且 `_calibration_set_torque` 的第一条语句就是
   `self._refuse_frame_stream_action('follower calibration torque')`；遥操作/帧流路径里没有 torque 写点。
   处理：把"全面禁止"收窄成可检的三条——非标定命名空间里不得出现 torque 调用、标定写舵机前必须先
   拒绝帧流会话、拒绝函数确实按传输判定；新增 `test_the_frame_stream_path_still_refuses_torque`。
   现在该文件 **12 passed, 9 skipped**。原断言的意图（遥操作绝不松力矩）仍在，标定这条合法路径也不再伪装违规。
2. **`test_remote_inference.py::test_client_command_targets_the_tunnel`** —— 仍然失败，且与本阶段无关。
   证据：该用例断言命令行里是 `…fps: 30}`，实际安装版 LeRobot 现在追加 `backend: 200, fourcc: MJPG`；
   `git status --porcelain` 对 `app_ros_mobile/lelab/rollout.py`、`app_ros_mobile/tests/test_remote_inference.py`
   和 `lerobot/src/lerobot/async_inference/` 全为空（最后一次触及相关测试的提交是 `4beecee`）。
   结论：远程推理链路的既有断言漂移，保留为已知问题，不改测试也不改生产代码。
3. **`test_rosbridge_source.py:171`**（历史上记为时间敏感）—— 本轮单独安静复跑 **通过**
   （`reports/known-failures-recheck.log`：1 failed / 9 passed / 2 skipped，唯一失败就是第 2 条）。
   之前一次失败发生在与故障矩阵并发抢占 CPU 时，不把它写成回归，也不写成"稳定通过"。

## 3. ARM64 与固件：只有构建尝试，没有任何产物

`reports/arm64-probe.json`（全部是本机实测输出）：

- `arm-none-eabi-gcc`、`arm-none-eabi-objcopy`、`aarch64-linux-gnu-gcc`、`qemu-aarch64*`、
  `docker-buildx` 的 `which` 全为 `None`；
- `arm-none-eabi-gcc --version` → `/bin/sh: 1: arm-none-eabi-gcc: not found`；
- `/proc/sys/fs/binfmt_misc` 只有 `python3.12 register status`，没有 ARM 注册；
- 实跑 `docker run --rm --platform linux/arm64 arm64v8/debian:bookworm-slim uname -m`
  → 镜像能拉下来，执行返回 **rc=255，`exec /usr/bin/uname: exec format error`**；
- 厂商固件工程是 Keil ARMCC 5.06u6 + STM32F407VETx（`tools/ros2_collection/vendor_patches/firmware/README.md:41`），
  本机无该工具链；只有原生 host harness 可跑。
- `tools/ros2_collection/local_ros2/bin/doctor` 全 PASS，但它自己声明 pinned deb 集是 amd64。

结论：**本轮没有生成任何 ARM64 包/固件二进制，也没有烧录。** arm64 那套 deb 仍只有
`image.lock.json` 里的摘要记录、未经构建验证。这一条是外部工具链缺口，不是"已完成"。

## 4. 版本化候选包

旧包 `frame-facade-offline-candidate.*`（2026-11-05 18:37 生成）已被本轮源码推翻，**禁止直接部署**：
抽查 5 个文件哈希与当前工作树不一致（`agent.py`、`recorder.py`、`agent_licence_bridge.py`、
`hybrid_hardware.py`、`hybrid_stream_hardware.py`），且清单缺少本轮未跟踪的生产模块。

新包：`work/ros2_collection/facade-integration/package/landerpi-frame-20261106T122111Z.tar.gz`
（sha256 `db4f5a9c2793d239af55f005b8ae07795cbaa57e514417b30d26ed6034d76956`，36 个文件，
`source_head=e4312f0…`，`working_tree_is_clean=false` 如实记录）。

构建器 `tools/ros2_collection/local_ros2/bin/package` 的两处改动：

1. 补齐上位机帧契约清单（`hybrid_session/hybrid_recording/hybrid_facade/hybrid_calibration/
   hybrid_export_schema/hybrid_fixtures/hybrid_gamepad/follower_calibration_asset`），
   ALLOW_LIST 从 28 → 36 条。
2. 新增 `car_closure_gaps()`：走一遍已随包发布的车端模块的第一方 import，任何被导入却没进清单的模块
   直接拒绝打包。变异验证（内存里改清单，不动文件）：拿掉 `licence_ipc.py` 或 `recorder.py`
   立刻报出对应缺口；当前配置为 `[]`。清单里 `hardware_safety.py` 未被闭合检到（没有第一方引用者），
   仍按原样保留。

解包验证 `reports/package-verification.json`：逐文件 sha256 与清单一致
（`hash_mismatches_after_extract: []`）、无缺文件、导入闭合无缺口、全部 Python 可解析。

## 5. 启动 / 停止 / 回退：本机能验的与只能在车上验的

| 动作 | 本轮状态 |
| --- | --- |
| 隔离栈启动/停止 | 实测多次：`sg docker -c 'tools/ros2_collection/local_ros2/bin/docker up'` rc=0（`agent_health_ok`、`DISARMED by construction`）、`down` rc=0 并明确保留挂载数据；P1/P2/P3 都在这个边界内跑 |
| `bin/down` 的停进程 | 实测发现 `sim_camera` 对 SIGTERM 不回话（`term_timeout` 后仍在跑，导致再起一份变成双发布者）。`container_ops.stop_recorded` 现在在身份仍匹配时升级到 SIGKILL 并如实报 `killed_after_term_timeout`；新增 `tools/ros2_collection/tests/test_container_ops_stop.py` 4 项（含"命令不同不杀""pid 复用不杀"两项负向保护） |
| 控制进程重启是否真的活着 | 故障矩阵里 `start-control` 返回 pid 后进程死于 `No module named 'rclpy'`（引号让外层 shell 吃掉了 `$PYTHONPATH`）。改为单引号 + 完整初始化，并新增检查 `control_process_running_after_restart`（ticks 15 → 35 才算活着） |
| 车上部署脚本 | `deploy_readonly_agent.sh --render` rc=0（本地渲染，未碰 Pi，输出保存在 `reports/deploy-render.log`）；`--check` rc=1：`ssh: connect to host <private-ip> port 22: No route to host`，脚本自己声明 "no remote changes made"。**`--apply` / `--stop` / `--rollback` 需要 Pi 可达，本轮没有跑，也不声称跑过** |
| 回退入口 | 旧候选包与其清单原样保留在 `package/` 目录（未删除）；`releases/` 与 `current` 未改动 |

## 6. 剩余真机门禁（候选包不能替代）

1. 真车/真主臂/真串口/真相机接入下的读回、限幅、保持与急停行为（含"停止发包 ≠ 物理停止"）。
2. 帧模式急停是否必须落到车端 `estop_latched`（本轮实测：不落，见 P2 README §3）。
3. 网页急停后如何恢复会话（本轮实测：清除急停未离开 estop、重开会话 60 s 内回不到 ready，见 P1 README §3）。
4. 跨主机网络路径的真实时延/抖动/丢包与 9–10 Hz 是否维持（P3 只有同主机 loopback）。
5. Pi 上 arm64 构建与 `--apply/--stop/--rollback` 三步演练。
6. 数据相关：`simulated_offline` 产物不可训练；真实采集需重新标定与逐台相机确认。
