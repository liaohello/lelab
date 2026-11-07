# 任务 #9 证据：生产 agent 授权、录制与 6D 闭环

日期：2026-11-05（UTC）。范围：本机 Docker / ROS 2 Humble / Python 3.10.12 离线仿真；
未连接 Pi、未访问真实 USB、未部署、未产生真实运动。

## 1. 环境与实例身份

| 项 | 值 |
| --- | --- |
| 镜像 | `lelab/humble-dev:local`，基线固定 `ros:humble-ros-base-jammy@sha256:a3f4de78…ef93` |
| 容器标签 | `com.lelab.owner=tools/ros2_collection/local_ros2/bin/docker`、`com.lelab.instance=landerpi-local-humble-offline` |
| 隔离 | host 网络、无 `--device`、无 `/dev` 映射、仓库只读挂载、镜像内无 pyserial、帧端口仅回环 |
| 车端 | ROS_DOMAIN_ID 93、帧流端口 18122、agent HTTP 18130、Python 3.10.12 |
| 上位侧 | 工作站 Python 3.12、`app_ros_mobile/.venv`（本地定制 LeRobot 可编辑安装） |

身份见 `manifest.json`（27 个改动文件的 SHA256、报告清单、四张截图哈希、数据位置）。

## 2. 一条命令复跑

```bash
# 建/重建栈（默认缴械：不取租约、不绑 epoch、不给输出权限）
sg docker -c "tools/ros2_collection/local_ros2/bin/docker up"
# 9A/9B：容器内单测 + 唯一发放者核验 + preview/follow/faults + 旧动作入口拒绝
sg docker -c "tools/ros2_collection/local_ros2/bin/docker accept"
# 9E：经 agent HTTP 与帧流现场驱动故障矩阵
sg docker -c "tools/ros2_collection/local_ros2/bin/docker faults"
sg docker -c "tools/ros2_collection/local_ros2/bin/docker faults --kill-agent"   # 含 agent SIGKILL 场景
# 9D：一次会话、两集 observe/grasp、真实 sqlite3 录包、归档、同步、受限模拟 6D 导出
sg docker -c "tools/ros2_collection/local_ros2/bin/docker record"
# 缴械（先在帧流上暂停跟随，再由 agent 撤销输出权限）
sg docker -c "tools/ros2_collection/local_ros2/bin/docker disarm"
# 只停止本启动器创建且标签归属正确的实例
sg docker -c "tools/ros2_collection/local_ros2/bin/docker down"
```

数据集网页读回需要上位侧应用：

```bash
cd app_ros_mobile
LELAB_ROS_AGENT_URL=http://127.0.0.1:18130 \
LELAB_ROS_COLLECTION_ROOT=$PWD/../work/ros2_collection/local_humble/upper6d \
HF_LEROBOT_HOME=$PWD/../work/ros2_collection/local_humble/lerobot_offline \
  .venv/bin/python -m uvicorn lelab.server:app --host 127.0.0.1 --port 18260
.venv/bin/python ../tools/ros2_collection/local_ros2/offline/verify_viewer.py \
  ../work/ros2_collection/local_humble/lerobot_offline/lelab/<数据集目录> \
  http://127.0.0.1:18260 /tmp/viewer-readback.json
```

## 3. 门禁结果（现场运行，非单元测试推断）

### 9A 生产 agent 是唯一租约/许可权威

- `up` 不再启动模拟发放器；`container_ops assert-single-issuer` 扫 `/proc` 证明没有
  `licence_agent.py` 进程存活：`reports/agent-status-after-accept.json` 同轮记录 agent
  自身 `licence.bridge` 计数。
- 许可镜像来自 agent：`reports/fault-matrix.json` 的 `mirror_carries_the_agents_token_digest`
  显示镜像携带 agent 租约 token 的摘要、`outputs_enabled=false`。
- 许可 generation 只由事实变化铸造；心跳只刷新，不重新授权（单元测试
  `test_agent_licence_bridge.py` 与现场 `short_lease_*` 用例一致）。

### 9B 输出仲裁与故障恢复

- 旧动作入口在帧传输持有输出时全部 409 `frame_transport_active`：
  `/hybrid/apply`、`/control/arm`、`/control/gripper`、`/expert/run`、`/task/reset`
  （`accept` 输出的 `assert-legacy` 段，逐条打印）。
- 帧传输模式下 agent 不创建第二个可输出的 servo publisher：agent 日志
  `frame transport: readback bound, no agent-side servo output driver`。
- 急停后 renew、新 generation、重连都不能恢复运动：`faults.json`（帧流急停）与
  `fault-matrix.json`（agent HTTP 急停）分别证明；恢复必须显式 `clear_estop` 后再
  `enable_output`，且 `clear_estop` 本身不带任何输出权限。
