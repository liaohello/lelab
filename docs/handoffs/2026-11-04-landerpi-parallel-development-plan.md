# LanderPi 联合遥操作并行开发计划（2026-11-04）

本计划落实 [整体设计](../landerpi-hybrid-teleop-system-design-2026-11-04.md)。目标是：手柄移动底盘、网页看视频、SO101 主臂控制小车五轴从臂与夹爪，明确记录 observe/grasp 两阶段，车端 sqlite3 原始录制，上位机校验与 LeRobot 导出。不做训练、自主策略或解除机械限位。

## 1. 并行组织

使用五个用户可见的开发会话；它们是独立执行会话，不是只读检索子智能体。本会话负责总协调、公共契约、交叉集成、硬件探查与验收。各开发会话的辅助子智能体仍按 AGENTS.md 限制为 default 只读角色。

所有会话使用 /home/liaozhengyan/Desktop/lelab 当前工作区，保留现有未提交修改。没有用户指定 worktree，本轮不切分支、不创建会改变基础版本的工作树。采用严格文件归属实现并行：不要回退、覆盖他人修改；不要 git add/commit/reset/checkout/rebase/push。最终由总协调统一检查、提交（如用户要求）与发布。

| 会话 | 负责文件/责任 | 首轮交付 |
| --- | --- | --- |
| T1 驱动与真实反馈 | ROS agent 包内新的 hardware_io.py、hardware_adapter.py、hardware_safety.py；follower_feedback.py；该会话独立测试；vendor_patches/ros/ | 有界串口事务、可信状态、真 r_joint 适配和无输出安全监督 |
| T2 MCU 固件 | tools/ros2_collection/vendor_patches/firmware/；独立协议规范/测试；work 下独立固件副本 | 编码器反馈、版本能力、板端失联停车补丁及可构建/回退证据 |
| T3 联合会话与标定 | app_ros_mobile/lelab/mobile_teleop.py、mobile_teleop_coordinator.py、mobile_teleop_session.py、mobile_leader_reader.py；ROS agent.py、contract.py；新标定/手柄纯逻辑模块；新 hybrid profile | 两输入分通道控制、版本标定资产、可信反馈门禁、双层租约和无输出模式 |
| T4 录制与导出 | ROS recorder.py；app_ros_mobile/lelab/ros_mobile.py、ros_export_worker.py、dataset_viewer.py；phase/schema 新模块与相关测试 | observe/grasp 事件、六/九维显式 schema、缺失拒绝、同步/导出/阶段读回 |
| T5 网页 | app_ros_mobile/frontend/src/；frontend/dist 构建；必要前端测试/模拟fixture | 完整标定/预览/联合控制/阶段录制/数据检查 UI 与实际点击证据 |

禁止跨写：
- T1 不改 agent.py/contract.py，交给 T3 接入。
- T3 不改 ros_mobile.py；新 HTTP 路由由 T4 接入 T3 的 public facade。T3 先提供独立 facade 与测试并写交接，T4 可先用 fixture 开发，不必等待。
- T4 不改 frontend；T5 依据固定 API 编写，阶段联调前用模拟接口。
- T2 不改 /home/liaozhengyan/Desktop/resource 中的参考树、不烧录、不操作设备。
- 公共契约、根 .gitignore、部署脚本、总 runbook 由总协调管理；其他会话发现需改时写各自交接文件。
- 每个会话只更新 docs/handoffs/parallel-20261104/Tn-status.md 及自己的证据子目录，避免共同追加同一日志。

## 2. 当前事实与保留项

先读 AGENTS.md、整体设计、2026-11-03 handoff 与 2026-11-02 只读验收。实际源码、服务与环境状态以重新核验为准。

- ROS 来源：/home/liaozhengyan/Desktop/resource/lander_pi。
- FreeRTOS 来源：/home/liaozhengyan/Desktop/resource/RosRobotControllerLite_ros_250811，有大量本地差异；HEAD 或目录名不能证明车上固件。
- 当前参考 ROS joint_states 是目标缓存，odom_raw 是命令积分，不可标为实测。
- 原厂 SDK 有无界等待与错帧风险；旧 get-state 服务不能拿来盲探。
- 真夹爪是 r_joint/ID10；仿真 mimic 五关节不是五个独立执行器。
- 当前 watchdog 有真机路由、缺反馈补0和 envelope 丢失等断点；不能直接宣布硬件适用。
- 旧只读 profile/session、invalid session、用户两个 training-monitor release、现有未提交源码与维护工具全部保留。
- 首轮不 SSH、不打开主臂/相机/手柄/控制板，不启停现有 8000/18186 服务，不 docker start、不 bringup、不调用维护/运动接口。
- 不把口令、密钥、私有配置写进提示词、报告或 Git。

