# LanderPi MCU 反馈与失联停车开发

下面内容可作为独立开发会话的用户 Goal Prompt。

```text
在 /home/liaozhengyan/Desktop/lelab 执行 LanderPi 联合遥操作并行开发。这是用户明确要求的多会话开发工作，你是本会话的实施主智能体，可以修改自己负责的源码；辅助子智能体仍只允许 default 只读，不操作硬件。

先读 AGENTS.md、docs/handoffs/2026-11-04-landerpi-parallel-development-plan.md、docs/landerpi-hybrid-teleop-system-design-2026-11-04.md，以及相关现有模块/测试。目标是：Pi手柄移动底盘、网页视频、上位机SO101主臂操控小车五轴从臂和r_joint夹爪，同一episode记录observe/grasp阶段，车端sqlite3原始数据、上位机校验和LeRobot导出；本轮不训练、不自主推理、不解除机械限位。

你不是唯一在修改此代码库的会话。使用当前工作区和现有分支，保留所有已有未提交/未跟踪修改，不覆盖他人工作。只改下述归属文件。禁止git add/commit/checkout/reset/rebase/push；禁止跨写公共文档、根.gitignore、部署脚本与总runbook。接口契约以并行计划第3节为准；接口变更写自己的status文件供总协调处理，不自行修改公共字段。

首轮只做无硬件开发：不SSH、不打开串口/相机/手柄、不启停服务/容器、不烧录、不操作真实设备；不读取或传播凭据。参考ROS树 /home/liaozhengyan/Desktop/resource/lander_pi 与固件树 /home/liaozhengyan/Desktop/resource/RosRobotControllerLite_ros_250811 只读。车端Python3.10/Humble，上位机Python3.12+；本地定制LeRobot保留，不替换PyPI。默认无输出、旧只读profile/session不变、invalid session不删除。

主动完成所有不依赖硬件的实现和针对性测试，不停在计划或能力说明。依赖其他会话时先用明确fixture实现自己的模块和契约测试，不在生产代码加默认正常/虚假反馈分支。新协议及安全行为覆盖过期、错帧/来源、旧会话、缺数据和取消路径；测试和模拟结果不能冒充实机验收。不要同时跑全量测试和多个前端构建争资源。报告public接口、准确测试结果、文件范围、证据与硬件缺口。

你负责：tools/ros2_collection/vendor_patches/firmware/、本目录协议说明/清单、独立命名T2协议测试，以及 work/ros2_collection/firmware-development/ 内独立工作副本。不要修改参考固件树、ROS/上位机/前端或其他会话文件。当前固件树大量dirty，基线以精确源码hash与可再应用补丁为证，不能只写HEAD。

实现最小固件增量：能力/构建ID/boot标识协议、四轮累计编码器计数/真实RPS/目标RPS/采样tick/故障的周期快照与报文；有效命令超时置零和重新使能/锁存语义；非法motor id、数量、报文长度、NaN/Inf、TTL和乱序/旧epoch拒绝。保留现有轮电机PID和ROS侧运动学；不要同时重写底盘控制。串口协议增量做版本/能力协商，不能宣称原固件已支持新字段。IWDG不等于通信超时。中断内不阻塞串口，处理计数溢出和快照一致性。

先给T1可消费的报文规范和golden vectors（包含旧版不支持、新boot、错帧和计数溢出）。尽可能在本机使用实际修改后的C逻辑做host harness，区别模型测试与真实固件构建；核验现有工具链后进行可复现构建。不安装/替换全局工具链，不把已有hex当本次编译产物。工具链或私有依赖缺失时完成独立代码/补丁/协议测试并精确记录构建缺口。禁止烧录或连接板卡。实际停车/复位/负载行为留实机验收。

首个可集成交付：版本协议、golden vectors、可重复应用的补丁、host检查与构建状态、回退说明；写 docs/handoffs/parallel-20261104/T2-status.md，证据仅放 docs/evidence/parallel-20261104/T2/。

现在执行Wave A，完成你的独立开发和验证后写status并向本会话用户交付；不要只给计划。无需等待小车或其他会话才能开始。
```
