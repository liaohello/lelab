# LanderPi ROS 2 原生接入：实机测试执行计划与 Agent Goal Prompt

日期：2026-11-07。本文件是待执行计划，不是实机验收结果。本轮编写计划未连接 Pi、未打开串口、未启动运动。

## 1. 目标与当前依据

在底盘锁定、STM32 固件及其 SDK 不变的前提下，验证：真实反馈 → 双机 DDS → 无输出预览 → 安全姿态准备 → SO101 主从六轴小范围同步 → observe/grasp 两阶段录制 → sqlite3 校验、6D 导出与网页读回。

本轮只读核实：

- 工作区 HEAD 为 `803652e`；存在未提交修改，不能只凭 HEAD 代表候选源码。
- 候选包：`<repo>/work/ros2_collection/local_humble/dist/landerpi-ros2native-20261107T152145Z.tar.gz`。
- 实际 SHA256：`518500c0e0d04548c172b9797319dc74b38af25a6c7dc107c20a0a1c73fdc37e`。执行前重新计算并逐文件核对 manifest；任何修复须生成新版本候选，不能覆盖此包。
- R0–R3 证据在 `<repo>/docs/evidence/landerpi-ros2-native-r1-20261107/README.md`，全部为 `simulated_offline`。历史测试数不能写成本轮实机结果；7 项 skipped 不计作通过。
- `hardware-hybrid-follow-v1-dds.json` 的真实 `runtime.data_source=hardware`、`output_transport=dds_v1`、底盘锁定；其 `offline_validation.simulated=true` 是独立离线说明，不能误读为整个 runtime 是模拟。
- 当前 profile 的 `follower_feedback.stale_after_s=1.0`，上位 `StreamHardwareObserver.FEEDBACK_MAX_AGE_S=1.0`。源健康诊断也有 1 秒参数，诊断与运动放行门限必须分别核对。
- `bin/dds_r1` 是离线装置：启动模拟板卡/相机，桥带 `--simulated --no-services`。禁止将这条命令当作上车启动入口。
- `patch_guard_deadline.py` 要求 vendor 已匹配 0001–0005 后的准确基线；只处理停止的、明确选择的版本化副本，不覆盖原厂工作区。

## 2. 固定边界与提速原则

1. 主臂 USB 在上位机；车端 `/dev/rrc` 仅由一个板卡 owner 持有。采集、力矩读回和位置输出均通过同一 owner，不另开串口。
2. 只复用 Pi 的 MentorPi/Humble/Python 3.10 环境。新增代码、接口包构建产物放在持久化版本目录；先核实挂载，不覆盖 `/home/ubuntu/ros2_ws`。
3. 不修改、构建或烧录 `<vendor-reference>/RosRobotControllerLite_ros_250811`。允许在版本化副本应用本轮 Python/ROS vendor 补丁，必须核对实际加载路径及 SHA。
4. 本计划首轮跟随以 **0.5 秒有效反馈年龄上限**验收，包含源扫描跨度、传输与时钟映射误差。实施前将上位/车端有效门限一致化；如需代码调整，只跑受影响的定向测试、重新出包。不得通过延长门限消除故障。健康卡片的诊断阈值不能替代动作门禁。
5. 保留位置/速度/单帧限制、TTL、双层租约、急停和重新接合要求。所有许可初始关闭；不要将 profile 的输出能力声明当作已获输出许可。
6. 不重跑已完成的全量离线测试；不为每个小步骤重新打包。先集中修完部署阻碍，冻结一个候选，再连续执行。现场动作和主臂摆放可一次说明；相同授权范围内无需逐轴重复询问。
7. 首次输出采用短时低速小幅验证，通过后同一窗口内进入六轴联动。归位沿用当前任务的顺序与进度检查，不能六轴瞬时跳到 raw 500，也不能将 raw 500 等同六轴标定 50%。
8. 故障测试先在静止保持或无输出状态执行，运动中的暂停/急停另作小幅验证。证明“不再发布”与证明“机械实际停住/保持”分别记录。
9. 禁止自动换传输、自动重放或自动恢复运动。停止不以统一卸载力矩代替，承重姿态不能因测试收尾跌落。
10. 仅管理经 PID/启动时间/所属文件或容器标签核实属于本任务的进程；禁止泛用 pkill、清空 sessions 或删除 invalid session。

