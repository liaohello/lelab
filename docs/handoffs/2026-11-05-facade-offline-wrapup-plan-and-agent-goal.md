# FrameLink 离线开发收尾、后续计划与 Agent Goal Prompt

用户当地日期：2026-11-05（America/Los_Angeles）。运行证据按 UTC 命名，因此部分路径含 20261106。

## 1. 交接结论

上位机生产 FrameStream facade、生产车端 agent、真实 ROS2 sqlite3 录包、同步、受限 6D 导出和本地 LeRobot SDK 已在模拟设备环境闭环。实际浏览器已点击两集 observe/grasp、对齐、接合、阶段暂停和结束；这次网页产生的数据为 **2 集 / 410 帧 / state 6 / action 6**，导出 `failed=[]`，始终 `data_identity=simulated_offline`、`training_eligible=false`。

不能据此宣称所有非真机开发完成。网页会话后续再次出现 `lease_lost;stop_failed:HardwareCompositionError`；失效发生时的精确原因未定位，可能涉及操作员在场心跳终止、缓存时效或其他失效路径，不能仅凭最后状态归因为某一个。车端当时已无活动租约且输出关闭。网页截图未在目标目录找到，故没有持久截图证据；数据查看器逐轴读回、故障交互、三链路基准、ARM 构建和候选包更新仍待完成。

## 2. 本轮源码成果

1. `app_ros_mobile/lelab/hybrid_stream_hardware.py`：显式整帧传输组合；后台观察缓存、单个映射推进线程、同一新样本最多一个六轴帧、窗口/准入超时、停止清队列。重新接合先重新绑定相同租约身份的 epoch、清旧队列、等待新的完整反馈。默认硬件传输仍由 `hybrid_hardware.py` 保持 legacy_http，显式配置才选 frame_stream_v1。
2. `hybrid_recording.py`：仅对明确模拟离线 profile 允许 offline_6d；分别保存控制会话 ID 和 agent 返回的 `car_recording_id`。FrameStream 阶段切换先停止并等待车端输出关闭、队列为空及后续 tick，再等待车端控制进程确认新 episode/phase/clock/bundle 上下文；之后必须重新对齐和显式接合。
3. `agent_licence_bridge.py`：读取控制进程状态，公开过滤后的 `frame_process` 确认信息、上下文及年龄；不复制许可中的 token digest。文件内容与 mtime 从同一文件句柄读取。
4. `ros_mobile.py`：显式注入 `offline_agent_recording=True` 的模拟 facade 可经生产 HTTP 录制控制器访问限定 loopback、限定 profile 的生产 agent；使用当前 facade 的车端租约，而非再取另一套 bridge 租约。默认内存仿真和普通硬件路径仍分别存在。
5. `HybridTeleopPanel.tsx`：展示帧模式下阶段切换需要重新接合；增加操作员在场心跳，实时关节采样与执行仍在服务端/车端。
6. `hybrid_session.py`：acquire/heartbeat/release/clear-estop 在返回含录制状态的 status 前释放 facade 锁，修复心跳持有 facade 锁同时等待 recorder 锁造成的锁顺序反转；有并发回归用例。
7. 离线工具：`facade_smoke.py`、`facade_record.py`、`facade_web.py`；包清单补入缺失模块及 ROS resource。`facade_web.py` 是生产 React dist + 生产 hybrid routes + 模拟设备边界的最小 harness，不是完整 `lelab.server`。其 SPA 兜底会对未实现 API 返回 HTML，此行为尚需收紧，不能把这些 HTTP 200 当作 API 通过。

本轮没有提交、推送或切换 current；HEAD 为 `e4312f05eea4f319d7402d570f5b3fd073b04af5`。工作区包含大量接管前未提交/未跟踪文件，不能用整个 git diff 当作本轮差异。14 个相关文件的当前哈希在证据目录的 `handoff-source-manifest.json`，它是交接快照，不是完整可部署清单。

## 3. 本次实测与证据

证据根目录：`work/ros2_collection/facade-integration/local_humble/facade-smoke/`。

