# LanderPi 实机开发收敛记录

日期：2026-11-07（Pi 本地时间；工作机时为 2026-11-06 America/Chicago）。

## 当前安全状态

本轮在用户准备给小车断电时主动停止，没有留下控制会话或运动进程：

- hybrid 会话已释放；最后一次车端控制状态为 `outputs_enabled=false`、`paused=true`。
- 本轮启动的 `lelab_mobile_agent.agent`、`car_control_process` 和
  `ros_robot_controller` 板卡 owner 均已停止。
- `/dev/rrc` 不再有活动 owner；底盘没有新的控制入口。
- 六轴动力在断电前保持加载，没有主动卸载承重轴。机械臂保持最后位置，随后由用户
  正常整车断电。
- 本轮最后没有执行关节运动。joint1 仍是 raw 527，没有到达目标 500。

最终六轴只读回包（约同一帧查询）：

| ID | 关节 | raw position | torque | voltage 回包 | temperature |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | joint1 | 527 | 1 | 7132 | 27 C |
| 2 | joint2 | 632 | 1 | 7092 | 23 C |
| 3 | joint3 | 76 | 1 | 7119 | 22 C |
| 4 | joint4 | 118 | 1 | 7081 | 23 C |
| 5 | joint5 | 907 | 1 | 7075 | 23 C |
| 10 | r_joint | 457 | 1 | 7084 | 23 C |

原始回包：
`docs/evidence/landerpi-homing-20261107-live/final-six-axis-readback-20261107.log`。

## 本轮实际完成

### 1. 六轴动力状态已真实确认

加载前：ID1 为 `torque=1`，ID2/3/4/5/10 均为 `torque=0`。

使用现有唯一 ROS 板卡 owner 逐 ID 写入并读回，最终六轴均为 `torque=1`。工具修复了
两个实际错误：

- 首次发布前等待 ROS 订阅匹配，避免首帧在发现完成前丢失。
- `present_id` 必须写成 `[1, servo_id]`，原工具误写成
  `[servo_id, servo_id]`，导致只有 ID1 偶然通过 wrapper 校验。

通过结果：
`docs/evidence/landerpi-homing-20261107-live/torque-load-v2c-20261107.log`。

### 2. 厂商 SDK 力矩语义修复已部署

车端版本化 vendor 目录：
`/home/ubuntu/shared/lelab_car/vendor/ros_robot_controller_t1_20261107/`。

- `enable=True` 发送 `0x0C`（load），`enable=False` 发送 `0x0B`（unload）。
- 电压、力矩查询方法名已修正。
- 状态数组在 wrapper 写入前校验。
- 原厂目录未覆盖，旧文件保留版本化备份。

部署文件哈希：

```text
ros_robot_controller_sdk.py
cdc04f097beaf4cfaa0ebc739dd21aec476f0b75709f7aa868db07ddb8747b2b

ros_robot_controller_node.py
9f7fcabf96b5fa68de1b5a4e20f7f9c3d6221b5f07055bef5296a992b528befe
```

### 3. 回中启动力矩消息契约修复

车端 `RosServoOutputDriver._enable_homing_torque()` 原先把 `present_id` 写成
`[servo_id, servo_id]`。v2 wrapper 要求 `[1, servo_id]`，所以回中启动时只有 ID1
能通过校验。

现已修正为每个 ID 单独发送：

```text
present_id = [1, servo_id]
enable_torque = [1, 1]
```

该改动包含在 v14 车端候选内，但没有上车启动或实机验收。

### 4. joint1 单轴测试范围

为避免“joint1 验证完成后立即开始 joint2”，新增受控请求字段：

```json
{"only_joint": "joint1"}
```

服务端会读取六轴新鲜反馈，只把 joint1 目标设为 500；其余五轴目标每轮固定为当前
实测 raw。车端原有的单轴、限位、步长、TTL、租约和急停约束继续生效。

离线测试确认模拟运行中只有 joint1 变化，其余五轴保持原值。

### 5. 反馈序号重启恢复

