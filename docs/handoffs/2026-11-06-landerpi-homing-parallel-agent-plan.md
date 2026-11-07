# LanderPi 指定姿态回中与主从同步：并行开发交接

日期：2026-11-06（America/Los_Angeles）。本文件记录交接时的状态，设备和进程状态
必须在操作时重新检查。

## 目标与现场授权

用户的最新明确目标是先令从臂五个执行关节分别到原始舵机位置 500，夹爪到 350，
然后手动摆放主臂对齐，再显式接合并实现主从同步。这里的 500/350 是 **raw tick**，
不是 50% 开合量或角度值；不要把这个目标改写成各轴已标定范围的 50%。用户希望减少
妨碍使用的试验性限制，但要求仍适用：保留机械与软限位、设备身份、双租约、急停、
真实反馈、命令时效、断连和故障保护；不能为实现运动而绕过这些保护。

用户已授权起臂栈、逐关节低速小幅测试及向上述目标移动，并已确认从当前姿态到目标
的大幅转动路径无障碍或线缆缠绕、现场有人观察并可触发硬件急停。不要重复询问同一
授权；开动前重新核实实际状态，如果现场条件变化或真实保护能力缺失则停止并说明。
禁止自动卸力矩、运行整车 `bringup/init_pose`、无差别清进程、覆盖原厂树或删除数据。

## 交接时可核实的现场

- Pi：`<private-ip>`，`MentorPi` 容器。已建立 SSH 密钥；不要把密码写入命令、
  日志、文档或提示词。上位机页面 `http://127.0.0.1:18287/landerpi/teleop`，
  端口 18110 是现有 agent 隧道，18126 转发帧流至车端 18123。
- agent 与控制进程使用已部署 `hardware_hybrid_arm2_trial_v1` 候选，设备数据身份是
  hardware；最后一次检查 control active、agent 输出许可和 emitter 输出均为 false，
  `reengage_required=true`。不要自动恢复运动。candidate 包
  `work/ros2_collection/local_humble/dist/landerpi-arm2-trial-v1-20261107.tar.gz`，
  SHA256 `db2e9aebb647374643b40fe016450c931cff88fdb0b00a8cb99f63088cd2a19b`。
- 真实六路反馈最后一次为 `joint1..joint5,r_joint = 541,630,76,118,907,457`；
  按机械臂 0.24°/tick 换算，joint3、joint4、joint5 到 500 各需约 100°；这不是
  一次小幅试验。来自人工录入的 B 层端点不是已验证的机械端点。
- 第 1、2 轴分别观察到真实小幅跟随，joint1 raw 544→541，joint2 raw 632→630；
  后者用户确认看见移动，随后 `trial_displacement_exceeded:joint2` 停止，页面报错。
  不把这些写成全轴同步或持物能力通过。
- 上位机主臂是 `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149723-if00`。
  Aurora 相机节点曾冻结，重启后页面/帧序号恢复；长期稳定性未验。车端必须再核对
  `/dev/rrc` 唯一 owner 和 actuator topic 唯一发布者。不能把读取状态等同真实执行。

## 已完成但不能误用的代码

- `app_ros_mobile/lelab/hybrid_home_plan.py` 是 **只读规划器**，不具备执行或授权能力；
  根据六路新鲜、真实、绑定标定的 raw 反馈计算单个最多 10 tick 的建议步长。
  `app_ros_mobile/tests/test_hybrid_home_plan.py` 本轮 11 passed。
  真实只读计算见 `docs/evidence/landerpi-midpoint-ros2-20261106/home-request-readonly-20261107.json`，
  首步 joint1 从 541 指向 531；**没有发送这条目标**。
- 根目录 `app_ros_mobile/` 是本次 18287 服务的实际源码，`app`/`current` 指向其他
  release；不要误改后者或切换 `current`。
- 新建了各轴试验 profile，`arm2` 版本已部署，`arm3`、`arm4`、`arm5` 及对应源码
  改动只在工作区，尚未打包部署或进行实机验证。Humble/Python 3.10 车端针对性测试
  83 passed，上位机帧链路相关 36 passed；只代表离线测试。
- 现有 trial 单次只允许指定轴，最大相对位移 0.08 rad，且不支持同时切换到夹爪。
  现有代码没有自动回中接口。不得把上述只读规划器当成已实现的控制器，不得绕过
  facade、agent 和车端单写者直接发布 ROS 伺服消息。
- 工作树大量 tracked 改动及 untracked 源码是既有成果。不得 reset、checkout、清理、
  覆盖其他会话编辑，未经明确要求不提交、不推送。旧 invalid sessions 必须保留。

