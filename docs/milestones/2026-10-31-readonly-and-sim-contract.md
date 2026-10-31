# 只读车端采集与仿真数据契约收尾

日期：2026-10-31。基线：`codex/ros2-mobile-collection`。本轮目标是只读相机预览、带 episode 的车端原始录制、上位机校验同步，以及本地数据真实性和异常处理修复。

**结论：本地仿真与只读适配器验收通过。树莓派曾在 2026-10-31T15:54Z 被动探测到真实相机/IMU/odom/joint topic，但当日 17:02Z 再次不可达；没有完成 Pi 部署、Pi 本地 bag、网页消费真实相机或 Pi 断连测试。不能把本文的本地结果当作实机采集证据。**

脱敏证据：[ros2-readonly-sim-contract-20261031.json](../evidence/ros2-readonly-sim-contract-20261031.json)、异常录包：[ros2-readonly-abnormal-recorder-20261031.json](../evidence/ros2-readonly-abnormal-recorder-20261031.json)。

## 1. 已完成并验证

| 项目 | 实现 | 本地证据 |
| --- | --- | --- |
| sim / hardware adapter 分离 | `runtime.mode=hardware_readonly` 时只创建事件、传感器、录制和 HTTP 入口；不创建底盘/手臂/夹爪 publisher 和控制定时器 | `/lelab_readonly_test` 发布者仅 `/lelab/events`、`/parameter_events`、`/rosout` |
| 只读拒绝 | `/control/*`、`/expert/*`、`/task/reset` 返回 403；启停 session 不需要控制 token | acquire 返回 `read_only:true`；base 403；expert 403 |
| 只读录制 | 使用硬件只读 profile，记录真实传感器 topic 与事件；episode 允许 `success:unknown`、`unlabeled` | `ros_20261031T170156Z_1a91b4ad`，2 episodes，raw_ready |
| 上位机断连本地收尾 | 车端继续录制，上位机 backend 被杀后直接结束 episode/session | `ros_20261031T170238Z_95697d4f`，`stop_reason=upper-disconnected-local-stop`，README 哈希一致 |
| 异常录制不 READY | bag 提前退出/被 kill 时置 error，不写 READY；必需流中断、磁盘低同样不写 READY | `ros_20261031T155753Z_ee7808e9`，exit=-9；`ros_20261031T171045Z_970e8861`，`required_stream_missing`，无 READY；磁盘低直接测试 `disk_low` |
| 启动/清理竞态 | MCAP 启动时文件可为空，改为 bag 文件存在 + 进程存活；启动失败关闭 bag/日志并清理 session 句柄，保留部分 raw | 后续会话正常 raw_ready；失败启动不阻塞下一次 session |
| 同步完整性 | 暂存 → 解压（拒绝链接）→ size/SHA-256 → READY 哈希 → 原子发布 → 再校验 | 只读 session 7 个文件全部校验；bag `ros2 bag info` 可读 |
| 训练导出门禁 | 非 simulation、缺 action/关节/base、尺寸不符、stale 超限均拒绝 | 只读 session 返回 HTTP 409；旧失败 session 的缺 action 也被拒绝 |
| 导出任务隔离/取消 | `/export` 启动独立进程；`/export/status/{id}`、`/export/cancel/{id}` | 两个仿真数据集导出成功；进程不占用 API 事件循环 |
| 底盘 state 来源 | Gazebo `OdometryPublisher` → `/lelab/base/model_odom`；轮式 odom 仅诊断 | 前进 0.05 m/s 约 2.4 s：model x≈0.0713 m，wheel x≈0.0902 m，state source=`model_odometry` |
| 双腕部相机 | xacro/launch 选择 `wrist_original`（0.0 rad）或 `wrist_demo_downward`（0.55 rad），profile/外参写入 session/导出 | tray 导出 200 帧/俯视；box 导出 200 帧/原始 0.0 rad |
| 参数化箱顶放置 | `place_on_box.sdf`（箱顶 0.06 m，世界名保持 `pick_place`）；目标确认阶段；支撑、释放稳定、夹爪撤离判定 | `ros_20261031T165901Z_fa5a85da` success=true，box_top_support=true，retreated=true |
| 抬升阈值统一 | 两种任务均读取 `task.success_min_lift_m = 0.03`；专家抬升约 0.08 m | tray/box 评估 `lift_threshold_m=0.03` |
| 仿真数据读回 | 本地 LeRobot SDK 读取 200 帧、9 维 state/action、640×480 RGB；网页 `/datasets` 列出新数据集 | repo `lelab/ros-mobile-20261031t164152z-4a59021d` |
| Pi 轻量部署路径 | `deploy_readonly_agent.sh` 复用既有容器，支持 check/apply/stop/rollback，不改系统环境；预览支持 Pillow 或 OpenCV | 脚本 `bash -n` 与依赖检查逻辑已验证；未在 Pi 执行（不可达） |

