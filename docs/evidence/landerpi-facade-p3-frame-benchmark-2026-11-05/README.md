# P3 真实链路性能基准：生产 facade 异步整帧 vs 同协议同步整帧

工作日期 2026-11-05（用户时区 America/Los_Angeles），实测 2026-11-06 UTC。
范围：交接文档第 5 节 P3。工具 `tools/ros2_collection/local_ros2/offline/facade_bench.py`，
运行在隔离 Humble 模拟实例（域 94、stream 18123、agent 18131、bundle `ae3bf41b…c62ca`），
上位机用生产 `FrameStreamHardwareFacade` + `FrameStreamDriver` + `FrameLink`，车端是生产
`car_control_process`。**不是真机测量**：发布等于把目标交给模拟舵机源，不证明手臂真的动到位。

## 1. 测的是什么，没测的是什么

三种链路分别说明：

| 类别 | 本轮状态 |
| --- | --- |
| 真实链路（同主机 loopback） | 已测：生产 facade 异步整帧 + 同 `FrameLink`/同协议的同步整帧，走真的 socket、真的准入、真的发布与回执 |
| shape 模拟（frame+ping） | 未采用为本轮结论。`offline/bench_links.py` 里那套 frame+ping 仍只是形状模拟，本报告不引用它的数字 |
| 真实 legacy HTTP | **N/A**，不是"很慢"也不是"等价"。用生产工厂 `build_hardware_facade`（`LELAB_HYBRID_TRANSPORT=legacy_http`）实际尝试，得到具体拒绝：`calibration_not_ready:leader calibration missing for identity 'landerpi-so101-leader-v1'`——离线 profile 只有模拟主臂标定资产，legacy 路径要求真实 leader 标定，因此无法在本模拟环境跑起来。没有用 frame+ping 冒充它 |

分段命名按实际边界，`queued→written` 是本进程交给写线程到 `sendall` 返回；
`written→receipt` 是 `sendall` 返回到本进程收到回执（同主机、同一时钟）；
车端自己的窗口年龄与发布间隔留在车端时钟单独报，不做跨主机相减。反馈 500 ms 与帧 TTL 200 ms
未改动，限幅未放宽，本轮没有任何"为了跑得快而调安全预算"的动作。

## 2. 实测结果（30 s × 2 轮，两种模式同输入同车同 TTL）

| 轮次 | 模式 | 提交帧率 | 车端发布率 | queued→written p50/p95 | sendall p50/p95 | written→receipt p50/p95 | 车端窗口年龄 p50/p95 | 拒绝 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | facade 异步 | 9.83 Hz | 9.80 Hz | 0.129 / 0.298 ms | 0.082 / 0.130 ms | 99.63 / 99.995 ms | 26.9 / 27.3 ms | 0 |
| 1 | 同步整帧 | 9.96 Hz | 9.83 Hz | 0.156 / 0.315 ms | 0.079 / 0.130 ms | 2.016 / 2.078 ms | 22.9 / 31.9 ms | 4（`channel_refused`） |
| 2 | facade 异步 | 9.87 Hz | 9.87 Hz | 同上量级 | 同上量级 | 100.20 ms p95 | — | 0 |
| 2 | 同步整帧 | 9.99 Hz | 9.83 Hz | — | — | 2.336 ms p95 | — | — |

短程筛查（各 5 s）同向：异步 10.2 Hz 提交、50/50 接纳。

**这组数字说明的事：**

1. **9–10 Hz 的目标由实测回答：达到。** 生产 facade 在 30 s 里稳定提交 9.83–9.87 Hz，
   车端发布 9.80–9.87 Hz，车端自己的发布间隔 p50 = 100.01 ms（控制周期就是 10 Hz）。
   不需要调调度、相位或窗口发放节奏，本轮也没有改它们。
