# SO101 本地双臂采集：实现与验证交付

日期：2026-10-31
状态：代码、合成写入器、mock 页面交互已验证；**未进行四台实机、真实相机或实机运动验收**。
基线提交：`07216e0`（主仓库 `main`，`current -> releases/sanitized-20261024-01`）
开发副本：`/home/liaozhengyan/Desktop/lelab-bimanual`（git worktree，分支 `feat/local-bimanual`）
本地 LeRobot：0.6.2 定制源码；应用仍通过 `../lerobot` 可编辑依赖使用该源码。未切换主仓库 `current`，未提交、推送或发布。

## 1. 参考实现与固定版本

| 项目 | 固定版本 | 采用内容 |
| --- | --- | --- |
| Hugging Face LeRobot | `3f797de5952432284a7c77ad2760218cb74e2f26` | `BiSOFollower` / `BiSOLeader`、`record_loop()`、episode 生命周期、`VideoEncodingManager` 收尾 |
| OneRobotAI/LeLab | `9a182993a2a9a3b04312c13ff8365f68d379fe80` | 网页事件适配、录制期间共享相机缓存 |
| vr-teleop-kit | `80589ee26474d0ed12b98b9c5315782446c76c23` | 浏览器控制 episode 的事件接入方式（未引入 VR 依赖） |

本实现基于本地 0.6.2 源码做最小扩展，没有用上游新版覆盖本地安全定制。

## 2. 实施顺序（实际执行）

1. 核对基线/`current`/本地 LeRobot 与参考副本，建立独立 worktree。
2. 修复 `BiSOFollower/BiSOLeader` 的安全参数转发、子设备标定身份保留、相机键合并；扩展 `record_loop()` 的可选回调与动作语义。
3. 集中实现 rig 校验与配置构造；接入 `record.py`、`teleop.py`、远程推理配置。
4. 修正 Stop/超时/重录/收尾/租约顺序，接入 `VideoEncodingManager`、finalize 后重开校验和旁路诊断。
5. 增加专用双臂 setup/录制前端、双臂快照和相机健康显示，构建 dist。
6. 运行相关测试、合成 LeRobot writer、浏览器 setup/mock 录制状态验证，记录证据。

## 3. 修改清单

### 新增

- `app/lelab/rig.py`：四设备配对/串口去重/角色目录/标定指纹校验；集中构造单臂与双臂 LeRobot 配置；12 维关节契约与快照拆分。
- `app/lelab/session_diagnostics.py`：标准数据集之外的旁路诊断（真实单调时间、相机年龄、周期耗时、原始示教目标与安全裁剪后的下发目标）。
- `app/frontend/src/pages/BimanualRecording.tsx`：双臂 setup 页面（选择左右两组设备、四设备核验、显式相机扫描/预览、启动）。
- `app/tests/test_bimanual_recording.py`：13 个双臂配置、清理、生命周期、远程配置与真实写入器测试。
- `app/deploy/local/run-bimanual.sh`：回环绑定、独立数据根的本机启动入口。

### 后端

- `app/lelab/record.py`
  - `RecordingRequest` 支持 `bimanual`，拒绝单/双臂混用、四串口重复、左右同标定、相机设备重复。
  - 采集循环使用一个 `record_loop()`：左右 follower 状态 + 左右 leader 目标 + 全部相机进入同一 episode。
  - 双臂数据集 `action` 保存 `send_action()` 返回的**安全裁剪后下发目标**；原始示教目标写入旁路诊断。单臂保持旧语义（teleop 管线输出），不静默改变旧数据集。
  - 超时、Web Stop、保存本集均提交当前有效集；只有显式“丢弃重录”调用 `clear_episode_buffer()`。
  - `VideoEncodingManager` 在正常/异常退出时收尾；`finalize()` 后重新打开数据集校验 12 维 names/shape、episode 索引和多路视频解码，完成前不显示保存成功。
  - 记录 `finalizing/verifying/completed/failed` 状态；重复 Stop 幂等。
  - 逐子设备非交互连接（bus → calibration → camera → configure），部分失败释放已打开设备。
