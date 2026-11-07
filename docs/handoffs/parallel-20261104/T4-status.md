# T4 Wave A 状态（2026-11-04）

## 状态与范围

**离线事件/导出契约、路由夹具、阶段读回与拒绝用例已可集成；车端阶段时间轴与 9D 导出在真实链路上仍缺两处车端改动（见“接口变更请求”），因此当前生产路径会明确拒绝，不会假装就绪。**

本轮只做无硬件开发：未 SSH、未开串口/相机/手柄、未启停服务或容器、未操作真实设备、未读取凭据。所有结果为离线 fixture 或合成 sqlite3 包，不是实机验收。

| 交付 | 状态 |
| --- | --- |
| 车端 append-only 阶段事件与人工标签契约 | 完成（`recording_contract.py` + `recorder.py` 守卫） |
| observe/grasp(+drive/transport/release) 人工阶段状态机 | 完成（控制器强制顺序、不自动开集、无运动副作用） |
| 结束先停再收尾、急停不等保存 | 完成（含暂停失败降级为 discarded） |
| `mobile_hybrid_manual_9d_v1` 显式 schema 与来源校验 | 完成（导出/拒绝/逐帧来源） |
| 保留 `arm_gripper_6dof_phase1` 六维 | 完成（回归 8 项仍通过） |
| 按 episode/phase 定位视频与曲线 + 切片来源 | 完成（`dataset_viewer`） |
| 拒绝报告落盘 | 完成（`export_rejected.json`） |
| 车端 `/recording/set_phase` 路由 | **缺失**，属 `agent.py`，非本会话可改 |
| 车端 `feedback_provenance` 元数据块 | **缺失**，属 T1/T3 profile 与 agent metadata |
| 真实硬件 `controller_acknowledged`/`execution_observed` | 未接入，一律 `unavailable` |

本会话改 `recorder.py`、`ros_mobile.py`、`dataset_viewer.py`、`ros_export_worker.py`；新增 `recording_contract.py`（车端）、`hybrid_recording.py`、`hybrid_export_schema.py`、`tests/t4_fixtures.py`、三份 T4 测试；证据只在 `docs/evidence/parallel-20261104/T4/`。**未改** `agent.py`、`contract.py`、协调器、反馈模块、前端、根 `.gitignore`、部署脚本与总 runbook。工作区中他人未提交修改保持原样。

## 公开接口

新增/接管的 HTTP 路由（保留原路径，语义按计划 §3.3）：

| 路由 | 行为 |
| --- | --- |
| `GET /ros-mobile/hybrid/status` | T3 facade 只读状态，`outputs_enabled` 恒 false |
| `GET /ros-mobile/hybrid/capabilities` | 优先 `facade.capabilities()`，缺失回退 `calibration_capabilities()` |
| `POST /ros-mobile/hybrid/session` | `acquire` / `release` |
| `POST /ros-mobile/hybrid/control` | `preview|align|arm|engage|release_clutch|pause_all|estop|clear_estop|enable_base|lock_base`；`estop` 走控制器并以 `save_deferred` 立即返回 |
| `POST /ros-mobile/hybrid/calibration` | `validate` / `save`（改为调用 facade 真实 `calibration()`，不再硬编码 503） |
| `POST /ros-mobile/hybrid/recording` | `start_session|start_episode|set_phase|end_episode|stop_session` |
| `GET /ros-mobile/hybrid/recording/status` | 控制器状态（会话/阶段/事件计数/错误），不再返回 facade 的空壳 |

Python 接口：

- `lelab.hybrid_recording.HybridRecordingController(facade, car, *, clock_ns, wall_ns, background)`，方法 `dispatch(action, payload)` / `status()` / `estop(reason)`；`car` 只需 `post(path, body)`，生产实现 `HttpCarClient(_agent_post)`。
- `lelab.hybrid_recording.RecordingError.reason/.detail/.status_code`；`CarRouteUnavailable`（503）。
- `lelab.hybrid_export_schema`：`CONTRACT`、`check_command`、`check_base_state`、`check_joint_state`、`build_9d_row`、`check_phase_timeline`、`phase_slices`、`require_observe_grasp`、`command_status_view`、`manual_result`；拒绝统一抛 `SampleRejected.reason`。
- `lelab.dataset_viewer.episode_phase_slice(dataset, episode, phase)`、`episode_timeseries(..., phase=...)`、`PHASE_SLICE_SOURCE`。
- `lelab.ros_mobile.export_session` 内部新增 `_hybrid_feedback_provenance`、`_hybrid_state_vector`、`_check_command_stream`、`_command_ttl_ms`；`ros_mobile.HYBRID_SCHEMA`/`ARM_GRIPPER_SCHEMA`/`ARM_JOINTS` 现由 schema 模块单一来源导出。

