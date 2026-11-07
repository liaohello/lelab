# SO-101 主臂遥操作小车手臂与夹爪——逐项状态（2026-11-03）

本文件是需求一到需求八的验收台账：每条写清**目前的实际证据等级**与**剩余缺口**，避免把上位机测试当成实机验收。工作副本 `/home/liaozhengyan/Desktop/lelab/app_ros_mobile`，分支 `codex/ros2-mobile-collection`。本轮改动已按功能分组提交到本地（`300d65b…10958bc` + 文档/证据一笔），**未推送**；提交只说明代码进了版本库，不代表下面任何 `未验证` 项过了实机。

证据等级用词（全文件统一）：

- `实机`：2026-11-02/11-03 在真车上测过并有 `docs/evidence/` 取证。
- `上位机`：只在上位机跑通（单元/契约/端到端测试，或合成车端桩 + 真页面）。
- `仅静态`：代码与配置就位、被静态守卫钉住，但没有运行时证据。
- `未验证`：需要硬件或运动授权，本轮没有做，不能当作通过。

## 一 理解并保留既有成果

| 要求 | 等级 | 证据 / 缺口 |
| --- | --- | --- |
| 保留只读采集链路与 sqlite3 原始格式，不强制迁 MCAP | 上位机 | 录制仍走 `ros2 bag record`（sqlite3）；`tools/ros2_collection/tests/test_recorder_faults.py` |
| 原有无 action/feedback 的只读 session 仍禁止训练导出 | 实机 + 上位机 | 车端保留 3 个 `invalid_session_json` 未删除；只读 profile 的 `arm_sources` 不含 `leader_arm`，导出门禁有测试 |
| 不假定已有真实 controller/servo 反馈 | 上位机 | 反馈来源只读核查见任务 #9 记录；`follower_feedback.available` 缺失即拒绝进入跟随（有测试） |
| 部署脚本 PID 不再记录包装 shell | 上位机 | `tools/ros2_collection/tests/test_deploy_readonly_agent_script.py`；2026-11-03 停机时再次踩到并确认新脚本行为 |
| 不把网络可达当串口可访问 | 上位机 | 主臂读取走上位机本地 by-id 串口；页面/后端没有任何"通过网络打开主臂串口"的路径 |

## 二 模块边界

`SO101LeaderReader`（`app_ros_mobile/lelab/mobile_leader_reader.py`）、`TeleopMapper` 与 `CalibrationStore`（`mobile_teleop.py`）、`MobileTeleopCoordinator`（`mobile_teleop_coordinator.py`）、车端网关适配（`ros2_ws/src/lelab_mobile_agent/agent.py` + `contract.py`）、网页向导（`frontend/src/components/control/MobileTeleopPanel.tsx`）各自独立；车端从臂不被伪装成 `SO101Follower`，也不写 Feetech 标定文件（静态守卫：`tests/test_agent_exclusivity_guard.py` 断言部署版 agent 永不写力矩、A 层标定只走已有 SO-101 逻辑）。实时串口循环与安全检查都在上位机服务，浏览器只做流程与显示。等级：`上位机`（含 2026-11-03 主臂串口 `实机` 只读取证）。

## 三 真实从臂反馈与最小启动入口

- 只读核查 vendor 节点构造/定时器/launch 副作用，最小入口是 `tools/ros2_collection/start_car_arm_stack.sh`，含 `init_pose` 的整车 bringup 未被使用。等级：`上位机`（脚本有测试：`test_car_arm_stack_script.py`）。
- 舵机型号/ID/协议/原始单位/关节顺序/限位写在 `configs/mobile_teleop/follower.example.json` 与 profile limits，来源是 lander_pi 参考库（2026-11-03 查阅 `src/driver/kinematics/kinematics/transform.py`）。等级：`仅静态`（**未逐台实机确认**）。
- 超时、响应匹配、迟到回包、单一串口驱动、反馈年龄与"缺反馈禁止跟随"：`contract.CommandGate` 18 项 + 协调器嵌套形状 8 项，等级 `上位机`。
- 读取/控制/超时可配置（`teleop.read_hz`、`command_hz`、`arm_command_ttl_s` 等）。等级：`上位机`；**按真实总线负载定频**属 `未验证`。

## 四 三层标定向导

A/B/C 三类标定分别保存、原子写入、带版本与哈希并绑定设备身份；失配时拒绝使能并点名（`mapping binds leader=… but loaded …`）。夹爪走 0–100 百分比→脉冲→弧度，禁止度转弧度（`test_mobile_teleop_examples.py` 钉住）。等级：`上位机` + A 层主臂串口 `实机`（2026-11-03 只读取证）。缺口：B/C 层的**真实数值**（零位、方向、软限位、mimic 关系）必须由操作员在授权后逐台确认，示例里仍标 `example_not_measured`。

**2026-11-03 补：模板原样保存曾经能过门禁（点页面时抓到，等级=上位机）**。`readiness()` 原先只查"在不在、哈希对不对、绑定身份与关节顺序合不合"，而 `app_ros_mobile/lelab/*.py` 里**没有任何一处引用 `example_not_measured` / `derived_not_measured`**——那两个标记只写在示例文件里、只由页面当提示文字显示。于是"载入模板 → 原子保存该层"三步就能让三层齐备并给出 `calibration_version`，只剩"主臂没插"挡在接合前面；真车上这就是拿出厂猜测值（方向、零位、软限位）去驱动舵机，正是需求四禁止的"静默使用默认值"。现在 `CalibrationStore.unmeasured_paths()` 会走完整份文档，把带 `_example_not_measured` 标记的对象和以两种标记**开头**的字符串值收成 JSON 路径，`readiness()` 按层给出 `leader layer still holds shipped example values at <root>, lerobot_calibration.elbow_flex, … (+3 more)` 这样的点名原因；只有标记本身被换掉才算就绪（描述这个约定的散文 `_note` 不算标记，不会误锁操作员）。`test_mobile_teleop_example_guard.py` 4 项用**真实示例文件**验：三份示例存进去后三层各自被拦；同样的文档把标记去掉就 ready（证明拦的是标记不是形状）；路径解析精确到 `joints[1]` 与 `gripper.span_note`。变异检查（`if paths:` 改成 `if False and paths:`）只让第一项变红。页面侧实测：`就绪：否 · 标定版本 …` 后面跟着三条逐层原因，`取得双层租约` 的内联错误同样是这几句，预览/接合/开始会话仍全部 disabled。取证 `docs/evidence/so101-mobile-teleop-wizard-click-faults-20261103.json`（同档还记了本轮点出来的其他失败路径与"handler 派发 ≠ 指针点击"的界限）。

同日补上 A 层两处只有真文件才能暴露的问题（取证 `docs/evidence/so101-leader-calibration-reuse-20261103.json`）：① 需求写的"复用现有设备识别和标定逻辑"原本只是让操作员把 lerobot 已写好的 6 电机 × 5 字段**手抄**进网页——现在 `GET /ros-mobile/teleop/leader-calibrations` 只读枚举本机 `teleoperators/*/*.json`（含 root、sha256、电机 id、缺哪个电机/哪个字段、不可解析原因），页面"列出现有主臂标定 → 填入"直接复用原文，且列表为空时会区分"没标定过"与"`HF_LEROBOT_CALIBRATION` 指错目录"（本机实测：默认根下 `so_leader` 是空的，真实文件在 `~/.local/share/lelab-jetson-20261031/approot/hf/calibration/teleoperators/so_leader/{left,right}_rig.json`，两者 usable、id 序列 1..6）。② `build_bus` 过去只校验字段齐不齐，**不校验这份标定属于哪台臂**：拿右臂标定打开左臂，读数连续、年龄正常、度数却是错的，映射层与状态机都得不到信号。现在协调器在建总线前逐电机比对标定 id 与轮询 id，任何不一致以 `409 leader_calibration_does_not_match_this_arm: shoulder_pan:calibration_id=7!=bus_id=1` 拒绝且不创建 reader；变异检查（把守卫换成恒不触发）两项拒绝用例立刻变红，正向用例仍绿。等级：`上位机`；**by-id 序列与"这台物理臂就是这个 rig 名"的对应仍需操作员肉眼确认**，本改动只保证 id 层一致。计数：tools **212 passed**、app 遥操作五文件 **33 passed**、宽子集 **116 passed**、前端 tsc 干净并重建 dist。

## 五 防跳变遥操作

状态机十态齐备，进入 following 前需两端租约、标定匹配、反馈新鲜、记录双端参考、目标在限位内、显式接合；目标 = 从臂实测参考 + 主臂相对变化，再方向/比例、回绕、软/硬限位、EMA、限速。重连/急停恢复/标定切换/门失效都清空旧目标并要求重新对齐（`realign_required_after_release`）。等级：`上位机`。

本轮修掉两处真实缺陷（`follow_loop_parking_round`）：跟随线程自停时 `thread.join(自己)` 抛 `RuntimeError` 吞掉失败原因；主臂拔线/读失败时只静默停发、不停线程也不给页面原因。另修正映射模板把 `wrap` 开在有限位关节上（`mapping_wrap_flag_round`）。缺口：`保持策略`目前只有 profile 声明 + 状态块上报 + 静态禁断力矩，**手臂断电/停止下发后实际是否停在最后指令位置**属 `未验证`（八.5）。