## 3. 执行关口

### T0：当前现场、版本与回退冻结

操作：

- 以 `pi@192.0.2.20` 为待核实地址，通过已有认证确认实际主机和 SSH 可达性；不要将历史地址、开机状态写成当前事实。
- 核对 MentorPi、架构、Humble/Python、挂载、剩余空间、当前节点和本任务监听端口。进程信息只采集必要字段，不导出完整环境、令牌或其他工具命令行。
- 识别当前运行 agent/control/owner/相机的实际代码、profile、标定、ROS overlay 和哈希；记录 `/dev/rrc` 唯一 owner 及 vendor/app/joystick 是否有其他输出入口。
- 核对 SO101 by-id、序列号和 A/B/C 标定绑定；检测串口是只读探测还是会 configure/写入，不把 `calibrate=False` 当作零写入。
- 将当前车端实际可回退的一整套源码、vendor、profile、接口 overlay、启动环境保存为版本清单与备份。历史旧包仅作历史，不默认选 11 月 6 日候选回退。
- 核实本次运动授权仍覆盖拟执行动作；现场状态若已经变化，只补充确认变化项。路径障碍、承重支撑和能随时触发的急停必须现场确认。

放行：目标身份、版本来源、回退与进程归属明确。取不到独占控制即禁止输出；无明确回退不替换运行栈。板卡 owner 的启动可能有初始化写入，须列明，不能称为纯只读操作。

证据：`baseline.json`、`rollback-manifest.json`、`ownership.json`、`calibration-binding.json`。

### T1：ARM64 原生构建与部署预检

- 在 Pi MentorPi 内、独立 build/install/log 目录构建 `lelab_mobile_interfaces`，执行 Python 消息/服务/action 导入，保存退出码与实际导入路径。只构建本次缺失依赖，不重装整个 ROS。
- 核对候选 manifest、上位/车端接口定义 SHA 和 ROS 类型支持；amd64 成功不能代替 ARM64 成功。
- 在 vendor 副本对 0006 运行 guard check；基线不符时保留差异并整合已存在修复，禁止强行 patch 或替换成较旧基线。
- 明确所有有效反馈门限、控制周期、TTL/window、扫描预算与 quiet 参数；0.20 秒 quiet 下限不缩短。
- 审阅启动脚本并生成实际命令清单，确认没有整车 bringup、init_pose、自动接合或遗留模拟进程。若缺少 DDS 真机启动入口，主 agent 在现有部署工具旁做最小适配，不能直接运行离线 launcher。

放行：ARM64 构建与导入成功、manifest 一致、依赖闭合、门限一致、初始化副作用明确。失败只修对应问题，不回到全量离线测试。

证据：`arm64-build.log`、`imports.json`、`deployment-manifest.json`、`startup-plan.txt`。

### T2：无位置输出的真实总线基线

- 在任务授权下启动最小 owner/采集链，保持控制许可关闭；预检阶段不发送 torque load/unload 或位置目标。
- 连续 60 秒记录六个执行关节 `[joint1,joint2,joint3,joint4,joint5,r_joint]`，ID `[1,2,3,4,5,10]`。
- 记录 owner instance、seq、raw/物理单位、scan 起止/跨度、最后成功年龄、锁等待、逐 ID 超时、quiet/recovery 与 owner/consumer 首次失败。
- 记录真实采样频率、扫描间隔 p50/p95/max；不得以重复序号填充采样率。首轮维持当前约 5 Hz 配置，不同时调到 30/50 Hz。

放行：位置输出增量为 0；所有轴来自测量；连续窗口内没有超过 0.5 秒的不可用反馈；owner/consumer 诊断可区分。若反馈异常，先定位总线、owner、consumer 哪层失败，不能继续运动或靠扩大 stale 门限通过。

