# Agent Goal Prompt：LanderPi 本地 ROS 2 离线控制闭环

以下内容可以直接交给开发 agent 执行。本任务的授权范围是本地离线开发、必要的隔离环境准备与验证；
后续真机联调需要作为独立阶段推进。本 prompt 不授权 SSH 连车、启动真实臂栈、访问真实串口或发送动作。

---

你在 `/home/liaozhengyan/Desktop/lelab` 开发 LanderPi 的本地 ROS 2 控制链路。
目标是完成可复跑的离线闭环，使同一实现后续可以部署到 Pi 的既有 MentorPi Humble 容器。
请实施、测试并交付，不停留在重新写计划；但不能用模拟结果声称真机已经通过。

先完整阅读：
`/home/liaozhengyan/Desktop/lelab/docs/handoffs/2026-11-05-landerpi-local-ros2-offline-development-plan.md`。
下面是必须落实的执行约束和完成门禁。

## A. 接管与范围

1. 读取根/子目录 AGENTS.md、README、MIGRATION，实际开发副本的README/CLAUDE/pyproject及部署说明，
   然后读2026-11-05的vendor Docker复用、当前调试记录和遥操作审查、现有runbook及集成status。
   旧记录中的“无真实输出实现”已可能过时；以实际源码为准，不能重复实现已有模块。
2. 核对仓库边界、symlink、tracked/untracked差异、现有服务/PID归属，不覆盖未提交修改。
   当前开发目标为 `app_ros_mobile/`、`ros2_ws/`、`tools/ros2_collection/`，不改发布快照或切current。
   发现目标文件在被其他会话写入时保留现场，限定自己的文件所有权，不整文件覆盖。
3. 本阶段不SSH、不连接SO101或车端串口、不启动真实控制/相机、不改网络/固件/电机ID/机械限位。
   不扩大为手柄底盘、9D硬件验收、训练或策略推理。底盘锁定，目标为arm+gripper 6D。
4. 主智能体负责全部修改和验证。按AGENTS可用最多3个default只读子agent核查环境/协议、控制/安全、
   数据/网页；明确具体问题和证据边界，不让它们写文件、运行写入测试、部署或操作硬件，不派生子agent。
   模型目标遵循AGENTS；不能核验主模型配置时如实说明，不修改全局配置或声称已切换。

## B. 固定架构

- 上位机保留 LeLab Python3.12+、本地定制LeRobot、SO101 Reader、三层资产、持久Mapper和同一Coordinator。
- 本地新建隔离的Ubuntu22.04/Humble/Python3.10开发环境，真实运行ROS节点、custom msgs、sqlite3 recorder；
  模拟输入和反馈必须明确simulated。现有Jazzy只作快速逻辑/回归，不替代Humble运行证据。
- Pi候选程序使用与离线相同的控制包：原agent负责HTTP/视频/租约/录制，独立ROS控制进程负责
  本地反馈缓存、安全校验、限速和六轴输出；两者通过本地有界IPC传递许可与撤销。
- 上位机按一个输入快照生成一个arm+gripper完整目标帧；先用一次合帧HTTP验证，再接长连接帧流。
  控制周期不再同步调用远程/status或lease.valid；后台状态缓存必须含年龄，过期严格退出跟随。
- 不要求上位机应用直接加入Pi DDS域，不混用两发行版Python扩展，不为了此次开发重构整个平台。

## C. 按依赖顺序实施

### C0 基线与环境

- 先检查现有隔离环境、容器运行时、镜像/包缓存和磁盘；不重复下载已有资源。
- 生成可复现Humble开发配方、doctor和锁文件；核验候选官方镜像tag/amd64 manifest后锁实际digest。
  未取得digest不得把placeholder或Pi的本地image ID当成registry digest。
- 提供prepare/build/up/accept/down等一条命令入口，支持已有离线镜像/依赖导入和明确的无网络复跑。
  脚本实现前说明哪些步骤要联网；不能把“不连硬件”与“所有依赖已可断网重建”混写。