## 六 控制权与失联

双层租约一个会话（任一失败回滚，`upper_lease_invalid`/`car_lease_invalid` 都会退出跟随）；录制作为子任务不再二次申请租约；标定/桌面遥操作/小车遥操作/策略控制互斥已在真实 `HardwareLeaseManager` 上测（`teleoperation` vs `inference`）。命令信封含 session/seq/source/TTL/标定版本，车端按自己的单调时钟判超时并拒绝旧 session/乱序。车端独占门禁按发布者拓扑拒绝（`vendor_publisher_present` / `publisher_topology_unknown`）。等级：`上位机`。

六种失联来源都有上位机用例：主臂拔线/读失败（`test_a_lost_gate_mid_follow_names_the_gate_and_never_resumes_by_itself`）、上位机退出（`UpperExitTests`）、隧道断开（`test_a_dead_tunnel_still_drops_the_local_session`、`test_tunnel_down_makes_every_gate_fail`）、租约在跟随中过期（`test_an_upper_lease_that_expires_mid_follow_exits_the_stream`）、车端 agent 退出（`test_gateway_failure_demotes_and_clears_targets` + 车端 TTL/watchdog 契约）。`gate_lost:<哪项>` 由状态机的 `_on_clear_targets` 写入 `last_error`，页面"最近错误"显示的就是它。缺口：**真机上"上位机消失后车端还撑多久、手臂停在哪"**属 `未验证`（八.6），且 vendor 摇杆/App 的真实节点名与是否常驻仍未观测（白名单当前为空）。

## 七 网页与录制

只读入口保留，遥操作走新增 profile（默认不放开只读那份）。页面显示身份、标定版本、控制权、离合、反馈年龄、跟随误差、相机、`最近错误`、车端独占/手臂流/保持策略 pill。录制语义：开始/结束 episode、停止会话、急停不等保存（estop 用例断言不再触发包收尾）。车端记录六类字段（主臂读数、映射前目标、实发目标、网关回执、实测反馈、离合/故障/标定版本/相机/episode 事件）由 `teleop_state_payload` 18 字段契约钉住。LeRobot `action` 用限幅后实发目标（断言不等于主臂读数）、`state` 用实测反馈、六维 schema、不补零成九维、成功/失败/unknown 人工标注且 profile 声明"无仿真真值"。等级：`上位机`（含导出端到端 + 页面 viewer 读回）。缺口：页面按钮的**真实点击**在双层租约已 acquire 且集已打开的路径上需要真实主臂串口——没有主臂就没有可点路径，属 `未验证`（八.8）。

## 八 验证与交付

1. 映射/单位/限位/回绕/标定绑定/超时/状态机测试 —— `上位机`，已完成（tools 203 passed）。
2. 无硬件与仿真流程，预览无执行器输出 —— `上位机`，已完成（预览阶段车端 `/control/arm` 计数不增长）。
3. SO101 实机读取与向导交互 —— 部分 `实机`（串口只读 29.2 Hz、独占、释放），向导走完真主臂 `未验证`。
4. 从臂真实反馈与无输出映射 —— `未验证`（车端 teleop profile 尚未部署）。
5. 逐关节低速小幅 → 多关节与夹爪跟随 —— `未验证`（需运动授权 + 现场急停确认）。
6. 故障注入、断连、恢复不重放旧动作 —— 上位机侧 `上位机`；真机 `未验证`。
7. 两段短 episode + 校验同步 + SDK/网页读回 —— 合成数据端到端 `上位机`；真车 bag `未验证`。**SDK 与网页两条读回都跑在本机车端代码产出的数据集上**（不是打桩写入）：`docs/evidence/so101-mobile-teleop-page-readback-20261103.json` 记的 12 项判据里包含"页面能定位共享 mp4 内每一集的区间"，它顺手暴露并修掉了播放器只 `#t=起` 不 `#t=起,止` 导致第 0 集播进第 1 集的缺陷。取整帧率（14.74 → 15）下的逐帧时间戳已单独核验 `上位机`：网格 = `i/15`、LeRobot `timestamp` = `frame_index/15`、mp4 用 pyav 读回帧数/平均帧率/pts 步长/总时长全部一致，且无未来观测；代价量化为 46 帧里 1 帧复用同一张相机影像（约 2.2%），两次变异检查（网格解耦、向前看 100 ms 取图）都变红。见 `docs/evidence/so101-teleop-export-timestamp-check-20261103.json`。
8. 实际点击网页主要按钮 —— 失败路径已在本机真页面真接口上过一遍（`取得双层租约` 空身份 409 点名 `identity ''`、`原子保存该层` 空身份被客户端先拦、`载入模板` 显示示例来源与替换要求、无主臂时"打开主臂串口"保持 disabled 且端口列表只有"未选择"），并因此抓到上面那条模板门禁缺陷；**但点击是用页面自己的 onClick 派发（`element.click()`）而不是真实指针事件**——in-app 浏览器视口未附着（`NATIVE_BROWSER_VIEWPORT_UNAVAILABLE`），所以命中测试、禁用态外观、播放器是否真在集边界停下都没验到。等级：`上位机（handler 派发）`；真指针点击与真主臂/真车 `未验证`。计数同日实测：app 遥操作相关 6 个文件 + 租约 API 共 **50 passed**（新增 `test_mobile_teleop_example_guard.py` 4 项），`tools/ros2_collection/tests` 仍 **228 passed**。

交付物：源码差异（已提交，`git log 8aebd04..HEAD` 即清单）、标定与映射 schema 示例（`configs/mobile_teleop/*.example.json`，2026-11-03 修正 wrap 语义）、网页流程（`MobileTeleopPanel`）、启动/停止/回退手册（`docs/ros2-collection-runbook.md` §5.1/§5.2，含逐项"算通过"判据）、版本与设备证据（`docs/evidence/so101-*`、`docs/evidence/raspberry-pi-*`）、实测结果与剩余缺口（本文件 + evidence 25 轮）。

计数（2026-11-03 逐文件实测，不再照抄历史数字）：`tools/ros2_collection/tests` **225 passed**（另 5 个 subtest；A 层标定复用与身份绑定 +9，本机车端排练暴露的四处修复 +13，之前同一命令是 212）；app 侧遥操作五个文件 **33 passed**（leader-bus 4 / app coordinator 7 / export-schema 6 / teleop-export 8 / routes 8），再加上 `test_hardware_lease_api` 的 12 项是 **45 passed**；宽子集 `-k 'ros or leader or teleop or lease or export or sync'` **116 passed**（本轮改动后重跑仍是 116）。更早几轮写的"37 passed"复现不出来（同一组文件按不同选法是 31/32/33/43/44/45），以逐文件计数为准。app 全套含本轮改动重跑 **531 passed / 1 failed（676.00 s，日志 `/tmp/lelab_full_app_suite5.log`）**，唯一失败仍是 `test_remote_inference.py::test_client_command_targets_the_tunnel` 的既有断言漂移，保留为已知问题。

## 本机车端排练（2026-11-03T13:02Z，等级=上位机合成，不是实机）

假 transport 的测试看不见车端 recorder/网关的真实行为，所以把**部署用的那份 agent 代码**在本机跑起来，用合成 DDS 话题喂它（`tools/ros2_collection/local_car_rehearsal.py` + `local_teleop_session_drive.py`），走完 控制权 → 预览 → 对齐/接合 → 两段 episode → 停会话 → sqlite3 包回读。完整一条链通过：manifest `raw_ready` + `READY` 已写、两集 `source: leader_arm`/`success: unknown`、包里 6 类字段齐全（61/61 条 arm 命令都带 leader_rad、pre_clip、target、action、calibration_version、clutch、ttl_s、seq、clipped），相机 79 帧 / 反馈 285 / teleop 状态 61 / 事件 124，臂与夹爪 trajectory 各 61 条。取证见 `docs/evidence/so101-mobile-teleop-local-car-rehearsal-20261103.json`。

排练找到 4 个**只在真实车端代码路径上才会暴露**的阻塞，全部已修并有回归测试（`tools/ros2_collection/tests/test_agent_teleop_recording.py`、`test_mobile_teleop_session.py`）：

1. **话题健康表只记收到的消息** → `/lelab/recording/teleop` 由本节点自己发布，永远读成 stale：`start_session` 直接 409 `topics_missing`，运行中的健康轮询又会给会话写 `required_stream_missing`，任何遥操作会话都到不了 READY。修法是反馈订阅记 `last_rx`、状态发布记 `last_tx`，三处话题年龄统一走 `_topic_ages()`（收发取更新的一次）。
2. **`start_episode` 的模式白名单写在 `leader_arm` 之前** → 录制作为遥操作会话的子任务被自己所在的模式拒绝（409 `mode: leader_arm`）。
3. **`_control_tick` 开头 `if self.base_pub is None: return`** → 底盘锁定时 base_pub 正是 None：命令被接受、被记成 applied、上位机以为在跟随，而一条 trajectory 都没发出去（包里两个轨迹话题计数为 0）。这条如果不排练就只会在真车上表现为"网页一切正常、臂不动"。顺手补了控制权失效/急停时丢弃排队目标，避免恢复瞬间重放过期动作（需求六）。
4. **`paused` 是没有出口的死角**：结束一集默认暂停（需求七），但 `enter_preview` 不接受 paused、`align` 只从 preview/aligned 进、`engage` 从 paused 明确拒绝 → 一个会话的第二集根本无法开始。现在 paused 可以回预览，接合仍要求建立新参考（暂停时对齐已被清除）。页面同步改成与转移表一致：暂停态下"无输出预览"可点，"记录主/从参考"不再在 paused 显示为可点（旧写法点了必被状态机拒），`npx tsc --noEmit` 与 `npm run build` 均通过；**这两颗按钮在浏览器里的实际点击仍未验（需要主臂与车端在线）**。

