# T1–T5 总集成状态 — 2026-11-04

## 接管与基线

分支 `codex/ros2-mobile-collection`，HEAD `e4312f05eea4f319d7402d570f5b3fd073b04af5`。`current` 指向 `releases/sanitized-20261024-01`；本轮开发目录为 `app_ros_mobile`。未切换链接。五个指定会话已收到停写交接请求；T1、T2、T3、T5 已明确停写，T4 已报告停止修改和测试，最终交接待返回。T1/T5 重复实例归属正在核查，不视为已确认。

基线 `work/ros2_collection/integration-20261104/baseline/`：tracked/staged diff、完整 status、相关 tracked/untracked/ignored 源码和 dist 的 `source-snapshot.tar.gz`、SHA256、HEAD/branch；不包含凭据、环境或运行数据。恢复时先在隔离空目录解包并对照文件，再按单文件人工恢复，不在现有工作区直接覆盖。归档是停写请求后的初始快照，待各方最终确认后复核。

T5 的 87 行骨架在 `docs/evidence/parallel-20261104/T5/baseline/`。另发现 Git 对象 `eafb9bc2affb985ba2e167e82e3b901947369ab1` 含较早完整实现，已另存基线，作者归属尚未证实；常规路径历史不包含该未跟踪文件。当前版本保留 owner、分离数据检查和门禁，早期版本需要逐项对照，未以最后写入时间裁决。

## 问题清单（进行中）

| 实际代码及证据 | 裁决/修改 | 验证 | 状态 |
| --- | --- | --- | --- |
| `ros_mobile.py` control/release 丢弃 session/epoch；旧请求可影响新会话 | 所有普通变更须绑定服务端当前 session/整数 epoch；急停可抢占但不宣称物理停止 | 新旧 epoch、bool、缺字段、急停路径 | 待实施 |
| `hybrid_recording.py` 请求 `/recording/set_phase`，车端 `agent.py` 缺路由；两个独立录制状态 | 对齐控制器和车端真实 recorder、校验租约和 episode | 生产 handler 到事件/manifest | 待实施 |
| T1 要求连续 seq，T3 可丢旧目标；MCU 一个 seq 作用域 | 选单调递增，最终 MCU 单独映射并保持不重放；真实 sink 未接入前禁用硬件输出 | 丢帧、乱序、故障/epoch 切换 | 待实施 |
| 能力缺 profiles/actions；默认 facade 没有适配器；`lib/` 忽略前端源码 | 明确模拟身份与 fail-closed 硬件；最小修正规则 | 路由、浏览器及 git ignore 检查 | 待实施 |
| T2 协议文档与 C action/fault 不一致，T1 未有生产轮快照桥接 | 以已验证代码/向量裁决并补真实跨模块测试，硬件保持禁用 | host harness、向量、补丁生命周期 | 待实施 |

所有历史测试均非本轮验证。无硬件、无 SSH/设备、无服务重启、无训练、不提交推送。

---

## 2026-11-04 总集成接管进展

### 会话冻结与基线

- 通过 `CODEX_HOME=/home/liaozhengyan/.codex codex queue` 向 T1–T5 五个原
  threadId 实际投递停写交接消息；旧协调会话 `01a1a74d-*` 与
  `01a1a77f-*` 也收到停写/只读交接请求。
- `01a1a780-*` 三个会话经 rollout metadata 确认是旧协调会话派生的只读子任务，
  不是新的 T 实现实例；它们未写源码。
- 本轮另按用户明确指令，用 `codex-deepseek
  --dangerously-bypass-approvals-and-sandbox` 在 tmux 启动三个独立 CLI 实施
  会话，文件域分别为上位机路由/会话、车端录制/agent、前端/构建。三者共享
  工作区但文件集合互斥，主会话负责最终整合。
- 接管基线：
  `.migration-backups/landerpi-integration-20261104T181027Z/`。包含 HEAD
  `e4312f05eea4f319d7402d570f5b3fd073b04af5`、tracked/staged diff、
  tracked HEAD archive、全部 2747 个 untracked 条目、`frontend/src/lib`、
  当前 `dist`、集成证据、逐文件 SHA256 和 `RESTORE.md`。

### 已裁决的共享契约

- 会话身份：公开 JSON 使用字符串 `session_id` 和 JavaScript 可精确表示的
  非负整数 `session_epoch`（0..2^53-1）。车端 wire epoch 使用同一整数；不接受
  bool、float、溢出值或静默转换。
