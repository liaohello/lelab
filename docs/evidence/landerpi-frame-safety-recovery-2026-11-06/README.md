# FrameLink 帧模式安全闭环：急停闭锁、租约状态、网页恢复与旧连接隔离

日期 2026-11-06（America/Los_Angeles），全部离线：车、主臂、相机、串口均未连接，
不运动、不训练、不推理、不烧录、不提交推送、不切换 `current`。
计划见 `docs/handoffs/2026-11-06-frame-safety-recovery-plan-and-agent-goal.md`（S0→S4）。

## 1. 本轮结论（先给状态）

| 项 | 状态 | 直接证据 |
| --- | --- | --- |
| S0 三个红灯最小复现 | 本轮通过 | `reports/s0-redlights.{json,log}`，退出码 1，逐条原始请求/响应 |
| S1 租约到期清理与 legacy publisher 解耦 | 本轮通过 | `reports/s1-green.{json,log}` 退出码 0；`tests/test_agent_lease_reaper.py` 9 项 |
| S1 页面急停落两层车端闭锁 | 本轮通过 | `reports/s1-green.json`、网页 `round{1,2}_car_side_latched_after_the_page_stop` |
| S1 请求许可/有效许可/实际执行分层带年龄 | 本轮通过 | agent `/status.licence` 新字段 + `reports/s1-lease-reaper.json` |
| S2 有效/过期/已释放 token 三条恢复分支 + 无关客户端拒绝 | 本轮通过 | `reports/s2-token-branches.{json,log}` 退出码 0，17 项 PASS |
| S2 网页连续两轮 急停→清除→释放→获取→预览→对齐→待命→接合 | 本轮通过 | `reports/web-recovery.log` 退出码 0（14 项 PASS）+ `browser/*.png` 8 张 + `browser/web-recovery.json` |
| S3 每帧准入与真正发出前的许可身份/代际复核 | 本轮通过 | `reports/s3-stale-generation.{json,log}` 退出码 0；`tests/test_car_emission_identity.py` 8 项 |
| S3 在线旧 socket 迟到帧被拒且不消耗新序号 | 本轮通过 | `s3-stale-generation.json`：`refused / stream_epoch_retired`，`frames_emitted` 与队列不变 |
| S4 定向回归（上位 3.12 / 车端容器 3.10） | 本轮通过 | `reports/s4-regressions-upper.log` 52 passed；`reports/s4-regressions-car.log` 130 passed 0 skipped；`test_teleop_stream.py`+`test_mobile_leader_reader.py` 37 passed |
| S4 新候选包与逐文件哈希/导入闭合 | 本轮通过 | `reports/s4-package-verify.log`：36/36 哈希一致、36/36 与工作区源码一致，车端 16 个模块在容器 py3.10.12 全部导入成功 |
| 本轮新增两条红→绿变异检查 | 本轮通过 | 关掉 `_deliver_clear()` 的到期分支 → 桥接测试转红；去掉 `_emitter_clear_seen()` 判定 → 上位清除测试转红；两处均已恢复并按 sha256 校验一致 |
| 急停/清除端到端耗时 | 本轮通过（单次实测） | 两轮网页停止→发布冻结 0.1~0.2 s 量级，见 `web-recovery.log` 内 `emitted` 计数；不构成真机承诺 |
| ARM64 交叉构建 / FreeRTOS 固件产物 | 明确受阻 | 见第 5 节条件；本轮无二进制产物 |
| Pi 上 `deploy_readonly_agent.sh --apply/--stop/--rollback` | 真机待验 | 本轮只做渲染与 dry-run |
| 真车两层闭锁、真串口 hold、物理急停回路 | 真机待验 | 软件闭锁 ≠ 硬件急停回路，页面与本报告都不宣称后者 |

## 2. 四个缺口的根因与改动

1. **帧模式租约到期不清理。** `_control_tick()` 在没有 base/arm publisher 时提前 return，
   而帧模式刻意不创建这些 publisher，于是到期回收根本没跑。现在计时器对所有 profile 注册，
   清理条件不再依赖旧输出通道；新增 `_expire_control_lease()` 统一撤权、清身份、清
   `frame_outputs_enabled` 并复位 hybrid 绑定，`_check_token()` 也走同一条路。
   文件：`ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/agent.py`。
2. **页面急停只断上位流。** 车端闩锁依赖 socket 上的 preempt，agent 的 `/control/estop`
   与许可撤销、状态上报现在各自成阶段上报；`status()` 暴露 `stop_stage`，
   缺车端确认写 `unconfirmed`/`failed`，不写"已停"。
   文件：`app_ros_mobile/lelab/hybrid_stream_hardware.py`、`hybrid_session.py`。
