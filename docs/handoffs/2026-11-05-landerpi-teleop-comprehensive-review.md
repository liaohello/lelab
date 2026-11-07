# LanderPi 主臂无法控制从臂：当前源码与运行状态审查

日期：2026-11-05，America/Los_Angeles。证据记录约 09:03 UTC。

## 1. 结论与范围

当前实现完成了真实主臂输入、车端六路读回、三层资产和无输出目标预览，尚未完成普通遥操作执行链。主臂移动只改变预览目标、从臂不动，与当前代码一致；不是靠修改夹爪方向、增大反馈重试或重启服务即可完成的故障修复。

同时存在数个可离线复现的映射与门禁缺陷，以及停止/心跳/显示问题。在连接真实输出之前必须处理；不能直接将 outputs_enabled 改为 true。

本轮只读核查运行服务与 HTTP GET 状态，检查源码/资产，并运行隔离的离线测试及复现。未 SSH、打开真实串口、获取控制租约、发送控制 POST、改变力矩或触发运动。三个 default 子智能体只读核查不同源码域，主智能体整合并执行验证。

依据：用户提供的[最新调试记录](2026-11-05-landerpi-current-debug-development-summary.md)、当前工作树、运行服务的环境与选定资产，以及[本轮证据](../evidence/landerpi-teleop-review-20261105/review-evidence.json)。没有把历史实机结果计成本轮实测。

## 2. 当前运行状态

- `lelab-landerpi-left.service` 为 active/running，工作目录是 `app_ros_mobile`，监听 18286。
- `LELAB_HYBRID_HARDWARE=1`，实际主臂身份为 `so101-leader-5B90149723`；实际 agent URL 为 `http://127.0.0.1:18110`。
- tunnel unit 为 activating/auto-restart，最近 SSH 退出码 255。本轮 18287 未监听。
- GET `18286/ros-mobile/hybrid/status` 返回 disconnected、session_id=null、upper/car held=false、feedback_valid=false、outputs_enabled=false。hardware_probe 报 car_unreachable/connection refused。
- capabilities 返回 preview_readback_only、hardware_verified=false、outputs_enabled=false，动作列表没有 arm/engage。

车端当前不可达，与交接中的断电状态一致；本轮未独立确认 Pi 的物理供电。即使车端恢复可达，下面的无输出实现仍然存在。

## 3. 主从链路逐段结论

| 链路 | 当前实现 | 审查判断 |
| --- | --- | --- |
| SO101 左臂读取 | UpperLeaderLease/SO101LeaderReader；历史真实采样已留证 | 已有实现，当前因会话结束没有重新打开读取 |
| 主从参考与预览 | HardwareObserver.capture_reference/_preview | 有目标计算，但存在状态/计时问题，见 F02/F03 |
| 页面接合 | beginPreview 只执行 acquire、preview、align | 没有普通受控跟随入口，见 F01 |
| 周期动作提交 | submit_intent/dispatch 接口存在 | 未接入主臂周期控制 worker，见 F01 |
| 上位机执行适配 | ReadbackOnlyDriver | submit 明确抛错，无输出 |
| 车端 gateway | epoch/序号/TTL/许可队列 | outputs_enabled=false，dispatch 不调用真实执行器 |
| 车端 driver | RosServoReadbackDriver | 真实反馈适配，submit 同样明确抛错 |
| 校准移动 | 独立 calibration job/ROS publisher | 与普通遥操作分开；校准能动不能证明 following 可用 |
| 停止/保持 | 当前正常路径清逻辑状态、只读 driver.stop | 不能证明机械停止、持载或急停效果 |

## 4. 确定发现与修复要求

### F01 · P1 交付阻断：真正的 following 执行链尚未实现

证据：

- `app_ros_mobile/lelab/hybrid_hardware.py:564`：ReadbackOnlyDriver.submit 恒抛 `readback_only_driver_has_no_output`；`:697–699` 的 hardware 工厂实际绑定此 driver。
- `app_ros_mobile/lelab/hybrid_facade.py:210–215`：router 固定 outputs_enabled=false；普通主臂采样没有进入 submit_intent/dispatch 的周期调用点。
- `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/ros_servo_readback.py:268–277`：车端 driver 只读，submit 抛错，stop 返回 not_applicable_readback_only。
- `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/hybrid_gateway.py:152–175`：未使能时弹出命令后返回 no_output_wave_a，driver_sent=false。
- `app_ros_mobile/frontend/src/pages/LanderpiTeleop.tsx:202–220`：按钮流程只到 preview/align；`:429–469` 没有 arm/engage 的实际跟随交互。

