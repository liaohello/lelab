# LanderPi 安全停放姿态与摇操作门禁

日期：2026-11-07。

## 操作员确认的安全姿态

在六轴力矩全部卸载、机械臂由操作员摆稳后，连续六次采样完全一致：

```text
joint1=531
joint2=492
joint3=525
joint4=502
joint5=510
r_joint=457   # ID10，raw tick
```

这组值已成为以后每次恢复和摇操作的默认安全停放姿态。它不是 raw 中点，也不是角度
中点；它是当前小机械臂经过现场确认的安全姿态。

原始连续采样：
`docs/evidence/landerpi-homing-20261107-live/new-safe-pose-samples.jsonl`。

## 固定流程

未来每次从急停、断连或新会话恢复时：

1. 保持底盘锁定，确认 `/dev/rrc` 只有一个板卡 owner。
2. 启动 v17 或更新的版本化 agent/control。
3. 先读六轴真实反馈，再逐 ID 加载和验证力矩。
4. 清除操作员确认过的急停，获取新的 hybrid 会话并持续心跳。
5. 低速逐轴回到上述安全姿态；每段都从新反馈重算，活动轴单帧不超过 15 tick，
   车端硬限制保持 20 tick。
6. 六轴读回与安全姿态偏差不超过 5 tick 后才允许下一步。
7. 用户手动摆放 SO-101 主臂到相近姿态；读取两端实际位置并记录对齐参考，不要求
   两套机械臂 raw 或关节角完全相等。
8. 用户显式执行 align、arm、engage 后才进入摇操作。
9. 任何停止、故障、反馈过期、租约丢失或 owner 变化都撤销执行输出，禁止自动重放。

不允许在回中未完成、反馈不新鲜或未显式接合时直接开始摇操作。

## 当前现场状态

- 急停保持锁存，原因为此前操作员判断车头附近可能受机械限位影响。
- 执行输出为关闭状态。
- 六轴力矩已逐轴卸载并二次读回，全部 `torque=0`。
- 当前安全姿态读回与上述目标一致。
- 已上传但尚未启动 v17 候选。

## 版本与验证

v17 候选归档：

```text
/home/ubuntu/shared/lelab_car/candidates/landerpi-arm-homing-v17-20261107/
SHA256 ba03075be64a79cb711fac45fb713433eee8a93ad998f4e666df0a2d15c2a456
```

相关变更：

- `app_ros_mobile/lelab/hybrid_home_plan.py`
- `app_ros_mobile/lelab/hybrid_stream_hardware.py`
- `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/car_control_process.py`
- `ros2_ws/src/lelab_mobile_agent/config/hardware-hybrid-homing-v1.json`
- `configs/ros2_collection/hardware-hybrid-homing-v1.json`
- `app_ros_mobile/frontend/src/pages/LanderpiTeleop.tsx`
- `tools/ros2_collection/readonly_amplitude_monitor.py`

验证：

- 上位机安全姿态、回中、watchdog、owner 变化和单轴保护测试：`43 passed`。
- 车端 ROS 输出与反馈契约测试：`22 passed`。
- 证据：
  - `docs/evidence/landerpi-homing-20261107-live/safe-pose-tests-final.log`
  - `docs/evidence/landerpi-homing-20261107-live/safe-pose-car-tests.log`
- v17 已上传并解包，但还没有启动或做实机运动验收。

## 只读幅值页面

```text
http://127.0.0.1:18300/
```

页面只允许 GET，不提供运动、使能力矩、卸载、清急停或会话接口。目标标记已经切换为
上述安全姿态。源码：
`tools/ros2_collection/readonly_amplitude_monitor.py`。

## 当前跟随阶段与平滑

2026-11-07 实机确认主臂变化已经能传到从臂，从臂 raw 曾从
`531/492/525/502/510/457` 变化到 `550/516/540/503/510/457`。

为避免正常摇操作被误停，跟随阶段使用独立的
`hardware_hybrid_follow_v1`：

- 上位机映射限速与车端上限对齐为 `0.15 rad/s`；
- 车端对正常跟随的过快目标执行平滑限幅，不再整帧 fault；
- 跟随 watchdog 为 `1.0 s`，仍要求约 10 Hz 的新目标；
- 关节 EMA 为 `alpha=0.35`，输入死区为 `0.004 rad`；
- 夹爪目标使用相同 EMA；
- 机械软限位、TTL、急停和反馈门禁不变。

RTC 在 LeRobot 中属于策略分块推理机制，不适用于当前无策略的
SO-101 主从直连遥操作。当前平滑使用实时 EMA/死区/限速，而不是 RTC。

正式页面：

```text
http://127.0.0.1:18287/landerpi/teleop
```

关闭旧的独立幅值页，避免额外轮询竞争。

### 当前待处理