还有一处流程教训值得记下来：**第一次排练"跑通"了但跑的其实是 `ros2_ws/install/` 里的旧 colcon 拷贝**——`source install/setup.bash` 会把构建目录前置到 `PYTHONPATH`，我改的 `src` 没生效。现在 launcher 在 source 之后重新固定 src 路径，并在 exec 前解析 `lelab_mobile_agent.agent.__file__`，不是被测源文件就拒绝启动，把解析到的路径与 sha256 前 16 位写进 `READY_FOR_DRIVER.json`。这与部署脚本 PID 记成包装 shell 是同一类"看起来成功了"的错误。

**接着把最后两环也接到生产代码上（同一排练会话，取证 `docs/evidence/so101-mobile-teleop-sync-export-chain-20261103.json`）**：`tools/ros2_collection/local_full_chain_sync_export.py` 不打桩，直接调 `ros_mobile.sync_session`（走车端 `/sessions/<id>/archive`，逐文件按 manifest 校验 sha256/大小后原子发布）与 `ros_mobile.export_session`（真读 sqlite3 包、真写 LeRobotDataset、SDK 读回）。结果：`status=validated`、2 集 90 帧、`dims=6 / chassis_included=false`、`fps=15`（profile 声明 14.74，取整并写进 warnings）、相机 `observation.images.wrist_real`、`action_source=gateway_sent_target_after_clamp`、`success_labeling=manual_or_unknown_no_simulator_ground_truth`，逐帧 `leader_rad/pre_clip/target/命令序号/影像与状态年龄` 齐全。

这一步又暴露第 5 个阻塞：**导出器把夹爪事件的 `target` 限定成 1 个值，而车端记录的是它实际发出的整套 mimic 关节（`gripper_base_joint` + 4 个派生 jaw，master 在首位）→ 真车上任何遥操作会话都永远无法导出**（`ExportRejected: has no arm or gripper command events`）。既有导出测试喂的是合成的 1 值夹爪事件，与被测方实际记录不一致，所以一直是绿的——这是"测试与实现各说各话"的又一例。修法：接受 mimic 形状并取 `target[0]`（网关夹取后真正下发的 master 角）作第 6 个动作列，1 值老形状继续可用；新用例 + 变异检查（把过滤器改回 `len(target)==1` 即以同样的 ExportRejected 变红）。

**故障注入也在同一条排练链上做了（取证 `docs/evidence/so101-mobile-teleop-car-agent-exit-fault-injection-20261103.json`）**：episode 正在录时对 agent 进程发 SIGKILL（与 MentorPi 容器被杀、车断电 137 退出同一种死法）。上位机侧如需求六所料：状态从 following 落到 paused、原因点名是哪几道门禁失效（`gate_lost:car_lease_invalid,follower_feedback_missing_or_stale,control_not_exclusive`）、发送序号不再增长、也不会自己恢复。但车侧暴露第 6 个阻塞：**崩溃收尾只存在于优雅退出路径** —— agent 被杀后 `ros2 bag record` 成了孤儿继续往旧 session 目录写，`session.json` 永远停在 recording，没有 manifest/READY 也没人再管（车上就是磁盘被慢慢写满、这轮采集一直悬着）。现在 `recorder.recover_interrupted_sessions()` 在启动时按 `--output <session>/raw/bag` 精确匹配、只对那个进程组发 SIGINT（让 rosbag2 写完 metadata.yaml，绝不 SIGKILL），再把会话记成 `error / agent_restarted_mid_session / ready_eligible=false`；实测重启日志与 metadata.yaml 都已生成、孤儿录包进程为 0，已定稿与坏 json 的会话都不动。诚实的限制：从 HTTP 侧分不出"agent 进程没了"与"隧道断了"，所以原因按失效门禁命名，不假称知道死因；真机上舵机是否停在最后指令位置仍未验（本机无执行器）。

**再把"网页读回"这半边落到链条产出的数据集上（2026-11-03T13:36Z，取证 `docs/evidence/so101-mobile-teleop-page-readback-20261103.json`）**：`local_full_chain_sync_export.py` 原先只做 SDK 读回，现在接着调用页面路由实际使用的那三个函数（`dataset_viewer.list_episodes / video_path / episode_timeseries`），判据扩到 12 项并全绿：相机 `wrist_real`、`fps=15.0`、两集 45+46 帧且与数据集总帧数一致、series 每行 state/action 都 6 列、每集都能给出 `from_s/to_s`。这一步**当场找到一个只有真数据才能看见的显示缺陷**：LeRobot v3 把两集塞进同一个 `chunk-000/file-000.mp4`，靠时间戳定位（实测 `0→3.0` 与 `3.0→6.067`），而 `DatasetViewer.tsx` 的播放器地址只写了 `#t=<起>`，于是第 0 集会播过自己的帧、连着第 1 集的画面播下去，还 `loop` 整文件——需求八.7 要的是"数据检查"，看不出集边界的回放会让操作员拿下一集的画面评这一集。现改成 `#t=<起>,<止>`，卡片标题仍显示区间；`tsc --noEmit` 与 `npm run build` 通过，并在 `dist/assets/*.js` 里核对到编译后的片段拼接式。新增判据 `page_can_locate_each_episode_inside_the_shared_file`（区间必须存在、在共享文件里首尾相接、且跨度 = 帧数/帧率）；变异检查把 `dataset_viewer` 的 `to_timestamp` 分支改坏后，该判据与跨度判据同时变红，而旧有的"视频文件能解析"仍然绿——说明原来那组断言对这个失败模式是盲的。**浏览器里播放器是否真的在第 3.0 s 停下仍未验**（要主臂与车端在线才能走完向导并打开数据集），所以八.8 照旧 `未验证`。

计数（同日实测）：`tools/ros2_collection/tests` **228 passed**（5 subtests，崩溃恢复 +3；本轮只动排练脚本与前端，未新增车端用例）；app 遥操作五文件 leader-bus 4 / coordinator 7 / export-schema 6 / teleop-export **9** / routes 8 = **34**；宽子集 **117 passed**。

**模板门禁的连带更正（同日，等级=上位机）**：加上"未实测标记拦就绪"之后，`tools/ros2_collection/tests/test_mobile_teleop_examples.py` 里原先那句 `assertTrue(ready["ready"])` 就红了——同一份文件里另一条用例本来就断言"出厂示例绝不该看起来像真标定"，两条要求互相矛盾，是 readiness 把"结构一致"和"可以使能"混在了一个布尔里。现在拆成"结构原因必须为空 + `ready` 必须为 false"，两条同时成立才算对。计数随之：tools **238 passed**（含新增 `test_teleop_preflight.py` 10 项），app 全套 **535 passed / 1 failed**（唯一失败仍是既有的 `test_remote_inference.py::test_client_command_targets_the_tunnel` 断言漂移，与遥操作无关、刻意不改），app 遥操作 + 租约 6+1 文件 **50 passed**。另新增 `tools/ros2_collection/teleop_preflight.py`：一条只读命令把 §5.2 的主臂归属、车端 profile/保持策略/反馈话题、部署副本 sha、两个轨迹话题与反馈话题的发布者名单一次问完，探针清单是白名单、混进写操作就拒绝运行。

另：排练 agent 仍在跑时 `test_rosbridge_source` 的分片消息用例出现过一次 `ConnectionResetError`，停掉本任务启动的排练后同一文件 7 passed——本机端口/时序互相干扰，不作为代码结论。

## 上车前置核查（2026-11-03T11:32Z，全部只读）

按用户要求的顺序，动任何硬件之前先核对前置，取证见 `docs/evidence/so101-mobile-teleop-hardware-precondition-20261103.json`：

- **主臂不在上位机**：`/dev/serial/by-id` 目录不存在、无 `ttyACM/ttyUSB`、`/sys/bus/usb-serial/devices` 不存在，`lsusb` 只有集线器/网卡/笔记本摄像头，`journalctl -k --since -24h` 里 0 条 USB 插桩事件。因此 §5.2 第 3 步（A 层向导走真主臂）与八.8 的真实按钮点击仍然没有可执行路径；主从身份映射不确定 → 不允许接合。
- **车端 vendor 容器没在跑**：`docker ps` 为空，`MentorPi` 是 `Exited (137) 5 hours ago`，`RestartPolicy=no`、`FinishedAt=2026-11-03T06:36:10Z`（正是断电时刻），Pi 本身已开机约 19 分钟、SSH 公钥可达但 5 次里 2 次超时。§5.2 第 0/2 步（`ros2 topic info -v`、`--check/--render/--apply`）都要求该容器在跑。
- **没有替用户起容器**：`docker start MentorPi` 会带起 vendor 的 ROS 栈，其中 `ros_robot_controller` 构造阶段就写控制板（enable_reception、舵机偏移、机型、LED、底盘零速），而"是否授权写板"用户明确留给现场决定，所以我既没起容器，也没跑 `start_car_arm_stack.sh --apply`。

