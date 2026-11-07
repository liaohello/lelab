# 下一轮计划与 Agent Prompt：生产 agent 授权、录制和 6D 闭环

日期：2026-11-05。本文是下一轮执行任务，不是接线已完成的报告。
范围：本地 Docker/Humble 离线开发；不连 Pi、不访问真实 USB、不部署或运动。

## 1. 下一轮安排

下一轮只做 #9 及其数据验收：生产 agent 成为唯一车端租约/录制权威，向独立控制进程
发布有界许可，并通过已有归档接口打通两集 6D 数据。上位机 facade 传输替换、三链路性能优化、
arm64 和真机联调留到下一轮；本任务不要以优化频率为由调整 TTL、反馈门限或机械限位。

| 顺序 | 工作 | 门禁 |
| --- | --- | --- |
| 0 | 固定接管源码与证据，核对启动器实例身份 | 容器 label 的 owner/instance 精确匹配；禁止误管其他实例 |
| 9A | 生产 agent → LicencePublisher → 控制进程 | 不再依赖 simulated licence_agent；真实生产 API 获取/绑定/撤销许可 |
| 9B | 输出仲裁、停止与故障恢复 | stream/legacy 互斥；generation 增大或重连不能自动清急停并恢复运动 |
| 9C | 录制身份、阶段和时钟接线 | 帧/事件/反馈/manifest 有可验证关联，同一会话两集 observe/grasp |
| 9D | 真实录包、归档、同步与 6D 读回 | 生产 agent HTTP归档、生产sync/export、本地SDK及网页实际读回 |
| 9E | 回归、故障和收尾 | 相关测试与离线实跑留证，撤销许可、无新目标输出，数据保留 |

开发期间保留已有 Docker 环境，不重装 docker/ROS、不改 daemon 代理、不重拉已锁定基线。
容器保持缴械，验收阶段显式使能模拟输出；收尾先撤销许可再处理录包，保留挂载数据。
外部容器只操作经身份核验属于本任务的实例，是否保留运行写入交接。

## 2. 本次只读核查发现的具体接缝

- 生产 `agent.py` 已有 acquire/renew/release、begin_hybrid_epoch、enable_output、stop/estop、
  SessionRecorder 和 `/sessions/<id>/archive`，不需要另写归档 HTTP 夹具。
- `licence_ipc.py` 已有 LicencePublisher / LicenceMirror；当前离线 launcher 仍启动模拟发放器。
  本轮补 agent 适配器即可，不新建另一套租约服务。
- `car_control_process.py::_tick_locked` 当前会在新许可 generation 大于故障时 generation 后自动
  清除 latched_fault。正式接线必须区分“许可代际变化”“明确解除故障”“重新对齐/接合”，
  generation 递增本身不能成为恢复运动授权。
- `car_control_process.py::_record_frame` 当前记录 `car_process_monotonic`，还需与 agent/recorder
  的 episode 时间、ROS 相机时间和导出时钟规则核对，不能直接混合比较。
- `bin/docker::owned_container` 当前对 instance 标签只检查非空；需与预期值精确比较。
  有外部标签的实例不能借 `LROS_ADOPT_UNLABELLED` 被接管；显式收养仅限真正无归属标签的旧实例。
- `agent.py` 本次 `git status` 为 tracked modified，而不是未跟踪；其他新增模块大量未跟踪。
  接管、备份、打包以实际 Git 状态为准，不复制交接文字里的文件身份判断。
- offline-status 顶部仍有早期“无docker”的环境描述，最新 Docker 证据在追加章节。
  更新当前摘要时保留历史失败和首次修复证据，避免同页当前/历史结论混淆。

上面是源码接缝核对，不是运行复验。本轮未重跑用户报告的65项测试或Docker验收。

## 3. 可直接交给下一 Agent 的 Prompt

---

你在 `/home/liaozhengyan/Desktop/lelab` 实施 LanderPi 本地离线任务 #9。
目标：让生产 agent 真实持有车端控制租约、recorder 和授权镜像，接通既有独立 ROS 控制进程，
并完成同一 session 两段 observe/grasp episode 的真实 sqlite3 录制、归档同步、6D 导出、SDK和网页读回。
请实施和验证，不停留在方案；不能使用归档/租约夹具冒充生产接线。

### A. 读取、接管与修改范围

1. 先读根及子目录AGENTS、README/MIGRATION、实际app_ros_mobile的依赖与部署说明，随后完整读：
   - `/home/liaozhengyan/Desktop/lelab/docs/handoffs/2026-11-05-landerpi-local-ros2-offline-development-plan.md`
   - `/home/liaozhengyan/Desktop/lelab/docs/handoffs/2026-11-05-landerpi-local-ros2-offline-status.md`
   - `/home/liaozhengyan/Desktop/lelab/tools/ros2_collection/local_ros2/README.md`
   - `/home/liaozhengyan/Desktop/lelab/docs/handoffs/2026-11-05-landerpi-agent-wiring-plan-and-goal.md`
   Docker追加章节为当前历史证据，早期rootfs回归是另一条运行路径，不能混用结果。
