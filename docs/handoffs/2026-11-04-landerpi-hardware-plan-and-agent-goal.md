# LanderPi 真机接入计划与 Agent Goal Prompt

日期：2026-11-04，用户时区 America/Los_Angeles。本轮只读探查约为 2026-11-05 02:10 UTC；车端日志使用另一时区，不能直接比较显示时间。

本计划基于[本轮硬件清单证据](../evidence/landerpi-hardware-planning-probe-20261104.json)、当前源码和已有离线集成结果。本轮没有录包、打开控制板串口、发送动作、应用补丁或启停机器人服务。

## 1. 本轮确认的环境与实施影响

| 对象 | 本轮观察 | 对实施的影响 |
| --- | --- | --- |
| 主机 | Raspberry Pi 5 Rev 1.1，约 8GB RAM，Debian 12/aarch64 | 不在宿主重新安装整套 ROS；优先复用车端容器 |
| 网络 | 无线 `<private-ip>`；有线 `<private-ip>`；SSID `<wifi-ssid>` | 无线地址为现有手动配置，不能继续写成 DHCP 漂移；保留有线作为维护入口，连接前核验身份 |
| ROS | `MentorPi` 正在运行，`ros:humble`，Python 3.10.12，host 网络、privileged、映射 `/dev`，restart=always | 每次 exec 显式 source Humble 与 vendor overlay；容器重启不属于无副作用的连接操作 |
| ROS 环境变量 | 正在运行的节点使用 `MACHINE_TYPE=LanderPi_Mecanum`、`DEPTH_CAMERA_TYPE=aurora`、`ROS_DOMAIN_ID=0`、`LIDAR_TYPE=MS200` | 新 exec 的默认环境没有这些变量，不能照空环境启动板驱动；变量仍不能证明物理机型和尺寸 |
| 录包运行时 | rosbag2 storage/default plugins 存在；`ros2 bag record --help` 已成功，包含 sqlite3 | 上位机缺 ament 的旧阻塞有可用目标环境；真实录包尚未执行，下一步可直接补这一项 |
| 相机 | USB Aurora 930；被动收到 20 帧，640×400、bgr8、frame_id=rgb_camera_link，短窗口约 13.35Hz | 复用已有发布者；仍需相机物理安装、外参、长时间频率与端到端帧绑定证据 |
| 控制板串口 | `/dev/rrc -> /dev/ttyACM0`，已由 `ros_robot_controller` 占用 | 新反馈采集不能第二次打开串口，应扩展唯一 owner |
| 激光串口 | `/dev/ttyUSB0`，由 `ldlidar_stl_ros2` 占用 | 不是 SO101 串口，不占用、不替换 |
| 手柄 | USB 2563:0575，`/dev/input/js0`，joy_node 发布 `/joy` | 可在 Pi 用 ROS 手柄输入；按钮轴向、死区、失联和死手开关尚未校准 |
| 现有执行入口 | full bringup 正在运行；底盘命令 5 个发布者，舵机命令 6 个发布者，含 joystick_control、视觉 app、init_pose | 当前没有取得遥操作独占权；接管与旧入口隔离是启用前置条件 |
| 已安装代码 | Board node、SDK 的 SHA256 与 T1 patch_guard 的 BASE 一致；servo_controller 也对应已核查 vendor 版本 | 候选补丁基线可对应，但车端当前仍是未应用补丁的 SDK；需选择停机部署副本和回退 |
| 舵机配置 | joint1..joint5 对应 ID1..5，r_joint 对应 ID10；额外 w_joint/ID9 出现在 YAML | YAML 是配置，不是舵机发现。先保持六维任务；ID9 的实际设备与用途未确认，不自动控制 |
| 舵机范围配置 | 主轴配置 0..1000、init=500；r_joint 的 min=1000/max=0 | 不能当物理限位/实测位置；夹爪有方向语义，不能简单排序后丢失方向 |
| LeLab agent | 8110 未监听，容器内 `/home/ubuntu/lelab_car` 不存在 | 历史部署不能视为仍在；按现有脚本审查后做首次/恢复部署，避免假定已有路径 |
| 上位机 SO101 | 本轮未发现主臂 USB/by-id | 主臂现场连接是后续条件；可先做 recorder、反馈与接线代码 |
| 磁盘 | 根分区约 57G，已用 83%，可用约 9.4G | 先短包并设置容量门禁，测真实增长率；及时校验同步，不自动清理 sessions/invalid |

