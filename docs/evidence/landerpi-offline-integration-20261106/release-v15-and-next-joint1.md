# LanderPi 离线加固候选 v15 与下一次 joint1 真机验收

日期：2026-11-06（工作机）。本轮只做离线开发：未连接 Pi、未打开 `/dev/rrc`、未发送动作、
未修改/构建/烧录 STM32。参考树保持原样。

## 1. 候选与哈希

### 1.1 车端/上位源码候选
```text
archive : work/ros2_collection/hardware-preflight-20261106/package/landerpi-arm-homing-v15-20261106.tar.gz
sha256  : 1d18d616c55131f21a1cc3f3ff91625ccea388f2d41ba228e10730126741d413
manifest: .../landerpi-arm-homing-v15-20261106.manifest.json
closure : car_package_imports_shipped=true, car_package_modules=17, files=43
```
`working_tree_is_clean=false` 是本轮要求保留未提交修改的结果；manifest 对每个文件逐哈希，
不依赖 `git archive HEAD`。

### 1.2 版本化 vendor（板卡 owner，两文件）
```text
bundle  : .../landerpi-vendor-readback-v3-20261106.tar.gz
sha256  : 9552507500c283064faee1c1d1053b17cb418f2f721161864d28d9d65db01b97
manifest: .../landerpi-vendor-readback-v3-20261106.manifest.json
sdk     : cdc04f097beaf4cfaa0ebc739dd21aec476f0b75709f7aa868db07ddb8747b2b (未变，仍为 load=0x0C)
node    : 8da2f4b7ab2d0f1e69fb2542670cc344607283e0df601116c5ab3e312d914537 (新增 v2 反馈)
```
补丁链（从只读参考树全新副本开始）已实测可逐字节复现 v3：
`patch_guard.py apply` → `patch_guard_torque.py apply` → `patch_guard_owner.py apply`，
退出码均 0，最终两文件哈希与 v3 一致。vendor 包内同时携带 0001–0005 补丁与三个 guard。

### 1.3 本轮变更文件（SHA256，完整清单见 `source-sha256-postchange.txt`）
```text
984f027f...  ros_servo_output.py        逐 ID 力矩 requested→verified/failed；watchdog 首帧起算
41c34c88...  ros_servo_readback.py      v2 owner 实例/代际；拒绝 v1/未知版；退役实例迟到帧；有界力矩读回
2faafd41...  car_control_process.py     owner 变化立即撤权；invalidate torque；state topic/service 参数
4e5e21fe...  agent_licence_bridge.py    进程 status 转发 homing_torque_state/results/error
308e0fe0...  hybrid_hardware.py         反馈 envelope 透传 publisher instance/generation
7d9b2b82...  hybrid_stream_hardware.py  回中 owner 绑定、pending 清理、方向/振荡/漂移判别、emitter-ready 等待
2493fbdd...  LanderpiTeleop.tsx         only_joint 选择器、逐轴加载确认、反馈年龄/代际、停止原因
4e6e8171...  sim_board_owner.py         v2 反馈、力矩写/读服务、坏 ID/owner 重启/初始位姿故障旋钮
1402dc8b...  hardware-hybrid-homing-v1-offline.json  （新增，离线专用 homing profile）
723cdbb2...  homing_offline_diagnostic.py            （新增，连续诊断）
8830b21a...  homing_offline_diagnostic.sh            （新增，隔离装置启动/收尾）
0585fb76...  homing_web_probe.py                     （新增，浏览器点击 harness）
```
参考树哈希、msg/srv/命令码/单位契约见同目录 `contract-and-hash-manifest.md`。

## 2. 本轮验证（命令、环境、结果）

| 检查 | 环境 | 命令 | 结果 |
|---|---|---|---|
| 上位 homing/readback/输出 | app_ros_mobile/.venv (3.12) | `pytest tests/test_hybrid_stream_hardware.py tests/test_hybrid_home_plan.py tools/.../test_ros_servo_output.py` | 64 passed |
| 车端输出/控制/许可/帧契约 | Humble 3.10.12 容器 | `pytest test_ros_servo_output.py test_car_control_process.py test_car_emission_identity.py test_agent_licence_bridge.py test_teleop_frame_v1.py` | 117 passed |
| 连续离线诊断 | 隔离 docker 装置（domain 94 / 18123 / 18131） | `tools/ros2_collection/homing_offline_diagnostic.sh` | passed, exit 0 |
| 浏览器点击 | 生产 dist + 诊断 facade（模拟 leader） | `/landerpi/teleop`：acquire → 选 joint2 → 回中 → 释放 | 见 `browser-click-evidence.md` |
| 前端类型/构建 | node v22.20.0 | `npx tsc --noEmit && npm run build` | tsc 干净，build 成功 |
| vendor 补丁链 | /tmp 副本 | 三个 guard `apply` | 0/0/0，哈希与 v3 一致 |

连续诊断报告：`homing-offline-diagnostic-20261107T064022Z.json`（status=passed）。
覆盖：只读预检（torque=idle、publish_count=0、outputs=off）→ 新会话（bundle 匹配）→
有界心跳 → only_joint joint1 完成（six-ID torque verified）→ only_joint r_joint 启动后取消 →
释放（心跳停止、control inactive、outputs off）。模拟身份 `simulated_offline`，未触物理串口。

## 3. 启动 / 停止 / 回退（离线装置）

