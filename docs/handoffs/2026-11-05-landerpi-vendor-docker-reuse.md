# LanderPi vendor 源码与 Docker 复用分析

分析时间：2026-11-05（America/Los_Angeles）。本轮只读分析
`/home/liaozhengyan/Desktop/resource/lander_pi`，未启动或操作小车。

## 结论

可以复用，而且应该以 Pi 上正在运行的 `MentorPi` 容器作为 ROS 运行时和
vendor 依赖基准；不能把 `/home/liaozhengyan/Desktop/resource/lander_pi`
整个目录直接覆盖到容器，也不能把上位机的 Jazzy/Python 3.12 环境当作从臂
运行验收环境。

复用边界：

| 复用 | 不复用/需隔离 |
| --- | --- |
| ROS 2 Humble、Python 3.10.12 | 上位机 Jazzy 或系统 Python |
| 已安装的 custom msgs | 在宿主重新安装同名消息包 |
| 已编译 third-party 相机/雷达库 | 重新拉取/升级第三方驱动 |
| vendor `JointPositionController` 的 240°/1000 tick 换算 | 在 UI 中另写一套不透明单位转换 |
| 原厂 `/dev/rrc` 唯一 owner 和 `GetBusServoState` | 第二个串口读取进程 |
| `peripherals` 相机 launch 的 Aurora 参数/remap | 自建第二套相机发布者 |
| 原厂从臂控制板上已有的服务与 topic | 原厂 full bringup 的旧控制发布者 |

## Vendor 源码事实

- 目录约 226 MiB、943 个文件、25 个 ROS `package.xml`。
- 目录没有 `.git`、Dockerfile、Compose 或运行镜像定义，只有源码、启动脚本和文档。
- `files_des.md` 明确写出控制链：
  `/arm_controller/... -> controller_manager -> /servo_controller -> ServoManager ->
  /ros_robot_controller/bus_servo/set_position -> Board -> STM32`。
- `.robotrc` 假定工作区位于 `/home/ubuntu/ros2_ws`，并显式 source
  `/opt/ros/humble`、主工作区 install 和 `third_party_ros2/third_party_ws/install`。
- `.typerc` 设置 `MACHINE_TYPE=LanderPi_Mecanum`、`DEPTH_CAMERA_TYPE=aurora`、
  `LIDAR_TYPE=MS200`、`ROS_DOMAIN_ID=0`。
- vendor `.typerc` 的版本字符串是 `V1.0.0|2025-11-08`，而本次失败前从 Pi
  实际运行环境读到的版本是 `V1.0.0|2026-05-26`，语言配置也从源码的
  `Chinese` 变为现场 `English`。因此 source snapshot 与已部署副本不是完整
  版本等价。

### 核心文件指纹

| 文件 | SHA256 |
| --- | --- |
| `ros_robot_controller_sdk.py` | `f7c1584b5b1f393041bd86c16c68310d38969c15bf8dfd1f92df10373a6cc504` |
| `ros_robot_controller_node.py` | `0c4b5df728f18c4a7d1850f0647e26a0d1c892ab2455f3fa0079319a9d2c619f` |
| `servo_controller.py` | `709e42977ea8c98b0e31a4f58f89f5fdf2e2e83a35589eef1e37a39e2e7c84a4` |
| `controller_manager.py` | `e49cba56878e9d80a751d2826da4650819eb78a651e7c2e56cc4bb375c645142` |
| `joint_position_controller.py` | `abd8ace10e5997b8d39483ae05c86bf312adbf470de0f63aa7e56a8d86816f04` |
| `servo_controller.yaml` | `fa68e017c13fda3735f0daa5a96f2f9221f6e43235cf3eb662b3342331b6a594` |

前三个文件中的 board SDK/node 指纹与历史 T1 BASE 以及本轮容器读取值一致；
servo controller 也与历史容器核对值一致。这足以证明局部复用基础，但不能证明
整个 25 包工作区、相机驱动或系统配置完全一致。

## 关键代码行为

1. `ros_robot_controller_node.py` 构造时就调用 `enable_reception()`、
   `load_servo_offsets()`、`send_machine_type()`，并下发四个底盘电机零速。
   因此启动它不是纯连接操作。
2. `ros_robot_controller_sdk.py::bus_servo_read_and_unpack()` 原版使用无超时
   `queue.get(block=True)`，且不校验回复 ID/子命令/长度/状态。协议无事务 ID，
   任意迟到帧都可能污染后续读取。
3. `servo_controller.py` 的 `ServoState` 从 500 初始化，`connect()` 是空操作，
   只有 `set_position()` 才更新缓存。`controller_manager.py` 又把这个缓存发布成
   `/controller_manager/joint_states`。因此该 topic 是命令缓存，不是六路实测。
4. `joint_position_controller.py` 的可用基准换算是：
   `radians_per_tick = 240/360*2π/1000`；
   `rad = (raw-500)*scale`，若 YAML `min > max` 则方向翻转。
   `r_joint` 的 `min=1000,max=0` 正是翻转项。B 层应复用此语义，并实测软限位。