| 项目 | 本次结果 | 证据 |
| --- | --- | --- |
| 相关控制/录制/桥接回归 | 51 passed，Python 3.12；含新增锁顺序回归 | `pytest-phase-final.log` |
| 正式前端构建 | Vite 8.0.16 exit 0，产物 JS `index-D-Ly_1hS.js` | `app_ros_mobile/frontend/dist/` |
| 无固定阶段等待的脚本闭环 | 阶段确认屏障生效，两集受限 6D 导出 57 帧，failed=[] | `sync-export-barrier.json` |
| 实际浏览器生产路由录制 | stopped，两集都含 observe/grasp | `web-recording-status.json` |
| 网页数据校验同步/导出 | 7 文件校验，203 + 207 = 410 帧，failed=[] | `web-sync-export.json`、`web-sync-export.log` |
| 本地 LeRobot SDK | 410 帧、2 集、state/action 均 6 维 | `web-sdk-readback.json`、`web-sdk-readback.log` |
| 收尾释放 | facade disconnected、身份清空、outputs=false；已过期 token 导致 release_warning 被保留 | `handoff-cleanup.json` |
| 源码与仓库快照 | 14 文件 SHA256、未提交修改清单 | `handoff-source-manifest.json`、`final-git-status.txt` |

SDK 运行时 TorchCodec 无法加载，按现有实现回退到 PyAV 后成功读取；未安装或替换全局依赖。原先阶段错标拒绝和未完成 session 都保留在同一运行目录及 `docker/sessions/`，没有自动删除。

真实浏览器操作通过 cua 的本地浏览器完成；目标截图文件不存在，所以不交付为“有截图验收”。未执行这批新数据的数据页曲线逐轴对照，也未用完整生产 server 跑上述所有功能。普通 `/export` 未因本轮 6D 模拟接线被放宽。

## 4. 收尾状态和复跑入口

- 只停止本轮独立 `facade_web.py` 进程和经标签确认属于本任务的 `lelab-humble-facade-integration` 容器。
- 容器域 94，stream 18123，agent 18131，网页 18288；本轮收尾时已 down，挂载数据保留。
- 原 `lelab-humble-dev`（域 93 / 18122 / 18130）本次最后查询无活动租约、frame_outputs=false，未停止。
- 本轮未连接或操作真实小车、SO101、串口或真实相机；不沿用更早的硬件状态作为现在事实。
- 上一轮 `frame-facade-offline-candidate` 部署包早于本轮源码变化，已经过时，禁止直接部署。

重新启动隔离栈，先在仓库根目录设置：

```bash
export LROS_WORK_DIR=/home/liaozhengyan/Desktop/lelab/work/ros2_collection/facade-integration/local_humble
export LROS_CONTAINER=lelab-humble-facade-integration
export LROS_DOCKER_DOMAIN_ID=94 LROS_DOCKER_STREAM_PORT=18123 LROS_DOCKER_AGENT_PORT=18131
export LROS_DOCKER_BUNDLE="$(app_ros_mobile/.venv/bin/python tools/ros2_collection/local_ros2/offline/facade_smoke.py --prepare)"
sg docker -c 'tools/ros2_collection/local_ros2/bin/docker up'
app_ros_mobile/.venv/bin/python tools/ros2_collection/local_ros2/offline/facade_record.py
# 生产前端 + hybrid 路由最小 harness（当前还需完善的入口）
LROS_WEB_PORT=18288 app_ros_mobile/.venv/bin/python tools/ros2_collection/local_ros2/offline/facade_web.py
# 退出网页服务并确认释放后，仅关闭本实例
sg docker -c 'tools/ros2_collection/local_ros2/bin/docker down'
```

必须复用已冻结模拟标定，不在栈启动后重生成 bundle。端口冲突或容器标签不匹配时应拒绝，不能接管别的实例。

## 5. 下一轮仅离线开发计划

按依赖顺序推进，每项完成即固化证据，不先重复全量测试。

