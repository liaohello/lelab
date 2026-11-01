# VLA 底盘接口与下一轮场景约定

日期：2026-10-31。状态：根据用户最新反馈和三路只读源码核查确定开发方向；本次未修改或重启运行中的仿真/服务，未连接硬件。

## 1. 当前需求边界

当前目标是 VLA/遥操作的机体平面速度接口、机械臂与夹爪协同和数据采集。底盘以 `(vx, vy, wz)` 表达动作，单位为 m/s、m/s、rad/s，采用约定的机体坐标系。完整移动操作动作仍包含五轴手臂和夹爪目标，图像及状态作为观测。

本阶段允许采用明确标记的平面运动模型，不再把真实麦轮滚子/接触力学复现作为采集软件验收前置条件。实际轮速控制、运动响应、限幅、时效和反馈仍需正确；真实接触、打滑与坡面动力学在后续按需要验证。用户对 VLA 接口层的最新澄清优先于此前要求先修复全部轮地动力学的实施顺序。

## 2. ROS 与 FreeRTOS 的实际分工

已确认的现有实机参考代码链路是：

```text
上层 VLA / 键盘 / 控制网关
  → ROS geometry_msgs/Twist (linear.x, linear.y, angular.z)
  → ROS MecanumChassis.set_velocity
  → 四个 MotorState(id, rps)
  → 串口 SDK set_motor_speed
  → FreeRTOS MOTOR 回调设置各电机速度目标
  → 编码器测速、PID、PWM
```

主要证据：

- `resource/lander_pi/src/driver/controller/controller/odom_publisher_node.py:143,241`：订阅 `controller/cmd_vel` 和 `cmd_vel`；麦轮分支调用运动学再发布电机命令。
- 同目录 `mecanum.py:25,46`：以轴距、轮距和轮径将机体速度转换为四轮 RPS；`MotorState.id` 为 1–4。
- `resource/lander_pi/src/driver/ros_robot_controller/ros_robot_controller/ros_robot_controller_sdk.py:344`：将 ID 和 RPS 编码成串口电机命令。
- `resource/RosRobotControllerLite_ros_250811/Hiwonder/System/packet_handle.c:397`：MOTOR 命令直接设置单/多电机 PID 速度目标，不调用麦轮运动学。
- 同固件 `Core/Src/stm32f4xx_it.c:419`：编码器读取和电机控制闭环。

固件中也存在另一套麦轮运动学：

- `Hiwonder/Chassis/mecanum_chassis.c:42` 实现机体速度到四轮速度，再转为 RPS。
- `Hiwonder/Portings/chassis_porting.c:35,46` 设置车型、几何和电机方向映射。
- Keil 工程引用了这些源文件，但 `Hiwonder/System/app.c:54` 一段中的 `chassis_init()` 调用被注释；默认串口 MOTOR 路径不经过它。

因此要区分“固件有实现”和“当前运行链路启用”。仅从本次源码能确认 ROS 端执行逆运动学，不能断言目标设备当前烧录版本与这些文件一致。

ROS 参考运动学采用 m/s，固件独立底盘库的线速度采用 mm/s，几何和电机方向约定也应分别核对。未来若改为 MCU 直接接收机体速度，需要明确启用初始化、协议及单位转换，不能同时在 ROS 和 MCU 做两次逆运动学。

现有 ROS `odom_raw` 使用命令积分，且位置积分未完整处理侧向速度；不能把它当作实测全向里程计。真实硬件阶段仍需打通编码器/IMU 等反馈，并保留有限超时、响应匹配及板端 watchdog。

## 3. 当前 Gazebo 仿真了哪些层

当前 `ros2_ws/src/lelab_mobile_description/urdf/gazebo_sim.urdf.xacro` 同时加载：

- `MecanumDrive`：四轮关节运动与轮式里程计。
- `VelocityControl`：接收相同平面速度 topic，直接给模型设置机体运动。