- 租约到期即撤销镜像（`expired_lease_revokes_the_mirror`，原因
  `car_lease_released`，不是等 TTL 兜底）；agent 被 SIGKILL 后镜像按 `ipc_eof` 失效。

### 9C 录制身份、阶段与时钟

- 许可套接字上新增独立的 `recording_context` 消息（不授予任何权限、不产生 generation）：
  agent 录制会话/集/阶段/`clock_epoch` 由 agent 自己发布，控制进程只照抄。
- 每条 `teleop_frame` 记录同时带两个时钟并明确命名：`stamp_ns`+`clock_domain=
  car_system_time`（与 bag/事件同域）与 `control_mono_ns`+`control_clock_domain=
  car_process_monotonic`（本地 TTL/窗口）。`bag-verify.json` 的
  `monotonic_clock_never_equal_to_recorded_clock` 与 `row_stamp_is_the_bag_clock`
  （最大偏移 0.5 ms）现场核验。
- 无上下文的行明确记为 `recording_context=missing`，导出阶段拒绝把它塞进任何集。

### 9D 真实录包、归档、同步与 6D 读回

`record` 一轮（会话 `ros_20261105T185501Z_3aa0f300`）：

| 步骤 | 结果 |
| --- | --- |
| 生产 SessionRecorder 起 `ros2 bag record` | sqlite3 `raw/bag/bag_0.db3`，`bag_exit_code=0`，READY + manifest_sha256 |
| bag 读回核验（rosbag2_py，不信计数器） | 21 项全 PASS：108 行、2 集各 54 行、相机 214 帧 640×400、阶段窗口 108/108 命中 |
| 逐帧对应 | `(stream_epoch, frame_seq)` 108 个唯一对；每集恰好一个 stream_epoch；28 行 pre-clip joint5 与发送值不同（限幅可见） |
| 生产归档 HTTP | `/sessions/<id>/archive` 下载 808125 字节；tar 内含 bag、manifest、events，且不含归档自身 |
| 生产 sync_session | `status=raw_ready`、7 个文件逐一 SHA256 校验、暂存→原子发布 |
| 6D 导出（受限模拟契约） | `export_contract=simulated_offline_6d_v1`、2 集 × 54 帧、`fps=10`（实测行频）、`data_identity=simulated_offline`、`training_eligible=false` |
| 本地 LeRobot SDK 读回 | 六维名称 `joint1..joint5,r_joint`（state `.position`/action `.target`）、单位全 rad、来源标注模拟、图像键 `observation.images.wrist_simulated` 400×640×3 |
| 网页读回 | 真实浏览器点击：整集/observe/grasp 切片、第 0 与第 1 集，视频 Range 206，控制台 0 错误；四张截图哈希互不相同 |
| 数值逐项核对 | `viewer-readback.json`：页面每条曲线每个采样点与数据集存储行逐轴比对，2 集 0 差异 |

### 9E 回归（最终一轮现场数字）

| 命令 | 结果 |
| --- | --- |
| `bin/docker accept` | rc=0，容器内 114 项单测全通过，随后 23 项 preview/follow/faults/旧入口拒绝全 PASS，0 FAIL |
| `bin/docker record` | rc=0，46 项 PASS（会话 12 项、bag 读回 21 项、同步/导出与收尾 13 项），0 FAIL |
| `bin/docker faults` | rc=0，22 项 PASS，0 FAIL |
| 容器内车端 17 个测试文件 | `320 passed, 1 failed`（唯一失败见下节，非本轮引入） |
| 上位侧导出/录制/组合相关 app 测试 | `73 passed` |
| 上位侧 app 全量 `tests/`（除 `test_jobs.py`） | `716 passed, 1 failed`；唯一失败 `test_remote_inference.py::test_client_command_targets_the_tunnel` 期望的相机字典里没有 `backend/fourcc`，而 `remote_inference.py` 与它的测试文件在工作树里都未修改，即该失败在 HEAD 就存在，属推理线，不是本轮接线 |

- 工作站宿主 Python 3.12 无 rclpy，`test_agent_exit_teardown.py`、
  `test_integration_car_phase.py` 等车端文件只能在容器内跑；这不是本轮引入的限制。
- 收尾状态实测：`/status` 的 `licence.frame_outputs_enabled=false`、
  `bridge.state="idle"`、`issuing=false`、`control.active=false`、`estop_latched=false`；
  控制进程 `licence: False / outputs_enabled: False / paused: True`。

## 4. 未通过的门禁（如实记录）

