# LanderPi 本地 ROS 2 离线开发 — 本轮状态与门禁

日期：2026-11-05。上位机：Ubuntu 24.04.4 / x86_64，无 docker、无 sudo、无 `/opt/ros`。
本阶段**未**连车、**未**接真实串口或相机、**未**发送真实动作、**未**部署到 Pi。
本轮所有输出数据身份为 `simulated_offline`，`training_eligible=false`。

## 1. 结论先说

> 本节是 rootfs 阶段的历史摘要。当前结论见 §7（Docker 成为运行路径）与 §8（任务 #9：
> 生产 agent 授权 + 录制 + 6D 闭环已现场闭合）；下面保留当年的失败与首次修复证据，
> 不与 §8 的现况混用。

打通了一件此前被判定为"本机做不到"的事：**真实 ROS 2 Humble / Python 3.10.12 运行时**与
**真实 `ros2 bag record`（sqlite3）**。合帧协议、独立 ROS 控制进程、帧流、租约镜像 IPC 都
在该运行时里跑通并留下了可复跑证据；录制之后的**同步/6D 导出/SDK 与页面读回**仍未闭合，
`bin/accept` 的"重挂租约→跟随"这一步目前回归失败（见 §5）。

| 门禁 | 结论 |
| --- | --- |
| Humble 运行时可用（Python 3.10.12、rclpy、ros2 CLI） | 通过（实测） |
| vendor 消息在 Humble 从源码构建并导入 | 通过（实测） |
| 真实 `ros2 bag record` 落 sqlite3 并 `ros2 bag info` 读回 | 通过（实测：38 条消息，storage id=sqlite3） |
| Docker 引擎（docker.io 29.1.3，digest 固定基础镜像） | 通过（用户授权后安装；见 §7） |
| 合帧协议纯逻辑（validate-then-commit、窗口、latest-wins） | 通过（65 项测试，3.12 与 3.10.12 各跑一次） |
| 独立控制进程 + 帧流 + 租约镜像 + 六轴一次发布 | 通过（实测跟随：148/149 帧准入，135 次发布，有效 8.88 Hz） |
| 预览零输出 | 通过（实测：板卡输出计数不变、`publish_count=0`） |
| 故障矩阵（坏帧/过期窗口/TTL 上限/急停/租约失效） | 大部分通过，1 项被 §5 的夹具问题遮蔽 |
| 租约重挂后的跟随（rootfs `bin/accept`） | 失败（历史记录，根因见 §7）；Docker 路径按阶段显式 arm，已通过 |
| 三段链路基准（legacy 形状 / 同步单帧 / 帧流） | **未完成**（`bench_links.py` 首帧即失败，见 §5） |
| 录制后的校验同步、6D 导出、LeRobot SDK、页面读回 | **未开始**（需要生产 agent 进程在运行时内提供 `/sessions/*/archive`） |
| 前端展示目标 Hz / 反馈年龄 / 拒绝原因 | **未做** |
| 断网复跑 | **未验证**（`--offline` 只跳过 apt 步骤；apt 缓存已 clean，全新机器仍需联网） |
| arm64 构建 | **未执行**（本轮只有 amd64 证据） |

## 2. 环境与隔离（实测确定，勿再试错）

