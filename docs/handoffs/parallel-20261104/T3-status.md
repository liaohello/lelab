# T3 Wave A 状态（2026-11-04）

## 状态与范围

**Wave A 完成：上位机 ROS-free 公共 facade、三通道许可 / (epoch,channel,source) 序号 / deadline 队列、可信反馈门禁、车端无输出 gateway、版本化标定资产与严格策略、T1 接口 fixture、两套独立测试。硬件仍未接通，`outputs_enabled` 恒为 false。** 本轮未 SSH、未打开串口/相机/手柄、未启停服务或容器、未烧录、未操作真实设备，未读取或传播凭据。所有"通过"均为离线 fixture 结论，不构成实机、物理停车或训练数据可用性验收。

改动文件（本会话 T3 归属）：

| 文件 | 性质 |
| --- | --- |
| `app_ros_mobile/lelab/hybrid_channel.py` | 新增：通道许可、序号、deadline 队列、可信反馈门禁、v1 命令构造 |
| `app_ros_mobile/lelab/hybrid_facade.py` | 新增：`HybridPublicFacade`、`validate_asset_strict`、`capability_report` |
| `app_ros_mobile/lelab/hybrid_fixtures.py` | 新增：显式 `simulated=True` 的双层租约 / T1 driver / observer fixture |
| `app_ros_mobile/lelab/hybrid_session.py` | 扩展：`capabilities()/calibration()/recording()` + 503 不可用语义（T4 路由直接 `HybridSession()`） |
| `app_ros_mobile/lelab/mobile_teleop_coordinator.py` | 追加：`get_hybrid_session_facade()`、`configure_hybrid_facade()`、`hybrid_lease_adapters()` |
| `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/hybrid_gateway.py` | 新增：车端 ROS-free 无输出 gateway（`HybridGateway`） |
| `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/contract.py` | 追加：`HYBRID_ENVELOPE_FIELDS` 信封白名单校验 |
| `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/agent.py` | 修改：hybrid profile 由"拒绝启动"改为注入无输出 gateway + 旧执行端 409 `hybrid_no_output` |
| `configs/ros2_collection/hardware-hybrid-manual-v1.json` + 包内同名副本 | 逐字节一致的 `hardware_hybrid_manual_v1` profile |
| `app_ros_mobile/tests/test_t3_wave_a.py` | 新增：32 项上位机测试 |
| `tools/ros2_collection/tests/test_t3_channel_contract.py` | 新增：27 项车端契约测试 |

未改：`ros_mobile.py`（T4 胶水）、`recorder.py`、`follower_feedback.py`、前端、部署脚本、只读/锁底盘 profile。工作区中这些文件存在其他会话的未提交修改（`follower_feedback.py`、`ros_mobile.py` 为 M，`hybrid_recording.py` 与 T4 测试为未跟踪），已保留未回退。

## 冻结的 public facade

上位机导入 `from lelab.hybrid_session import HybridSession` 或 `from lelab.mobile_teleop_coordinator import get_hybrid_session_facade`，**均不依赖 rclpy**（子进程断言 `'rclpy' in sys.modules` 为 False）。

| 方法 | 语义 |
| --- | --- |
| `status()` | 计划 3.3 节字段齐全；`outputs_enabled` 恒 false；`channels.*.permitted` 仅离线许可；`missing_for_following` 含 `hardware_driver_not_integrated` |
| `acquire_session(request)` | 双层租约（upper → car）；car 失败回滚 upper；取消（`KeyboardInterrupt`）走 stop → car release → upper release |
| `release_session(reason)` | 先 `car.stop` 再释放 car、upper；新 epoch 不复用旧序号、不自动跟随 |
| `set_arm_state(event)` | 字符串或 `{"action":...}`；`preview/align/arm/engage/release_clutch/pause_all/estop/clear_estop/enable_base/lock_base/reconnect/calibration_changed/fault` 与 `{"action":"set_phase"}` |
| `ingest_gamepad(sample)` | `{seq, received_mono_ns, axes[vx,vy,wz], deadman}`；中位、新按下、周期输入、掉线均为纯逻辑 |
| `preview()` | 对齐引用 + 手柄意图，无映射后实车目标，无输出 |
| `capabilities()/calibration()/recording()` | T4 路由入口：不可用 → 503（`BridgeError`），状态冲突 → 409 |
| `calibration_capabilities()/calibration_readiness()/validate_calibration()/validate_calibration_bundle()` | 标定能力与就绪；示例/仿真资产必失败 |

