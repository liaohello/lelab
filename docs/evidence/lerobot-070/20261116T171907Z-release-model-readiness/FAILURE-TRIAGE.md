# ROS 套件失败分类与处置（R1）

源码版本：worktree `codex/lerobot-070-integration`，triage 起点 `0bcc598`，
修复提交 `cc935fe`（连续性测试）、`5daa788`（急停修复 + fixture 替换）。
本机环境：Python 3.12.3（worktree venv），`ulimit -u` 4096/4096，
`ulimit -v` 4 GiB，桌面常驻线程约 3700+（套件可用线程余量很小）。

对账基线：`../20261116T145909Z-m4m6/ros-app-suite.txt`（9 failed / 818 passed）
与本轮 `0bcc598`+ 修复后重跑（`/tmp` 日志，9 failed / 813 passed / 1 error）。
两轮失败集合有漂移：压力类失败随套件推进的线程/内存峰值浮动，
这是分类为环境失败而非固定契约失败的直接依据。

## 逐项分类

| # | 测试节点 | 现象 | 分类 | 处置 |
| --- | --- | --- | --- | --- |
| 1 | `test_t3_wave_a.py::test_calibration_strict_policy_rejects_examples_mismatch_and_over_age` | `FileNotFoundError: docs/evidence/parallel-20261104/T3/calibration-schema-example.json` | **缺失 fixture**（历史私有证据文件不在本树） | **已修复**（5daa788）：改用版本受控的模拟 fixture 生成 example 文档，断言 `validate_bundle` 以 `unmeasured_example` 拒绝；单测 passed |
| 2 | `test_t4_hybrid_9d_export.py::test_two_short_episodes_export_and_read_back_as_nine_dimensions` | `av.error.MemoryError: avcodec_open2(libsvtav1)` | **环境失败**（编码器分配失败） | 兼容环境重跑：见 g128 结果（下） |
| 3 | `test_t4_hybrid_9d_export.py::test_zero_base_covariance_is_kept_as_declared_evidence` | 同上 | **环境失败** | 同上 |
| 4 | `test_t4_hybrid_9d_export.py::test_an_unlabelled_episode_stays_exportable_and_a_failure_is_dropped` | 同上 | **环境失败** | 同上 |
| 5 | `test_t4_hybrid_9d_export.py::test_explicit_simulation_identity_exports_without_claiming_hardware` | 同上 | **环境失败** | 同上 |
| 6 | `test_t4_hybrid_recording.py::test_real_sqlite_sync_export_sdk_and_viewer_two_episodes` | 同上（libsvtav1） | **环境失败** | 同上 |
| 7 | `test_t4_hybrid_recording.py::test_hybrid_routes_pause_before_close_and_never_on_phase` | 本轮 `500`（`can't start new thread`，TestClient portal） | **环境失败**（线程耗尽级联） | 同上 |
| 8 | `test_t4_hybrid_recording.py::test_estop_returns_without_waiting_for_the_car_save` | 历史证据轮：`409`；本轮：`500`（portal 线程） | **实现缺陷 + 环境触发**（见下） | **已修复**（5daa788）+ 兼容环境重跑 |
| 9 | `test_t4_hybrid_recording.py::test_facade_without_adapters_is_honestly_unavailable` | 本轮 `500`（portal 线程） | **环境失败** | 同上 |
| 附 | `test_t4_hybrid_recording.py::test_estop_latches_even_when_the_host_cannot_start_threads`（新增回归） | 本轮套件中因 portal 线程失败 | 环境失败（新回归本身单测 passed） | 同上 |

（另有 1 个非测试 error：套件收尾阶段的线程耗尽；历史轮同类。）

## 急停 409 专项（R1 重点）

排除过程（按计划要求，未改预期、未放宽保护）：

1. **单测隔离**：estop 测试单独跑 → `1 passed`（0.71s）。排除"测试本身契约错误"。
2. **模块内组合**：整个 `test_t4_hybrid_recording.py` → `20 passed`。排除本模块内状态泄漏。
3. **跨模块组合**：`test_integration_upper.py` + estop → `10 passed`；`test_t4_hybrid_9d_export.py` + 本模块 → `34 passed`。
   排除"前一模块遗留全局 facade 会话"的直接假设（`monkeypatch` 会还原
   `ros_mobile._HYBRID_FACADE`，`coordinator._hybrid_facade` 虽被
   `configure_hybrid_facade` 置换但 estop 路径 `allow_estop=True` 绕过身份校验，
   不是 409 的来源）。
4. **全量套件长 traceback**：真正的异常是 `RuntimeError: can't start new thread`
   ——既有历史轮的 409（`_hybrid_guard` 把 RuntimeError 统一映射为 409，
   `estop()` 内 `thread.start()` 失败逃逸），也有本轮的 500（TestClient 的
   anyio portal 线程本身起不来）。同一资源根因，两个暴露面。

**实现缺陷（已修复）**：`estop()` 的两处 `threading.Thread(...).start()`：
- 主路径：锁已先 latch（正确），但 spawn 失败把"已急停"变成 409；
- 忙锁回退路径：spawn 失败意味着** latch 根本没应用**，仍返回 409。

修复保持契约不变：latch 仍然第一优先；spawn 失败改为内联完成保存并如实
报告 `save_mode: deferred|inline|none`；忙锁回退先有界等待锁（5s）再内联
latch；仅当 latch 完全不可用时才抛出显式 `estop_latch_unavailable`。
回归测试 `test_estop_latches_even_when_the_host_cannot_start_threads` 在
控制器自身的 spawn 缝隙模拟线程耗尽（不影响 TestClient），断言两条路径都
200 + estop=True + outputs_enabled=False + latch 生效 + 丢弃式 end_episode 到车端。

## g128 兼容环境重跑

- 主机：g128（已授权访问；本轮预检 Tesla V100S-32GB 空闲、492 GB RAM、113 GB 磁盘）。
- 范围：仅上表 2–9 的确切失败节点 + 新回归，同一源码（rsync 自 `5daa788`），
  独立 venv（Python 3.12 + 锁外 CPU torch + 可编辑 lerobot/app_ros_mobile）。
- 结果：见 `g128-rerun.log`（执行后回填）。