## 现在唯一能推进剩下的事（都要用户现场）

1. 主臂 USB 接回上位机；确认 by-id 序列与身份绑定、物理安装方向（用户已说明由其确定主臂，我这边只做只读枚举与核对）。
2. 把车端栈起起来（`docker start MentorPi` 或平时的方式）——注意这一步本身就带 vendor 的板卡写入副作用。
3. `PI=… PROFILE_NAME=hardware-teleop-v1 tools/ros2_collection/deploy_readonly_agent.sh --check → --render → --apply`。
4. `tools/ros2_collection/start_car_arm_stack.sh --apply --i-authorize-board-writes`（会写控制板，需明确授权）。
5. 车上先 `ros2 topic info -v /lelab/arm/trajectory_request`、`/lelab/gripper/trajectory_request`，再决定 `exclusive_exempt_nodes`。
6. 按 runbook §5.2 的 0–10 步逐项验收（顺序：单关节小幅 → 停止行为 → 故障注入 → 才做主从跟随与两段 Episode）；每步留 `/status`、`manifest.json` 片段、页面截图或导出报告到 `docs/evidence/`（带日期），并区分"实测 / 仅页面显示 / 由后续反馈推断"。

另有一项纯软件侧决定（用户已给方向）：本机 `app_ros_mobile/.venv` 的 torchcodec 缺 libavutil（.56~.60 全缺），视频编码退回 pyav 而 pyav 拒绝浮点 fps，所以导出把采样频率取整（真车腕相机约 14.74 Hz → 15 Hz）。用户同意**先保留取整行为做基础验证，但必须核对实际帧时间戳**；后续为精确编码要在隔离环境装依赖并验证真实编码路径——单纯装系统库不保证编码器支持非整数帧率。取整对时间戳的影响见 runbook §6 与 `docs/evidence/` 的时间戳核验记录。

## 桌面双臂工作台 + 车端舵机只读识别（同日续，2026-11-03T15:35Z）

用户选定范围：**桌面双臂为主、复用现有遥操作 WebSocket 流、先写代码后真机验**。取证见 `docs/evidence/so101-desktop-workbench-and-car-readout-20261103.json` 与 `docs/evidence/so101-car-servo-readout-20261103.json`。

**为什么桌面侧也要"标定归属"守卫**：逐一读四个 rig 标定文件核实——**每台臂的电机 id 都是 1..6、关节名完全相同**，而 lerobot 的 `FeetechMotorsBus.write_calibration` 按 motor **name** 遍历、经总线自身 name→id 映射下发，**从不比对条目里的 id**。于是"右 leader 的标定写进左 leader"不会有任何报错，只给出平滑合理但属于另一台臂的角度。唯一稳定身份是 `/dev/serial/by-id` 序列号。新增 `lelab/arm_bindings.py`：标定保存时记 `role/config/内容sha256/逐关节id/测量时间`（`calibrate.py::_record_arm_binding`），桌面遥操作启动前核查并**在两台设备构造完、串口仍未打开时**拒绝（`teleoperate.py::_identity_guard`）。拒绝条件只有证据矛盾：同口兼主从、端口上次量成另一侧、该端口量的是别的文件、标定 id 与总线将轮询的 id 不一致；没有测量记录（改造前标的臂）与手工改过限位**只提示不阻断**，绑定表损坏按无记录处理。新增 `GET /so101/devices`、`GET /so101/identity`（都不开串口）。前端 `/so101` 工作台：by-id 单选指派主从、标定文件带哈希与"量自哪些端口"、逐关节主臂目标 vs 从臂实测曲线 + 跟随误差、租约/在线状态、核查不过时启动置灰，并补上**离开页面即停会话**的安全网（与 `/teleoperation` 同款 pagehide keepalive）。`joint_update` 增加 `leader` 字段，值取自本轮**已经用于下发**的 `get_action()` 读数——不追加第二次主臂 `sync_read`，环路周期与总线负载不变。

**车端（用户授权 `docker start MentorPi` 后，只读）**：`/dev/serial/by-id` 有两个口，vendor udev 规则把 ttyACM 链接为 `/dev/rrc`，实测 `/dev/rrc -> ttyACM0` ⇒ 从臂总线在 ACM0（`ttyUSB0` 是讯飞麦克风）。**纠正一个我先前的错误说法**：`/dev/rrc` 不是裸 Feetech 舵机总线，而是**控制板的串口协议**，舵机挂在板子后面。用 vendor 自带 SDK 的读命令（`PACKET_FUNC_BUS_SERVO` + 0x12/0x05/0x09/0x07/0x0D）做纯读识别，实测 **1–5 号舵机在答**（自报 id 与请求一致；位置 483/740/31/117/495；温度 22–27°C；供电 6263–6318；**torque_state 全为 0**），**6 号五次全 `servo_error(-1)`** 即该路当前读不到。复读位置逐台不变而电压抖动 ⇒ 是活读不是缓存。走网关那条路仍被挡住：`ros_robot_controller_node.py:61` 用 `os.environ['MACHINE_TYPE']`（容器 env 与宿主 profile 都没有，不设即 KeyError，我们的启动脚本因此拒绝起它），所以 `/controller_manager/joint_states` 还不存在——**需求八.3 的"真实从臂反馈经网关"没有完成，本次只到总线级只读识别**。用户提供的机型 `LanderPi_Mecanum` 留待授权写板那一步使用；本轮**没有写板、没有使能力矩、没有发位置命令**，车上两份本任务副本已删除。

**这一轮又抓出 4 个只在真跑时才暴露的缺陷**（都有回归测试）：① `start_car_arm_stack.sh` 的 UART/共控审计里 `" ".join(bytes.split(b"\0"))` 抛 TypeError ⇒ "谁持有控制板串口、有没有别的 vendor 节点在跑"这个安全前置**从未答过**，而原测试只 grep 标签字符串所以一直绿；② 修好①后审计把**自己的命令行**（heredoc 含全部 needle）认成运行中的 vendor 节点 ⇒ 改为沿 PPid 跳过整条祖先链；③ 审计把 pid 固定在路径第 3 段，`PROC_ROOT` 指向别处时取到临时目录名 ⇒ 改为相对 PROC 取 pid；④ 只读探测脚本自身三处误判：没开 `enable_recv`（全部超时）、把回复里的 `status==0` 当成失败（vendor 语义是 **0 才是成功**，结论正好反过来）、回显后清队列会丢掉真答案（板子先回显 2 字节请求，而 SDK 队列 maxsize=1）。这些是"协议没有事务 ID 就要串行化并处理迟到回包"（需求三）的实测版本。

计数（同日再测）：`tools/ros2_collection/tests` **250 passed**（5 subtests，含 car_servo_readout 10 + 假 /proc 审计 3）；app 定向 **112 passed**；app 全量 **558 passed / 1 failed**（仍是既有已知失败 `test_remote_inference.py::test_client_command_targets_the_tunnel`）；`npx tsc --noEmit` 干净、`npm run build` 通过。ruff 在本机两个环境都未安装，本次未执行。

**仍未验证**：主臂在上位机的实际枚举（14:36Z 与 15:0xZ 两次都是 `/dev/serial/by-id` 不存在、lsusb 无 `1a86 USB_Single_Serial`、内核 2 小时内 0 条 `new-*-speed USB device`——用户说的"leader 已插好"既没落在上位机也没落在车上）；有真实主臂读数时的曲线与主从一致性；有会话时离开页面即停；车端臂栈起起来后的网关反馈；6 号舵机为何无响应；车上真机全链路（等充满电后整体调试）。

### 续：把守卫做到所有会写板的路径上（15:40Z）

上一笔只守住了 `/move-arm`。**桌面录制走的是同一批舵机、同一套 `write_calibration`**（`record.py` 里"先连总线→再写标定→再连相机→configure"的顺序是历史原因，因为 `robot.connect()` 会在标定不一致时读 stdin 并在 configure 末尾使能力矩），所以只要录制入口不核查，"选错臂的标定"仍然只差一个按钮。现在把连接与写板那段提成 `record.bring_up_devices(robot, teleop, cfg)`：先跑与遥操作同一个 `arm_bindings.check_desktop_pair`（用每台设备自己的 `calibration_fpath` 与总线电机 id，避免再猜一次标定树），核查不过就抛 `ArmIdentityError`，**不开串口、不写板**；通过才依次 connect/write_calibration，顺序与原来一致（用例断言日志等于 `[connect:follower, connect:leader, write:follower, write:leader]`）。