- 操作员报告有一个关节方向相反。页面“映射方向”可以只翻转对应 follower
  轴；保存后必须重新获取会话、预览并记录对齐。
- 主臂已重新插回，`5B90149723` 可健康读取。
- 自动回安全姿态时，joint1 从 497 移到 526，但 joint2 在 raw 157
  连续 2.5 秒未向 173 移动。当时 joint2 为 `torque=1`、电压回包 8051、
  温度 21 C、位置限位 0-1000；车端实际发出了目标，判断为当前姿态下机械阻力/
  卡滞，而不是软件限位。
- 已保护停止并逐轴卸载六轴力矩，最终 `torque=0`。当前 raw 为
  `joint1=526, joint2=157, joint3=350, joint4=126, joint5=473,
  r_joint=403`。
- 需要操作员先把从臂摆回安全姿态
  `531/492/525/502/510/457`，再重新加载力矩并重新对齐。
- 平滑后的主从方向、抖动和停止响应仍需下一次实机小幅单轴验收。

## 腕部相机与急停恢复（18287 当前实例）

操作员报告 `/ascamera/camera_publisher/rgb0/image` 消失后，重新核验得到：

- `aurora930_node` 和 launch 均在运行；
- 使用容器相同的 CycloneDDS 环境观测到约 `4–15 Hz` 的不均匀真实帧；
- 车端 agent 最新相机年龄约 `0.05 s`，`stale=false`；
- `/ros-mobile/preview.jpg` 返回 `640x400` JPEG，帧序号继续增长；
- 原始画面保存在 `docs/evidence/landerpi-homing-20261107-live/camera-preview-live.jpg`。

此前直接执行 `ros2 topic hz` 使用默认 DDS，出现“topic does not appear to
be published”是观测环境错误。复现相机 CLI 时必须同时使用：

```bash
source /opt/ros/humble/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_LOCALHOST_ONLY=1
```

急停 409 的根因是上层重启后本地 token/owner 丢失，而车端仍按原
`estop_owner_client` 校验。页面清除请求现在携带稳定浏览器身份；若该身份与车端
记录的原 client 不同，上层只从车端有界的 `/status?full=1` 事件记录中查找最近一次
`estop` 之前最近的 `control_acquired.client_id`，再用该记录走原有
`prior_owner_recovery` 分支。清除仍不授予运动权限。

实机结果：

- `/ros-mobile/hybrid/clear-estop` 返回 `200`；
- `car_clear=confirmed`，`authorised_by=prior_owner_recovery`；
- 车端 agent `estop_latched=false`；
- 车端 emitter `latched_fault=null`，`clear_still_queued=false`；
- `outputs_enabled=false`，`reengage_required=true`；
- 六路反馈 raw 为
  `joint1=504, joint2=509, joint3=610, joint4=363, joint5=492,
  r_joint=374`，反馈年龄约 `0.22 s`；
- 相机同步为 `valid=true, stale=false`。

定向测试文件：
`docs/evidence/landerpi-homing-20261107-live/estop-owner-recovery-tests.log`
（`3 passed`）。

18287 当前由 tmux 会话 `lelab-upper-18287` 承载。页面上的 MJPEG 连接会让
Uvicorn 的正常退出等待，因此重启该实例时需要先关闭页面连接或终止旧监听进程；
不要启动第二个 18287 实例。

## 跟随易于 fault 的定位与稳定性修正

2026-11-07 实际出现的两类故障不是机械臂同时损坏：

1. 车端曾记录
   `frame_refused:window_expired:window 9205 closed 1.7 ms ago`。当前跟随是
   5 Hz，即标称帧间隔 200 ms；车端接收窗口原为 250 ms，只剩 50 ms 给 TCP、
   Python 调度和车端 tick，因此正常抖动也能关闭窗口。
2. 随后出现六轴完全相同的
   `stale_or_invalid_timestamp`。故障后实测六路话题仍约 4.5 Hz，agent 反馈也
   立即恢复 `available=true`、`valid=true`。真实发生的是上层 observation cache
   短暂超过 500 ms；`StreamHardwareObserver.snapshot()` 返回了旧缓存，六轴共用
   同一次扫描时间戳，所以六轴一起报错。

辅助测量还显示，原观察器以 20 Hz 拉取约 24 KB 的完整 `/status`。相机进程约占
一个 CPU 核的 68%，Python agent 约占 25%；这类请求压力和 SSH 调度会让后台
观察线程偶发错过 500 ms 门限。

已实施的修正：

- observation cache 默认轮询从 20 Hz 降到 10 Hz；反馈本身上限约 4.5 Hz。
- 跟随、回中和开始回中在缓存超过 200 ms 时执行一次最长 350 ms 的同步刷新。
  如果刷新后真实反馈仍超过 500 ms，原有 timestamp 门禁照常拒绝输出。