## 并行分工与统一契约

先由总协调读取本文件、`AGENTS.md`、运行手册和现场证据，冻结源码快照并核验状态。
给不同 agent 分配互不重叠文件；多个会话共享同一目录时不能同时写同一文件。若使用
worktree，须先解决大量 untracked 源码不会自动出现的问题，不能丢失或覆盖它们。
先约定并固定以下契约，再并行实现，禁止各自另造控制入口。

回中是同一个 hybrid session 下用户明确启动的 **可取消任务**。请求包含固定目标
`joint1..joint5=500,r_joint=350`、session/epoch 和标定 bundle；状态包括实际 raw、
当前轴/目标、进度、反馈年龄、许可、故障和停止结果。只产生当前一步，下一步必须
重新读取真实反馈，先确认实际达到或明确停止；禁止预排整段轨迹和重放旧目标。
每次命令继续走既有帧流、TTL、车端 monotonic deadline 和唯一串口 owner。持力/停止
效果必须如实报告，不能把发布成功称为机械执行或把仿真 watchdog 当硬件急停。

### A：车端受控回中（拥有 `ros2_ws/src/lelab_mobile_agent/` 相关实现、config 和车端测试）

设计独立的明确授权回中策略：单次只允许一个轴，包括 gripper；每步根据**当前真实
读回**检查方向、步长、软限位、速率、session/epoch、校准和 TTL；错误、停滞、反馈
过期、租约或连接失效、急停必须中止并清旧目标。用户授权的是整次前往指定姿态，
因此可以设计持续受限的多步执行，不应把临时试验的 0.08 rad 总位移阈值当成机械
极限；但不能简单增大/移除阈值，必须有逐步边界与可验证停机。明确 gripper raw→rad
方向和允许范围，确保输出过滤仅当前执行轴，其他轴不发目标。测试真实生产类的
正常路径、stale/拒绝/停滞/乱序/急停/断连/恢复，以及每段前重新读取反馈。

### B：上位机会话与计划（拥有 `app_ros_mobile/lelab/hybrid_home_plan.py`、
`hybrid_stream_hardware.py`、`ros_mobile.py` 及各自上位机测试）

在现有 `FrameStreamHardwareFacade` 的工作线程、双租约、router permits 和
`FrameStreamDriver` 内接入回中状态；使用已落地规划器，不在浏览器或新进程中实现实时
控制。新增 route 必须用 `_validate_hybrid_request` 校验 session/epoch，急停继续
无身份抢占。轴切换、每段完成、取消和故障要有明确应答；实时展示实测而非命令缓存。
回中结束撤权并清旧目标，人工调整主臂后重新采集双端参考、显式接合，不自动跟随。
只读 planner 中的 `motion_authorized=false` 不能改成“已启用”或作为执行凭据。

### C：网页（拥有 `app_ros_mobile/frontend/src/pages/LanderpiTeleop.tsx` 及前端测试）

在现有页面增加“回到指定原始姿态”、取消、进度和明确故障原因；展示六轴 raw 目标
和真实值、当前执行轴、反馈年龄、输出许可及视频。急停在任何状态立即可用，不能被
保存或网络等待阻塞。回中完成引导摆放主臂→对齐→接合，默认底盘锁定。前端不执行
实时循环或计算安全控制。构建真实 Vite 产物并实际点击主要按钮及失败/取消路径。

### D：总协调/真机（不与 A/B/C 同时写它们的文件）

合并前逐项核对跨端字段、profile 与 bundle 绑定、单写者、相机和急停；只针对相关
风险运行必要测试，至少包括上位/车端故障与无输出、Humble/Python 3.10、前端构建。
生成新版本化候选、逐项哈希并可回退，先在隔离仿真证明不能自动恢复，再以用户已有
授权在真车低速逐段执行，实时记录指令/拒绝/实测、进度、帧新鲜度及误差。每轴到位
核验后才继续，偏差、碰撞、停滞或不可信反馈立即停止；无需先征求已给过的同一授权。
最后以实时读回证明五轴 500、夹爪 350（注明实际容差），让用户手动放置主臂，重新
对齐并逐轴、再多轴测试同步。底盘保持锁定；不要把这些结果写成 episode/录制验收。

交付实际源码差异、最少必要测试结果、包哈希、页面状态、逐轴 raw 实测及未完成项；
真实未完成时明确指出，不用 fixture、命令成功或发布计数冒充机械位置。
