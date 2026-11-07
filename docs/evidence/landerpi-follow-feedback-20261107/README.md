# 六轴同时反馈过期：本次只读排查

日期：2026-11-07（America/Los_Angeles）。

用户报错：

```text
frame_follow_failed:HardwareCompositionError:follow_feedback_untrusted:
joint1:stale_or_invalid_timestamp,...,r_joint:stale_or_invalid_timestamp
```

## 结论与证据边界

这是跟随工作线程捕获反馈门禁异常后进入 fault，不能单凭该错误称为 SDK、ROS 节点或
Web 进程崩溃。六轴都来自同一个顺序扫描结果，因此共享链路/缓存超过门限时会一起报错。

`TrustedFeedbackGate`（`app_ros_mobile/lelab/hybrid_channel.py`）以 500ms 为上限，
取报告年龄和上位机本地时间经过量的较大值。该错误同时涵盖超龄和非法/未来时间戳。
本次现场已无法取得故障瞬间的六轴年龄/缓存快照，不能确证是哪一个条件触发。

错误没有包含 sample_invalid，且后续反馈恢复为 fresh；结合源码，比较符合“投影时有效、
缓存被控制线程消费时变旧”的路径。SDK 短时停顿、网络或上位机调度延迟都可能贡献，
不能把其中任何一项当作已经实证的唯一根因。

## 本次实测：没有运动输出

采样文件：`readonly-samples.json`。通过现有本地隧道读取车端 `/status` 与上位机
`/ros-mobile/hybrid/status`，60 对样本，约 12 秒，未获取会话或发送运动/torque 命令。

| 项 | 车端 | 上位机 |
| --- | ---: | ---: |
| 请求失败 | 0/60 | 0/60 |
| 反馈无效 | 0/60 | 0/60 |
| 最大报告年龄 | 0.218995 s | 0.304482 s |
| 最大 HTTP 耗时 | 0.043461 s | 0.295592 s |
| 反馈序号 | 9919→9972 | 9919→9972 |

上述只证明采样窗口内读回持续工作，不能证明运动时绝不会出现抖动。车端和上位机的
请求并行发起，但其快照采样时刻不同，不能拿某一对的年龄差直接当作单向网络延迟。

初次检查：上位机仍 fault，follow active=false；执行进程 outputs=false、paused=true、
queue_depth=0、publish_count=512。后续检查车端 control.active=false、许可和 emitter
outputs 均为 false。机械保持与物理停止效果本轮没有重新验证。

## 确定存在的上位机恢复/呈现问题

1. `HybridSession.status()` 每次进入 `_refresh()`；已有 fault 时仍可能因过期租约再次
   `_stop()`。Car adapter 保留旧 token 时，停止请求返回 403。现场诊断环形队列显示连续
   stop_failed，约与页面轮询节拍一致。这是恢复代码问题，不是 SDK torque 或串口回包。
2. `_refresh()` 会用 lease_lost 替换原始 last_error，之后再追加 stop_failed，导致第一次
   跟随故障原因被后续失败覆盖。follow_runtime.last_error 尚保留第一次反馈错误。
3. `LanderpiTeleop.tsx` 的 faultExplanation 未映射 frame_follow_failed，故将有定义的错误
   显示为“未识别的服务端状态”。这不代表服务端进程已经崩溃。

建议故障时只执行一次幂等停止，保留 primary fault 与 stop/release 结果；后续 GET 只读
观察并展示结果，不使用无效 token 无限重试，不将通信失败伪装成机械停止确认。

## 并发修改事实

排查期间另一会话修改了 `hybrid_stream_hardware.py` 和其测试，新增/接入
`fresh_snapshot()`，在消费晚到缓存前尝试刷新；随后 18287 的运行实例也已被替换，
上位机状态变为 disconnected、outputs=false。主审没有修改这些生产文件、没有重启
服务，也未覆盖其他会话的修改。不能把其他会话的修复写成本轮自行实施或验收的结果。

当前源码与初始运行代码不同，因此本报告只把初始现场快照、采样结果及分别读取到的
代码事实作为证据，不声称当前新实例已经解决原故障。其部署归属与验证需与修改者协调。

## 最小后续

- 在原始反馈保护触发点留存六轴实际年龄、local stamp age、cache age、最近请求耗时、
  采样序号/owner、poll error 和首次异常，先记录后清目标。