证据：`bus-readonly.jsonl`、`bus-summary.json`、`driver-first-failure.json`（无失败也明确为空）。

### T3：双机 DDS 只读对照与时间可信性

- 先画清实际 domain/RMW/网卡/peer/容器网络和 topic 路径。首轮优先让上位桥匹配车端实际 domain；只有跨域桥已经存在且验证时才使用隔离新 domain。把两个 observer 改到 95 并不能连接仍在 domain 0 的 owner。
- 两机统一对应的 Humble 类型支持；按需要仅配置本任务进程的 LAN DDS，保留原配置备份。不能直接照搬离线的 `ROS_LOCALHOST_ONLY=1` 后声称双机可达。
- 只启动反馈桥/observer，不启动有许可的命令发布。连续 60 秒按相同 owner+seq+标定匹配 HTTP 与 DDS，比较 raw；非同扫描不得直接比较为误差。
- 记录源年龄、缓存年龄、clock epoch、offset、误差上界和依据；两机 monotonic 不直接相减，不能用接收时间重戳成 fresh。
- 定向停止本任务上位桥后恢复一次：旧缓存必须 stale；恢复不自动授予运动。

放行：同扫描 raw 差值为 0，标定/owner 一致，seq 正常，年龄上界不超过 0.5 秒；时钟映射依据不成立时失败关闭。HMAC 只说明命令鉴权，不宣称已经部署 DDS-Security 或反馈端身份认证；首轮限定于受控现场网络。

证据：`dds-network.json`、`feedback-compare.jsonl`、`clock-mapping.json`、`bridge-recovery.json`。

### T4：真实网页与 ROS 客户端的无输出流程

- 使用真实 hardware DDS profile；保留旧只读入口和模拟数据身份，不以替换 profile 名称冒充接通。
- 实际浏览器访问 `http://127.0.0.1:18287/landerpi/teleop`，从获取控制权开始点击，不再以 API 获取会话代替被禁用按钮。门禁失败须显示具体绑定/反馈原因，修根因而非删门禁。
- 完成获取 → 预览 → 读取对齐参考 → 暂停 → 释放，并验证输出计数不增长。SO101 在预览中可读，但浏览器不承担实时控制循环。
- 用真实 ROS service/action 客户端验证 SessionControl 的无输出动作、身份拒绝和 ClearEstop 的身份/返回语义；HomeArm 在不具备许可时应被拒，实际执行及取消放到 T5。接口仅有薄包装单测不能算实测。
- 检查新会话选择 `dds_v1` 后不能中途切 TCP；旧 apply/arm/gripper/expert/reset 入口被正确拒绝。无 key/错误 ticket 的拒绝先在无输出状态验证，不记录 key/token 内容。

放行：网页能从头进入会话、状态清楚、主臂与六路真实读数可见；位置发布增量仍为 0；清除急停只解除锁存，`motion_granted=false`，不能自动接合。

证据：`browser-preview/`、`ros-client-results.json`、`preview-output-counts.json`。

### T5：姿态准备与六轴低速同步

- 先核对当前实际姿态与 B/C 标定。历史现场安全姿态是 raw `531/492/525/502/510/457`，只是核验依据，不是本次未经检查就可下发的目标；不默认用 `500/500/500/500/500/350`。
- 逐 ID 非阻塞请求 torque load 并真实读回 verified；任一未确认则不启用位置输出。加载完成后还需新的一帧反馈。只在实际运动准备阶段加载，预检/预览不加载；不自动卸载承重关节。
- 如需归位，运行现有有界任务，内部按已验证次序推进、每段从新鲜反馈重算；监控无进展、反向、振荡、非选轴漂移与取消。路径可达性必须现场判断，关节范围合法不等于无碰撞。
- 到达核验过的安全姿态后，让操作员把主臂放到舒适且接近的姿态；记录主臂参考与从臂实测参考，用相对增量建立映射，不要求不同构型 raw/角度完全相等。
- 先做一段约 10 秒低速小幅方向/暂停验证；通过后连续做 30–60 秒六轴含夹爪小范围联动。不是逐轴重复等待问答；首次异常即暂停对应阶段。
- 沿用实际校准限位、映射比例、滤波、速度与单帧硬限，不为顺滑临时扩大。夹爪按独立开合映射，不对百分比直接度转弧度。
- 暂停后主臂重新摆放，再显式对齐/接合一次，验证不跳变。真实 HomeArm 客户端若纳入首轮，复用这一归位任务并验证取消，不另外重复一轮大行程运动。