同时 `/teleoperation-status` 增加 `session` 字段：正在跟随的是哪两个端口、哪两份标定、以及核查时接受到的绑定证据（含哈希）。这是需求七"页面显示设备身份与标定版本"的前提——否则页面会把**当前选择框里的臂**说成**正在驱动的臂**；`/so101` 工作台改为显示会话实际身份，并在会话进行期间禁用端口单选与配置下拉。空闲时 `session` 显式为 `null`（有两条断言钉住：一条对 idle，一条在内联跑完一次真实环路后取广播时刻的状态，验证会话期间确有值、拆除后确被清空）。

计数：app 定向 **128 passed**（含录制路径两条新用例），`tsc`/`build` 干净。一处操作教训：收尾时我用 `pkill -f "uvicorn lelab.server:app.*18192"` 停自己的验证服务，命令模式匹配到了**执行它的 shell 自身**而把 shell 一起杀掉（正好是刚在车端审计里修掉的同一类自匹配）；用户自己的 18186 实例未受影响，之后改为按记录下来的 PID 停。

### 续：关节流不再用全零冒充读数（15:47Z）

桌面链路上还留着一条与需求三/七直接冲突的老行为：`get_joint_positions_from_robot` 在读异常时返回 `dict.fromkeys(六关节, 0.0)`，缺键也逐个补 0.0。也就是说**从臂总线一掉线，前端就把"读不到"画成"臂回到零位"**，跟随误差还算成 `|目标 − 0|`——正是设计文档 §7 写的"缺失状态显示异常，不用全零姿态伪装成真实数据"要防的事。现在：

- `joints_from_motor_reading` 把缺键作为 `missing` 报出来，不再补零；`read_joint_side` 统一给出 `measured / read_failed / partial` 三种状态，后两种 `joints` 为 `None`，并记录 attempted_at。
- `joint_update` 因此带 `joints`/`leader` 可为 null，外加 `follower_status`、`leader_status` 与两侧 `*_missing`；主臂读数照常来自本轮已下发的 `get_action()`，**一次失败的从臂读取不会拖停跟随环路**（用例断言此时 `sent == 1`、`leader` 仍有值）。
- `/joint-positions` 不再返回 `success: true` + 全零，而是 `success: false` 并说明是 read_failed 还是缺哪几路。
- 前端 `useRealTimeJoints` 在 `joints` 为 null 时仍把帧交给页面（只跳过 URDF 更新），曲线在缺失处断开（recharts null → 断点），面板底部写明 `从臂 读数失败 / 残缺（缺 xxx）`，逐关节读数显示"缺"。

变异检查：把补零改回去（两处），`test_a_partial_reading_is_not_padded_with_zeros`、`test_a_failed_read_is_reported_as_failed`、`test_a_dead_follower_bus_still_streams_the_leader_without_faking_a_pose` 三条同时变红；恢复后 12 passed。app 相关集 **143 passed**，`tsc`/`build` 干净。**仍需真机**：总线掉线时页面的实际观感（曲线断点 + 红色状态），以及 URDF 保持上一姿态不跳零。

### 续：桌面跟随在反馈读不到时会自己结束（15:50Z）

上一条让"读不到"变得可见，但看见之后还得停下来。桌面跟随环路是本地的（`robot.send_action(action)` 直接下发），从臂总线掉线不会让它乱动——可一旦每一帧实测都失败，就等同于闭眼继续下指令。现在连续 `LELAB_MAX_FOLLOWER_READ_FAILURES`（默认 10，20 FPS ≈ 半秒）次读不到就写 `stop_reason=follower_feedback_unavailable: N 次读取 read_failed/partial`、置 `teleoperation_active=False`，由工作线程自己的 `finally` 断开两台设备（**不在请求线程里碰串口**）。`/teleoperation-status` 新增 `stop_reason`，`/so101` 在遥操作步骤里显示上一次会话自行结束的原因。

两条用例钉住边界：失败计数在真的读不到时于第 3 次停止下发（`sent == 3`）且 `disconnected is True`；**偶发两帧失败后恢复读数则不会结束会话**（跑到测试上限 9 次下发、`stop_reason is None`）——限幅可配但坏值不回退成"禁用"（`not-a-number → 10`、`-5 → 1`）。变异检查：把停止条件短路后只有"该停"那条变红。app 相关集 **176 passed**，`tsc`/`build` 干净。

诚实的限制：这仍然是**上位机本地链路**的保护，不等于车端那条"不能把上位机停止发包当成车端已经停止"的要求；车端 TTL/单调时钟/停止逻辑与网关反馈下的跟随仍要在车上实机验（需求六、八.4/八.6）。

### 续：控制权冲突的原因不能停在 `[object Object]`（15:53Z）

用 curl 抢下 `teleoperation` 租约后 `POST /move-arm` 得到 **409**，`detail` 是 `{"message": "teleoperation is holding the hardware lease (owner: somebody-else, 60s left)…", "holder": {...}, "task": "teleoperation"}`——互斥本身是对的（页顶横幅同时显示"遥操作 somebody-else · 剩余 118s"）。但 `lib/apiClient.ts` 一直把 `errBody.detail` **原样**当字符串用：`ApiError.detail` 与 `${label} failed: ${detail}` 都会拿到对象，于是任何 `setError(exc.detail)`/toast 的地方要么打印 `[object Object]`、要么把对象当 React child 渲染抛错。这对 `/so101`、`MobileTeleopPanel` 等所有以 `exc?.detail || exc?.message` 显示错误的页面都成立，等于**控制权被谁占着这个操作员最需要的信息被吃掉了**。现在由 `describeDetail()` 统一取 `message`，带 `holder` 时补成"（持有者 owner / task）"，纯字符串照旧，其它结构 `JSON.stringify`。tsc/构建干净；**该消息在页面上的最终观感还没点到**——换到新验证端口后臂卡片没渲染出已选 rig（Workbench 图标不出现），留到实机一轮一起看。

### 两条必须留在账上的操作事实（15:54Z）

1. **`lib/` 被 .gitignore 全目录吞掉，前端源码有一部分从来没进过 Git。** `.gitignore:17` 是 `lib/`，实测 `git check-ignore -v` 命中 `app_ros_mobile/frontend/src/lib/apiClient.ts`，且 `git ls-files app_ros_mobile/frontend/src/lib | wc -l` = **0**（目录里实际有 17 个文件：apiClient、datasetViewerApi、calibrationTargets 等）。后果：今晚这条"控制权冲突原因显示成 [object Object]"的修复**只存在于工作区与 dist，进不了提交**。我没有擅自 `git add -f` 17 个文件——那会一次性改变仓库跟踪范围，留给你决定（要么改 .gitignore 为 `**/node_modules/**/lib/` 之类的窄规则再补跟踪，要么显式接受这部分源码不入库）。
2. **我自己犯了一次与刚修的缺陷同类的错**：收尾时用 `ss … | grep -oE 'pid=[0-9]+' | tail -1` 取 pid 杀验证服务，取到的是**最后一个监听者的 pid**而不是我的 uvicorn，被杀的是别的本地进程（用户 18186 已确认无恙，但那次误杀的受害者我无法从这份快照反查——之前的 `ss` 输出被 head 截断了）。正确做法本来就是启动时 `echo $!` 记 pid；之后我按 18194 的真实 pid（1171433）停掉了它。车端识别阶段那个 `pkill -f` 也是同一个教训的第一次。

### 续：跟随环路自己死掉时，要说出是哪一侧断的（16:00Z）

上一条给了 `follower_feedback_unavailable`，但环路里还有两处会抛异常然后**只留一条日志**：主臂 `get_action()`（线被拔掉/读数失败）和从臂 `send_action()`（写目标被拒）。操作员看到的现象一样——"会话结束了"，处理方式却不一样：前者该查主臂 USB，后者得先确认从臂还在不在总线上、能不能安全重试。现在两处都在重抛前写 `stop_reason=leader_read_failed: …` / `follower_send_failed: …`，`finally` 仍由工作线程自己断开两台设备。

两条用例把环路**真的跑起来**（Thread 内联、sleep 里置停），断言原因的**前缀 + 原始异常文本**都在，并且：主臂侧失败时 `sent == 0`（一个目标都没下发），从臂侧失败时 `sent == 1`（原因确实来自那次被拒绝的写入）。变异检查：把 `send_action` 的 except 摘掉后 `test_a_follower_that_refuses_the_command_names_the_follower` 变红（`stop_reason` 为 None），恢复后 `test_teleoperate.py` **17 passed**。

> 修这条用例的过程本身也值得记：最初的假从臂总线没有 `connect()`，会话在连接阶段就失败，断言拿到 `None.startswith` 报错——**失败在测试的桩上，不在生产代码上**。把 `_Bus`（`connect` + `write_calibration`）提到模块级复用，才让环路真正走到 `send_action`。

### 续：前置核查现在会预告车端那个 KeyError（16:01Z，全部只读）

`teleop_preflight.py` 的探针表加两条，都是读：`printenv MACHINE_TYPE || echo MACHINE_TYPE_UNSET`、`ros2 node list`。理由来自今晚的实车只读识别——容器里 `MACHINE_TYPE` 没设，vendor `ros_robot_controller_node.py:61` 直接 `os.environ['MACHINE_TYPE']`，节点在建构造阶段就 KeyError；而 `ros2 node list` 能区分"反馈话题没发布者"到底是**节点没起**还是**起了但没发**。两条判定都是 `blocking=False`：已验收的只读录制从不需要 vendor 栈，把它们算成阻塞会一夜之间把绿的核查变红。

