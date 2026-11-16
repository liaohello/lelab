# LanderPi 网页录制流程改造 — 实现与交接

日期：2026-11-08（PST）。分支 `codex/ros2-mobile-collection`。目标页 `http://127.0.0.1:18288/landerpi`。
本轮完成源码实现 + 离线隔离验证 + 真实浏览器点击；**未做任何真机运动验收**。

## 1. 目标主流程（已实现）

获取控制 → 开始数据集 → REC 开始本集 → 标记抓取 → 结束本集 → 保存/丢弃/重录 → 下一集 → 结束数据集（收尾/同步/导出）。
操作员不再点 preview/align/arm/engage/start_session/start_episode 等底层步骤；由后端薄编排层统一编排，保留原有双层租约、TTL、急停、反馈门禁。

## 2. 关键源码差异（本会话）

新增/重写（本任务主体）：
- `app_ros_mobile/lelab/hybrid_capture.py`（新）：`CaptureOrchestrator` 薄编排层。区分 `task_id / session_epoch / attempt_id(=car episode_id) / episode_index`；请求级 `request_id` 幂等（processing/completed/failed，重放返回存根）；`operator_stage` 单一派生；`recording_active` 仅在 attempt 处于 recording **且** 控制身份匹配当前 facade **且** 输出开启时为真（session_id/episode_id 非空不再等于录制）。持久化到 `COLLECTION_ROOT/upper/capture/<task_id>/task.json + journal.jsonl`，重启从磁盘+facade 真实状态恢复。
- `app_ros_mobile/lelab/ros_mobile.py`（M，叠加在既有未提交改动上）：
  - 新路由 `GET /ros-mobile/hybrid/capture/status`、`POST /ros-mobile/hybrid/capture/{action}`，`get_hybrid_capture()` 复用同一 facade/recording 单例，不新建绕过租约的通道。
  - 导出读取车端 `capture_review.json`（上位审核）：`dispositions==saved` 才进入数据集；追加模式用 `LeRobotDataset.resume()` 在私有副本写入、校验后带备份原子换回；`_check_append_target` 用实际生成的 feature names/units/shape 严格比对。
  - `preview.jpg?fresh=true` 必须带 `X-LeLab-Viewer`（匿名返回 400 `viewer_required`），杜绝多页面共享全局帧进度互相 409。
- `app_ros_mobile/lelab/hybrid_recording.py`（M，叠加）：`start_session` 在 INVALID 且 `session_id is None`（无活动会话的急停污染）时归档并复位，不再永久卡死；保留 `archived_runs` 审计。offline_6d profile 取车端实际加载名（接受 `_dds` 变体），缺快照时回落离线字面量。
- `app_ros_mobile/lelab/hybrid_export_schema.py`（M，叠加）：`SIMULATED_6D_PROFILES` 加入 `hardware_hybrid_manual_v1_offline6d_dds`。
- 前端：`LanderpiWorkspace.tsx` 重写为单一 `operator_stage`+单一主按钮+状态条+REC/未录制+审核三键+常驻暂停/急停；诊断/映射端点/范围/测量移到 `LanderpiAdvancedSettings.tsx`（挂在 `/landerpi/calibration`）；`StableCameraPreview.tsx` 增加 409→“相机帧未前进（可能断流）”而非硬错误、保留上一帧并继续轮询；`captureApi.ts` 提供请求封装与错误文案（发生了什么/是否已停止/下一步）。
- 测试：`tests/test_hybrid_capture.py`（12 项编排/恢复/幂等/审核/追加/急停污染）、`tests/test_camera_live_preview.py`（3 项，含匿名 400）。

注意：`ros_mobile.py / hybrid_recording.py / hybrid_export_schema.py` 在本会话开始前就已是未提交 M 状态；上列 diff 是叠加在既有未提交改动之上的本会话内容，不是全部相对 HEAD 的新增。

## 3. 已修复的状态恢复缺陷

- `recording.status=invalid / control_session_changed`：新控制会话不再继承旧“正在录制”，旧 attempt 标 `interrupted` 并 `car_episode_open` 记录车端是否仍开集。
- 页面刷新恢复：`operator_stage/recording_active` 由服务端权威 status 推导，不依赖 React 内存/localStorage。
- `start_episode`+`set_phase` 半完成窗口：编排层在失败时统一停输出、给唯一主因、把车端已开的窗口落到 paused，绝不显示“录制中而车端未开始”。
- 结束本集重复 pause：前端只发一个 `end_episode` 意图，后端 controller 内部先停后收尾，前端不再额外 `pause_all`。
- 故障后旧录制收尾：`recover` 用**车端自身 session_id**（car_recording_id）关闭旧包，容忍“已关闭/属另一会话”家族（BridgeError/RecordingError 一并分类），最终由 pipeline 的 sync 校验真实 manifest 是否 raw_ready。

