# ROS 移动操作采集运行手册

日期：2026-10-31。适用工作区：`/home/liaozhengyan/Desktop/lelab`。本手册对应当前已实际运行的 Jazzy/Harmonic 仿真闭环；树莓派和真实硬件尚未验证。

## 1. 目录与边界

| 路径 | 用途 |
| --- | --- |
| `ros2_ws/` | 版本化 ROS 2 工作区：描述/仿真、车端代理、watchdog |
| `app_ros_mobile/` | LeLab 开发副本（含 ROS 路由、页面和 LeRobot 导出）；发布快照 `current/app` 未被修改 |
| `configs/ros2_collection/runtime-profile-v1.json` | 规范数据/控制契约；agent 包内有一份同内容安装副本 |
| `work/ros2_collection/envs/ros_jazzy` | 用户空间 ROS Jazzy + Gazebo 环境（约 5.6 GB，不进入 Git） |
| `work/ros2_collection/car/sessions/` | 逻辑车端原始 bag、事件、manifest、READY |
| `work/ros2_collection/upper/sessions/` | 上位机通过 HTTP 拉取并哈希校验后的原始 session |
| `work/ros2_collection/upper/exports/` | 对齐报告与导出报告 |
| `work/ros2_collection/lerobot/lelab/` | 导出的 LeRobot v3.0 数据集（默认本地保存，`push_to_hub=false`） |
| `docs/evidence/` | 本次故障注入、数据集和相机证据 |

发布快照 `releases/sanitized-20261024-01` 保持原样；本次源码位于独立分支 `codex/ros2-mobile-collection` 的 `ros2_ws/` 和 `app_ros_mobile/`。

## 2. 安装（用户空间，无需 sudo）

```bash
cd /home/liaozhengyan/Desktop/lelab
tools/ros2_collection/install_ros_env.sh        # micromamba 环境、版本锁、安装日志
source work/ros2_collection/envs/ros_jazzy/setup.bash
```

实际安装版本（2026-10-31）：ROS 2 Jazzy（conda/RoboStack）、Gazebo 8.10.0、`gz_ros2_control` 1.2.20、`ros_gz_bridge` 1.0.24，Python 3.12.16；版本锁在 `work/ros2_collection/envs/ros_jazzy/package-lock.txt`。

## 3. 构建与启动

```bash
cd /home/liaozhengyan/Desktop/lelab/ros2_ws
source ../work/ros2_collection/envs/ros_jazzy/setup.bash
colcon build --symlink-install --packages-select lelab_mobile_description lelab_mobile_sim
# setuptools 84 不支持 setup.py develop --editable；agent 单独用非 symlink 构建
colcon build --packages-select lelab_mobile_agent
source install/setup.bash

ros2 launch lelab_mobile_sim sim.launch.py gui:=false rviz:=false agent:=true
# 任务/相机变体：默认 place_in_tray + wrist_demo_downward
ros2 launch lelab_mobile_sim sim.launch.py gui:=false rviz:=false agent:=true \
  task:=place_on_box world:=place_on_box camera_profile:=wrist_original
```

`camera_profile` 只影响腕部相机俯仰和外参记录（`wrist_original` 0 rad，`wrist_demo_downward` 0.55 rad）；`task`/`world` 必须同时切换。`place_on_box.sdf` 的 SDF world 名保持 `pick_place`，bridge 的 service/contact 路径不变。

启动内容：Gazebo Harmonic（headless rendering）、`robot_state_publisher`、`ros_gz_bridge`、`gz_ros2_control`（手臂/夹爪）、`MecanumDrive`＋平面 `VelocityControl`、`OdometryPublisher`、关节/接触/相机/激光/IMU bridge、watchdog（11 s）、HTTP agent（15 s，端口 8110）。

常用验证：

```bash
ros2 control list_controllers
ros2 topic hz /lelab/wrist/rgb/image_raw
ros2 topic hz /lelab/lidar/scan
ros2 topic echo --once /lelab/base/model_odom
curl -s http://127.0.0.1:8110/status | python3 -m json.tool | head
curl -s -o /tmp/wrist.jpg http://127.0.0.1:8110/preview.jpg
```