- `app/lelab/utils/devices.py`：双臂递归清理；相机对象去重；失败时强制关闭已打开串口/相机。
- `app/lelab/teleoperate.py`：双臂遥操作复用同一配置构造/连接顺序；左右关节一次读取后分两路广播；新增标定互斥检查。
- `app/lelab/rollout.py` / `remote_inference.py`：双臂推理配置、12 维状态/动作 + 相机 + processor + normalization 契约校验；单臂 ACT（6 维）被明确拒绝；本地双臂推理明确未实现。
- `app/lelab/server.py`
  - `/stop-recording` 只受理停止，**不再提前释放租约**；租约由活动探测在 worker 收尾/断开硬件后归还。
  - `/emergency-stop` 先发停止信号再抢占租约。
  - 新增 `/bimanual/validate`、`/recording-save-episode`。
  - 标定启动拒绝录制/遥操作/推理进行中的会话。

### LeRobot 本地定制（保留 `../lerobot`）

- `bi_so_follower.py`：转发 `max_position_deviation`、`calibration_range_margin`；优先使用子配置的 `id/calibration_dir`；`robot.cameras` 对臂级相机加 `left_/right_` 前缀，避免同名覆盖。
- `bi_so_leader.py`：优先使用子配置 `id/calibration_dir`。
- `bi_so_*` 配置：子臂类型改为自带 `id/calibration_dir` 的配置，支持 CLI/远程客户端保留实体标定身份。
- `lerobot_record.py`：新增可选 `cycle_callback`、`save_sent_action`；Stop 在采样前生效。默认值保持旧行为。
- 没有修改 `robots/so_follower` 的安全限幅实现。

### 前端

- `/bimanual` 页面：左右两组设备选择、四设备核验（端口/标定文件/sha256/角色）、配置错误显示、显式相机扫描按钮、启动保护。
- `/recording`：双臂同周期关节快照、双 URDF 视图、相机健康（断连/陈旧/年龄）、最终收尾状态、保存/丢弃/结束并保存语义；只有 `finalize+verify` 完成后才进入上传。
- `UrdfViewer`/`useRealTimeJoints`：按 `left/right` 选择同一 WebSocket 消息中的臂数据。
- 生产构建已更新：`frontend/dist/index.html` 引用 `assets/index-Dj2BtAlc.js`、`assets/index-DFJPKW8-.css`。

## 4. 数据契约

- 固定 12 维顺序：`left_shoulder_pan.pos, left_shoulder_lift.pos, left_elbow_flex.pos, left_wrist_flex.pos, left_wrist_roll.pos, left_gripper.pos`，随后同样的 `right_*` 六项。
- `use_degrees=True` 时五个关节为相对标定零位的**度**，夹爪为 0–100；前端只在视图层转弧度。
- `observation.state`：follower 实测位置。
- `action`（双臂）：`BiSOFollower.send_action()` 返回的安全裁剪后下发目标（`sent_target_after_local_safety_clamps`）。这是命令，不是电机已执行证明。
- 原始 leader 目标与逐帧真实采样时间/相机年龄/周期耗时写入 `LELAB_SESSION_DIAGNOSTICS_DIR/<dataset>/<stamp>_<pid>/frames.jsonl`；`session.json` 记录 schema、单位、标定 sha256、安全参数与动作语义。
- 标准 `timestamp` 仍是 LeRobot 的 `frame_index / fps`；诊断时间不覆盖它。
- 左右总线按左后右顺序读取/发送，只声明为**软件同步**，不宣称硬件级同时采样。

## 5. 保存 / 重录 / 结束语义

| 操作 | 行为 |
| --- | --- |
| 保存本集 | 停止当前采集段；有效缓冲 `save_episode()`；进入复位/下一集 |
| 丢弃重录 | 仅 `clear_episode_buffer()`，不增加集数；之前已保存集保留 |
| 正常到时 | 保存当前有效集，再进入复位 |
| 结束并保存 | 保存当前非空有效集 → 收尾编码/parquet/meta → 重新打开校验 → 显示成功 |
| 急停/失联 | 先停止动作输入；worker 尽力保存/收尾并记录错误；不会把异常集标成成功 |
| 重复 Stop/Save | 幂等；不会双重保存 |

`finalize()` 只落盘已保存集，不代替 `save_episode()`；未保存的当前集在崩溃时可能丢失，已在设计中如实说明。

## 6. 本机隔离与运行

隔离根默认 `~/.local/share/lelab-bimanual`（`LELAB_BIMANUAL_HOME` 可覆盖）；独立 Python 环境为 worktree `.venv`（从已验证 CPU 环境复制，未修改共享 site-packages），运行时通过 `PYTHONPATH` 强制导入本 worktree 的 `app`/`lerobot`：
- `hf/calibration/teleoperators/so_leader/*.json`：四个 leader 的独立标定
- `hf/calibration/robots/so_follower/*.json`：四个 follower 的独立标定
- `diagnostics/`：旁路诊断
- 默认只绑定 `127.0.0.1`；本次验证使用未占用的 `18181`。