当前可复用的主要成果：网页与正式 Vite 构建、session/epoch/TTL 门禁、标定及映射模块、六/九维 schema、同步导出与 SDK/viewer、候选 ROS/MCU 补丁。当前生产 hardware facade 尚未绑定完整真实适配器，车端 gateway 默认 `driver=None`，硬件输出保持关闭。

## 2. 按门槛执行的计划

| 阶段 | 实施内容 | 完成判据 | 主要依赖 |
| --- | --- | --- | --- |
| P0 基线与接管准备 | 核验源码/运行版本、备份未提交及未跟踪源码；列出 full bringup、串口 owner、全部执行入口和自动启动链；准备精确停止/恢复方案 | 有可审查的进程、topic/service/action 清单与回退，未发生误停或抢串口 | 当前即可只读推进；运行栈接管需明确归属 |
| P1 真 recorder，无运动 | 在现有 Humble 容器验证真实 sqlite3 写入、结束和校验同步；先隔离测试话题，再短录现有相机；最后接生产 SessionRecorder 的 phase 时间轴 | bag 由真实 ros2 子进程产生且可读；phase/event/manifest 一致；同步哈希一致、退出码可解释 | 不依赖 SO101、不依赖新 MCU 固件；需要任务专属存储和无输出部署 |
| P2 独占控制与真实从臂反馈 | 安全接管/隔离旧输出；对已确认部署副本应用有界查询适配；六个舵机由原串口 owner 串行读；绑定真实反馈适配器与独立监督，输出仍关闭 | 没有绕过网关的旧执行路径；真实读回六路完整、有年龄/跨度/来源；丢反馈拒绝跟随 | 板驱动接管及补丁部署，确认查询和启动写入副作用 |
| P3 主臂、三层标定与无输出映射 | 接 SO101 到实际 USB 所在主机；A 主臂标定、B 车臂独立校准、C 语义/方向/比例/夹爪映射；接双租约与真实 Reader | 设备与哈希绑定正确；预览显示真实两端数据；执行提交计数不增长；无反馈/旧会话门禁有效 | SO101 接入、六路反馈、设备身份和安全校准条件 |
| P4 锁底盘 6D 实机闭环 | 先验证停止/保持和急停，再逐关节低速小幅、夹爪、多关节；故障及恢复；两段短 episode 均包含 observe/grasp | 真动作、真反馈、真 raw bag、生产 sync/6D 导出/SDK/页面一致；结束默认暂停；恢复不重放旧目标 | 当前现场保护条件、既有运动授权范围、全部使能门槛 |
| P5 底盘校准与保护 | 核实板型、固件能力和编码器；审查/交叉构建候选固件，必要时再申请烧录；校验轮序/符号/速度单位/几何与残差、TTL 和板端停车 | 有可追溯轮反馈和有效估计；停止时延、失联停车及 boot/epoch 行为实测；无命令积分伪装 | 独立硬件闭环；新固件烧录和底盘运动不在旧机械臂授权内 |
| P6 手柄与 9D 联合验收 | Pi 手柄输入经同一 gateway；人工调整观察视角/抓取；按阶段锁底盘；整集记录并导出九维；真浏览器演断网、冻结、取消和恢复 | 真实底盘估计+六路反馈齐全；两集 observe/grasp 与视频/曲线对齐；所有控制入口互斥且可停止 | P4/P5 通过、手柄与相机校准、联合运动授权 |

P1 的相机只读录制不能直接导成六维训练数据，因为它没有合格 action/state。可另用明确标记的合成状态/指令测试真实 recorder 的格式与生命周期，这种数据继续保持 simulated 身份。只有 P4 的真实采集满足全部 schema 才作为硬件 6D 数据。

