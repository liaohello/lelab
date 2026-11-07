# P0→P4 离线收尾交付（2026-11-06 UTC）

工作日期 2026-11-05（用户时区 America/Los_Angeles）。按 `AGENTS.md` 与
`docs/handoffs/2026-11-05-facade-offline-wrapup-plan-and-agent-goal.md` 第 5 节 P0→P4 推进。
HEAD 仍为 `e4312f05eea4f319d7402d570f5b3fd073b04af5`：**未提交、未推送、未切 `current`**，
接管前的未提交/未跟踪文件与失败 session 全部保留（工作树 144 项：31 修改 / 113 未跟踪）。

## 1. 结果一览

| 阶段 | 状态 | 关键实测 | 取证 |
| --- | --- | --- | --- |
| P0 网页 lease_lost | 已闭合（上一轮） | 锁顺序定位 + 修复；真实录包收尾 2.103 s 内 105 次 `/status` p95 1.9 ms、心跳 4/4 成功 | `docs/evidence/landerpi-facade-p0-lease-stability-2026-11-05/README.md` |
| P1 网页闭环 + 数据读回 | 已闭合，**留两条待你判定** | 生产 server + 真实 Firefox 两集 observe/grasp 96 帧、受限 6D 导出、负向 11 项检查、图像冻结/断网/同帧绑定、SDK 与 DatasetViewer 逐轴 0 处不一致 | `docs/evidence/landerpi-facade-p1-web-closure-2026-11-05/README.md` |
| P2 故障矩阵 | 已闭合，1 项未完成 | 8 类故障 65+17+5 项检查全绿；慢录包期间急停 0.135 s 生效 | `docs/evidence/landerpi-facade-p2-fault-matrix-2026-11-05/README.md` |
| P3 真实链路基准 | 已闭合 | 生产 facade 异步 9.83–9.87 Hz（车端 9.80–9.87 Hz）；同协议同步 p95 2.08/2.34 ms；legacy HTTP 记 N/A 并附具体拒绝 | `docs/evidence/landerpi-facade-p3-frame-benchmark-2026-11-05/README.md` |
| P4 构建与候选包 | 已闭合，ARM/车端部署留作外部缺口 | 上位机 136 passed / 9 skipped、车端 3.10 121 passed / 31 skipped、tsc 干净、vite rc=0、新候选包 36 文件哈希与导入闭合全对 | `docs/evidence/landerpi-facade-p4-build-and-candidate-2026-11-05/README.md` |

本轮新增/修改的源码在 `source-manifest.json`（14 个文件，sha256 + 是否被 git 跟踪）。

## 2. 本轮改了什么（除文档与取证脚本外）

- `app_ros_mobile/lelab/hybrid_hardware.py`：相机冻结时 `video.reason` 不再谎报
  `fresh_camera_frame`，改为 `camera_frame_stale` / `camera_unavailable` / `fresh_camera_frame`。
- `app_ros_mobile/lelab/teleop_stream.py`：`StreamClient.send` 判空与写之间的竞态会让
  `abort()` 把写线程打成 `AttributeError`；改为持一次局部引用并归入 `send_failed` 计数。
  基准运行真实撞到过一次，修好后才有可用的 P3 数据；变异检查（还原旧写法 → 用例以
  `AttributeError` 变红 → 恢复后 17 passed、sha256 逐字一致）。
- `tools/ros2_collection/local_ros2/offline/container_ops.py` + 新测试
  `tools/ros2_collection/tests/test_container_ops_stop.py`：`stop_recorded` 在身份仍匹配时
  从 SIGTERM 升级到 SIGKILL 并如实报 `killed_after_term_timeout`；同时钉住"命令行不符不杀"
  "pid 复用不杀"两条负向保护。起因是实测到 `sim_camera` 收了 SIGTERM 仍在发布，
  旧实现返回 `stopped:false` 后调用方继续把栈当健康用。
- `tools/ros2_collection/local_ros2/bin/package`：ALLOW_LIST 28 → 36（补齐上位机帧契约文件），
  并新增"被导入却没进清单就拒绝打包"的车端闭合门（变异验证：删一项立刻报出该项）。
- 新工具：`offline/facade_faults.py`（故障矩阵）、`offline/facade_bench.py`（三链路基准）、
  `offline/web_media_checks.py` 补 `dropped` 阶段与按端口取属主、`offline/web_closure.py` 补
  取消/错误/急停恢复点击。

## 3. 已实测 / 仅文档确认 / 推断可行 / 未验证

- 已实测（本机隔离模拟设备边界，rc 与产物见各 README）：P1 全部网页点击与读回链路、
  P2 八类故障的四项判据、P3 两种真实帧模式的时序、P4 两套契约回归 + 前端构建 + 候选包校验 +
  `--render` 与 `bin/docker up/down`。
- 未验证且本轮不能声称：**任何真机行为**（手臂实际停在哪、舵机断电/超期是否保持、真实急停回路、
  跨主机网络、真实相机与串口）。P3 只有同主机 loopback；P2 的"停止"指停止发包与准入。
- 明确未完成：① 在线核验"重新绑定 epoch 后旧 socket 迟到帧被拒"没做出来
  （第二路 `FrameLink` hello 超时 `no complete message before the deadline`），
  仍只有单元层 `stream_epoch_retired` 证据；② ARM64/固件交叉构建只有失败证据，无产物；
  ③ `deploy_readonly_agent.sh --apply/--stop/--rollback` 需 Pi 可达，未跑。
- 需要你判定的两条行为差异：① 帧模式急停不给车端上 `estop_latched`（legacy HTTP 会），
  且租约到期后 `licence.frame_outputs_enabled` 仍留 `true` 而控制进程自报 `false`；
  ② 网页急停后点"清除急停"没有离开 `estop`，随后"释放+重新获取"60 s 内回不到 `ready`，
  UI 侧恢复路径本轮没走通。两者都只记录、没改语义。

## 4. 数据与身份

- 本轮新数据：session `ros_20261106T115158Z_a1e43d7e`，2 集 / 96 帧 / state 6 / action 6，
  同步校验含 manifest sha256，受限导出 `repo_id lelab/ros-mobile-20261106t115158z-a1e43d7e`，
  始终 `data_identity=simulated_offline`、`training_eligible=false`；普通 `/export` 对同一 session
  仍返回 409 `requires a hardware_teleop session`。
- 失败与旧数据保留：`web-datasets/…-canceled-attempt`（被取消任务的残留，逐文件哈希对照后原样挪走）、
  车端 `docker/sessions/` 里此前的错标/未完成 session、6 份故障矩阵失败日志、
  旧候选包 `frame-facade-offline-candidate.*` 都未删除。
- 旧只读 session 与普通导出路径身份未变（本轮没碰那两条链路）。

## 5. 收尾动作

- 撤销许可与释放租约：`facade_faults`、`facade_bench`、`web_media_checks`、`web_closure` 每次运行
  结束都显式 release 并把车端读到 `control.active=false` / `frame_outputs_enabled=false`。
- 停本任务服务：`facade_web.py`（18288）按监听行取 pid 停止；只 down 本任务标签的
  `lelab-humble-facade-integration`；为拉 arm64 镜像做过的一次 `docker run` 之后把该镜像删回去。
- 不属于本任务的 `lelab-humble-dev`（域 93）与 18286 上的既有 uvicorn 未停、未改。
- 未连接或操作真实小车、主臂、串口或相机；未烧录；未训练；未提交/推送/切 `current`。
