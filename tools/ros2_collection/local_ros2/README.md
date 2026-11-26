# 本地 ROS 2 Humble 离线运行时（无容器引擎、无 sudo）

对应方案：`docs/handoffs/2026-11-05-landerpi-local-ros2-offline-development-plan.md`
当前状态与门禁结论：`docs/handoffs/2026-11-05-landerpi-local-ros2-offline-status.md`

## 为什么不是 docker

本机没有 docker/podman/`/opt/ros`/Python 3.10，`sudo` 需要密码，且 Docker Hub 直连与本地代理
均不可达（`CONNECT aborted`）。因此 `ros:humble-ros-base-jammy` 的 registry digest **未取得**，
`lock/env.lock.json` 里记为 `NOT_VERIFIED`，可追溯性改用官方 Ubuntu base rootfs 的 SHA256
（与发布目录自带 `SHA256SUMS` 核对）加固定 deb 版本清单。

实际隔离方式是两层，缺一不可：

```text
bwrap --dev /dev ... -- unshare --map-root-user --mount -- bash -c '<命令>'
```

- 单独 `unshare --map-root-user`：能拿到 uid 0，但本机内核/AppArmor 拒绝写字符设备，
  `echo x > /dev/null` 失败，dpkg postinst 直接崩。
- 单独 `bwrap`：`/dev` 可用，但进程仍是 uid 1000，apt/dpkg 装不了包。
- 嵌套两层同时得到可写 `/dev/null` 与 uid 0，且不开 PID namespace，
  因此 `run/*.json` 里记录的 PID 就是宿主机可见 PID，可用 `/proc/<pid>/cmdline` 与 `ss -ltnp` 核验。

其它被内核挡掉的路径（不要再试）：`mknod`（任何命名空间都 EPERM）、`newuidmap`（不存在，
所以只能单 uid 映射）、`mount -t proc`（需要 PID namespace；改用 `--rbind /proc`）、
绑定 `/proc` 的非 rbind 形式。

单 uid 映射的连带后果：任何 chown/chgrp 到其他 uid 都会 EINVAL。`dbus` 的 postinst 用
`dpkg-statoverride` 把 launch helper 交给 `messagebus`，因此卡住整条 gtk/dconf 依赖链。
`bin/prepare` 关闭 recommends 并在检测到卡死时清除该链——这些包 rclpy/rosbag2/colcon 都不需要。

## 一条命令

```bash
tools/ros2_collection/local_ros2/bin/doctor      # 只读体检；FAIL 行即阻塞门禁
tools/ros2_collection/local_ros2/bin/prepare     # 首次需联网：下载 rootfs + 装 Humble
tools/ros2_collection/local_ros2/bin/build       # 暂存 vendor 消息（逐文件 SHA256）并 colcon 构建
tools/ros2_collection/local_ros2/bin/up          # 起模拟板卡 + 独立控制进程 + 租约发放器
tools/ros2_collection/local_ros2/bin/accept      # 协议/跟随/故障/租约失效 全套验收
tools/ros2_collection/local_ros2/bin/record      # 生产 SessionRecorder + 真实 ros2 bag record
tools/ros2_collection/local_ros2/bin/down        # 只停自己记录的实例（无 pkill）
tools/ros2_collection/local_ros2/bin/package     # Pi 部署候选包 + 清单（排除离线夹具）
tools/ros2_collection/local_ros2/offline/bench_links.py   # 三种链路基准
```

运行产物全部在 `work/ros2_collection/local_humble/`（rootfs、ws、run、logs、state、data、
evidence、dist），不写发布快照，不动 `current`。

## 安全默认

- 离线 profile 固定 `simulated`，不随环境变量改变；`bin/up` 不接受真实输出入口。
- 运行时**没有安装 pyserial**，也没有 `/dev/rrc`、`/dev/tty*`、`/dev/video*`，
  真实总线/相机在结构上不可达，而不是靠约定。
- 所有 ROS 名称在 `/lelab_offline/` 命名空间下重映射，不复用真实板卡 topic。
- `/repo` 与 `/vendor` 以只读绑定进入，并写入探针强制校验，失败即中止（保护未提交修改）。
- 端口占用先检查，PID 用 pidfile + `/proc` 指纹双重确认后才发信号。

## 断网复跑

`bin/prepare --offline` 会跳过 apt 步骤，仅使用已缓存的 rootfs 与 rootfs 内已装好的包。
`--offline` **不**等于已验证的完全离线重建：apt 缓存已 `clean`，所以在全新机器上重建仍需
一次联网下载（这一点如实记录，不写成已通过）。真正可断网重建需要先导出 deb 缓存/镜像。