```bash
# 一次性端到端（自动建独立容器、跑完即收）
tools/ros2_collection/homing_offline_diagnostic.sh
# 产物: docs/evidence/landerpi-offline-integration-20261106/homing-offline-diagnostic-*.json

# 手工复用同一装置（仅调试）
LROS_WORK_DIR=$PWD/work/ros2_collection/homing_offline/local_humble \
LROS_CONTAINER=lelab-homing-offline LROS_DOCKER_DOMAIN_ID=94 \
LROS_DOCKER_STREAM_PORT=18123 LROS_DOCKER_AGENT_PORT=18131 \
LROS_DOCKER_PROFILE=/work/lelab/ros2_ws/src/lelab_mobile_agent/config/hardware-hybrid-homing-v1-offline.json \
LROS_DOCKER_BUNDLE=<print-bundle 输出> LROS_DOCKER_BOARD_RAW=joint1=527,joint2=632,joint3=76,joint4=118,joint5=907,r_joint=457 \
  tools/ros2_collection/local_ros2/bin/docker up
# 停止（只停本容器）
LROS_WORK_DIR=... LROS_CONTAINER=lelab-homing-offline tools/ros2_collection/local_ros2/bin/docker down
```
隔离性：独立容器名、domain 94、端口 18123/18131、独立 `LROS_WORK_DIR`；镜像内无 pyserial，
无 `/dev` 映射；所有 topic 在 `/lelab_offline` 下。现有 `lelab-humble-dev` 容器未被操作。

## 4. 车端部署映射（下次上车时）

1. 解包 v15 到新目录（不要覆盖在跑的目录），核对 manifest 逐文件哈希。
2. 解包 vendor v3 到版本化目录，**不要覆盖** `vendor/ros_robot_controller_t1_20261107/`：
   `/home/ubuntu/shared/lelab_car/vendor/ros_robot_controller_readback_v3_20261106/`。
3. 启动板卡 owner 前把该目录置于 `PYTHONPATH` 首位；用 `start_car_arm_stack.sh` 的
   `vendor_sdk_import` 探针确认导入路径指向新副本（不是 install 里的旧包）。
4. 验证 v2 反馈：`ros2 topic echo /lelab/ros_robot_controller/servo_readback --once`，
   `data[0]==2`、`data[1]` 为非零 owner 实例、`len(data)==16`。
5. 控制进程新增参数（硬件必须显式给真名）：
   `--state-topic /ros_robot_controller/bus_servo/set_state`
   `--state-service /ros_robot_controller/bus_servo/get_state`
   `validate_hardware_trial` 会拒绝错误值。
6. 启动顺序：板卡 owner → 控制进程 → agent；使用新的状态目录，不复用旧 lease/旧状态文件。
7. 回退：同时回退 **控制/agent 代码 + vendor**。只回退其中一侧会造成 v1/v2 反馈格式不匹配，
   消费端会以 `readback_format_unsupported:v1` 失败关闭（不会动）。
   回退到 v14 + `vendor_t1_20261107` 后重新确认反馈与 torque 状态。

## 5. 下一次 joint1 真机验收入口

前置：SSH/Pi 可达、MentorPi 容器、`/dev/rrc` 无其他 owner、机械臂有支撑/急停可达、
六路反馈新鲜（v2，owner 实例存在）、`homing_torque_state=idle` 且 `torque_publish_count=0`。

1. 获取新 session；确认 `feedback.publisher_instance` 非空、`publisher_generation=0`。
2. 网页“回中范围”选 `joint1`，点击“开始从臂回中（真实运动）”。
3. 观察逐轴加载确认：六个 ID 必须 `verified`；任一 `failed/readback_timeout/identity_mismatch`
   → 不产生位置输出，立刻停止并检查（供电/板卡 owner/服务）。
4. 首个位置帧只允许 ID1，`abs(step) ≤ 14 raw`；joint1 必须朝 500 单向收敛。
5. 出现以下任一即判失败并停止（系统自身也会报）：`homing_reversed`、`homing_oscillating`、
   `homing_no_progress`、`homing_unselected_drift:<axis>`、`homing_feedback_owner_changed`、
   `homing_frame_refused:*`。不要放大步长/限位/TTL 重试。
6. 完成后确认 `outputs_enabled=false`；点击释放，确认 control inactive、outputs off。
7. 故意重启板卡 owner 一次：预期 `homing_feedback_owner_changed`/emitter 撤权、pending 清空、
   不自动恢复运动；必须重新 hello + enable + 重新加载/读回 torque 才能再动。

`torque=1` 只证明加载位读回成功，不证明供电、机械阻力、实际输出力矩或物理急停。
六轴回中、主从同步、6D 采集导出仍未在本轮达到实机验收。

## 6. 明确未完成 / 未验证

- 未连接 Pi、未启动 v15、未执行任何真实运动；v14 在 Pi 上仍只是已上传未启动。
- 未核验车端实际运行字节与已烧录 STM32 固件哈希；参考树 ≠ 部署 ≠ 烧录。
- MCU 无事务 ID / boot ID / 写命令回执 / 批量力矩；读回可能被 MCU 误标来源（见契约文档）。
  本轮未虚构这些能力，Python 层保持 fail-closed。
- arm64 构建/导入未执行（只在 amd64 Humble 容器验证）。
- 浏览器点击使用模拟 leader + 模拟板卡，非实机页面验收。
- 真实停止、机械保持、物理急停、断联停机仍待真机。