- 验证新快照刷新逻辑在既有 500ms 门限内工作，反馈确实过期时仍拒绝；不降低报告年龄、
  不把全 RTT 粗暴减半、不放宽 TTL 或反馈门限。
- 本次读取的新实现中，`_poll()` 先阻塞获取 `_poll_lock`，再发带 timeout 的 HTTP 请求。
  因此 `fresh_snapshot(timeout_s=0.35)` 只限制 HTTP，尚未限制等待后台 poll 的锁；不能
  直接称为整次调用最多 0.35s。需要单独验证锁争用下的调用总时限。这是当前源码风险，
  不是已证明的本次历史故障原因。
- 验证 fault 后反复页面轮询不重复发送 stop、不覆盖第一次故障；补中文错误文案。
- 修复验证后再进行操作员显式接合的短时跟随；本轮没有自动恢复运动。

STM32 SDK/固件未修改、未构建、未烧录；未修改原厂参考树、未提交、未推送、未切 current。

## 后续处理结果（主开发会话）

复核确认，本报告列出的三项恢复/呈现缺陷和缓存刷新锁时限风险均已进入代码修复：

1. `HybridSession._refresh()` 进入 fault 后不再在每次状态轮询时调用旧 token 的
   `_stop()`。首次故障只触发一次停止请求，后续 GET 只读并展示停止结果。
2. 首次故障原因单独保存在 `primary_error`，结构化证据保存在 `fault_detail`；
   停止结果单独保存在 `last_stop`。停止 403 不再追加覆盖
   `last_error`/`primary_error`。
3. `StreamHardwareObserver.fresh_snapshot(timeout_s=0.35)` 的总预算现在同时覆盖
   `_poll_lock` 等待和 HTTP 请求。后台 poll 占用锁时最多等待 350 ms，不再先阻塞
   最多 4 秒再计算 HTTP timeout。
4. `LanderpiTeleop.tsx` 已加入 `frame_follow_failed`、
   `follow_feedback_untrusted`、`stale_or_invalid_timestamp`、`window_expired`
   和 admission timeout 的中文解释，并优先显示 `primary_error`。

验证：

- `app_ros_mobile/tests/test_hybrid_stream_hardware.py`：`35 passed`；
  日志 `recovery-and-budget-tests.log`。
- 前端 TypeScript：exit 0，`frontend-tsc.log`。
- Vite 生产构建：成功，产物
  `app_ros_mobile/frontend/dist/assets/index-FXXfgr76.js`，
  日志 `frontend-build.log`。
- 18287 已重启到新代码，`/landerpi/teleop` 以浏览器 HTML Accept 请求返回 200。
- 实机 10 秒显式接合保持测试：20/20 状态为 `ready`，反馈最大年龄
  `0.256 s`，缓存最大年龄 `0.150 s`，单次刷新最大 `0.055 s`；
  车端 `52` 帧接收、`52` 帧发出、`0` 拒绝。
- 测试结束已解除离合并释放会话；最终 `disconnected`、`outputs_enabled=false`、
  `primary_error=null`。

实机数据：`follow-stability-after-recovery-fixes.json`。本次仍未取得历史故障发生瞬间的
原始样本；新的 `fault_detail` 只从后续故障开始留存。

## 样本失效故障现场（本轮新增）

操作员随后再次遇到 `sample_invalid + stale_or_invalid_timestamp`。这次
`fault_detail` 成功保存了现场：

- 六轴来自同一次扫描 `sample_seq=13263`；
- 车端 `FeedbackStore` 已报告 `feedback_stale:0.504641`；
- 上层样本年龄 `0.561782 s`，所以这不是上层缓存误报；
- 当前车端反馈随后恢复 `reason=ok`、`oldest_age≈0.15 s`，扫描跨度约 `25 ms`；
- 因此当前可确认的是“反馈在运动期间短暂越过了 500 ms 门限”，还不能证明是
  SDK 持续故障、板卡 owner 调度还是 agent 回调抖动。

为避免一次轻微越界直接终止整场跟随，新增了安全的有界恢复：

1. 门禁仍使用 500 ms，未放宽；
2. 只有错误字段明确为 `sample_stale`/`feedback_stale` 时才等待新扫描；
3. 最多等待 `250 ms`，等待期间不映射、不发送旧目标；
4. 新扫描在窗口内到达才继续，否则仍进入 fault；
5. 其它 `sample_invalid`、标定、时间戳或 owner 错误立即拒绝。

