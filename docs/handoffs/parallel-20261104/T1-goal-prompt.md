# LanderPi 驱动与真实反馈开发

下面内容可作为独立开发会话的用户 Goal Prompt。

```text
在 /home/liaozhengyan/Desktop/lelab 执行 LanderPi 联合遥操作并行开发。这是用户明确要求的多会话开发工作，你是本会话的实施主智能体，可以修改自己负责的源码；辅助子智能体仍只允许 default 只读，不操作硬件。

先读 AGENTS.md、docs/handoffs/2026-11-04-landerpi-parallel-development-plan.md、docs/landerpi-hybrid-teleop-system-design-2026-11-04.md，以及相关现有模块/测试。目标是：Pi手柄移动底盘、网页视频、上位机SO101主臂操控小车五轴从臂和r_joint夹爪，同一episode记录observe/grasp阶段，车端sqlite3原始数据、上位机校验和LeRobot导出；本轮不训练、不自主推理、不解除机械限位。

你不是唯一在修改此代码库的会话。使用当前工作区和现有分支，保留所有已有未提交/未跟踪修改，不覆盖他人工作。只改下述归属文件。禁止git add/commit/checkout/reset/rebase/push；禁止跨写公共文档、根.gitignore、部署脚本与总runbook。接口契约以并行计划第3节为准；接口变更写自己的status文件供总协调处理，不自行修改公共字段。

首轮只做无硬件开发：不SSH、不打开串口/相机/手柄、不启停服务/容器、不烧录、不操作真实设备；不读取或传播凭据。参考ROS树 /home/liaozhengyan/Desktop/resource/lander_pi 与固件树 /home/liaozhengyan/Desktop/resource/RosRobotControllerLite_ros_250811 只读。车端Python3.10/Humble，上位机Python3.12+；本地定制LeRobot保留，不替换PyPI。默认无输出、旧只读profile/session不变、invalid session不删除。

主动完成所有不依赖硬件的实现和针对性测试，不停在计划或能力说明。依赖其他会话时先用明确fixture实现自己的模块和契约测试，不在生产代码加默认正常/虚假反馈分支。新协议及安全行为覆盖过期、错帧/来源、旧会话、缺数据和取消路径；测试和模拟结果不能冒充实机验收。不要同时跑全量测试和多个前端构建争资源。报告public接口、准确测试结果、文件范围、证据与硬件缺口。

你负责：ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/follower_feedback.py，以及该目录新增 hardware_io.py、hardware_adapter.py、hardware_safety.py（可最小化合并）；tools/ros2_collection/vendor_patches/ros/；独立命名的T1测试。不要修改 agent.py/contract.py/recorder.py/上位机/前端。实际接入agent由T3负责。

交付可注入transport、默认无输出的硬件层。公共接口先提供 capabilities(), read_snapshot(), submit(command), stop(reason)，并在status说明精确签名。实现有界串口事务/回包长度、ID、子命令、错误检查、串行化和迟到帧处理；旧协议无transaction id，不得用generation伪造匹配。修复思路通过vendor ROS patch整合到唯一串口所有者，不能再开第二串口。反馈必须区分servo_position_readback/command_cache/model_placeholder/synthetic；finite、age、所有预期关节、扫描跨度和标定版本均验证。真实r_joint单执行器，禁止五个mimic下发。原始越界读数保留并标异常，不能先夹成正常值。

硬件安全监督独立于HTTP进程，保留deadline/epoch/seq到最终输出；丢队列/过期/缺反馈时不能补0保持。实现真实底盘话题与单位适配边界，纯逻辑先测试；stop/hold硬件效果标待验证，不把函数调用成功当停车。输出ROS补丁前核验来源sha，并提供可应用/回退与错基线拒绝验证；参考源码本身不动。六路回读吞吐需留配置，不能把50Hz缓存发布频率当回读能力。

首个可集成交付：接口、纯Python协议/反馈/真夹爪单测、vendor patch和模拟串口故障测试；写 docs/handoffs/parallel-20261104/T1-status.md，证据仅放 docs/evidence/parallel-20261104/T1/。

现在执行Wave A，完成你的独立开发和验证后写status并向本会话用户交付；不要只给计划。无需等待小车或其他会话才能开始。
```