放行：六轴方向与语义逐项有实际证据；反馈持续可信；目标连续，停顿无累积漂移；跟随误差按关节单位和标定容差评估，不能给全部关节套一个未经确认的角度容差。

证据：`motion-timeline.jsonl`、`torque-verification.json`、`mapping-direction.json`、`pause-realign.json`、现场视频或操作员观察记录。发布/准入/driver_sent/机械执行分别标记。

### T6：集中故障与恢复

先在静止保持状态执行，恢复后必须新会话或显式重新对齐接合，不能自动续播。已有状态机若要求新会话，不能仅清错后继续用旧引用。

| 场景 | 实施方式与检查 |
| --- | --- |
| 暂停与软件急停 | 先静止验证，再小幅动作中各做一次；记录两层状态、最后发布、真实停止与保持。只暂停不冒充 operator estop。 |
| 清除急停 | 合法原 owner 清除；无关身份拒绝；两层确认，清除后不获运动许可。 |
| DDS 断连 | 定向停止任务 bridge/命令端；不关闭整机 Wi-Fi或公共 SSH。窗口/TTL 到期后车端自行撤权，恢复不回退 TCP。 |
| 租约到期 | 停止本会话续期，保留状态观察通道；agent 与执行者均撤权，旧 token/epoch 无效。 |
| 反馈 owner 重启 | 保持姿态并按已确认的启动副作用处理；新 owner instance 不能等待旧 seq 追平；torque 验证及目标作废，旧帧拒绝。 |
| agent/control 退出 | 先确认该状态下停止/保持策略；只对核实的本任务 PID 注入一次。确认依赖方按 IPC/TTL 停止，不能把观察不到进程等同物理已停。 |
| 过期/重放/旧连接帧 | 通过真实 DDS 发送匹配本测试身份的坏帧，确认不消耗有效序号、不入队、不发目标；不放宽正常流身份与签名校验。 |

主臂真拔 USB、板卡真拔 USB、硬件急停按钮、断电只在现场能配合且机械支撑明确时执行；否则列为未完成，不由软件替身代验。软件急停通过不能替代硬件回路有效性。

停止时延以具体故障发生时间、有效 window/TTL/watchdog/控制周期和实测物理响应分别报告；不临时发明统一停止时限，也不把软件时间当作电机停止时间。

证据：`fault-matrix.json`、每项原始时间线、恢复前后状态及旧目标丢弃证据。

### T7：两集真实数据与交付/回退

- 录制两集，每集约 20–40 秒，包含人工 observe 与 grasp 两阶段。只有现场物体和夹持条件合适时抓取物体，否则如实标记动作阶段/任务结果 unknown，不能以空夹宣称抓取成功。
- 结束本集先暂停控制，再收尾录包；下一集需显式接合。急停不等待保存。只使用生产 SessionRecorder、真实相机、真实 sqlite3 和生产归档/同步接口。
- 检查每集实际时间区间内 frame、反馈、准入/拒绝、发布、owner/scan/标定、离合、相机与阶段事件。计数按录制窗口比对，不能把录制外的 emitted 数与 bag 行数直接相等。
- 录制队列 dropped/未发数量必须可见；控制不中断不代表数据完整。缺少必需 action/state/phase 行或来源失配时拒绝导出，不能补行补零。
- 按真实硬件 6D 契约导出，不走 `simulated_offline_6d_v1` 放行入口；action 是发送目标，state 是真实反馈。底盘不补零成 9D，旧只读 session 继续禁止训练导出。
- 本地定制 LeRobot SDK 和 DatasetViewer 读回两集，按 from_s/to_s、observe/grasp 检查视频/曲线及关节名、顺序、单位；结果为人工或 unknown，不套仿真真值。
- 撤销双层许可、停止流、清队列、释放主臂租约；按本任务进程身份停止服务。保留合适的机械保持状态，不主动统一卸载。若要回退，只恢复 T0 冻结的配套版本，恢复后默认无输出并重新预检。
- 本轮至少演练本任务 stop；实际 rollback 需要按冻结清单核验副作用后执行。未执行的恢复/回退不能仅凭脚本存在记为通过。