2. **异步模式那 ~100 ms 不是链路延迟，而是取回执的节拍。** 跟随线程每 10 Hz 才 `collect()` 一次，
   所以回执平均要等一个周期才被记录；同步模式每帧立刻 `collect()`，同一条路的
   `written→receipt` p95 = 2.08 / 2.34 ms。把 100 ms 报成 RTT 就是交接文档要避免的那种错。
   注意 2 ms 也只到"本进程看到回执"，不含舵机执行。
3. **车端窗口年龄在两种模式下都是 20–32 ms**（相对 250 ms 发放窗口、200 ms 帧 TTL），
   也就是提交相对窗口的时差很小，没有逼近过期边界。
4. **同步整帧反而出现了 4 次 `channel_refused`（299 帧里 1.3 %）**：许可刚授予还没传到控制进程的
   一个 IPC 心跳窗口内发帧会被拒。生产异步路径本轮 0 次拒绝；这个差异记下来，
   将来若有人改用停等方式，要按这条证据处理，而不是假设两者等价。
5. `window_issues` 增量（异步一轮 298）不是故障计数：控制进程每个没有窗口的 tick 都会加一，
   空闲时同样增长（空闲样本里 3340 ticks 对应 202 帧发布）。不要把它当错误率读。

## 3. 产物与复跑

- 汇总：`reports/bench-frame-path.json`（含 `screen`、逐轮 `comparison`、`comparison_summary`、
  `legacy_http` 的 N/A 记录）。
- 原始帧级数据：`reports/bench-facade_async-round{1,2}.jsonl`、
  `reports/bench-sync_frame-round{1,2}.jsonl`，共 1190 行，每行
  `frame_seq / sample_seq / queued_ns / written_ns / receipt_seen_ns / sendall_ms / state /
  window_remaining_ms / window_age_at_arrival_ms`。
- 运行日志：`reports/bench-run.log`（rc=0）。第一次运行的日志留在 P1 目录
  `landerpi-facade-p1-web-closure-2026-11-05/reports/bench-run-attempt1.log`：它在本轮暴露的
  写线程竞态上崩溃（`AttributeError: 'NoneType' object has no attribute 'sendall'`），
  修好之后才有上面这份数据，见 §5。

```bash
export LROS_WORK_DIR=/home/liaozhengyan/Desktop/lelab/work/ros2_collection/facade-integration/local_humble
export LROS_CONTAINER=lelab-humble-facade-integration
export LROS_DOCKER_STREAM_PORT=18123 LROS_DOCKER_AGENT_PORT=18131
app_ros_mobile/.venv/bin/python tools/ros2_collection/local_ros2/offline/facade_bench.py \
  --screen-s 5 --compare-s 30 --repeats 2 \
  --out docs/evidence/landerpi-facade-p3-frame-benchmark-2026-11-05/reports/bench-frame-path.json
```

## 4. 未做的事

- 没有跨主机测量（上位机与车端在同一台机器、同一 loopback 上）。真实车上网络路径的时延、
  抖动与丢包仍未测，本报告不能被引用成"上车也是 9.8 Hz"。
- 没有做半开/背压下的时序（P2 的矩阵只回答"是否停止接纳与发布"，没量延迟分布）。
- 没有扩大负载找拐点：按要求只做短程筛查 + 30 s × 2 正常负载，未发现异常就没有加压。
- 相机一路只在 P1 的图像检查里验过同帧绑定与冻结语义，没有把影像吞吐并入本基准。

## 5. 基准暴露并修掉的一个真实缺陷

`app_ros_mobile/lelab/teleop_stream.py` 的 `StreamClient.send` 在"判空"与"写"之间存在竞态：
`abort()`/`close()` 一旦落在中间，`self._sock.sendall(...)` 抛 `AttributeError`，
而写线程只捕 `StreamError`——也就是说一次急停/释放期间可能悄悄杀死发帧线程，
页面只看到发帧停止而没有 `send_failed` 计数。现在先取一次局部引用，失败归入 `send_failed`。
变异检查：还原旧写法 → `test_send_reads_the_socket_once_and_reports_a_write_failure`
以 `AttributeError` 变红；恢复后 `17 passed`，源码 sha256 与修复版逐字一致。