2. 核对实际仓库、symlink、Git差异与文件哈希；保护全部tracked/untracked修改。
   `agent.py` 约163KB且本次状态为tracked modified，不能以重写全文件方式接线。
   对要修改的文件保存基线和差异，不使用reset/checkout覆盖，不提交、不推送、不切current。
3. 允许修改：车端agent、独立控制/IPC所需最小接缝、新增小模块、离线profile/launcher/验收工具、
   对应测试与任务专属文档。同步/6D exporter/DatasetViewer如有实际契约缺口，只做必要且有测试的调整。
   `hybrid_hardware.py`默认控制传输不在本轮切换；不重写前端、不做三链路调优、不加底盘/9D。
4. 主智能体实施和验证；按AGENTS可用最多3个default只读子agent分别核查租约/输出、录制/导出、
   launcher/测试证据。子agent不得写文件、测试构建、运行服务、访问硬件或派生子agent。
   主模型配置不能核验时如实记录，不改全局模型配置。
5. 本轮仅本地模拟设备。禁止SSH、真实串口/相机、Pi部署、刷固件、真实运动；保留既有服务和其他实例。
   复用已安装Docker与digest固定环境，不重装系统组件、不改代理或组权限，不引入凭据。

### B. 首先完成生产授权接线

1. 画出最小调用表：acquire/renew/release、epoch绑定、enable、pause/stop/estop/clear_estop、
   lease过期、agent正常退出、录制开始/结束，各自在何处改变许可。
   先核对全部旧写入口，再写一个小的agent→LicencePublisher适配模块，避免将socket循环塞进HTTP handler。
2. agent保持唯一生产车端租约权威。独立控制进程只消费许可镜像，不再次申请另一份互斥租约。
   token由生产acquire生成；不保留离线固定token、模拟licence_agent或第二个publisher作为备用权威。
   token不写日志/归档，镜像按现有协议传token摘要、session/epoch/bundle、permits、generation与短期有效期。
3. acquire/绑定epoch仅建立缴械许可；只有显式使能路径、有效两端会话条件和可信反馈满足后才允许输出。
   许可TTL不能越过实际生产租约剩余寿命，普通心跳不延长动作TTL。
   心跳初始沿用100ms/镜像超时300ms；发送有界，不持agent主锁做socket重连/等待/磁盘操作。
4. release、租约过期、session/epoch替换、stop/estop、agent退出必须撤销镜像、废弃旧窗口和待发目标。
   IPC发送失败/EOF/进程失联fail closed；agent和控制进程重启均不能沿用旧token或自动恢复运动。
   generation跨重启语义明确，拒绝旧代际；持久化/启动身份方案按既有协议最小适配并测试。
5. frame模式agent不得创建第二个可输出的servo publisher或启用旧driver。
   同一epoch中拒绝 `/hybrid/apply`、command/dispatch以及 `/control/arm`、gripper和其他旧动作入口；
   mode/expert等控制也不能绕过仲裁。legacy模式兼容行为单独验证，不自动双发或隐式回退。
   STOP/ESTOP保留高优先级，不能因为动作入口互斥而禁掉停止。
6. 修改 `car_control_process.py` 中“generation增加自动清故障”的路径：
   普通renew、连接恢复、重新发布许可或新generation均不自动解除急停/故障并恢复输出。
   明确解除故障后仍是无输出状态，清队列/旧reference，必须重新对齐和显式接合。
   不用重启进程冒充解除流程；clear_estop不得隐式设置outputs=true。
7. 正式launcher的arm/disarm应经过生产agent授权入口，不再直接启动或指挥模拟许可发放器。
   保留专门协议单测夹具，但生产接线验收明确确认它未启动、且没有第二个许可发送者。
8. 顺手补精确instance标签核验；不同instance拒绝，带外部标签不能走unlabelled收养分支。
   保留container_ops的PID+cmdline+start tick核验和只管理本实例的停止范围。

### C. 接通录制上下文和时间轴

1. 生产agent继续唯一拥有现有SessionRecorder与原归档API，不实现替代recorder或归档HTTP服务。
   独立控制进程发布记录/确认事件，agent不复制生成第二份伪动作流。
2. 先核对frame的session_id/session_epoch与agent录制session/episode/clock_epoch的关系。
   同一事实使用一致身份；确需区分control-session与recording-session则显式绑定和验证映射，
   不把不匹配字段忽略、硬编码成offline-session/epoch=1或在导出时重写原始数据。
3. 将agent的start_session/start_episode/set_phase/end_episode/stop_session上下文有界地传到控制记录端。
   缺上下文不进入该episode；两集建议放同一个session，均有observe/grasp。
   不为满足状态机硬插无实际内容的drive阶段；底盘继续锁定，成功标注manual/unknown。
4. deadline/TTL使用Pi进程本地monotonic；ROS图像、phase事件、feedback和recorded row采用可比较的
   车端采集时钟与clock_epoch。核对当前car_process_monotonic记录和agent/recorder时间域差异，
   明确转换/采集策略、boot/clock epoch与跳时行为，禁止直接比较不同时间域或用一项常量掩盖跳时。