## 3. 共享契约 v1：先对齐字段，独立开发

本节是首轮固定契约。实现细节可各自决定；字段变更先写本会话 status 的“接口变更请求”，由总协调决定并通知依赖方，不自行改公共文档或多处复制成不同版本。

### 3.1 命令

logical command:
protocol_version=1
session_id, session_epoch
channel=base|arm|gripper
source=gamepad|so101_leader|maintenance
seq（按 session_epoch/channel/source 递增）
sample_seq
ttl_ms
calibration_bundle_hash
clutch（arm/gripper）
target:
  base: vx_m_s, vy_m_s, wz_rad_s
  arm: joint_names=[joint1..joint5], positions_rad（长度5）
  gripper: joint_name=r_joint, position_rad

maintenance 默认不允许进入运行 profile；只在显式维护会话下可用。
公开 HTTP 请求只给 ttl_ms；本机收到后记录 received_mono_ns/deadline_mono_ns。相同设备内传递保留 deadline；跨设备转发只传剩余有效期和可校验的会话时效信息，不传本机 monotonic 让另一主机直接比较。不得因为每次转发重新接收就重新获得完整 TTL。无事务ID协议不宣称可以按事务匹配。

新状态机/模块可使用 dataclass；车端兼容 Python3.10，上位机 Python3.12+。公共数据 JSON 可序列化，不在上位机导入时强依赖 rclpy。

### 3.2 可信反馈

joint sample:
name, servo_id, raw_position, position_rad
source_kind=servo_position_readback|command_cache|model_placeholder|synthetic
sample_seq, received_mono_ns
sample_time_ns（如设备支持）, clock_domain
age_s, valid, error, calibration_hash

状态完整/新鲜/有限且来源允许，才可用于真实跟随。synthetic 只限显式 rehearsal profile，不进入 hardware profile；command_cache/model_placeholder 绝不成为 measured。
raw_position 超标保留原样并标异常，不能夹成“正常实测”。
六路读取保留扫描跨度和每关节年龄，不宣称同时采样。

wheel feedback:
wheel_ids, counts, measured_rps, target_rps
board_tick, boot_id, seq, valid, faults

base estimate:
vx_m_s, vy_m_s, wz_rad_s, source_kind
sample_time_ns, received_mono_ns, covariance, valid
source_kind 区分 wheel_odometry_estimate、imu_fused_estimate 与 command_integrated_estimate；最后一类不作为九维训练 state。

确认事件级别:
requested, gateway_accepted, driver_sent, controller_acknowledged, execution_observed。
缺少ACK填 unavailable，不从写串口成功推导执行成功。

### 3.3 会话与 API