- 序号：`(session_epoch, channel, source)` 严格单调递增。允许因 latest-wins
  主动丢弃造成的空洞，不把跳号当传输故障；相同或更小序号才是重放/倒退。
  坏帧不推进序号。T1 supervisor、车端 gateway 和导出校验按此调整。
- TTL：公开 HTTP 只接受 `ttl_ms`; 本机 trusted ingress 生成
  `received_mono_ns/deadline_mono_ns`。同一设备传递保留 deadline，跨设备转发
  只传剩余 TTL；不跨机器比较 monotonic。
- 能力和 profile：`hardware_hybrid_manual_v1` 在无真实证据时保持不可用。
  离线正向验收使用单独命名的 explicit simulated profile/identity；该身份不得
  被报告成真实硬件已验证。
- 录制：`HybridRecordingController` 是本轮录制的单一阶段事实源；
  `/hybrid/status.recording` 应从它读取，不得保留第二套独立 phases。
- 急停：可在普通控制门禁无法通过时抢占；普通 control/release 和录制动作必须
  绑定当前 session/epoch。
- T2 function `0x0C`：保留为候选 T2 能力/轮反馈协议；不证明任何车上固件已
  支持。命令序号同口径为严格递增、允许跳号、禁止重放/回绕，RELEASE=4 清锁存。

### 本轮已实施的主会话增量

- 新增
  `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/wheel_protocol.py`：
  T1 receiver-side T2 frame/CRC/command/snapshot codec。只处理已经由唯一串口
  owner 有界接收并通过 CRC 的字节，不打开串口。snapshot 明确标记为
  `wheel_encoder_readback`，不是车体速度真值；current fault 存在时
  `usable_for_state=false`，不修补原始数据。
- 新增 `tools/ros2_collection/tests/test_wheel_protocol_contract.py`，直接读取
  T2 `golden_vectors.json` 复核 capability、snapshot、count wrap、command
  enable/update/stop/release、boot change、bad CRC、wrong function/length。
- 统一 T1 supervisor、车端 `HybridCommandGate` 和导出 `check_command` 的序号
  语义为严格递增、允许空洞；pending 新目标替换旧目标，重放才退役/拒绝。
- 真实浏览器路径已确认可用：system Firefox 159.0 + geckodriver 0.37.1，
  headless WebDriver 可启动、导航并截图。复用客户端暂存于
  `tools/ros2_collection/webdriver_firefox.py`；最终页面点击证据仍待正式构建。

### 当前验证

- `PYTHONPATH=tools/ros2_collection:ros2_ws/src/lelab_mobile_agent
  work/ros2_collection/envs/ros_jazzy/bin/python -m pytest
  tools/ros2_collection/tests/test_t2_firmware_protocol.py
  tools/ros2_collection/tests/test_wheel_protocol_contract.py
  tools/ros2_collection/tests/test_t1_hardware.py
  tools/ros2_collection/tests/test_t1_wave_a_acceptance.py
  tools/ros2_collection/tests/test_t3_channel_contract.py
  tools/ros2_collection/tests/test_t3_hybrid.py -q -p no:cacheprovider`：
  137 passed, 25 subtests passed, exit 0（Python 3.12 Jazzy venv；不是
  Humble/Python 3.10 runtime 验证）。
- Firefox WebDriver data-URL smoke：导航成功、标题读取成功、截图 9642 bytes、
  browser error 列表为空。该 smoke 只证明浏览器驱动链可用，不是 LeLab 页面验收。

### 尚未完成

- 三个 CLI 实施会话仍在修改各自文件；需收齐后检查差异和失败。
- `/ros-mobile/hybrid/*` session/epoch、能力声明、录制单一状态源和显式模拟入口
  的最终结果尚未验收。
- 车端 set_phase、adapter provenance、simulation profile、command audit
  envelope 尚未完成最终复验。
- 6D/9D 两集离线同步/导出/SDK/页面读回与正式 Vite build、真实浏览器点击仍待执行。

---

## 2026-11-04 单会话总集成接管（主会话实测，18:36Z 起）

自本节点起改为单一主会话逐步开发，未再启动并行实现会话，未派生子智能体。以下全部为本轮实测命令与结果；历史数字只作对照，不计入本轮通过。

### 第 1 步 冻结与审计

证据：[freeze-audit.md](../../evidence/integration-current/main/freeze-audit.md)