---

# 追加：真正的 Docker 路径（2026-11-05 晚些时候，已装 docker）

用户授权后安装了 `docker.io 29.1.3`（apt），并配置了 daemon 代理——Docker Hub 在本机
**只有经 `127.0.0.1:7890` 才可达**，直连超时：

```text
/etc/systemd/system/docker.service.d/proxy.conf   # 仅 HTTP(S)_PROXY + NO_PROXY，不含任何凭据
usermod -aG docker liaozhengyan                    # 之后无需 sudo，组权限即可
```

镜像按 digest 固定（不是靠 tag）：

```text
ros:humble-ros-base-jammy@sha256:a3f4de78e42cca693a543ece5798aedcf1cae0171fe71c334a047332cc72ef93
  amd64 清单 10 层 / 压缩 264 MB / Ubuntu 22.04.5 / Python 3.10.12 / ROS_DISTRO=humble / 构建于 2026-10-27
  arm64 清单 sha256:48435c01f287… （Pi 那一侧的架构，供上机核对）
  注意：按 tag 拉取时 docker 报告的是 index digest（sha256:d9bcf2e6…），与上面的平台清单 digest 不同；
        构建用 FROM <平台清单 digest>，不要只信 tag。
```

一条命令：

```bash
tools/ros2_collection/local_ros2/bin/docker build          # 造 lelab/humble-dev:local
tools/ros2_collection/local_ros2/bin/docker up             # vendor 消息构建 + 模拟板 + 相机 + 独立控制进程 + 生产 agent（默认缴械）
tools/ros2_collection/local_ros2/bin/docker arm true|false # 经 agent HTTP：取租约→绑 epoch→（显式）给输出权限
tools/ros2_collection/local_ros2/bin/docker renew          # 续租约（上位侧周期性做的事）
tools/ros2_collection/local_ros2/bin/docker accept         # 容器内单测 + 唯一发放者核验 + preview/follow/faults + 旧入口拒绝
tools/ros2_collection/local_ros2/bin/docker faults [--kill-agent]  # 现场故障矩阵（含租约到期、agent SIGKILL）
tools/ros2_collection/local_ros2/bin/docker record         # 一次会话两集：真实 sqlite3 录包→归档→同步→受限模拟 6D 导出
tools/ros2_collection/local_ros2/bin/docker disarm         # 先在帧流上暂停，再由 agent 撤销输出权限
tools/ros2_collection/local_ros2/bin/docker doctor         # 安全形状、实例归属、进程与许可状态快照
tools/ros2_collection/local_ros2/bin/docker down           # 只停/删标签归属核验通过的自己的容器
```

授权权威只有一个：`up` 启动的是生产 `lelab_mobile_agent.agent`（`--ros-args -p
licence_socket:=… -p control_status_path:=…`），许可镜像与录制上下文都由它经
`agent_licence_bridge` 在同一 0600 unix socket 上发布；`offline/licence_agent.py` 只剩
协议单测夹具，启动器不再执行它，`assert-single-issuer` 会扫 `/proc` 证明没有第二个发放者。
`record` 的导出走 `POST /export/offline-simulated-6d` 那条受限入口
（契约 `simulated_offline_6d_v1`）：它不放宽任何门禁，普通 `/export` 对同一会话仍然拒绝，
产物始终 `data_identity=simulated_offline`、`training_eligible=false`。

形状与 Pi 的 `MentorPi` 对照：host 网络、Humble/Python 3.10.12；**刻意不同**：不 privileged、
不映射 `/dev`、`cap_drop ALL` + 最小 cap、`no-new-privileges`、仓库只读挂载、镜像内**没有 pyserial**。
`ROS_DOMAIN_ID=93`、帧端口 127.0.0.1:18122，与上面 bwrap 运行时（92/18121）互不冲突。

本路径实测（`work/ros2_collection/local_humble/docker/evidence/`）：容器内 65 项测试通过；
preview 4/4（板卡输出 0→0）；follow 8/8（118/119 准入、109 次发布、板卡收 110 条、每条 6 位置、
有效 8.93 Hz、反馈年龄 36.7 ms、往返 p50 100.2 / p95 100.3 ms）；faults 10/10；
真实 sqlite3 录包 `READY` 且 `raw_ready`，bag 内实测：
`recording/teleop` 39 行、`set_position` 39 条、`servo_readback` 21 次扫描、相机 image/camera_info 各 64。

