# 树莓派实机环境评估与开发规划

评估日期：2026-10-31。目标：`pi@192.168.19.138`。源码基线：`codex/ros2-mobile-collection` / `4beecee`。最新状态：**用户重启后已确认 Humble 容器，实收 RGB/深度/IR/IMU/状态数据；MCAP、时钟、雷达及数据语义仍需处理**。

第 1–5 节保留首次断连前的评估快照。第 6 节为重启后的新证据，运行环境与传感器现状以第 6 节为准；此前“容器和相机未确认”已被后续核验补齐。

本轮由主智能体执行连接与有限重试，三个只读子智能体分别核查系统设备、ROS/容器、车端采集与上位机导出适配。所有结论按实际返回范围记录；没有把本地参考源码、历史仿真结果当作实机运行结果。

脱敏证据汇总：[raspberry-pi-assessment-20261031.json](../evidence/raspberry-pi-assessment-20261031.json)。这是结构化测量摘要，非完整原始终端转录；系统子智能体未记录远端测量的 UTC 起止时间，故不补造具体时刻。后续执行提示：[下一阶段 Goal prompt](../ros2-hardware-observation-goal.md)。

## 1. 实测结果

| 项目 | 本次结果 | 含义与边界 |
| --- | --- | --- |
| 主机 | Raspberry Pi 5 Model B Rev 1.1；aarch64；4 核 Cortex-A76 | 可以规划轻量车端采集服务；尚无实际负载测试 |
| 操作系统 | Debian GNU/Linux 12 bookworm，内核 `6.6.20+rpt-rpi-2712` | 不是此前设想的 Ubuntu ROS 宿主 |
| Python | 宿主 `3.11.2` | 不满足上位机 LeLab 的 Python ≥3.12 要求；不应把整套上位机依赖直接装入宿主 Python |
| 内存 | 总量约 7.9 GiB、可用约 7.2 GiB | 单次快照；不代表录像、压缩和驱动同时运行的性能 |
| 根盘 | `/dev/mmcblk0p2 ext4 57G 46G 8.7G 85% /` | 安装大型镜像及连续原始录制前先做容量预算；没有清理或改分区 |
| 温度/节流 | 约 45.2–45.5°C；`throttled=0x0` | 只反映查询时工具报告的标志；不能代替电源、满载稳定性验证 |
| 串口 | by-id 链接分别指向 `ttyUSB0`、`ttyACM0`，权限 `crw-rw-rw-` | 仅枚举；实际控制板/雷达身份、协议和固件版本待确认 |
| 视频 | 存在 `video19`–`video37`；`root:video` 权限带 ACL；pi 属于 video 组 | 未取得 sysfs 名称或 by-id，不能断定哪些是图像采集节点、编码器或其他节点 |
| ROS 宿主 | 未发现 `/opt/ros`、检查 PATH 中的 `ros2`、四个常见工作区或匹配的 ROS 进程 | 不能据此断言设备没有 ROS，仍可能在其他路径或容器 |
| Docker | `docker ps` 触发 Go runtime 的 invalid-pointer 崩溃 | 容器列表、daemon 状态、镜像和 ROS 发行版均未核实；不能直接判定 Docker daemon 损坏 |

USB 枚举包含 CH340、USB Single Serial、音频设备、hub 和名称为 Linux Foundation Aurora 930 的设备。这些名称不能单独证明相机类型、深度功能或对应的 ROS 驱动。没有打开串口、相机；`fuser` 未列出持有进程只表示当次查询结果，不能证明没有其他所有者。

### 连接与无线边界

本机到目标的实际路由是 `192.168.19.137 → enx00e04c68034c → 192.168.19.138`，本轮走有线。Pi 的 eth0 为 UP/LOWER_UP，默认路由经 `192.168.19.1`；wlan0 为 UP/DORMANT，另有 `192.168.149.0/24` 路由。没有核实无线接入方式，不把它解释成正常的 Wi-Fi 上行连接。

两次短 ping 分别收到 2/3 和 6/10，后者 RTT 平均约 0.283 ms。SSH 曾成功认证并返回系统信息，后续多次停滞、断开或超时；三次单独的 TCP banner 探测一次超时、两次成功。最后直接 SSH 尝试超时。接口累计 errors/dropped 的零值不能否定这些端到端现象。