- 不改全局Docker daemon、系统Python或共享base；优先可用用户级隔离运行时。
  运行时缺失时继续纯逻辑实现和构建配方，明确Humble门禁受阻，不假装已运行。
- 容器不privileged、不映射/dev；所有ROS测试节点在隔离域/命名空间、只发布测试topic。
  所有端口回环绑定并检查占用；只管理本任务实例、真实PID/命令指纹，不pkill。
- 从 `/home/liaozhengyan/Desktop/resource/lander_pi/src/driver/ros_robot_controller_msgs` 准备只读输入副本，
  保留来源和SHA256，在Humble编译；不覆盖参考树。补齐当前package.xml遗漏的实际依赖，
  处理可选Gazebo依赖，不整树构建vendor视觉/导航包。arm64编译产物不能拿amd64产物冒充。

### C1 版本化整帧与时效

- 用一个纯Python3.10可运行的事实源定义teleop_frame_v1，arm/gripper仍保留现有channel权限和command_v1字段。
  同帧共享session/epoch/bundle/sample身份，增加frame_seq/stream_epoch/新鲜度窗口，严格白名单。
- 保留 IDs1..5/10、joint1..5/r_joint、rad单位；夹爪percent必须经已保存映射，禁止直接度转弧度。
- validate-then-commit：坏帧任一通道失败整帧不输出、不推进任意seq、不改变滤波/限速状态。
  六目标一次构造ServosPosition发布，经唯一board owner。不能把一次ROS publish写成板端接受或实际执行。
- 本地队列latest-wins、有界，发送与接收均不积压旧轨迹。两端不能直接相减monotonic。
  按方案实现Pi发放的短期freshness window、连接代际及Pi本地deadline；发送排队、网络迟到、
  本地排队不允许通过重新盖完整TTL变新。心跳不能刷新动作样本寿命。
- 网络流有限长度前缀/尺寸上限、读取和发送超时、异步回执；认证绑定已有租约，不复制新的互斥租约。
  流绑定127.0.0.1并兼容既有SSH端口转发。不要未经核验增加对外监听。
- capability显式协商frame/stream版本；同一epoch只能有一个传输输出，旧apply入口与新流互斥。
  协商失败拒绝，不静默回退到双发、默认值或无门禁路径。

### C2 上位机与Pi本地控制进程

- 复用Reader/Mapper，读取、映射、发送解耦；Mapper会话持久、整帧共同dt、实际参考相对映射。
  重连/松离合/换标定/故障清旧目标，重新对齐和显式接合，不能自动恢复跟随。
- worker不等HTTP/status、六路扫描、录包或磁盘；后台缓存带时间/年龄/扫描跨度，过期fail closed。
  不凭本地时间尚未到期就宣称车端租约仍有效，Pi最终输出边界必须核验当前授权。
- 复用SafetySupervisor、HybridGateway与servo readback/output，补真实独立进程entry point和IPC接线。
  agent保持既有租约权威，控制进程只持受约束镜像，IPC断开/agent心跳缺失有界撤销。
- 控制进程不与HTTP/视频共享GIL；正确配置ROS回调组，避免在控制timer内同步service等待。
  六路串口仍由vendor唯一owner负责，有限超时和无事务ID迟到回包处理不退化。
- STOP/ESTOP抢占并清队列/撤销generation，独立于慢请求和保存锁；不盲目断力矩。
  初始保持10Hz目标、命令TTL<=200ms、反馈年龄<=500ms，并核对扫描跨度；任何调整须说明依据，
  不扩大安全窗口来通过性能测试。其他频率与桥心跳按方案配置、分别实测。
- 控制进程SIGKILL/Pi断电的机械停止仍是硬件缺口；不能因模拟watchdog通过就声称有板级保护。

### C3 录制与网页

- 复用现有SessionRecorder、observe/grasp、校验同步/READY、6D导出和SDK/网页读回。
  新帧事件包含主臂raw/标定值、pre_clip、sent_target、frame/channel seq、反馈来源/年龄/扫描跨度、
  lease/clutch/fault/版本，保持车端时钟域与clock_epoch，确认级别逐层标明。