- 分支 `codex/ros2-mobile-collection`，HEAD `e4312f0`，相对 upstream ahead 49（相对 main 为 79，口径不同）。tracked 修改 22 文件，`git diff --stat` +2745/−281；未跟踪含 8 个 `hybrid_*.py`、`frontend/src/lib/`、`Hybrid*.tsx`、`hybridContract.ts`、`wheel_protocol.py` 等。未 `reset/clean/checkout --`，未提交，未推送，未切 `current`，两个 `releases/training-monitor-20261030-*` 未触碰。
- 写入者判定：对 6 个源码/证据目录共 **733** 文件做间隔 **75 秒**的 `mtime+size` 双快照，集合与时间完全一致，窗口内零写入；工作区最后写入 18:34:20Z，早于接管。三个 `codex-deepseek` 进程（PID 2981878 等）cwd 在本仓库但 rollout 最后活动 18:21–18:34Z，处于空闲；tmux `work` 内三个 codex 面板 cwd 全为 `pi05-reproduction`，属另一项目。**处置：只存证据、不终止**——它们承载用户交互上下文且未在写入，杀掉属于不可逆且非必需动作。
- 遗留 T5 夹具：PID 2834242→`127.0.0.1:42437`、PID 2856376→`127.0.0.1:34621`，父进程 `systemd --user`（孤儿），脚本绝对路径在本仓库 `docs/evidence/parallel-20261104/T5/mock_hybrid_wavea.py`，docstring 自述测试夹具且服务 `frontend/dist`；两端口无既有连接。按授权以精确 PID `kill -TERM` 停止，复查两进程已退出、端口释放，未用 pkill，未影响 8000/18186 服务与 codex 进程。
- profile 字节一致：`hardware-hybrid-manual-v1.json`（`f0f726bd…`）与 `hybrid_manual_sim_v1.json`（`a40ddbef…`）在 `configs/ros2_collection/` 与 `ros2_ws/src/lelab_mobile_agent/config/` 两处 `cmp` 一致。`.gitignore` 第 17 行宽泛 `lib/` 保留，第 255–256 行最小放行 `app_ros_mobile/frontend/src/lib/`。

### 第 2 步 生产链路审计（以代码为准）

- 单一入口：`ros_mobile.py:1019 get_hybrid_facade()` 走协调器 `get_hybrid_session_facade()/configure_hybrid_facade()`，仅当 `LELAB_HYBRID_SIMULATED=1` 时装入显式模拟夹具 facade，默认无适配器；录制控制 `ros_mobile.py:1076 get_hybrid_recording()` 全进程单例，并经 `bind_recording_controller()` 绑回同一 facade。
- 身份门禁：`hybrid_session.py:92 validate_session_request()` 要求字符串 `session_id` 与非负整数 `session_epoch`（`0..2^53-1`），拒绝 bool/float/越界，不符抛 `session_epoch_mismatch`。release（`:1140`）、control（`:1173`，`allow_estop=(action=="estop")`）、recording（`:1202`）全部走该门禁；calibration 用 `validate_session_request_if_active()`（`:1104`，无会话时允许标定，有会话后同样绑定）。急停为唯一显式抢占路径，且走 `controller.estop()` 不等保存。
- 一致性裁决：`capabilities()`（`hybrid_session.py:488`）声明 `profiles`/`actions`/`profile`，前端 `hybridContract.ts:200` 要求 `available===true || simulated`，`:295` 要求 `status.profile ∈ capabilities.profiles`，`:300/306` 再按 `capabilities.actions` 白名单放行。实测：模拟会话下 `profiles=['simulated_hybrid_manual_v1']`、`actions` 含 preview…export，顶层 `available=false`，`hardware_hybrid_manual_v1` 为 `available=false / hardware_capabilities_unverified`。**同名字段语义已裁决并记录**：`capabilities.channels.*.permitted` 表示“可信硬件驱动是否绑定”（恒 false，除非真硬件验收），`status.channels.*.permitted` 才是当前会话许可（`base_enabled`/`following`）；前端门禁用后者与 profiles/actions，不用前者。此项不改代码，避免把硬件未验收伪装成许可。
- 录制单一事实源：`/hybrid/status` 在返回前调用 `get_hybrid_recording()` 绑定，`hybrid_session.py:80` 的 `_recording_snapshot` 只读控制器；T4 报告里“两处 phases”的双源问题在生产路由已消除。
- 命令事件与确认分级：`contract.py` 的 `HYBRID_ENVELOPE_FIELDS` 白名单 + 车端拒 `received/deadline_mono_ns`；`test_integration_car_phase.py` 断言三通道事件具同一 `session_id/session_epoch/source/seq/ttl_ms/calibration_bundle_hash` 形状，旧 epoch 与缺 TTL 被拒且不消耗序号。`agent.py:223/233` 对 `hardware_hybrid_manual_v1` 强制 `hybrid_no_output` 并压掉 `actuator_outputs`，`:2567` 旧执行端 409，路由 `/hybrid/begin_epoch|command|dispatch|stop`（`:2572-2578`）与 `/recording/set_phase`（`:2619`）齐备；`recorder.py:302 set_phase` 校验同集、时间严格递增、重复阶段拒绝并写 `phase_started/phase_ended`。
- 未见“第二套 phases / 静默零填充 / command_cache 当 measured / 模拟身份当 hardware”的生产路径：`capabilities.trusted_feedback_sources` 仅 `servo_position_readback`，`rejected_feedback_sources` 含 `command_cache/model_placeholder/synthetic/vendor_joint_states`；9D 导出对这些来源一律拒绝（T4 用例覆盖）。

