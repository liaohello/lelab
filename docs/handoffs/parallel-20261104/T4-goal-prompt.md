# LanderPi 阶段录制与数据导出开发

下面内容可作为独立开发会话的用户 Goal Prompt。

```text
在 /home/liaozhengyan/Desktop/lelab 执行 LanderPi 联合遥操作并行开发。这是用户明确要求的多会话开发工作，你是本会话的实施主智能体，可以修改自己负责的源码；辅助子智能体仍只允许 default 只读，不操作硬件。

先读 AGENTS.md、docs/handoffs/2026-11-04-landerpi-parallel-development-plan.md、docs/landerpi-hybrid-teleop-system-design-2026-11-04.md，以及相关现有模块/测试。目标是：Pi手柄移动底盘、网页视频、上位机SO101主臂操控小车五轴从臂和r_joint夹爪，同一episode记录observe/grasp阶段，车端sqlite3原始数据、上位机校验和LeRobot导出；本轮不训练、不自主推理、不解除机械限位。

你不是唯一在修改此代码库的会话。使用当前工作区和现有分支，保留所有已有未提交/未跟踪修改，不覆盖他人工作。只改下述归属文件。禁止git add/commit/checkout/reset/rebase/push；禁止跨写公共文档、根.gitignore、部署脚本与总runbook。接口契约以并行计划第3节为准；接口变更写自己的status文件供总协调处理，不自行修改公共字段。

首轮只做无硬件开发：不SSH、不打开串口/相机/手柄、不启停服务/容器、不烧录、不操作真实设备；不读取或传播凭据。参考ROS树 /home/liaozhengyan/Desktop/resource/lander_pi 与固件树 /home/liaozhengyan/Desktop/resource/RosRobotControllerLite_ros_250811 只读。车端Python3.10/Humble，上位机Python3.12+；本地定制LeRobot保留，不替换PyPI。默认无输出、旧只读profile/session不变、invalid session不删除。

主动完成所有不依赖硬件的实现和针对性测试，不停在计划或能力说明。依赖其他会话时先用明确fixture实现自己的模块和契约测试，不在生产代码加默认正常/虚假反馈分支。新协议及安全行为覆盖过期、错帧/来源、旧会话、缺数据和取消路径；测试和模拟结果不能冒充实机验收。不要同时跑全量测试和多个前端构建争资源。报告public接口、准确测试结果、文件范围、证据与硬件缺口。

你负责：ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/recorder.py；app_ros_mobile/lelab/ros_mobile.py、ros_export_worker.py、dataset_viewer.py；必要phase/schema新模块；独立T4测试。不要改agent.py/contract.py/协调器/反馈模块/前端。T3提供hybrid_session facade，你负责在ros_mobile.py接入计划第3节hybrid API路由；facade暂未到位时先用明确测试fixture推进phase和导出，不在生产环境虚构就绪。

实现同一episode中的observe/grasp（可drive/transport/release）人工阶段事件，切换阶段无运动副作用、不自动另开episode；会话录制作为同一控制租约的子任务。end_episode/stop_session先调用控制暂停再收尾；急停不等待保存。阶段边界写入车端时间轴，支持视频/曲线按episode和phase定位及显式切片来源。结果标签manual success/failure/unknown。

保留sqlite3、manifest/READY、staging+SHA256原子同步、重复同步校验/导出写保护与invalid保留。旧只读profile/session继续拒绝训练导出。保留六维arm_gripper_6dof_phase1；新增mobile_hybrid_manual_9d_v1显式硬件schema，3底盘速度命令+5臂+1夹爪action，state为已验证底盘估计+六路实测。command_cache/命令积分/缺失或过期反馈不能补0通过。缺ACK明确unavailable，driver_sent不是executed。导出保留age/source/clock/phase/拒绝与切片报告，不跨episode/clock epoch插值，不从未来state构造当前观测。

用合成sqlite3包通过生产sync/export及本地LeRobot SDK/数据页函数读回，验证两短集含observe/grasp、阶段和共享mp4区间对应、6/9维兼容/缺失拒绝/旧只读拒绝；不要为测试访问车或下载大模型。记录本地SDK导入路径。

首个可集成交付：事件与导出契约、路由fixture、阶段读回/拒绝用例与结果；写 docs/handoffs/parallel-20261104/T4-status.md，证据仅放 docs/evidence/parallel-20261104/T4/。

现在执行Wave A，完成你的独立开发和验证后写status并向本会话用户交付；不要只给计划。无需等待小车或其他会话才能开始。
```