这是“此前已通电也不跟随”的直接原因。当前反馈 driver_bound 只证明有读回适配器，不能解释为具备动作输出能力。普通状态机虽然能接受 arm/engage 并产生逻辑 following，也不等于发出过动作；不能把状态枚举当执行证据。

修复应补齐后台实时控制 worker、主从目标提交、跨主机剩余 TTL 传递、车端真实命令 adapter、唯一 owner 的最终门禁及可验证 stop/hold，随后新增明确的受控输出使能/离合流程。保留 preview 默认无输出。

### F02 · P1：每次预览新建 Mapper，滤波和速度限制丢失

`hybrid_hardware.py:380–395` 每次 snapshot 创建新的 TeleopMapper。其滤波缓存、上次目标和时间均清空。`mobile_teleop.py:214–223` 对首次目标没有前次状态限制。

离线复现：最大速度 0.5rad/s，在新的 0.1s 周期直接得到 1.0rad 目标，相应允许增量应仅为 0.05rad。该结果对应当前每轮重建对象的行为，不代表已发生真实跳变。

修复：会话持有持久 Mapper；对齐时以从臂实际参考初始化滤波/限速状态；一个周期共同计算完整六维目标。换标定、松离合、故障及重连时丢弃旧状态并重新对齐。输出 owner 仍需独立验证目标速度与连续性。

### F03 · P1：Mapper 跨关节共用时间戳，后续轴会冻结

`mobile_teleop.py:185,217–223` 使用一个 `_last_wall`，每调用一个关节即更新时间。正常逐轴调用使用同一个周期 now，第一轴 dt 正常，后续轴 dt=0。已有目标时它们的允许步长变为 0。

离线复现：两轴从 0 同时请求 1rad，1s 后按 0.5rad/s 应都到 0.5rad，实际 joint1=0.5、joint2=0.0。当前 F02 重建对象暂时掩盖了此问题；只修 F02 会暴露持续状态下其他轴不动的问题。

修复：采用整帧 dt 或每关节独立时间戳，验收同周期多轴连续目标。夹爪也需要独立明确的 raw/rad 速度限制；现有 gripper_target 的 now 参数未用于限速。

### F04 · P1：HardwareObserver 可把无效反馈重新标成有效

`hybrid_hardware.py:449–491` 根据本地标定是否存在覆盖 sample.valid，并主要按样本数/age 判断 feedback.valid；忽略输入 available/valid、来源拒绝、扫描跨度等失败信息。`:594–600` 又用该结果覆盖页面的 trusted 声明。

离线复现输入六个 command_cache、valid=false、available=false、reason=scan_span_exceeded、scan_span=2s、age=0.01s，输出 feedback.valid=true、sample.valid=true，仍保留 sample.source_kind=command_cache，形成矛盾声明。

修复：只允许在输入有效性基础上进一步收紧，不能提升可信等级。统一检查来源、ID/关节序、有限值、采样/扫描年龄与跨度、错误、标定身份，并计入本地 HTTP 接收耗时。保持真实来源和失败原因。

此复现证明 observer/ready/显示层有缺陷；不表示已有真实执行器能绕过全部下游门禁。当前仍无正常输出，部分通道还有独立 TrustedFeedbackGate。

### F05 · P1：三层 readiness 未核对当前映射与实际主臂的跨层绑定

`hybrid_hardware.py:298–318` 加载三个各自合法的资产后直接生成 bundle 并返回 ready=true。HybridCalibrationStore.load 已做单文件格式/哈希检查，但这不验证 mapping.bound_identities 是否等于本次加载的 leader/follower。

离线复现：把合法 mapping 的 bound leader 改成 different-leader-device，重新通过正常 store.save 保存；实际加载另一个 leader，HardwareObserver.calibration_readiness 仍返回 ready=true。

修复：增加三层联合语义检查，覆盖实际设备身份、关节顺序、B/C 坐标与单位及需绑定的版本/哈希。不能仅复用要求六类资产齐全的 9D bundle 门禁，导致锁底盘 6D 被无关底盘条件卡住。