顺带修掉一个更普遍的问题：不带 `--pi` 时原来只给 `car_container` 打一条 skipped，其余探针**悄悄没有判定**——"没查"被显示成"没这条检查"。现在每条探针都记 `not read: skipped: no --pi target`。实测本机一遍（16:00Z，纯只读）：

    NO  [blocking] leader_serial_on_upper_host: no /dev/serial/by-id entry: the arm is not plugged into this host
    NO  vendor_node_machine_type_exported: not read: skipped: no --pi target
    NO  vendor_board_node_driving_the_uart: not read: skipped: no --pi target
    ready for a read-only session: False

同时直接确认了 `ls /dev/serial/by-id`、`/dev/ttyACM*`、`/dev/ttyUSB*` **在本机全部不存在**——两条主臂此刻不在这台上位机（与"已经插好"的 earlier 状态不同，应是随小车搬运一起拔走了）。这不影响今晚的代码工作，但需求八.3（SO101 实机读取与向导交互）仍要等臂重新插上。

`tools/ros2_collection/tests` **253 passed**（新增 3 条：MACHINE_TYPE 未设要说 KeyError、图里有 board 节点要说它是 UART 的现有主人、没有 board 节点要说"现在没人驱动控制板"而不是假定存在）。

### 续：桌面链路补上"无执行器输出的映射预览"（16:07Z，无硬件）

需求五要求跟随之前先有 preview，验证顺序八.2 更直接写着"确认预览模式无执行器输出"。之前只有车端网关有 preview，**桌面这对主/从臂没有**：`/move-arm` 一起来就 `robot.configure()` + `send_action()`，操作员没有任何"先看目标长什么样"的档位。

现在 `TeleoperateRequest` 多一个 `preview: bool = False`，预览会话里：

- **不调用 `robot.configure()`**——这一步才是给从臂上力矩的地方；主臂仍 configure（它只负责被读）。
- 环路照旧读主臂、读从臂并广播，但**跳过 `send_action()`**，帧里多 `preview`/`target_sent` 两个字段，页面据此显示"这是将要下发的目标"而不是"已经下发"。
- 反馈读不到的**自停规则在预览下不适用**：没东西被写到总线，缺读数就是屏上缺数据，不该把会话拆掉（跟随下仍然第 N 次失败就停）。
- 会话记录带 `preview`，`/so101/devices` 的"会话中的臂"多显示 `· 仅预览`；页面在第 3 步给两个按钮——"仅预览（不下发目标）"和"开始跟随"，预览中另有黄条说明力矩未开。

三条用例：预览下 `sent == 0` 且 `configured == 0`、帧仍带 `leader`+`joints`；跟随下 `configured == 1`、`sent == 1`（防止有人把预览分支写成默认跳过）；预览下从臂总线读失败时 `stop_reason is None`、`follower_status == "read_failed"`、`joints is None`。另外 `test_broadcast_carries_the_leader_reading_that_drove_the_follower` 把帧的键集合钉死，新字段一加它就变红——这是好事，说明流的契约真的受测试保护。

变异检查：把预览的 `configure()` 跳过改回无条件调用、`if request.preview` 短路成 `if False`，**恰好那两条预览用例变红**，恢复后 `test_teleoperate.py` 20 passed；桌面集 **181 passed**，`tsc --noEmit` 与 `npm run build` 干净。

诚实边界：这是**上位机本地总线**的预览，代码路径已由单测钉住，但"页面点下仅预览后机械臂确实纹丝不动"还没在真臂上看过（此刻两条主臂都不在这台机器上）；车端那条 preview 走的是网关 + 无输出注入，另一条验收线，仍要等小车充电完。

### 续：桌面跟随补上离合与相对增量，不再有"一启动就甩过去"（16:32Z，无硬件）

需求五的两个关键句此前只在车端满足：`preview → aligned → armed → following` 与"以从臂**实际**参考位置加主臂相对变化生成目标"。桌面这对 SO-101 是 `handle_start_teleoperation` 一起来就 `robot.configure()`（上力矩）+ 每帧 `send_action(主臂读数)`：主/从臂摆放不可能一致，**第一帧目标就是主臂当前位姿**，只能靠 `max_relative_target` 每步夹住，等于"用限速掩盖跳变"，而且没有"释放离合/重新接合要重新对齐"这一层。

现在：

- 新模块 `lelab/desktop_follow.py`（纯函数）：`capture(leader, follower_measured)` 只有在**每一路都有实测**时才成参考，缺任一路就返回 missing 并拒绝整帧参考；`follow_target` = 从臂参考 + 主臂相对变化，`wrist_roll` 这类连续关节先把差值折到 (-180°, 180°]。
- 环路默认**离合断开**：会话建立后照常读主臂、读从臂并广播，但不写总线。`/teleop-clutch {engage:true}` 只是"请求接合"，真正建参考由工作线程在**下一个已实测帧**完成——请求线程不去抢从臂总线（同一总线只有一个驱动）。
- 接合被拒绝要写清原因：`follower_feedback_not_measured: <status>`（实测读不到，绝不用最后目标或零值冒充位置）、`alignment_incomplete: [缺的路]`、预览会话 `preview_session_sends_nothing`。**没有实测读数就不允许进入跟随**，这条现在是代码而不是文档。
- 断开 = 清参考（`aligned=false`），重新接合必然重新对齐，不会从旧参考续跑；会话 `finally` 也清，`handle_start_teleoperation` 建立新会话前再清一次。
- 显式表达"我要从一开始就跟随"的入口是 `TeleoperateRequest.engage=true`（`/so101` 的"开始跟随"按钮、旧版机器人卡片入口都传 `true`；`/so101` 的"仅预览"传 `false`）。默认值是 `false`，新调用方不会意外拿到运动。
- 帧里加 `target`（真正下发/将要下发的那份，URDF 弧度）与 `clutch` 快照；曲线图的"目标"优先取 `target`，接合前二者相等，接合后就是相对量——页面上能直接看出参考建立后"目标不再等于主臂绝对位姿"。

测试 8 条（`test_desktop_follow.py` 6 + `test_teleoperate.py` 新增 5，含改名/合并）：参考缺一路就整帧拒绝；主臂不动 ⇒ 目标 = 从臂自身位姿（不会漂）；连续关节 179°→0° 折成 −179° 而不是 +181°；会话建立但从未接合 ⇒ `sent == 0` 且 `clutch.engaged=false`；请求接合后在第二个实测帧下发相对目标；从臂读不到时接合被拒且仍 `sent == 0`；断开清参考、预览拒接合、会话不在时拒操作；`engage=true` 的请求在首个实测帧自行接合。`test_teleoperate.py + test_desktop_follow.py` **33 passed**，涉及 `/move-arm`、`/teleoperation-status` 的模块合计 **91 passed**，`tsc --noEmit` 与 `npm run build` 干净。

诚实边界：这条离合/相对映射链路是**上位机本地两臂**的实现与单测验证，跳变保护是否真达到预期，要看真臂插上后按"仅预览 → 接合 → 断开 → 再重新摆放主臂再接合"走一遍；车端那条（网关 + 车端状态机）不受本次改动影响，也仍未实机验证。

补测（16:34Z）：把环路的离合门整段换成「恒定有一份空参考」（等于没有门），四条离合/接合用例加上 `test_broadcast_carries_…` 共 **5 条同时变红**；恢复后两模块 **33 passed**，`git diff` 对 `lelab/teleoperate.py` 为空，确认回到已提交状态。app 全量 **583 passed / 1 failed**，唯一失败仍是既知的 `test_remote_inference.py::test_client_command_targets_the_tunnel`（与本次改动无关，早已记为已知问题）。

### 浏览器真实点击（16:41Z，无主臂参与，八.8 的一部分）

起了个自用的只读实例 `127.0.0.1:18198`（uvicorn + 已构建 dist）点了 `/so101`：四步条可点切换；第 1 步在没有任何串口时给出的是**事实**而不是空表——「这台主机没有列出任何外部串口。主臂必须接在上位机的 USB 上——网络可达不等于串口可访问」，并列出绑定表路径；第 3 步两个新按钮都在（`仅预览（不下发目标）`、`开始跟随`）且都 `disabled`，旁边写明原因「先选定两台臂与各自的标定文件。」；第 4 步能列出真采集过的 session（如 `lelab/ros-mobile-20261031t164152z-4a59021d`，1 集/200 帧/10 fps）；点回第 1 步端口选择器还在（步骤切换不丢状态）。

顺手验证了控制权链条本身：`GET /teleoperation-status` 已带 `clutch` 快照；用 curl 以 `teleoperation` 认领租约但没有对应会话时，看门狗按 `task-finished` 把它回收，页面第 3 步的租约条回到 `idle`——**空占硬件不会把总线锁死**。同一时刻 `POST /move-arm` 返回 409 且 `detail` 是 `{message, holder, task}` 结构（curl 侧核对；页面上的可读渲染要等有臂时再点一次）。

