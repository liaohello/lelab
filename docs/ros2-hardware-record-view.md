# 小车实机只读录制画面：页面与证据快照

日期：2026-11-01。状态：**代码与离线测试完成，车端链路当轮不可用，实机画面未在本轮复验。**

## 1. 为什么要专门做一个页面

车端链路会成分钟地掉到 `NO-CARRIER`（2026-10-31 记录：约 20:00–20:15 恢复、随后再次断开）。
在这个条件下，**实时看着画面不等于留下了证据**：连接一断，帧就没了，"上周还能看到图"无法复核。
因此本轮把"看得见"和"留得下"分开：

| 能力 | 位置 | 说明 |
| --- | --- | --- |
| 实时画面 | `/ros-hardware` 页面 | 车端 `web_video_server` → 上位机 `/ros-mobile/preview.jpg` 逐帧拉取 |
| 证据快照 | `POST /ros-mobile/hardware/snapshot` | 把真实帧 + 同时刻车端状态 + 帧龄 + 尺寸一致性落到本机磁盘 |
| 配置对照 | `GET /ros-mobile/hardware` | 只读 profile（记录了哪些话题在实机核验过）与在线状态的合并视图 |
| 快照索引 | `GET /ros-mobile/hardware/snapshots` | 按目录 mtime 倒序列出历史快照 |

页面入口：顶部导航「实机采集」，或直接访问 `/ros-hardware`。

## 2. 变更新增

| 文件 | 内容 |
| --- | --- |
| `app_ros_mobile/frontend/src/pages/RosHardware.tsx` | 实机只读采集页：真实帧 + 帧龄/实测分辨率/配置分辨率对照、传感器健康表、只读 session/episode 控制、证据快照 |
| `app_ros_mobile/lelab/ros_mobile.py` | `fetch_preview_jpeg()`、`jpeg_dimensions()`、`load_hardware_profile()`、`hardware_overview()`、`hardware_snapshot()`、`hardware_snapshots()` 及 4 条路由 |
| `app_ros_mobile/tests/test_ros_hardware_readonly.py` | 9 项离线测试（JPEG 尺寸解析、profile 校验、车端离线降级、快照三种结局、倒序索引） |
| `app_ros_mobile/frontend/src/App.tsx`、`.../landing/LandingTopBar.tsx` | `/ros-hardware` 路由与导航入口 |

只读语义没有放松：页面不提供底盘/机械臂/夹爪入口；profile 里 `runtime.actuator_outputs=false`，
`training_export.enabled=false`，两处在页面上显式展示，避免把"能看到画面"读成"能采集训练数据"。

## 3. 接口契约

| 方法 | 路径 | 返回要点 |
| --- | --- | --- |
| GET | `/ros-mobile/hardware` | `mode`、`read_only`、`profile` + `profile_sha256`、`camera{expected_width,expected_height,fps,age_s,stale}`、`sensors[]`、`echoes{actuator_outputs,training_export,...}`、`agent_ok`、`agent_error` |
| POST | `/ros-mobile/hardware/snapshot` | `snapshot_id`、`path`、`jpeg_sha256`、`jpeg_bytes`、`jpeg_width/height`、`size_matches_profile`、`camera_age_s`、`has_frame`、`notes[]` |
| GET | `/ros-mobile/hardware/snapshots?limit=N` | `snapshots[]`（含 `corrupt` 标记）、`root` |
| GET | `/ros-mobile/preview.jpg` | 真实帧 JPEG，附 `X-LeLab-Fetched-At`，`Cache-Control: no-store` |

车端不可达时的行为是明确的：`/hardware` 仍返回本地 profile（`agent_ok=false`、`agent_error` 带原因），
快照接口在没有帧时**仍写 `agent_status.json` 和 `capture.json`**，把失败原因记进 `notes`，
不产生 `frame.jpg`，也不返回 `has_frame=true`。

快照目录默认 `~/.cache/lelab/ros2_collection/upper/hardware/snapshot-<UTCstamp>-<hex6>/`，
含 `frame.jpg`、`agent_status.json`、`capture.json` 三件套。写入先落 `.tmp` 再 `os.replace`。

## 4. 现场怎么用

```bash
# 0) 装好车端只读代理（需在 Pi 上交互执行，见 docs/ros2-collection-runbook.md）
# 1) 上位机起服务，指向车端代理
export LELAB_ROS_AGENT_URL=http://127.0.0.1:8110   # 经 SSH 隧道指向车端 8110
# 2) 浏览器打开
#    http://127.0.0.1:<port>/ros-hardware
# 3) 看到画面 → 点「采集快照」→ 本机落 frame.jpg + capture.json
# 4) 需要原始 session 时：开始 session → 开始 episode → 结束并保存本集 → 停止 session
```

`size_matches_profile` 是这条链路最容易出问题的检查项：profile 记的是 `640×400`，
若实际帧是别的尺寸，说明相机被改过配置或换了设备，页面会用黄色横幅顶出来，快照里也记为 `false`。

## 5. 本轮验证

| 项 | 结果 | 证据 |
| --- | --- | --- |
| 后端单元测试 | **13 passed**（含既有 2 项桥接测试 + 1 项同步测试 + 新增 9 项） | `.venv/bin/python -m pytest tests/test_ros_mobile_bridge.py tests/test_ros_mobile_sync.py tests/test_ros_hardware_readonly.py -q` |
| 前端类型检查 | `tsc --noEmit` 通过 | 命令输出为空、exit 0 |
| 前端生产构建 | **未完成**：本机沙箱内 `vite build` 触发 `WebAssembly.instantiate(): Out of memory`，两次尝试（含 `--max-old-space-size=4096`）均失败 | 与代码无关，属本机资源限制；`app_ros_mobile/frontend/dist` 未纳入版本控制，不影响本提交 |
| 实机画面 | **未复验**：本轮 `enx00e04c68034c` 为 `NO-CARRIER`，`192.168.19.138:22` 连接超时 | `ip -br link`、`ssh` 实测 |
| 车端代理在线 | **未验证**：SSH 无非交互凭据，代理未部署 | 见 `docs/milestones/2026-10-31-raspberry-pi-environment-assessment.md` |
| 原始 session 落盘 | **未验证**：同上 | — |

历史上唯一一次真实帧证据仍是 `docs/evidence/pi-rgb-snapshot-20261031.jpg`
（2026-10-31 20:0x 本地窗口，sha256 `3de68274…`，640×400）。本轮没有产生新的实机帧。

## 6. 下一步（按顺序）

1. 拿到 Pi 的免交互凭据或让现场人员执行 `tools/ros2_collection/deploy_readonly_agent.sh`，把只读代理跑起来。
2. 链路恢复的窗口内先点「采集快照」，再开 session；顺序反过来会先消耗掉最不稳定的那几分钟。
3. 至少两段短 episode 在 Pi 本地成功落盘，其中一段经受上位机断连后仍保存（见 `docs/ros2-hardware-observation-goal.md` 验收项）。
4. 只有 1–3 都成立，才能把"小车实机录制画面"标成已验收；现在只能说页面与快照链路可用、实机未复验。