3. **清除只确认了许可权威，没确认执行者。** `clear_estop` 先前只看 agent 的 `estop_latched`，
   控制进程仍自报 `latched_fault="estop"`（第一轮网页复现即为此）。现在：
   - 桥接把控制进程自己的 `latched_fault`/`reengage_required`/`last_clear` 转发进 agent
     `/status.licence.bridge.frame_process`，并带 `age_s`；
   - 上位 `_car_clear_estop()` 有界等待**两层**都报告已清，否则返回 `pending_or_failed`，
     会话停在 estop 并把原因给页面；
   - 到期/释放后没有许可快照时，桥接仍会把排队的清除送出去（`_deliver_clear()`），
     否则旧急停会成为无人能解的死路；
   - `clear_estop` 同时清 `estop_latched` 与 `_estop_requested`，`_refresh()` 不再把页面
     拉回 estop；清除回执带 `authorised_by` 且 `motion_granted=false`。
   文件：`ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/agent_licence_bridge.py`、
   `agent.py`、`app_ros_mobile/lelab/hybrid_stream_hardware.py`、`hybrid_session.py`。
4. **旧连接迟到帧只在 hello 时核身份。** 现在准入通过后、真正 `submit_frame` 之前再核一次
   当前许可身份与 stream 代际（`_emission_identity_ok`），不一致就丢帧、记
   `frames_dropped_before_emit` 与 `last_refusal` 并停止跟随，不消耗序号也不发目标；
   清除时同时轮换 windows 与 admission 绑定的代际。
   文件：`ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/car_control_process.py`。

## 3. 状态语义表（本轮实现并实测的判据）

| 场景 | agent（许可权威） | 控制进程（执行者） | 上位会话 | 页面应看到 |
| --- | --- | --- | --- | --- |
| 显式急停 | `control.estop_latched=true`、`frame_outputs_enabled` 有效值 false、记 `estop_owner_client`/时间 | `latched_fault="estop"`、队列清空、`hold` 已发 | 立即停产清队列，`stop_stage` 分列 local/agent/process | 「已闭锁（软件）」，缺回执则 pending/failed |
| 租约到期 | token/身份/`client_id`/`epoch`/输出请求位全清，`frame_lease_live=false` | 许可镜像失效 → `_stop_following`，不再启用输出 | `frame_lease_lost`，state=fault | 「许可过期」，需重新对齐 |
| 普通暂停 | 不改许可 | `paused=true`，队列清 | 关跟随，不自动恢复 | 暂停态，需新参考 |
| 清除急停 | `estop=false`、`authorised_by∈{control_token, prior_owner_recovery}`、`motion_granted=false` | 收到更新代际的清除后 `latched_fault=null`、`reengage_required=true` | 两层都确认才离开 estop，否则 `pending_or_failed` | 「已清除，未授权运动」 |
| 无关客户端清除 | 403 `control_token`，仍保持闭锁 | 不变 | 失败并保留原因 | 拒绝，无后门 |

身份与代际：`session_id`+`session_epoch`+`calibration_bundle_hash` 三者绑定当前控制租约，
`control_generation` 单调（许可与清除各占一代），`stream_epoch` 每次 hello 或清除轮换。
每帧判据顺序为：hello 身份 → 准入（TTL≤200 ms、窗口 250 ms、水位、序号）→ 发出前许可身份与
代际复核 → 可信反馈（≤500 ms）→ 提交。跨进程/跨主机时钟不做直接比较，只用各自单调域与年龄。

## 4. 命令与退出码（本轮实跑）

