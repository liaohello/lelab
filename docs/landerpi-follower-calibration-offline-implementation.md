# LanderPi 从臂离线标定：实现与契约（2026-11-04）

本文件冻结本轮实现的 HTTP 契约、车端状态机、服务端资产规则和离线验证结果。
范围是源码、测试、构建与 HTTP fixture；本轮**没有**连接 Pi、没有打开串口、没有发布
ROS 话题、没有让任何机械臂运动。

上一版设计见 `docs/landerpi-gripper-auto-manual-calibration-design.md`，本文只记录
已经落地的差异：自动扫描被移除，夹爪改为「操作员拖动滑轨 + 两次开闭确认」。

## 1. 结论

| 项 | 状态 |
| --- | --- |
| 车端标定任务（background worker，start 立即返回） | 已实现，车端单测 49 项通过 |
| joint1–joint5 手动范围采集、逐轴力矩 | 已实现，覆盖进度由车端累计 |
| r_joint 滑轨标定（0–1000‰ → 显式 raw，≤10 Hz） | 已实现，sink 只出现 ID10 |
| 服务端生成 v2 follower 资产 | 已实现，浏览器无法写 `measured` |
| 新 B 层保存后旧 C 层 mapping 失效 | 已实现（写 sidecar 标记，不删原文件） |
| 同步长循环自动扫描 `/auto-gripper`、`/step` | 已删除 |
| Humble / Python 3.10 实机运行 | **未验证**（无硬件、无容器） |
| 正式 Vite build 写入 `frontend/dist` | **被环境阻断**，见 §8 |

## 2. 冻结的 HTTP 契约（车端，端口 8110）

全部为 `POST`，JSON body，错误统一 `{"error": <reason>, "detail": <text>}`。

| 路由 | 作用 | 关键字段 |
| --- | --- | --- |
| `/calibration/follower/start` | 创建任务，立即返回 | `safe_window_raw:[min,max]`, `operator_confirmed_safe_window`, `support_confirmed`, `operator` |
| `/calibration/follower/status` | 纯状态快照 | `token`, `job_id`, `generation`, `include_evidence?` |
| `/calibration/follower/torque` | 逐轴释放/恢复 | `enabled`, `joints:[jointN]`, `support_confirmed` |
| `/calibration/follower/gripper/target` | 滑轨目标 | `target_permille`, `client_seq`, `ttl_ms`, `pointer_active`, `pointer_generation`, `safe_min_raw`, `safe_max_raw` |
| `/calibration/follower/gripper/mark` | 确认闭/开端 | `end: "closed"|"open"` |
| `/calibration/follower/gripper/stop` | 结束夹爪会话 | — |
| `/calibration/follower/stop` | 停止任务 | `reason` |

`start` 返回 `token`、`job_id`（16 hex）、`generation`、`baseline_raw`、`safe_window_raw`、
`device_identity`。所有后续调用必须同时带上三者，任一不匹配即 409。

`target` 的服务端复核顺序（任一失败即拒绝，不会静默修正）：

1. `pointer_active === true`；
2. 客户端回显的 `safe_min_raw`/`safe_max_raw` 必须等于任务自身窗口，否则
   `safe_window_mismatch`；
3. `target_permille` 必须是整数或有限数值（`bool`/字符串/NaN 拒绝），越界按
   `[0,1000]` 限幅并回 `permille_clamped`；
4. `ttl_ms` 必须落在 20–300；
5. `client_seq` 必须严格递增（回放→`client_seq_not_increasing`）；
6. `pointer_generation`：`null` 表示新一次按下，服务端自增并返回；整数必须等于当前值；
7. 指令预算：单任务 400、单次拖动 150；
8. 10 Hz 上限：间隔 < 100 ms 的命令**不发布**，返回 `accepted:false, reason:"rate_limited"`。

发布由车端 worker 执行：目标带 TTL 入队，worker 每 20 ms 取一次，过期目标丢弃并
`commands_dropped += 1`，绝不迟到补偿。

`mark` 的后端判定（不信任按钮文字）：

- 距最后一次目标请求 ≥ 0.35 s（大于允许的最大 TTL）；
- ID10 反馈年龄 ≤ 0.5 s；
- 最近 1 s 内 ≥ 3 个样本且极差 ≤ 6 tick；
- `|feedback − 最后发布目标| ≤ 12 tick`；
- 反馈值必须落在安全窗内；
- 同一轮同一端不能重复标记。

## 3. 车端状态机

```text
idle → manual_range → gripper_drag → review → stopped
                          ↓            ↓
                        fault ←────────┘
```