1. `test_agent_exclusivity_guard.py::test_the_deployed_agent_never_writes_torque_state`
   失败。失败原因不是本轮接线：`git show HEAD` 的 `agent.py` 里没有任何
   `*torque(` 调用形状，而本轮开始前已存在的 tracked 修改（follower calibration 扭矩
   功能）引入了 `set_torque(`、`_calibration_set_torque(`、`follower_calibration_torque(`
   与 `"enable_torque"`。本轮对该文件的唯一相关改动是给
   `_calibration_set_torque` 加了帧流拒绝守卫（调用次数不变）。结论：扭矩写能力与
   “网关只写位置、绝不禁用扭矩”的既有安全断言相互冲突，需要该功能作者裁决；
   本轮不擅自放宽该断言，也不把它记为通过。
2. 离线仿真无法证明物理停车：控制进程被 SIGKILL 或 Pi 掉电时的实际停车效果
   未验证，也验证不了；现场只证明“不再接纳/不再发布新目标”和模拟 hold 行为。
3. 网页曲线坐标轴标签仍是 `state[0..5]`/`action[0..5]`，不显示关节名；名称与单位
   目前由数据集 meta 与 `viewer-readback.json` 逐项核对，页面可读性属后续前端小改。
4. 本轮未做三链路频率基准，也未调 TTL：`follow` 段有效发射率仍记录为
   8.29–8.93 Hz（`follow.json`），留给下一轮。

## 5. 现场发现并修掉的问题（不是测试改动）

- `clear_estop` 的桥接调用写在 `return` 之后，成为死代码：许可清除从未发出。
- 租约替换/过期不撤销帧模式输出标志：新租约会继承上一会话的“已给输出”。
- `hybrid_stop` 只关旧网关标志，不撤销镜像：停一拍后控制进程仍持有输出许可。
- 许可镜像在租约到期后仍按心跳续期：现在镜像随租约一起被显式撤销。
- 许可清除复用当前 generation，被镜像当作重放拒绝：清除改为自带递增序号，
  同一租约内的第二次急停/清除循环不再永久卡死。
- 急停时状态文件在驱动实际停发之前就被写出，读者会看到“已锁但仍在输出”的半状态。
- `on_hello` 不持控制锁，与 tick 线程并发改写准入/窗口/暂停标志。
- 帧传输的会话 epoch 在 HTTP 侧是整数、在帧侧是字符串，身份比较判为不同：
  按渲染值比较，仍是同一身份。
- 离线 profile 曾把模拟读数标成 `measured_servo_position_readback`、把 action 标成
  `gateway_sent_target_after_clamp`、图像键叫 `wrist_real`：现改为明确的模拟标注。
- 启动器把容器状态文件留在原地，崩溃后的旧计数被读成“栈还活着”：改为轮转保留 `.prev`。
- 租约标记由容器内 root 写出后宿主用户读不到：会话标记改由上位侧写入并保持 0600。
- 会话驱动结束录制后不归还控制权，下一次 `arm` 直接 409 `control_held`：现在
  `hybrid_session.py` 收尾必定 `/hybrid/stop` + `/control/release`，并核验镜像真的暗下去。
- `disarm` 在“已经没有任何租约”的状态下会抛错退出 1：暂停步骤现在把“无 lease/无许可/
  已锁”都视为本就缴械，命令可重复执行。
- 急停后的板端计数被读成“多了一条”：读板端状态改为等待两次一致读数（静默点），
  不再用固定 sleep 猜。

## 6. 数据在哪里

| 内容 | 路径 |
| --- | --- |
| 车端会话（真实 bag、events、manifest、READY） | `work/ros2_collection/local_humble/docker/sessions/ros_20261105T185501Z_3aa0f300/` |
| 归档 tar | `work/ros2_collection/local_humble/docker/evidence/agent-session-archive.tar.gz` |
| 上位侧已同步会话 | `work/ros2_collection/local_humble/upper6d/upper/sessions/…` |
| 导出数据集 | `work/ros2_collection/local_humble/lerobot_offline/lelab/ros-mobile-20261105t185501z-3aa0f300` |
| 本轮全部报告 | `docs/evidence/landerpi-agent-wiring-2026-11-05/reports/` |

派生数据一律 `data_identity=simulated_offline`、`training_eligible=false`；
普通 `/export` 入口对这批会话仍然拒绝（`test_the_offline_contract_is_the_only_entry…`），
训练侧不得混入。

## 7. 收尾状态

容器保留运行且处于缴械状态：无租约、无 epoch 绑定、无输出权限
（`bin/docker up`/`record` 结束时都会 `disarm`）。要彻底停止用 `bin/docker down`，
它只处理标签归属核验通过的实例，并保留挂载的数据与证据。