5. `controller_manager.py` 读取 `base_frame` 参数；不传时发布线程会因
   `frame_id=None` 退出。本轮最小栈已显式传 `base_frame=base_footprint`。
6. `bringup.launch.py` 同时启动底盘、相机、雷达、rosbridge、web video、全部 app、
   手柄和 `init_pose`。`init_pose.yaml` 默认 `type=action/action_name=init`，
   启动时会发机械臂动作。因此它不能作为 LeLab 独占臂控制的最小启动入口。

## Docker 运行事实与风险

历史现场只读证据和本轮只读检查确认：

- 容器名为 `MentorPi`，镜像 `ros:humble`，使用 host network、privileged、
  `/dev` 映射，restart policy 为 always。
- 2026-11-05 04:30Z 重启后实测容器 image ID：
  `sha256:4863da8b74e6aa185bfd5f6a40ad4462e8fd6b5b66411881ed2be6dbccdb4898`；
  `Config.Image` 仍为可漂移的 `ros:humble`，`Cmd=["tail","-f","/dev/null"]`。
- 容器 mounts：`/home/pi/docker/tmp:/home/ubuntu/shared` 可写；
  `/dev:/dev`、X11 和 PulseAudio bind。主 ROS 源码树
  `/home/ubuntu/ros2_ws` 没有单独 bind mount，位于容器 writable layer。
- 容器内 Python 3.10.12，ROS Humble；相机、雷达、rosbag2 sqlite3、custom msgs
  和 controller 相关包都能在容器内导入。
- 主机系统的 `start_app_node.service` 通过
  `/home/pi/robot_pi/start_node.sh` 调用 `docker exec` 启动 full bringup；
  vendor source 本身不包含容器构建文件。

可复用，但有三个可靠性风险：

1. 镜像 tag `ros:humble` 未固定 digest，单靠源码目录无法重建当前精确镜像。
2. `/home/ubuntu/ros2_ws/src`、`build`、`install` 和
   `third_party_ros2/.../install` 是运行时事实来源；其上可能已有
   2026-05-26 版本差异和编译产物。
3. 容器 writable layer、宿主 Docker 数据目录与源码快照没有自动版本绑定。
   修改容器内共享路径会造成不可见漂移。

当前 LeLab 部署目录 `/home/ubuntu/lelab_car` 也不在 bind mount 内；它能承受
容器 stop/start，但容器若被删除或重建，部署代码和 sessions 会一起丢失。
唯一已确认的持久共享入口是：
`/home/pi/docker/tmp -> /home/ubuntu/shared`。
下一次离线部署应先改为版本化的 `/home/ubuntu/shared/lelab_car`（宿主侧
`/home/pi/docker/tmp/lelab_car`），迁移时保留现目录只读，待新路径完成哈希
校验后再切换启动脚本。

## 推荐复用方式

1. 只在 `docker exec -u ubuntu MentorPi` 内运行车端 ROS；每次显式 source
   `.typerc`、`/opt/ros/humble/setup.bash`、主工作区和 third-party overlay。
2. 把 LeLab 改动放在容器 `/home/ubuntu/lelab_car` 隔离目录；原厂
   `/home/ubuntu/ros2_ws` 只作为只读依赖和消息/接口来源。
3. 对 board 读取补丁继续使用隔离副本
   `/home/ubuntu/lelab_car/vendor/ros_robot_controller`，通过 `PYTHONPATH`
   选择补丁源码；不覆盖原厂包。
4. 启动 profile 使用最小栈：board owner、controller manager、按需相机；
   不启动完整 bringup、init_pose、joystick、视觉 app。
5. 从臂 B 层继续读取 `GetBusServoState` 的 position raw，使用原厂
   `JointPositionController` 换算规则；`/controller_manager/joint_states`
   仅用于检查控制器是否运行，不能作为 state 真值。
6. 若建立独立开发镜像，应从当前容器导出 `docker inspect`、镜像 digest、
   关键 source/install 哈希，再以固定 digest 构建；不在没有 pin 的情况下
   重新 pull `ros:humble`。
7. 上位机 Jazzy/Python 3.12 可继续跑单元测试、构建前端和做协议测试，但最终
   ROS 接口、bag、serial 和 controller 验收必须在容器 Humble/Python 3.10 内完成。

## 从臂标定影响

当前 `landers` UI 的 B 层应固定指向 Pi 容器里的从臂，而不是上位机 USB
SO101 follower。它只使用：

- `/dev/rrc` 唯一 owner：`ros_robot_controller`
- six servo IDs：1,2,3,4,5,10
- read：`/ros_robot_controller/bus_servo/get_state`
- command：`/servo_controller` → controller manager → board owner

上位机负责 UI、SO101 主臂读取、标定资产和哈希门禁；Pi 负责从臂 raw、
vendor 单位换算、执行 owner 和相机。这样既复用原厂代码，也不把车端
实时控制错误地搬到不可靠的宿主环境。