`/lelab/base/model_odom` 来自 Gazebo `OdometryPublisher`，是模型实际位姿/速度；`/lelab/base/odom` 只是 `MecanumDrive` 轮式诊断，二者不相等是预期现象。`/joint_states` 由 Gazebo 的 `JointStatePublisher` 发布（约 1 kHz，单源）；录制使用 agent 以 50 Hz 重发布的 `/lelab/recording/joint_states`，避免 1 kHz 膨胀。不要同时启动 `joint_state_broadcaster`。

只读车端（无执行器输出）启动：

```bash
ros2 launch lelab_mobile_agent readonly.launch.py car_root:=/home/pi/lelab_car
```

该 launch 只启动 agent，不启动 Gazebo、controller 或 watchdog；默认端口 8110、回环绑定。只读 profile 的相机 topic 必须先由车端核验后填入，否则启动 session 会返回 `camera_topic_unconfigured`。停止仍通过本地 agent 或直接结束进程；退出路径不会发布速度、零速或力矩。

停止：在 launch 终端 `Ctrl-C`，再确认 `gz sim`、bridge、agent、watchdog 和控制器没有残留；原始 session 的停止只应在 agent 在线时通过 API 发起。

### 停止与回退

- 正常停止：在 launch 终端 `Ctrl-C`；确认 `gz sim`、bridge、agent、watchdog 退出。不要用 `pkill -f` 匹配包含自身命令行的模式，按 PID 或 `ps -eo pid,comm` 过滤。
- 录制中的 session 先用 agent API `POST /recording/stop_session` 结束，再停进程；bag 不会因上位机断连丢失。
- 回退代码：`git switch --detach <上一个已验证 commit>` 或 `git revert` 本轮提交；不要覆盖 `releases/` 快照、`current` 指向或 `lerobot/` 本地定制。
- 回退配置：只读 profile 与仿真 profile 分文件；恢复默认仿真用 `task:=place_in_tray camera_profile:=wrist_demo_downward`，不要修改 Pi 系统 Python、驱动或已有容器。
- 不删除失败 session 的部分 raw；它们保留 `status=error`、`error_detail` 和无 READY，用于复现。

## 4. 车端 HTTP 契约（默认 127.0.0.1:8110）

- 控制：`POST /control/acquire`、`/control/renew`、`/control/release`、`/control/mode`（`keyboard|scripted_expert|idle`；`leader_arm` 明确不可用）、`/control/base`、`/control/arm`、`/control/gripper`、`/control/estop`、`/control/clear_estop`。
- 采集：`/recording/start_session`、`start_episode`、`end_episode`、`discard_episode`、`stop_session`；`GET /sessions`、`GET /sessions/<id>/manifest`、`GET /sessions/<id>/archive`。
- 专家：`POST /expert/run`、`/expert/stop`；`POST /task/reset`。
- 观测：`GET /health`、`/status`、`/preview.jpg`（页面用有界快照）、`/preview.mjpg`（MJPEG，调试用）。

所有运动命令必须带 `session_id`（即控制 token）、递增 `seq`、`source`、`mode` 和 TTL 语义；agent 校验后由独立 watchdog 通过 `/lelab/base/cmd_vel_request`、`/lelab/arm/trajectory_request`、`/lelab/gripper/trajectory_request` 转发。watchdog 使用单调墙上时钟：0.6 s 心跳丢失、0.3 s 底盘命令过期、急停或 odom 过期都会零速/取消轨迹/保持；`gz topic` 直写是 bridge 退出时的最后兜底。

夹爪由五个关节显式命令（主关节 `gripper_base_joint` 与倍率 `-1.5/-1.5/-0.0115995/+0.0115995`），不再依赖 Gazebo 的 URDF mimic 约束；接触证据来自三个指爪/夹爪 contact sensor。

## 5. LeLab 上位机环境与页面

