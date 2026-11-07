# T1 Wave A 状态（2026-11-04）

## 状态与归属

**离线接口和模拟验证可供 T3 集成；真实硬件输出受阻，不能启用。** 交付纯 Python 反馈、事务、监督器、独立进程运行循环、硬件适配边界，以及基于参考源码的 ROS 补丁候选。未连接设备、SSH、启停服务、构建/安装 ROS、修改参考树或操作硬件。尚未接入 `agent.py`、ROS 节点、车端服务、真实 transport 或固件；这不是实机验收。

本会话更改：
- `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/follower_feedback.py`
- 同目录新增 `hardware_io.py`、`hardware_adapter.py`、`hardware_safety.py`
- `tools/ros2_collection/vendor_patches/ros/{0001-bounded-servo-read.patch,patch_guard.py,README.md}`
- `tools/ros2_collection/tests/test_t1_hardware.py`
- 本状态文件及 `docs/evidence/parallel-20261104/T1/` 下的三份证据。

本会话未修改 `agent.py`、`contract.py`、`recorder.py`、上位机、前端、公共文档或部署脚本；这些文件可能有其他会话同时修改。

## 公共接口与集成语义

- `HardwareAdapter(supervisor: SafetySupervisor, feedback: Any)`：`capabilities() -> dict`、`read_snapshot() -> dict`、`submit(command: dict) -> dict`、`stop(reason: str) -> dict`。默认输出由 `SafetySupervisor(..., outputs_enabled=False)` 关闭；`submit` 不触发 sink。capabilities 中底盘话题 `/controller/cmd_vel`、SI 单位、夹爪 ID10 来源于参考源码配置，**不是现场设备身份确认**。
- `SafetySupervisor(sink, feedback_ok, *, calibration_hash: str, limits: dict[str, tuple[float,float]], outputs_enabled: bool = False, clock_ns=time.monotonic_ns)`。先 `arm_session(session_id: str, session_epoch: int, *, maintenance: bool = False)`；`submit(command)` 在输出边界重检、深拷贝并传递完整 command envelope；`tick()` 检查已激活通道的 deadline/反馈；`stop(reason)` 清理并退役该会话。`feedback_ok(channel)` 必须明确返回 `True`；缺失、异常和其他 truthy 值均拒绝。发生当前活动通道的错误序号、过期、反馈缺失或 sink 异常时停止所有活动通道并退役 session；重启必须使用新 epoch。进程重启后退役 epoch 记忆不会持久化，协调方必须提供跨重启不可复用的 session/epoch 和租约管理。
- 指令使用计划第 3 节字段以及同设备 `received_mono_ns` / `deadline_mono_ns`；必须保证 `deadline <= received + ttl_ms` 且尚未到期。必须按 `(channel, source)` **连续递增** `seq`；第一次可为任意非负数。检测到序号跳跃即按队列丢失处理；这比第 3 节的“递增”更严格，需要 T3 明确适配。`sample_seq` 目前只校验非负整数并保留，未验证其单调性或输入设备的独立过期时间。允许 base/gamepad、arm|gripper/so101_leader；maintenance 仅显式维护 session。arm/gripper 必须 clutch=True；全部通道使用显式提供的有限数值限位；arm 只有 joint1–joint5，夹爪只有 `r_joint`。
- 正常提交仅表示 `gateway_accepted`，`controller_acknowledged` 和 `execution_observed` 均为 `unavailable`。`stop` 返回 `hardware_effect=unverified`，不会补零或宣称停车。`base_twist_fields(target)` 做 vx/vy/wz 到 Twist 的 linear.x/linear.y/angular.z SI 映射。`gripper_servo_target(target, converter)` 只产生一个 ID10，并检查转换结果在控制器 0–1000 范围；`converter` 仍必须依据已验证机械标定和机械限位拒绝非法值，不得只依赖通用范围。
- `run_supervisor(connection, supervisor, *, tick_interval_s=0.02) -> None` 是可在独立进程运行的受信本地 IPC 循环；支持 `op=arm_session|submit|tick|stop`，轮询时检查时效，IPC EOF/错误最终调用 stop。它**不自行启动/监护进程**，IPC 使用 Python connection，不能作为不可信网络输入。实际部署需要独立进程生命周期/父进程死亡检测、唯一输出所有权、独立有界 sink，sink 必须在最终硬件/ROS 输出处再次验证 session/epoch/seq/deadline，不得在排队或跨设备转发中重置 TTL。当前未提供实现该约束的真实 sink；若 supervisor 进程卡死/崩溃，软件自身不能保证设备停车。
- `FeedbackStore(expected_joints, stale_after_s=0.5, calibration_hash=None, servo_ids={}, max_scan_span_s=0.5, allow_synthetic=False)`，通过 `ingest_sample(JointSample(...))` 接收每关节样本，`snapshot(now=None)` 返回每关节 `source_kind/raw_position/position_rad/servo_id/sample_seq/received_mono_ns/sample_time_ns/clock_domain/age_s/valid/error/calibration_hash`、扫描跨度及整体门禁。构造 `JointSample(name, position_rad, wall, source, servo_id=None, raw_position=None, sample_seq=None, sample_time_ns=None, clock_domain="host_monotonic", valid=False, error=None, calibration_hash=None)` 时，`wall` 是本机 monotonic **秒**；序号单调检查按关节，原始位置范围 0–1000 含端点。原始有限越界值原样保留并标 `raw_out_of_range`；非有限 JSON 数值在快照显示为 null 并携带错误。六个关节、ID 配置、标定哈希、来源、有限性、新鲜度、扫描跨度均需满足；人工合成样本仅 `allow_synthetic=True` 的显式 rehearsal 允许。该层信任输入方对位置换算和来源的声明，不能自行证明串口来源或标定换算正确。
- 旧 `ingest_joint_states` 保留接口供显示，但 topic 名不能证明来源；没有原始值/ID/标定/序号的普通 JointState **不再能通过跟随门禁**。现有旧 `agent.py` 如仍只接这种 topic，反馈门禁会失败，需要 T3 接入可信流，不能把命令缓存标为测量。旧只读 profile/session 的数据和配置未删除或改写；其运行行为需由集成检查确认。
- `ServoTransactions(send, *, timeout_s, clock=time.monotonic)`：`read(servo_id, subcommand, fmt, *, cancel=None)` 和由**已有串口所有者 CRC 校验后的**解析器调用的 `receive(data)`。串行读取、限定等待和检查回包长度/ID/子命令/状态；异常、取消、超时、意外或重复帧锁定该实例。`send` 必须由现有所有者提供，要求有界且串行；此类不打开串口，也不能对任意阻塞的注入回调提供硬超时。