### 第 3 步 6D 锁底盘

- `cd app_ros_mobile && .venv/bin/python -m pytest tests/test_ros_mobile_export_schema.py tests/test_ros_mobile_teleop_export.py tests/test_t4_hybrid_9d_export.py -q`：**29 passed，exit 0**（Python 3.12.16）。
- `PYTHONPATH=tools/ros2_collection:ros2_ws/src/lelab_mobile_agent work/ros2_collection/envs/ros_jazzy/bin/python -m pytest test_t2_firmware_protocol test_wheel_protocol_contract test_t1_hardware test_t1_wave_a_acceptance test_t3_channel_contract test_t3_hybrid test_recorder_faults test_agent_teleop_recording test_integration_car_phase -q`：**173 passed + 25 subtests，exit 0**（Jazzy/3.12，非 Humble/3.10 运行验证）。
- **真实 `ros2 bag record` 未能跑通，阻塞点在 ROS 运行时而非 recorder 或导出**：本机无 `/opt/ros`，`work/ros2_collection/envs/ros_jazzy` 为 pip 轮子，`ros2 bag record --help` 与实录均抛 `RuntimeError: Environment variable 'AMENT_PREFIX_PATH' is not set or empty`；补 `AMENT_PREFIX_PATH` 后改抛 `package 'rosbag2_storage' not found`，未生成任何 bag 目录。日志：[ros2-bag-record-ament-missing.log](../../evidence/integration-current/main/realbag-probe/ros2-bag-record-ament-missing.log)、[ros2-bag-help.log](../../evidence/integration-current/main/realbag-probe/ros2-bag-help.log)。
- 同时纠正一处读数：`test_integration_car_phase::test_real_http_phase_route_appends_to_session_recorder_timeline` 与 `test_recorder_faults` 的 `sleeper()` 用 `FakeBagProcess`／`sys.executable -c sleep` 冒充 recorder 子进程，**它们验证的是生产 recorder 的会话/回合/阶段与孤儿回收逻辑，不是真实 bag 落盘**。因此本轮不存在“真实 recorder 全链通过”的证据，合成 sqlite3 包不得写成真实 recorder 全链（此项列入未完成）。

### 第 4 步 9D 模拟全链（含首次失败与修复复验）

命令：`app_ros_mobile/.venv/bin/python tools/ros2_collection/hybrid_offline_acceptance.py --root /tmp/lelab-hybrid-final`

