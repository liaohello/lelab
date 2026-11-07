# LanderPi 离线控制链 P0 契约与哈希基线

日期：2026-11-06（工作机）。本轮约束：不动 STM32 SDK、不连真机、不发送动作。
本文件由三个只读子智能体（A STM32 / B ROS / C 集成）核查、主智能体复核汇总。
所有行号为本次静态读取实测；参考树哈希≠实际部署≠已烧录固件，三者分别列出。

## 1. 完整调用链（已核对）

```text
LanderpiTeleop.tsx startHoming → POST /ros-mobile/hybrid/home (ros_mobile.py:1254)
  → FrameStreamHardwareFacade.start_home (hybrid_stream_hardware.py:560)
  → _home_enable → FrameStreamDriver.enable → agent /hybrid/enable_output
  → agent 许可镜像 (licence_ipc) → car_control_process._tick_locked (car_control_process.py:210)
  → RosServoOutputDriver.enable → _enable_homing_torque (ros_servo_output.py:204)
  → ROS SetBusServoState /ros_robot_controller/bus_servo/set_state
  → 修复版 vendor node set_bus_servo_state → SDK bus_servo_enable_torque
  → AA55 func=0x05 子命令 0x0C(load)/0x0B(unload) → packet_handle.c:234-243
  → serial_servo.c:121 内层 cmd31 → UART5 115200 → 舵机
位置输出：submit_frame → ServosPosition /ros_robot_controller/bus_servo/set_position
  → SDK bus_servo_set_position → 子命令 0x01（批量 count×{id,pos}）
反馈：vendor node servo_readback_loop（0003 补丁）逐 ID bus_servo_read_position
  → 子命令 0x05 → Int32MultiArray /lelab/ros_robot_controller/servo_readback
  → RosServoFeedbackTopicDriver（agent 与 control process 各一实例）
  → agent /status control.follower_feedback → 上位 HardwareObserver.snapshot
```

## 2. ROS msg/srv 契约（B 实测）

| 字段 | 类型/单位 | 语义 | 来源 |
|---|---|---|---|
| SetBusServoState.duration | float64 **秒** | SDK `int(duration*1000)` 转 ms | lander_pi msg/SetBusServoState.msg:3, sdk.py:471 |
| BusServoState.present_id | uint16[] | **[1, servo_id]**；标志非 1 整条忽略 | msg:1, 修复版 node.py:265-283 |
| BusServoState.enable_torque | uint16[] | **[flag, value]**，flag=1 执行，value 1=load 0=unload | msg:9, node.py:297-299 |
| position/offset/target_id/max_temperature_limit | uint16[] | [flag,value]；position raw 0-1000 | msg:2-4,10 |
| position_limit/voltage_limit | uint16[] | [flag,min,max] | msg:7-8 |
| save_offset/stop | uint16[] | [flag] | msg:11-12 |
| ServosPosition.duration | float64 **秒** | 同上转 ms；ServoManager 钳 0.02-30s | msg/ServosPosition.msg:1 |
| ServoPosition.id/position | uint16 | raw 脉冲 | msg/ServoPosition.msg:1-2 |
| GetBusServoCmd | uint8 id + 9 个 get_* 开关 | 服务逐项勾选；get_torque_state=1 读动力位 | msg/GetBusServoCmd.msg:1-10 |
| GetBusServoState.srv | cmd[] → success+state[] | **无超时字段**；修复版任一读失败→success=False | srv:1-4, 修复版 node.py:423-427 |
| 读回 position | 协议 int16，msg uint16[] | 补丁 0002 按 &0xffff 透传，消费端 decode_position 还原符号 | sdk.py:492, ros_servo_readback.py:28 |

超时：原版 SDK queue 无限阻塞；修复版 0.1s 有界读+故障锁存（补丁 0001）。
力矩读回工具 `car_bus_servo_torque_ros.py:36-59` 用同一 get_state 服务，默认 2s 超时，
校验 present_id==[servo_id]、position/torque 各 1 项。

## 3. STM32 能力边界（A 实测，Python 层无法解决）

