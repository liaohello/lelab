# 下一阶段 Agent Goal prompt

以下内容可作为下一次开发 Goal 的完整任务输入；本轮只交付提示词，没有自动创建或启动 Goal。

---

你在 `/home/liaozhengyan/Desktop/lelab` 开发。目标是在保留现有仿真闭环的基础上，完成“实机只读预览、车端 episode 原始录制、上位机校验同步”的第一阶段移植，同时修复会污染数据或错误发布 READY 的问题。完整实机运动采集、VLA 训练/推理和固件烧录不属于本阶段。

先读根 AGENTS.md、README、MIGRATION、部署说明，以及 `docs/milestones/2026-10-31-raspberry-pi-environment-assessment.md`、`docs/ros2-vla-base-interface-decision.md` 和仿真运行手册。核对仓库状态、子目录规则和既有服务，不覆盖用户修改。基线为 `codex/ros2-mobile-collection` / `4beecee`；开发代码在 `ros2_ws/`、`app_ros_mobile/`、`configs/ros2_collection/`。评估收尾时已出现额外 agent/recorder、hardware-readonly profile 和 launch 等未提交开发变更，本轮未验收；先审阅并复用当前实现，再补缺口，不按旧清单覆盖它们。保留发布快照和 current 指向，保留本地定制 LeRobot 依赖。

实机为 `pi@192.168.19.138`，已测 Pi 5 / Debian 12 / aarch64 / 宿主Python3.11.2，根盘约剩8.7GiB。用户重启后 SSH/Docker 恢复，已确认运行容器 `MentorPi`（ros:humble，Ubuntu22.04，Python3.10.12，host网络）。工作区是 `/home/ubuntu/ros2_ws` 和 `/home/ubuntu/third_party_ros2/third_party_ws`。以驱动同一UID1000、ROS_DOMAIN_ID=0、ROS_LOCALHOST_ONLY=0被动订阅，已收到Aurora RGB/depth/IR/camera_info、IMU、joint和odom消息；默认UID0探测曾无数据，原因尚未完全隔离。RGB实际为640×400/bgr8，depth为640×400/mono16。雷达两次短窗没有scan消息；MCAP插件未发现；Pi日期为2026-04-08，而本机为2026-10-31，NTP未同步。上述为本次评估快照，开工复核；详细证据见评估报告第6节及 `docs/evidence/raspberry-pi-recheck-20261031.json`。使用已有授权认证方式，不把密码、令牌或私密配置写入命令、脚本、报告或 Git。

按以下顺序执行，并持续报告可验证结果：

1. 完成 P0 剩余缺口。复核连接、存储与上述既有 Humble 容器，优先解决未同步时钟和缺失 MCAP 插件，不重新安装整套ROS。时间变更安排在没有录制和实际控制的维护窗口，确认影响范围并验证同步结果，不能只改时区；不得给新采集继续套用旧时间。安装插件前评估容量并保留回退，不做全局升级。核对 UID1000、ROS环境、设备身份、消息类型/QoS/时间戳、相机光学变换/深度单位与bag可读性。ROS图查询避免自动启动daemon；继续只读定位MS200无消息问题，可选雷达不可用要显式标记。宿主没有ROS不代表容器没有；Aurora已通过SDK/ROS出图，不要求普通V4L2 camera节点。连接不可用时记录具体错误，继续独立本地开发，不用mock冒充实机证据。

2. 实现显式 sim/hardware adapter 和独立 hardware-readonly profile。仿真 Contacts、真值、/clock、gz CLI 仅在仿真路径加载。实机只读模式不创建/发送底盘、手臂、夹爪执行器 publisher/client；控制、reset、expert、回放执行入口必须被拒绝，启停和异常退出也不得发送零速、保持或使能力矩。健康检查只依赖该 profile 明确要求的真实传感器；配置 topic、设备身份、关节名、单位、状态性质、存储与时钟，不假定 Gazebo 映射适用。未知字段明确缺失，启动须有预检结果。

3. 复用已有 Humble/Python3.10/UID1000 环境，在独立源码/overlay与持久数据目录部署最小车端组件，不在 Pi 安装完整 LeLab/torch/LeRobot/Gazebo，不覆盖系统 Python或既有驱动。现有ROS安装映射指向 `/home/ubuntu/ros2_ws/src/driver`，存在hx2快照副本；不要改错副本，也不依赖容器可写层作为唯一源码/数据副本。安装前估算容量；禁止直接重装系统、全局升级或清理未知数据。只允许启动经检查无执行器副作用的采集组件，不重启已自启动且包含init_pose/摇杆/控制器的整车launch。新服务回环绑定，上位机经SSH隧道连接；预览使用有界队列，和raw录制相互独立。

4. 修复 recorder。开始录制必须确认 bag 就绪；记录 start/end/discard/interrupted 事件，允许 success=unknown/unlabeled。只有正常结束、退出状态符合该 rosbag 版本预期且文件完整可读的 session 才封存 READY；提前退出、强制 kill、磁盘不足、必需流缺失必须标记 incomplete/error，保留部分 raw 和日志。增加容量水位门禁、采集频率/丢帧统计，减少1kHz joint_states 录制膨胀。传输沿用暂存、size/SHA-256、READY 与原子发布；断线重试不得覆盖原始主副本。

5. 修复时间与数据真实性。保存消息源 stamp、接收时间、时钟域、回退标记、观测年龄、配置/校准/软件版本。实机不用仿真时钟；时钟回跳应结束或分段；租约/TTL 使用单调时钟。区分请求、网关发送目标、真实回执与传感器反馈；命令积分 odom、目标缓存不能作为实测 state。缺关节或 action 不补零、不复制 state 生成 action。只读实机 session 保留 raw，缺少训练必需字段时上位机明确拒绝九维训练导出，不捏造已完成的动作采集。

6. 在本机修复对应仿真回归：平面 `(vx,vy,wz)` 执行的 state 必须来自实际模型运动，轮式 odom 独立诊断；实现 `wrist_original`/`wrist_demo_downward` 两个相机配置，将外参和 profile 写入 session/导出来源。增加参数化 place_on_box 和目标位置确认阶段，先验证臂展/碰撞；高壁托盘使用独立任务/判定。箱顶成功需真实支撑、释放稳定、夹爪撤离；统一 profile 抬升阈值与代码。专家标记 scripted_expert，真值用途显式记录。保留旧数据来源，不静默重写成功标签。大导出移出 API 事件循环并可取消。

验收：本地相关检查与实际页面交互通过；只读模式拒绝运动并确认无执行器输出；实机真实相机在网页可见并显示过期状态；至少两段短 episode 在 Pi 正常录制，其中一段经受上位机连接中断后仍在本地保存；安全故障注入只针对本任务代理/隧道，不关闭整车网络或停止他人进程。验证正常停止、传感器缺口/录包异常不发布 READY、校验同步、可读 bag、时间和配置来源；模拟或损坏副本的故障测试与实机结果分开标注。仿真完整数据仍须由本地 LeRobot SDK 和网页读回，缺 action/反馈的样本须被拒绝训练导出。实机训练数据、实际控制、Wi-Fi 性能未测时明确列未完成，不因页面200或设备枚举判定通过。

主智能体负责修改、部署与验证；最多三个 default 只读子智能体核查环境/接口、数据契约、仿真来源，不让子智能体写文件、运行测试或触碰硬件。交付源码差异、环境版本与导入路径、运行/停止/回退手册、脱敏证据、剩余阻碍，以及下一阶段真实反馈/板端watchdog/统一控制联调清单。真实运动前另行核实设备归属、标定、急停与运动授权。