P2/P3 的代码与隔离测试可以交错推进；真实硬件操作由一个主智能体串行执行。P5 的协议/构建工作可与机械臂阶段并行准备，但不能让底盘开发阻塞独立的 6D 闭环，也不能跳过底盘门槛直接放行 9D。

相机是否确实固定在从臂上仍需现场确认。若它固定在车身，改变从臂姿态不能实现用户要的相机视角调整；应记录安装改造或新增相机的必要性，不凭 TF 名称推断安装、不擅自使能 w_joint/ID9。

## 3. 可直接交给执行 Agent 的 Goal Prompt

以下正文整体交给一个执行会话；该会话接管真实硬件阶段。本文内的环境事实是起点，执行前必须复核，不能照搬 PID。

---

你是 LeLab 真机闭环的主执行 Agent。在 `/home/liaozhengyan/Desktop/lelab` 继续把已离线集成的 LanderPi 系统接到真实硬件，完成“SO101 主臂 → 车端五轴从臂/夹爪 → observe/grasp 原始录制 → 6D 导出和页面读回”，通过后再推进手柄底盘与 9D 联合闭环。

先完整阅读：

- 根目录及相关子目录 AGENTS.md、README/MIGRATION 和实际工作副本 `app_ros_mobile` 的说明。
- `docs/handoffs/2026-11-04-landerpi-hardware-plan-and-agent-goal.md` 的硬件事实与 P0–P6 计划。
- `docs/evidence/landerpi-hardware-planning-probe-20261104.json`。
- `docs/handoffs/parallel-20261104/INTEGRATION-status.md`，从早期未完成清单读到最新追加结果，不能只读开头。
- `docs/landerpi-hybrid-teleop-system-design-2026-11-04.md`、`docs/ros2-collection-runbook.md`、相关部署与 vendor patch README。

目标机优先通过已有 SSH 配置连接 `pi@<private-ip>`，有线维护地址 `pi@<private-ip>`。先验证 host key 和机器身份；已有密钥连接可用，不把凭据写入脚本、证据或 Git。无线地址现为手动配置，不能以历史 DHCP 叙述为准。不要为测试网络直接重启：当前 full bringup 与 init_pose 会影响动作安全。

### A. 组织、权限与保护已有成果

1. 先核查共享工作区写入者，备份 tracked/untracked/相关 ignored 源码、配置及构建产物，并保存实际导入映射与 SHA256。不要覆盖用户修改，不切 current，不替换本地 LeRobot，不暂存全工作区、不提交推送。
2. 一个主智能体实现、部署和操作真机。最多三个 default 只读子智能体核查代码/协议/数据契约；不再并行启动同文件写入者，子智能体不 SSH 或操作硬件。
3. 本会话历史已有“最小机械臂栈板端写入、逐关节低速小幅运动”的授权，执行时沿用其范围，不能反复索取相同授权。使能前仍须核验当前设备、支撑/清场、现场监护、急停与保持能力；旧记录不能替代当前条件。
4. 当前原厂 full bringup、手柄/视觉 app 等不是可随意清理的测试进程。先准备受影响的具体进程/启动链、精确接管动作与回退；若尚未明确交接其控制权，仅针对这些运行栈的停止/变更集中确认一次，其他独立开发和无运动验证继续推进。不做泛用 pkill，不停止无关服务或进程。
5. 新固件烧录、底盘/联合运动、扩大限位、连续旋转不包含在旧机械臂授权中。需要时先完成可审查方案、补丁和构建产物，再明确询问缺失的具体授权。不要因此停下已授权的独立工作。

### B. 优先补齐真实录制，避免继续被上位机 ROS 环境阻塞

在 MentorPi 内显式 source `/opt/ros/humble/setup.bash` 与 `/home/ubuntu/ros2_ws/install/setup.bash`；当前是 Python 3.10.12，storage 插件与 ros2 bag CLI 已验证存在。复核实际依赖后使用它，不在 Debian 宿主或共享 base 随意重装 ROS。