`app_ros_mobile` 是独立开发副本；它的 LeRobot 依赖来自本地 `../lerobot`，而不是 PyPI。当前机器是 AMD GPU，完整 CUDA lock 下载不可取，使用 CPU torch：

```bash
cd /home/liaozhengyan/Desktop/lelab/app_ros_mobile
export PATH=/home/liaozhengyan/Desktop/lelab/work/ros2_collection/envs/ros_jazzy/bin:$PATH
uv venv --python /home/liaozhengyan/Desktop/lelab/work/ros2_collection/envs/ros_jazzy/bin/python3.12 .venv
uv pip install --python .venv/bin/python --index-url https://download.pytorch.org/whl/cpu \
  --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match \
  fastapi[standard] websockets uvicorn psutil rosbags
uv pip install --python .venv/bin/python --no-sources \
  --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple \
  --index-strategy unsafe-best-match \
  --constraint <(printf 'torch==2.11.0+cpu\ntorchvision==0.26.0+cpu\n') \
  -e "../lerobot[core_scripts,feetech,training]"
uv pip install --python .venv/bin/python --no-deps -e .
```

前端（Node 用户空间安装，或系统 node）：

```bash
cd frontend && npm ci && npm run build
```

启动后端：

```bash
cd /home/liaozhengyan/Desktop/lelab/app_ros_mobile
HF_LEROBOT_HOME=/home/liaozhengyan/Desktop/lelab/work/ros2_collection/lerobot \
LELAB_ROS_COLLECTION_ROOT=/home/liaozhengyan/Desktop/lelab/work/ros2_collection \
LELAB_ROS_AGENT_URL=http://127.0.0.1:8110 \
.venv/bin/lelab
```

页面：

- ROS 小车：<http://127.0.0.1:8000/ros-mobile>（获取控制权、键盘驾驶、腕部相机、关节/夹爪手动调试、e-stop、session/episode、专家、同步导出）
- 数据集查看器：<http://127.0.0.1:8000/dataset-viewer>（视频 + 9 维 state/action 曲线）

租约：ROS 页面复用现有 `recording` 硬件租约；全局急停和租约失效的 stop hook 也会调用 ROS bridge 的急停。页面每 3 s 心跳续租；关闭页面后租约由 LeLab watchdog 释放并触发车端停车。

## 6. 同步与导出

1. 页面/API 结束 session，agent 写 `manifest.json` 和 `READY`。
2. `POST /ros-mobile/sync` 通过 HTTP 拉取 `.tar.gz`，在暂存目录解压，逐个比较 manifest 中的 size 和 sha256，然后原子发布到 `upper/sessions/<id>`；哈希不一致会返回 422 并保留原件。
3. `POST /ros-mobile/export` 先做只读/同步/字段预检，再启动独立 worker 进程；返回 `job_id`。查询 `GET /ros-mobile/export/status/<job_id>`，取消 `POST /ros-mobile/export/cancel/<job_id>`。worker 在子进程中读取上位机 bag，按 10 Hz 选择帧时刻之前最新且未过期的图像/关节/模型 odom，动作使用网关已发送目标的零阶保持（最长 3 s），禁止未来观测；尺寸/字段/缺失检查通过后才写 `LeRobotDataset.create → add_frame → save_episode → finalize`，并用本地 SDK 验证后原子发布。
4. 非 `simulation` source、`training_export=false`、缺 action/关节/base 或图像尺寸不符的 session 返回 409，不生成训练数据；只读 session 只允许同步原始 bag/事件。
5. 数据集默认写入 `HF_LEROBOT_HOME/lelab/...`，不执行 Hub 上传。

## 7. 已知限制（必须与本手册一起阅读）