本轮运行环境及实际 C 层均已指向左臂 5B90149723，未发现当前仍读取历史右臂的证据。这是门禁缺陷，不是宣称本次绑定已经错误。

### F06 · P1：急停仍等待状态查询持有的锁，前端也可能禁用急停

`hybrid_session.py:236–239` 在共享 RLock 内执行 _refresh；刷新又同步调用 car.valid 和最多三次 observer.snapshot（`:187–216`），每次车端 HTTP 可等待 4s。`emergency_stop:475–482` 仍获取同一 RLock。录制控制器已绕开保存锁，但没有绕开此状态查询锁。

离线双线程复现：status 在 observer 中阻塞时，emergency_stop 至少 200ms 无法完成；释放 status 后才完成。4s 网络超时和重复查询会进一步放大潜在等待。本轮没有测量实际硬件最坏停止延迟。

前端 `LanderpiTeleop.tsx:463–466` 还在无 session 或任意 busy 时禁用急停，与后端允许身份异常时抢占的设计不一致。

修复：慢 I/O 离开控制状态临界区；独立可抢占停止与车端执行 watchdog，避免锁在网络调用期间被占用。前端急停不应被普通操作 busy 禁用，并明确显示“请求未送达/逻辑已停止/硬件已确认”不同事实。

### F07 · P2：心跳 effect 随每次状态刷新重建，实际频率不是固定 3s

`LanderpiTeleop.tsx:174–178` 的 session useMemo 依赖整个 status；`:181–200` 的心跳 effect 依赖该 session 对象，并在每次运行时立即 heartbeat。状态每 350ms 换一个新对象，因此 effect 被反复清理重建，3s timer 很可能始终来不及触发，立即调用反而按状态刷新频率发生。

这是静态可确定的 React 依赖行为；本轮未持有真实会话测量实际 HTTP 频率。75s 脚本按 3s 发送心跳的结果不能验证该浏览器 effect。

修复：仅依赖 session_id/session_epoch/hash 等稳定标量，保证一个在途心跳；状态轮询不触发额外续租。统计浏览器实际心跳频率、重叠请求与车端 HTTP 负载。

### F08 · P2：故障后保留旧预览，同时轮询会抹去操作错误

`LanderpiTeleop.tsx:149–160` 只在新预览 available 时更新 preview，失效时没有清空；`:285–296` 继续展示旧目标和旧 follower 值。每次成功轮询又执行 setError(null)。`:307–308` 的从臂正常标记仅检查 driver_bound 和六个 sample 数量，没有检查有效性/年龄。

后果：页面可能保留“跟随误差”和旧实测，操作失败提示在下一次 350ms 轮询后消失，妨碍用户区分预览、失效和真执行。

修复：按 epoch/来源/采样年龄使旧目标和反馈失效，明确标记历史数据；错误按所属请求维护并由用户关闭或同一操作成功清除。无输出时使用“预览差值”，不把差值绿标当物理跟随证据。

### F09 · P1（启用前证据缺口）：200ms 静默自动恢复不能证明旧回包已全部排空

`vendor_patches/ros/0002-ros-uint16-state-guard.patch:34–63` 清队列后等待 0.2s，无新帧就清除锁存；`:138–141` 在读失败后调用该恢复。协议没有事务 ID，超过 0.2s 才到达、恰好匹配后续 ID/子命令的旧回包仍不可与新回包区分。

目前没有回包最大迟到界、独立 transport reset 或等价物理证据支持这个 0.2s 假设。静默恢复改善可用性不能自动获得实测新鲜度保证；README 的“故障永久锁存”描述也需与新补丁行为统一。

修复方向：任何读异常先使跟随失能；恢复采用验证过的链路复位/排空策略并重建会话和参考。增加跨静默窗口的迟到相同回包故障测试，明确仍不可观测的协议限制。单纯延长等待时间不构成证明。

## 5. 标定与方向复核：已确认什么、不能推断什么