1. **P0：稳定性和明确停止语义。** 定位网页会话后续 lease_lost；记录 heartbeat 发起/完成、两租约到期、缓存年龄、状态变化和操作员在场时间，区分正常浏览器失联与异常丢租约。确认并发心跳/阶段/结束/急停/状态不会再锁反转；阶段确认失败必须保持停发、阻止接合。避免在录制阶段锁内等长请求阻塞急停。补查重新绑定 epoch 与旧 socket/worker 迟到的代际保护。保留 500ms 反馈和 200ms 帧 TTL，不放宽时效掩盖故障。
2. **P1：网页完整闭环与数据回放。** 完善可复现的离线 Web 启动器；使用完整生产 server，或严格限定并清楚标识最小 harness 的 API，未知 API 返回真实 404。网页提供明确受限模拟 6D 导出入口，普通导出/训练继续拒绝。点击完成两集 observe/grasp、暂停、同步、受限导出、取消、错误和恢复；SDK 与 DatasetViewer 按 episode/phase 对照每轴数值。持久保存真实浏览器截图和请求/结果，补图像冻结、断网、JPEG 同帧绑定核验；曲线按真实 meta 显示关节名与单位。
3. **P2：必要故障矩阵。** 在同一生产 facade 会话注入 Reader停更、反馈/状态过期、许可撤回、流断开/背压/半开、agent退出、重复急停恢复、慢录包。核对本地产出停止、车端不再接纳/发布、队列清空、恢复不自动接合；逐层确认，不声称物理停车。
4. **P3：性能基准与有界优化。** 先测生产 facade 异步帧与同协议同步整帧，记录实际 socket写出、准入、发布时间，避免把轮询延迟称RTT。真实 legacy HTTP 若离线 profile无法运行则记 N/A，不以 frame+ping 冒充。先短程筛查，再对正常负载做 30s×2 定向比较；有异常才扩大。保留原始帧级数据，统一输入、相机、反馈、限幅与 TTL。定位后仅调调度/相位/窗口发放节奏，不能扩安全预算。9–10Hz 与 p95/p99 目标要以实测回答，不提前宣称达标。
5. **P4：构建和交付。** 补上位机/车端 Python 3.10/3.12 受影响契约回归、前端正式构建和类型检查；独立裁决历史 torque guard 与其他已知失败，不通过删断言掩盖。用隔离工具链尝试 ARM64 包/固件交叉构建，仅构建不烧录，工具链缺失或厂商工程无法构建时给具体错误。重建包含全部未跟踪生产依赖的版本化候选包，核验哈希、导入、启动/停止/回退 dry-run，更新手册和剩余真机门禁。

不在本轮范围：真车/串口/相机/手柄接入、固件烧录、实际运动/夹爪探边、物理保持与急停验收、训练或自主推理、推送/提交/切 current。最终不要承诺“只差真机”除非上述离线缺口均有结果；外部工具链缺口需明确列出。

## 6. 可直接粘贴的 Agent Goal Prompt

```text
在 /home/liaozhengyan/Desktop/lelab 继续完成 LanderPi SO101 主臂→从臂/夹爪的全部非真机开发收尾。

先读 AGENTS.md、docs/handoffs/2026-11-05-facade-offline-wrapup-plan-and-agent-goal.md、docs/handoffs/2026-11-06-facade-framelink-progress.md 和原 FrameLink implementation-plan。核验 app/current 链接、git 未提交差异和实际运行实例。当前 HEAD 历史记录为 e4312f0，存在大量未跟踪生产源码；不覆盖、不自动清理、不提交推送、不切 current。工作日期使用用户时区 America/Los_Angeles；证据保留 UTC。

目标：在模拟设备边界下，将生产网页→同一 Coordinator/FrameStream facade→生产 agent/控制进程→真实 sqlite3 两集 observe/grasp→校验同步→受限 6D 导出→本地 LeRobot SDK 和 DatasetViewer 读回稳定闭合，并完成必要故障验证、真实链路性能基准、离线构建和可回退候选包。底盘锁定；不开展训练或自主推理。保持 simulated_offline、training_eligible=false；普通 /export 和旧只读 session 身份不变。

已知当前事实（接管后仍需按范围核验）：
- FrameLink 已显式接入上位机生产 facade，legacy_http 默认保留；背景缓存与控制 worker 分离，完整六轴一次提交。
- 生产录制控制器支持显式 offline_6d、保存 car_recording_id；模拟网页到生产 agent 的路径仅在 facade.offline_agent_recording=True、loopback、simulated=true、profile=hardware_hybrid_manual_v1_offline6d 时启用，使用同一车端 lease token。
- 阶段切换先确认车端输出关闭/队列为空/后续tick，再确认控制进程中的 recording上下文，不依靠固定等待；完成后必须重新对齐显式接合。
- 已修复心跳在 facade锁内读取 recorder状态造成的锁顺序反转，相关定向测试 51 passed；正式 Vite 构建 exit0。不要把历史数量写成本轮实测。
- 真实浏览器点击生产 React dist + 最小 hybrid 路由 harness 完成两集；session ros_20261106T043048Z_3df3e236 已同步校验7文件并导出410帧，SDK读回2集/6D state/6D action；证据在 work/ros2_collection/facade-integration/local_humble/facade-smoke/{web-recording-status.json,web-sync-export.json,web-sdk-readback.json}。
- 该数据没有完成新一轮 DatasetViewer逐轴页面对照；目标截图不存在，不能称已有持久截图证据。
- 收尾仍观察到网页 lease_lost;stop_failed，具体原因未定位。车端已缴械；释放后 facade disconnected，过期token清理警告保存在 handoff-cleanup.json。
- 本轮独立 facade_web 服务和 lelab-humble-facade-integration 容器已经停；原域93离线容器未停。禁止操作真实小车或主臂。

按交接第5节 P0→P4 实施。优先定位租约稳定性、确认屏障失败和并发急停；然后补网页闭环/数据页/错误取消/截图证据，再做必要故障和性能，最后构建与候选包。facade_web.py 当前是最小 harness，未知 API 可能被 SPA兜底为HTML 200，必须收紧或使用审查过副作用的完整生产server，不把200当功能验收。

主智能体统一修改和验证；可用最多3个 default 只读子agent（遵守AGENTS目标配置与实际工具限制）做独立事实核查，不得修改、运行写入测试或操作硬件，不创建用户侧新聊天。避免并行写同名文件。

用户要求加快开发：只跑与当前变更和风险相关的现有测试；不重复全量测试，不扩建低价值测试体系。关键身份、停止/恢复、时效、真实录包/导出和实际网页点击不可省略。只在失败、代码变更或具体未解风险需要时复测。

性能必须测真实路径：同协议同步整帧、生产facade异步整帧；真实legacy无法离线运行就记N/A，shape模拟单列。保存原始时序，区分input/mapped/queued/socket-written/admitted/published/hold；不将发布当执行，不比较跨主机monotonic。不得放宽反馈500ms、帧TTL200ms或限位来提高频率。

所有安装构建仅在隔离环境；ARM64和固件只构建不烧录。只管理标签与/proc身份核验属于本任务的进程，禁止泛用pkill；保留原始/invalid sessions与失败报告。候选包已有版本过时，重建并验证全部未跟踪依赖、哈希及dry-run后才能交付为候选。

完成后交付：准确源码增量/manifest、每项实测命令与退出码、真实浏览器证据、两集数据与SDK/页面读回、故障矩阵、真实/shape/N/A分开的性能报告、启动/停止/回退手册、剩余工具链/真机门禁。结束时撤销许可、释放Reader、停止本任务worker和测试服务，保留数据。明确已实现/本轮实测/未验证，不把模拟冒充实机，也不因为预算或收尾需要把未完成项标成完成。
```