**已知双源（需 T5/总协调知晓）**：`GET /hybrid/status` 里的 `recording` 字段仍来自 T3 facade 的逻辑记账，而 `GET /hybrid/recording/status` 来自 T4 控制器（真实阶段/事件/错误）。两者刻意不合并——控制器不做 facade 的内部状态改写，facade 不假装知道车端时间轴。前端展示应以 `/hybrid/recording/status` 为准；若要求单一口径，需总协调定契约后再统一，本会话不擅自改 T3 返回结构。

## 关键判定

| 规则 | 实现 |
| --- | --- |
| 阶段切换无运动副作用 | `set_phase` 只调 `facade.recording('set_phase')` + 车端事件；测试断言 `set_arm_state` 未被调用、`motion_commands_sent == 0` |
| 不自动另开 episode | 已有 `episode_id` 时 `start_episode` 拒绝；`set_phase` 复用当前集 |
| 结束先停 | `end_episode`/`stop_session` 先 `pause_all`；暂停失败→车端 `discarded=True`，本地 `invalid`，HTTP 503 |
| 急停不等待保存 | `estop` 立即置 invalid 并返回；`end_episode(discarded)/stop_session` 在后台线程执行，失败记 `estop_save_failed` |
| 缺 ACK | `controller_acknowledged`/`execution_observed` 恒 `unavailable`；仅凭 `driver_sent` 断言执行 → `unproven_execution` |
| 9D action | 底盘 vx/vy/wz **命令** + 五轴最终目标 + 夹爪最终目标；缺任一通道拒绝，不补 0 |
| 9D state | 已验证底盘估计 + 六路 `servo_position_readback`；`command_cache`/`command_integrated_estimate`/`model_placeholder`/`synthetic` 一律拒绝 |
| 命令时效 | 每通道按自身 `ttl_ms`（兼容旧 `ttl_s`）判定；缺 TTL 视为不能无限保持 → 拒绝 |
| 不跨集/不跨 epoch 插值 | `validate_event` 校验 `session_epoch`/`clock_epoch`；导出按集切分，不做跨边界保持 |
| 不从未来构造当前观测 | 事件与反馈 `time_ns` 晚于帧即拒绝（`future_event`/`state_from_future`） |
| 单集必须有 observe+grasp | 预检比名字、后检比帧覆盖；缺一即 409 |
| 旧只读/关闭导出 | `training_export=false`、`hardware_readonly`、未知 schema 继续拒绝 |

## 接口变更请求（交总协调）

1. **`agent.py` 缺 `/recording/set_phase` POST 分支**（现落到 404）。T4 已在 `HttpCarClient` 把 404/405 转成 `CarRouteUnavailable(503)` 并把该集标为 `phase_timeline_complete=false`，因此当前**生产路径必然拒绝**带阶段记录的 hybrid 导出——这是有意的 fail-closed，不是缺陷掩盖。
   需要的最小改动（属 `agent.py`，本会话无权限）：
   ```python
   elif self.path == "/recording/set_phase":
       out = agent.sessions.set_phase(str(body.get("phase", "")), agent._sim_ns())
   ```
   同时 `recorder.set_phase` 已就绪（校验同集、严格递增、重复相位拒绝、`error` 状态下拒绝）。
2. **车端 command 事件缺统一 `ttl_ms`**：`component="base"` 事件目前没有 `ttl_ms` 也没有 `ttl_s`；arm/gripper 只有 `ttl_s`。导出据此拒绝（`declares no ttl_ms`）。建议 T1 在三个命令事件统一写 `ttl_ms`（或明确 `ttl_s` 语义），否则 9D 导出无法成立。
3. **session metadata 缺 `feedback_provenance`**：`agent.py` 现有 metadata 有 `runtime_mode`/`training_export`/`export_schema`，但没有 `feedback_provenance`（`joint_source_kind`、`base_source_kind`、`calibration_hash`、`base_estimate_has_covariance`、`max_state_age_s`、`joint_topic`、`base_topic`、`scan_span_ms`）。导出要求该块由车端**可信适配器事实**生成，缺失即拒绝。
4. **hybrid profile 仍 `training_export.enabled=false`、`recording.required_topics=[]`、无 `recording.topics`**：profile 属 T3。在这些字段由硬件验证结论填好前，hybrid 会话本身不可录制/不可导出；T4 不做任何默认放行。
5. **`/recording/start_episode` 忽略请求里的 `episode_id`**：车端自行生成 ID，T4 采用车端返回值为准（已按此实现并测试），如后续要由上位机指定需另行约定。