- 实际服务和资产均指向左臂 `5B90149723`，A/B/C 版本与 handoff 一致。版本来自内容哈希；handoff 的 sha256 对应完整资产的 canonical JSON 哈希，不是文件字节哈希，也不能直接与 content_sha256 字段混比。例如当前 A 层三种哈希前缀分别为 canonical=277fb7e5、file=051541d4、content=cbf6d23f。
- 当前 B 层 method=operator_manual_full_entry，limits_source=operator_entry_not_mechanical；保留该来源是正确的。它不足以证明机械极限、回差、重复性及五轴实际方向已验收。measured、格式合法、preview_ready 和 motion_qualified 应拆开，避免一个 ready 兼任所有资格。
- 夹爪观察端点为 closed=504/open=192，但工作端点已内缩为 closed=496/open=200；当前 C 层 direction=-1、每百分比 2.96tick、pulse_min=200/max=496，与工作跨度 296tick 一致。没有依据认为当前“夹爪不动”是这个百分比跨度写错。
- raw=500 是 vendor 坐标原点，保留它不等于伪造 physical zero；需要明确 B 层是在继承坐标还是建立新的物理参考，不能仅因是常量就直接改零点。
- joint5 wrap=true、五轴方向/scale=1、手填范围等仍需针对实际机构和线缆逐轴验证。无输出预览可确认数学符号，物理方向必须在真实执行 driver 和停止保护完成后以低速小幅验证，不能先要求“无输出完成物理方向验收”再无限期推迟 driver。
- 当前不存在普通 following 的真实动作/状态采集证据，尚不能交付两集真实 6D 遥操作数据；校准动作、预览目标、录制框架和已存在的相机 bag 不能互相替代。

## 6. 验证结果与证据位置

本轮既有定向测试：**89 passed，4 warnings，exit 0，4.66s**。覆盖 hardware composition、上位机路由、T3 契约、硬件租约和 HybridSession。环境为上位机 Python 3.12.16/pytest9.2，非 Humble/Python3.10 或真机输出验收。

另用[可复现离线脚本](../evidence/landerpi-teleop-review-20261105/reproduce.py)确认 F02/F03/F04/F05/F06 五个失败模式：[结果 JSON](../evidence/landerpi-teleop-review-20261105/reproduction-result.json)。脚本 exit 0 表示这些现存错误行为被复现，不表示修复通过。

```bash
PYTHONDONTWRITEBYTECODE=1 app_ros_mobile/.venv/bin/python \
  docs/evidence/landerpi-teleop-review-20261105/reproduce.py
```

运行快照、测试命令、输出末段和源文件哈希位于 `docs/evidence/landerpi-teleop-review-20261105/`。既有测试通过证明当前无输出合同被保持，没有覆盖完整真实跟随。未运行运动、断电注入或真实停止测试。

## 7. 下一轮实施顺序

1. 将五个离线复现转换为独立的回归测试，先修 Mapper 整帧状态/计时、反馈可信度、跨层绑定和急停争锁，再修浏览器心跳/陈旧数据。保留无输出默认。
2. 实现一个持久的上位机实时控制 worker：从 Reader 新样本、真实从臂参考和确定的 Mapper 生成目标，执行 sample_seq、限位/限速、TTL、会话/标定绑定。UI 只负责显式 arm/engage/release。
3. 在唯一板 owner 前接通车端 ArmGripperOutputAdapter 和独立监督，明确 raw/rad 转换、唯一执行入口、停止/保持策略及接收/发送/执行确认层级。校准 job 不作为绕过正常安全链的替代。
4. 用显式模拟边界验证完整“Reader → worker → command transport → gateway → owner sink”端到端；验证旧 session、过期、队列阻塞和读/网断时不发送。新测试必须能在当前缺少执行链的版本上失败，不能继续只断言 motion_commands_sent=0。
5. 车端上电后核验真实部署哈希、唯一 owner、六路反馈和保护条件；按已有授权范围从单轴低速小幅验证 driver、方向和 stop/hold，再做多关节/夹爪。范围新增或现场条件缺失时只在具体关口确认。
6. 最后完成两段短 observe/grasp 的真实 6D 录制、校验同步、SDK/页面读回。底盘保持锁定，9D 与新固件工作不阻塞本轮机械臂闭环。

本轮新增审查文档和隔离复现证据，未修改任何生产控制代码、标定资产或运行服务。下一步的工作性质是补齐并验证真实遥操作功能，同时修复上述门禁和算法缺陷。