| 子命令 | payload | 单位 | 确认级别 | 文件:行号 |
|---|---|---|---|---|
| 0x01 位置+时长 | cmd,u16 dur(LE),u8 count,count×{u8 id,u16 pos} | pos raw；duration ms（推断，透传） | fire-and-forget（TC 即释放） | packet_handle.c:196-202, serial_servo.c:59-70 |
| 0x03 停止 | cmd,u8 count,count×id | — | fire-and-forget | packet_handle.c:203-209 |
| 0x05 位置读回 | cmd,u8 id → {id,subcmd,success,i16 pos} | raw int16 | 有读回**无 ID/命令匹配** | packet_handle.c:210-217, serial_servo.c:72-82 |
| 0x07 电压读回 | cmd,u8 id → {…,u16 vin} | mV（推断） | 读回无匹配 | packet_handle.c:218-225 |
| 0x0B 卸载 | cmd,u8 id | load=0 → **unload** | fire-and-forget 无回执 | packet_handle.c:234-238 |
| 0x0C 加载 | cmd,u8 id | load=1 → **load** | fire-and-forget 无回执 | packet_handle.c:239-243 |
| 0x0D 动力读回 | cmd,u8 id → {…,u8 load} | 0/1 状态位 | 读回无匹配 | packet_handle.c:244-251, serial_servo.c:193-203 |

根路径 R=/home/liaozhengyan/Desktop/resource/RosRobotControllerLite_ros_250811。
- 无批量力矩接口（0x0B/0x0C 单 ID）；批量仅位置 0x01、停止 0x03。
- 无事务 ID / boot ID / 序号；同 ID 同命令旧/新回包协议上不可区分。不虚构该能力。
- 写命令返回 void，丢弃 serial_write_and_read 错误；tx_only“成功”仅 TC 发送完成。
- 上报 ID/子命令来自**请求**而非舵机回包（packet_handle.c:187-188），MCU 可能误标来源。
- MCU 事务超时 5 RTOS tick × 1kHz = 5ms（serial_servo_porting.c:50, FreeRTOSConfig.h:68）。
- IWDG 无条件喂狗，Pi 断联臂不停（app.c:124-126）。板级命令 TTL 不存在。
- 历史审查缺陷全部仍在（length<3 未拒、length==6 误判、args 无容量检查、ISR lwmem_free、
  FIFO 溢出静默、子命令长度未校验、读失败局部变量未初始化、位置只钳上界）。本轮不修固件。

## 4. 反馈话题与命令缓存（B 实测）

- `/controller_manager/joint_states` 与 `~/servo_states` 来自 ServoManager.get_position()
  = **发送目标缓存**（servo_controller.py:41-42,61），不是实测；无序号/实例。
- 实测话题 `/lelab/ros_robot_controller/servo_readback`（0003 补丁新增）：
  v1 payload `[seq, scan_us, id,pos ×6]`（14 int32），seq 重启归零，**无实例 ID**。
  消费端旧启发式：seq≤last 且 0≤seq≤5 且距上次成功>stale → 认定重启（ros_servo_readback.py:385-391）。
- 本轮 P2 已把该 payload 版本化为 v2 `[2, owner_instance, seq, scan_us, id,pos ×6]`（16 int32），
  owner_instance 为软件 owner 启动时生成的 int32（非 MCU boot ID）；消费端拒绝 v1/未知版，
  序号只在同实例内比较，实例变化立即清缓存并撤权，退役实例的迟到帧被拒。
  实机板卡 owner 的 v2 由补丁 0005 提供（node 哈希 8da2f4b7…，sdk 不变）。

## 5. 力矩极性（B 实测，不再翻转）

- 参考 lander_pi SDK：enable=True→0x0B（与固件相反，确定缺陷）。
- 仓库修复版（补丁 0004）：True→0x0C(load)、False→0x0B(unload)；电压/力矩方法名修正；
  写前数组校验。**本轮保持该修复，不再反转。**
- 修复版副本 work/ros2_collection/vendor-candidate-torque-v2-20261107/ros_robot_controller/
  实测哈希 = 车端部署记录哈希（cdc04f09…/9f7fcabf…，交接 2026-11-07:50-64）。
  车端实际运行字节本轮未核验（无 SSH）。

## 6. SHA256 基线

### 6.1 STM32 参考树（A 实测，只读，未改）
```text
a3bdf756ebf42f11110dc51e7cec4a2875fde4986e0060d9ec1b7bc6b84164b9  Hiwonder/System/packet_handle.c
3269bcdbbd9d82371b0f8fdd7a8758c63bb6034c4e6295499846315ae0be195f  Hiwonder/Peripherals/serial_servo.c
aa1a53dded3dc7f9c99d685d1021c80debb0b4debe653522f4d09c14d6652f00  Hiwonder/Portings/serial_servo.h
f67e8298d89e79a6d3de65901cf7603858b26636be3f5eedbd87ee560f1e56ee  Hiwonder/Portings/serial_servo_porting.c
9e91ae52663e5c7a946fd8b26c9e64de9ebca3159315d3ea68d6d6f72aa9351c  Hiwonder/Portings/packet_porting.c
751bda5d6b5624dd75e6729655a06e2fe2c4572a6c9ae802c9d649b65304dbab  Hiwonder/Misc/packet.c
03a1a5391a8cb00d4117295030a3bccbf0a25f9c1eade3dbdd903571cf700492  Core/Src/stm32f4xx_it.c
774f1ed2e7558a1540a5a12cc9aee83893ee332fb85c23e6a916f87e0ea89cb5  Core/Inc/FreeRTOSConfig.h
1c82cadfbdedc198cc0aaaf734ad92bb24dba429d251522c3b6d042d293431b0  Hiwonder/System/app.c
```