先审查 `deploy_readonly_agent.sh --render/--check` 与实际路径、目标端口、PID 管理和退出逻辑。上次探查 `/home/ubuntu/lelab_car` 不存在、8110 未监听，所以必须核实部署，不能假定历史 agent 仍在。任何会订阅执行器并可发动作的 profile 必须显式选择，默认使用无输出配置。保持现有 8080/9090 服务可追溯，不能为试录启动第二套相机或 full bringup。

在任务专属目录先用非执行器测试话题验证真实 ros2 bag sqlite3 子进程、正常结束和故障收尾，再短录已运行的真实相机。接入生产 SessionRecorder 和 `/recording/set_phase`，验证事件落到真实时间轴及 manifest。不得用 FakeBagProcess/sleep 或手工合成 sqlite 文件冒充真实 recorder。

先测磁盘增长率，设置短 episode、可用空间门禁和容量预算；保留原始数据、invalid、READY/哈希同步协议。只读相机 bag 不应变成可训练六维数据；模拟测试始终标明身份和 training_eligible=false。

### C. 接管唯一执行路径并获得真实反馈，保持无输出预览

当前 `/dev/rrc -> ttyACM0` 已由 vendor board driver 持有，`ttyUSB0` 是激光串口；不要新开控制板串口。current MACHINE_TYPE=LanderPi_Mecanum、DEPTH_CAMERA_TYPE=aurora、ROS_DOMAIN_ID=0 应从实际运行环境复核，不能用空的 docker exec 环境启动驱动。

先解决旧执行入口：`/controller/cmd_vel` 有多个 app/joystick 发布者，`/servo_controller` 有 app/init_pose 发布者，另有底层 bus_servo topic/service/action。控制权必须约束实际写板路径；只在网页拿到 lease、只删掉一个 publisher 或只持有唯一串口均不证明旧入口无法绕过。采用最小且可回退的运行 profile/入口隔离，验证网关以外的执行请求被拒绝。

不要直接运行完整 bringup 来“连接设备”，不要启动第二个 ros_robot_controller。按已核实的停止/接管方案准备最小栈，并审查构造时的板端写入、offset、机型、LED/零速等副作用。不能覆盖现有舵机标定或自动改电机 ID。

车端安装的 node/SDK 指纹上次与 T1 patch_guard BASE 一致。执行前重新比较，选择停机的真实部署副本，先备份、check/dry-run，再应用有界读取适配；不得修改 resource 参考树，不能对 symlink 表层乱打补丁。核对 source/build/install 映射以及重启后真正加载的源码。

经同一串口 owner 逐个读取 ID1..5 与 ID10，记录实际舵机型号/协议/原始单位、方向、范围、每关节年龄、扫描跨度、超时及健康。YAML 的 init=500、0..1000 不是实测或物理限位；r_joint 的反向关系单独处理。w_joint/ID9 只做身份核查，不自动纳入控制量。

协议没有事务 ID：串行请求、有限超时、匹配 ID/子命令/长度/状态、迟到包与过期处理必须有效。故障后不能靠简单重启就宣称链路已排空；读吞吐和时效按总线实测配置，不能拿缓存发布 50Hz 当六路回读 50Hz。

完成实际接线：上位机真实 lease adapters、SO101 Reader、车端 lease/反馈 observer、MobileFollowerAdapter、独立 SafetySupervisor 与 gateway driver。当前默认 driver=None；不得通过硬编码 available=true 或直接解除 outputs_enabled 绕过绑定与保护。

### D. 主臂接入与三层校准

上次上位机未检测到 SO101 USB。连接后按 by-id 确认实际 USB 归属与身份，不把网络可达当串口可访问。独占租约和串口锁同时生效，复用本地定制 LeRobot 的设备识别与校准。

A 层主臂：明确 Reader 只读与 configure/calibrate 写入/力矩的差异，网页交互不被 input 阻塞。B 层从臂：独立保存 raw→物理关节转换、方向/零位/软限位/夹爪关系，不写 SO101Follower 标定文件，不强行拖动带载机械臂。C 层映射：显式语义、方向、比例与参考姿态；夹爪百分比单独转换。三层资产原子保存、设备绑定并带版本/哈希。

