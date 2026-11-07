# LanderPi：参考厂商源码的离线集成计划与 Agent Goal Prompt

日期：2026-11-06（工作机日期；上一轮 Pi 交接文件使用 2026-11-07）。

用户最新边界：**暂时不动底层 STM32 SDK。** 本轮只做无硬件开发；STM32 工程和
SDK/固件仅作为协议与能力核查来源，不修改、不生成针对它的补丁、不构建、不烧录。
旧并行计划中涉及固件修改或直接上车的步骤不适用于本轮。

## 已有成果与真实缺口

- 最新交接：`docs/handoffs/2026-11-07-landerpi-hardware-convergence.md`。
  六轴逐 ID 加载与读回已留实机证据；v14 已上传，但未启动及验收。
- 本轮不能把历史安全停点写成重新检查过的实时状态；不连接 Pi 或主臂核查。
- 当前开发源码为 `app_ros_mobile/`、`ros2_ws/src/lelab_mobile_agent/` 和相关工具；
  `app`/`current` 指向另一 release，不修改或切换。
- 回中后端已有 `only_joint`、取消和反馈保护，不重新搭建控制器。
- 回中驱动力矩状态仍在发布后直接标记 enabled，与逐 ID 验证工具尚未统一。
- 反馈重启恢复依赖 stale + 低序号启发式；回中 pending 仍持有旧序号，必须做跨层恢复。
- 网页回中请求尚未携带 `only_joint`；现有预检也未覆盖完整 hybrid/homing 路径。

## 两套厂商源码必须如何使用

只读来源：

1. `/home/liaozhengyan/Desktop/resource/RosRobotControllerLite_ros_250811`
2. `/home/liaozhengyan/Desktop/resource/lander_pi`

先记录相关源文件 SHA256，再按实际代码建立调用链：

`网页/API → facade → FrameLink → agent/control → ROS 消息 → Python node/SDK →
AA55 板卡帧 → packet_handle → UART5 舵机命令 → 回包 → ROS 实测反馈`。

优先读取：

- ROS：`src/driver/ros_robot_controller/ros_robot_controller/ros_robot_controller_node.py`、
  `ros_robot_controller_sdk.py`，以及同级 driver 中的 `ros_robot_controller_msgs/msg`
  和 `srv`。
- ROS：`servo_controller/servo_controller/controller_manager.py`、`servo_controller.py`，
  以及相关 launch/setup/install 映射；确认命令缓存与硬件反馈的区别。
- STM32：`Hiwonder/System/packet_handle.c`、`Hiwonder/Peripherals/serial_servo.c`、
  `Hiwonder/Portings/serial_servo.h`、`serial_servo_porting.c`、`packet_porting.c`、
  `Core/Src/stm32f4xx_it.c`。**只读取这些文件，不改它们。**

输出字段契约表：消息类型、flag/value 数组、servo ID、位置/时长单位、命令码、
返回字段、超时、错误语义和确认级别。特别确认：

- `present_id=[1, servo_id]`；不能把 servo ID 填到 flag 位。
- 当前修复版 Python SDK 的 True→load 与参考 MCU 的命令码一致；不再次反转。
- 六轴位置、逐 ID torque 写入及其读回不是同一种批量/回执能力。
- 原厂 controller_manager 的目标缓存不能作为 measured state。
- 参考源码哈希、已部署版本记录、实际烧录版本证据分别列出；不能相互替代。
- 不增加 MCU 请求序号或事务 ID，不改既有线缆协议；缺失能力如实保留。

## 实施顺序

| 步骤 | 实施范围 | 交付与通过条件 |
| --- | --- | --- |
| P0 契约确认 | 两套 vendor 树、当前应用和版本化 ROS 补丁 | 调用链、字段/单位表、源哈希；区分已修复、待集成和 MCU 无法提供的能力 |
| P1 力矩与使能 | 车端 Python/ROS、既有逐 ID 工具和相关测试 | requested/verified/failed 分开；逐 ID 有限时读回；确认缺失不得进入回中；写入后重新取得新鲜位置快照 |
| P2 反馈恢复 | 现有 owner 发布、反馈消费者、回中与跟随状态 | 明确发布者实例/代际；重启立即撤权并丢弃 pending/旧目标；恢复观测不恢复运动；旧实例迟到帧拒绝 |
| P3 连续诊断 | 现有预检、会话和离线 Humble 装置 | 一次运行完成预检→会话→有界心跳→only_joint→取消/故障→释放，产出完整时间线和停止原因 |
| P4 网页与回中判别 | LanderpiTeleop 页面、现有回中状态机和模拟板卡 | 单轴选择正确透传；方向错误、无进展、振荡、延迟与非选中轴漂移可辨识；实际点击验证 |
| P5 冻结交付 | 当前打包、校验及回退工具 | 新候选包与源码逐哈希一致；Humble/3.10 导入通过；版本化 vendor 依赖可追溯；输出真机待验清单 |

