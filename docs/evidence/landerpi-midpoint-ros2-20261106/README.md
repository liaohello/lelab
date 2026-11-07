# LanderPi ROS 2 中位与 joint1 恢复：本轮证据

日期：2026-11-06（America/Los_Angeles）。

## 1. 结论与边界

本轮把首帧不连续导致永久拒绝的缺陷修复并打包为独立 v3 候选，在 Pi 的
Humble/Python 3.10.12 容器中完成包哈希和模块导入检查。v3 部署后没有发送运动目标：
第一次启动的控制进程因命令缺少真实话题参数被配置门禁拒绝；第二次启动后六路反馈在
30 秒时仍未出现，随后定位到 ROS DDS 环境不一致。用户在此后关闭小车电源，SSH
连接返回 `Connection refused`，因此本轮没有执行新的机械臂动作，也没有把 joint1
恢复测试计为通过。

历史 v2 已在真实硬件上产生过 joint1 小幅运动，但存在大量步进拒绝；其状态和本轮修复
分别记录在下面，不能把旧 v2 的 `publish_count` 当作本轮 v3 结果。

## 2. 电源关闭前最后可见状态

最后成功读取的真实六路反馈：

| 关节 | servo ID | raw | 标定范围 50% | 差值 |
| --- | ---: | ---: | ---: | ---: |
| joint1 | 1 | 544 | 504 | -7.68° |
| joint2 | 2 | 641 | 485.5（下发取 486） | -37.32° |
| joint3 | 3 | 72 | 495 | +101.52° |
| joint4 | 4 | 118 | 540 | +101.28° |
| joint5 | 5 | 907 | 495 | -98.88° |
| r_joint | 10 | 457 | 348 | -26.16° |

范围来自操作员手动录入，不是机械极限实测。相机、agent 和反馈当时均新鲜；随后
用户关闭整车电源。

## 3. 本轮真实操作与失败

### 3.1 v2 joint1

真实命令话题监听只看到 servo ID 1。joint1 回读曾由约 raw 536 变为 533，说明存在
实际小幅位移。旧控制状态同时记录：

```text
frames_received=381
frames_emitted=198
frames_refused=182
publish_count=198
last_refusal=trial_first_frame_discontinuous:joint1
```

根因是首帧被拒绝后步进基准仍为空，后续每一帧都被继续当成首帧检查。修复后首帧只拒绝
一次，并以实测位置初始化步进基准；后续目标仍受 0.15 rad/s、0.08 rad 单次接合
位移、锁定轴容差和证据 TTL 限制。

### 3.2 v3 启动

第一次控制进程被真实配置门禁拒绝：

```text
refusing to start: hardware_trial_configuration_mismatch
```

原因是手工命令未传真实输出、回读和录制话题。补齐后进程启动，初始状态为：

```text
outputs_enabled=false
publish_count=0
frames_received=0
frames_emitted=0
frames_refused=0
trial_first_frame_pending=true
```

随后发现 `/lelab/ros_robot_controller/servo_readback` 只有厂商发布者，v3 进程看不到
ROS 图。厂商节点环境为：

```text
ROS_DOMAIN_ID=0
ROS_LOCALHOST_ONLY=1
RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
```

新进程落到了默认 RMW。`/home/ubuntu/ros2_ws/.typerc` 还设置了
`CYCLONEDDS_URI=file:///etc/cyclonedds/config.xml`，但该文件在容器内不存在；显式
启用了 CycloneDDS 后节点因此创建失败。以后必须沿用现有启动脚本已经实现的顺序：
加载 `.typerc` 和 Humble overlay 后 `unset CYCLONEDDS_URI`，再设置
`RMW_IMPLEMENTATION=rmw_cyclonedds_cpp`、`ROS_LOCALHOST_ONLY=1`、
`ROS_DOMAIN_ID=0`。

## 4. 本轮构建与验证

### v3 包

```text
release_id: landerpi-arm1-trial-v3-20261106
files: 37
archive: work/ros2_collection/hardware-preflight-20261106/package/landerpi-arm1-trial-v3-20261106.tar.gz
sha256: ba8cbea861373b82a592c381e2ada960a1edccc1223be5a98cab7fc4e095a239
```