入口：`tools/ros2_collection/local_ros2/bin/{doctor,prepare,build,up,accept,record,down,package}`
运行产物：`work/ros2_collection/local_humble/`。细节与踩坑记录见
[tools/ros2_collection/local_ros2/README.md](file:///home/liaozhengyan/Desktop/lelab/tools/ros2_collection/local_ros2/README.md)。

要点：`bwrap --dev /dev … -- unshare --map-root-user --mount`（两层缺一不可）；不开 PID
namespace 以便记录宿主机可见 PID；`/dev` 只逐个绑定 null/zero/full/random/urandom/tty/ptmx；
`/repo`、`/vendor` 只读绑定并做写探针；**运行时内不安装 pyserial**，真实总线在结构上不可达；
`ROS_DOMAIN_ID=92`、`ROS_LOCALHOST_ONLY=1`、CycloneDDS；所有 topic 重映射到 `/lelab_offline/`。

`Docker Hub` 不可达（含本地代理 CONNECT 失败），因此 `ros:humble-ros-base-jammy` 的
registry digest **未取得**，`lock/env.lock.json` 记为 `NOT_VERIFIED`；可追溯性改用
Ubuntu base rootfs SHA256 `df6fe77cee11…`（与官方 `SHA256SUMS` 核对）与
`lock/installed-deb-packages.txt`（665 行）、`lock/installed-ros-packages.txt`（195 行）。

## 3. 本轮源码差异

新增（本会话）：

- [app_ros_mobile/lelab/teleop_frame_v1.py](file:///home/liaozhengyan/Desktop/lelab/app_ros_mobile/lelab/teleop_frame_v1.py)
  合帧事实源；车侧为**同一文件的符号链接**
  [ros2_ws/…/teleop_frame_v1.py](file:///home/liaozhengyan/Desktop/lelab/ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/teleop_frame_v1.py)，
  有测试断言两者 SHA256 相同（防漂移）。
- [app_ros_mobile/lelab/teleop_stream.py](file:///home/liaozhengyan/Desktop/lelab/app_ros_mobile/lelab/teleop_stream.py)
  长度前缀 JSON 帧流（回环绑定、租约令牌摘要鉴权、单连接换代、带缓冲读取、非阻塞发送）。
- [app_ros_mobile/lelab/hybrid_frame_link.py](file:///home/liaozhengyan/Desktop/lelab/app_ros_mobile/lelab/hybrid_frame_link.py)
  上位机侧：一个快照一帧 + 带年龄的后台状态缓存。
- [ros2_ws/…/car_control_process.py](file:///home/liaozhengyan/Desktop/lelab/ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/car_control_process.py)
  独立控制进程（10 Hz tick、窗口发放、租约镜像校验、六轴一次发布、录制行、状态快照）。
- [ros2_ws/…/licence_ipc.py](file:///home/liaozhengyan/Desktop/lelab/ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/licence_ipc.py)
  agent↔控制进程的本地授权镜像（心跳 100 ms / 超时 300 ms / EOF 即撤销）。
- [ros2_ws/…/config/hardware-hybrid-manual-v1-offline6d.json](file:///home/liaozhengyan/Desktop/lelab/ros2_ws/src/lelab_mobile_agent/config/hardware-hybrid-manual-v1-offline6d.json)
  离线 6D 录制 profile：真实回读 topic 进入 `required_topics`，`raw_storage=sqlite3`，
  `/controller_manager/joint_states` 降为仅记录（不当作 measured）。
- `tools/ros2_collection/local_ros2/`（工具链 + `offline/` 模拟夹具）与
  `tools/ros2_collection/tests/{test_teleop_frame_v1,test_teleop_stream,test_hybrid_frame_link}.py`。

修改（本会话，均为其它会话留下的未跟踪文件）：

- [ros2_ws/…/ros_servo_output.py](file:///home/liaozhengyan/Desktop/lelab/ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/ros_servo_output.py)
  加 `submit_frame()`（一次消息含 ids 1..5+10、共同 duration，先全量校验后发布）、
  `check_only()`（纯限位检查）与 `command_topic`（避免记录里出现未重映射的真实 topic）。
  sha256 前缀 `fa1d9c66f3c01a34`。

**未改**：`agent.py`、`hybrid_hardware.py`、`hybrid_channel.py`、发布快照与 `current`。
上位机 facade 的每周期 3 次 HTTP 调用（`hybrid_hardware.py:814-888`）**仍是 legacy 形状**：
`FrameLink` 已实现并通过测试，但把它接进 facade 属于会改变真实控制路径的动作，
在没有小车可验证的情况下不做静默替换——这是一个显式待办门禁，不是遗漏。

其它会话留下的未提交改动全部保留：仓库仍有 28 个 tracked 修改与大量未跟踪文件，
本轮未提交、未推送、未切 `current`。

## 4. 已实测的证据

- 测试：新模块 65 项在 Python 3.12.16 与运行时 Python 3.10.12 各全通过。
- 全量 `tools/ros2_collection/tests`（`ros_jazzy` 解释器，仓库文档规定的命令）：
  **575 passed, 7 failed**；其中 6 项属于本会话未触碰的文件
  （`test_car_arm_stack_script.py` 5 项对应会话前已被改动的 `start_car_arm_stack.sh`，
  `test_integration_car_phase.py` 1 项、`test_agent_exclusivity_guard.py` 1 项对应会话前已改动的
  `agent.py`），已用 `grep`/`git status` 确认这些测试不引用本会话改动的模块。
- 真实录包：`evidence/ros_smoke/`（`ros2 bag info` 输出：storage id sqlite3、Duration 3.72s、
  Messages 38、topic `/lelab_offline/smoke/chatter`）。
- vendor 消息构建：`evidence/vendor-msgs-manifest.json`（21 文件逐文件 SHA256，
  combined `4e0527433c50c1b3…`）、`evidence/servos-position-interface.txt`、
  `evidence/interface-probe.txt`（`MSG_IMPORT_OK ['duration','position']`）。
- 跟随闭环（一次成功运行的记录，`evidence/acceptance/follow-*.json`）：
  提交 149、准入 148、控制进程发布 135、板卡收到 137 条 `ServosPosition`、
  每条恰好 6 个位置、最后一条 ids=`[1,2,3,4,5,10]`、有效 8.88 Hz（目标 10 Hz）、
  反馈最大年龄 178.5 ms（门限 500 ms）、往返 p50 100.2 ms / p95 100.5 ms。
- 预览零输出、坏帧/过期窗口/TTL 上限整帧拒绝且**不消耗任何序号**、
  急停闭锁后板卡输出不再增长且后续帧被 `fault_latched:estop` 拒绝、
  租约发放器被 SIGKILL 后控制进程在桥接超时内失效（不等 30 s 会话 TTL）。
- 部署候选包：`work/ros2_collection/local_humble/dist/landerpi-frame-20261105T134608Z.tar.gz`
  （sha256 `813e61c86a47…`）与同名 `.manifest.json`（逐文件哈希 +
  `arm64` 未构建声明；离线夹具在排除清单内）。

## 5. 当前失败的门禁与首次失败证据

`bin/accept` 最后一轮第 5 步失败，症状是跟随阶段 0/119 准入、
拒绝理由 `arm: ValueError: licence_outputs_disabled`。取到的现场：

- 租约发放器 `state/licence_agent_status.json`：`outputs_enabled=true`、
  `control_generation=11`、`sent=0`、`state="connecting"` —— 它连不上
  `state/licence.sock`；
- 控制进程 `state/car_control_status.json`：镜像 `received=1118, replaced=0`
  仍停在旧 generation，`latched_fault="estop"`。

也就是说这是**验收夹具的重启/换代路径**缺陷（租约重启后新 generation 未到达镜像，
急停闭锁也未随之解除），不是控制逻辑本身——同一份跟随断言在上一轮完整通过（§4）。
`bin/down` 会清 `licence.sock`，`bin/up` 起新一轮；本轮停止前未再验证修复。

`bench_links.py` 在同一失败状态下起步即退出（`BENCH_EXIT=1`），因此三种链路对比
**本轮没有数据**，不能给出"帧流优于 legacy 形状"的实测结论。

真实 recorder 侧：`bin/record` 通过生产 `SessionRecorder` 生成了 session 目录，
但后续 `rosbag2_py` 读回与 READY 校验在这一轮同样受上面的准入失败影响，
6D 导出/SDK/页面读回**未执行**（需要先让生产 agent 进程在运行时内跑起来，
它才提供 `/sessions/<id>/archive` 与同步入口）。

## 6. 最小下一步

1. 修 `bin/accept` 的租约重启路径：让控制进程镜像可靠收到新 generation，
   并让 §5 的急停闭锁由 agent 侧显式解除（当前依赖 generation 递增这一模型条件）。
2. 在 Humble 运行时里跑生产 `agent.py`（只读+录制路由），把 `bin/record` 变成
   "agent 拥有 SessionRecorder + 控制进程拥有输出"的真实形状；随后接
   `sync_session`/`export_session`、LeRobot SDK 与 `DatasetViewer` 读回。
3. 完成三种链路基准（`--seconds 60 --repeats 3`）与故障矩阵的剩余项
   （TCP 半开/粘包注入、慢磁盘、反馈过期、录包收尾很慢）。
4. 把 `FrameLink` 接进 `hybrid_hardware.py` 的 worker，删除每周期
   `GET /status` + 两次 `POST /hybrid/apply`；这一步必须在有小车或至少有
   真实 agent 端点时才切换默认路径。
5. 前端：目标 Hz / 反馈年龄 / 拒绝原因 / 许可与离合展示 + 真实浏览器点击验收。
6. 真机门禁（本阶段一律未做，不可由 P1–P5 代替）：Pi 只读身份与容器核验 →
   vendor arm64 构建 → 唯一板卡 owner 与六路 ID → 无输出预览（真实输出计数不得增长）→
   逐轴低速授权 → 真急停/hold/失联 → 两集 observe/grasp 实录。
   另外仍需独立验收：200 ms 迟到回包、固件 watchdog、控制进程 SIGKILL/Pi 掉电的机械停止。

## 7. 同日更新：Docker 容器路径成为当前结论

用户确认本主机也用 docker 后，实测发现**这台工作站原本并没有 docker**（无 CLI、无 socket、无
`/var/lib/docker`、无 systemd 单元、dpkg/snap/flatpak 均无记录）。经授权后安装
`docker.io 29.1.3` 并配置 daemon 代理（Docker Hub 直连超时，仅 `127.0.0.1:7890` 可达），
随后 `usermod -aG docker`，之后不再需要 sudo。代理 drop-in 只含代理地址，无凭据。

镜像按 digest 固定，不靠 tag：`ros:humble-ros-base-jammy@sha256:a3f4de78e42cca693a543ece5798aedcf1cae0171fe71c334a047332cc72ef93`
（amd64 平台清单；按 tag 拉取时 docker 报的是 index digest `sha256:d9bcf2e6…`，两者不同，已写入
`local_ros2/lock/image.lock.json`；arm64 清单 `sha256:48435c01f287…` 留给上机核对）。
容器内实测 Ubuntu 22.04.5 / Python 3.10.12 / ROS_DISTRO=humble，`ros-base` 自带 rosbag2。

入口：`tools/ros2_collection/local_ros2/bin/docker doctor|build|up|arm|accept|disarm|down`
（镜像 `lelab/humble-dev:local` 只是环境层；代码、vendor、构建产物运行时挂载）。
容器形状：host 网络 + Humble/3.10，但**不 privileged、不映射 /dev、`cap_drop ALL` + 最小 cap、
`no-new-privileges`、仓库只读、镜像内无 pyserial**；`ROS_DOMAIN_ID=93`、帧端口 127.0.0.1:18122，
与 rootfs 运行时（92/18121）并存不冲突。

实测（`work/ros2_collection/local_humble/docker/evidence/`，容器重建后复跑，全绿）：

- 容器内契约/流/帧链路测试 **65 passed**（Python 3.10.12）。
- preview **4/4**：49 帧到达，板卡输出 `0 → 0`，控制进程 `publish_count=0`，拒绝理由点名 `licence_outputs_disabled`。
- follow **8/8**：119 提交 / 118 准入；控制发布 109、板卡收 111 条；每条 6 个位置、ids=`[1,2,3,4,5,10]`；
  有效 **8.93 Hz**（目标 10 Hz，未达的部分是发送 tick 与窗口余量，见 §8）；反馈年龄 **56.6 ms**；
  往返 p50 **100.2 ms** / p95 **100.3 ms**；准入后剩余可用寿命中位 **124.4 ms**。
- faults **10/10**：`inf` 夹爪值整帧拒绝且不消耗序号、过期窗口 `stale_window`、TTL 越限 `ttl_too_long`、
  拒绝后有效帧仍可准入、急停 `hold_published=true` 且驱动输出关闭、急停后 11 帧全部 `fault_latched:estop`
  且板卡输出停在 112 不再增长。
- 真实录包（上一轮）：`READY` + `manifest=raw_ready`，bag 内 `recording/teleop 39`、`set_position 39`、
  `servo_readback 21`、相机 `image/camera_info` 各 64。

### 本轮同时修掉的启动器缺陷（用户点名的前置项）

1. `up` 之前无条件 `docker rm -f "$NAME"`。现在按 `com.lelab.owner` / `com.lelab.instance`
   **标签值**判定归属，未打标记者一律拒绝，只有显式 `LROS_ADOPT_UNLABELLED=1` 且镜像确认是本项目时才放行。
   （第一版我用 `key=value` 子串匹配 `{{json .Config.Labels}}` 的 `{"key":"value"}` 输出，
   把自己的容器误判成外部容器；已改为按键取值。）
2. 容器内进程改由 `offline/container_ops.py` 启停：写 pid 时同时记录 `/proc/<pid>/cmdline` 与
   `stat` 起始 tick，发信号前校验二者，pid 复用或命令行不符即拒绝；按 `licence/board/control` 显式停止，
   不使用 `pkill`/按名杀。授权函数不再硬编码容器名，统一取 `$LROS_CONTAINER`。
3. 租约改为**分阶段显式 arm**（`up` 起于缴械态，`arm true|false`，`accept` 收尾自动 `disarm`），
   这正是 rootfs 路径 §5 失败的根因（重挂后新 generation 没可靠到达镜像，急停闭锁又未随之解除）。
4. 入口脚本与 `docker exec` 的引号/展开问题：`set -u` 与 ROS `setup.bash` 冲突、
   `export PYTHONPATH=...` 覆盖掉 site-packages、宿主双引号内 `${VAR}` 提前展开、
   `--arg --flag` 的 argparse 歧义、`--module` 缺 `-m`——全部修掉并复跑验证。

### 8. 关于 8.93 Hz 的实测解释

发送端每 tick 只在"窗口剩余寿命 ≥ 60 ms"时才发帧（否则会制造注定被拒的帧），
12 s 内 119 提交 / 118 准入 / 109 发布，差额集中在 tick 相位与 100 ms 周期的取整，
不是空 tick 或重复发布充数（`frames_emitted` 只统计真正发出的一帧一消息）。
补足到 9–10 Hz 需要动的变量已明确：控制 tick、窗口时长与发送相位的对齐，
在生产 agent 接通后于三链路基准里一并复验。

## 8. 任务 #9：生产 agent 成为唯一权威，录制与 6D 闭环现场闭合

日期 2026-11-05。运行位置仍是本机 Docker/Humble 容器（域 93、帧口 18122、agent 18130），
设备全部仿真；未连 Pi、未访问真实 USB、未产生真实运动。逐条证据与复跑命令见
`docs/evidence/landerpi-agent-wiring-2026-11-05/README.md`。

### 接线结果（不是单元测试推断）

- 模拟许可发放器退出生产路径：`bin/docker up` 启动生产 agent，`arm/disarm/renew/
  assert-legacy` 全部经 agent 自己的 HTTP（`/control/acquire`→`/hybrid/begin_epoch`→
  `/hybrid/enable_output`→`/hybrid/stop`）。`container_ops assert-single-issuer` 扫
  `/proc` 证明没有第二个发放者存活。
- agent 在帧传输模式下不再创建自己的 servo 输出 publisher（日志显式声明），只绑读数
  driver 用于可信反馈门；`/hybrid/apply`、`/control/arm`、`/control/gripper`、
  `/expert/run`、`/task/reset` 现场全部 409 `frame_transport_active`。
- 许可镜像由 `agent_licence_bridge` 按 100 ms 心跳发布，token 只以摘要过界，TTL 被
  夹在实际租约剩余寿命内；租约到期即显式撤销，不再靠镜像 TTL 兜底。
- 急停恢复语义收紧：只有 agent 发出的显式 `clear_estop` 消息能解锁，renew、重连、
  更高 generation 都不行；清除后仍无输出，必须重新 hello 再显式 enable。
- 录制上下文经同一 socket 的第二类消息（`recording_context`，不授予任何权限）镜像到
  控制进程，`teleop_frame` 行因此能写出 agent 自己宣布的 session/episode/phase/
  `clock_epoch`；缺上下文的行标 `recording_context=missing`，导出阶段拒绝归集。
- 时钟命名固定为两域并存：`stamp_ns`+`clock_domain=car_system_time`（与 bag、事件同域，
  现场最大偏移 0.5 ms）与 `control_mono_ns`+`control_clock_domain=car_process_monotonic`
  （本地 TTL/窗口），两者永不相减。

### 数据链

一轮 `bin/docker record`（会话 `ros_20261105T185501Z_3aa0f300`）：生产 SessionRecorder 起
真实 `ros2 bag record` 写 sqlite3，108 行 `teleop_frame`（两集各 54 行、observe/grasp 各
27 帧、`(stream_epoch, frame_seq)` 全唯一、28 行 pre-clip 与发送值不同）、相机 214 帧
640×400；`/sessions/<id>/manifest|archive` 出 808125 字节 tar；生产 `sync_session` 逐文件
SHA256 校验并原子发布；受限模拟 6D 契约导出 2 集 × 54 帧数据集；本地 LeRobot SDK 读回六维
名称/单位/来源；真实浏览器点开页面，整集与两个阶段切片、两集切换，视频 Range 206、控制台
0 错误，页面每条曲线每个采样点与数据集存储行逐轴零差异。

### 本轮现场发现并修掉的缺陷

`clear_estop` 的桥接调用曾是 `return` 之后的死代码；租约替换/过期不撤销帧模式输出标志；
`hybrid_stop` 不撤销镜像；镜像在租约到期后仍被心跳续期；清除复用 generation 被镜像判为重放
（同一租约内第二次急停会永久卡死）；急停状态文件早于驱动停发写出半状态；`on_hello` 与 tick
线程无锁竞争；HTTP 侧整数 epoch 与帧侧字符串 epoch 判为不同身份；离线 profile 把模拟读数/
动作标成 measured/gateway 且图像键沿用 `wrist_real`；启动器轮转前的旧状态文件会把崩溃读成
存活；容器内 root 写的 0600 租约标记宿主读不到；会话驱动录完不归还控制权，下一次 `arm`
直接 409 `control_held`（现在收尾必定 `/hybrid/stop` + `/control/release` 并等镜像暗下去）；
`disarm` 在“已无租约”时抛错退出 1（现在把无 lease/无许可/已锁都视为本就缴械，可重复执行）。

### 仍未通过或不在本轮

1. `test_agent_exclusivity_guard.py::test_the_deployed_agent_never_writes_torque_state` 失败，
   起因是本轮开始前已存在的 tracked 修改（follower calibration 扭矩写能力），不是 #9 接线：
   `git show HEAD` 的 agent.py 无任何 `*torque(` 调用形状。该断言与该功能相互冲突，留给
   功能作者裁决，本轮不放宽断言也不记为通过。
2. 物理停车仍不可证明（无串口）；SIGKILL/掉电场景只证明"不再接纳与发布新目标"。
3. 页面纵轴仍显示 `state[i]/action[i]` 索引而非关节名，名称/单位靠 meta 与
   `viewer-readback.json` 核对；属后续前端小改。
4. 三链路频率基准与 TTL/限幅调优不在本轮；`follow` 有效发射率 8.29–8.93 Hz 原样记录。
5. 上位机 facade 的默认传输仍未切到 `FrameLink`；下一步就是它，然后才是三链路基准、
   arm64 与真机联调。

### 收尾状态

容器保留运行且缴械：无租约、无 epoch 绑定、无输出权限；`up`/`record` 结束都会自动
`disarm`。数据与证据留在挂载目录内，`bin/docker down` 只清理标签归属核验通过的实例。