- 首跑 **exit 1 / status failed**：`sixd_locked_chassis` 已 passed，但浏览器阶段结束后在 `webdriver_firefox.py:240` `os.killpg` 抛 `PermissionError: [Errno 13]`，`except OSError` 只护住 killpg，回退 `terminate()` 未护，导致 `__exit__` 抛错、整轮报告丢失（`report["browser"]` 未写入）。失败证据归档为 [browser-failed-run1/acceptance-report.json](../../evidence/integration-current/main/browser-failed-run1/acceptance-report.json)（SHA256 `0ffee67b…`）、`failure.png fe6c0b4f…`、`geckodriver.log f86b7c93…`、`server-stderr.log 97ec03d5…`；下方第三次通过报告的 SHA256 为 `6ec2a19d…`，两者不是同一文件。
- 第二次复跑同点失败，补 traceback 后定位为根因：geckodriver 来自 **snap**（`/snap/firefox/9137/usr/lib/firefox/geckodriver`），运行在独立 user namespace，宿主侧即便对自己启动的子进程也无法发信号（`kill -TERM 3061929` 同样 Permission denied）。
- 修复（两处，均为测试工具，不改生产语义）：`webdriver_firefox.close()` 对 `terminate/kill` 也捕获 `OSError` 并记 `teardown_error`，不再让无法回收的驱动作废已完成的浏览器验收；`hybrid_offline_acceptance.py` 增加 `failed_stage`/`error_traceback`、`capabilities` 快照、以及截图前 `scroll_to()`。
- 第三次 **exit 0 / status passed**，证据归档在 [main/browser/](../../evidence/integration-current/main/browser/)（`acceptance-report.json` SHA256 `6ec2a19d40834e49a275e91ce7cf4fbc31a46c4b287079ff779186cd610773b7`）：31 次浏览器动作、6 张截图**全部互不相同**（旧 run4 的 `02/03/04` 三张 SHA256 完全相同，属视口未滚动造成的假证据，已在新版修正，旧目录另存 `browser-run4-prior/` 备查）、`browser_errors: []`、`sync.status=already_synced`（浏览器已用生产路由同步）、SDK 读回 **episodes=2 / frames=400 / state_dims=9 / action_dims=9**、阶段视频 `…/episodes/0/video/wrist_real#t=10,20`。
- 身份核对：`capabilities` 顶层 `available=false`、`simulated=true`、`outputs_enabled=false`、`reason=explicit_simulated_profile`；全报告唯一的 `available: true` 位于 `profiles_detail.simulated_hybrid_manual_v1`（模拟档案），硬件档案保持 `false / hardware_capabilities_unverified`。导出报告 `collection/upper/exports/ros_browser_9dsim/export_report.json`：`data_identity=simulated_offline`、`training_eligible=false`、`export_schema.kind=mobile_hybrid_manual_9d_v1 dims=9`、`base_velocity_source=simulated_wheel_odometry_estimate`。
- 残留环境事实：本轮共 4 个 snap geckodriver 无法从宿主信号回收（PID 3057576/3058758/3060788/3061929 及 run3 新增一个），只占本机回环端口，不影响验收结论，需用户自行处置或改用非 snap Firefox。

### 第 5 步 前端正式交付

- `cd app_ros_mobile/frontend && PATH=work/tools/node/bin:$PATH npm run build`（Node v22.20.0，**Vite 8.0.16** 官方链，未用 esbuild 拼装）：**exit 0**，`✓ 2428 modules transformed`，产物 `dist/assets/index-BOEl9nMY.js` `1ce88af7c3e37afce311110f4007ff6834305f7fd2743c292912f393d2587a95`、`dist/assets/index-BiXvMVlz.css` `b640e6bcba345e8e010aafed1ea5a88030e8389ac1ee14ea09d816db61144b52`——与接管前 dist 逐字节相同，证明构建可复现且源码未漂移。
- `./node_modules/.bin/tsc --noEmit -p tsconfig.app.json`：**exit 0**（历史报告里 `Training.tsx:438` 两处 TS2551 已不再出现）。
- SPA 由生产后端实际提供并由真实 Firefox 点击：验收中 uvicorn（`lelab.server:app`）服务当前 dist，`geckodriver.log` 记录页面实际请求 `/assets/index-BOEl9nMY.js`；覆盖标定错误保留与关闭、控制序列（预览/对齐/待命/接合/释放/暂停/锁底盘）、阶段 observe/grasp、两回合、同步、导出取消后重试、`phase-curves` 读回、dataset-viewer 阶段视频、急停/清除/释放。jsdom 与 DOM 静态检查只作补充，不作为本步结论。
- 视频同帧绑定、序号过期、断网与旧 epoch 拒绝由 `test_integration_car_phase.py::test_preview_jpeg_headers_and_status_share_the_same_source_frame`、`test_mjpeg_parts_carry_the_same_source_identity_without_blocking_http`、`test_hybrid_command_rejects_stale_identity_without_consuming_sequence` 及前端 `hybridContract.ts` 门禁在本轮测试中通过（同上 173/29 passed）。**口径澄清**：本轮真实 Firefox 流程实际覆盖的是标定错误保留与关闭、控制序列、observe/grasp 阶段、两回合结束顺序、同步、导出取消与重试、曲线与阶段视频读回、急停/清除/释放；**断网与画面冻结过期两项没有在真浏览器里演**，其证据来自上述自动化测试与旧轮 jsdom 点击，不写成真实浏览器已验证。

