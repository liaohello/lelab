# LanderPi 真机闭环执行状态

执行时间：2026-11-05 02:40Z–03:06Z（America/Los_Angeles 为 2026-11-04）。
工作区：`/home/liaozhengyan/Desktop/lelab`。基线 HEAD：
`e4312f05eea4f319d7402d570f5b3fd073b04af5`。未切换 `current`，未提交或推送。

本轮实际接管现有最小臂栈并完成真实录制、六路反馈读取和无输出页面接入；SO101 主臂当前未连接，
因此真实三层标定、P4 两段 6D episode、P5 底盘运动及 P6 9D 均未宣称完成。

## 基线与回退

- 执行前备份：`.migration-backups/landerpi-hardware-20261105T024001Z/`，包含 HEAD、branch、
  tracked working-tree diff、全部 untracked 文件、相关 ignored/dist/证据、逐文件 SHA256 和 `RESTORE.md`。
- 原系统 full bringup 由 `start_app_node.service` 启动。已停止该 unit 并执行
  `sudo systemctl disable start_app_node.service`；unit 文件保留，当前为 `inactive/disabled`。
- 原 full bringup 容器进程组 392 经 `SIGINT` 精确停止，未使用 `pkill`。
- 恢复旧整车栈：先停止本会话最小栈，再执行
  `sudo systemctl enable --now start_app_node.service`。该恢复会重新启动 init_pose、
  底盘/视觉/手柄发布者，执行前必须重新核验现场。
- 最小栈停止：
  `PI=pi@<private-ip> DOCKER_EXEC_USER='-u ubuntu' START_CAMERA=1 ROBOT_CONTROLLER_PYTHONPATH=/home/ubuntu/lelab_car/vendor tools/ros2_collection/start_car_arm_stack.sh --stop`。
- 重启顺序必须是“先停 agent，再重启最小栈，最后启动 agent”。若 agent 仍在轮询时重建 board，
  旧请求可能在重启窗口内触发协议锁存；锁存后必须重启唯一 UART owner，不能靠继续重试恢复。

## P0 现场与接管

有线 SSH `pi@<private-ip>` 使用已有密钥身份登录，host key 校验通过。主机：
`raspberrypi`，machine-id `ad522b778a1541f8b20dd02a8a3660ec`，Pi 5 Model B Rev 1.1，
Debian 12/aarch64，Humble 容器 `MentorPi` 运行中。

- 控制板：`/dev/rrc -> /dev/ttyACM0`，USB `1a86:55d4`，唯一 owner 为
  `ros_robot_controller`。
- 激光：`/dev/ttyUSB0`，未占用或替换。
- 相机：Aurora 930，真实图像 640×400；`camera_info` 实测 fx/fy
  `415.3629/416.0658`，frame `rgb_camera_link`。
- 接管前 `/controller/cmd_vel` 有 4 个 app/handle 发布者，`/servo_controller` 有 6 个发布者，
  `init_pose` 正在运行。接管后上述旧入口全部消失。
- P0 证据：`docs/evidence/landerpi-hardware-20261104/p0/`。

当前最小栈只启动：

1. 补丁版 `ros_robot_controller`（唯一 UART owner）
2. `servo_controller` / `controller_manager`
3. 独立 Aurora 相机
4. LeLab hardware-hybrid agent

## P1 真实 recorder

隔离非执行 topic 上真实启动 `ros2 bag record -s sqlite3`：

```bash
docker exec -u ubuntu MentorPi bash -lc '
  source /opt/ros/humble/setup.bash
  source /home/ubuntu/ros2_ws/install/setup.bash
  ros2 bag record -s sqlite3 -o <task-dir> /lelab_p1_probe'
```

- 15 条消息，recorder exit 0，SQLite `PRAGMA integrity_check=ok`。
- 真实相机短录：55 帧、40.4 MiB、3.919 s、约 14.0 Hz、磁盘增长约 10.3 MiB/s。
- 生产 `SessionRecorder` 通过 camera-only profile 写入 53 帧和 1 条 `/lelab/events`，phase 为
  `observe -> grasp`，bag exit 0、`bag_readable=true`、`raw_ready=true`、`READY` 写出。
- 生产 `sync_session` 经 SSH 隧道拉回 6 个文件，全部按 manifest 校验大小与 SHA256；manifest
  SHA256 为 `91a7db9d9a8effc898b937b793baff32237e3c9542a6526f98ad2d4c49aaaa9c`。