```text
# 隔离栈（domain 94 / stream 18123 / agent 18131 / web 18288 / bundle ae3bf41b…c62ca）
sg docker -c 'tools/ros2_collection/local_ros2/bin/docker down'      -> 0
sg docker -c 'tools/ros2_collection/local_ros2/bin/docker up'        -> 0
bash work/ros2_collection/facade-integration/restart_harness.sh      -> 0（/ 200，SPA 需 Accept: text/html）

LROS_*... app_ros_mobile/.venv/bin/python \
  tools/ros2_collection/local_ros2/offline/safety_repro.py --only token_branches
  --out reports/s2-token-branches.json                               -> 0（17 PASS）
  第一次运行为 1（红灯：process_latched_after_clear="estop"，reacquire=RuntimeError:estop_latched）

LROS_*... app_ros_mobile/.venv/bin/python tools/ros2_collection/local_ros2/offline/safety_repro.py \
  --only lease_expiry_cleanup --only estop_car_latch --only clear_then_new_session \
  --only stale_generation_online --out reports/s1-s3-final.json      -> 0（21 PASS，最终代码上重验 S1/S3）

LROS_*... work/ros2_collection/facade-integration/browser-env/bin/python \
  tools/ros2_collection/local_ros2/offline/web_recovery_clicks.py --rounds 2
  --out-dir browser --car-doc .../docker/state/car_control_status.json
  --agent-url http://127.0.0.1:18131                                 -> 0（14 PASS）
  第一次运行为 1（"ready session" 等待超时：上一轮 stranger 检查合法保留的闭锁）

app_ros_mobile/.venv/bin/python -m pytest app_ros_mobile/tests/{test_hybrid_stream_hardware,
  test_hybrid_hardware,test_hardware_lease_api,test_ros_mobile_teleop_export}.py -q -> 0（52 passed）
docker exec … python3 -m pytest tools/ros2_collection/tests/{test_agent_licence_bridge,
  test_licence_ipc,test_car_control_process,test_car_emission_identity,test_agent_lease_reaper,
  test_teleop_frame_v1,test_agent_exclusivity_guard}.py -q            -> 0（130 passed, 0 skipped）
app_ros_mobile/.venv/bin/python -m pytest tools/ros2_collection/tests/{test_teleop_stream,
  test_mobile_leader_reader}.py -q                                    -> 0（37 passed）
app_ros_mobile/.venv/bin/python tools/ros2_collection/local_ros2/bin/package
  --out work/ros2_collection/facade-integration/package               -> 0
```

## 5. 候选包与剩余门禁

新候选包 `landerpi-frame-20261106T141033Z`：
`work/ros2_collection/facade-integration/package/landerpi-frame-20261106T141033Z.tar.gz`，
sha256 `25c57aada4ad4a18c628ad9105b9dd946c411a5f68dd5206e43301afb4b6dfde`，36 文件，
`closure_check.car_package_imports_shipped=true`。逐文件哈希全部一致，且与工作区源码逐字节相同；
解压出的车端包在容器 Python 3.10.12（含 rclpy）下 16 个模块全部导入成功。
旧包 `landerpi-frame-20261106T122111Z` 已被本轮源码取代，只作历史快照，不得部署。
候选包通过校验 ≠ 可发布：臂+夹爪这条发布**不依赖**新的轮子/固件协议增量——offline6d profile
仍只发既有 `bus_servo/set_position`，上位 `begin_epoch` 只申请 `arm`/`gripper` 两个通道，
因此固件缺口不阻塞这条 Python/ROS 候选；但下列仍未闭合：

- ARM64 真实执行与消息包交叉构建：本宿主无可用 arm64 工具链/执行环境，
  `exec format error` 即失败判据；需要隔离容器或原生 arm64 构建机，不改宿主全局 binfmt。
- FreeRTOS/`/dev/rrc` 板命令固件二进制：没有合法可用的 ARMCC 工程产物，host harness 不算固件。
- Pi 的 `--apply/--stop/--rollback`、真实串口回读与保持、真实急停：需要实机窗口。
- 数据身份不变：所有导出仍 `simulated_offline`、`training_eligible=false`；普通/export 仍拒绝。

## 6. 本轮操作教训（照做可避免重复踩坑）

- 重启隔离容器**必须带上 `LROS_DOCKER_BUNDLE`**（本任务 `ae3bf41b…c62ca`）。漏掉时控制进程以默认
  `a…a` 启动，hello 立刻 `epoch_or_bundle_not_licensed`——这是环境错，不是产品回归。
- 容器内设置 `PYTHONPATH` 必须写成 `export PYTHONPATH=$C_SRC:${PYTHONPATH:-}`；直接覆盖会屏蔽 rclpy，
  表现为成片 `ModuleNotFoundError: No module named 'rclpy'` 的假跳过。
- 车端源码改完只能 `docker down/up` 之后才算被跑到；每轮判据前先看 `/status` 的进程计数器有没有动。
- `curl` 探 SPA 路由要带 `Accept: text/html`，否则 404 只反映 SPA fallback 的 Accept 判断。
- 本轮把上一份 `reports/web-recovery.log` 覆盖掉了（先写日志、才发现是环境不匹配）；
  该轮的原始结论已由本轮同路径的成功运行取代，`browser/round1-error.png` 保留了那次被闭锁挡住的一帧。