仍未闭合：6D 导出/LeRobot SDK/页面读回（需要生产 `agent.py` 在容器内提供归档 HTTP 接口）、
三链路基准、arm64 与真机项。

## 追加：DDS 原生离线接通（2026-11-07）

`bin/dds_r1 up|verify|down|all` 在 domain 94、端口 18132/18140、独立状态目录
`work/ros2_collection/dds_r1` 中启动模拟板卡/相机、生产 agent、DDS profile 控制进程和
`upper_dds_bridge`，再跑生产 `SessionRecorder` + DDS 命令/反馈的贯通验证。它只停止自己
记录的 pid，不触碰既有 domain 93 实例。详细结果与门禁见
`docs/evidence/landerpi-ros2-native-r1-20261107/README.md`。

## 手柄底盘与机械臂联合录制（离线）

使用现有 Humble Docker 环境。手柄试验实例使用 domain 95、本机回环端口
18133（帧服务）、18142（agent）、18289（网页），工作目录为
`work/ros2_collection/dds_gamepad`。试验档从 `/lelab_offline/base/joy` 接收模拟
摇杆，将四轮目标作为一条 `MotorsState` 发往隔离的
`/lelab_offline/ros_robot_controller/set_motor`。同一目标的 Twist 发往
`/lelab_offline/base/cmd_vel` 供诊断。试验档使用模拟板卡 owner。

```bash
sg docker -c 'LROS_DDS_GAMEPAD=1 tools/ros2_collection/local_ros2/bin/dds_r1 up'
LROS_DDS_GAMEPAD=1 tools/ros2_collection/local_ros2/bin/dds_r1 web
# 在另一个终端运行验证，保持网页服务在前台
sg docker -c 'LROS_DDS_GAMEPAD=1 tools/ros2_collection/local_ros2/bin/dds_r1 verify'
# 用 Ctrl-C 停止上面的 web 后，按 PID 归属停止其余试验进程
sg docker -c 'LROS_DDS_GAMEPAD=1 tools/ros2_collection/local_ros2/bin/dds_r1 down'
```

网页入口为 `http://127.0.0.1:18289/landerpi`。获取控制权并开始新数据集后，
本集从“驶近”开始。底盘只有在该阶段、机械臂跟随有效且本地手柄样本新鲜时才可使能。
网页授权后，摇杆在中位保持 250 ms 即可驾驶，无需按使能键。摇杆回中直接发零速。
300 ms 输入超时、停车进入观察、暂停或急停会发布模拟零目标。输入超时后用网页
“重新授权底盘”或暂停后恢复跟随，重新回中才可驾驶。模拟 owner 的独立 350 ms
无指令超时用于覆盖发布进程退出。
根据实车接收器的轴号，离线档采用左摇杆前后（轴 1）、左右（轴 0）和右摇杆左右
（轴 2），分别映射到车体的 `vx`、`vy`、`wz`。
点击“停车并开始观察”，再点“标记抓取”，最后结束和审核本集。
原有机械臂专用录制从观察开始，仍使用原有导出流程。

原始 SQLite 包把手柄样本、限幅后速度、四轮模拟目标及软件停发状态写入
`/lelab_offline/recording/base`。同步后仅保留原始档案，显式标注
`simulated_offline`、`training_eligible=false` 和缺失的底盘实测状态。
每集还记录以该集起点为原点的 `command_odometry`，按已派发的
`vx/vy/wz` 积分，包括麦轮横移，来源为 `command_integrated_estimate`。
`verify` 使用生产录制与同步接口检查本集三个阶段和车端输出门禁，
结果在 `work/ros2_collection/dds_gamepad/evidence/gamepad-capture.json`。
探针用空按钮数组提交摇杆样本，核对中位启动门槛、三个轴的方向、四轮目标和零目标、
输入超时、重新授权以及原始包的阶段与审核记录。

### Pi 只读探查与回放

使用已登记的 SSH 目标及实际 `/dev/input/by-path/*-joystick` 名称。以下例子使用
已核实的 Pi USB 端口。去掉 `sudo -n` 和 `--usb-reports` 可作为普通用户只读采集 JS。

```bash
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$PI" \
  'sudo -n python3 - --port platform-xhci-hcd.1-usb-0:2:1.0-joystick --seconds 300 --usb-reports' \
  < ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/linux_gamepad.py > pi-gamepad.jsonl
PYTHONPATH=ros2_ws/src/lelab_mobile_agent python3 \
  tools/ros2_collection/local_ros2/offline/replay_gamepad_input.py pi-gamepad.jsonl
```