新增测试：

- 一次短暂反馈过期可恢复：通过；
- 持续过期在恢复窗口后仍 fault：通过；
- 缓存刷新总时限（锁+HTTP）：通过；
- 实机 15 秒接合保持：30/30 样本 `ready`，最大反馈年龄 `0.245 s`，
  车端 78 帧接收、78 帧发出、0 拒绝，结束后 `disconnected`。

现场证据：
`fault-20261107-sample-invalid.json`、
`fault-20261107-car-side.json`、
`follow-after-feedback-retry.json`；测试日志
`feedback-retry-tests.log`。

## 操作员指定 1.0 秒门限

按操作员要求，当前主从跟随反馈新鲜度门限已从 500 ms 调整为 **1.0 s**。

- 上位机 `FrameStreamHardwareFacade.FEEDBACK_MAX_AGE_S=1.0`；
- 车端 agent 和 `car_control_process` 的 `follower_feedback.stale_after_s=1.0`；
- 1.0 s 仍是最长等待，超过后仍会拒绝并停止；
- 250 ms 的短暂扫描等待仍然保留，等待期间不发送旧目标；
- 其它标定、owner、命令 admission 和输出 watchdog 未取消。

车端版本：
`/home/ubuntu/shared/lelab_car/candidates/landerpi-arm-follow-v24-20261107/landerpi-arm-follow-v24-20261107`。
只重启了 agent 和 control，板卡 owner 与相机未重启。

验证：

- 本地流硬件测试：`37 passed`；
- 实机 15 秒接合保持：30/30 样本 `ready`，最大反馈年龄 `0.245 s`，
  车端 78 帧接收/发出、`0` 拒绝；
- 实机状态确认 `stale_after_s=1.0`，最终 `disconnected`、输出关闭。

数据：
`follow-after-one-second-threshold.json`；
前端构建日志：`frontend-build-one-second.log`。

1.0 s 意味着在极端情况下会允许最多 1 秒的旧反馈继续满足门禁；这是操作员明确选择的
响应性/保护取舍，不应再把它描述成 500 ms 保护。

## 无 token 急停恢复死锁

实际测试发现：网页在没有活动会话时触发急停，会得到
`estop_latched=true`，而普通 `/control/acquire` 在锁存状态下也返回 409，于是既没有
token 也没有原 owner，无法清除。

v24 增加了专用恢复路径：

- `/control/acquire` 只有携带 `recovery:true` 才能在急停锁存时取得临时控制身份；
- 该身份只用于执行一次 `clear_estop`，随后立即 release；
- 普通 acquire 仍返回 `estop_latched` 409；
- 清除后不会开启输出，也不会自动启动/接合跟随；
- 控制板 owner 和相机不重启。

已通过 ROS 环境锁存测试 `10 passed`（`agent-recovery-acquire-tests-ros.log`），并在
无活动会话、输出关闭的条件下成功完成一次“触发急停 → 清除”实机闭环：
上层 `state=disconnected`，车端 `estop_latched=false`、`control_active=false`、
`emitter.outputs=false`。

现场数据：`recovery-deadlock-test.json`。

## 车端外层位置 PID

2026-11-07 已将跟随输出从“直接把上位机目标写给舵机”扩展为车端外层位置 PID。
上位机仍然发送绝对位置目标；车端每帧用六路真实反馈计算：

```text
error = requested_position - measured_position
correction = Kp*error + Ki*integral(error) + Kd*d_error/dt
servo_command = requested_position + correction
```

当前参数：

```json
{
  "enabled": true,
  "kp": 0.3,
  "ki": 0.02,
  "kd": 0.01,
  "deadband_rad": 0.004,
  "integral_limit_rad": 0.03,
  "max_correction_rad": 0.04,
  "derivative_filter_alpha": 0.2
}
```

PID 只在普通跟随生效，回中和单轴试运行仍走各自的状态机。积分抗饱和、最大修正、
校准限位、步进限速、输出 watchdog 和反馈门禁均保留。车端版本：

```text
/home/ubuntu/shared/lelab_car/candidates/landerpi-arm-follow-v25-20261107/
```