6.2 ROS 参考与修复版（B 实测）
```text
f7c1584b5b1f393041bd86c16c68310d38969c15bf8dfd1f92df10373a6cc504  lander_pi 参考 sdk.py（基线）
0c4b5df728f18c4a7d1850f0647e26a0d1c892ab2455f3fa0079319a9d2c619f  lander_pi 参考 node.py（基线）
7ea4e4f5aeea69d778f5cf93e35abd2c5b227bf3a12796fdfd7bc84d6fa2b8b7  0001-0003 后 sdk.py（中间态）
7e171990ecbc7a99179266e00e6c915be10f01d74478b34f8af77d431d8e2615  0001-0003 后 node.py（中间态）
cdc04f097beaf4cfaa0ebc739dd21aec476f0b75709f7aa868db07ddb8747b2b  0004 后 sdk.py（=车端部署记录；0005 不改 sdk）
9f7fcabf96b5fa68de1b5a4e20f7f9c3d6221b5f07055bef5296a992b528befe  0004 后 node.py（=车端部署记录）
8da2f4b7ab2d0f1e69fb2542670cc344607283e0df601116c5ab3e312d914537  0005 后 node.py（v2 反馈；本轮候选）
```
补丁链已从参考树全新副本实测逐字节复现 v3（三个 guard apply 退出码 0）。

### 6.3 本轮改动前仓库基线（主智能体实测）
```text
595dae99d0299371953f72eea54f9779936a38d63482290143fb35915e88e719  ros_servo_output.py
f7e30add62a5f67cb6637fb8fb71c9a1ef0ceb8066d84fe669470902180ae125  ros_servo_readback.py
16075b55b1235b35412fc715a16b2743e2321acbab2485427299deb0eb80c94e  car_control_process.py
01f55b41b4458d1844600289a96b5d3b1538f6bbca0e16df80b3b269a24a0ee2  follower_feedback.py
bf30caf983804af772d180e70a96794d020da2a0b02adc240758f188b16e8cdc  hybrid_stream_hardware.py
476f2fdecfb73fd8b8f216c5c2bfb75cb3fb6283841fb8464b3bff1b71b7de18  hybrid_hardware.py
e05233d7565141d57670e63fbc6980b0c1dc902d6447b58a3d9ff878b34ced3c  hybrid_home_plan.py
293ddd8e01eb408f19c047273f4e6739215271833cdb491340ae12ee2cdc123b  sim_board_owner.py
6acfe7d0b0367ffb4f29ac11101ac34c7cc9e82f3328c755326b4ab799a150ac  car_bus_servo_torque_ros.py
44b54e7fcaf6555e7a24ab80fc02cb9606c5ffa366b54a9e6ae453bc3bae4ae2  LanderpiTeleop.tsx
```

## 7. 集成缺口（C 实测，本轮实施对象）

1. `_enable_homing_torque`（ros_servo_output.py:204-224）发完 6 轴直接标 enabled，无读回。
2. 反馈无 owner 实例；homing pending 持旧 sample_seq（hybrid_stream_hardware.py:758,810-818），
   重启序号归零被当旧数据；非选中轴 targets 每轮跟随实测（:727-735），漂移被掩盖。
3. 回中进展只认绝对位移 motion>0（:759-774），不辨方向/振荡/反向。
4. 网页 startHoming（LanderpiTeleop.tsx:343-348）不带 only_joint；不显示反馈年龄/加载确认。
5. 离线装置无串联预检→会话→有界心跳→only_joint→取消→释放的连续诊断入口。
6. 预检/无输出预览当前不触发 torque（torque 仅在 driver.enable），本轮保持。

## 8. 环境事实（本次核验）

- 上位测试：app_ros_mobile/.venv/bin/python（3.12）。
- 车端隔离：docker lelab/humble-dev:local（Humble/Python 3.10.12，amd64），
  现有容器 lelab-humble-dev 在跑（35h，本工作区标签）；本轮诊断用独立容器/端口/domain，不动它。
  docker 需 `sg docker -c`（当前 shell 未继承 docker 组）。
- 前端 node：/home/liaozhengyan/Desktop/lelab/work/tools/node/bin（v22.20.0）。
- 打包工具：tools/ros2_collection/local_ros2/bin/package（allow-list + 逐文件 SHA256 + import 闭包门禁）。