## 7. P0 已闭合（2026-11-06 UTC 实测，详见 docs/evidence/landerpi-facade-p0-lease-stability-2026-11-05/README.md）

网页 `lease_lost;stop_failed:HardwareCompositionError` 的原因已定位到具体锁顺序，不再需要按最后状态猜测：车端 `recorder.stop_session()` 在持有 recorder 锁的情况下等待录包进程（最长 20 s）并逐文件哈希；`MobileAgent.status()` 又在 agent 主锁内调用它，于是 `/status`、`/control/renew`、`/hybrid/stop` 一起被排到录包收尾之后，上位机 4 s 超时抛 `HardwareCompositionError`；同时观测缓存年龄越过 0.5 s，`FrameCarControlLease.valid()` 旧实现只看缓存即判 False，`_refresh()` 一律写成 `lease_lost`。此时租约其实还在。此后 fault 使网页心跳 effect 永久停发（它只在 `state === "ready"` 运行，且只允许一个未完成请求），30 s 后租约才真正过期，收尾 403 是这个过期的后果。

修复与验证：`/status` 改用新的 `recorder.status_snapshot()`（锁忙即以 `recording_status_cached` 标记返回上次快照），不再在 agent 锁内等 recorder 锁；`valid()` 拆分“租约是否仍在车端”与“观测是否够新”，缓存过期时按 1 s 超时、每 0.5 s 新鲜窗口最多一次向车端直读，并给出 `car_control_inactive`/`car_session_mismatch`/`car_status_read_failed:*` 等原因；`lease_lost` 现在带来源，`stop_failed`/`heartbeat_failed` 保留异常文本；新增 heartbeat/两租约 TTL/缓存年龄/状态迁移/操作员在场年龄诊断。反馈 500 ms 与帧 TTL 200 ms 未放宽，读不到车端仍判 False（fail-closed）。

隔离实例实测：真实录包收尾 2.103 s 内 105 次 `/status` 轮询 p95 1.9 ms、max 2.1 ms、超时 0，其中 23 次由缓存快照应答（证明确实跨过了持锁窗口），4 次心跳续租全部成功，会话保持 `ready`、`last_error=null`；回归为上位机 116 passed（3.12）、车端 101 passed/17 skipped（Humble 3.10）。

仍未做：本轮未做真实浏览器点击与页面读回（P1）、故障矩阵（P2）、三链路基准（P3）、构建与候选包（P4）；“重新接合后旧 socket 迟到帧被拒”只在车端 `teleop_frame_v1` 单元层有 `stream_epoch_mismatch` 用例，在线核验留到 P2。改前的秒级阻塞没有真机/旧实例对照数字，改前形状由持锁计时用例复现，不是猜测。