- 这是 `hardware_readonly` 相机 session，`training_eligible=false`，不可作为 6D 数据。
- 首次失败保留：ROS setup 与 `set -u` 冲突；容器无 `sqlite3` CLI；profile 缺 `units`；
  agent 以 root 启动时 DDS endpoint 可见但收不到相机帧。修复为 sourcing 时关闭 `-u`、
  Python sqlite 检查、补齐 profile、默认以容器 `ubuntu` 用户运行。
- 证据：`docs/evidence/landerpi-hardware-20261104/p1/`。

## P2 有界六路反馈与真实 driver

原厂 SDK/node 指纹与 patch BASE 一致。补丁只应用到隔离部署副本
`/home/ubuntu/lelab_car/vendor/ros_robot_controller`，原厂源码树未修改。

- patched SDK SHA256：`7ea4e4f5aeea69d778f5cf93e35abd2c5b227bf3a12796fdfd7bc84d6fa2b8b7`
- patched node SHA256：`529f5717dd082a08c233ac243c24e5f73bf70e15dbba652c74b305524e34da52`
- 最小栈实际导入路径：`/home/ubuntu/lelab_car/vendor/ros_robot_controller/ros_robot_controller_sdk.py`
- 六路真实 raw：`joint1=500, joint2=728, joint3=124, joint4=99, joint5=499, r_joint=150`
- agent 成功扫描跨度约 `49.88–50.39 ms`，各关节 age 约 `10–155 ms`；采样周期 `0.2 s`。
- `/controller_manager/joint_states` 实测约 `47.9 Hz`，但位置仍是 controller 初始化缓存，
  已明确拒绝作为可信反馈；agent 只把 `bus_servo/get_state` 的 raw/ID/age 视为测量。
- 真实故障注入：不存在 ID 253 在 `8.16 ms` 返回 `success=false`；紧接的六路查询在 `1 ms`
  返回失败，旧值被清除。重启唯一 UART owner 后六路恢复。协议无事务 ID，异常锁存是预期行为。
- `HybridGateway.driver_bound=true`；base/arm/gripper permits 全 false，outputs false；
  `driver_sent/controller_acknowledged/execution_observed` 均为 unavailable。
- 接管后执行图只有最小栈与 agent；`/controller/cmd_vel` 不存在，`/servo_controller`
  publisher count 为 0，`init_pose/joystick_control` 不再运行。
- 稳定复测：重启 3 秒后启动 agent，连续约一分钟得到 293 次成功扫描、4 次仅出现在启动窗口的失败；
  最新 raw 仍为 `500/728/124/99/499/150`，`last_error=null`，输出保持关闭。
- 修复：最小栈必须 source vendor `.typerc`，controller manager 传入
  `base_frame=base_footprint`；board wrapper 改为直接执行补丁包模块，避免 `ros2 run`
  入口导入路径不确定；相机单独启动。
- 证据：`docs/evidence/landerpi-hardware-20261104/p2/`。

## P3 三层标定与无输出预览

已新增生产 composition：

- `app_ros_mobile/lelab/hybrid_hardware.py`
- `ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/ros_servo_readback.py`
- `app_ros_mobile/tests/test_hybrid_hardware.py`
- 前端新增真实主臂、从臂实测、映射前目标、限幅后目标、软限位与执行提交计数显示。

生产 composition 已接入：

- 上位机 serial lease + `SO101LeaderReader`
- 车端 HTTP control lease + `/hybrid/begin_epoch`
- 六路 servo service observer、A/B/C 三层标定 readiness
- 只读 T1 driver，硬编码 outputs=false
- 无输出 preview 的映射/夹爪/限幅

无标定时 capability 只开放 `validate/save/estop`；三层就绪后才开放 preview/session。
离开 control lease 后不会自动恢复跟随或重放目标。

当前页面入口（隔离开发实例）：
`http://127.0.0.1:18286/ros-mobile`。它通过本地 SSH 隧道 `18110 -> Pi:8110`
读取真实车端状态，前端已由正式 Vite 构建。

启动方式：

