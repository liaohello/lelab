# LanderPi 当前调试与开发记录

记录时间：2026-11-05
记录范围：树莓派 LanderPi 从臂、SO-101 左主臂、三层标定、无输出预览控制台、
会话租约和反馈故障恢复。
当前状态：小车已断电，联调暂停；代码、日志和未提交修改均保留。

## 1. 当前结论

本轮已经完成从“只能读硬件”推进到“真实三层标定 + 左主臂实时输入 + 六路从臂反馈
+ 相机 + 无输出映射预览”。控制台能稳定显示主臂、预览目标和从臂实测，但没有开放
任何执行器输出。

当前安全边界仍为：

```text
outputs_enabled=false
motion_commands_sent=0
control_semantics=preview_readback_only
```

也就是说，移动 SO-101 左主臂只改变预览目标，不会让树莓派从臂舵机物理跟随。

## 2. 真机与运行环境

- Pi WiFi：`pi@<private-ip>`，本轮联调使用此入口。
- 有线地址：`<private-ip>`，历史检查存在明显丢包。
- ROS 容器：`MentorPi`，ROS 2 Humble，Python 3.10.12。
- 板卡 owner：唯一 `ros_robot_controller`。
- 从臂反馈：`/ros_robot_controller/bus_servo/get_state` 和 agent
  `/status -> control.follower_feedback`。
- 相机：`/ascamera/camera_publisher/rgb0/image`。
- 当前上位机隧道 unit：`lelab-pi-tunnel.service`。
- 当前左侧主臂 unit：`lelab-landerpi-left.service`，监听
  `127.0.0.1:18286`。
- 页面：
  - 标定：<http://127.0.0.1:18286/landerpi>
  - 主从无输出控制台：<http://127.0.0.1:18286/landerpi/teleop>

## 3. 三层标定与当前版本

当前三层 bundle：

```text
f8936f62ba1ed73ecd91abac33f50c3d9ad54a2dae2c0a1323b0733277ebd88b
```

### 3.1 左主臂

现场发现旧 C 层绑定的是历史审计标为 right leader 的
`5B90149652`，因此用户移动左臂时，系统读取的是错误设备身份。现绑定：

```text
identity: so101-leader-5B90149723
version: cbf6d23f79d3
sha256: 277fb7e524b542b5ac744ea8294a6fb2e8ead57e0b51104a484f0fe18f3472bc
```

### 3.2 树莓派从臂

按现场要求使用全部手动录入，没有伪造自动采样、机械极限或重复性结论：

```text
joint1: 10 .. 998
joint2: 10 .. 961
joint3: 0 .. 990
joint4: 100 .. 980
joint5: 80 .. 910
gripper closed: 504 raw
gripper open:   192 raw
```

资产来源标记为 `operator_entry_not_mechanical`，方法为
`operator_manual_full_entry`。当前版本：

```text
identity: landerpi-follower-v1
version: 46da965c331d
sha256: f4eebac5da8a3fdfb42d404a8f0355d836c3c9fcb2de30443f60e37d2299c9d1
```

### 3.3 主从映射

当前 C 层：

```text
shoulder_pan  -> joint1, direction +1
shoulder_lift -> joint2, direction -1
elbow_flex    -> joint3, direction +1
wrist_flex    -> joint4, direction -1
wrist_roll    -> joint5, direction +1, wrap=true
gripper %     -> r_joint / ID10, direction -1
```

其他参数：

```text
scale=1.0
filter_alpha=0.35
max_step_rad_s=0.5
command_hz=10
```

当前版本：

```text
identity: landerpi-so101-mapping-v1
version: 3a8e4edf229d
sha256: 030a2df762deeb0aa691d9151de2fe68c119e77d515570831c8b14ed24a2ffc9
```

五轴方向、比例、夹爪开合语义和从臂物理跟随还没有完成现场人工验收。

## 4. 主要开发内容

### 4.1 从臂标定

- 车端使用后台标定任务，采样和状态机在 Pi 容器内运行，不依赖浏览器保持同步长循环。
- 五轴允许操作员直接手填范围，来源明确标记为非机械测量。
- 夹爪使用滑轨拖动和端点标记；大跨度移动、反馈短暂丢失和松手后的过期目标不立即
  误报跟踪故障。
- 手动移动受保护的五轴时，夹爪流程会退出到五轴范围阶段，不直接进入 fault。
- B 层资产由服务端根据车端 evidence 生成，浏览器不能自行填写 `measured=true`。
- 已生成并读回 `landerpi-follower-v1`，三层复验通过。