只读 Reader 要求 by-id、by-path 与打开的字符设备一致，并匹配 USB `2563:0575`。
重插会更新设备代次和初始化状态。记录 JS 变化和 USB 报告，RF 活性字段保持未验证。
这些样本可计算预览速度和轮速，实车命令字段为 null。

在已选的硬件臂配置副本中，加入 `local_gamepad_preview: {"usb_port": "<by-path 名称>"}`，
保持 `teleop.chassis_locked: true`，关闭 `training_export.enabled`，并将
`/lelab/recording/base_gamepad_preview` 加入 `recording.topics` 和
`recording.required_topics`。车端 agent 和控制进程加载同一配置副本，运行身份
为 `hardware`。这项配置启用原始包及 UI 只读状态，底盘输出保持锁定。
常规 SO-101 6D 配置沿用原有录制和导出路径。

原厂单车链路为 `peripherals/joystick_control.py` →
`controller/cmd_vel` → `controller/odom_publisher_node.py` →
`ros_robot_controller/set_motor`。底盘节点给麦轮类传入 0.1368 m 轴距、
0.1446 m 轮距、0.065 m 轮径。厂商麦轮模型的 ROS 电机 ID 依次为 1–4，
SDK 转为板端 0–3。模型图中的轮位
为左前、左后、右前、右后，ROS `rps` 的符号由厂商公式给出，实车轮位与方向仍需
架空确认。厂商 owner 的 `set_motor` 订阅将目标交给 SDK。ROS 发布返回、订阅收到、
串口写入和车轮停转是不同的证据。厂商 `odom_raw` 来自命令积分，联合包缺少可信
实测 `vx/vy/wz` 时保留原始数据，跳过 9D 导出。

实车门禁需要可信无线活性或有明确时限且能重复验证的接收器失效保护，以及独立于
控制进程的停车证据。通过这些门禁、获得明确现场授权后，再做架空低速的轮位、车体
三轴验证，并分别记录触发、零目标发出和车轮停止。

### 实机联合采集 MVP

联合档为 `hardware-hybrid-joint-capture-v1.json`。沿用已有 SO-101 DDS 硬件部署。
车端在启动现有 `live_stack.sh` 时，把 `LELAB_CAR_PROFILE_PATH` 指向已部署候选目录中
的这份配置。上位启动前设置 `LELAB_HYBRID_CAR_PROFILE=hardware_hybrid_joint_capture_v1`
和 `LELAB_HYBRID_TRANSPORT=dds_v1`，沿用原来的连接与标定参数。Pi 上控制进程需能
读取配置 USB 口对应的 JS 和 hidraw 设备。使用同一个板卡 owner，底盘调试页面保持
只读，避免有第二个 `set_motor` 发布者。

在现有 LanderPi 页面建集后开始本集，驶近阶段用摇杆。点击“停车并开始观察”后只
继续机械臂跟随，再标记抓取并结束审核。必录话题 `/lelab/recording/base_gamepad`
保存原始轴、限幅后速度、四轮目标、阶段和标记为 `command_integrated_estimate`
的估算轨迹。相机、臂状态、同步和审核沿用原流程。联合档在 `raw_synced` 结束。
USB 收包时间只描述 USB 传输，四轮目标只描述软件派发。

### 上位机直接保存原始包

实机联合档与 DDS 跟随档的 `recording.storage_location` 为 `upper_computer`。
上位机使用现有 SSH 连接，把本地 `upper/sessions` 挂到 MentorPi 容器内的
`/tmp/lelab-upper-storage/sessions`。SQLite、事件、阶段和清单直接写到上位机磁盘。
存储挂载不可用时录制入口拒绝开始。在上位机落下 `READY` 后，复用已有校验流程。

启动录制前，在上位机新开终端保持挂载进程运行。`--sessions` 传入运行中上位服务
的 `LELAB_ROS_COLLECTION_ROOT` 下的 `upper/sessions`。

```bash
cd /home/liaozhengyan/Desktop/lelab
python3 tools/ros2_collection/upper_recording_mount.py \
  --pi "pi@<Pi地址>" --sessions "$LELAB_ROS_COLLECTION_ROOT/upper/sessions"
```

上位机需有 Bubblewrap 和 OpenSSH `sftp-server`，MentorPi 容器需有 `sshfs`、
`/dev/fuse` 和现有挂载权限。脚本使用 SSHFS 的反向挂载模式，目录服务走 SSH 标准
输入输出。见 [SSHFS 反向挂载说明](https://github.com/libfuse/sshfs/blob/master/sshfs.rst)。
结束数据集后，在网页确认原始包已校验，再停止挂载进程或关闭 Pi。