因此，模型中有麦轮结构及运动学控制，但车体运动并非仅由四轮滚子/地面接触产生。手臂、夹爪与物体接触是另一条物理执行链，不能因底盘简化而忽略抓放接触验收。

官方源码核对：

- [Gazebo Sim 8 VelocityControl](https://github.com/gazebosim/gz-sim/blob/gz-sim8/src/systems/velocity_control/VelocityControl.cc)：写入模型 LinearVelocityCmd/AngularVelocityCmd。
- [Gazebo Sim 8 MecanumDrive](https://github.com/gazebosim/gz-sim/blob/gz-sim8/src/systems/mecanum_drive/MecanumDrive.cc)：驱动轮关节，其里程计从四轮 JointPosition 更新。

### 必须修正的数据来源

当前 agent 和 exporter 用 `/lelab/base/odom` 的轮式里程计作为 state 的前三维，而车体由 VelocityControl 影响，二者可能不一致。action 是目标，state 是结果，数值本来允许不同；问题在于 state 的观测来源可能与实际车体运动脱节。

后续平面模型模式必须：

1. 明确一个机体运动执行来源。
2. 从仿真模型实际速度取得机体反馈，或由实际世界位姿差分并正确转换到机体坐标系。
3. 将轮式里程计作为独立诊断流，不能混称为已经验证的机体实际速度。
4. 保留限速、加速度/停止响应、TTL、断连保护、时间戳与观测年龄，验证前进/横移/转向的方向和量级。
5. 在 profile、session、导出来源中记录 `planar_velocity` 等实际模型身份；旧数据保留原貌，必要时产生重新评估/派生版本，不静默覆盖。

采用平面模型足以推进当前上层软件/VLA 接口验证，但不由此得出实机迁移性能或轮地动力学验证结论。

## 4. 放置场景调整

用户希望托盘壁更高，也接受把方块放到箱子上面。下一轮优先建立参数化的箱体顶面放置任务，保留高壁托盘作为另一场景：

- `place_on_box`：目标为箱顶区域和高度；检查方块受到箱面支撑、位置合规、释放后稳定且夹爪撤离。
- `place_in_tray`：检查方块整体位于托盘有效内部，不穿壁，底部支撑正确，并有必要的越壁净空。
- 两类任务语言、成功判定与数据身份分开，不沿用同一个“inside container”布尔条件。
- 箱体高度、目标区域、抬升和释放净空参数化，先验证手臂可达性及碰撞，再调整专家轨迹，不提高场景后仍硬套旧关节序列。
- 保留目标位置确认接口/专家阶段：记录目标 pose、坐标系、确认来源和时间；脚本使用仿真真值时记录特权信息，不能冒充视觉识别或真人示教。
- 抓放任务仍必须补足底盘接近阶段。

已有成功判定中 profile 的 3 cm 抬升阈值与代码硬编码 2 cm 不一致，仍属必须修复项：使用单一配置来源并重新评估，不为使旧样本通过而调整阈值。

## 5. 腕部相机双配置

用户已确认保留：

- `wrist_original`：原始安装方向，不增加演示用俯仰。
- `wrist_demo_downward`：演示俯视方向，当前虚拟俯仰为 0.55 rad；与实机安装尚未校准。

后续实现需把相机 profile ID、挂载变换/外参及其版本写入 session 和数据集来源。两个配置分别标记，不把相同 feature key 当作相同外参证明；合并或训练前做显式兼容检查。实机阶段根据实际安装和标定校准，不宣称当前虚拟俯仰已经在硬件验证。

截至本次核查，运行源码仍将俯仰硬编码为 0.55 rad；双配置是已经确认、尚待实现的要求。

## 本次范围

已完成 FreeRTOS、ROS、当前 Gazebo 执行/反馈三条只读核查，汇总用户对场景和相机的选择。本次未修改运行配置、生成新数据、启动/停止服务、编译固件或移动设备。下一轮实施应结合既有工程收尾项：录制节流、导出进程隔离/取消、reset 分段及租约/断连端到端验证。