```bash
cd /home/liaozhengyan/Desktop/lelab-bimanual
LELAB_PORT=18181 \
releases/sanitized-20261024-01/app/deploy/local/run-bimanual.sh
```

打开 `http://127.0.0.1:18181/bimanual`。页面加载只读取已保存设备记录，不连接串口/相机；相机扫描必须点击“配置并预览相机”。四台设备仍沿用现有单设备标定页逐台完成，不在本功能中新增并行标定调度器。

## 7. 验证结果

### 自动化测试（合成/单元）

命令：

```bash
PYTHONPATH=<worktree>/app:<worktree>/lerobot/src <env>/bin/python -m pytest \
  <worktree>/app/tests/test_record.py \
  <worktree>/app/tests/test_teleoperate.py \
  <worktree>/app/tests/test_remote_inference.py \
  <worktree>/app/tests/test_server.py \
  <worktree>/app/tests/test_hardware_lease_api.py \
  <worktree>/app/tests/test_bimanual_recording.py \
  <worktree>/app/tests/test_safety.py \
  <worktree>/app/tests/test_calibration_limits.py \
  <worktree>/app/tests/test_smoke.py -q
```

结果：**109 passed**（2026-10-31，CPU 环境）。新增双臂测试 14 项全过，包括：
- 配置拒绝重复串口/左右同标定/重复相机设备；子臂保留标定 id 与三项安全参数。
- 双臂部分连接失败释放全部已打开资源；共享相机只连接一次。
- Stop 幂等且不清空当前集；租约在 Stop 后仍被持有。
- 远程双臂客户端命令使用 `bi_so_follower`、左右子 id 与左右独立安全参数；6 维单臂契约被拒绝。
- **真实 LeRobot 写入器**：合成双臂设备 + 三路合成图像，写 2 个完整集 + 1 个部分集（3/3/2 帧），`finalize()` 后重新加载：3 episodes、8 frames、state/action names 为固定 12 维、action shape `(12,)`、三路视频成功解码且颜色通道正确；`action` 为裁剪后目标（105.0），诊断 JSONL 8 行、manifest 记录动作语义。

Ruff：改动文件全部通过。

### 前端

- `tsc --noEmit`：通过。
- `npm run build`：通过；dist 已更新（Vite 8.0.16）。
- 浏览器（本机回环、隔离数据根）：
  - `/bimanual` 加载不连接硬件；左右记录选择/四设备核验显示四个 sha256；删除一份标定后核验显示明确错误；相机扫描保持折叠直至点击（点击前无 `/available-cameras` 请求，点击后恰好一次）；未配置相机时开始按钮禁用。
  - `/recording`（mock API，仅验证 UI 状态，不连接设备）：显示左右同周期关节值、相机年龄、`保存本集并继续`；保存后进入 `RESET — GET READY`；菜单“丢弃本集并重录”触发 `POST /recording-rerecord-episode 200`；Stop 后依次显示 `STOPPING — SAVING CURRENT SEGMENT`、`VERIFYING DATASET`，完成并校验后才跳转上传页（2 集）。
  - 3D URDF 视图在 mock 浏览器会话显示 `Disconnected`（mock WebSocket 未被该隐藏视口连接）；同周期数值姿态来自状态快照已验证。真实 WebSocket 广播路径已实现，但未在实机浏览器中验证。

### 未验证 / 阻碍

- **无四台实机、真实相机或实机运动验收**：本机未连接/占用任何串口或相机；所有设备、图像、视频均为合成/mock。
- 未验证真实 `max_position_deviation` / `calibration_range_margin` 在四臂上的方向、范围与限幅效果；需现场授权、支撑、低速小幅测试和急停验证。
- 未验证真实相机 USB 带宽、四串口周期、掉线/重连、磁盘写入与长时间多集稳定性。
- 无兼容的双臂 12 维策略 checkpoint；未训练、未下载、未启动远程 GPU 或付费资源。单臂 ACT 不能用于双臂。现有 async 动作块传输也不等同于 RTC；本阶段没有改动或宣称 RTC。
- 远程 PolicyServer 未实际启动；仅完成配置构造和契约校验。
- 全量 40+ 测试文件的一次运行在 10 分钟内未完成（与本次改动相关测试组 109 项已全过）；未把全量挂起解释为通过。
