# LanderPi 联合会话与标定开发

下面内容可作为独立开发会话的用户 Goal Prompt。

```text
在 /home/liaozhengyan/Desktop/lelab 执行 LanderPi 联合遥操作并行开发。这是用户明确要求的多会话开发工作，你是本会话的实施主智能体，可以修改自己负责的源码；辅助子智能体仍只允许 default 只读，不操作硬件。

先读 AGENTS.md、docs/handoffs/2026-11-04-landerpi-parallel-development-plan.md、docs/landerpi-hybrid-teleop-system-design-2026-11-04.md，以及相关现有模块/测试。目标是：Pi手柄移动底盘、网页视频、上位机SO101主臂操控小车五轴从臂和r_joint夹爪，同一episode记录observe/grasp阶段，车端sqlite3原始数据、上位机校验和LeRobot导出；本轮不训练、不自主推理、不解除机械限位。

你不是唯一在修改此代码库的会话。使用当前工作区和现有分支，保留所有已有未提交/未跟踪修改，不覆盖他人工作。只改下述归属文件。禁止git add/commit/checkout/reset/rebase/push；禁止跨写公共文档、根.gitignore、部署脚本与总runbook。接口契约以并行计划第3节为准；接口变更写自己的status文件供总协调处理，不自行修改公共字段。

首轮只做无硬件开发：不SSH、不打开串口/相机/手柄、不启停服务/容器、不烧录、不操作真实设备；不读取或传播凭据。参考ROS树 /home/liaozhengyan/Desktop/resource/lander_pi 与固件树 /home/liaozhengyan/Desktop/resource/RosRobotControllerLite_ros_250811 只读。车端Python3.10/Humble，上位机Python3.12+；本地定制LeRobot保留，不替换PyPI。默认无输出、旧只读profile/session不变、invalid session不删除。

主动完成所有不依赖硬件的实现和针对性测试，不停在计划或能力说明。依赖其他会话时先用明确fixture实现自己的模块和契约测试，不在生产代码加默认正常/虚假反馈分支。新协议及安全行为覆盖过期、错帧/来源、旧会话、缺数据和取消路径；测试和模拟结果不能冒充实机验收。不要同时跑全量测试和多个前端构建争资源。报告public接口、准确测试结果、文件范围、证据与硬件缺口。

你负责：app_ros_mobile/lelab/mobile_teleop.py、mobile_teleop_coordinator.py、mobile_teleop_session.py、mobile_leader_reader.py；新增 hybrid_session.py/public facade 与必要纯逻辑标定/手柄模块；ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/agent.py、contract.py；新hardware_hybrid_manual_v1 profile及复制到包内配置；独立T3测试。不要改ros_mobile.py/recorder.py/follower_feedback.py/前端/部署脚本/原只读与锁底盘profile。ros_mobile.py的路由胶水由T4负责，你先给可调用facade与fixture。

实施一个会话协调双层租约、base/arm/gripper通道许可。保留锁底盘阶段和相对对齐/离合，新增gamepad输入意图及manual hybrid模式，禁止靠全局keyboard/leader_arm来回切换实现并发。按session_epoch/channel/source维护seq，保留deadline并清除旧队列；硬件mode只接受T1可信反馈来源，不默认相信原厂joint_states。driver适配边界先用T1接口fixture完成，随后接公共接口。状态/异常/租约获取失败必须回滚；停止先于网络/录包收尾；新会话/重连/标定变更不自动跟随。机械臂+夹爪原有follow路径可保留兼容，不让新功能绕过同一硬件租约。

扩展版本化标定资产：base/follower/leader/mapping/gamepad/camera与rig bundle。复用现有三层存储/USB绑定/LeRobot主臂；车从臂不伪装SO101Follower。示例值、角色/版本失配、缺关节/非有限/超龄均拒绝；相机/固件能力未知显式报告。手柄中位、deadman新按下、周期输入和掉线纯逻辑；首轮不真正打开输入设备。连续旋转硬件不纳入。

先冻结 public facade：status(), acquire_session(request), release_session(reason), set_arm_state(event), ingest_gamepad(sample), preview()及标定能力/readiness；与计划第3节API一致。提供T4可导入、上位机无rclpy依赖的接口。默认抓取阶段锁底盘，不把软件并发能力当稳定性验收。

首个可集成交付：facade、状态fixture、标定schema/示例、租约/序号/TTL/无输出/故障/两输入并发单测；写 docs/handoffs/parallel-20261104/T3-status.md，证据仅放 docs/evidence/parallel-20261104/T3/。

现在执行Wave A，完成你的独立开发和验证后写status并向本会话用户交付；不要只给计划。无需等待小车或其他会话才能开始。
```
