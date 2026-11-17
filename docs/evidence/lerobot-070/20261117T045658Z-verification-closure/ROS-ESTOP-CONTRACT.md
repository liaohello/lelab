# ROS 混合录制急停降级契约（SAF-DG-01/02/03）

实现：`app_ros_mobile/lelab/hybrid_recording.py`（提交 `64527ac`）。
测试：`app_ros_mobile/tests/test_t4_hybrid_recording.py`，本机 **26 passed / 25.71s**
（`OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1`，
Python 3.12.3，`/home/liaozhengyan/Desktop/lelab-lerobot070/.venv`）。

## 1. 为什么改

旧实现把"保存"放在 `estop()` 里起线程：

```python
threading.Thread(target=self._deferred_estop_save, args=(...)).start()   # 旧
```

线程创建失败（进程线程耗尽）会在**本地停机标志写入之前**抛 `RuntimeError`，
`_hybrid_guard` 把任何 `RuntimeError` 映射成 HTTP 409，于是：

- 操作者看到的是"冲突"，不是"急停已记录、保存待完成"；
- 本地禁止后续动作的闩锁可能根本没落下；
- 车端保存慢时，HTTP 响应被保存阻塞，急停确认时效取决于保存耗时。

这不是"预期过严"，是控制流顺序错误。

## 2. 现在的顺序（不变式）

1. `_estop_requested = True` —— 在取任何锁**之前**写入，因此并发录制命令
   （同一把锁）即使抢到锁也会被 `_refuse_if_estop_pending()` 拒绝。
2. `_new_estop_job()` 编号（`seq`）并入队到**预启动**的保存 worker；入队不建线程。
3. 车端 facade 物理停止：`emergency_stop(reason)`，失败/抛异常只记
   `facade_stop_ok=false`，不改变 1、2。
4. 非阻塞 `lock.acquire(blocking=False)`：拿到就 `_latch_for_job_locked()`；
   拿不到则由 worker 或下一次 `status()` 的 `_drain_pending_estop()` 补闩锁。
5. 返回体区分**已确认停止**与**请求待确认**，保存状态单独上报。

| 字段 | 含义 |
| --- | --- |
| `estop` | 请求已被接受（恒 `true`） |
| `estop_confirmed` | `facade_ok and latched_now`；锁忙或 facade 失败时为 `false` |
| `facade_stop_ok` | 物理停止调用是否成功返回 |
| `save_deferred` | 保存不在请求线程内完成（恒 `true`） |
| `save_mode` | `queued`（已闩锁）/ `queued_pending_latch`（锁忙，待补） |
| `preempted_recording_lock` | 响应是否因抢不到锁而降级 |
| `outputs_enabled` | 恒 `false`：急停后不产生可用产物 |
| `estop_request_seq` | 请求序号，供迟到保存/重复请求对账 |

`recording` 在未闩锁时只回 `{"status": "estop_pending", session_id, session_epoch}`，
**不**伪造 `stopped`。

## 3. 迟到保存与重复请求

- `_estop_saved_request >= seq` 时同一请求不再重复保存；
- 保存完成前若会话身份（`session_id`, `session_epoch`）已变，
  `_estop_stopped_sessions` 记录该身份，`_finish_after_estop()` 跳过对新会话的停止调用；
- 重复急停只 `POST /session/stop` 一次（`test_repeated_estop_posts_one_session_stop`）；
- 未确认闩锁期间 `_refuse_if_estop_pending()` 拒绝 `start/phase/stop` 等推进命令。

## 4. 用例映射

| 用例 | 故障注入 | 断言 | 测试 |
| --- | --- | --- | --- |
| SAF-DG-01 | worker 未启动（spawn 失败等价）、facade 保存等 Event | 响应立即返回；`estop_confirmed=false`、`save_mode=queued_pending_latch`；drain 后补闩锁；只处理当前会话 | `test_estop_without_a_worker_latches_and_drains_the_save` |
| SAF-DG-01 | 车端保存阻塞 | 响应时间有界（同步事件，不 sleep 到保存完成） | `test_estop_response_is_bounded_while_the_car_save_blocks` |
| SAF-DG-02 | 锁被慢操作长期持有 | 先回 `estop_confirmed=false`，锁释放后 worker 补闩锁；不重启、不下发旧动作 | `test_estop_with_busy_lock_reports_unconfirmed_then_worker_latches` |
| SAF-DG-03 | 迟到保存 + 新会话 | 迟到保存不停止新会话，不提前释放写入保护 | `test_estop_late_save_skips_a_new_session` |
| SAF-DG-02 | 重复急停 | 仅一次 `POST /session/stop` | `test_repeated_estop_posts_one_session_stop` |
| SAF-DG-02 | 闩锁未确认时的推进命令 | 全部拒绝 | `test_state_advancing_commands_refused_while_estop_pending` |

## 5. 仍未证明的部分（不要当作已通过）

- **物理停止**：facade 只证明"调用返回成功"，没有关节力矩/编码器回落证据。
  真正的物理停必须由实机（`/dev/rrc` + 舵机断电或抱闸）观测，本仓库不声称。
- **线程耗尽**：测试用"未启动 worker"模拟 spawn 失败，没有在真实
  `RLIMIT_NPROC` 耗尽的进程里跑；`estop()` 现在不再创建线程，因此该路径与线程
  预算无关，但这是设计推理，不是压测结果。
- **车端保存成功**：`save_mode=queued` 只说"已入队"，完成与否由
  `status()`/`estop_saved_request` 对账，测试里用 fake car 验证，未连真车。