输出适配器测试：`23 passed`（`position-pid-tests.log`）。实机 10 秒接合保持
20/20 个状态样本 `ready`，车端 52 帧接收/发出、0 拒绝；最终已释放并关闭输出。
本次没有主动移动主臂，因此 PID 的闭环改善仍需下一次小幅真实跟随运动验证。

## 按当前标定实现行程归一化增量映射

根据当前 SO-101 左主臂标定和 LanderPi 从臂标定，五轴改为按各自标定行程映射：

```text
leader_delta_norm =
  (leader_current - leader_reference)
  / (leader_range_max - leader_range_min)

follower_target =
  follower_reference
  + direction × follower_travel
  × leader_delta_norm
```

主臂行程直接读取
`so101-leader-5B90149723` 的 LeRobot `range_min/range_max`，从臂行程读取当前
`landerpi-follower-v1` 的安全范围。它不是绝对位置归一化，因此对齐瞬间目标等于当前从臂
实测位置，不会因主臂和从臂初始姿态不同而跳变。

2026-11-07 无输出预览验证：

- 六个目标均标记 `normalization=leader_range_to_follower_range`；
- 对齐后每轴 `leader_normalized_delta=0`，目标等于当前从臂位置；
- 例如 joint1 的主臂行程约 `[-1.870, 1.870] rad`，从臂行程约
  `[-2.053, 2.086] rad`；
- 夹爪继续使用主臂 `0–100%` 到从臂脉冲行程的归一化映射。

验证日志：
`normalized-mapper-tests.log`（30 passed）、
`normalized-hybrid-tests.log`（4 passed）、
`normalized-mapping-preview.json`。

## 夹爪绝对端点归一化

操作员将主臂夹爪闭合到约 `0%` 时，从臂仍停在 `r_joint≈351 raw`。原因是旧夹爪逻辑
仍使用“主臂百分比增量 × 2.96 tick/%”；对齐参考已经是 `0.649%`，继续闭合到 0% 只产生
约 2 tick 的目标变化，无法覆盖 200–496 的完整夹爪行程。

夹爪已改为绝对端点归一化：

```text
leader 0%   -> follower closed endpoint 496 raw
leader 100% -> follower open endpoint   200 raw
```

当前映射使用 `direction=-1`、`pulse_min=200`、`pulse_max=496`。为避免接合瞬间跳变：

- 五轴仍要求接合目标与当前实测位置接近；
- 夹爪允许绝对端点目标进入后续车端步进限速；
- 车端仍执行 0.15 rad/s 步进门禁、校准限位和 PID；
- 每帧只发送当前可执行的小步，不直接跳到 496。

实机验证（`absolute-gripper-close-live.json`）：

- 主臂夹爪 `0.649%`；
- 预览目标逐步收敛到 `495.981 raw`；
- 从臂实测 `351 → 389 → 422 → 450 → 488 → 490 raw`；
- 全程 `ready`，无 fault；剩余约 6 tick 为舵机死区；
- 测试结束已释放离合并关闭输出。

测试：`absolute-gripper-tests.log`（31 passed）、
`absolute-gripper-hybrid-tests.log`（4 passed）。

### 夹爪缩放修正

实测主臂夹爪 `57.386%` 时，旧的目标约 `494 raw`。根因是绝对映射把
`pulse_span_per_hundred_pct=2.96` 再除以 100，错误地只移动约 1.7 tick。
该字段实际是每 1% 的 tick 数，修正后应为：

```text
496 - 57.386 × 2.96 = 326.136 raw
```

绝对夹爪模式不再叠加浏览器侧 EMA，页面直接显示 `326.136 raw`；由车端步进限速负责
平滑执行。`348 raw` 约对应夹爪行程 50%，不是 57.386% 的端点比例。

实机打开验证（`gripper-open-final-live.json`）：

- 主臂 `57.386%`；
- 映射目标固定 `326.136 raw`；
- 从臂实测 `492 → 467 → 421 → 389 → 336 → 328/327 raw`；
- 全程 `ready`、无 fault，最终与目标相差约 1–2 tick；
- 测试结束已释放离合并关闭输出。

验证：`gripper-scale-fix-tests.log`（32 passed）、
`gripper-scale-hybrid-tests.log`（4 passed）、
`gripper-scale-fix-preview.json`。