`HybridPublicFacade` 在 `HybridSession` 之上追加 `submit_intent(channel, target, sample_seq=)`、`dispatch(channel)`、`trusted_feedback()`、`router`；`submit_intent` 在无可信反馈时抛 `trusted_feedback_unavailable`，`dispatch` 恒 `driver_sent=False`。

失败与回滚：状态、异常、租约获取失败均回滚到无会话；停止先于网络/录包收尾（`stop` → `end_episode` 顺序在测试中断言）；新会话、重连、标定变更一律 park + 清 pending，绝不自动跟随。

## 通道、许可、序号与 TTL

- 三通道 `base/arm/gripper` 独立许可，来源固定 `gamepad` / `so101_leader`，**不存在全局键盘/leader_arm 模式开关**；`ChannelPermits.grant(epoch, *channels)` 的 epoch 不符即拒绝（`epoch_mismatch`）。
- 序号按 `(session_epoch, channel, source)` 独立；坏帧（版本、会话、epoch、标定哈希、来源、TTL、seq 类型、未知信封字段、目标形状/非有限值）在推进序号之前拒绝，因此不消耗序号，随后同 seq 合法帧仍被接受，重复才 `stale_seq`。
- 队列 latest-wins per channel，deadline 由本机单调时钟建立；过期即 purge，`dispatch` 出队时二次检查许可与截止；`begin_epoch/stop/rollback` 清队列并清序号槽，返回 `queue_dropped` / `seq_slots_dropped`（车端 gate 退役时返回 `retired_with_gate`）。
- 车端 `HybridGateway` 另接受 `forward(command)`：剥离本地时间戳、只转发剩余 TTL，绝不重置原截止时间。
- 硬件 mode 只接受 `servo_position_readback`；`command_cache` / `model_placeholder` / `synthetic` / 原厂 `joint_states` 一律不可信，缺关节、错 servo id、非有限值、超范围 raw、超龄、校准哈希不符、`clock_domain` 缺失均报原因而不修复。

## 车端集成

`agent.py` 对 `hardware_hybrid_manual_v1` / `runtime.mode=hardware_hybrid_manual`：

1. 构造 `HybridGateway(self.profile)`（无 driver，`outputs_enabled=False`），不再抛 `RuntimeError`；
2. `actuator_outputs=False`，即使 profile 声明为 true 也被压掉；
3. 旧执行端 `/control/arm|gripper|base|...` 返回 409 `hybrid_no_output`，不落入 simulation/arm-only 路径；
4. `/status` 增加 `hybrid` 段（gateway snapshot）。

`HybridGateway` 自身验证：默认无 driver 时 `dispatch` 恒 `driver_sent=False`；driver 抛错或取消时 `rollback()`（先清队列与许可再 stop）；driver stop 失败时保留原始回滚原因于 `last_error`，另给 `driver_stop_error`，不掩盖。

## 标定资产

`base/follower/leader/mapping/gamepad/camera` + `rig bundle`：三层存储/USB 绑定/LeRobot 主臂复用；车从臂不伪装 `SO101Follower`。严格策略在 `validate_asset` 之上追加：示例/仿真资产拒绝（`simulated_or_example_asset`）、缺或非法测量时间拒绝、超 30 天拒绝（未来时间亦拒绝）、role 不符、schema/kind 不符、缺关节或非有限/非 JSON 值、bundle 内容 hash 与身份绑定失配。`camera_capability` / `firmware_capability` 恒 `unknown`，`firmware_watchdog_verified=False`，`hardware_stop_effect=unverified`，`controller_acknowledged=unavailable`——未知即报告，不猜测为健康。

## 测试与证据

| 命令 | 结果 | 证据 |
| --- | --- | --- |
| `cd app_ros_mobile && .venv/bin/python -m pytest tests/test_t3_wave_a.py -q`（Python 3.12.16） | **32 passed，exit 0** | [wave-a-checks.txt](../../evidence/parallel-20261104/T3/wave-a-checks.txt) |
| `PYTHONPATH=tools/ros2_collection:ros2_ws/src/lelab_mobile_agent work/ros2_collection/envs/ros_jazzy/bin/python -m pytest tools/ros2_collection/tests/test_t3_channel_contract.py tools/ros2_collection/tests/test_t3_hybrid.py -q` | **59 passed（27 新 + 32 既有），exit 0** | 同上 |
| T4 `/ros-mobile/hybrid/*` 路由冒烟（TestClient，无适配器） | status/capabilities/recording/status 200；preview 200 且 `outputs_enabled=False`；acquire 409 `hybrid_adapters_unavailable` | [route-and-regression.txt](../../evidence/parallel-20261104/T3/route-and-regression.txt) |
| `py_compile`（agent.py、contract.py、hybrid_gateway.py、`hybrid_*.py`、coordinator） | exit 0 | 同上 |
| 车端不引入 ROS | 子进程 `import lelab_mobile_agent.hybrid_gateway` 后 `'rclpy' in sys.modules == False` | `test_default_gateway_has_no_output_and_reports_unknown_capabilities` |

