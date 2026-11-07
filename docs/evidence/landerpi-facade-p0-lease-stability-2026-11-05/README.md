# P0 交接落地：网页 lease_lost 的定位、修复与实测

工作日期 2026-11-05（用户时区 America/Los_Angeles）。运行产物按 UTC 命名，车端 session id 形如 `ros_20261106T…Z`。

范围：只处理 `docs/handoffs/2026-11-05-facade-offline-wrapup-plan-and-agent-goal.md` 第 5 节 P0。全部在本地 Docker/Humble 模拟设备边界内完成；未连接真实小车、主臂、串口或相机，未发送真实动作，未做物理保持或急停验收。

## 1. 结论：失效链已定位到具体代码，而不是凭最后状态猜测

交接文档记录的网页现象是 `lease_lost;stop_failed:HardwareCompositionError`，收尾时又出现 `release_failed:...car_http_403:control_token`。原因是同一条链，403 是后果不是起因：

1. 车端 `recorder.stop_session()` 在整个录包收尾期间持有 recorder 锁：先 `bag_proc.wait(timeout=20)`（recorder.py 内），再遍历 session 目录逐文件 SHA256（同一把锁内）。
2. `MobileAgent.status()` 在持有 agent 主锁时调用 `self.sessions.status()`，而后者必须等待 recorder 锁。于是 `/status` 被排在录包收尾之后，最长 20 s 以上。
3. 同一路径上的 `/control/renew`、`/hybrid/stop` 都要 `with self.lock`，因此一起被拖住；上位机 HTTP 客户端超时 4 s，抛 `HardwareCompositionError`。
4. 上位机 `StreamHardwareObserver` 每 50 ms 缓存一次车端状态。`/status` 一慢，缓存年龄超过 0.5 s，`FrameCarControlLease.valid()` 旧实现直接返回 False（只看缓存，不做任何确认）。
5. `HybridSession._refresh()` 把 `valid()==False` 一律写成 `lease_lost`，随后 `_stop()` 内的 `/hybrid/stop` 又超时，拼出 `lease_lost;stop_failed:HardwareCompositionError`。此时车端租约其实仍然有效。
6. 网页操作员在场心跳只在 `status.state === "ready"` 时运行（`LanderpiTeleop.tsx` 的 effect），且 `heartbeatInFlight` 只允许一个未完成请求。状态一旦落入 fault，心跳永久停止；30 s 后车端租约真正过期。此后 `_check_token` 清空 `control_token`，收尾 release 才得到 403。

即：一次正常的录包收尾被误判为丢租约，误判又导致心跳停发，最终把真租约耗尽。旧字符串只保留异常类型名，所以事后无法区分“被拒绝”和“超时”。

## 2. 本轮改动

- `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/recorder.py`：新增 `status_snapshot()`，锁空闲时读取并缓存分离副本，锁被占用时立即返回上次快照并标记 `recording_status_cached`/`recording_status_cache_age_s`，无缓存时明确返回 `recording_status_unavailable: recorder_busy`。`status()` 本身行为不变。
- `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/agent.py`：`/status` 的 recording 区块改用 `status_snapshot()`，不再在 agent 锁内等待 recorder 锁。
- `app_ros_mobile/lelab/hybrid_stream_hardware.py`：`FrameCarControlLease.valid()` 拆分“租约是否仍在车端”与“观测是否够新”。缓存过期不再直接判丢租约，而是以 1 s 超时向车端直读一次（每 0.5 s 新鲜窗口最多一次，不冲击慢车端）；判定结果带原因 `car_lease_held`/`car_control_inactive`/`car_frame_transport_missing`/`car_session_mismatch`/`car_epoch_mismatch`/`car_bundle_mismatch`/`car_status_read_failed:*`。读不到仍然判 False，保持 fail-closed。观测 0.5 s 新鲜度门禁和 200 ms 帧 TTL 未放宽，跟随仍会被 `feedback_valid_missing` 等真实原因停住。
- `app_ros_mobile/lelab/hybrid_hardware.py`：`_local_state`/`_live_status`/`lease_state` 与 `last_reason`，车端与上位机租约都能说明自己为什么不再有效；`UpperLeaderLease` 同样上报。
- `app_ros_mobile/lelab/hybrid_session.py`：`lease_lost` 改为携带具体来源（例如 `lease_lost:car=car_control_inactive`）；`stop_failed`/`heartbeat_failed` 保留异常文本（截断 200 字符）；新增 `diagnostics`：heartbeat 次数/失败数/最近结果/耗时、操作员在场年龄、两租约剩余 TTL 与原因、观测缓存年龄/轮询次数/最近轮询耗时/最近错误、最近 24 条状态迁移及其原因。状态变更统一经 `_set_state` 记录。
- `tools/ros2_collection/local_ros2/offline/facade_convoy.py`（新增）：在同一生产 facade 会话内量测真实录包收尾期间的 `/status` 延迟、缓存命中来源、心跳续租结果，并断言不再出现 lease_lost。