- 车端接收窗口由 250 ms 调到 400 ms；命令 `ttl_ms=150`、1 s 输出 watchdog、
  软限位、真实反馈门禁、急停和租约均未放宽。
- 车端 control 进程已以 `--window-ms 400` 重启，agent 和唯一板卡 owner 未重启。

验证：

- 上位机流硬件定向测试 `33 passed`：
  `docs/evidence/landerpi-homing-20261107-live/follow-cache-refresh-tests.log`。
- 实机进行 12 秒、主臂不主动移动的显式接合保持测试，不是仅预览：
  `docs/evidence/landerpi-homing-20261107-live/follow-stability-hold-live.json`。
- 24/24 个状态样本为 `ready` 且 `outputs_enabled=true`；
- 反馈年龄最大 `0.232 s`，观察缓存最大 `0.157 s`，单次轮询最大 `0.043 s`；
- 车端接收、门禁接受和实际发出均为 `62` 帧，`frames_refused=0`；
- 测试结束已 `release_clutch` 并释放会话，最终 `disconnected`、
  `outputs_enabled=false`、`last_error=null`。

这些修正降低的是无效轮询和边界抖动，并未绕过反馈新鲜度。如果真实反馈持续超过
500 ms，系统仍应停止；若继续复发，需要优化车端 `/status` 为轻量反馈接口，而不是
继续扩大阈值。

## 操作员指定 1.0 秒反馈门限

按操作员明确要求，2026-11-07 将主从跟随的反馈新鲜度门限统一改为 1.0 秒：

- 上层 `FEEDBACK_MAX_AGE_S=1.0`；
- 车端 v23 的 `follower_feedback.stale_after_s=1.0`；
- 250 ms 短暂扫描等待和 400 ms 帧 admission 窗口保留；
- 标定、owner、命令 TTL、输出 watchdog 和急停未取消。

车端版本：
`/home/ubuntu/shared/lelab_car/candidates/landerpi-arm-follow-v24-20261107/landerpi-arm-follow-v24-20261107`。
只重启了 agent/control，板卡 owner 和相机未重启。

验证：流硬件测试 `37 passed`；15 秒显式接合保持 30/30 样本 `ready`，
最大反馈年龄 `0.245 s`，车端 78 帧接收/78 帧发出/0 拒绝。
证据：
`docs/evidence/landerpi-follow-feedback-20261107/follow-after-one-second-threshold.json`。

这会把可接受反馈年龄上限从 500 ms 放宽到 1.0 s，是操作员明确选择的运行参数。

同时修复了“无活动会话时网页急停无法恢复”的死锁：agent 支持仅限恢复用途的
`recovery:true` acquire，取得临时控制身份后只允许清除急停并立即释放，不能开启输出。
实机已在无运动条件下完成触发、清除和车主反馈状态核验；普通 acquire 仍保持 409。

车端 v25 增加了普通跟随的外层位置 PID：上位机仍发送绝对位置，车端用真实六路反馈
计算 `target + Kp*e + Ki*∫e + Kd*de/dt`，再经过校准限位和步进门禁后发布。当前参数
位于 `hardware-hybrid-follow-v1.json` 的 `teleop.position_pid`；回中和单轴试运行
不走该 PID。输出适配器定向测试 `23 passed`，实机 10 秒接合保持无 fault；完整运动
跟随验收仍待下一次小幅真实运动。

2026-11-07 又按当前三层标定加入五轴行程归一化增量映射。主臂每轴使用 LeRobot
`range_min/range_max` 转成弧度行程，从臂使用当前标定安全范围；对齐瞬间归一化位移为
零，目标保持当前从臂位置，之后按行程百分比叠加。夹爪继续使用 0–100% 到脉冲区间的
归一化。无输出预览已确认六轴都返回
`normalization=leader_range_to_follower_range`。规范测试 `30 passed`，硬件观察测试
`4 passed`。

夹爪从相对百分比增量改为绝对端点归一化：主臂 0% 映射从臂闭端 496 raw，100% 映射
开端 200 raw。为防接合跳变，允许夹爪目标通过车端步进限速逐步收敛，五轴连续性门禁不变。
实机主臂 0.649% 时，从臂由 351 raw 分步移动到 490 raw，目标 495.981 raw，剩余约
6 tick 为舵机死区；测试无 fault，结束后已释放会话和关闭输出。

随后修正绝对夹爪映射的百分比缩放：`pulse_span_per_hundred_pct=2.96` 是每 1% 的
tick 数，不应再除以 100。主臂 57.386% 现在直接目标 `326.136 raw`，页面不再显示
494 raw。实机打开测试读回 `492 → 467 → 421 → 389 → 336 → 328/327 raw`，无 fault，
最终差目标约 1–2 tick；结束后已释放离合和会话。`348 raw` 约对应 50%，不是 57.386%。