- `manual_range`：worker 持续读 `RosServoReadbackDriver.read_snapshot()`，浏览器轮询不参与
  采样。每轴累计 `reference/min/max/span/samples/noise/anomalies/duplicates/stale/missing`。
  只有「新鲜 + `sample_seq` 递增 + `source_kind=servo_position_readback`」的样本才可能扩大范围。
- `gripper_drag`：首次 `target` 时暂停周期扫描（独占同一 UART owner），worker 每 tick 读六路，
  只向 ID10 发布；同时监视 joint1–5 是否偏离拖动起点 > 8 tick，偏离即 `non_target_joint_moved`。
- `review` / `stopped`：恢复周期扫描，只读。
- `fault`：任何链路/跟踪异常立即停止发布并恢复力矩。

TTL 全部由车端单调时钟判定：

| TTL | 值 | 触发 |
| --- | --- | --- |
| 空闲 | 15 s（无任何认证请求） | `client_timeout`，停止并恢复力矩 |
| 任务 | 300 s | `absolute_timeout` |
| 复审窗口 | 180 s（stop 后仍可读/提交） | `token_expired` |
| 命令 | ≤ 300 ms | 过期目标丢弃 |

`noise_raw` 用**二阶差分中位数**估计：匀速手动扫掠的二阶差分为 0，不会被误判成噪声；
抖动读数不会。

温度、电压、力矩读回、接触/负载一律返回 `unavailable`，不猜测。

## 4. 服务端资产生成（上位机）

- `lelab/follower_calibration_asset.py`：`validate_evidence` → `build_follower_asset` →
  `validate_follower_asset_v2`。
- `POST /ros-mobile/hybrid/follower-calibration/commit` 只接受 `token`/`job_id`/`generation`，
  自行从车端拉 `status?include_evidence=true`，重建资产。浏览器传入的 `measured`、
  `device_identity`、`method`、`source_kind`、`identity`、`measured_at_utc` 一律忽略。
- `POST /ros-mobile/hybrid/calibration`（`kind=follower`）现在直接 409
  `follower_calibration_requires_job_commit`，堵住旧的前端自填 `measured: true` 路径。
- `GET /ros-mobile/hybrid/follower-calibration/asset` 供保存后刷新读回。

v2 follower 资产新增字段：

```jsonc
{
  "schema_version": 2,
  "kind": "follower",
  "method": "operator_manual_plus_gripper_drag",
  "source_kind": "servo_position_readback",
  "limits_source": "observed_range_not_mechanical",
  "device_identity": {"board_serial": "...", "board_serial_source": "environment|unavailable",
                      "controller_owner": "ros_robot_controller", "servo_ids": {...}},
  "units": {"centre_raw": 500, "radians_per_tick": 0.0041887902,
            "gripper_flip": "vendor_yaml_min_greater_than_max",
            "conversion_owner": "vendor_joint_position_controller"},
  "limits_rad": {...}, "soft_limits_rad": {...}, "raw_to_rad": {...},
  "joints": {"joint1": {"method": "manual_range_capture", "evidence_sha256": "..."}},
  "gripper": {"method": "operator_drag_safe_window", "mechanical_limits": null, ...},
  "evidence": {"sample_log_sha256": "...", "job_id": "...", ...}
}
```

`mechanical_limits` 恒为 `null`：没有独立端点证据时不允许写机械极限。`limits_rad` 来自
**已观察范围**，字段 `limits_source` 明确标注。

v1 follower 文件继续可读（`CalibrationStore.SUPPORTED_VERSIONS=(1,2)`），v2 只允许 follower。

新资产保存成功后，所有 `bound_identities.follower` 指向同一 identity 的 mapping 会写入
`mapping/<id>.invalidated.json`；`HybridCalibrationStore.load("mapping", ...)` 随后抛
`mapping ... invalidated by follower_calibration_replaced`。重新配对（再次 save mapping）
会清除标记。原 mapping 文件不被删除。

## 5. 前端

`app_ros_mobile/frontend/src/pages/LanderpiSetup.tsx` 第 3 步：

- 安全窗输入 + 两个勾选（窗口确认、支撑确认）→「开始从臂标定任务」；
- 五轴卡片：参考/min/max/跨度/样本/噪声/覆盖条 + 逐轴释放/恢复力矩；
- 夹爪卡片：0–1000‰ 滑轨、目标 raw、实测 raw、误差、反馈年龄、状态文案、
  两次开闭标记、重复性、可用区间；
- 拖动：`pointerdown` 立即发送一次，之后 120 ms 间隔发送最新值；`pointerup` /
  `pointercancel` / `blur` / 页面隐藏立即停止，且不依赖 React 重渲染；
- 拖动中标题徽章改为「受控运动中：仅 ID10」，不再显示「执行输出关闭」；
- 标记按钮的禁用原因（`data-testid="mark-blocker"`）始终可见；
- 温度/电压/力矩/接触一律显示 `unavailable`；页面不提供任何原始 JSON 输入。