Docker 客户端首次报错包含 `runtime: bad pointer in frame runtime.(*mcache).refill` 和 `fatal error: invalid pointer found on stack`。为避开重复调用崩溃客户端，后续尝试只读 Docker Unix socket 的 version/container-list GET，但连接未返回可用结果。网络、SSH 与客户端崩溃是否同因，当前没有证据；没有据此重装 Docker、重启 Pi 或修改网络。

因此，本轮未能核验：容器里的 ROS、节点/topic/QoS/频率、相机帧、时钟同步、MCAP 插件、串口反馈、已烧录固件、Wi-Fi 吞吐、实际录制。后续第一步应从上述缺口继续，不能复用“未找到宿主 ROS”作为重装系统的依据。

## 2. 已有程序不能直接复制到实机启动

上位机应用仍依赖本地定制 LeRobot，留在当前 Python ≥3.12 环境。车端 `ament_python` 包可以独立适配，但**当前版本尚不具备硬件模式**：

| 代码位置 | 必须处理的问题 |
| --- | --- |
| `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/agent.py` | 顶层强制导入 Gazebo Contacts；健康检查依赖方块/容器真值；source、时钟与任务评估写死为仿真；启动即建立执行器发布端 |
| 同目录 `watchdog.py` | Gazebo 归零兜底不是硬件 watchdog；只读模式不应启动任何控制/停车发布路径 |
| 同目录 `recorder.py` | bag 进程提前失败或被强制结束后，退出结果未可靠传播到 error，可能仍发布 READY；必须校验退出方式、文件和可读性 |
| `app_ros_mobile/lelab/ros_mobile.py` | 缺关节补零、缺 action 合成动作、缺真值位置使用占位、header stamp 回退没有充分记录来源；实机训练导出必须拒绝不完整语义 |
| `ros2_ws/src/lelab_mobile_agent/package.xml`、`setup.py` | 分离仿真可选依赖，声明实际 NumPy/Pillow/ROS 依赖并在目标环境验证 |
| `configs/ros2_collection/runtime-profile-v1.json` | hardware 映射尚为空；不能将 Gazebo 关节名、topic、相机外参、640×480 等视为硬件配置 |

控制记录至少区分 `requested command`、网关限幅后 `sent command`、有真实回执时的 `acknowledged/applied`、传感器 `measured/estimated state`。发布动作不等于电机执行；控制目标缓存不等于关节实测。

只读阶段只保存确实观测到的图像、状态和事件。没有动作流就不生成九维训练 action；没有关节或底盘反馈就不填零凑 state。episode 可以是 `unknown/unlabeled`，不能套用 Gazebo 成功判定。缺少训练必要项时，保留 raw 并给出可解释的导出拒绝。

## 3. 运行位置与环境选择

推荐边界保持不变：

```text
树莓派：真实驱动 → ROS 图像/反馈 → 只读 agent + episode 原始录制
                                      ↓ 暂存传输、size/SHA-256、原子发布
当前上位机：LeLab 网页/API → 数据检查/对齐 → 本地定制 LeRobot 导出
```

预览优先压缩、降采样与有界队列；原始录制留在车端，网络断开不依赖上位机收流保存。暂停上传/预览不应阻塞原始录制。上位机和 Pi 通过稳定 agent 协议连接，不要求跨 ROS 发行版直接 DDS 互通。默认回环绑定与 SSH 隧道，不增加公网暴露。