收尾只杀了我自己那个 pid（先 `ss` 取 pid 再用 `/proc/PID/cmdline` 确认是 `--port 18198` 的 uvicorn），用户自己的 18186 确认仍在监听。证据：`docs/evidence/so101-workbench-browser-clicks-20261103.json`。

### 无硬件整链重跑 + 车端 agent 的退出状态（17:00Z，八.2 与八.7 的本地部分）

用户要求"先完善逻辑、等小车充满电再整体调试"，所以这一轮仍然是无硬件的，但对象换成**部署用的那份车端代码**：`local_car_rehearsal.py` 起 agent → `local_teleop_session_drive.py` 走双层租约/预览/接合/两段 episode → `local_full_chain_sync_export.py` 用生产 `ros_mobile.sync_session / export_session` 补上校验同步与导出读回。重跑的价值当场兑现——它抓到两个只有"真的把链跑一遍"才会暴露的问题，253 条 tools 测试一个都没红。

- **回归 1（我自己的门禁把排练打死）**：`local_teleop_session_drive.py` 直接从 `configs/mobile_teleop/*.example.json` 播种三层标定，而 4679595 让 `CalibrationStore.readiness()` 拒绝"原样保存的示例值"，于是整条无硬件链第一步就 `calibration not ready: leader layer still holds shipped example values at <root>`。修法是把"排练"这件事写成来源标记：`as_rehearsal_provenance()` 去掉 `_example_not_measured`、把 `example_not_measured/derived_not_measured` 改写成 `rehearsal_synthetic`，**数值一字不改**。真页面"载入模板后原样保存"仍然被拒绝——这条和新链路各有一条测试钉住；把改写拿掉，`test_the_driver_seed_passes_the_shipped_example_gate` 立刻以现场同款文案变红（变异核验过）。
- **回归 2（收尾脚本信了一份旧报告）**：它从 `<root>/../rehearsal_report.json` 取 session id，找不到就退回硬编码的 `/tmp/rehearsal_report.json`，于是要么对着活 agent 要一个它从没录过的包（`404 session_not_found`），要么同步到上一次排练的数据。现在 `ready_session()` 只认**正在应答的那个 agent**：`GET /sessions` 里恰好一个 `raw_ready` 且路径在 `--root` 下，并用 `READY_FOR_DRIVER.json` 的 `port`/`car_root` 对照 URL 与 `--root`。现场真演示了歧义拒绝：两个就绪包时报 `expected exactly one raw_ready session under this root, found ['ros_20261103T164600Z_795c1e86', 'ros_20261103T165436Z_fab042eb']; pass --session to pick one`。7 条用例覆盖端口不符、无端口 URL、car_root 不符、零个/两个就绪包、就绪包在 root 之外。顺带把 `ros_mobile` 的导入挪进 `main()`——原来一导入就拖 FastAPI，jazzy 环境里根本没法给这个纯函数写测试。
- **重跑结果（八.7 的本地部分）**：`--session ros_20261103T165436Z_fab042eb` 退出码 0、`expectations.failed` 为空、同步状态 `raw_ready`；SDK 读回 2 集 / 60 帧 / 15 fps、`state_dims=action_dims=6`、features 里没有任何底盘或速度列、相机只有 `observation.images.wrist_real`；导出报告 `action_source=gateway_sent_target_after_clamp`、`success_labeling=manual_or_unknown_no_simulator_ground_truth`、`export_schema={kind: arm_gripper_6dof_phase1, dims: 6, chassis_included: false}`；页面侧 `dataset_viewer` 读回每集曲线两侧各 6 列，并能在同一个 mp4 里定位本集（ep0 `0.0–1.933 s`、ep1 `1.933–4.0 s`）。第一次重跑（`795c1e86`，91 帧）同样 `failed: []`。
- **写保护没有被绕过**：重复同步返回 `already_synced`（且重新逐文件比对 sha256），重复导出返回 `409 dataset already exists: …`；两条都是现场观察到的，不是改出来的。
- **车端 agent 的退出状态（新发现的第二个真缺陷）**：上一轮 SIGTERM 停 agent 时，`main()` 的 `finally` 里那句 `rclpy.shutdown()` 抛 `RCLError: failed to shutdown: rcl_shutdown already called on the given context`——context 早被 rclpy 自己的信号处理器关掉了。它排在 `finally` 最后一行，所以停车动作没被跳过（两个 session 都正常 `raw_ready`），但每次"计划内停止"都在车尾日志留下一个栈并把退出码写成 1，很容易被读成崩溃；而且任何加在那行之后的代码都永远不会执行。现在：`_teardown()` 只在 `rclpy.ok()` 仍为真时关 context，`main()` 把 `KeyboardInterrupt` 与 `ExternalShutdownException` 当计划内停止记一行日志。**实测**：改前 SIGTERM → 退出码 1、日志含 RCLError 栈；改后同样的停止 → 退出码 0、`grep -ci traceback` 为 0、日志是 `agent stopping: ExternalShutdownException`、端口释放、无残留进程（只对我自己那个 pid 动手，先 `/proc/PID/cmdline` 与 `ss -ltnp` 双重确认）。收尾顺序本身也被测：`stop_session → 零速 Twist → 停 HTTP → destroy_node → 关 context`，录包进程拒关时仍发零速；rclpy 那句 `Failed to publish log message to rosout: publisher's context is invalid` 仍会出现（context 已没了，发不出去），属可接受噪声。
- **测试**：`tools/ros2_collection/tests` **268 passed, 5 subtests**（253 → +10 排练链 +5 agent 退出）；`test_mobile_teleop_coordinator / test_ros_mobile_export_schema / test_teleoperate / test_desktop_follow` **46 passed**。app 全量与前端构建本轮未重跑——只改了 `tools/` 与车端 agent，二者由上面的实时链路与针对性测试覆盖。

诚实边界：主臂是合成的、`acks` 为空（没有真 controller 回执）、相机是 64×48 未压缩帧，这一轮证明的是**代码路径**，不是机械行为；`PLANNED_STOP_ERRORS` 的 `except ImportError` 分支是给车上 Humble 的兜底，本地 Jazzy 跑不到那条分支，真机起进程时才能确认那个名字在不在。八.3–八.8 的实机项一个都没变绿。证据：`docs/evidence/so101-mobile-teleop-chain-rerun-and-agent-exit-20261103.json`。

补测一（17:04Z，同一件事的第二次实测）：把排练根里的 agent 用 `run_local_agent.sh` 起回来时，**车侧崩溃收尾真的跑到了**——日志点名 `recovered interrupted session ros_20261103T170333Z_e118caa7: stopped orphan recorder pids [1265954]`，孤儿 `ros2 bag record` 消失、包停在 540K、`raw/bag/metadata.yaml` 写出（6628 B），会话记成 `status=error / stop_reason=agent_restarted_mid_session / ready_eligible=false`，没有 manifest/READY 也没被删。顺带纠正了 §5.2 item 7 的一处**文档不实**：`local_car_fault_injection.py` 从来只杀不重启，按原文照做会把一个还在往旧 session 写的孤儿进程留在车上（这次就实测到了：包从 528K 继续长）。现在文档写明两步，判据是"孤儿录包进程数为 0 且包体积不再增长"。

补测二（17:06Z，退出状态这件事没做完）：重启那个 agent 再 SIGTERM 时，rclpy 抛的**不是** `ExternalShutdownException`，而是 `_wait_for_ready_callbacks` 里的 `RCLError: the given context is not valid` —— 信号落在哪决定异常类型，只按类名接等于漏接，日志又变回一个栈。判据换成 context 本身（`_is_planned_stop()`：命中类名元组 **或** `rclpy.ok()` 已为假才算计划内停止，其余照旧抛出保留栈）。实测 SIGTERM 与 SIGINT 各一次：退出码均 0、`grep -ci traceback` 均 0、原因行都是 `agent stopping: ExternalShutdownException`、端口释放。测试多一条：活 context 下的 `RuntimeError` 必须仍算真故障（`tools/ros2_collection/tests/test_agent_exit_teardown.py`）。tools 全量 **269 passed**。

### 第六路的另一半也在本机演练过了（17:13Z，八.6 的本地部分）

需求六那句"不能把上位机停止发包当成车端已经停止"以前只有单测。新工具 `tools/ros2_collection/local_car_upper_stream_stall.py` 用部署用车端 agent 把它跑成事实：把驱动进程（发包侧）SIGSTOP 2.5 s，agent 不冻、车端控制租约仍 `active:true`、录包继续。车端在 `ttl_s=0.6` 的单调时钟上自己把臂流判过期（采到 `live:false`、`age_s=0.264`、`remaining_s=-0.064`），期间 `control.teleop.last` 完全不变（没有"顺手重放一下"），`torque_control_offered` 仍是 false、`hold_policy` 原样上报。SIGCONT 后流重新 live，而新的 `last` 的 `wall` 前进了、载荷也变了（夹爪 50.377→52.859、`pre_clip_rad` 变化）——恢复是**新目标**，不是卡住那一刻的位姿。上位机侧照常收尾：驱动 8 项期望 `failed: []`、manifest `raw_ready`、两段 episode 都在；`--follow-seconds 8` 的两次排练都没留孤儿录包进程。

