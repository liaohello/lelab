# LanderPi 联合遥操作网页开发

下面内容可作为独立开发会话的用户 Goal Prompt。

```text
在 /home/liaozhengyan/Desktop/lelab 执行 LanderPi 联合遥操作并行开发。这是用户明确要求的多会话开发工作，你是本会话的实施主智能体，可以修改自己负责的源码；辅助子智能体仍只允许 default 只读，不操作硬件。

先读 AGENTS.md、docs/handoffs/2026-11-04-landerpi-parallel-development-plan.md、docs/landerpi-hybrid-teleop-system-design-2026-11-04.md，以及相关现有模块/测试。目标是：Pi手柄移动底盘、网页视频、上位机SO101主臂操控小车五轴从臂和r_joint夹爪，同一episode记录observe/grasp阶段，车端sqlite3原始数据、上位机校验和LeRobot导出；本轮不训练、不自主推理、不解除机械限位。

你不是唯一在修改此代码库的会话。使用当前工作区和现有分支，保留所有已有未提交/未跟踪修改，不覆盖他人工作。只改下述归属文件。禁止git add/commit/checkout/reset/rebase/push；禁止跨写公共文档、根.gitignore、部署脚本与总runbook。接口契约以并行计划第3节为准；接口变更写自己的status文件供总协调处理，不自行修改公共字段。

首轮只做无硬件开发：不SSH、不打开串口/相机/手柄、不启停服务/容器、不烧录、不操作真实设备；不读取或传播凭据。参考ROS树 /home/liaozhengyan/Desktop/resource/lander_pi 与固件树 /home/liaozhengyan/Desktop/resource/RosRobotControllerLite_ros_250811 只读。车端Python3.10/Humble，上位机Python3.12+；本地定制LeRobot保留，不替换PyPI。默认无输出、旧只读profile/session不变、invalid session不删除。

主动完成所有不依赖硬件的实现和针对性测试，不停在计划或能力说明。依赖其他会话时先用明确fixture实现自己的模块和契约测试，不在生产代码加默认正常/虚假反馈分支。新协议及安全行为覆盖过期、错帧/来源、旧会话、缺数据和取消路径；测试和模拟结果不能冒充实机验收。不要同时跑全量测试和多个前端构建争资源。报告public接口、准确测试结果、文件范围、证据与硬件缺口。

你负责：app_ros_mobile/frontend/src/全部前端源码，以及必要前端fixture/检查和本地frontend/dist构建。不要改Python、ROS、固件、根.gitignore/部署脚本。前端src/lib存在被宽泛lib/忽略的历史问题；优先用现有模块，需新文件时在status精确列出被忽略路径，由总协调纳入版本管理，不自行改根规则。

按并行计划第3节固定hybrid API开发现有/ros-mobile页面：设备/能力/版本、底盘/从臂/主臂/映射/手柄/相机校准资产、无输出预览、base驾驶使能与锁定、arm离合/对齐、急停/恢复、反馈来源与age、视频过期、同一episode中observe/grasp人工阶段、录制/暂停/结束、同步导出与阶段视频/曲线检查。保留旧实机只读入口；hardware能力未知时fail closed，隐藏或禁用该profile不允许的旧键盘/专家/直接执行器入口。任何窗口都不能在浏览器承担串口/安全实时循环。

T3/T4接口尚未落地时创建独立模拟HTTP服务/fixture，显式标记模拟，仅用于测试。不要在生产API加默认健康分支。必须实际点击主要按钮及失败/取消/恢复路径，检查错误不被轮询清掉，phase变化无运动调用、结束先停、旧profile只读不放开。验证断网/旧状态降级和视频source frame_seq/年龄展示，HTTP200或缓存JPEG不能叫新画面。

核验本机Node路径，使用现有node_modules，执行tsc --noEmit和生产build。仅用空闲回环端口运行本会话测试服务并记录PID；不碰8000/18186及他人服务。模拟网页验收单独标明，不能写成真车闭环。

首个可集成交付：API映射说明、主流程与错误实际点击记录、截图/构建结果及待后端联调项；写 docs/handoffs/parallel-20261104/T5-status.md，证据仅放 docs/evidence/parallel-20261104/T5/。

现在执行Wave A，完成你的独立开发和验证后写status并向本会话用户交付；不要只给计划。无需等待小车或其他会话才能开始。
```
