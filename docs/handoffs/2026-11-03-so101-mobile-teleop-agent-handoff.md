# SO-101 主臂遥操作小车机械臂 —— 开发进展与接手说明（2026-11-03）

> 这份文件是给**下一个 agent/人**的交接：当前做到哪、证据在哪、还缺什么、下一步按什么顺序做，以及一段可以直接粘进新会话的 Goal Prompt。
> 工作区：`/home/liaozhengyan/Desktop/lelab`，分支 `codex/ros2-mobile-collection`。

## 0. 一句话状态

**软件侧与"无硬件可验证"的部分已经做完并留证；目标尚未达成**——§八.3–八.8 的实机项全部需要一个已充满电的小车、接在**这台上位机**上的 SO-101 主臂，以及现场的运动授权。不要把这些写成已验收。

## 1. 仓库状态

- 分支 `codex/ros2-mobile-collection` 领先 `origin/main` 约 77 个提交，**全部只在本地，未推送**。
- 本轮（2026-11-03 下午）的关键提交：
  - `abd5017` 排练链修好：示例值门禁不再打死排练播种；收尾脚本只认正在应答的 agent（不再信旧报告）
  - `8fa7072` / `f415fb1` 车端 agent 计划内停止：干净退出码 0、判据看 context 而不是异常类名
  - `a30e775` 上位机停止发包排练（车端仍在运行）
  - `e1877b1` 主臂失联排练
  - `7de8219` 录制中急停排练
  - `f525bc8` 上位机退出时释放主臂串口（真缺陷）
  - `bacb4c0` 租约过期后不再挂旧持有者名字（真缺陷）
  - `25057d8` 交接块
- `git status` 只剩用户自己的未跟踪目录 `releases/training-monitor-20261030-01/`、`-02/`；**不要动它们**。
- 没有遗留排练进程；`127.0.0.1:18115` 未监听；用户的 `127.0.0.1:18186` 服务一直没被碰。

## 2. 目标与验收映射（原始需求 → 现状）

| 需求 | 现状 | 证据 |
| --- | --- | --- |
| 一 保留既有成果 | 只读采集、旧 session、invalid session 未改动；不推、不删 | `docs/evidence/so101-mobile-teleop-*` |
| 二 模块边界 | `SO101LeaderReader`（`app_ros_mobile/lelab/mobile_leader_reader.py`）、`MobileFollowerAdapter`（协调器内网关适配）、`TeleopMapper`（`mobile_teleop.py`）、协调器（`mobile_teleop_coordinator.py`）、网页向导（`MobileTeleopPanel.tsx`） | tools 测试 |
| 三 真实从臂反馈 | 车端反馈新鲜度/年龄/TTL 已实现；**真车反馈未验** | 真机项 |
| 四 三层标定 | A/B/C 层原子保存 + 版本/哈希绑身份；示例值/错误身份拒绝 | `test_mobile_teleop_examples.py` 等 |
| 五 防跳变 | 状态机 + 离合 + 相对增量 + 限幅 + 回绕；预览零输出 | 排练链 `rehearsal_report`、`test_desktop_follow.py` |
| 六 控制权/失联 | 六种失效在**部署代码**上本机演练；真机项待做 | 见 §4 |
| 七 网页与录制 | 页面字段齐全；action=网关实发目标、state=实测；6 维、无底盘列；成功标注 manual/unknown | `chain2.json`、`test_ros_mobile_export_schema.py` |
| 八 验证与交付 | 1/2/7（本机部分）已做；3/4/5/6/8 需硬件 | 本文件 §5 |

## 3. 已实测的数字（可直接引用）

- 全链排练（部署用车端代码 + 生产 `ros_mobile.sync_session / export_session`）：退出码 0、`expectations.failed: []`；SDK 读回 2 集 / 60 帧 / `state=action=6` 维 / 仅 `wrist_real`；`action_source=gateway_sent_target_after_clamp`；`success_labeling=manual_or_unknown_no_simulator_ground_truth`；页面能按 `from_s/to_s` 在本集 mp4 区间定位（0–1.933 s / 1.933–4.0 s）。
- 写保护照旧：重复同步 `already_synced`（重新比对 sha256），重复导出 `409 dataset already exists`；未绕过。
- 失效演练（全部在部署代码上，判据 `failed: []`）：
  - 停止发包 2.5 s：车端 0.6 s 单调 TTL 自判过期，`last` 一字节不变，SIGCONT 后是新目标不是重放；
  - 主臂失联：`paused` + `gate_lost:leader_not_connected,leader_unhealthy`，seq 冻结，重接合被拒并点名；
  - 录制中急停：`estop_latched`、`active:false`，清锁存不恢复运动，原始包 `/lelab/events` 里留下按下与解除；
  - 上位机退出：双层租约交还、主臂串口释放、车端 `teleop.stream` 变 `expected:false`；
  - 租约过期：`active:false` 自己发生，旧 token 被拒（`403 control_token`），恢复必须重新 acquire+align。