- 真实反馈topic必须是新增录制profile的必要流；/controller_manager/joint_states命令缓存不能作measured。
  action用限幅后命令，state用实际反馈；离线state明确simulated，不改名实测。
- 仍用sqlite3，调用生产recorder和真实ros2 bag record，不能FakeBagProcess/sleep冒充真实录包。
  两段短episode均含observe/grasp，核对事件、视频/曲线切片与6D值，使用本地定制LeRobot SDK读回。
- 离线数据data_identity=simulated_offline、training_eligible=false；测试导出目录保留该身份，训练入口拒绝。
  不改旧只读session身份，不伪造底盘零值组成9D，保留invalid session。
- 复用现有/landerpi及teleop面板，展示有效输入/目标Hz、反馈年龄、延迟/拒绝原因、许可、离合。
  浏览器不承担实时循环。生产前端用正式构建，实际浏览器点击，分别测试断网、视频冻结、错误取消和恢复。

### C4 性能、故障和交付

- 跑与修改范围匹配的现有测试，增加真实风险测试；Python3.10实际运行与Humble ROS集成分别留证。
  不用AST通过代替运行，不用现有历史通过数代替本轮结果，失败保留首次日志与修复复验。
- 以相同模拟输入/总线条件比较legacy、合帧HTTP、stream，每种正常60秒×3，再测HTTP慢/视频负载/读回超时。
  记录采样、映射、有效新frame、Pi接收/发布、RTT、p50/p95/p99、deadline miss、反馈年龄和CPU。
  10Hz配置初始目标：有效新帧约9–10Hz、稳定场景发布间隔p95<=150ms/p99<=200ms。
  空tick/重复帧不算新帧，跨主机单向时延没有误差界就不报伪精确值。超时应停而非回放。
- 故障矩阵覆盖坏帧/单通道拒绝、过期窗口、上位机退出、TCP半开/重连、IPC EOF、agent退出、租约失效、
  反馈过期、慢磁盘、急停。明确“停止接纳/发布新目标”与“机械实际停止”的不同证据级别。
- 提供完整部署候选包、manifest、依赖与schema示例、启动/停止/回退手册，部署dry-run通过。
  当前大量生产文件未跟踪，禁止只git archive HEAD漏文件；不提交不推送不切current，除非另有明确要求。
  Pi候选路径为/home/ubuntu/shared/lelab_car，release/state/logs/sessions分开；核验后才能使用。
  不覆盖vendor工作区，不删除旧部署/标定/sessions，不执行真实部署或固件烧录。

## D. 完成判据与输出

目标闭环：模拟设备识别 → 三层模拟资产绑定 → 无输出预览 → 对齐/离合 → 六轴合帧跟随
→ observe/grasp 两集 → 真实ROS sqlite3 bag → 校验同步 → 6D测试导出 → 本地SDK及网页读回。
同一生产控制实现通过离线数据通道执行，模拟仅在设备边界替换。

交付时先给结论与入口，包含：

1. 本轮源码差异/归属/版本与未提交修改保护情况；列出实际安装或复用环境，不声称未核验模型配置。
2. 一条命令准备、启动、验收、停止，各自作用范围/实例/日志/退出码；断网复跑是否真的通过。
3. 协议/schema、初始频率与TTL、真实依赖与哈希、三种链路性能对比，首次失败和修复复验。
4. 测试/ROS通信/真实recorder/SDK/真实浏览器各自证据，明确模拟硬件与真实设备未验证。
5. 未完成硬门禁与最小下一步：Pi只读身份/源码/接口/串口owner → 无输出预览 → 当次现场低速授权
   → 真急停/hold/失联 → 两集实录。底盘、手柄、9D和固件保护另列，不混作本轮完成。

证据建议放 `docs/evidence/landerpi-local-ros2-<实际日期>/`；状态与启动/停止说明写到本任务专属交接文档。
只在目标真实达成时写“完成”；环境缺失或某项失败时交付所有已完成独立工作、具体日志和缺失条件，
不把编写脚本、进程启动、HTTP200、mock通过或子agent建议作为核心闭环已经通过的证据。