## 4. 验证范围与证据

命令与产物在 `docs/evidence/landerpi-capture-flow-20261108/`。全部为**隔离模拟栈**（domain 94，dds_v1，profile `hardware_hybrid_manual_v1_offline6d_dds`，`data_identity=simulated_offline`、`training_eligible=false`），非真机：
- `drive_acceptance.py` 22/22 PASS：两集完整流；丢弃/重录不覆盖旧 attempt；重复 request_id 不重复开集/计数；close 待审核拒绝静默丢弃；导出后本地 LeRobot SDK 读回 3 集 6 维 action/state、关节顺序、observe+grasp 边界；页面 `dataset_viewer` 读回一致。`acceptance.json`。
- `drive_recovery.py` 17/17 PASS：控制换代中断且不继承 REC、页面刷新恢复、请求超时重放仅一次、相机新鲜度门禁、双 X-LeLab-Viewer 独立取帧、匿名 fresh 400、急停抢占录制。`recovery.json`。
- 真实浏览器点击（in-app browser，`evaluate_script` 派发原生 click）：获取控制→开始数据集对话框→REC（观察→抓取→结束→待审核→保存→准备下一集→结束数据集→sync/exporting→done，1 集 192 帧）。错误卡、REC 计时、只读检查 vs 本集画面、状态条五事实均按服务端渲染。
- 回归：`app_ros_mobile` 相关 86 passed（含 `test_ros_mobile_teleop_export` 真实 SDK 读写、`test_hybrid_stream_hardware`、`test_camera_live_preview`、`test_t4_hybrid_recording`）。前端 `tsc --noEmit` 干净，`npm run build` 通过，`dist/index.html` 引用最新 bundle。
- 已知非本轮引入：`torchcodec` 缺 `libavutil.so.56` → 导出视频编码回落 pyav（既有环境项，结论不变）。

## 5. 复跑命令

```bash
cd <repo>
# 隔离验收（需 lelab-humble-dev 容器在跑；只动 dds_r1/run 记录进程，不碰 live bridge）
bash docs/evidence/landerpi-capture-flow-20261108/start-dds-stack.sh
bash docs/evidence/landerpi-capture-flow-20261108/rerun.sh          # 两集完整
app_ros_mobile/.venv/bin/python docs/evidence/landerpi-capture-flow-20261108/drive_recovery.py
# 单元回归
cd app_ros_mobile && .venv/bin/python -m pytest tests/test_hybrid_capture.py tests/test_camera_live_preview.py tests/test_t4_hybrid_recording.py tests/test_hybrid_stream_hardware.py tests/test_ros_mobile_teleop_export.py -q
```

## 6. 运行入口 / 停止 / 回退

- 18288 当前已恢复为 **live 硬件服务**（`run-live-18288.sh`，dds_v1，agent 隧道 18110 + `live_bridge.sock`）。真机不可达时 `operator_stage=no_control`，获取控制会失败并给出原因——与改造前一致。
- 停止本任务离线验证栈：仅按 `run/*.pid` 记录身份 `container_ops stop-*`（见 `start-dds-stack.sh` 的停止段）；**不要** `bin/docker down`（会连容器与 live bridge 一起停）。
- 离线 web 服务停止：按 `ss -ltnp | grep 18288` 的真实 pid SIGTERM。
- 回退代码：本轮均为工作区未提交改动，`git diff`/`git restore` 相应文件即可；未提交、未推送、未切 `current`；未删 sessions/invalid；未动串口 owner 或无关服务。
- 冻结标定 bundle 复用 `9d217ce9…`，未重生成。

## 7. 剩余缺口（未验证）

- **真机 T7**：两集实机录制、同步、硬件 6D 导出与 SDK/页面读回、实机急停抢占录制、上位/车端两层失联恢复——均未做，需主臂接上位机 + Pi 在线 + 现场运动授权。
- 追加现有数据集只在离线以新会话验证兼容与 resume 写入；对已存在真机数据集的追加/备份换回未在真机跑过。
- 相机“真实断流”门限（录制中断流停止/标不合格）依赖车端 recorder `required_stream_missing`（既有能力），本轮只验证了 REC 前新鲜度门禁与前端过期显示，未在真机拔相机验证录制中断流收尾。
- 页面“遥操作复位”入口调用 resume_follow，未在真机确认复位不产生意外运动。
- `frontend/src/lib` 仍被 gitignore，本任务新增文件都在 `components/landerpi/` 与 `pages/`，可正常入库。