### 4.2 主从无输出控制台

- 新增 `/landerpi/teleop`，布局为相机、会话控制、五轴主臂/预览目标/从臂实测卡片。
- “开始无输出联动”一次完成获取租约、进入预览和记录对齐。
- 页面每 3 秒续租，状态每 350 ms 刷新。
- 相机改用连续 MJPEG，解决频繁重建 `<img>` 导致的闪屏。
- 控制台增加暂停、急停、释放会话和故障恢复入口。
- 故障恢复现在会重新获取会话并自动进入 preview/align。
- 页面把 `feedback_valid_missing`、`lease_lost`、`stop_failed` 等原始状态翻译为
  中文原因，同时保留原始错误码。
- 预览路径固定 `outputs_enabled=false`，不存在隐藏执行器写入。

### 4.3 反馈故障恢复

现场曾出现：

```text
feedback_valid_missing
stop_failed:HardwareCompositionError
```

根因和过程：

1. 会话刷新只在调用瞬间检查一次六路反馈；一次瞬时 missing/stale 就直接锁成 fault。
2. 页面进入 fault 后停止心跳。
3. 车端 30 秒控制租约随后过期。
4. 之后尝试发送停止请求时旧 token 已失效，`stop_failed` 又覆盖了最初的
   `feedback_valid_missing`，导致页面只剩较难理解的次级错误。

修复：

- `HybridSession._refresh()` 对主臂连接、主臂健康、六路反馈和独占控制的观察结果
  最多尝试 3 次，间隔 0.12 秒。
- 瞬时缺口恢复后会话保持 `ready`，不会误停。
- 连续 3 次仍失败才进入 fault 并停止逻辑通道。
- 停止请求失败时不再覆盖最初故障原因，而是记录为组合原因，例如
  `feedback_valid_missing;stop_failed:HardwareCompositionError`。
- 故障恢复按钮重新进入 preview/align，不再只获取一个未对齐的租约。

## 5. 调试现场与验收证据

证据目录：

`docs/evidence/landerpi-hardware-20261104/online-calibration/`

### 5.1 左主臂实时读数

用户持续移动左臂期间采集 60 帧，60/60 健康，串口失败增量为 0：

```text
shoulder_pan   -0.380520 ..  0.952835 rad
shoulder_lift  -1.197564 .. -0.418112 rad
elbow_flex      0.016111 ..  0.797098 rad
wrist_flex      1.261240 ..  1.750699 rad
wrist_roll     -0.209440 ..  0.795563 rad
gripper         0.081169 .. 42.694805 %
reads: 1733 -> 2197
```

证据：

- `left-leader-live-sample.json`
- `left-leader-probe.json`
- `capabilities-left-leader.json`
- `switch-to-left-leader.log` / `.exit`

### 5.2 左主臂预览会话

获取会话、preview 和 align 均返回 HTTP 200，状态 `ready`：

```text
hardware_preview.available=true
reason=preview_only_no_execution_output
outputs_enabled=false
motion_commands_sent=0
```

证据：

- `left-leader-preview-session.json`
- `left-leader-mapping-live-sample.json`

### 5.3 浏览器读回

Firefox headless 实测：

```text
主臂在线: true
从臂六路反馈: true
相机实时: true
租约已持有: true
执行输出关闭: true
“等待映射”出现次数: 0
浏览器脚本错误: []
```

证据：

- `left-leader-teleop-browser-check.json`
- `left-leader-teleop-live.png`

### 5.4 车端反馈抖动检查

对 agent `/status` 连续采集 160 次：

```text
HTTP 成功: 160/160
feedback unavailable: 0
成功扫描增量: 67
失败扫描增量: 0
最大 oldest_age_s: 0.341
```

证据：`follower-feedback-jitter-16s.json`

### 5.5 修复后的 75 秒保持测试

修复后重新获取左主臂无输出会话并持续 75 秒：

```text
状态采样: 121/121 ready
心跳: 25/25 HTTP 200
feedback_valid=false 采样: 0
心跳失败: 0
最大反馈年龄: 0.344 s
最终 upper lease: held=true
最终 car lease: held=true
最终 preview: available=true
最终 outputs_enabled=false
最终 motion_commands_sent=0
```

证据：`session-retry-75s-soak.json`

故障发生和手工恢复的原始记录：`session-fault-recovery.json`。

## 6. 本轮验证结果

### 6.1 Python

