# LanderPi 真机联调现场记录

时间：2026-11-05（Pi 本地时区，UTC+8）。

本轮已完成最小臂栈接管、真实六路反馈、SO101 主臂只读探测、相机新鲜度检查和
`/landerpi` 页面读回。**没有释放关节力矩、没有发送夹爪目标、没有底盘运动。**

## 现场版本

- Pi：`raspberrypi`，Debian 12，aarch64，machine-id
  `ad522b778a1541f8b20dd02a8a3660ec`
- 容器：`MentorPi`，`ros:humble`，Python 3.10.12，ROS_DOMAIN_ID 0
- 板型：`LanderPi_Mecanum`
- 最小栈：
  - `/home/ubuntu/lelab_car/vendor/ros_robot_controller/ros_robot_controller_sdk.py`
  - `ros_robot_controller`
  - `controller_manager` / `servo_controller`
  - 独立 Aurora 相机
- agent profile：`hardware_hybrid_manual_v1`
- 上位机页面：`http://127.0.0.1:18286/landerpi`
- SSH 隧道：WiFi `<private-ip>`，keepalive + systemd 自动重启

有线地址 `<private-ip>` 与 WiFi 地址 `<private-ip>` 本次确认指向同一 Pi。
有线链路实测约 50% ICMP 丢包；WiFi 0/10 丢包，因此本轮页面隧道使用 WiFi。
已用 `/status`、真实相机帧和六路反馈确认两个地址下的服务身份一致。

## 实际结果

最小栈启动后，`/controller_manager/joint_states` 实测 47.912 Hz，194 个样本。
可信位置来自原厂 `GetBusServoState`，不是 joint-state 命令缓存。

首次六路真实脉冲：

```text
joint1=155
joint2=83
joint3=533
joint4=94
joint5=499
r_joint=229
```

六路扫描跨度约 0.052 s，所有样本 age 小于 0.5 s。`/landerpi` 页面显示
“六路正常”，相机显示“画面正常”，执行输出显示“执行输出关闭”。

SO101 主臂只读探测：

```text
port=/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149652-if00
claim=flock+tioxcl
reads=5 / failures=0
shoulder_pan=-0.052168 rad
shoulder_lift=-1.832788 rad
elbow_flex=1.683955 rad
wrist_flex=1.067144 rad
wrist_roll=0.017645 rad
gripper=2.477291 %
```

当前三层标定状态：主臂 A 层已加载，唯一缺失项为
`follower calibration missing for identity 'landerpi-follower-v1'`。
因此 `feedback.valid=false` 是预期的安全门禁结果；不能把“六路可读”误写成
“已完成 B/C 层或已允许执行”。

联调中发现 `joint3` 返回合法的带符号 `int16` 值 `-49`，但原厂 ROS
`BusServoState.position` 是 `uint16[]`。旧实现把负值直接赋给 ROS 字段，
触发 `AssertionError` 并杀死唯一 UART owner。修复方式：

- 节点用二补码无损编码 int16，agent 收到后还原为 `-49`；
- 标定观察范围扩展为 `-1000..1000`；
- 夹爪目标范围仍保持 `0..1000`；
- 非负高位、布尔值及无法表示的字段仍在赋给 ROS 前被拒绝。

部署后的真实六路为：`495 / 638 / -49 / 266 / 473 / 227`，页面可显示全部
六个关节。该修复的离线回归为 `116 passed, 13 subtests passed`；上位机标定/契约
测试为 `67 passed`。节点 SHA256：
`fecf08bddc535ea4b26e59c3710646dbde6daa4f111c08fb7fbca78a58c4a30d`。

原驱动在偶发超时后还会永久锁存读故障。修复后的节点在失败响应后排空串口队列，
保持独占读锁并等待 200 ms 静默；只有静默窗口内没有迟到帧时才解除锁存。修复部署后
连续 60 秒监控得到 30/30 次完整六路反馈，期间恢复计数为 237 次成功、1 次失败，
没有再要求整栈重启。

夹爪滑块在任务开始前、五轴范围未达标或仍有轴释放力矩时保持禁用。页面现在直接
显示禁用原因，并根据当前 `r_joint=227 raw` 提供只用于方向验证的窄窗建议
`177–277`，不会自动填写或代替现场确认。