## 接口变更请求和未解决事项

1. 请 T3/总协调确认更严格的连续 `seq`、故障退役全 session、新 epoch 重建及 IPC envelope；它们没有修改共享契约字段，但改变恢复流程。跨进程重启的 epoch 持久性和所有权须由协调方实现。
2. 请协调可信 `JointSample` 的发布来源、单关节序号作用域、原始位置到弧度的标定函数、标定 hash 以及真实 ID 映射；旧通用 JointState 不能恢复旧的“可跟随”语义。设备时间不可用时 `sample_time_ns=None`，采用本机接收时间，不可跨设备比较 monotonic。
3. 无事务 ID 的旧协议无法区分“前一成功请求的重复响应，在新请求期间才到达，且 ID/子命令完全相同”的帧；任何本地 generation、简单排空或重启都不能证明其新鲜。该候选在可观察异常后锁定，但需要经验证的物理复位/静默窗口，或 T2 增加事务标识的协议，再论证持续可信读取。`LateReplyGuard.reset_after_transport_reset()` 仅可在独立验证复位后使用，本身不执行/证明复位。
4. 当前没有真实 deadline-enforcing sink、wheel feedback/底盘估计、设备会话租约或硬件停止证明；不能开启真实输出或宣称九维 state 可用。人工确认命令仅为请求/网关接受，不等于控制板 ACK 或动作发生。

## ROS 补丁、来源与回退

参考源码只读，SHA-256 本次实测：
- `lander_pi/src/driver/ros_robot_controller/ros_robot_controller/ros_robot_controller_sdk.py`: `f7c1584b5b1f393041bd86c16c68310d38969c15bf8dfd1f92df10373a6cc504`
- 同目录 `ros_robot_controller_node.py`: `0c4b5df728f18c4a7d1850f0647e26a0d1c892ab2455f3fa0079319a9d2c619f`

补丁在唯一 `Board` 所有者内增加有界读取、回包检查、故障锁定、串口写锁和写超时；服务对失败读报告 `success=False`。禁用无法验证来源的广播读 ID；PWM 读仍有自身原有阻塞风险，但不再持有总线舵机读锁。具体应用/回退命令与前置条件见 `tools/ros2_collection/vendor_patches/ros/README.md`；`patch_guard.py` 对两个文件逐一检查 SHA、对补丁 dry run、应用后复验 SHA，错基线拒绝，反向操作也有严格基线。实际部署必须对停止的目标副本先备份，任何部分失败从备份恢复。本轮只在临时副本测试，未证明车上版本与该基线一致。`servo_read_timeout` 默认 0.1 秒可从 `Board` 构造注入，尚无 ROS 参数；六路扫描频率和扫描跨度必须实测，不能以缓存 50 Hz 发布推断回读能力。

