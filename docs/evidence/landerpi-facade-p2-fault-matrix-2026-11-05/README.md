# P2 故障矩阵：同一生产 facade 会话内的注入、停止与恢复语义

工作日期 2026-11-05（用户时区 America/Los_Angeles），实测时间 2026-11-06 UTC。
范围：交接文档第 5 节 P2。全部在本地 Docker/Humble 模拟设备边界内完成：域 94、stream 18123、
agent 18131、容器 `lelab-humble-facade-integration`、冻结模拟标定 bundle `ae3bf41b…c62ca`，
上位机 Python 3.12 + 生产 `FrameStreamHardwareFacade`/`FrameStreamDriver`/`HybridRecordingController`。
未连接真实小车、主臂、串口或真实相机；**这里没有一项是物理停车证据**。

## 1. 工具

新增 `tools/ros2_collection/local_ros2/offline/facade_faults.py`：在同一生产 facade 会话里注入故障，
每个故障统一回答四件事，并且都从车端自己的证据文件读，不读上位机的说法：

1. 上位机是否停止提交（`facade._follow_commands` 增量）；
2. 车端是否停止接纳与发布（`car_control_status.json` 的 `counters.frames_received/frames_emitted`）；
3. 待发队列是否清空（`admission.queue_depth`）；
4. 故障消失后是否**不会**自动接合（静默 3 s 后再显式恢复）。

`reports/` 里保留了失败尝试 `fault-matrix-attempt1..6.json/log`、`fault-matrix-tail-attempt1..2`，
以及成功产物 `fault-matrix.json`（65 项检查）、`fault-matrix-tail.json`（慢录包期间的急停）、
`fault-matrix-tail-attempt1.json`（重复急停 17 项检查）与 `fault-matrix-generation.json`（未完成项）。

复跑：

```bash
export LROS_WORK_DIR=/home/liaozhengyan/Desktop/lelab/work/ros2_collection/facade-integration/local_humble
export LROS_CONTAINER=lelab-humble-facade-integration
export LROS_DOCKER_DOMAIN_ID=94 LROS_DOCKER_STREAM_PORT=18123 LROS_DOCKER_AGENT_PORT=18131
export LROS_DOCKER_BUNDLE="$(app_ros_mobile/.venv/bin/python tools/ros2_collection/local_ros2/offline/facade_smoke.py --prepare)"
sg docker -c 'tools/ros2_collection/local_ros2/bin/docker up'
app_ros_mobile/.venv/bin/python tools/ros2_collection/local_ros2/offline/facade_faults.py \
  --out docs/evidence/landerpi-facade-p2-fault-matrix-2026-11-05/reports/fault-matrix.json   # rc=1，见第 4 节
```

## 2. 逐故障实测结果（全部来自上面三份报告）

| 注入 | 手段 | 上位停提交 | 车端停接纳/发布 | 队列清空 | 不自动接合 | 关键读数 |
| --- | --- | --- | --- | --- | --- | --- |
| Reader 停更 | 合成主臂 reader 冻结样本、`reads` 不再增长、`age_s` 递增 | 是（增量 0） | 是（0/0） | 是（depth 0） | 是 | `leader_cached_sample_stale` |
| 反馈过期 | `kill -STOP` 模拟板 | 是 | 是 | 是 | 是 | `feedback_stale:4.576 s` |
| 车端状态过期 | `kill -STOP` 生产 agent | 是 | 是 | 是 | 是（fail-closed） | `car_status_read_failed:car_unreachable:<urlopen error timed out>`，`valid()` 仍为 False |
| 许可撤回 | 第二客户端直接 `/control/release` | 是 | 是 | 是 | 是 | `car_local_lease_expired` |
| 流断开（RST） | 上位 socket `SO_LINGER(1,0)` 后 close | 是 | 是 | 是 | 是 | 车端 `stream.client_connected=false`，`frames=82`, `push_failures=0` |
| 背压/半开 | `kill -STOP` 控制进程 3 s（socket 仍开，无人读） | — | 冻结期间 0 发布 | — | 是 | 恢复后 1.5 s 内仅发布 **3 帧**，未倾泻旧帧；`expired_in_queue` 增量 0 |
| 控制进程退出 | `container_ops stop-control` + `start-control` | 是 | 是 | 是 | 是 | agent 侧 `frame_outputs_enabled` 不再为真；重启后 ticks 15→35（证明进程真的活着） |
| 重复急停（2 轮） | 生产录制控制器 `estop()`（即网页急停按钮同一条路） | 是 | 是 | 是 | 是 | 车端检出流断开耗时 0.2 s / 0.0 s；`state=estop`、`session_latched=true` |
| 慢录包期间急停 | 真实 sqlite3 收尾进行中发急停 | — | — | — | — | 急停生效 **0.135 s**，收尾正常返回，`last_error=null`，未误判丢租约 |

65 项检查全部为 PASS（`failed: []`），第 4 节另有两项未完成。

## 3. 两条必须写下来的差别

1. **帧模式急停不会给车端上 estop 闩锁。** `emergency_stop` 走的是
   `FrameStreamHardwareFacade.emergency_stop` → `driver.abort()` + 撤销本地许可；车端只体现为
   `licence.frame_stream_connected` 由真变假（0.2 s 内）。`/status.control.estop_latched`
   与 `latched_fault` 都仍是 false/None——那个字段属于 legacy HTTP `/control/estop` 路径。
   后果：清闩锁只在上位（`HybridSession.estop_latched`），车端不会因急停而拒绝后续 enable。
   这不是本次能自行改的安全语义，需要你确认是否要求帧模式也落到车端闩锁。
2. **故障后恢复必须重开会话，不是重新接合。** 每个故障案例都先记录
   `resume_failures: RuntimeError:session_not_ready`，随后 `recovery_path="new_session"`
   才恢复运动（12–15 帧/1.5 s）。也就是说 `_follow_fail` 已经停租并置 `fault`，
   在原会话里点“对齐/接合”是不可能的——这正是“不会自动接合”的强证据，也说明网页按钮在
   故障后必须引导操作员重开会话。

## 4. 未完成 / 未验证

- `retired_generation_frame`（在线核验“旧 socket 迟到帧被拒”）**没做出来**：新连接第二个
  `FrameLink` 的 hello 在离线栈上超时（`no complete message before the deadline`），
  报告见 `fault-matrix-generation.json`。目前该保护仍只有单元层证据
  （`tools/ros2_collection/tests/test_teleop_frame_v1.py:277/:297` 的
  `stream_epoch_retired`、`test_teleop_stream.py:185`、`test_car_control_process.py:290/:305`）。
  没有在线复现，就不能说已经验过。
- 矩阵是分三次跑完的：`fault-matrix.json`（案例 1–7 全部 + 案例 8 的检查，末尾因跨案例残留
  会话状态报错）、`fault-matrix-tail-attempt1.json`（重复急停 17 项）、`fault-matrix-tail.json`
  （慢录包期间急停 5 项）。单跑 `facade_faults.py` 全部 10 个案例仍会以 rc=1 结束，
  这是编排残留，不是被检行为的失败；已如实保留 6 份失败日志。
- 队列倾泻只观察到 3 帧的余量发布，没有制造更深的积压；`expired_in_queue` 为 0，
  因此“过期帧不会在恢复后补发”这一条只在 200 ms TTL 的自然到期下成立，未做长背压压力测试。
- 停止发包不等于物理停止：真实板卡的保持行为、真实急停回路、断连后的机械行为仍未验证。
- 反馈 500 ms 新鲜窗口与帧 TTL 200 ms 未放宽；本矩阵没有为提高通过率改动任何时限或限幅。