先做无执行器输出的 preview，显示主臂数据、相对映射目标、从臂真实反馈、误差、年龄和限幅。检查执行提交计数为零；缺反馈、身份/哈希不匹配、租约失败、旧 epoch、TTL 过期应拒绝使能。

### E. 锁底盘完成真实 6D 闭环

following 前验证停止/保持与急停的实际效果及最坏延迟，不能把断力矩统一当停止。独立监督和车端 TTL 必须到执行路径，不能把上位机停止发包当车已停。SIGKILL/断电场景若硬件保护不足，明确缩小可验收范围；模拟 watchdog 不替代实机证据。

底盘锁定，依次逐关节低速小幅、夹爪、多关节。离合接合使用从臂实际参考加主臂相对变化，恢复必须重新对齐。拔主臂/断隧道/读取失败/租约到期/agent 异常的故障验证按当前安全条件分步进行，不自动恢复跟随或重放动作。

两段短 episode 均含人工 observe/grasp，结束先暂停控制再收尾，下一集重新显式接合。记录 raw/calibrated leader、映射前/后目标、网关接受/拒绝、driver_sent、可得回执、真实反馈、相机、离合/故障/标定/phase 事件。发布、接受、执行确认分级，缺回执标 unavailable。

action 使用正式目标，state 使用六路真实反馈；锁底盘 schema 不补底盘列。真实 bag 经生产校验同步、6D 导出、本地 LeRobot SDK 和网页按 episode/phase 读回，视频区间与曲线一致；旧只读 session 仍禁止训练导出。成功标签人工或 unknown。

### F. 通过六维后推进底盘与九维

先核实实际控制板、现有固件、编码器和轮序。候选 T2 协议、host harness、golden vectors 与跨端 decoder 先离线验证，能交叉构建则留产物/哈希；没有工具链则完成隔离构建准备，不伪造 hex。需要烧录时另行取得明确授权及完整回退方案，不为机械臂 6D 强制烧新固件。

底盘标定确认物理轮序/符号、RPS 与 SI 转换、几何、限速、估计误差和来源。vendor odom_raw/odom 及 IMU 融合输出的名字不能证明可信，命令积分不得当实测；验证估计实际输入和协方差。板端 TTL/停车/boot/epoch/失联保护通过后，再让 Pi 手柄经同一 gateway 控制底盘；标定按钮/轴、死区、死手、失联时间与 grasp 阶段底盘锁定规则。

确认相机实际装在可随从臂移动的位置；安装不符时标出机械条件缺口。联合操作中人工选择 observe/grasp，录制真实底盘估计和六路反馈，条件齐备才允许 9D 导出，缺可靠底盘数据不补零。

### G. 验证、交付与停止条件

修改后运行相关测试，在真实 Humble/Python3.10 复核车端；不要以 AST 检查或桌面 Jazzy/Python3.12 代替。前端变更走正式 Vite 构建，真实浏览器实际点击，补上断网、相机冻结过期、错误取消、旧 epoch、阶段/episode 恢复与视频切片。本次证据不可使用早期预生成 9D 包来代表实时采集。

在 `docs/evidence/` 建独立真机目录，记录 UTC/时钟域、设备身份、软件/配置/固件版本和哈希、命令/退出码、首次失败及修复后结果、频率/延迟/扫描跨度、原始包与导出报告路径。状态追加到专属真机交接文档，保留离线历史证据。

交付源码差异、真实标定/映射 schema、启动/停止/回退、实际网页入口、两段真实 6D episode 与读回证据、9D 条件和未通过项。已有授权且独立可做的工作持续完成；仅在需要现场条件、控制权交接或新增授权的具体关口停下，集中说明已准备好的动作及依据，不反复询问相同事项。

不要在“容器可连”“真实 bag 能录”或“预览能显示”时提前宣告全任务完成；分别记录 P0–P6 的事实状态。硬件未满足的项明确未完成，其他独立开发继续推进。