Pi 内逐文件哈希与 manifest 一致，`hash_mismatches=[]`；Humble/Python 3.10.12 下
`ros_servo_output`、`car_control_process`、`agent_licence_bridge`、`agent`、
`ros_servo_readback`、`teleop_frame_v1`、`teleop_stream` 全部导入成功。旧 v2
候选和旧状态目录均保留。

### 测试

| 命令范围 | 结果 | 日志 |
| --- | --- | --- |
| 驱动首帧、锁定轴和位移保护 | 7 passed | `driver-tests.log` |
| 上位机 stream/hardware/integration | 33 passed | `upper-tests.log` |
| 本机隔离 Humble/Python 3.10 三个车端目标文件 | 49 passed | `humble-car-tests.log` |
| TypeScript | exit 0 | `tsc.log` |
| Vite production build | exit 0，2430 modules | `vite-build.log` |

测试后当前正式产物为：

```text
frontend/dist/assets/index-DOeM4q85.js
frontend/dist/assets/index-BjqetR5a.css
```

## 5. 收尾

本轮启动的旧 18287 上位机进程已在核验监听端口和命令行后停止。18286 既有服务未动。
用户随后关闭小车电源，无法再访问 Pi。最后一次安全状态没有运动会话，控制进程已停止；
旧的 v2 状态、失败日志和 v3 `rmw-mismatch` 日志均保留。

## 6. 2026-11-07 重新上电复测

用户重新上电后，无线地址 `<private-ip>` 恢复，机器 ID 仍为
`ad522b778a1541f8b20dd02a8a3660ec`。有线地址 `<private-ip>` 当时不通。

最小栈最初启动时厂商 `ros_robot_controller` 没有测量回读发布者。使用本仓库
`vendor_patches/ros/patch_guard.py` 对原始源码副本执行 0001→0003 后，两个文件哈希为：

```text
ros_robot_controller_sdk.py
  7ea4e4f5aeea69d778f5cf93e35abd2c5b227bf3a12796fdfd7bc84d6fa2b8b7
ros_robot_controller_node.py
  7e171990ecbc7a99179266e00e6c915be10f01d74478b34f8af77d431d8e2615
```

补丁副本从
`/home/ubuntu/shared/lelab_car/vendor/ros_robot_controller_t1_20261107` 加载；原厂源码
未覆盖。重启后的节点状态：

```text
ros_robot_controller PID 3685
controller_manager PID 3906
camera PID 4031
/dev/rrc -> /dev/ttyACM0，唯一 holder 为 ros_robot_controller
```

真实测量回读已恢复。控制进程在 3 秒内收到 72 个样本，扫描跨度约
`0.017661 s`，失败扫描为 0：

```text
joint1=544, joint2=629, joint3=76,
joint4=118, joint5=907, r_joint=457
```

相机实时，ROS 节点包含 `/ros_robot_controller`、`/lelab_mobile_agent`、
`/lelab_car_control`。输出话题有控制进程与 vendor `servo_manager` 两个发布者，但
当前没有会话、没有许可：

```text
outputs_enabled=false
publish_count=0
frames_received=0
frames_emitted=0
frames_refused=0
trial_first_frame_pending=true
```

上位机 `18287` 和 frame tunnel `18126 → Pi:18123` 已启动，页面
`/landerpi/teleop` 返回 200。当前 agent 尚未绑定三层 bundle，因此 `/status`
把六路样本标为 `calibration_not_configured`；控制进程已经用配置中的
`bb62bd95…adaf` 验证到六路有效回读。这个区别会在获取新 epoch 时由车端
`set_calibration_hash()` 消除。

当前唯一现场阻断是主臂不在上位机：

```text
expected=/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149723-if00
observed on upper host=absent
probe=409 leader_serial_missing
```

Pi 上的 `/dev/ttyACM0` 是控制板的 `/dev/rrc`（USB ID `5AE2006307`），不是 SO-101
主臂；`/dev/ttyUSB0` 是另一路 CH340 串口。不能把这两个设备误绑定为主臂。

本轮重新上电后的日志：

- `arm-stack-check-20261107.log`
- `arm-stack-apply-20261107.log`
- `vendor-patch-apply-20261107.log`
- `vendor-upload-20261107.log`
- `v3-run2-20261107.log`
- `web-v3-live-20261107.log`

## 7. 2026-11-07 第 1 轴跟随复测与相机恢复