### 接口与 schema 裁决（本轮生效）

- 会话身份：公开 JSON 用字符串 `session_id` + 非负整数 `session_epoch`（≤2^53−1），拒绝 bool/float/越界；release/control/recording 必须精确匹配当前会话，calibration 在无会话时放行、有会话后同样绑定；急停为显式抢占且不依赖身份。
- 序号：`(session_epoch, channel, source)` 严格递增，允许 latest-wins 空洞，重复/倒退拒绝，坏帧不消耗序号。
- TTL：公开 HTTP 只收 `ttl_ms`，接收端生成本机 `received/deadline_mono_ns`，跨端只转剩余时效，白名单拒绝伪造本地时钟字段。
- 能力：`capabilities.profiles/actions` 为前端正式门禁，`status.profile` 必须属于前者；`capabilities.channels.permitted`=硬件绑定态，`status.channels.permitted`=会话许可态。
- 确认分级：`requested → gateway_accepted → driver_sent → controller_acknowledged → execution_observed`；无唯一串口 owner 时 `driver_sent=false`，后两级恒 `unavailable`。
- schema：6D `arm_gripper_6dof_phase1`（5 轴 + `r_joint`，底盘锁定不补列）；9D `mobile_hybrid_manual_9d_v1`（action=底盘三命令 + q1..q5 目标 + r 目标；state=已验证底盘估计 + 六路实测），可信来源仅 `servo_position_readback` 与合格估计，缺来源拒绝导出、不补零。

### 复现与回退

- 启动 9D 离线全链：`app_ros_mobile/.venv/bin/python tools/ros2_collection/hybrid_offline_acceptance.py --root /tmp/lelab-hybrid-final`（回环 127.0.0.1 + `free_port()` 动态端口；`--keep` 保留工作目录；退出码 0 为通过）。停止即进程退出，服务线程 `should_exit` 与 `car.shutdown()` 自带收尾；失败时写 `evidence/failure.png`。
- 车端/上位机测试命令见上文第 3 步；前端构建见第 5 步。回退：接管前基线 `.migration-backups/landerpi-integration-20261104T181027Z/`（含 HEAD、diff、2747 个未跟踪条目、`frontend/src/lib`、当时 dist、逐文件 SHA256 与 `RESTORE.md`）；本轮只新增/修改 `webdriver_firefox.py`、`hybrid_offline_acceptance.py` 两个测试工具与证据、本文档，未触碰生产源码，未提交。

### 未完成（不得当作通过）

1. 车端 sqlite3 **真实 `ros2 bag record` 全链**（record→生产 sync→6D 导出→SDK→dataset_viewer）：本机阻塞于 ROS 运行时（缺 ament 资源索引，`rosbag2_storage` 未注册），且现成 recorder 测试用假子进程，需在真车 Humble/Python 3.10 或完整 ROS 安装上补。
2. 真实六路 servo readback、扫描跨度与年龄；T2 固件 ARM 交叉构建与烧录、板端回读；真实底盘反馈与轮序/符号；真实手柄输入；相机端到端（含 JPEG 与 frame_seq 实测同帧）；真实停车/保持能力区分、失联保护、独占控制与硬件急停有效性。
3. 上述均未在本轮验证；本轮的“通过”全部限于无硬件离线链，模拟数据身份为 `simulated_offline` 且 `training_eligible=false`。
4. 环境残留：4+ 个无法从宿主回收的 snap geckodriver 进程；三个空闲 `codex-deepseek` 会话进程（未终止，需用户确认后再处置）。

### 下一轮上车最小动作（只读优先）

先在不发送任何运动的前提下完成设备身份与反馈门禁：板型与 `MACHINE_TYPE`、实际 Python 导入路径与运行版本、控制板固件版本与能力协商、`/dev/rrc` 串口唯一所有者、六个舵机 ID 与真实位置回读、相机身份/分辨率/帧率、底盘轮序与真实反馈通路；随后才安排支撑与急停有效性验证，再谈逐关节低速授权窗口。历史 1 轴卸力矩记录须重新核验，不按“系统正常”重启 init_pose。