## 2. 数据与来源

- 只读双 episode：`work/ros2_collection/readonly_test_car/sessions/ros_20261031T170156Z_1a91b4ad`（7 文件，bag 40.5 MiB，READY）
- 只读断连收尾：`ros_20261031T170238Z_95697d4f`（6 文件，bag 81.0 MiB，stop_reason 记录断连原因）
- 仿真 tray：`ros_20261031T164152Z_4a59021d` → 导出 `lelab/ros-mobile-20261031t164152z-4a59021d`（200 帧，`wrist_demo_downward`）
- 仿真 box：`ros_20261031T165901Z_fa5a85da` → 导出 `lelab/ros-mobile-20261031t165901z-fa5a85da`（200 帧，`wrist_original`）

只读 clock domain 为 `local_ros_clock`，事件保存 `time_ns` 与 `wall_time`；仿真为 `ros_sim_time`，事件同时保存 sim 时间和 bag 接收时间。动作来自网关已发送目标（零阶保持，最长 3 s），state 来自测量关节和模型 odom，没有补零或合成 action。

## 3. 未完成与阻塞

- **物理链路**：17:11Z 检查宿主有线网卡 `enx00e04c68034c` 为 `NO-CARRIER`（ethtool `Link detected: no`），宿主当前仅在 Wi-Fi `192.168.31.54/24`；`192.168.19.0/24` 已无路由。这不是 Pi ROS 结论，需恢复网线/供电或确认新地址。
- **Pi 状态**：15:54Z 的被动探测已确认 Humble 容器（MentorPi，Ubuntu 22.04/Python 3.10）及真实 RGB（640×400 bgr8，约 8.6 Hz）、深度、IMU、关节和 odom topic；但 17:02Z 复核 `pi@192.168.19.138` 时 SSH BatchMode `Connection timed out`，ping 2/2 丢包。没有采集或输出任何凭据。
- 被动探测未发现 MCAP 插件，硬件只读 profile 因此改用 `sqlite3`；相机外参、关节/夹爪实测反馈和时钟同步仍未确认。
- 因此以下均未验证：Pi 上的 ROS 运行时/驱动/容器启动与部署、真实相机外参/标定、网页消费真实相机、Pi 本地两段 episode、Pi 断连保存、真实 bag 同步、实机只读无执行器输出。真实 topic/帧尺寸/近似接收频率只有被动探测结果，不能替代本次运行验证。
- 未进行真实运动、固件烧录、系统重装或策略训练。
- 原始相机未压缩，640×480/约 14 fps 约 0.8 GB/min；Pi 8.7 GiB 余量下只能短片段，必须依赖水位门禁和独立数据介质。
- 导出仍默认 `HF_LEROBOT_HOME` 下的路径；网页数据集查看器按当前 app 配置读取。

## 4. 下一阶段最小清单

1. 网络/认证恢复后，重新核验 Pi 的 ROS 发行版、容器、驱动、设备 by-id、真实 topic/QoS/时间戳、MCAP 插件与可用空间；不因宿主无 ROS 就重装。
2. 在 Pi 复用现有环境部署 `lelab_mobile_agent` + `readonly.launch.py`，只启动传感器与录制；保持回环绑定和 SSH 隧道。
3. 用真实相机跑两段短 episode；断开上位机 backend 后本地结束，再同步并核对 SHA-256/READY。
4. 板端 watchdog、统一租约/TTL、真实反馈和低速运动授权单独立项；不得用本轮仿真结果替代。

### 真实反馈 / 板端 watchdog / 统一控制联调

- **真实反馈**：确认串口 SDK 的编码器、IMU、舵机回读链路；把 `requested command`、`sent command`、`acknowledged/applied`、`measured state` 分开记录；缺字段时保持 `unknown`，不补零。
- **板端 watchdog**：在 Pi/控制板侧实现独立超时与停车，不依赖上位机、Gazebo 或 `gz topic`；验证断网、agent 被杀、控制租约过期、上电/复位后的状态。
- **统一控制**：键盘底盘、五轴主臂、夹爪和策略推理进入同一租约/急停/TTL 状态机；切换控制端时先停旧端、清空过期动作、重新取权；低速小幅真机验证前另行确认设备归属、标定和急停。