## 本次验证

- `work/ros2_collection/envs/ros_jazzy/bin/python -m pytest tools/ros2_collection/tests/test_t1_hardware.py -q -p no:cacheprovider`：**39 passed, 2 warnings**；Python 3.12.16。包含假反馈、真夹爪单执行器、禁用输出、顺序/epoch/TTL/反馈故障、受控独立子进程 expiry/IPC EOF、模拟回包的错误 ID/子命令/长度/状态、取消/并发/超时/迟到锁定，以及补丁临时副本应用/回退/错基线拒绝。证据 `docs/evidence/parallel-20261104/T1/t1-pytest.txt`。
- 受影响旧测试 `work/ros2_collection/envs/ros_jazzy/bin/python -m pytest tools/ros2_collection/tests/test_mobile_teleop.py -q -p no:cacheprovider`：**22 passed, 3 failed, 2 warnings**。两项期望不具来源证明的 JointState 通过门禁，一项期望超时后立即重启同通道；新行为有意拒绝这些假设，测试未跨归属修改，仍需总协调处理兼容与测试迁移。证据 `docs/evidence/parallel-20261104/T1/legacy-teleop-pytest.txt`。
- Python 3.10 语法解析通过、参考基线 SHA 和项目解释器版本见 `docs/evidence/parallel-20261104/T1/baseline-and-syntax.txt`；本机未找到 Python 3.10，因此未实际运行 Humble/Python 3.10。Ruff 在现有项目环境不可用，未安装依赖；`git diff --check` 对已跟踪的本会话修改通过。

## 硬件下一步

总协调完成源码与车端运行版本/串口唯一所有权核验后，先完成 T1/T3/T2 无硬件契约对接，确认连续 seq、进程/租约及最终输出 sink。另需现场核验真实六路回读与带宽、标定/范围/扫描跨度、底盘单位和轮序、停止/保持/丢通信效果及独立急停；任何真实运动或部署仍须遵守已授权现场范围。没有执行训练或自主推理。

会话运行时仅报告 GPT 系列模型，无法核实主模型具体变体和推理强度是否满足项目目标 GPT-6.1 Sol Max；本轮只读子智能体使用工具提供的 default 角色。

---

## 补充：另一 T1 并行实例的独立验收与增量（本会话追加）

> 上文由同一工作区中的并行 T1 实例写入；本节由**另一个 T1 执行会话**追加，不改动上文任何内容。两者共用 `T1-status.md`，请总协调合并阅读。上文「本会话更改」清单未包含本节文件。

### 增量文件（本会话；全部为保留他人内容后的追加或新建）

| 文件 | 动作 |
| --- | --- |
| `.../hardware_adapter.py` | **末尾追加** `SERVO_READBACK_JOINTS` / `SERVO_READBACK_IDS` / `READBACK_SCAN_DEFAULTS` / `REAL_BASE_TOPIC` / `SIMULATION_BASE_TOPIC` / `BASE_SI_FIELDS` / `validate_si_twist()` / `base_route_boundary()`；未改 `HardwareAdapter` 四个公共方法及 `capabilities()` 返回字段 |
| `tools/ros2_collection/vendor_patches/ros/readback_config.example.json` | 新建六路回读配置模板：`max_readback_hz=None`、`scan_period_s=None`、`measured_on_hardware=False`，50 Hz 明确标为 `command_cache` 发布、非回读能力 |
| `tools/ros2_collection/tests/test_t1_wave_a_acceptance.py` | 新建独立命名 T1 验收测试，不覆盖 `test_t1_hardware.py` |
| `tools/ros2_collection/tests/test_t1_hardware.py` | 仅修复 1 处 `sample_seq` 重复传参 `TypeError`（`params.setdefault("sample_seq", 2)`），未改生产代码 |
| `docs/evidence/parallel-20261104/T1/{pytest-t1-waveA.txt,verification-T1-waveA.json,public-api-T1.json}` | 新建；上文提到的另三份证据由并行实例写入，本会话未动 |

### 新增纯逻辑（精确签名）

```python
def validate_si_twist(target: Any) -> dict[str, Any]
def base_route_boundary(profile: str, *, calibration_verified: bool,
                        machine_type: str | None = None) -> dict[str, Any]
READBACK_SCAN_DEFAULTS: dict   # joints / servo_ids / 顺序扫描 / 超时 / 跨度 / 未实测吞吐
```