## 3. 本轮实测

隔离实例（域 94，stream 18123，agent 18131，容器 `lelab-humble-facade-integration`，复用已冻结模拟标定 bundle `ae3bf41b…c62ca`）。

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 上位机 Python 3.12 受影响回归 | 116 passed，退出码 0 | `reports/pytest-upper-3.12.log` |
| 车端 Python 3.10 / Humble 契约回归 | 101 passed，17 skipped，退出码 0 | `reports/pytest-car-3.10-humble.log` |
| 空闲 `/status` 延迟（50 Hz，2 s） | 100 次，p50 1.3 ms，p95 1.7 ms，max 2.3 ms，超 0.5 s 窗口 0 次 | `reports/status-convoy.json` |
| 真实录包收尾期间 `/status` | 收尾 2.103 s；105 次轮询 p50 1.4 ms，p95 1.9 ms，max 2.1 ms，超时 0 次 | 同上 |
| 收尾期间锁被持有的直接证据 | 23 次轮询由缓存快照应答（`recording_status_cached=true`） | 同上 |
| 收尾期间心跳续租 | 4 次尝试全部成功，失败 0 | 同上 |
| 收尾后会话状态 | `ready`，`last_error=null`，`car.last_reason=car_lease_held`，`follow_error=null` | 同上 |
| 诊断字段 | heartbeats=4/0 失败、在场年龄 0.405 s、观测缓存年龄 0.047 s、129 次轮询无错误、状态迁移含原因 | 同上 |
| 收尾缴械 | `car_disarmed=true`，退出码 0 | 同上 |

复跑命令（仓库根目录）：

```bash
export LROS_WORK_DIR=/home/liaozhengyan/Desktop/lelab/work/ros2_collection/facade-integration/local_humble
export LROS_CONTAINER=lelab-humble-facade-integration
export LROS_DOCKER_DOMAIN_ID=94 LROS_DOCKER_STREAM_PORT=18123 LROS_DOCKER_AGENT_PORT=18131
export LROS_DOCKER_BUNDLE="$(app_ros_mobile/.venv/bin/python tools/ros2_collection/local_ros2/offline/facade_smoke.py --prepare)"
sg docker -c 'tools/ros2_collection/local_ros2/bin/docker up'
app_ros_mobile/.venv/bin/python tools/ros2_collection/local_ros2/offline/facade_convoy.py   # rc=0
sg docker -c 'tools/ros2_collection/local_ros2/bin/docker down'   # 本轮结束时未执行，实例仍在本任务标签下运行
```

## 4. 本轮没有做、也不能说成做完

- “修复前”的秒级阻塞数字没有真车/真容器对照：域 93 的旧实例进程内存里是改动前代码，但本轮未在那上面做写入实验，避免动别人的实例。改前行为由代码路径（锁范围）与新增单元用例（持锁时阻塞读 vs 非阻塞读计时）证明，不是模拟冒充实测。
- 未做真实浏览器点击、页面截图、DatasetViewer 逐轴对照，未跑三链路基准、故障矩阵、ARM64 构建和候选包（属 P1…P4）。
- 20 s 收尾上限是现有代码里的真实等待值，本轮只保证它不再拖住 `/status`、`/control/renew`、`/hybrid/stop`；没有缩短它，也没有声称录包收尾本身变快。
- 重新绑定 epoch 与旧 socket 迟到帧的代际保护：车端 `teleop_frame_v1` 已有 `stream_epoch_mismatch` 拒绝并有单元用例（`test_teleop_frame_v1.py:240`），但“重新接合后旧 socket 迟到帧被拒”的在线核验留到 P2 故障矩阵。
- 停止发包不等于物理停止；急停、机械保持、断连仍需按真机条件逐项验证。