- 上次现场跟随因 `window_expired` 退出；这次将上位机的窗口发送余量改为
  120 ms，车端仍按原期限拒绝过期帧，没有放宽车端 TTL。试验映射速度上限
  进一步收紧为 0.10 rad/s，车端保留 0.15 rad/s 上限；没有改动三层标定资产。
- 车端驱动异常停发、拒绝目标或租约撤销后，需要先撤销输出许可并重新对齐、
  显式接合，不能依靠尚存的许可自动恢复。模拟测试：Humble/Python 3.10 定向
  75 passed，额外租约测试 23 passed；上位机帧链路及映射测试 36 passed。
- v5 候选 `landerpi-arm1-trial-v5-20261107`：37 文件逐项哈希通过，tar SHA256
  `87664b7d851d62042652857a8e0d5d5673cff7090e32356809bd117890f7336c`。
  v3 agent 和 v5 控制进程的共同协议及许可模块哈希相同。v4 包是中间候选，
  不应部署。仓库和原厂目录均未切换 `current`。
- 复测前，本任务启动的 vendor `servo_manager` 是第二个舵机命令发布者；核对
  PID 和命令后停止该节点，ROS 图确认只剩 `lelab_car_control` 发布、
  `ros_robot_controller` 订阅。其余节点和串口所有者没有停止。
- 第一轮静止约 57 秒，车端发送 571 帧、拒绝 0 帧；主臂没有变化，随后暂停。
  第二轮用户转动主臂后确认从臂“动了”，测量 `joint1` raw 从 544 变到 541。
  主臂相对参考偏移超过单次 0.08 rad 试验上限后车端拒绝，状态为
  `trial_displacement_exceeded:joint1` 并撤销输出；日志见
  `joint1-v5-follow-20261107.log`。这是单轴小幅链路验证，不代表完整跟随、
  关节中位或其他关节和夹爪已验收。两次采样中 joint2、joint3 读回也有少量变化，
  不据此断言它们绝对静止；试验驱动只发布 ID 1。
- 腕部 Aurora 有设备和 ROS 发布者但图片停更。只重启本任务启动的相机 launch
  （原 PID 4031，新 PID 11372）；连续采样帧号 2271→2306，页面显示“相机实时”，
  尚未做长时间稳定性测试。相机日志 `camera-restart-20261107.log` 位于车端
  `/home/ubuntu/lelab_car/car_arm_stack_20261107/`。
- 最终核对：车端控制会话和输出许可均关闭、控制进程输出关闭、故障后需重新接合；
  页面断开并释放租约。关节安全端点仍有人工录入且未实测的部分，不能将各轴一起
  自动移动到 50% 位置。物理保持与完整硬件故障保护尚未验收。

## 8. 第 2 轴试验及用户指定 raw 姿态

- 以独立 `hardware_hybrid_arm2_trial_v1` 部署；candidate 的 38 文件逐项哈希一致，
  tar SHA256 `db2e9aebb647374643b40fe016450c931cff88fdb0b00a8cb99f63088cd2a19b`。
  只读预览期间车端 `frames_emitted=0`；实际接合后从臂 joint2 回读 raw 632→630，
  用户确认看见肩部运动。第 157 帧的目标超过本次 trial 0.08 rad 位移范围后被拒，
  车端撤销输出、需要重新接合，网页显示真实拒绝原因。会话随后释放。
- 用户随后明确要求五个关节 raw 均为 500、夹爪 raw 350，并确认完整转动路径清空、
  有现场观察和可触发急停；这是指定的 raw 姿态，不等同各轴校准范围 50%。
- 本轮编写 `hybrid_home_plan.py`，对真实状态只读计算下一步，11 项离线测试通过；
  `home-request-readonly-20261107.json` 记录目标及当前值。规划器没有发送任何
  回中目标，完整回中执行与多轴跟随尚未实现。`arm3`–`arm5` profile 只在工作区，
  未部署或实测。Humble 车端定向测试 83 passed，属于模拟/离线验证。
- 已核查当前系统尚无支持跨轴和夹爪的自动回中控制接口；下一步应在现有双租约、
  帧流、车端输出门禁和网页急停中实现，不可用直写 ROS 的脚本绕过。详细并行计划：
  `docs/handoffs/2026-11-06-landerpi-homing-parallel-agent-plan.md`。