覆盖：租约回滚（car 失败、取消）、停止先于收尾、新 epoch 清队列/清序号、无输出、坏帧不消耗序号、过期不派发、剩余 TTL 转发、可信反馈九类篡改、两输入并发、grasp 锁底盘、重连/标定变更不自动跟随、手柄中位/新按下/周期/掉线、标定严格策略与 bundle hash 绑定、信封字段白名单。

**不属于本会话的既存失败（已确认非本次引入，未修改他人文件）**：

- `app_ros_mobile/tests/test_t4_hybrid_recording.py` 3 项失败：`hybrid_recording.py`（未跟踪）与 `ros_mobile.py`（M）在本会话运行期间被 T4 持续改写（mtime 08:32 / 08:34），其路由已改为 `HybridRecordingController` 而非 facade，且 `_check_exportable` 与自身 fixture 元数据不一致。属 T4 在途工作。
- `tools/ros2_collection/tests/test_mobile_teleop.py` 3 项失败（`FeedbackStore` / `LateReplyGuard`）：源在 `follower_feedback.py`，该文件被其他会话标记为 M；按分工 T3 不改此文件。

## 接口变更请求（供总协调裁决）

1. **epoch 类型**：上位机 `HybridChannelRouter` 同时接受非空字符串与 52 位整数 epoch（拒绝 bool/float/None），车端 `HybridCommandGate` 只接受整数。请统一 v1 规范，否则跨端必须做一次显式转换。
2. **信封白名单（新增，需通知 T1/T4）**：两端现在都拒绝 `received_mono_ns` / `deadline_mono_ns` 及任何未知字段（上位机 `unexpected_field`，车端 `unexpected_envelope_field` / `local_clock_fields_forbidden`）。若 T4 需随命令携带额外审计字段，必须先进白名单。
3. **503 vs 409**：`facade.recording/calibration` 在"没有会话/没有存储"时抛 `BridgeError(503)`（部署缺口），仅在"有会话但状态顺序不对"时 409。T4 路由若自行接管 recording（当前已改为 `HybridRecordingController`），需在控制器内实现同一语义。
4. **agent.py 行为变更**：hybrid profile 不再启动即失败，而是以无输出 gateway 启动并 409 拒绝旧执行端。T1/T4 的启动脚本与健康检查如依赖"启动即失败"，需同步修改。
5. **`forward()` 不证明在途时效**：v1 相对 TTL 无法覆盖网络在途时间，接收端重建本机截止时间会遗漏在途耗时。硬件输出开启前需与 T1 定带时钟不确定度的端到端方案。
6. **标定 schema 仍为 T3 提议 v1**：mapping/gripper 尚未映射到既有 `TeleopMapper` 脉冲参数，不能直接用于真实相对跟随；跨进程 hash 一致性与正式版本迁移待确认。

## 硬件与未验证缺口

| 缺口 | 现状 |
| --- | --- |
| T1 可信 driver | 仅接口 fixture；`HybridGateway(driver=None)` 时无任何输出通路 |
| 车端租约桥 | T1 `HardwareAdapter` 只有 `capabilities/read_snapshot/submit/stop`，缺 `acquire/valid/renew/release`；本会话仅提供 fixture |
| 物理停车 / 急停 | 全部 `unverified`；`stop` 回执不证明物理停止 |
| 相机 / 固件 | `unknown`，未探测；手环/连续旋转硬件未纳入 |
| 真实手柄 | 首轮不打开输入设备；死区/掉线 TTL 无生产默认值 |
| 联调 | 网页视频、sqlite3 原始录包、上位机校验与 LeRobot 导出属 T4/总协调，未在本会话验证 |

运行时只报告 GPT-6 模型族，无法核实具体变体与推理强度是否为项目要求的 GPT-6.1 Sol Max；Wave A 未派生辅助子智能体，全部核验由主智能体完成。