证据：两集 sessions、sync/export/SDK/viewer 报告、`final-state.json`、`process-cleanup.json`、`rollback-result.json`（未执行则写未执行）、`source-manifest.json` 与最终 README。

## 4. 结果格式与中断规则

每项结果记录：`id / status(pass|fail|blocked|not_run) / hardware_or_simulated / source_hash / actual_command / exit_code / started_at / ended_at / evidence / failure_layer / next_action`。不包含密码、token 或 HMAC key。

同一失败不无限重试或等待旧序号追平。保存第一现场，定位一层根因；独立工作可继续，但禁止跨越失败的输出门禁。修改后只重跑失效项和直接依赖关口；接口/门限/映射改变需重新预览与对齐，候选字节改变需新版本。

推荐证据根目录：`<repo>/docs/evidence/landerpi-ros2-native-live-<实际时间>/`；使用新目录，不覆盖 R0–R3 的离线证据。

## 5. 可直接交给执行 Agent 的 Goal Prompt

```text
你在 <repo> 负责 LanderPi ROS 2 原生接入的实机联调。执行目标是：底盘锁定，在真实 Pi/主臂/从臂上完成反馈与 DDS 验证、无输出网页流程、受控六轴小范围同步、两集 observe/grasp 录制、真实 sqlite3 校验同步、硬件 6D 导出和本地 LeRobot SDK/网页读回。不要只给建议，按下列关口实际推进并留下证据。

先读根 AGENTS.md，以及：
1. docs/handoffs/2026-11-07-landerpi-ros2-native-hardware-test-plan.md（本计划，T0–T7 为执行顺序）。
2. docs/evidence/landerpi-ros2-native-r1-20261107/README.md、web-clicks.md、hashes.sha256。
3. docs/handoffs/2026-11-07-landerpi-safe-pose-and-teleop-gate.md。
4. docs/handoffs/2026-11-07-landerpi-hardware-convergence.md 与相关最新交接；历史运行状态必须重新检查。
5. docs/ros2-collection-runbook.md、tools/ros2_collection/local_ros2/README.md，及实际要使用的启动/部署脚本。

当前候选是 work/ros2_collection/local_humble/dist/landerpi-ros2native-20261107T152145Z.tar.gz；已核实 SHA256 为 518500c0e0d04548c172b9797319dc74b38af25a6c7dc107c20a0a1c73fdc37e，执行前重算。工作树有未提交源码；不能只凭 commit 判断版本，更不能覆盖他人修改。候选一旦修改必须新版本打包。

Pi 待核实地址 192.0.2.20、用户 pi，优先使用已有认证，不在文件/日志/回复中记录密码、token 或 key。复用 MentorPi/Humble/Python3.10，代码与接口 overlay 放持久化版本目录；不覆盖原厂 /home/ubuntu/ros2_ws、不切 current、不提交或推送、不删除 sessions/invalid session/数据集。STM32 源码与 SDK 仅可只读参考，不修改、构建或烧录。Python/ROS vendor 补丁只能在匹配基线的停止副本上应用并校验实际导入路径。

按 T0–T7 连续执行：
- T0：只读识别真实运行版本、串口唯一 owner、其他控制入口、A/B/C 标定和现场状态，冻结当前实际配套回退，不盲退 11 月 6 日旧包。
- T1：Pi ARM64 原生构建/导入接口包，核对 manifest 和 vendor 0006 基线；准备真正的硬件 DDS 启动命令。bin/dds_r1 是模拟 launcher，不能当上车入口。当前上位/profile 有效反馈门限为 1.0 秒，本计划首轮按 0.5 秒验收，先一致化有效门限；不缩短 quiet 下限，不放宽 TTL 或限位。
- T2：关闭位置输出、不写 torque，采集 60 秒六路真实反馈、扫描跨度/间隔、锁等待、超时与 owner/consumer 首次故障；零位置输出，反馈年龄需满足门限。
- T3：核对实际 domain/RMW/网卡/peer/网络，避免新 domain 与旧 owner 不通；双机 DDS/HTTP 按同 owner+seq+标定对照 60 秒。证明时钟映射误差上界可信，不能重戳或直接比较两机 monotonic。桥重启后旧数据 stale、恢复不授予运动。
- T4：真实浏览器从获取会话开始走预览、对齐、暂停、释放，不再用 API 绕过禁用按钮；真实 ROS service/action 客户端补验证薄包装的无输出/拒绝路径。验证 HMAC、传输互斥、旧输出入口拒绝，位置输出增量仍为 0。
- T5：按现有授权与当前现场条件开始低速小幅运动。逐 ID load 并真实确认 torque，取得加载后新反馈；必要时按现有任务顺序回到现场核验过的安全姿态，不能六轴瞬时跳 raw500。历史安全姿态 531/492/525/502/510/457 仅作依据。主臂放舒适近似姿态，记录两端参考，以相对变化同步。先短时方向/暂停检查，通过后连续六轴含夹爪联动，不逐轴重复等待确认；异常自动暂停。保留位置/速度/TTL/租约保护，不把卸载当停止。
- T6：先静止保持再做故障注入，覆盖暂停/急停/清除、DDS 失联、租约到期、owner 重启、agent/control 退出和迟到/重放帧。物理停止与停止发布分别留证；恢复需重新对齐和显式接合，不自动回落 TCP。真拔线/硬件急停/断电须有现场配合，否则如实 not_run。
- T7：两集短 episode，各含 observe/grasp；先停控制再结束本集，急停不等保存。真实 sqlite3/相机/归档同步/硬件6D导出，不走模拟受限出口；state真实反馈、action发送目标，不补底盘0、不放开旧只读数据。检查必需行与录制丢弃计数，SDK和真实网页按每集/阶段读回。最后撤权释放，核验本任务进程停止与机械保持，保留全部数据。执行 stop；rollback 按本次冻结版本和副作用核实后演练，未做不能记通过。

为提速：最多使用3个 default 只读子agent，按 AGENTS 模型要求核对工具实际能力，禁止其改文件、跑写入测试、操作硬件或派生子agent。可分别核查候选/vendor基线、DDS/QoS/时钟鉴权、6D录制/网页契约；主agent独占所有修改与硬件动作。不要同时让多个会话写同一文件。无独立核查任务时无需凑人数。

不重跑全量离线测试、不重装环境；缺陷只做最小修复、相关定向检查、新候选与必要重验。同一故障不无限重试、不等待旧序号追平。只对影响当前现场安全或超出既有授权的缺失信息提问，不重复询问相同授权。所有真实动作先核对授权与现场状态，不把旧确认当作当前姿态证明。

每个关口记录 pass/fail/blocked/not_run、实际命令、退出码、源码/配置SHA、原始证据和失败层；每60秒以内给有信息量的进度。软件输出、控制器回执、真实机械执行分别标记。无证据不写通过；不以mock/同机DDS冒充双机或实机。证据写入新的 docs/evidence/landerpi-ros2-native-live-<实际时间>/，不覆盖离线证据。

最终交付：T0–T7结果表、真实问题根因及最小差异、候选/实际加载版本、两集数据读回、故障与恢复判据、启动停止回退命令、最终控制/进程/机械保持状态及剩余缺口。未成功的必需关口阻止后续运动，但可继续独立开发；不要把受阻写成完成。
```

提交说明（2026-11-16）：192.0.2.0/24 中的地址是脱敏示例，实际连接信息由本地配置提供。