- 测试面：tools `273 passed, 5 subtests`；app 全量 `583 passed / 1 known failed`（`tests/test_remote_inference.py::test_client_command_targets_the_tunnel`，与遥操作无关，早先即为已知问题）；`test_mobile_teleop_coordinator.py` 7 passed。

## 4. 本机可重复的七条命令（runbook §5.2）

```bash
# 0) 起排练（另开终端，长驻）
work/ros2_collection/envs/ros_jazzy/bin/python tools/ros2_collection/local_car_rehearsal.py --root /tmp/lelab_car_rehearsal
# -1 整链：驱动 + 收尾（同步/导出/读回）
app_ros_mobile/.venv/bin/python tools/ros2_collection/local_teleop_session_drive.py --root /tmp/lelab_car_rehearsal --follow-seconds 3
LELAB_ROS_AGENT_URL=http://127.0.0.1:18115 LELAB_ROS_COLLECTION_ROOT=/tmp/lelab_car_rehearsal/upper \
  app_ros_mobile/.venv/bin/python tools/ros2_collection/local_full_chain_sync_export.py --root /tmp/lelab_car_rehearsal
# 7b 上位机停止发包 / 7c 主臂失联 / 7d 录制中急停 / 7e 上位机退出 / 7f 租约过期
app_ros_mobile/.venv/bin/python tools/ros2_collection/local_car_upper_stream_stall.py    --root /tmp/lelab_car_rehearsal
app_ros_mobile/.venv/bin/python tools/ros2_collection/local_car_leader_loss_rehearsal.py --root /tmp/lelab_car_rehearsal
app_ros_mobile/.venv/bin/python tools/ros2_collection/local_car_estop_rehearsal.py       --root /tmp/lelab_car_rehearsal
app_ros_mobile/.venv/bin/python tools/ros2_collection/local_car_upper_exit_rehearsal.py  --root /tmp/lelab_car_rehearsal
app_ros_mobile/.venv/bin/python tools/ros2_collection/local_car_lease_expiry_rehearsal.py --root /tmp/lelab_car_rehearsal
# 测试
PYTHONPATH=tools/ros2_collection work/ros2_collection/envs/ros_jazzy/bin/python -m pytest tools/ros2_collection/tests -q
cd app_ros_mobile && HF_LEROBOT_HOME=/home/liaozhengyan/.cache/lelab-ros-mobile-tests/hf .venv/bin/python -m pytest tests -q
```

> 排练 agent 只按 marker 里的 `agent_pid`（并先核对 `/proc/PID/cmdline` 与 `ss -ltnp`）停；**禁止 pkill -f**。

## 5. 还没做（必须上车，不能 mock）

1. §八.3 主臂实读与向导交互（串口按 `/dev/serial/by-id`，读数连续、六轴齐全、夹爪 0–100%）。
2. §八.4 真从臂反馈 + 无输出映射预览（预览阶段车端 `/control/arm` 计数不增长）。
3. §八.5 逐关节低速小幅 → 多关节/夹爪；**先确认急停有效**再放行；验证 `hold_policy`（停止发包后停在最后指令位置）。
4. §八.6 真拔 USB、真 SSH 半开、真租约过期、真 SIGKILL/整机断电；恢复不自动接合、不复用旧目标。
5. §八.7 两段短 episode → 校验同步 → 导出 → 数据页读回（判据同 §3）。
6. §八.8 网页每步按钮真点一次失败路径（错身份、拔串口、车端关机、租约被 `inference` 占）。
7. 硬件急停回路与按钮有效性；真机 feedback 频率/超时按总线负载调参。
8. `exclusive_exempt_nodes` 仍为空——上车后用 `ros2 topic info -v` 读 vendor 摇杆/App 真节点名再决定。

## 6. 现场前置（用户侧）

- 小车充满电；起 `MentorPi` 容器与臂栈属于现场授权（`start_car_arm_stack.sh --apply --i-authorize-board-writes` 会写控制板）。
- SO-101 主臂接到**这台上位机**（当前 `/dev/serial/by-id` 为空、24h 内无 USB 插桩事件）。
- 先跑只读前置：`tools/ros2_collection/teleop_preflight.py --pi pi@<车IP> --agent-url http://127.0.0.1:8110 --app-url http://127.0.0.1:<端口>`，退出码 0 才开始。

## 7. 待用户决定（会改交付形态）