先核实并复用现有容器/ROS 环境。若确实没有可用运行时，才比较“隔离的 Ubuntu ARM64 ROS 容器”和“独立介质上的 Ubuntu 原生环境”，结合相机厂商驱动、设备映射与存储做选择。Jazzy 官方目标中 Ubuntu 24.04 arm64 为 Tier 1，Debian 12 arm64 为 Tier 3 源码支持，因此不能把 Ubuntu 的安装流程直接套在当前 Debian 宿主。来源：[ROS 官方 REP-2000](https://github.com/ros-infrastructure/rep/blob/master/rep-2000.rst#jazzy-jalisco-may-2024---may-2029)，2026-10-31 核验。官方 docs.ros.org 页面本轮被访问保护拒绝，改查上述官方源码。

目前不需要在 Pi 安装完整 LeLab、torch、LeRobot、Gazebo 或训练环境。MCAP、相机 SDK 和 ROS 包以车端真实运行环境为准，不能在没有插件的情况下静默切格式。

容量预算示例：单路未压缩 RGB，640×480×3×15 ≈13.8 MB/s，约 0.83 GB/min；10 分钟约 8.3 GB，尚未计深度、雷达、消息封装与其他日志。此为算术估算，非实机码率测试。根盘余量不适合未经预算的连续 raw 录制；下一阶段先验证短片段、磁盘水位门禁和正常停录，再实测压缩负载并规划独立数据介质。不得自动删除现有文件腾空间。

## 4. 分阶段开发与验收

| 阶段 | 实施内容 | 完成证据 |
| --- | --- | --- |
| P0 环境确认 | 稳定 SSH；Docker CLI/daemon 分别定位；确认 ROS 所在位置、驱动来源、设备身份、相机与时间戳、可用容量 | 新鲜环境清单与实际 topic/消息来源；缺口保留错误，不重装掩盖 |
| P1 实机只读采集 | sim/hardware adapter 分离；只读模式不建立执行器 publisher/client；配置真实 topic；网页预览、start/stop episode、车端 raw、本机校验同步 | 真实图像、短 bag 可读、时间戳与来源清楚；无 actuator 输出；网络断开仍能本地收尾 |
| P1 并行本地修复 | 录包退出/READY；缺数据导出拒绝；墙钟/源时间语义；节流与磁盘门禁；大导出进程隔离/取消 | 针对性故障测试与仿真回归；缺 action/反馈时明确拒绝训练导出 |
| P2 仿真契约收尾 | 修正平面执行与底盘 state 来源；双腕部相机 profile；箱顶放置与目标确认阶段；抬升阈值统一 | 前进/横移/转向反馈与实际车体一致；两配置分别标记；箱面支撑、释放稳定性与阈值评估 |
| P3 真实控制 | 确认固件运行链路；编码器与真实舵机反馈、有限超时/ID 匹配、板端 watchdog；统一租约/TTL/急停、键盘与主臂接口 | 完成静态与无运动检查后，另行进行授权的低速小幅实测、断连停车与恢复不重放旧动作 |
| P4 真实任务数据 | 键盘底盘＋主臂/专家，记录动作目标及真实反馈；目标确认、校准和任务标注；完整九维 LeRobot 导出 | 实机 episode 的 action/state/图像来源可追溯，本地 SDK 和网页读回；再讨论训练/推理 |

底盘 action 沿用机体平面 `(vx, vy, wz)`；实机路径究竟在 ROS 还是 MCU 做逆运动学，须以已运行程序/固件确认，不能重复做两次。此前本地 ROS 参考采用四轮 RPS 串口下发，`odom_raw` 为命令积分、部分关节发布为目标缓存；这些只是参考源码事实，不是实机测量结论。真实轮地力学精确仿真不是 P1/P2 前提。

相机继续保留 `wrist_original` 与 `wrist_demo_downward`，实机只使用实际安装/校准的 profile。不能把演示俯视 0.55 rad 直接当作实机外参。箱顶放置先做臂展/碰撞检查，参数化高度与顶面有效区域；高壁托盘作为不同任务。当前 0.03 m 配置与 0.02 m 硬编码判定冲突需统一，旧数据保留来源，不为保住历史成功而改门槛。详见[已有接口决策](../ros2-vla-base-interface-decision.md)。

## 5. 本轮执行边界

没有执行安装、容器/服务/ROS 节点启动或重启，没有改网络，没有打开硬件设备，没有发送运动或零速命令，没有烧录固件。没有修改发布快照、current、运行代码或既有数据。本轮只增加评估证据、规划与 Goal 文档；用户原有未提交修改保留。

实机相机和原始录制均尚未验收。连接恢复后应从 P0 缺口继续；当前有线测量不代表无线采集通过。

## 6. 用户重启后的复查（当前结论）

本次由主智能体及三个只读子智能体继续核验。复查证据：[raspberry-pi-recheck-20261031.json](../evidence/raspberry-pi-recheck-20261031.json)。本机观测时间为 2026-10-31 15:44–15:54 UTC 左右；Pi 自身时间错误，不能把其日期当成本次评估的真实日期。

### 6.1 SSH 恢复，既有 ROS 可以复用

新一轮有线 ping 为 10/10、0% 丢包，平均约 0.249 ms；SSH 查询成功。Docker CLI 本次正常返回，ssh/docker/containerd 服务均 active。重启后恢复不代表此前故障原因已经定位。

ROS 实际在运行容器 `MentorPi`（镜像标签 `ros:humble`，ID 前缀 `adb8457c2eec`）内：Ubuntu 22.04、Python 3.10.12、ROS 2 Humble。容器使用 host 网络；现有 web_video_server 监听 8080，rosbridge 监听 9090。工作区为：

- `/home/ubuntu/ros2_ws`
- `/home/ubuntu/third_party_ros2/third_party_ws`

已有 bringup 自启动包括驱动、相机、雷达、应用、摇杆和 init pose；本次没有重启或启动它。不得为采集重新拉起整车入口，避免重复控制器和初始化动作。

运行驱动进程 UID=1000；白名单环境为 `LanderPi_Mecanum`、相机 `aurora`、雷达 `MS200`、ROS_DOMAIN_ID=0、ROS_LOCALHOST_ONLY=0、need_compile=False。默认 `docker exec` 的 UID 为 0，不能照搬默认执行身份部署采集器。

### 6.2 被动订阅已收到真实流

主智能体只订阅既有发布者；审计节点禁用 rosout/外部日志和参数服务，不建立执行器发布端或控制客户端。第一次默认 UID0 的约 14 秒观测发现话题但没有消息；第二次以 UID1000 和显式匹配的 ROS 环境运行约 14 秒，收到下表数据。该结果验证了可用的执行配置，尚未单独隔离 UID、共享内存及启动时序的因果关系，不把初次失败解释成相机硬件故障。

| 真实 topic | 本次消息数 | 接收频率约值 | 消息格式 |
| --- | --- | --- | --- |
| `/ascamera/camera_publisher/rgb0/image` | 118 | 8.59 Hz | 640×400，bgr8，rgb_camera_link |
| `/ascamera/camera_publisher/depth0/image_raw` | 163 | 11.76 Hz | 640×400，mono16，depth_camera_link |
| `/ascamera/camera_publisher/ir0/image` | 188 | 13.53 Hz | 640×400，mono8 |
| RGB/depth 各自 camera_info | 各 206 | 各 14.84 Hz | 640×400，plumb_bob，带内参和畸变参数 |
| `/controller_manager/joint_states` | 602 | 46.79 Hz | joint1..5 + r_joint |
| `/joint_states` | 259 | 20.06 Hz | 含车轮、手臂、夹爪/mimic 名称 |
| `/ros_robot_controller/imu_raw` | 561 | 46.35 Hz | IMU 原始接口消息 |
| `/imu`、`/imu_corrected` | 各 597 | 各 46.42 Hz | 处理后的 IMU 消息 |
| `/odom` | 387 | 30.04 Hz | odom → base_footprint |
| `/odom_raw` | 572 | 47.24 Hz | odom → base_footprint |

频率是同一 Python 多流订阅器的短窗接收统计，不能等同于相机本征 FPS、源发布频率或量化丢帧率。后续需要分流测试、原始录制负载和有界队列统计。

另从 RGB topic 成功读取一帧并仅编码为 JPEG，保存在本机 [aurora_rgb.jpg](../../work/ros2_collection/pi_assessment_20261031/aurora_rgb.jpg)，640×400、16,804 字节；源 header 为 `1775670672344000000 ns`。画面主要为近处表面与夹爪附近边缘，后续采集需确认工作区可见范围。该文件属于本地运行证据，不写入发布快照。**这是 ROS 实帧采样，不代表已完成 LeLab 网页预览。** 既有 HTTP snapshot 接口一次直连请求关闭而未返回图片；初次经默认代理的 502 不能归因于 ROS 相机本体，已分别记录。

独立再对 `/scan`、`/scan_raw` 订阅约 10 秒，两者均 0 条消息；同一 UID1000 节点此时可以发现 `/aurora/aurora`、`/ros_robot_controller`、`/odom_publisher`、`/servo_manager` 等完整得多的节点名称。雷达话题存在不代表雷达采集可用，需后续定位 MS200 驱动/串口/发布路径。点云仅确认话题存在，未采样。

### 6.3 关键缺口已经具体化

1. **时钟未同步。** 本机 UTC 为 2026-10-31，Pi 报 2026-04-08，NTP active 但 synchronized=no，RTC 接近 1970。消息 header 也落在 Pi 时钟附近。采集前应建立时间策略和时钟同步；不能直接以两台主机墙钟相减计算年龄或对齐。容器继承宿主时间，不能靠改容器时区修复。应避免录制中或运动中突然校时；本次没有改时钟。
2. **MCAP 插件缺失。** ros2 bag CLI 可用，但 storage 清单只有 sqlite3 及测试插件，检查范围未发现 mcap。当前 recorder 硬编码 `--storage mcap`，直接部署会失败。下一阶段在兼容的 Humble 环境补齐并验证插件；若另选格式，必须显式更新 profile、契约和读回验证，不能静默替换。
3. **真实消息不等于真实状态。** 实机驱动四个关键文件与本机 `resource/lander_pi` 哈希相同：ROS 做麦轮逆运动学，向板端发送四轮 rps 目标；`odom_raw` 由命令积分，位置没有完整使用 lateral 分量。ServoManager.get_position 返回发送目标缓存；SDK 有总线舵机位置请求路径，本次没有调用。不能用收到的 joint/odom 消息来宣称已打通编码器与关节测量闭环。
4. **夹爪映射不一致。** 本次 `/controller_manager/joint_states` 的 `r_joint` 首值约 1.466 rad；`/joint_states` 使用 `gripper_base_joint` 等名称且首值为 0。需要确认映射、零位、单位和是否占位，不能直接互换或当作两份测量证明。
5. **安装布局需保留。** 容器内存在 `src/driver` 与 `src/hx2/lander_pi/src/driver` 副本；安装 egg-link 经 build 最终指向 `src/driver`。部分控制器节点副本存在方法名差异，不能任意编辑快照副本期待生效。此为安装映射证据，不是当前 Python 进程内存导入路径证明。新采集代码应有独立源码/overlay 和持久数据路径，不依赖现有容器可写层作为唯一副本。
6. **相机标定与资源仍需验证。** 驱动提供 RGB/深度 camera_info；深度单位、配准、光学 TF 与腕部外参尚未验证。video19 是 rpivid、video20–37 为 pispbe 图像处理节点，Aurora 使用已有 SDK/ROS 驱动出图，不能要求它必须出现普通 USB V4L2 camera 节点。根盘仍仅 8.7G，温度快照 49.9°C、节流0x0，未做录制性能测试。

### 6.4 对开发顺序的修订

无需先重装 ROS 或操作系统。以现有 **Humble / Python3.10 / UID1000** 环境建立轻量车端只读采集 overlay，保持上位机 Python≥3.12 与定制 LeRobot。先解决时间与 MCAP，配置真实 640×400 RGB/depth/topic，验证短 episode 和校验同步；雷达作为明确标记不可用的可选流继续排查，不能用空数组冒充实测。反馈、watchdog 与运动控制仍为后续阶段。

此前规划的仿真平面模型、箱顶任务、双腕部相机 profile 和数据完整性修复继续有效。实际相机配置必须按本次消息及安装标定建立，不能照抄仿真的640×480或俯仰0.55rad。

本次新增了被动 ROS 采样及本机一帧图片保存；没有直接打开串口/相机设备，没有发动作、调用硬件服务、录包/回放、安装、校时或重启服务。真实原始录制、LeLab 网页接入、同步导出和 Wi-Fi 性能仍待下一阶段实施。

收尾检查发现工作区在本轮期间新增了 agent/recorder/setup、runtime-profile、hardware-readonly profile 与 launch 等未提交改动。本轮只修改评估/Goal文档和证据，没有创建或验收这些代码变更，也不推定其作者；上文代码问题基于本轮起点的核查。后续先审阅当前差异、复用已完成部分，再验证，不能按旧问题清单重复覆盖正在开发的实现。