保留已有 /ros-mobile/teleop/* 接口；内部增加可独立测试的 facade（T3拥有）：
- status()
- acquire_session(request), release_session(reason)
- set_arm_state(event)
- ingest_gamepad(sample)
- preview()
- calibration capability/readiness methods

T4 拥有 ros_mobile.py，负责调用 facade 的路由胶水。
新增接口目标（保留现有兼容路由；新路径仅首轮联合功能使用）：
- GET /ros-mobile/hybrid/status
- GET /ros-mobile/hybrid/capabilities
- POST /ros-mobile/hybrid/session（action=acquire|release）
- POST /ros-mobile/hybrid/control（action=preview|align|arm|engage|release_clutch|pause_all|estop|clear_estop|enable_base|lock_base）
- POST /ros-mobile/hybrid/calibration（action=validate|save；kind=base|follower|leader|mapping|gamepad|camera）
- POST /ros-mobile/hybrid/recording（action=start_session|start_episode|set_phase|end_episode|stop_session）
- GET /ros-mobile/hybrid/recording/status
- 原始同步和导出复用 /ros-mobile/sync、/export 及现有任务状态/取消接口。

status 至少包含:
state, session_id, session_epoch, profile
leases.upper, leases.car
channels.base, channels.arm, channels.gripper
devices, calibrations, feedback, video
recording, last_error, missing_for_following, outputs_enabled

错误返回可读 reason/detail，不能 [object Object]，不能靠刷新把失败提示抹掉。UI 在接口暂未落地时使用独立模拟server/fixture，不在生产后端加“硬件默认正常”的分支。

### 3.4 录制与导出

event: event_id, kind, session_id, episode_id, time_ns, clock_domain, payload。
kind=phase_started|phase_ended|command_status|fault|calibration_snapshot 等。
phase 至少 observe/grasp，可包含 drive/transport/release。
phase 切换不发运动命令，不自动另开 episode；结束本集先暂停所有运动。

旧只读：继续禁止训练导出。
六维：保留 arm_gripper_6dof_phase1，五轴+真实 r_joint。
新九维：mobile_hybrid_manual_9d_v1。
action 顺序为 base vx/vy/wz 命令 + q1..q5 最终目标 + r_joint 最终目标；
state 顺序为验证后的底盘估计 vx/vy/wz + q1..q5 实测 + r_joint实测。
未具备有效底盘反馈/估计时拒绝九维导出，不补0。
成功标注人工 success/failure/unknown，不套仿真真值。
保留相机、时间区间、每字段来源、age、有效性、phase切片来源与拒绝报告。

## 4. 开发波次与依赖

### Wave A：所有会话立即离线开发

T1、T2、T3、T4、T5 同时开始，各自实现可测模块和 fixture。
- T1 先冻结 adapter 公共接口：capabilities(), read_snapshot(), submit(command), stop(reason)，纯逻辑可注入transport；默认无输出。
- T2 先冻结版本能力与轮反馈报文说明；无工具链时仍完成补丁、协议测试和可复现构建说明，不伪造编译/烧录结果。
- T3 先实现 facade 与状态/租约测试，提供 T4 路由调用说明；不要等待硬件或 UI。
- T4 先实现 phase/schema/导出拒绝规则与读回fixture，再接 T3 facade。
- T5 先对固定状态/录制API fixture 实际点击，随后只改网络绑定联调。

每个会话首个可集成交付必须包括 public interface、目标测试命令、已验证范围和硬件待测项。不要只交“计划”或因车未接就停止全部工作。

### Wave B：总协调只读探查，其他会话继续

总协调重新核验运行版本、串口占用、节点/话题、机型、固件能力、设备身份和输入/相机。
若旧查询驱动不安全，不为获取参数调用它；先用静态文件/进程探查，待安全单属主读取实现后再做有界查询。
不以探查为由启动整车 bringup 或安装/改写车端。探查结果写统一 hardware inventory，适配器据此更新配置；未知项明确标记。

### Wave C：无硬件交叉集成

顺序：
T1+T3：可信状态门禁、队列时效、真夹爪六维、预览零输出。
T3+T4：同一租约录制子任务、结束先停、phase无运动副作用。
T4+T5：实际页面开始/阶段/结束/错误/取消/同步/导出读回。
T2+T1：固件协议与解码器使用同一组golden vectors，host模拟超时/boot变化。
总协调跑一次受影响联合检查，避免五会话同时跑全量/构建争资源。

### Wave D：需要实机的验收窗口

D1：真实六路回读与主臂标定、从臂校准、底盘锁定预览。
D2：锁底盘低速机械臂/夹爪、observe/grasp 两短集、六维读回。
D3：可回退固件部署、轮反馈/通信失联停车、底盘外部参考标定、手柄独立运动。
D4：hybrid联合模式、故障矩阵、两个完整任务、九维导出/页面读回。

现场状态/急停/支撑和本次动作范围按现有授权及实际变化确认；新固件烧录和连续旋转硬件改造没有因“并行开发”而自动获得授权。现有1轴维护记录必须重新核验，不恢复旧目标或自动init_pose。

## 5. 验证与证据

只运行本职责相关检查，具体新测试文件由所属会话命名且注明T编号。已有 tools 测试环境可参考：
PYTHONPATH=tools/ros2_collection work/ros2_collection/envs/ros_jazzy/bin/python -m pytest tools/ros2_collection/tests/<相关文件> -q
上位机：
cd app_ros_mobile && .venv/bin/python -m pytest tests/<相关文件> -q
前端：
在已核验Node路径下运行 tsc --noEmit 与 Vite build，并用隔离端口实际点击。

这些命令/环境需先核实。Jazzy/Python3.12 测试不等于 Humble/Python3.10 运行通过。构建成功不等于烧录成功；仿真watchdog不等于真机保护。
本轮不为纯文档或低影响修改扩建无关测试。
新产品缺陷先给可复现的失败用例，修复后通过；协议和安全路径覆盖实际风险，不仅镜像实现。

每个 Tn-status.md 固定写：
- 状态：进行中/可集成/受阻（精确到子任务）
- 负责文件及已改文件
- public API/字段与接口变更请求
- 测试命令、结果、证据路径、版本
- 依赖已具备/缺少什么
- 未验证的硬件行为与最小下一步

最终总协调交付源码diff、接口/标定schema示例、构建/启动/停止/回退、原始和导出读回证据及缺口。不把历史通过数或子会话建议写成本次测试结果。