## 测试与证据

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| T4 三份 + 既有导出/同步回归 | `cd app_ros_mobile && .venv/bin/python -m pytest tests/test_t4_hybrid_recording.py tests/test_t4_hybrid_9d_export.py tests/test_t4_contract_parity.py tests/test_ros_mobile_export_schema.py tests/test_ros_mobile_teleop_export.py tests/test_ros_mobile_sync.py -q -p no:cacheprovider` | **90 passed**（T4 自有 69：recording 20、9D export 12、contract parity 37；既有回归 21 全通过） |
| 车端 recorder/agent 回归 | `PYTHONNOUSERSITE=1 PYTHONPATH=tools/ros2_collection work/ros2_collection/envs/ros_jazzy/bin/python -m pytest tools/ros2_collection/tests/test_recorder_faults.py tools/ros2_collection/tests/test_episode_success_labeling.py tools/ros2_collection/tests/test_agent_teleop_recording.py -q -p no:cacheprovider` | **33 passed, 2 warnings** |
| 语法/来源/差异检查 | 见 `checks.txt` | 车端两文件按 Python 3.10 AST 解析通过；`git diff --check` 干净 |

- 日志：[app-pytest.txt](../../evidence/parallel-20261104/T4/app-pytest.txt)、[car-pytest.txt](../../evidence/parallel-20261104/T4/car-pytest.txt)、[checks.txt](../../evidence/parallel-20261104/T4/checks.txt)。
- 本会话产出/覆盖的证据为 `app-pytest.txt`、`car-pytest.txt`、`checks.txt`、`export-report-summary.json`（时间戳 08:43–08:46）。目录内 `pytest*.log`、`sqlite-run*`、`ruff-initial.log`、`syntax.log` 属本轮之前的 T4 半成品基线，保留未删，不作为本次结论依据。
- 导出报告摘要：[export-report-summary.json](../../evidence/parallel-20261104/T4/export-report-summary.json)（两短集、observe/grasp 切片帧区间、逐帧来源、provenance）。
- 本地 LeRobot SDK 实际导入路径：`/home/liaozhengyan/Desktop/lelab/releases/sanitized-20261024-01/lerobot/src/lerobot/__init__.py`（Python 3.12.16、pyarrow 26.0.0、numpy 2.2.6）。数据集读回用 `lerobot.datasets.LeRobotDataset`。
- 端到端链路：合成 **rosbag2 sqlite3** 包（image + `/joint_states` + `/lelab/base/odom` + `/lelab/events`）→ 生产 `sync_session`（tar 暂存、逐文件 SHA256、READY 校验、原子发布）→ 生产 `export_session`（`LeRobotDataset.create/add_frame/save_episode/finalize` + 本地 SDK 复核）→ `dataset_viewer` 阶段切片读回。两次导出的两集共用同一 mp4，`video_ranges` 不重叠且阶段区间落在集内。

覆盖到的拒绝路径（全部离线）：缺 base/arm/gripper 任一命令流、命令 TTL 过期、命令无 TTL、序号重放/断档、非可信命令来源、`command_cache` 等禁用 state 来源、关节顺序错误、关节数不足、负协方差、未来样本、过期样本、缺 `observe`、阶段事件与 manifest 不符、文件被篡改、`training_export=false`、旧只读、未知 schema、重复导出、会话/纪元不匹配、阶段时间倒退、暂停失败、缺车端路由、缺 `clock_domain`、人工标签不可读。

## 硬件缺口与未验证项

- 未在车或实机上运行；阶段事件目前**无法**写入车端时间轴（缺路由），所以“同一 episode 的 observe/grasp 车端事件”只在 recorder 单测与本地上位机夹具中验证。
- 未验证真实六路 `servo_position_readback` 的扫描跨度、年龄、校准哈希一致性；`base_estimate_has_covariance` 只能依赖车端声明 + bag 内 `twist.covariance` 存在性，两者都不是“估计已被验证”的证明。
- 未验证 video 直播链路、手柄输入映射、机械限位、真实停车；`estop` 的物理效果未测。
- 未跑前端构建、未改前端；数据页新增字段需由前端会话按需消费。
- Ruff 未运行（当前环境无该模块），未安装任何依赖。
- 运行时只报告 DeepSeek 模型族，无法核实具体变体与推理强度是否满足项目目标；未做模型配置变更。

## 下一步（建议顺序）

1. 总协调派 `agent.py` 加 `/recording/set_phase` 分支（3 行），T4 立即用现有夹具复验车端时间轴真实性。
2. T1/T3 在 profile 与 metadata 补 `ttl_ms`、`feedback_provenance`、`recording.topics/required_topics`，并把 `training_export.enabled` 的解锁条件写成硬件验收项。
3. 车端到位后，T4 补一条“真实 recorder + HTTP 桥”的集成用例（仍不需要实机：可用本地 agent 进程 + 假 bag 进程），再交联调。