```bash
ssh -N -L 127.0.0.1:18110:127.0.0.1:8110 pi@<private-ip>
cd app_ros_mobile
LELAB_HYBRID_HARDWARE=1 \
LELAB_ROS_AGENT_URL=http://127.0.0.1:18110 \
LELAB_ROS_COLLECTION_ROOT=/home/liaozhengyan/Desktop/lelab/work/ros2_collection/hardware-20261104 \
LELAB_HYBRID_CALIBRATION_ROOT=/home/liaozhengyan/Desktop/lelab/work/ros2_collection/hardware-20261104/calibration \
.venv/bin/python -m uvicorn lelab.server:app --host 127.0.0.1 --port 18286
```

真实浏览器实测：

- 标题 `LeLab`，浏览器脚本错误为空；
- 页面显示六路 `servo_position_readback`、真实 raw 值、age 与来源；
- 因三层标定未就绪，六路均标成 `calibration_not_configured`，禁止跟随；
- 主臂显示 `leader_not_open`；
- 输出关闭，执行提交 `0`。
- 联合视频通过同帧缓存端点返回状态中的精确 `preview_seq`，浏览器显示
  `JPEG 与状态帧序号一致`，不再被旁路缩略图推进全局序号而误判 409。
- 最终浏览器证据（脚本错误为空）：
  `docs/evidence/landerpi-hardware-20261104/p3/browser-final5/`。

### P3 当前阻塞

上位机当前没有 `/dev/serial/by-id`，USB 枚举中没有 SO101 适配器。因此不能：

- 确认主臂 by-id/serial 与物理 rig；
- 执行 A 层当前设备复核；
- 采集 B 层逐关节方向/限位；
- 建立 C 层实时语义/方向/比例/夹爪映射；
- 完成真实双侧 preview。

不能用历史标定文件代替当前设备绑定，也不能把缺失层标成 measured。

2026-11-05 03:14Z 用户报告已插入 SO101 后再次只读核查：上位机仍无
`/dev/ttyACM*`、`/dev/ttyUSB*` 或 `/dev/serial`，`lsusb` 没有新增适配器；Pi 上也没有新增串口。
当前判断是设备未在本次会话所在上位机枚举，或线缆未提供数据连接。需要确认插入主机与数据线后重试。

## P4/P6

均未开始真实动作或数据验收。当前缺少：

- SO101 主臂 USB；
- 现场对机械臂支撑、清场、线缆余量和人工急停/监护的确认；
- 由此产生的 A/B/C 实测标定；
- 锁定底盘的真实 observe/grasp 两段 episode。

没有生成或伪造 6D/9D 训练数据。

## P5 底盘离线准备

已重新执行：

- T2 host harness：`PASS 76 checks, 0 failed`
- T2 protocol + wheel decoder：`14 passed, 25 subtests`
- golden vectors SHA256：`fffd04538e84be3cc81fd2784ed94c8c4691b19de6eaa091520669397e3107c1`
- host compiler 为 conda x86_64 GCC 16.2.0；`arm-none-eabi-gcc` 不在 PATH。

未交叉构建 ARM 固件、未烧录、未运动底盘。板型仍为 USB 描述和运行时
`MACHINE_TYPE=LanderPi_Mecanum`，不能证明固件/编码器能力。证据：
`docs/evidence/landerpi-hardware-20261104/p5/`。

## 下一步现场条件

1. 将 SO101 主臂接到上位机 USB，保留 by-id，确认物理 rig 名。
2. 由现场人员确认机械臂支撑/清场、线缆余量、电源和急停方式。
3. 然后执行 A 层当前设备校验、B 层逐关节低速小幅、C 层静态对齐映射。
4. 三层哈希绑定通过后，才进入无输出双侧 preview；仍保持 permits/outputs 关闭。
5. preview 验收后，再进入锁底盘的单关节/夹爪/多关节，以及两段 6D episode。

## 最终回归

- App 相关：`90 passed`，覆盖 hybrid hardware composition、三层 observer、会话/标定门禁、
  录制、6D/9D export 和页面 readback 代码。
- Tools 相关（本机 Jazzy/Python 3.12 测试环境）：`150 passed, 25 subtests passed`。
- 前端：TypeScript `--noEmit` exit 0；正式 Vite build exit 0，生成
  `dist/assets/index-CCAM7ifa.js`。只有既有 1.9 MB chunk warning。
- 静态检查：`py_compile`、`bash -n`、JSON parse、两份硬件 profile 字节一致均通过。
- Ruff 未安装，未运行。
- 完整日志：`docs/evidence/landerpi-hardware-20261104/final/`。