`base_route_boundary` 只允许 `hardware_hybrid_manual_v1` + 已验证标定 + 已知机型；旧只读、锁底盘 profile、未标定、未知机型一律 `outputs_enabled=False`。真实 `/controller/cmd_vel`（SI：m/s, m/s, rad/s）与仿真专用 `/lelab/base/cmd_vel` 分离；`wheel_rps_conversion_in_adapter=False`，四轮 RPS 运动学仍只在 vendor ROS `controller/mecanum` 一处。`validate_si_twist` 拒绝 NaN/Inf/bool/字符串/缺键/多余键。**未修改公共字段**，是否把 `readback` 纳入 `capabilities()` 由总协调裁决。

机器可读签名清单见 `public-api-T1.json`；四个公共方法精确签名为：
`capabilities(self) -> dict[str, Any]`、`read_snapshot(self) -> dict[str, Any]`、`submit(self, command: dict[str, Any]) -> dict[str, Any]`、`stop(self, reason: str) -> dict[str, Any]`。

### 独立验证（本会话，非上文数字）

| 项 | 结果 |
| --- | --- |
| 首跑仅 `test_t1_hardware.py` | **29 passed / 1 failed**，退出码 1；失败为测试构造 `sample()` 重复传 `sample_seq` 的 `TypeError`（生产代码无缺陷） |
| 修复后中间复验（两个 T1 文件合跑） | 57 passed, 3 warnings，退出码 0 |
| **最终复跑**（并行实例又追加 3 个用例后） | **60 passed, 2 warnings in 2.32s，退出码 0** |
| 命令 | `PYTHONPATH=tools/ros2_collection work/ros2_collection/envs/ros_jazzy/bin/python -m pytest tools/ros2_collection/tests/test_t1_hardware.py tools/ros2_collection/tests/test_t1_wave_a_acceptance.py -q` |
| 环境 | Python 3.12.16 / pytest 9.2.0（Jazzy venv）；**非 Humble/Python 3.10 运行通过** |
| 3.10 语法 | 5 个文件 `ast.parse(feature_version=(3,10))` 全 ok，仅语法层面 |
| 参考树 sha | SDK `f7c1584b…`、node `0c4b5df…`，与 `patch_guard.BASE` 一致，无 `.orig/.rej/.patch` 残留 |
| patch 生命周期 | `check/apply/check-reverse/reverse` 退出码 0/0/0/0，回退后 sha 恢复 `BASE`；错基线 `apply` 退出码 2 且目标文件字节未变；symlink 目标 `check` 退出码 2 |
| 模拟串口故障 | 正常回包、超时锁存、迟到同 ID/同命令帧不入队且不被后续请求误用、错长度/错 ID/controller error/重复帧/广播 ID 拒绝，全部通过 |

上文报告的 `test_mobile_teleop.py` **22 passed / 3 failed** 属旧期望与新门禁的兼容性迁移项；本会话未跨归属修改该文件，也未把其失败算入本会话 60 passed。未跑全量测试与前端构建。

### 补充接口变更请求

| # | 请求 | 理由 |
| --- | --- | --- |
| R-A | 是否在 `capabilities()` 增加 `readback` 子字段承载 `READBACK_SCAN_DEFAULTS` | 避免配置多处复制成不同版本；本会话只放独立模板，未自行改公共字段 |
| R-B | 确认事件分级补齐 `requested` / `driver_sent` | 契约 §3.2 要求 5 级；当前 `submit()` 仅 `gateway_accepted` + `controller_acknowledged/execution_observed=unavailable`。`driver_sent` 必须由唯一串口所有者在写成功后产生，不能由网关从 write 成功推断执行成功 |
| R-C | T2 golden vectors 与 T1 解码器在 Wave C 用同一组向量；迟到帧锁存的恢复需经验证的 transport reset/静默 | 旧协议无事务 ID，generation/排空/重启都不能证明新鲜 |

### 硬件缺口（未验证，不得当作实机验收）

六路真实回读吞吐与扫描跨度、`stop`/servo stop/保持力矩/断电的实际效果与最坏停止时间、底盘真实机型与轮序/RPS 符号/标定残差、车上烧录固件版本与回包语义、patch 部署后 ROS 节点行为、独立进程 parent-death 与 sink 有界写。当前 `hardware_effect=unverified`、`controller_acknowledged=unavailable`，函数调用成功不等于停车。

**一句话收工点**：Wave A 的 T1 接口、有界协议、可信反馈、真夹爪、独立安全监督、底盘边界与可回退 vendor patch 已离线可集成并留下证据；实机效果一律待验证。

**三问模板**：① 门禁 — 软件可集成 / 硬件受阻（Wave D）；② 证据 — 最终 60 passed、patch apply-reverse-错基线四项通过、参考树 sha 未变；③ 下一步 — T3 接入 + T2 golden vectors 对齐 + Wave B 探查，并裁决旧测试 3 failed 的兼容迁移，是否进入 Wave C？