P0 后优先完成 P1、P2，再做 P3/P4 的跨层集成；P5 收尾一次。不要让新的控制入口绕过
既有 facade、双租约、FrameLink、TTL、急停、标定、限位和串口唯一 owner。

P1 的“验证成功”只是实际读回加载位，不代表测得力矩或机械执行成功。读回服务继续
复用现有 owner，不能另开 `/dev/rrc`；不要让等待服务阻塞 ROS executor、HTTP 主循环、
控制 tick 或急停。任一轴确认失败时不启动位置输出，也不自动卸载其他承重轴。
无输出预览不应因接入新流程而触发 torque 写入。

P2 的发布者身份由软件 owner 产生，明确命名其来源；不能称为已验证的 MCU boot ID。
序号只在同实例内比较。先处理代际变化，再评估 pending 完成条件；单纯等新序号追平
或接收低序号不能算完整恢复。接口若版本化，应同步 profile、录制与消费者，拒绝未知版。

P3 心跳归属于当前诊断任务，设总时限；取消、操作者/父任务失联、故障或结束即停止
续租并撤权，不能用永久后台续租掩盖失联。预检默认只读，不自动加载 torque 或回中。
模拟设备使用隔离 domain/topic 和 simulated 身份，物理串口不可访问。

P4 依据朝目标方向的位移及误差收敛判断进展，不能只用绝对位移大于零。非选中轴需
独立比较任务起始位置与当前实测，不能用不断更新的保持目标掩盖漂移。容差和时间
要求从现有配置与采样分辨率解释，不能为了通过而扩大步长、放宽限位/TTL。

## 多智能体分工

遵守当前根 AGENTS.md：最多三个 `default` 只读子智能体，禁止子智能体修改文件、
运行会写入的测试/构建/部署、操作硬件或派生子智能体。**主智能体负责全部修改、验证
与最终裁决。** 本计划不是三个会话并写同一个工作区的授权。

- A：只读核对 STM32 命令分发、UART5 读写与能力边界。返回 ID/命令/参数/确认语义、
  路径行号和无法由 Python 层解决的限制；不设计或实施固件改动。
- B：只读核对 ROS msg/srv、node/SDK、现有补丁和安装映射，查找字段、单位、力矩极性、
  超时及命令缓存/实测混淆；不把参考树当成部署事实。
- C：只读核对当前回中、反馈恢复、页面 only_joint 与离线装置集成缺口，区分已有功能
  和待补部分，给出必要的最小回归范围。

子任务同轮派发；主智能体同时读交接与关键代码。收齐影响接口的结论后再修改，避免
反复推倒实现。禁止另起用户聊天或通过额外 CLI 绕过模型/并发约定。

## 最少必要验证与交付

- 按根规则使用已核实的上位 Python 环境；车端代码在隔离 Humble/Python 3.10 运行。
  不将 Jazzy/3.12 测试或 AST 解析当作 Humble 运行通过。
- 复用现有 tests/harness，增量覆盖字段、逐 ID 加载部分失败、服务超时、owner 重启、
  pending 清理、过期/旧代际、停滞/反向运动、单轴隔离、取消与急停。
- 至少一次生产类的离线 ROS/API 连续流程；模拟设备身份明确。页面修改做官方构建和
  实际浏览器主要按钮点击；若环境阻塞，保留错误，明确尚未验证。
- 只运行受影响检查；通过后不反复全量回归或重新制作历史录制/导出证据。
- 保留未提交内容、旧包、sessions、invalid sessions 和数据集；不提交、不推送、
  不切 `current`，不停止与任务无关的进程。
- 交付源码差异、vendor 契约与哈希、新包哈希、命令/环境/退出码、模拟证据、未完成项
  及下一次真机脚本。下一次真机仍先 joint1，不能宣称六轴回中或同步已验收。

## 可直接粘贴的 Agent Goal Prompt