用户现场确认可自行观察机械边界后，夹爪安全窗跨度上限已从 400 tick 提升到
供应商完整软件范围 1000 tick，页面提供 `0–1000` 填充按钮。目标仍限制在
`0..1000`，以不超过 10 Hz 发送，并在连续跟踪误差或位置卡滞时故障停止。夹爪现在
可以先于五轴范围标定；五轴保持力矩，夹爪结束后若五轴仍未完成则自动回到手动范围
采集。此变更离线测试为 `118 passed, 12 subtests passed`，上位机相关测试
`32 passed`，TypeScript 和正式构建均通过。

随后真实拖动暴露了 `gripper_tracking_error` 的误判：大范围滑块目标下，旧逻辑在
当前值与最终目标相差超过 18 tick 时连续 5 次即报错；实测夹爪其实持续移动
（反馈由 171 增至 376），却很快被错误中止。现改为按进展判定：目标发布后允许
爬行，只有误差在 1.5 s 内没有至少 2 tick 改善才故障停止。真实单轴往返验证：

```text
376 -> target 396 -> feedback 404 -> error 8
404 -> target 376 -> feedback 384 -> error 8
phase=gripper_drag throughout; no tracking fault
```

该往返只控制 ID10，五轴保持力矩；验证后正常 `stop`，没有生成或声称标定完成。
车端任务状态机回归：`53 passed, 12 subtests passed`。

第二次现场 fault 为 `gripper_feedback_lost`。当时六路数据仍新鲜，但标定 worker
把单次串口读取失败立即升级成 fault。现改为单次失败时先暂停发送新目标并重试，
只有连续 3 次或持续 1.5 s 仍无反馈才故障停止。更新后真实三目标往返：

```text
264 -> target 304 -> feedback 297 -> error -7
297 -> target 224 -> feedback 228 -> error 4
228 -> target 264 -> feedback 259 -> error -5
phase=gripper_drag throughout; no fault
```

状态机回归更新为 `54 passed, 12 subtests passed`。这三次运动仍只涉及 ID10，
结束后正常停止；这些是通信/跟踪验证，不冒充完整夹具开合标定。

第三次现场 fault 是从开端一次跳到闭端后触发 `gripper_tracking_error`。根因是
目标 TTL 已经过期、用户已经松手后，跟踪器仍要求夹爪必须到达旧目标。现改为目标
过期后停止追目标判定，保持当前位置供稳定采样和端点标记。真实大跨度复测：

```text
528 -> target 0 -> 0.5s feedback 110 -> 1.0s feedback 4
保持 3.0s：feedback 3/4，phase=gripper_drag，无 fault
再 target 103 -> feedback 101，error -2
```

随后正常停止。该结果证明从 `528` 跨到接近 `0` 并按松手后的过期目标保持，不会再
误报 tracking fault。

另一次现场 fault 发生在夹爪拖动期间直接用手移动五轴。旧实现把受保护轴移动
视为 `non_target_joint_moved` 并进入 fault。现改为安全交接：立即停止夹爪目标、
恢复六路采样并切回 `manual_range`，保留已有夹爪标记，允许继续手动五轴范围采集。
对应状态机测试仍为 `54 passed, 12 subtests passed`。

按现场要求新增五轴范围手动录入，页面默认值来自操作员提供的数据：

```text
joint1: 10..998
joint2: 10..961
joint3: 0..990
joint4: 100..980
joint5: 80..910
```

该路径在 evidence 和最终资产中标记为 `operator_manual_entry` /
`operator_entry_not_mechanical`，不会伪造 30 个自动采样点或噪声统计。第一版
联调仅提交了五轴范围并读回 `arm_valid=true`，随后正常停止；该次不包含关节或
夹爪动作。车端状态机测试 `56 passed, 12 subtests passed`，上位机标定 API
测试 `32 passed`，TypeScript 与正式构建通过。

随后按现场要求扩展为六轴全部手动设置。夹爪使用操作员确认的闭端 `504 raw`、
开端 `192 raw`，不再要求两轮拖动；目录和资产明确标记
`operator_manual_full_entry`，不声称完成自动重复性测试。

真实生成结果：

```text
follower: landerpi-follower-v1
schema: 2
version: 054e46596cb5
asset sha256: 054e46596cb5a4287ef00434e3af0b4c202422e74fde6c6ceec3d475d8c3bb83
path: work/ros2_collection/hardware-20261104/calibration/follower/landerpi-follower-v1.json
```

提交和页面读回均成功，`mapping_invalidated=[]`。全部手动测试：车端
`59 passed, 12 subtests passed`，上位机标定 API `32 passed`，TypeScript 与正式
构建通过。