5. 记录完整leader raw/标定值、pre_clip、限幅后sent_target、frame/channel/sample序号、
   实测或模拟反馈身份/年龄/扫描跨度、离合/版本/拒绝/停止事件。缺数据明确拒绝导出，不能填零或复制目标。
   `driver_sent`、控制器接受和实际执行分别记录；没有证据继续unavailable。
6. end_episode默认先暂停跟随并确认停止接纳/发布新目标，再保存；暂停失败不能保存为成功有效集。
   下一集显式重新接合。急停不等待recorder、归档或磁盘，慢保存不持控制锁。
   不改变旧只读session不可训练身份；保留invalid/discarded/中断数据和首次失败日志。

### D. 完成实际数据验收，不绕过导出门禁

1. 用本地生产agent HTTP接口真实运行：获取租约→绑定epoch→预览→显式模拟跟随→
   start_session→两段start_episode/observe/grasp/end_episode→stop_session。
   输入可以是明确simulated的SO101样本，控制/录制/归档链路必须是生产实现。
2. 通过生产SessionRecorder启动真实 `ros2 bag record`，固定sqlite3。
   核对required topics、phase/event、相机640×400、真实落盘退出码、READY和manifest；不FakeBagProcess/sleep。
3. 通过既有 `/sessions/<id>/manifest`、`/sessions/<id>/archive` 执行生产sync_session，
   验证SHA256、暂存→原子发布和READY。旧数据不覆盖，失败保留staging/错误证据。
4. 按frame_seq/sample/channel序号、完整六维目标、时间和确认事件验证记录对应关系。
   准入→发送/被新帧覆盖/过期/拒绝必须能解释；hold单独分类，解释frames_emitted与board消息差额。
   消息数量相等仅作初筛；ServosPosition本身没有frame_seq时不能宣称具有板端逐帧确认。
5. 跑生产6D解析/导出和本地定制LeRobot SDK读回，再用现有DatasetViewer读回两集的视频与曲线。
   逐项核对六维名称/单位/值、observe/grasp半开切片和episode边界，不能只查shape或HTTP200。
6. 当前6D exporter若只接受hardware_teleop/measured，须添加显式的simulated离线导出契约与受限入口，
   复用解析/校验/worker；不得将模拟metadata伪装成hardware、把来源改成measured或放宽全局门禁。
   派生LeRobot数据与页面始终保留 `data_identity=simulated_offline`、`training_eligible=false`；
   区分“允许生成测试派生格式”与“允许训练”，训练入口必须仍拒绝这批数据。
7. 用真实浏览器点击/查看现有页面两集与阶段切片，保存不同画面截图、API与控制台错误。
   jsdom/DOM和接口测试分别报告；真实浏览器不可用时保留该未完成门禁，不拿截图文件存在替代。

### E. 回归、故障和交付

- 先跑原有65项相关测试作为基线，再跑新增agent桥/legacy互斥/录制/导出测试及真实Humble实跑。
  Python3.10实际运行，AST或Jazzy通过不替代；如有已知失败，判断其与本轮租约、独占、阶段是否有关，
  不能只因“不是我改的”就将控制安全相关失败排除出发布门禁。
- 必须测试：外部同名/异instance拒绝、错误token/epoch/bundle、重复许可、IPC断开、agent正常退出/SIGKILL、
  agent重启、lease到期、stream断开/重连、反馈过期、急停后renew/新generation仍不运动、
  显式clear后仍需对齐/接合、旧HTTP动作拒绝、慢录包收尾与急停不等待。
- 离线只证明不再接纳/发布新目标和模拟hold行为，不能声称控制进程SIGKILL/Pi掉电时有物理停车保护。
- 不在本轮优化8.93Hz或放宽TTL。记录真实有效帧数/拒绝原因和当前频率，为下一轮三链路基准保留输入。
- 证据写到任务专属目录，例如 `docs/evidence/landerpi-agent-wiring-<实际日期>/`：源码哈希、环境/实例、
  命令/退出码、首次失败与修复复验、进程关系、许可事件、bag检查、sync/export/SDK/浏览器报告。
- 追加本轮status，不覆盖他人历史；当前摘要与Docker事实保持一致。打包包含必要未跟踪生产源码，
  新增纯模块列入允许清单；符号链接在包内解析完整，amd64产物不冒充arm64。
- 收尾撤销许可、清队列、确认新目标发布计数冻结，再收尾录包；容器保留缴械或按已授权stop精确停止，
  交接写清当前实例/状态/路径。只改本任务文件，不提交、不推送、不切current、不连真机。

最终回答：完成哪些门禁、准确命令和结果、哪些仍失败、数据在哪里、怎样复跑/停止，
并明确下一步是上位机FrameLink显式接入和三链路基准，之后才是arm64与真机联调。
不得将生产agent启动、HTTP200、READY文件存在、行数相等或子agent建议单独写成闭环验收通过。