## 6. 离线验证结果

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 车端任务单测（含 sink、超时、token、重复样本、故障） | 49 passed, 13 subtests | `tools-follower-job-tests.log` |
| 上位机 API/资产/失效（29 项新测试）+ 相关既有套件 | 91 passed | `app-follower-api-tests.log` |
| app 全套逐文件运行 | 45 个文件通过；13 个因环境 OOM/缺 ffmpeg 崩溃（见 README） | `app-tests-per-file.log` |
| 前端 `tsc --noEmit` | exit 0 | `tsc.log` |
| 正式 `vite build` | **未通过**（环境阻断，见 §8） | `vite-build-attempts.log` |
| Firefox fixture 测试 | **未执行**（依赖 dist，见 §8） | — |

## 7. 上机前必须确认

1. 安全窗是否覆盖夹爪真实行程且不顶到限位；窗口跨度限制 40–400 tick 是否足够。
2. 拖动方向与「闭/开」语义是否一致（本实现只按标记判定方向，不按 raw 大小猜）。
3. `servo_controller` 收到单 ID 消息时是否只动 ID10（离线只能确认消息里只有一个 ID）。
4. 逐轴 `set_state` 释放力矩的真实效果与保持能力。
5. stop / 空闲超时后恢复力矩是否会让手臂跳到旧目标（vendor 只在收到目标时运动，未实测）。
6. 六路顺序扫描下 10 Hz 拖动命令与读反馈的实际延迟；`stall`/`error` 阈值需按现场重标。
7. `/dev/rrc` 总线在拖动期间被标定任务独占时，遥操作/录制确实被拒绝。

## 8. 环境阻碍与精确命令

本会话的 `RLIMIT_AS` 硬上限是 4 GiB，Node 22 的 wasm trap-handler 与 V8 CodeRange 无法在
该空间内分配：

- 直接 `npm run build`：`WebAssembly.instantiate(): Out of memory`（来自 undici/fetch）；
- 加 `--disable-wasm-trap-handler`：可完成 2429 个模块转换，但 `vite:prepare-out-dir`
  复制 `public/favicon.ico` 到 `dist/` 时被工作区 shim 拒绝（`EPERM`）。

因此 `frontend/dist` 当前**为空**，必须由用户在普通终端重建（这一步也会恢复页面）：

```bash
cd /home/liaozhengyan/Desktop/lelab/app_ros_mobile/frontend
npm run build
```

构建完成后运行浏览器 fixture 测试：

```bash
cd /home/liaozhengyan/Desktop/lelab
app_ros_mobile/.venv/bin/python tools/ros2_collection/follower_calibration_browser_check.py
```

## 9. 启动 / 停止 / 回退 / 测试

```bash
# 车端（Pi 容器内，Humble/Python3.10；本轮未执行）
docker exec -u ubuntu MentorPi bash -lc '
  source /opt/ros/humble/setup.bash
  source /home/ubuntu/ros2_ws/install/setup.bash
  cd /home/ubuntu/lelab_car && python3 -m lelab_mobile_agent.agent \
    --ros-args -p profile_path:=/home/ubuntu/lelab_car/configs/profile_hardware_hybrid.json'

# 上位机
cd app_ros_mobile
LELAB_HYBRID_HARDWARE=1 \
LELAB_ROS_AGENT_URL=http://127.0.0.1:18110 \
LELAB_HYBRID_CALIBRATION_ROOT=<work>/calibration \
.venv/bin/python -m uvicorn lelab.server:app --host 127.0.0.1 --port 18286
# 页面： http://127.0.0.1:18286/landerpi

# HTTP fixture（无 ROS、无串口）
app_ros_mobile/.venv/bin/python tools/ros2_collection/http_follower_calibration_fixture.py \
  --host 127.0.0.1 --port 18111

# 测试
PYTHONPATH=tools/ros2_collection app_ros_mobile/.venv/bin/python -m pytest \
  tools/ros2_collection/tests/test_follower_calibration_job.py -q
cd app_ros_mobile && .venv/bin/python -m pytest tests/test_follower_calibration_api.py -q
cd frontend && npx tsc --noEmit -p tsconfig.app.json
```

回退：本轮只新增/修改源码，未提交、未切换 `current`、未改动
`/home/liaozhengyan/Desktop/resource/lander_pi` 与车端 `/home/ubuntu/ros2_ws`。
需要撤销时按文件还原：`follower_calibration_job.py`、`agent.py`、
`follower_calibration_asset.py`、`hybrid_calibration.py`、`hybrid_facade.py`、
`ros_mobile.py`、`mobile_teleop.py`、`LanderpiSetup.tsx`、两个 fixture/测试脚本。