## C 层映射与无输出控制台

已保存 C 层：

```text
identity: landerpi-so101-mapping-v1
version: b490c46b5b6b
leader: so101-leader-5B90149652
follower: landerpi-follower-v1
method: operator_semantic_mapping_pending_preview
```

三层 bundle 已就绪：
`b52ed1134b20736a6720fe135638aa5e15418843846847ed7eea30392814226d`。

主臂 → 从臂关节映射为：

```text
shoulder_pan  -> joint1
shoulder_lift -> joint2
elbow_flex    -> joint3
wrist_flex    -> joint4
wrist_roll    -> joint5
gripper %     -> r_joint / ID10
```

新增 `/landerpi/teleop` 控制台，采用视频 + 主臂/预览目标/从臂实测的大卡片布局，
包含获取会话、无输出预览、记录对齐、暂停、急停和释放会话。浏览器流程已实际通过：

- 主臂在线、六路从臂反馈、相机和三层标定均通过；
- 五个预览目标均显示，执行输出保持关闭；
- 映射身份显示正确；
- 页面脚本错误为空；
- 测试结束后已通过页面释放会话。

截图与机器结果：`browser-teleop-console-final.png` / `.json`。

现场反馈“完全不跟手、相机一直闪”的根因与修复：

- 控制台原先没有续租心跳；默认 60 秒租约到期后会话进入 fault。现在页面每 3 秒
  调用 `/ros-mobile/hybrid/heartbeat`，实测 69 秒、23 次心跳均为 `200`，
  `state=ready`、主臂健康和双层租约 23/23 保持。
- 相机原先每 200 ms 重建一次 JPEG `<img>`，加载空窗造成闪烁。已改为连续
  `/ros-mobile/preview.mjpg` 流，状态轮询为 350 ms。
- 当前按要求仍处于无输出预览，因此 SO-101 移动应改变预览目标，但从臂舵机不会
  物理跟随。真实跟随必须在方向检查完成后单独开放。

控制台现场修正：

- 相机从每 200 ms 重建一次 JPEG 元素改为连续 `preview.mjpg`，避免黑帧闪烁；
- 预览状态轮询周期为 350 ms；
- 增加 `/ros-mobile/hybrid/heartbeat`，页面每 3 秒续租一次；
- 60 秒以上心跳实测共 69 秒、23 次心跳，23/23 保持 `state=ready`，主臂和
  双层租约全部保持，随后正常释放。
- 增加无会话急停清除入口 `/ros-mobile/hybrid/clear-estop`。以前
  `estop_latched` 只能在持有会话时清除，会阻塞后续获取控制权。
- “开始无输出联动”现在一次完成获取租约、进入预览和记录对齐，避免页面只取得
  租约但五个关节持续显示“等待映射”。

主臂状态在会话中缺失的问题也已修复：`/hybrid/status` 现在无论是否持有会话，
都会返回主臂健康、设备和无输出预览数据。USB 连续观测 31 个采样点、30 秒，
主臂 897 次读取、0 失败。

现场再次确认：当前页面是**无输出预览**。主臂移动会更新预览目标，但从臂舵机
不会物理运动；这是 `outputs_enabled=false` 的预期行为。真实跟随仍需在方向检查
完成后单独通过输出使能门禁。

页面级最终回归 7/7 通过：单击开始联动、会话取得、自动对齐、五个映射卡片出现、
不再显示等待映射、主臂在线、连续 MJPEG 和执行输出关闭；浏览器脚本错误为空。

用户页面曾在 `22:58:07` 成功创建过一次从臂标定任务；随后拖动夹爪触发的目标请求
返回 `409`，因为 joint1–joint5 范围尚未达标。这是当前状态机的顺序门禁，不是滑块
组件故障。任务在驱动恢复期间被有意重置；新任务需要重新确认安全窗与支撑条件。

未确认安全窗的启动请求实测返回：

```text
HTTP 409
{"detail":"confirm the safe window before starting"}
```

首次联调时该错误被上位机 `_hybrid_guard` 漏掉并表现为 500。已修正为保留车端
`BridgeError` 的状态码和详情，并新增回归测试；修复后
`tests/test_follower_calibration_api.py` 为 `30 passed`。该失败请求在车端安全确认
检查处终止，没有读关节基线、释放力矩或发送目标。

## 左主臂切换与实时读数复核