- **底盘执行模型**：参考四球轮在 4 cm 轮径下负载分布不稳，实测纯轮地麦轮会固定对角漂移。当前用 Gazebo 平面 `VelocityControl` 执行底盘三自由度；`observation.state` 的底盘速度来自 `/lelab/base/model_odom`（实际模型位姿），`MecanumDrive` 的 `/lelab/base/odom` 只作独立诊断。不能据此声称已经验证真实麦轮接触力学。
- **频率**：`/joint_states` 由 Gazebo 每物理步发布（约 1 kHz），录制改由 agent 以 50 Hz 发布 `/lelab/recording/joint_states`。相机原始 RGB 仍未压缩，640×480/约 14 fps 约 0.8 GB/min；Pi 8.7 GiB 余量下只适合短片段，需依赖容量门禁和独立介质。
- **传感器**：腕部 RGB 640×480/约 14–15 Hz，深度 32FC1/约 15 Hz，camera_info 为 RGB 内参（深度 camera_info 未单独发布），激光 270 线/约 9.7 Hz；IMU 尚未作为可用模态验证。深度/激光只保留原始数据。
- **相机**：保留 `wrist_original`（0 rad）与 `wrist_demo_downward`（0.55 rad）两个 profile；由 launch 的 `camera_profile` 选择并写入 session/导出。实机安装与虚拟俯仰尚未校准，不能把 0.55 rad 当作实机外参。
- **专家**：`scripted_expert` 使用仿真真值做规划/目标确认/成功判定；真值不进入 state/action，显式标记 privileged。不是真人示教。箱顶任务检查支撑、释放后稳定和夹爪撤离，抬升阈值统一为 0.03 m。
- **时间/故障**：已验证只读无执行器输出、上机 backend 被杀后车端本地收尾、bag 提前退出不 READY、同步哈希校验、只读训练导出拒绝、导出 worker 状态/结果。尚未完成：真实时钟回跳分段、Pi 断连、真实相机/驱动、HTTP 传输篡改端到端测试。
- **硬件**：树莓派本轮不可达（SSH timeout、ping 全丢）；没有真实舵机、相机、串口、FreeRTOS 或真机运动验证。真实相机、真实 bag 与 Pi 侧 READY 必须另立证据，不能复用本地仿真结果。

## 8. 数据与证据

- 成功 session（侧视相机）：`work/ros2_collection/car/sessions/ros_20261031T133217Z_7b7ae3fc`；导出 `lelab/ros-mobile-20261031t133217z-7b7ae3fc`（204 帧）。
- 成功 session（俯视相机，包含先失败后成功两集）：`ros_20261031T140351Z_14a78c2b`；上机 2.8 GB；导出 `lelab/ros-mobile-20261031t140351z-14a78c2b`（187 帧，success=true）。
- 三次尝试 session（弃用/中断/失败）：`ros_20261031T134406Z_97f7e108`；UI 弃用/停止 session：`ros_20261031T135940Z_682218dc`。
- 本轮只读双 episode：`work/ros2_collection/readonly_test_car/sessions/ros_20261031T170156Z_1a91b4ad`；上位机断连本地收尾：`ros_20261031T170238Z_95697d4f`（`stop_reason=upper-disconnected-local-stop`）。
- 本轮仿真 tray/box：`ros_20261031T164152Z_4a59021d`、`ros_20261031T165901Z_fa5a85da`；导出的数据集见 `docs/evidence/ros2-readonly-sim-contract-20261031.json`。
- 证据：`docs/evidence/ros2-*` 与 `docs/evidence/ros2-new-camera-frames/`；完整验收见 `docs/milestones/2026-10-31-ros2-mobile-collection.md` 和 `docs/milestones/2026-10-31-readonly-and-sim-contract.md`。

## 9. 测试

```bash
PYTHONPATH=tools/ros2_collection app_ros_mobile/.venv/bin/python -m unittest discover -s tools/ros2_collection/tests -v
cd app_ros_mobile && .venv/bin/python -m pytest tests/test_hardware_lease.py tests/test_hardware_lease_api.py tests/test_server.py -q
```

2026-10-31 实测：预检 15 项通过；LeLab 回归 63 项通过（新增路由使 `app.routes` 含 `_IncludedRouter`，测试只对有 `path` 的路由收集路径；未改变业务断言）。本轮新增的只读/录制/同步/导出路径由实际本地会话与故障注入验证，证据见 `docs/evidence/ros2-readonly-sim-contract-20261031.json`。
