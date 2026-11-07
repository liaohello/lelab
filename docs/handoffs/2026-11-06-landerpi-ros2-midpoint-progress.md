# LanderPi ROS 2 开发状态与下次上电入口

日期：2026-11-06（America/Los_Angeles）。

## 1. 当前完成度

| 阶段 | 状态 | 已有证据 | 剩余门槛 |
| --- | --- | --- | --- |
| P0 基线、未提交资产、板卡 owner | 已完成 | 车辆身份、容器、`/dev/rrc` 唯一 owner、全部未提交修改保留 | 下次上电重新核验 PID 和设备身份 |
| P1 真实 recorder | 已完成隔离/生产代码与真实 Humble rosbag 验证 | sqlite3 bag、phase、manifest、同步和页面读回证据 | 真实整车 6D 数据仍待 P4 |
| P2 六路真实反馈 | 已完成 | ID1–5、ID10 真实回读，来源、年龄、跨度、帧签名 | 下电期间不可读 |
| P2 真实执行 adapter | 已接通，joint1 做小幅实机验证 | 唯一 frame stream 控制进程、ID1-only 真实命令、停止和 hold 路径 | 首帧恢复 v3 尚未完成一次稳定实机复验 |
| P3 主臂与三层标定 | 已完成 | 左主臂 `5B90149723`、手动 B 层、三层 bundle、无输出预览 | 五轴物理方向与语义仍需真实低速验收 |
| P4 锁底盘真实 6D 两集 | 未完成 | 生产网页和两集 96 帧数据已在模拟身份下走通 | 尚未产生合格真机 episode、同步、导出和页面读回 |
| P5 底盘 | 未开始/明确受限 | 仅保留协议、工具链和固件分析 | 需要板型/编码器/固件/烧录/底盘运动的新授权 |
| P6 手柄与 9D | 未开始 | 设计、语法和离线契约存在 | 依赖 P4/P5 与手柄实机 |

模拟两集 96 帧、`simulated_offline` 导出和页面读回是生产软件路径的有效证据，但不是
用户要求的真实 6D 数据。

## 2. 当前主要代码链

```text
SO-101 左主臂
  -> UpperLeaderLease / mobile_leader_reader
  -> HybridSession preview/align/arm/engage
  -> StreamHardwareObserver 持久 Mapper
  -> FrameStreamDriver
  -> SSH frame stream
  -> car_control_process（准入、TTL、序号、许可、反馈、步进）
  -> RosServoOutputDriver
  -> /ros_robot_controller/bus_servo/set_position
  -> 唯一 vendor ros_robot_controller
```

当前单轴试运行 profile 仍然只允许 joint1。请求其他轴或夹爪不会成为“输出被悄悄
放宽”的副作用；要推进中位，必须新增明确的单轴阶段工具或新 profile，并继续要求
其他轴保持实测位置。

主要文件：

- `app_ros_mobile/lelab/hybrid_stream_hardware.py`
- `app_ros_mobile/lelab/hybrid_hardware.py`
- `app_ros_mobile/lelab/hybrid_session.py`
- `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/car_control_process.py`
- `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/ros_servo_output.py`
- `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/agent_licence_bridge.py`
- `app_ros_mobile/frontend/src/pages/LanderpiTeleop.tsx`

## 3. 最新根因修复

### 首帧永久拒绝

旧实现首帧不连续后没有设置步进基准，后续所有帧仍按首帧比较。现在：

- `enable()` 重置首帧、目标和步进状态；
- 首帧越界只拒绝一次；
- 拒绝后用六路实测位置初始化步进基准；
- 下一帧仍以 0.15 rad/s 接近，并接受 0.08 rad 位移上限；
- 锁定轴、反馈丢失、TTL、许可和 watchdog 均未放宽。

### 页面可观测性

车端现在透传：

- `publish_count`
- `frames_received`
- `frames_emitted`
- `frames_refused`
- `last_refusal`
- `trial_first_frame_pending`
- `effective_emit_hz`

页面分开显示上位机提交、车端收到、车端实发、车端拒绝和有效帧率。实际 18287
进程在源码修改前启动，所以本轮看到的页面仍是旧模块；下次上电必须重启 18287 才能
加载当前代码。

## 4. ROS 2 启动环境

这是本轮 v3 启动失败的直接原因，下次必须作为一个门禁检查：