现场发现 C 层原本绑定的是历史标为 right leader 的
`5B90149652`，页面因此把左臂动作映射到了错误的设备身份。现已切换到左主臂
`5B90149723`，重新保存 C 层并生成三层 bundle：

```text
leader: so101-leader-5B90149723
version: cbf6d23f79d3
sha256: 277fb7e524b542b5ac744ea8294a6fb2e8ead57e0b51104a484f0fe18f3472bc

follower: landerpi-follower-v1
version: 46da965c331d
sha256: f4eebac5da8a3fdfb42d404a8f0355d836c3c9fcb2de30443f60e37d2299c9d1

mapping: landerpi-so101-mapping-v1
version: 3a8e4edf229d
sha256: 030a2df762deeb0aa691d9151de2fe68c119e77d515570831c8b14ed24a2ffc9

bundle sha256: f8936f62ba1ed73ecd91abac33f50c3d9ad54a2dae2c0a1323b0733277ebd88b
```

用户持续移动左臂期间，上位机连续读取 60 帧，间隔约 0.25 s。五轴和夹爪均真实
变化，60/60 帧主臂健康，串口失败增量为 0，最大读数年龄 0.038 s：

```text
shoulder_pan   -0.380520 ..  0.952835 rad
shoulder_lift  -1.197564 .. -0.418112 rad
elbow_flex      0.016111 ..  0.797098 rad
wrist_flex      1.261240 ..  1.750699 rad
wrist_roll     -0.209440 ..  0.795563 rad
gripper         0.081169 .. 42.694805 %
```

随后建立预览会话并记录对齐参考，状态为 `ready`，六路从臂反馈和相机仍有效。
15 秒映射采样包含 30 帧和 6 次心跳，期间全部保持 `ready`；
`outputs_enabled=false`、`motion_commands_sent=0` 在所有帧中恒定，
`hardware_preview.available=true`，原因是
`preview_only_no_execution_output`。这段采样同时证明左臂输入会更新预览目标；
采集后半段用户停止动作，因此部分目标范围为零，不能单独作为方向验收证据。

Firefox headless 读取 `/landerpi/teleop` 时显示主臂在线、从臂六路反馈、相机
实时、租约已持有、执行输出关闭；五个关节不再显示“等待映射”，浏览器脚本错误
为空。对应证据：

- `left-leader-live-sample.json`
- `left-leader-preview-session.json`
- `left-leader-mapping-live-sample.json`
- `left-leader-teleop-browser-check.json`
- `left-leader-teleop-live.png`

这轮仍是**无输出联动**。五轴方向、比例、夹爪开合语义和从臂物理跟随尚未完成
人工验收，不能据此打开执行器输出。

## 当前运行入口

```bash
systemctl --user status lelab-pi-tunnel.service
systemctl --user status lelab-landerpi-left.service
curl http://127.0.0.1:18286/ros-mobile/hybrid/status
```

标定入口：<http://127.0.0.1:18286/landerpi>  
控制台入口：<http://127.0.0.1:18286/landerpi/teleop>

页面已用 Firefox headless 实读验证，6/6 检查通过、浏览器脚本错误为空。
截图：`browser-live-ready.png`。

## 已有证据文件

- `stack-precheck.log` / `stack-precheck.exit`
- `stack-start.log` / `stack-start.exit`
- `agent-precheck.log` / `agent-precheck.exit`
- `agent-deploy.log` / `agent-deploy.exit`
- `agent-health.json`
- `agent-status-initial.json`
- `leader-probe.json`
- `precalibration-status.json`
- `browser-live.json`
- `browser-live-ready.png`
- `tunnel-health.json`
- `tunnel-systemd-state.txt`
- `uvicorn-systemd-state.txt`
- `follower-api-after-bridge-fix.log` / `.exit`
- `unconfirmed-start-response.json` / `unconfirmed-start.http`
- `leader-probe-after-restart.json`
- `signed-raw-tests.log`
- `signed-raw-host-tests.log`
- `signed-raw-agent-status.json`
- `signed-raw-upper-status.json`
- `browser-slider-state-final.log` / `.exit`
- `browser-slider-state-final.png`

## 尚未执行的实机门禁

A/B/C 三层标定和无输出预览已完成；执行器输出仍固定关闭。真实跟随前仍需验证
主从方向与比例、输出使能、命令 TTL、底盘锁定、保持/停止延迟和急停效果。
当前接口没有夹爪电流/负载反馈，不能把“卡住”识别成机械端点。