```text
在 /home/liaozhengyan/Desktop/lelab 完成 LanderPi 的离线控制链集成加固。先阅读
AGENTS.md 和 docs/handoffs/2026-11-06-landerpi-offline-integration-plan-and-agent-goal.md，
以这份计划及用户“暂时不动底层 STM32 SDK”的最新约束为准。

目标：让下一次上车不再逐个排查消息契约、加载确认、反馈重启和会话续租问题。
完成一条可复跑的离线流程：只读预检→新会话→逐 ID 加载确认→单轴回中→故障/取消→
撤权释放；网页支持选择 only_joint。不是这轮执行真实运动。

必须读取并对照这两套本地源码：
/home/liaozhengyan/Desktop/resource/RosRobotControllerLite_ros_250811
/home/liaozhengyan/Desktop/resource/lander_pi
记录关键文件 SHA256，核对消息字段、单位、flag/value、servo ID、命令码、回包、
超时、串口所有权、launch 副作用及源码/安装映射。输出具体文件与行号。参考树不等于
实际部署或已烧录版本。两套参考树都保持原样；ROS Python SDK/node 如需修正，只能
通过现有机制制作版本化补丁或隔离副本，保持现有 MCU 协议和可回退性。

STM32 源码只读：不修改、不生成针对它的补丁、不构建、不烧录，不以固件问题未修完
为由停止其他离线工作。协议没有事务 ID，不能虚构该能力；Python 层无法解决的问题
记录为限制，并保持 fail closed。

先读最新交接 docs/handoffs/2026-11-07-landerpi-hardware-convergence.md 和底层审查
docs/handoffs/2026-11-06-landerpi-firmware-servo-control-audit.md。六轴 torque=1 有历史
实机证据，v14 仅上传未启动；本轮不重新连接设备，不把历史状态写成当前实测。
实际开发目录是 app_ros_mobile/、ros2_ws/src/lelab_mobile_agent/ 和相关 tools/configs，
不要修改 app/current 对应 release 或切换 current。

按 AGENTS.md 同轮使用最多三个 default 只读子智能体：A 查 STM32 协议和能力边界，
B 查 ROS SDK/node/msg/srv/补丁，C 查回中/反馈/网页集成缺口。子智能体不得改文件、
运行写入式测试/构建、访问硬件或继续派生；主智能体负责全部实现、验证及裁决。

按优先级完成：
1. 统一 torque requested/verified/failed，复用唯一 owner 的逐 ID 有限时读回；确认
   缺失不进入回中。加载后重新读取新鲜位置；不要自动卸载承重轴。预检和无输出预览
   不做 torque 写入。现有 True→load 修复不能再次翻转。
2. 为软件反馈 owner 引入明确实例/代际并贯穿消费者与控制任务。重启即撤权、清空
   homing pending 和旧目标，不等待序号追平；恢复观测后仍须显式重新开始，不自动运动。
3. 复用现有离线 Humble 栈做连续诊断入口，统筹预检、双租约、有界心跳、only_joint、
   时间线、取消和释放。续租随任务/操作者存活，总时限到或失联即撤权。
4. 网页接通 only_joint，显示逐轴加载确认、目标/实测、反馈代际/年龄与停止原因。
   修正回中进展判别，覆盖反向/振荡/无进展；独立监测非选中轴漂移。
5. 复用模拟板卡添加必要故障注入，运行生产类跨层集成。模拟身份始终明确，不连接
   物理串口、不启动整车 bringup/init_pose，不绕过 facade/FrameLink/双租约/TTL/急停。
6. 最后生成新版本化候选，核对源码/vendor/profile/依赖哈希及 Humble 3.10 导入，
   给出启动/停止/回退说明和下一次 joint1 真机验收入口。

只跑受影响的必要测试与一次集成；页面变更做正式构建和真实浏览器点击。禁止通过
扩大步长、放宽限位/TTL、伪造反馈或让模拟冒充 hardware 获得通过。硬件卡滞、保持、
真实停止和物理急停均留待真机，不能由离线测试宣称完成。

保留全部既有未提交修改、旧证据、旧包和 invalid sessions；不提交、不推送、不清场，
不启动或停止现有真机服务。发现并行修改先协调，不能覆盖。交付可审查源码差异、
调用链/契约表、源与包哈希、验证命令/环境/退出码、明确未完成项。完成所有可独立的
离线工作后停止，不自行转入真机联调。
```