写判据时自己也错了一次，值得记下来：第一版断言读 `control.teleop.last.seq`，而这个块里没有 seq（只有 `pre_clip_rad/gripper_percent/clutch/calibration_version/wall`），于是 exit=1 报了个假失败。现在判据是 `target_is_newer()`——`wall` 必须前进**且**载荷必须变化，同一时刻换载荷不算前进；4 条用例钉住（`test_rehearsal_chain_tools.py::UpperStreamStallWitnessTests`，tools 侧合计 **273 passed**）。诚实边界：SIGSTOP 只模拟"发包停了/隧道卡住"，不等于真 SSH 隧道半开；舵机真停在哪仍未观测（无执行器、无回执）；拔主臂 USB、真租约过期两路仍只有单测。取证 `docs/evidence/so101-mobile-teleop-upper-stream-stall-20261103.json`。

### 主臂失联也进了排练链（17:18Z，八.6 的本地部分）

`tools/ros2_collection/local_car_leader_loss_rehearsal.py` 补上六 里第三种死法：会话正在 following、episode 正在录时让主臂快照变成 not connected/not healthy，而车端 agent、租约、录包全都不动。10 项期望全绿：上位机下一次读就 `paused`，原因点名 `gate_lost:leader_not_connected,leader_unhealthy`；此后 seq 一动不动；车端按 `ttl_s=0.6` 的单调时钟自己把臂流判过期（`age_s=2.146`、`live:false`），期间 `control.teleop.last` 一字节没变、`control.active` 仍 true；paused 下直接 engage 被拒（`engage refused: leader_not_connected,leader_unhealthy`）；episode 照常 `raw_ready`、`success=unknown`。

判据里唯一需要现场校准的是多久之内停止发包：环每控制周期查一次门禁，注入到降级之间**允许一个在飞的周期**（机械臂与夹爪各占一个 seq）。同一份代码两次跑出 0 和 2，所以判据写成 `0 ≤ Δseq ≤ 2` 并要求此后静默，而不是假装瞬时——10 Hz 下这个上界约 100 ms，且车端 0.6 s TTL 自己会兜住。工具第一版还把降级瞬间当成车端过期的证据，那一刻按 TTL 本来还在有效期内，于是恒判失败；过期改为取观察窗口末端那次采样，另加降级瞬间到窗口末端 last 不变专门盯重放。诚实边界：合成 close() 不等于真拔 USB（真机先给读超时/串口错误，检测延迟未测），插回后重新接合、真隧道半开、真租约过期仍要上车。取证 `docs/evidence/so101-mobile-teleop-leader-loss-rehearsal-20261103.json`。

### 录制中的急停也演练过了（17:24Z，五/六/七 的交叉项）

`tools/ros2_collection/local_car_estop_rehearsal.py` 在 episode 正在录时按急停，12 项期望全绿：上位机从 following 直接进 `estop`、`last_error=estop`、seq 停在 43 之后不动；车端 `estop_latched=true`、`control.active` 立刻 false、臂流按自己的单调 TTL 过期，从降级那一刻到 clear 之后 `control.teleop.last` 一字节没变（清锁存不等于恢复运动）；急停不拖累录包——episode 照常结束、会话照常 `raw_ready`+READY，原始包 `/lelab/events` 41 条里 2 条含 estop（按下与解除），故障进的是数据而不是页面文案。

两处现场坑写进入证：① 会话侧 `events.jsonl` 的键是 `kind`，而急停事件发在 `/lelab/events` 话题上、只进 sqlite3 原始包——第一版在 events.jsonl 里找 `type/event` 字段，恒判失败；② "锁存后没有再施加"的比较基线必须取状态降级那一刻，不然急停生效前那一个在飞周期（机械臂+夹爪各占一个 seq）会被读成重放。还有一条要如实说明：锁存期间塞进去的 `/control/arm` 回答的是**模式门禁** `409 mode: …`（agent 的 `estop()` 会把 `control_mode` 打回 idle），`estop_latched` 那道专用 409 只有假网关单测覆盖，本次排练没单独命中它，文档没写成命中。限制：无执行器，真舵机停在哪、硬件急停回路与按钮有效性仍要上车。取证 `docs/evidence/so101-mobile-teleop-estop-rehearsal-20261103.json`。

### 上位机退出：第五种死法，并且抓到一个会锁死主臂的缺陷（17:35Z）

`tools/ros2_collection/local_car_upper_exit_rehearsal.py` 在 following + 录制中调用生产 `MobileTeleopCoordinator.shutdown()`（LeLab 服务关闭钩子走的同一个函数）。改前第一跑就把一条真缺陷照出来了：**退出还了双层租约、收了录包，却没有释放主臂串口**——`status.leader.connected` 仍为 true，flock+TIOCEXCL 还在这进程的 fd 上，等于"退出钩子跑完但臂被锁死"，只有重启 LeLab 才能把端口交给别的工具，也与六 的"任一失效即退出并交还"不一致。修法：`release()` 用 try/finally 包住，无论如何都调 `close_leader()`（车端不可达也要放）。

改后 10 项期望全绿：跟随线程停、上位机租约 `idle`（`last_release.reason=rehearsal-server-exit`）、车端租约交还且 `control.active=false`、`teleop.stream` 变 `expected:false/ttl:null`（网关视这份会话已结束，不会重放旧目标）、主臂串口释放、退出后再调 `send_once()` 返回 None 且 `control.teleop.last` 不再变化、会话照常 `raw_ready`+READY 保留。seq 43→45 依旧是关闭落在周期边界那一个在飞周期（此后静默）。边界照实写：本排练没穿 uvicorn 生命周期，真 SIGKILL 只能靠车端 TTL 兜底，仍需上车。测试：app `test_mobile_teleop_coordinator.py` 7 passed、tools 273 passed。取证 `docs/evidence/so101-mobile-teleop-upper-exit-rehearsal-20261103.json`。

### 车端租约自己过期：第六格的最后一类，并修掉一个残留持有者名字（17:41Z）

`tools/ros2_collection/local_car_lease_expiry_rehearsal.py` 让上位机停止续租，等车端按自己的 5..300 秒 TTL（本次 `expires_in_s≈29.89`，等了 31.9 s）处置。6 项期望全绿：`control.active` 自己变 false、`teleop.stream` 变 `expected:false`；旧 token 再发命令被拒（实测先到 `403 control_token`，不写成命中 `409 control_expired`）；恢复必须重新 acquire+align，`client_id` 换成新名字后臂流才再 live，且 `last` 是恢复后新产生的目标——过期前排队的目标没有爆出去。

照出的缺陷：过期分支清了 `control_token` 与 `gate`，**却没清 `control_client`**，于是 `active:false` 旁边仍挂着旧持有者名字，而页面与协调器的独占判定读的正是 `control.client_id`（主动 release 路径本来就清，唯独过期路径漏）。修在 agent 的过期分支。另一个流程教训也写进取证：第一次复跑仍看到旧名字，是因为那个 agent 进程是打补丁之前起的——**改完部署侧代码必须重启被排练的进程**（这条与 [[prove-which-code-ran]] 同一类）。取证 `docs/evidence/so101-mobile-teleop-lease-expiry-rehearsal-20261103.json`；tools 273 passed，排练 agent 按 pid 停掉。


## 交接（17:43Z，本机侧已尽力，等硬件与两个决定）

工作区状态：分支 `codex/ros2-mobile-collection` 领先 `origin/main` 77 个提交，**本轮 12 笔全部只在本地，没有推送**；`git status` 除用户自己的两个未跟踪 `releases/training-monitor-*` 目录外干净；没有遗留排练进程（18115 未监听），用户 18186 服务未动。

要继续必须先满足的硬件条件：① 小车充满电并起 `MentorPi` 容器（`docker start` 属于现场授权）；② SO-101 主臂接到**这台上位机**的 USB（当前 `/dev/serial/by-id` 不存在，所以八.3 起所有项都无从下手）；③ 臂栈启动与运动放行需要现场授权（`start_car_arm_stack.sh --apply --i-authorize-board-writes`）。上车后按 §5.2 的 -1.5 → 1 → … → 8 顺序走，只读优先，逐项把车端 `/status`、`manifest.json`、页面截图存进 `docs/evidence/`。

需要用户拍板的两件事（会改变交付形态，不是我能替你定的）：
1. `frontend/src/lib/` 里有 17 个文件（含 apiClient 的错误对象渲染修复）被根 `.gitignore` 的宽泛 `lib/` 规则挡住，要不要收窄规则把它们纳入版本管理；
2. 桌面录制器 `record.py` 目前仍把主臂绝对读数写成 action，是否改用与小车/桌面跟随一致的离合+相对增量语义——这会改变今后数据集的 action 含义，属于数据契约决定，不该悄悄改。

仍未验证的清单（不写成已验收）：§八.3–八.8 的实机项、真拔 USB 后插回重连、真 SSH 半开、真 SIGKILL/整机断电、硬件急停回路与按钮有效性、以及"停止发包后舵机确实停在最后指令位置"这句 `hold_policy`。