```bash
source /home/ubuntu/ros2_ws/.typerc
source /opt/ros/humble/setup.bash
source /home/ubuntu/ros2_ws/install/setup.bash
unset CYCLONEDDS_URI
export ROS_DOMAIN_ID=0
export ROS_LOCALHOST_ONLY=1
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
```

`.typerc` 设置了不存在的 `/etc/cyclonedds/config.xml`，直接继承会让
CycloneDDS 节点创建失败。现有
`tools/ros2_collection/start_car_arm_stack.sh` 和
`tools/ros2_collection/deploy_readonly_agent.sh` 已包含 `unset CYCLONEDDS_URI`
及相同 RMW 设置，手工启动 agent/control 时必须复用相同语义。

启动后先确认：

```text
ros2 node list
ros2 topic info /lelab/ros_robot_controller/servo_readback -v
```

应同时看到:

- `/ros_robot_controller`
- `/lelab_mobile_agent`
- `/lelab_car_control`
- readback topic 有 agent/control 的订阅端点

节点互相不可见时，六路反馈为空不是反馈适配器故障。

## 5. 下次上电顺序

1. 核验当前机器身份、容器、板型、`/dev/rrc` holder 和 vendor PID；不沿用旧 PID。
2. 确认相机、六路反馈、三层 bundle 和主臂 by-id；夹爪保持当前姿态。
3. 按第 4 节设置 ROS 环境，启动 v3 agent，等 `follower_feedback.available=true`。
4. 启动 v3 control process，必须传真实 output/readback/recording topic；初始
   `paused`、无 licence、`outputs_enabled=false`、`publish_count=0`。
5. 重启当前源码的 18287 上位机，确认 `/ros-mobile/hybrid/status` 出现
   `frame_process`；18286 既有服务继续保留。
6. 获取预览会话、对齐；先只移动主臂 joint1，显式接合后观察真实 raw 变化和
   ID1-only 命令；确认首帧拒绝最多一次且后续计数持续增长。
7. 暂停后确认输出关闭、目标计数停止、raw 稳定；再做下一次接合。
8. 单轴稳定后才设计逐轴中位阶段。joint3/4/5 距各自中位约 99°–102°，不能一次
   发送目标，也不能六轴同时动。
9. 五轴到达各自标定范围 50% 后，再把主臂摆到同一姿态，重新捕获参考并做五轴
   逐轴低速跟随。
10. 只有方向和停止验收通过后，才开始两集真实 observe/grasp、同步、6D 导出和
    页面读回。

## 6. 本轮证据

- `docs/evidence/landerpi-midpoint-ros2-20261106/README.md`
- `docs/evidence/landerpi-midpoint-ros2-20261106/deploy-v3.log`
- `docs/evidence/landerpi-midpoint-ros2-20261106/humble-car-tests.log`
- `docs/evidence/landerpi-live-20261106-preflight/midpoint-plan-read-only.json`
- `docs/evidence/landerpi-live-20261106-preflight/axis1-live-ros-monitor.json`

本轮没有提交、推送、切换 `current`、烧录固件或使能底盘；全部未提交修改和旧证据保留。

## 7. 2026-11-07 上电复测结论

重新上电后，最小 arm stack、T1 测量回读补丁、v3 agent/control、18287 上位机和
frame tunnel 均已启动。六路测量回读、相机、输出零计数、页面和 API 已验证。

当前不能进入 acquire/preview/align，因为 SO-101 主臂没有连接在运行 18287 的上位机。
期望设备是：

```text
/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149723-if00
```

实际探测返回：

```text
409 leader_serial_missing
```

Pi 的 `/dev/ttyACM0` 是控制板 `/dev/rrc`，不能拔出或改绑为主臂。

主臂接入后按顺序继续：

1. 探测 `5B90149723`，确认五轴和夹爪读数是真实健康数据。
2. 获取会话；确认车端 epoch 绑定 `bb62bd95…adaf` 后六路变为 trusted。
3. 只做 preview/align，确认目标计数为 0。
4. 接合 joint1 试运行；主臂只动 joint1，检查唯一 ID1 命令、首帧拒绝仅一次、
   raw 真实变化与 pause/hold。
5. 重复稳定后，再为 joint2–5 分别设计单轴中位阶段；禁止同时把六轴送到中位。