```text
PYTHONPATH=tools/ros2_collection app_ros_mobile/.venv/bin/python -m pytest \
  tools/ros2_collection/tests/test_t3_hybrid.py -q

34 passed
```

```text
cd app_ros_mobile
.venv/bin/python -m pytest \
  tests/test_integration_upper.py \
  tests/test_hybrid_hardware.py \
  tests/test_t3_wave_a.py \
  tests/test_hardware_lease_api.py -q

55 passed
```

测试中的 Pytest/FastAPI deprecation warning 不影响退出状态。

### 6.2 前端

TypeScript：

```text
./node_modules/.bin/tsc --noEmit --pretty false
exit 0
```

正式构建：

```text
npm run build
exit 0
2430 modules transformed
dist/assets/index-CVnImGz3.js
dist/assets/index-BtE-AQXk.css
```

本轮机器的 `node`/`npm` 不在默认 PATH，实际使用：

```text
/home/liaozhengyan/.workbuddy/binaries/node/versions/22.22.2/bin
```

构建只有 Vite 的大 chunk 提示，没有构建错误。

## 7. 关键代码位置

- `app_ros_mobile/lelab/hybrid_session.py`
  - 会话状态机、租约刷新、观察结果有界重试、故障原因保留。
- `app_ros_mobile/lelab/hybrid_hardware.py`
  - 左主臂租约、车端租约、真实反馈观察、无输出 driver。
- `app_ros_mobile/lelab/hybrid_facade.py`
  - 通道权限、预览和反馈门禁。
- `app_ros_mobile/lelab/ros_mobile.py`
  - HTTP 路由、心跳、会话和控制器桥接。
- `app_ros_mobile/frontend/src/pages/LanderpiTeleop.tsx`
  - 主从控制台、中文故障说明、一键预览和故障恢复。
- `tools/ros2_collection/tests/test_t3_hybrid.py`
  - 瞬时反馈重试、持续故障和 stop error 保留测试。
- `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/`
  - 车端 agent、反馈、通道、记录和硬件安全。

当前工作树没有提交、推送或切 `current`，全部修改保持未提交。

## 8. 断电收尾状态

小车断电前已通过 API 释放当前会话：

```text
state=disconnected
session_id=null
upper lease held=false
car lease held=false
last_error=null
outputs_enabled=false
motion_commands_sent=0
```

断电后再次读取上位机状态，当前显示：

```text
state=disconnected
feedback_valid=false
outputs_enabled=false
```

这是因为小车断电，六路从臂反馈不可用，不是新的软件 fault。

## 9. 尚未完成与恢复顺序

尚未完成：

- 左主臂五轴方向、比例和夹爪开合语义的逐轴人工验收。
- 树莓派从臂真实执行器输出 driver。
- 输出路径的 session/epoch/TTL/标定 hash 绑定和控制器确认。
- 真实输出使能、保持/停止延迟、急停效果和固件 watchdog 验收。
- 锁底盘条件下两段 observe/grasp 6D 实机采集、同步、导出和页面读回。

重新上电后按以下顺序继续：

1. 确认 Pi 启动和 WiFi/隧道恢复。
2. 确认 `ros_robot_controller` 是唯一板卡 owner。
3. 确认六路反馈 `valid=true`、相机新鲜、三层 bundle 未变化。
4. 打开 `/landerpi/teleop`，获取无输出会话并记录对齐。
5. 逐轴只移动一个关节，核对主臂值、预览目标和物理方向，先不要启用输出。
6. 完成方向验收后，再单独设计和验证真实输出 driver。

状态检查命令：

```bash
systemctl --user status lelab-pi-tunnel.service
systemctl --user status lelab-landerpi-left.service
curl -sS http://127.0.0.1:18286/ros-mobile/hybrid/status | python3 -m json.tool
curl -sS http://127.0.0.1:18286/ros-mobile/hybrid/capabilities | python3 -m json.tool
```

## 10. 相关文件

- 总联调记录：
  `docs/evidence/landerpi-hardware-20261104/online-calibration/README.md`
- 原始执行目标：
  `docs/handoffs/2026-11-04-landerpi-hardware-plan-and-agent-goal.md`
- 车端复用分析：
  `docs/handoffs/2026-11-05-landerpi-vendor-docker-reuse.md`
- 系统设计：
  `docs/landerpi-hybrid-teleop-system-design-2026-11-04.md`
- 从臂标定设计：
  `docs/landerpi-follower-calibration-offline-implementation.md`