重启板卡 owner 后，话题发布序号从 0 开始。上位机原先仍拿重启前的序号比较，导致
六路反馈持续报 `readback_sequence_invalid`，本次实际等待约 15 分钟让新序号追过旧值。

已改为：只有旧反馈已超过 stale 门槛且新序号回到 0–5 范围内时，才认定为发布者重启，
清空旧反馈缓存后重新同步。正常重复帧和短间隔乱序仍被拒绝。

该修复仅完成离线测试，尚未在再次重启 owner 的真实场景验收。

## 未完成或受阻

- 六轴 torque=1 已确认，但修复后尚未重新执行 joint1 真实小步运动。
- v14 候选只在 Pi 上解包，没有启动、没有实机运动。
- 六轴回中、主从同步、6D observe/grasp 采集和页面读回仍未在本轮达到实机验收。
- 固件仍存在回包 ID/命令匹配、长度边界、错误传播和 DMA/ISR 堆释放问题。
- 固件没有修改、没有目标板构建、没有烧录。
- `torque_state=1` 只表示加载状态，不能证明供电、机械阻力和实际输出力矩充足。

## 代码与测试

本轮新增或修改的关键文件：

- `tools/ros2_collection/car_bus_servo_torque_ros.py`
- `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/ros_servo_output.py`
- `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/ros_servo_readback.py`
- `app_ros_mobile/lelab/hybrid_home_plan.py`
- `app_ros_mobile/lelab/hybrid_stream_hardware.py`
- `tools/ros2_collection/tests/test_ros_servo_output.py`
- `app_ros_mobile/tests/test_hybrid_home_plan.py`
- `app_ros_mobile/tests/test_hybrid_stream_hardware.py`

验证结果：

- 上位机回中与单轴范围测试：`37 passed`
  - `docs/evidence/landerpi-homing-20261107-live/single-axis-target-tests-final.log`
- 车端输出、力矩契约和反馈重启测试：`16 passed`
  - `docs/evidence/landerpi-homing-20261107-live/car-readback-restart-tests-final2.log`
- 力矩验证工具 Python 编译通过，现场工具与本地 SHA256 一致。
- 所有既有未提交修改、旧证据、只读记录和 invalid session 均保留；未切 `current`、
  未提交、未推送、未改原厂源码、未烧录固件。

## 下次上电后的恢复入口

Pi 当前地址仍是 `pi@<private-ip>`。`<private-ip>` 本轮连接超时，上电后需重新
确认 DHCP 地址。

已上传但未启动的候选：

```text
/home/ubuntu/shared/lelab_car/candidates/landerpi-arm-homing-v14-20261107/
```

归档 SHA256：

```text
b4a3bfd14cc6240508d60830468c20e4e775299a26de7bea5b3e89c6ae0fc668
```

恢复顺序：

1. 确认 SSH、`MentorPi` 容器、`/dev/rrc` 和唯一板卡 owner。
2. 启动 v14 agent/control，使用新的状态目录，不复用旧 lease 或旧帧。
3. 先确认六路反馈新鲜，再逐 ID 读回 torque；不得用位置命令代替 torque 验证。
4. 主臂连接后重新 probe，获取新 hybrid 会话，心跳保持在整个任务期间。
5. 发送 `{"only_joint":"joint1"}`，先只验证 joint1 朝 500 的真实小幅运动。
6. joint1 无进展时停止，检查供电、支撑、连接件和机械阻力；不放大步长、不放宽限位。

## 证据索引

- 底层审查：
  `docs/handoffs/2026-11-06-landerpi-firmware-servo-control-audit.md`
- 并行实现计划：
  `docs/handoffs/2026-11-06-landerpi-homing-parallel-agent-plan.md`
- 本轮实时证据：
  `docs/evidence/landerpi-homing-20261107-live/`
- 最终进程状态：
  `docs/evidence/landerpi-homing-20261107-live/final-stop-car-processes.log`
- v14 打包：
  `docs/evidence/landerpi-homing-20261107-live/package-v14.log`
- 最终六轴回包：
  `docs/evidence/landerpi-homing-20261107-live/final-six-axis-readback-20261107.log`