1. `.gitignore` 的宽泛 `lib/` 把 `app_ros_mobile/frontend/src/lib/`（17 个文件，含 apiClient 的 `[object Object]` 修复）挡在版本管理外：是否收窄规则并补跟踪。
2. 桌面录制器 `record.py` 仍把主臂绝对读数写成 action：是否改用离合+相对增量语义（会改变今后数据集 action 含义）。
3. 车端 6 号舵机不响应（历史遗留）——是否在本轮一并排查。

## 8. 可直接粘贴的 Agent Goal Prompt

```text
目标：在 /home/liaozhengyan/Desktop/lelab（分支 codex/ros2-mobile-collection）继续 SO-101 主臂遥操作小车机械臂与夹爪的实机验收。软件侧已完成，本阶段只做"上车验证 + 修由此暴露的问题"，不扩功能、不训策略。

先读（按顺序）：
1) docs/handoffs/2026-11-03-so101-mobile-teleop-agent-handoff.md
2) AGENTS.md
3) docs/ros2-collection-runbook.md 的 §5.1 / §5.1b / §5.2
4) docs/milestones/2026-11-03-so101-mobile-teleop-status.md 的"交接"一节
5) docs/evidence/ 下 2026-11-03 的六份 JSON

现状（不要重复做）：
- 全链（设备识别→三层标定→预览→离合相对跟随→录制→校验同步→6维导出→SDK/页面读回）已在部署代码上跑通并留证；
- 六 的六种失效（车端进程被杀、上位机停止发包、主臂失联、上位机退出、录制中急停、租约过期）已本机演练，判据 failed:[]；
- 已修：示例值门禁打死排练、收尾脚本信旧报告、agent 计划内停止留栈/退出码、退出未释放主臂串口、租约过期仍挂旧持有者名字；
- tools 273 passed；app 583 passed / 1 个既知无关失败（test_remote_inference 隧道断言）。

本阶段按 runbook §5.2 的 -1.5 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 顺序执行，每条都先只读、再按用户当次授权放行运动：
- 先跑 tools/ros2_collection/teleop_preflight.py（退出码 0 才继续）；
- 部署/起臂栈需要用户显式授权 --i-authorize-board-writes，运动前要用户现场确认急停有效；
- 每完成一项把车端 /status 片段、manifest.json 片段、页面截图或导出报告存到 docs/evidence/<日期>-<项>.json|png，并区分"实测/仅页面显示/推断"；
- 判据沿用 runbook：预览阶段车端 /control/arm 计数不增长；接合后跟的是"从臂实测+主臂相对变化"；拔线/断隧道/杀 agent/租约过期后必须退出跟随、页面降级、恢复不自动接合且旧目标被丢弃；两集导出 6 维、无底盘列、success=manual/unknown、页面能按 from_s/to_s 定位每集。

硬约束（原样遵守）：
- 沿用已授权认证方式，凭据不得进入脚本、报告或 Git；
- 只管理本任务进程：按记录的 pid 并核对 /proc/PID/cmdline 与端口归属，禁止 pkill -f 等泛用清场；
- 不覆盖用户未提交修改与 releases/ 快照，保留已有 invalid session 不删；
- 禁止把网络可达当成串口可访问；不以 mock 冒充实机验收；
- 最大三个只读子智能体做核查，不让子智能体操作硬件或改文件；
- 发现产品缺陷要先写可红测试再修（变异验证）。

先问我：小车是否已上电、主臂是否已接到这台上位机，以及本次允许做到哪一步（只读 / 预览 / 慢速运动）。
```

## 9. 本阶段新增工具一览

| 工具 | 作用 |
| --- | --- |
| `tools/ros2_collection/local_car_upper_stream_stall.py` | 上位机停止发包 2.5 s：车端自判过期、不重放 |
| `tools/ros2_collection/local_car_leader_loss_rehearsal.py` | 主臂变暗：降级点名 leader、seq 冻结 |
| `tools/ros2_collection/local_car_estop_rehearsal.py` | 录制中急停：锁存、拒命令、事件进原始包 |
| `tools/ros2_collection/local_car_upper_exit_rehearsal.py` | 上位机退出：租约交还、串口释放、不再发包 |
| `tools/ros2_collection/local_car_lease_expiry_rehearsal.py` | 车端租约自过期：拒绝旧 token、恢复须重来 |
| `tools/ros2_collection/local_full_chain_sync_export.py` | 同步+导出+SDK/页面读回（会话 id 从活 agent 解析） |
| `tools/ros2_collection/tests/test_rehearsal_chain_tools.py` | 上述工具的判据（含 4 条"新目标而非重放"用例） |
| `tools/ros2_collection/tests/test_agent_exit_teardown.py` | agent 退出顺序与计划内停止判据 |
