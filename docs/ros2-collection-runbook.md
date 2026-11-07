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

> 车端地址是 **DHCP**，已经从 `<private-ip>` 漂到 `<private-ip>`（2026-11-04）。不要信任记忆里的 IP：按 WiFi MAC `<hardware-mac>` 查 `ip neigh`，或扫 22 端口定位。`start_car_arm_stack.sh` / `deploy_readonly_agent.sh` 的默认地址已同步到当前值并在横幅里打印 `target=`，换网段时用 `PI=pi@<ip>` 覆盖。

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

`/lelab/base/model_odom` 来自 Gazebo `OdometryPublisher`，是模型实际位姿/速度；`/lelab/base/odom` 只是 `MecanumDrive` 轮式诊断，二者不相等是预期现象。`/joint_states` 由 Gazebo 的 `JointStatePublisher` 发布（约 1 kHz，单源）；录制使用 agent 以 50 Hz 重发布的 `/lelab/recording/joint_states`。任务真值 topic 由 Gazebo 以约 900 Hz 发布，录制使用 agent 以 20 Hz 重发布的 `/lelab/recording/{cube,container,box}_pose`；专家/评估仍读原始真值 topic。不要同时启动 `joint_state_broadcaster`。

只读车端（无执行器输出）启动：

```bash
ros2 launch lelab_mobile_agent readonly.launch.py car_root:=/home/pi/lelab_car
```

该 launch 只启动 agent，不启动 Gazebo、controller 或 watchdog；默认端口 8110、回环绑定。只读 profile 的相机 topic 必须先由车端核验后填入，否则启动 session 会返回 `camera_topic_unconfigured`。停止仍通过本地 agent 或直接结束进程；退出路径不会发布速度、零速或力矩。预览编码优先 Pillow，缺 Pillow 时回退 OpenCV；状态里的 `preview_backend` 记录实际后端。

上位机跨不过 DDS 时的只读 rosbridge 中继（把车端真实观测经 JSON/WebSocket 引到上位机本地话题再预览/录制）：在 profile 顶层加入

```json
"remote_source": {"enabled": true, "backend": "rosbridge", "url": "ws://<private-ip>:9090",
  "topics": [{"topic": "/ascamera/camera_publisher/rgb0/image", "kind": "rgb", "type": "sensor_msgs/msg/Image"}]}
```

agent 仅在 `hardware_readonly` 下接受该段，非只读或未知 backend 直接拒绝启动；每个话题在本地以同名话题重发布，因此 `recording.topics`、预览和过期判断都沿用原路径；`/status.sensors.remote_source` 给 `connected/connects/frames/dropped/last_receive_age_s/last_error`，manifest 的 `metadata.sensor_transport` 写 `rosbridge_relay`，并在 `relay_clock_note` 声明 header 时间来自车端时钟、bag 接收时间来自录制主机。中继只覆盖观测话题，绝不重发布执行器话题。实测吞吐：640×400 bgr8 经 base64+JSON 中继落 bag 约 4.9 Hz（源标称 8.6 Hz），所以它用于预览、连通性和断连恢复核验，实机原始采集仍以车端 `ros2 bag` 直录为主路径。演练证据：`docs/evidence/ros2-rosbridge-relay-rehearsal-20261102.json`（合成数据源，非真机）。

Pi 上不改系统环境、不装 Python 依赖的部署/检查/回退：

```bash
# 先看要投递什么（不连 Pi）；PROFILE_NAME 缺省即已验收的只读 profile
DOCKER_EXEC_USER="-u ubuntu" tools/ros2_collection/deploy_readonly_agent.sh --render
PI=pi@<private-ip> DOCKER_EXEC_USER="-u ubuntu" tools/ros2_collection/deploy_readonly_agent.sh --check
PI=pi@<private-ip> DOCKER_EXEC_USER="-u ubuntu" tools/ros2_collection/deploy_readonly_agent.sh --apply
# 遥操作需要显式点名 teleop profile（只读 profile 本身不被改动，也不作为默认值）
PI=pi@<private-ip> PROFILE_NAME=hardware-teleop-v1 DOCKER_EXEC_USER="-u ubuntu" tools/ros2_collection/deploy_readonly_agent.sh --apply
# 上位机经既有 SSH 配置建立隧道，再启动 LeLab
ssh -N -L 8110:127.0.0.1:8110 pi@<private-ip>
PI=pi@<private-ip> DOCKER_EXEC_USER="-u ubuntu" tools/ros2_collection/deploy_readonly_agent.sh --stop
PI=pi@<private-ip> DOCKER_EXEC_USER="-u ubuntu" tools/ros2_collection/deploy_readonly_agent.sh --rollback
```

容器内默认用户是 root，ROS 只由 vendor 的交互式 zsh 加载，所以必须显式带 `DOCKER_EXEC_USER="-u ubuntu"`；脚本自己 source `/opt/ros/humble/setup.bash` 与 `~/ros2_ws/install/setup.bash`，并以追加方式设置 `PYTHONPATH`，否则 `rclpy` 会被覆盖丢失。`readonly_agent.pid` 记录的是 agent 本体：wrapper 在写入 pid 后 `exec python3 -m lelab_mobile_agent.agent`，所以 `$$` 就是被 exec 替换后的 python 进程（2026-11-02 那次“pid 指向包装 shell”的残留问题已这样修掉，并有 shell 契约测试钉住 `echo $$` 与 `exec` 的顺序）。

车端话题与 CLI 版本约束（2026-11-02 实测）：录制命令的话题必须是位置参数（Humble 的 `ros2 bag record` 没有 `--topics`）；车端 Humble sqlite3 bag 不内嵌类型定义，上位机 `rosbags` 读取必须传 `default_typestore`，由 `lelab.ros_mobile._open_bag_reader` 统一处理。相机驱动在这台车上必须用 `DEPTH_CAMERA_TYPE=aurora`（`ascamera` 分支打开设备后零发布），只起相机而不启动 vendor 的 controller/servo/init_pose；`~/robot_pi/start_node.sh` 的完整 bringup 含 `init_pose(action_name=init)`，会驱动舵机摆臂，只读阶段禁止使用。

真机实测行为（2026-11-03，见 `docs/evidence/raspberry-pi-readonly-fault-injection-20261103.json`）：录制中挂住 32 个慢读者 MJPEG 连接 22 s，bag 图像仍以 14.716 Hz 完整落盘、零流缺口并正常 READY，但该窗口内 `GET /status` 会超时——预览面可被拖慢属预期，不得据此判断录制失败；`min_free_bytes` 门禁在容量不足时以 `disk_low` 拒绝 `start_session`，只留下无 `session.json`、无 READY、无 bag 的空壳目录；640×400 bgr8 实测约 0.66 GB/min，57 GB 盘已用 86%，采集后应尽快同步并清理车端；`readonly_agent.pid` 现记录 agent 本体 pid，`--stop` 可直接生效。

脚本默认容器 `MentorPi`、容器内路径 `/home/ubuntu/lelab_car`；认证完全交给用户已有 SSH 配置，不读取或打印凭据。`--rollback` 只删除部署的代码/profile/启动脚本，保留 `sessions/` 和日志。2026-11-02 已在实机执行 `--check/--apply/--stop`：两个真机 session 完成车端录制并通过上位机校验，详见 `docs/evidence/raspberry-pi-readonly-acceptance-20261102.json`。此前 2026-10-31 的不可达与 `Permission denied` 记录仍是事实，只是已被本次窗口取代。

无 SSH 时的只读话题核验（不登录、不部署，仅 rosbridge 订阅与 rosapi 查询，不创建 publisher/service client）：

```bash
source work/ros2_collection/envs/ros_jazzy/setup.bash
python3 tools/ros2_collection/probe_pi_via_rosbridge.py --duration 5 --output /tmp/pi_rosbridge_probe.json
curl -s -o /tmp/pi_rgb.jpg 'http://<private-ip>:8080/snapshot?topic=/ascamera/camera_publisher/rgb0/image'
```

该通道只能证明话题存在与真实图像内容，不能替代车端录制、断连保录和同步验收。`/rosapi/services` 在车端负载下会超时并关闭连接；证据见 `docs/evidence/raspberry-pi-live-probe-20261031.json`。宿主 Jazzy DDS（`ROS_DOMAIN_ID=0`、`ROS_LOCALHOST_ONLY=0`）未发现车端话题，跨机 DDS 组播尚不可用。

真实传感器被动核验（只创建订阅，不创建 publisher/client）：
```bash
# 部署包已包含 probe_real_sensors.py；在 Pi 容器内运行
docker exec MentorPi bash -lc 'cd /home/ubuntu/lelab_car && python3 probe_real_sensors.py --duration 10 --output /tmp/pi_sensors.json'
```

输出包含消息类型、publisher QoS、接收侧速率、header stamp 跨度和图像尺寸/编码。若已知驱动 QoS，可加 `--reliability reliable|best_effort`。该工具已在本地仿真话题上演练通过。

停止：在 launch 终端 `Ctrl-C`，再确认 `gz sim`、bridge、agent、watchdog 和控制器没有残留；原始 session 的停止只应在 agent 在线时通过 API 发起。

### 停止与回退

- 正常停止：在 launch 终端 `Ctrl-C`；确认 `gz sim`、bridge、agent、watchdog 退出。不要用 `pkill -f` 匹配包含自身命令行的模式，按 PID 或 `ps -eo pid,comm` 过滤。
- 录制中的 session 先用 agent API `POST /recording/stop_session` 结束，再停进程；bag 不会因上位机断连丢失。
- episode 的结束状态互不混用：`saved`（正常收尾）、`discarded`（操作员主动丢弃）、`interrupted`（session 停止时仍打开）。每个 episode JSON 与 manifest 都带 `end_reason`；由录制器错误（如 `required_stream_missing`、`disk_low`、`clock_jump`）强制收尾时写 `forced_by_<error>`，不写成操作员丢弃。
- 回退代码：`git switch --detach <上一个已验证 commit>` 或 `git revert` 本轮提交；不要覆盖 `releases/` 快照、`current` 指向或 `lerobot/` 本地定制。
- 回退配置：只读 profile 与仿真 profile 分文件；恢复默认仿真用 `task:=place_in_tray camera_profile:=wrist_demo_downward`，不要修改 Pi 系统 Python、驱动或已有容器。
- 不删除失败 session 的部分 raw；它们保留 `status=error`、`error_detail` 和无 READY，用于复现。

### 遥操作最小启动入口（`tools/ros2_collection/start_car_arm_stack.sh`）

只在需要真实从臂反馈时使用；它不启动含 `init_pose` 的整车 bringup，也不读取任何凭据（沿用既有 SSH 配置）。

```bash
# 1) 只读审计：容器、/dev/rrc 持有者、vendor 节点是否在跑、params 文件能否解析
tools/ros2_collection/start_car_arm_stack.sh --check

# 2) 预览将要执行的远端命令，不落任何写操作
tools/ros2_collection/start_car_arm_stack.sh --apply --render
LELAB_STACK_DRYRUN=1 tools/ros2_collection/start_car_arm_stack.sh --apply --i-authorize-board-writes

# 3) 实跑（必须显式授权：ros_robot_controller 构造即写板卡）
tools/ros2_collection/start_car_arm_stack.sh --apply --i-authorize-board-writes

# 4) 核查话题状态及独立舵机总线回读；joint_states 位置是命令缓存，不能视为实测
tools/ros2_collection/start_car_arm_stack.sh --status

# 5) 停止本脚本自己启动的两个节点
tools/ros2_collection/start_car_arm_stack.sh --stop
```

- 启动顺序固定为 `ros_robot_controller` → 等待 `/ros_robot_controller/init_finish`（有限超时，`wait_for_service` 也带超时）→ `servo_controller`；否则 `controller_manager` 会在无超时的服务等待里永久阻塞。
- 未带 `--i-authorize-board-writes` 时 `--apply` 直接拒绝并返回 `78`。脚本会列出写入项：`enable_reception`、从 YAML 加载的舵机偏移、机型、LED、底盘零速。
- 进程管理：包装脚本第一行 `echo $$ > <state>/<name>.pid`，再以 `nohup setsid` 启动，记录的就是会话首进程；`--status`/`--stop` 只读 `$STATE_DIR/*.pid`，kill 前用 `/proc/<pid>/cmdline` 校验属于本任务的节点名，不匹配就跳过。全程无 `pkill`/`killall`。
- `--status` 无反馈时非零退出并打印 `no real follower feedback`；缺 `SERVO_PARAMS`/`MACHINE_TYPE` 时包装脚本直接退出，不会用默认值起节点。
- 反例：不要把车端网络可达当成串口可用，也不要为“先连上”而运行 vendor 整车 launch；同一控制板串口只允许一个驱动持有。

### 单关节力矩卸载/恢复（`tools/ros2_collection/car_servo_torque.py`）

“某轴被限位卡住”经常是结论而不是观测。先量三件事再动手：位置、`angle_limit`、`torque_state`。2026-11-04 实测 joint1 = `position 500`（0..1000 中点）、`angle_limit [0,1000]`（满量程）、`torque_state 1`——没有限位可解，推不动的感觉来自保持力矩。

```bash
# 只读：打印各轴 before 力矩
python3 /home/ubuntu/shared/car_servo_torque.py --ids 1
# 卸载（必须显式授权；0x0B），自带 after 读回校验
python3 /home/ubuntu/shared/car_servo_torque.py --ids 1 --unload --i-authorize-servo-writes --json /home/ubuntu/shared/servo1_unload.json
# 恢复（0x0C）
python3 /home/ubuntu/shared/car_servo_torque.py --ids 1 --load --i-authorize-servo-writes
```

- 命令语义来自固件 `RosRobotControllerLite/Hiwonder/System/packet_handle.c`：`0x0B` 卸载动力、`0x0C` 加载动力、`0x0D` 读动力状态、`0x32` 读位置限制。
- **先看 URDF 轴向再卸载**：`arm.urdf.xacro` 里 joint1 ≈ Z 轴（底座偏航），卸力矩只是让臂可以水平自由转；joint2–joint4 承重，卸载会掉臂。默认只对 joint1 用。
- 脚本拒绝在 `/dev/rrc` 已被 vendor 节点持有时运行（返回 3），这不是形式主义，见下条。
- 同一串口上**不要**用 vendor 服务读 `get_position_limit`：`bus_servo_read_and_unpack` 是 `bus_servo_queue.get(block=True)`，无超时且不校验帧长/回显，服务回调拿到不匹配帧后 `struct.unpack` 抛错会直接把 `ros_robot_controller` 带走（2026-11-04 实测发生一次：节点退出、`/dev/rrc` 空闲、`bus_servo/set_position` 失去订阅者，`servo_manager` 与 `init_pose` 仍在）。需要限位/力矩这类原厂字段时，让 vendor 节点停掉、单独用上面的脚本读。
- 容器 `RestartPolicy=always` 但 `bringup` 不是开机自启（`.sync_typerc.sh` 只同步 `.typerc`）：断电重启后手臂栈不会自己回来，需人工 `ros2 launch bringup bringup.launch.py`，而它会跑 `init_pose` 再命令 `joint1=500`——确认关节已能自由转动、周围清空之后再起。

### 边转边看位置（`tools/ros2_collection/car_servo_watch.py`）

手感判断（“这里扭不过去”）只有配上读数才是证据。这个工具开一次串口、按固定频率打印各轴位置，结束时给 `min/max/span`：

```bash
cd /home/ubuntu/shared && python3 car_servo_watch.py --ids 1 2 3 --seconds 30 --rate 5
```

判读：读数跟手走且平滑 → 关节与编码器正常；推着读数不动 → 输出端被顶住/舵机与关节脱开/转的不是这个关节；读数在一处饱和 → 那就是机械终点。2026-11-04 用它确认 joint1 可从 500 转到 1021（`+125°`，已超出 `0..1000` 的 ±120°）后 `span=0`，即机械行程到头，不是限位。

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

租约：ROS 页面复用现有 `recording` 硬件租约；全局急停和租约失效的 stop hook 也会调用 ROS bridge 的急停。页面每 3 s 心跳续租；关闭页面后租约由 LeLab watchdog 释放并触发车端停车。旧页面的心跳只在“本页面自己取得控制权”时发送，遥操作会话持有车端租约时不会再刷 409。

### 5.1 遥操作页面与三层标定

只读入口不变；遥操作需要显式部署 `hardware-teleop-v1` profile（只读 profile 未放宽）：

```bash
cd /home/liaozhengyan/Desktop/lelab/app_ros_mobile
export LELAB_ROS_AGENT_URL=http://127.0.0.1:8110
export LELAB_ROS_COLLECTION_ROOT=/home/liaozhengyan/Desktop/lelab/work/ros2_collection
export LELAB_MOBILE_TELEOP_HOME=$HOME/.local/share/lelab-mobile-teleop   # 三层标定 JSON 落盘位置
export LELAB_MOBILE_TELEOP_PROFILE=$PWD/../configs/ros2_collection/hardware-teleop-v1.json
```

`/ros-mobile` 页面顶部的“设备与三层标定 / 遥操作会话 / 无输出映射预览 / 录制”即 `MobileTeleopPanel`：

1. A/B/C 三层（主臂 SO-101、车端从臂、主从映射）分别“载入模板 → 编辑 → 原子保存该层”，模板来自 `configs/mobile_teleop/<kind>.example.json`（`GET /ros-mobile/teleop/calibration/example?kind=`）。每层带版本、sha256 与绑定身份；“检查三层就绪”失配时直接列出 `mapping binds leader=… but loaded …`，不会静默用默认值。A 层必须带 `lerobot_calibration`（每电机 `id/drive_mode/homing_offset/range_min/range_max`，即现有 SO-101 `*_rig.json` 的那块）：缺失、字段不全或关节缺项时在真正打开串口处 `build_bus` 直接抛错，路由返回 409，不会用默认值连硬件。
2. “取得双层租约”按顺序：先只读打开主臂串口 → 就绪检查 → 上位机 `teleoperation` 租约 → 车端 `leader_arm` 控制租约；任一失败即回滚，页面显示具体原因且两个租约 pill 保持“无”。主臂未连接时不会先占车端租约；向导里已经打开的主臂串口会被 acquire 直接复用（不再报“reader already open”），但要换另一支主臂必须先“释放串口”，页面不会替你关闭正在独占的端口。串口下拉不会自动选中：只有 `/dev/serial/by-id` 的序列是稳定身份，ttyACM 编号随枚举变化。打开/获取都必须带 `leader_identity`（页面在身份为空时禁用按钮），标定在开串口时就绑定。
3. 主臂串口独占：reader 取到 LeRobot 的 fd 后依次 `flock(LOCK_EX|LOCK_NB)` 与 `TIOCEXCL`，状态里的 `port_claim` 只会是 `flock+tioxcl` / `flock_only` / `unverified:no_fileno`，被占时直接 `409 leader_serial_busy`。这两层只约束协作进程与使用 O_EXCL 的打开者，不遵守它们仍能打开节点，所以上位机硬件租约才是权威门禁。关闭时 `disconnect(disable_torque=False)`，被动观察者绝不写断力矩。端口选择只用 `/dev/serial/by-id` 序列：同一台主臂在 2026-10-31 解析为 ttyACM2、2026-11-03 解析为 ttyACM3。
4. 状态机按钮顺序 `无输出预览 → 记录主/从参考 → 待命 → 接合跟随`；释放离合、急停恢复、标定变更都会清空旧参考，必须重新对齐才能再次接合。预览表缺数据显示“缺失”，不补零。
5. 录制是同一会话的子任务，不再申请第二个租约；“结束本集”默认先暂停跟随再收尾，“停止会话”先停控制后关包，急停不等待保存。
6. 车端不可达或隧道卡住时，bridge 返回 `503 car agent timeout after Ns`（不再是裸 500）：命令是否落地未知，页面必须重新确认状态后再继续，不能把“上位机停止发包”当成“车端已停”。大 session 同步中途断流同样返回 `503 archive transfer interrupted after N bytes`，暂存副本被丢弃，不会发布半个 session。
7. 失联期间的页面语义：`/sessions` 仍返回已校验同步到上位机的本地副本（附 `agent_error`），但 `/ros-mobile` 会显示“车端状态未知 · 控制入口已禁用”并按只读渲染，不再沿用最后一次成功回复给出关节滑杆或急停按钮；隧道恢复后自动回到真实 profile 的形态。
8. 车端 `/status` 字段契约：部署版 agent 把 `follower_feedback` 与 `teleop.chassis_locked` 放在 `control` 块内（租约标志名是 `active`，独占判定用 `control.client_id`），协调器同时接受嵌套与顶层两种形状；只读顶层会让真车上所有门禁永久关闭。页面显示的底盘状态取自“最近一次真实观测到的车端 gate + 观测年龄”，本机 profile 的 `chassis_locked` 只另列为 `chassis_locked_profile`；尚未观测到过任何车端状态时显示“未知（尚未取得车端状态）”，不把配置默认值当成小车实际状态。空闲（未持有车端租约）时页面只轮询 `/teleop/status`（纯本地），持租约期间才用 `/teleop/readiness` 触发一次车端真值刷新。
9. 独占控制是车端门禁，不是页面提示：`hardware-teleop-v1` 里 `exclusive_control: true` + `exclusive_guard_topics`（手臂与夹爪两条 `trajectory_request`）声明守卫范围，`set_mode(leader_arm)` 进入前用 ROS 图形枚举这些话题的发布者，只有本网关节点（外加 `exclusive_exempt_nodes` 白名单）在发布才放行；出现任何外来发布者返回 `409 vendor_publisher_present`，枚举失败返回 `409 publisher_topology_unknown` —— 拿不到证据也算不可独占，绝不默认放行，两种情况都只记 `command_rejected` 不改 `control_mode`。上位机把车端结论原样透出（`/teleop/status` 的 `car_exclusivity`，未观测前为 `null`；被拒时 `acquire()` 回滚上位机租约并把车端原因抛给页面），页面 pill“车端独占”显示 未检测 / 可用 / 不可用（并列出外来节点名）。**上车后要先跑 `ros2 topic info -v` 确认 vendor 摇杆与 App 的真实节点名**，再决定 `exclusive_exempt_nodes`；在此之前白名单为空，任何非本网关发布者都会拒绝使能。
   - 上位机就能对着真实 DDS 图形演练这道门禁（不需要车、也不需要 Gazebo）：用 Jazzy 环境把部署版 agent 直接起在本地，`PYTHONPATH=ros2_ws/src/lelab_mobile_agent <env>/bin/python -m lelab_mobile_agent.agent --ros-args -p profile_path:=…/hardware-teleop-v1.json -p car_root:=<临时目录> -p host:=127.0.0.1 -p port:=<空闲端口> -p use_sim_time:=false`，再另起一个真的往 `/lelab/arm/trajectory_request` 发布的节点当“侵入者”，即可看到 `409 vendor_publisher_present` 与被点名的节点；侵入者退出后同样的请求会成功。2026-11-03 已按此法实测（见 evidence 的 `exclusivity_guard_round.live_ros_graph_rehearsal`）。
10. 上位机退出与租约兜底：`/teleop/*` 会话由 `shutdown_event` 收尾（先停跟随流 → 尽力 `end_episode`/`stop_session` → 释放在手的双层租约），隧道断了就只记 `recording_close_error`，不会卡住服务关闭；用的是 `peek_mobile_teleop_coordinator()`，从没打开过遥操作页面的服务不会在退出时凭空构造协调器。租约看门狗的活动探针现在同时认桌面 SO-101 遥操作和小车会话（`is_live()`：在手租约或跟随线程）——小车遥操作复用的是同一个 `teleoperation` 任务名，此前探针只看桌面标志，真车会话撑过 10 s 宽限期就会被以 `task-finished` 抢走租约、门禁失效。租约到期与 `/emergency-stop` 走同一条 stop hook 时会先把车端会话 `emergency_stop(reason="hardware-stop:<原因>")` 锁存急停、再 `release(reason=…)` 交还控制权，不再只停桌面臂；`is_live()` 为假时不产生任何车端流量。车端自身的 `arm_command_ttl_s` 与控制租约 TTL 仍是最后兜底：上位机消失不等于车端已停，手臂保持的是“车端不再收到新目标”这个事实（页面“车端手臂流”pill）。
11. 保持策略由车端声明，断力矩作为停止手段被静态禁掉：`hardware-teleop-v1.teleop.hold_policy` 写明这套舵机在位置模式下保持最后指令位置、力矩从不作为停止手段；车端 `/status.control.teleop` 上报 `hold_policy` 与 `torque_control_offered: false`，上位机缓存成 `car_hold_policy` 并从 `/teleop/status` 透出，页面把它作为“车端手臂流”pill 的悬停说明（车端没上报就写“车端未上报保持策略”，不替它编）。两道静态守卫：`test_the_deployed_agent_never_writes_torque_state` 扫 agent/follower_feedback/watchdog/contract 里的 torque 调用与键名，命中即失败；`test_the_deployed_status_block_declares_hold_and_torque_claims` 断言那四行状态字段还在（`status()` 需要真实节点才能跑，这里用源码检查兜住“改着改着把上报字段删了”）。真机上“手臂确实停在最后指令位置”仍待用户通电后按小幅动作确认。取证见 evidence 的 `hold_policy_round`。
    - 互斥的第四项也有测试了（同日）：`inference`（策略控制）占着上位机租约时，小车遥操作 `acquire()` 必须以 409 拒绝、把持有者名字交给页面，并且**完全不产生车端流量**（连 `/status` 都不问，也不会去 release 一个没拿到的租约）；另有直接跑真实 `HardwareLeaseManager` 的用例，证明 `inference` 与 `teleoperation` 抢的是同一个槽位、释放后可再接手。取证见 evidence 的 `lease_exclusivity_round`。
12. 成功标注是人工或 unknown，绝不套用仿真真值：车端 `_evaluate()` 先看 profile 的判定来源（`task.labels == "manual_or_unknown"` ⇒ 没有仿真真值），遥操作 episode 落进 `manual_evaluation()`——默认 `success: "unknown"`，操作员在“结束本集”时选 成功/失败 并写备注才会变成 `true/false`；这修掉了一个真实缺陷：此前 `hardware_teleop` 不在只读分支里，会走 Gazebo 物理门去读永远不存在的 cube/container 位姿，于是每条遥操作集都被记成 `success: false`，再被导出过滤（只剔显式 false）悄悄丢光。只读分支的 `reason`/`labels` 字段原样不变，仿真 profile 仍走物理判定且导出仍只留 `success is True`；遥操作导出报告的 `success_labeling` 写 `manual_or_unknown_no_simulator_ground_truth`。可验收范围：标注只发生在 episode 关闭那一刻，关闭后没有改写入口（manifest 有写保护），事后改判需要另立显式修订流程，本阶段未实现。取证见 evidence 的 `success_label_round`。

### 5.1b 桌面双臂工作台 `/so101`（上位机本地链路，与车端同一套门禁）

这一段是给"主臂与从臂都插在这台上位机"时用的：它不经过小车网关，但用的是**同一套安全语义**（标定归属核查、无输出预览、显式离合、相对增量目标、读不到实测就拒绝跟随）。车端那条链的判据在 §5.1/§5.2，两者不要混用措辞。

- 启动：`/robots` 里选中某个 rig 后点工作台图标（或直接访问 `/so101`）。设备清单来自 `GET /so101/devices`——只列 `/dev/serial/by-id` 的 by-id 路径，并把 Linux 上恒存在的 `/dev/ttyS\d+` 噪声滤掉；没有外部串口时页面写的是事实而不是空表。
- 步骤：**1 设备识别**（按序列选主/从臂 + 各自的标定文件，显示 sha256 与"这台标定绑在哪个端口"）→ **2 标定**（复用现有 SO-101 向导，写入时记录端口↔标定绑定）→ **3 遥操作** → **4 数据检查**（本地数据集列表，读回用 §6 的数据页）。
- 身份门禁在**打开串口之前**：`GET /so101/identity` 与启动时的 `_identity_guard` 共用 `arm_bindings.check_desktop_pair`，会把"把 A 臂的标定写进 B 臂"这类错误以 `409`/`success:false` 点名拒绝（同一 rig 的对侧臂、选了另一台臂的标定文件、标定里的电机 id 与总线 id 不符都会命中）；录制入口 `bring_up_devices` 走同一道检查，不存在"遥操作被拒但录制放行"。
- 第 3 步有两个按钮，语义不同：**仅预览（不下发目标）** = 会话建立、两臂照常读数与广播，但从臂**不 configure（力矩不开）也不 `send_action`**；**开始跟随** = 同一会话但 `engage=true`，工作线程在**下一个已实测帧**建立"主臂参考 + 从臂实测参考"，之后目标 = 从臂参考 + 主臂相对变化（`wrist_roll` 折角）。会话期间还可用**接合离合 / 断开离合**（`POST /teleop-clutch {engage}`）随时切断/恢复下发；断开即清参考，重新接合必然重新对齐，不会从旧参考续跑。
- 会自己停的三种情况，页面都给出原因（`/teleoperation-status` 的 `stop_reason` 与 `clutch.refusal`）：从臂连续读不到（`follower_feedback_unavailable: N consecutive reads …`，阈值 `LELAB_MAX_FOLLOWER_READ_FAILURES`，默认 10）、主臂读失败（`leader_read_failed: …`）、从臂拒收目标（`follower_send_failed: …`）；接合被拒则写 `follower_feedback_not_measured: …` / `alignment_incomplete: […]` / `preview_session_sends_nothing`。**任何一条都不补零、不用最后一次目标冒充实测。**
- 停止与回退：页内"停止遥操作"；关页面/刷新由 `pagehide` keepalive 兜底发一次停止；租约侧仍走 §5.1 第 10 条（看门狗活动探针认桌面 `teleoperation_active`，到期与 `/emergency-stop` 走同一条 stop hook）。要回到"只有只读会话"的状态：停掉服务进程即可，本链路不写车端、不动 profile，只往 `<HF_LEROBOT_HOME>/lelab/arm_bindings.json` 追加绑定记录（删掉该文件等于让"这台标定属于哪台臂"的证据清零，回退前先备份）。
- 可调项（都是环境变量，读一次即生效，不用改代码）：`LELAB_MAX_FOLLOWER_READ_FAILURES`（自停阈值）、`LELAB_ARM_BINDINGS`（绑定表位置，默认 `<HF_LEROBOT_HOME>/lelab/arm_bindings.json`）、`LELAB_ROBOT_LOCAL_ONLY` 等既有串口/安全项不变；从臂单步限幅仍由 `follower_safety_kwargs()` 注入。
- 已知边界（不要当成已验收）：**桌面录制入口 `record.py` 仍走 LeRobot 原生的"主臂读数→从臂"绝对映射，没有接这套离合/相对增量**；本轮没有擅自改它，因为这会改变既有采集数据的 action 语义（相对 vs 绝对），需要你先定口径。八.3/八.5 对这条链的实机判据仍是：预览时两臂纹丝不动、接合瞬间不跳、断开后重摆主臂必须重新对齐才动。

### 5.2 上车执行清单（八.3–八.8 的顺序，逐项写清"算通过"的判据）

这一节是给操作员的执行手册，不是已完成事项：以下每一步目前都**未做实机验证**，软件侧的测试只证明代码在约定输入下会这样做。需求一到八的逐项证据等级与剩余缺口见 `docs/milestones/2026-11-03-so101-mobile-teleop-status.md`。

2026-11-03T11:32Z 的前置状态（只读核查，取证 `docs/evidence/so101-mobile-teleop-hardware-precondition-20261103.json`）：主臂**不在上位机**（`/dev/serial/by-id` 不存在、无 ttyACM/ttyUSB、24 h 内无 USB 插桩事件），车 `MentorPi` 容器 `Exited (137)`（06:36Z 断电时刻）且 `RestartPolicy=no`，Pi 本身已开机且 SSH 可达但间歇超时。所以第 0 步之前先要：① 用户把主臂接回上位机并确认哪一台是主臂；② 用户起车端栈（`docker start MentorPi` 本身就会带 vendor 的板卡写入副作用，属现场决定）。这两项没有完成时，第 0/2/3 步都无从下手，也不要用合成桩代替。

-1. 上车之前先在本机排练一次（不需要车、不需要主臂）。`work/ros2_collection/envs/ros_jazzy/bin/python tools/ros2_collection/local_car_rehearsal.py --root /tmp/lelab_car_rehearsal` 会用**部署用的那份 `ros2_ws/src/lelab_mobile_agent` 代码**在本机起 agent，喂合成反馈/相机话题；另一个终端用 `app_ros_mobile/.venv/bin/python tools/ros2_collection/local_teleop_session_drive.py --root /tmp/lelab_car_rehearsal --follow-seconds 3` 走完 双层租约 → 无输出预览 → 接合 → 两段 episode → 停会话，并用 sqlite3 把包回读成报告。**判据**：报告 `expectations.failed` 为空、manifest 是 `raw_ready` 且有 `READY`、`/lelab/arm/trajectory_request` 与 `/lelab/gripper/trajectory_request` 计数 > 0、每条 arm 命令都带 `leader_rad/pre_clip/target/calibration_version/clutch/ttl_s`。这一条 2026-11-03 已经跑通（取证 `docs/evidence/so101-mobile-teleop-local-car-rehearsal-20261103.json`），它找到的五个阻塞（话题健康表只记收到的消息、`start_episode` 不认 `leader_arm`、底盘锁定时 `_control_tick` 提前 return 导致一条轨迹都不发、`paused` 无法回到预览、导出器把夹爪事件限定成 1 个值而对不上车端记录的 mimic 关节集）都已在代码里修掉并有回归测试。
   同一条链再把最后两环也跑成生产代码：agent 仍在时起第三个终端 `LELAB_ROS_AGENT_URL=http://127.0.0.1:18115 LELAB_ROS_COLLECTION_ROOT=/tmp/lelab_car_rehearsal/upper app_ros_mobile/.venv/bin/python tools/ros2_collection/local_full_chain_sync_export.py --root /tmp/lelab_car_rehearsal`，它不打桩，直接调 `ros_mobile.sync_session`（走 `/sessions/<id>/archive` + 逐文件 sha256 校验）和 `ros_mobile.export_session`（真读 sqlite3 包、真写 LeRobotDataset、SDK 读回）。**判据**：退出码 0、`readback.state_dims=action_dims=6`、`features` 里没有任何底盘/速度列、`episodes=2`、`export_report.action_source=gateway_sent_target_after_clamp` 且 `success_labeling` 是 manual_or_unknown；脚本还会用**页面路由实际使用的 `dataset_viewer.list_episodes / video_path / episode_timeseries`** 再读一遍（`page_readback`），判据是相机 `wrist_real`、每集 series 两侧各 6 列、以及 `page_can_locate_each_episode_inside_the_shared_file`——LeRobot v3 把多集塞进同一个 mp4，页面必须靠 `from_s/to_s` 定位本集，只"文件存在"不算读回成功（这一条已经抓到播放器只 `#t=起` 不 `#t=起,止`、第 0 集播进第 1 集的缺陷）。**注意**：`source ros2_ws/install/setup.bash` 会把 colcon 的构建拷贝前置到 `PYTHONPATH`，那份可能是旧的——launcher 现在会在 source 之后重新固定 src 路径并解析 `lelab_mobile_agent.agent.__file__`，不是被测源文件就拒绝启动；`READY_FOR_DRIVER.json` 里留有解析到的路径与该文件的 sha256 前 16 位，核对一下再采信结论。排练仍然**不是**实机验收：主臂是合成的、没有 controller 回执（`acks` 为空）、相机是 64×48 的未压缩帧。
   **2026-11-03 重跑这一条时抓到的两个缺陷已经写进代码并有测试**：① 驱动脚本 `local_teleop_session_drive.py` 直接从 `configs/mobile_teleop/*.example.json` 播种，而向导自 4679595 起拒绝“原样保存的示例值”，于是整条无硬件链在 tools 测试全绿的状态下跑不起来（`calibration not ready: … still holds shipped example values`）；现在 `as_rehearsal_provenance()` 去掉 `_example_not_measured`、把 `example_not_measured/derived_not_measured` 的来源改写成 `rehearsal_synthetic`（数值原样保留），排练能过就绪检查，而真页面“载入模板后原样保存”仍然被拒绝——两侧各有一条测试钉住（`tools/ros2_collection/tests/test_rehearsal_chain_tools.py`，含把改写去掉就会变红的变异核验）。② 收尾脚本原来从 `<root>/../rehearsal_report.json`（缺失时退回硬编码 `/tmp/rehearsal_report.json`）取 session id，于是要么对着活 agent 要一个它从没录过的包（404），要么同步到上一次排练的数据；现在 `ready_session()` 只认**正在应答的那个 agent**：`GET /sessions` 里恰好一个 `raw_ready`、且路径在 `--root` 之下，再用 `READY_FOR_DRIVER.json` 的 `port`/`car_root` 对照 URL 与 `--root`，对不上、没有就绪包或有多个就绪包都直接拒绝并点名（要指定只能用 `--session`）。现场就演示过一次：两个就绪包时报 `expected exactly one raw_ready session under this root, found […]`。
-1.5. 一条命令把 §5.2 的只读前置全问一遍：`tools/ros2_collection/teleop_preflight.py --pi pi@<车 IP> --agent-url http://127.0.0.1:8110 --app-url http://127.0.0.1:<上位机端口>`（先 `--render` 看它到底要跑什么）。它只读：本地 `ls /dev/serial/by-id`（主臂在不在**这台**上位机上）、车端 `/status` 的 profile/read_only/chassis_locked/hold_policy/torque_control_offered/反馈话题年龄、上位机 `/ros-mobile/teleop/status` 的就绪缺口，以及车内容器状态、已部署 agent 的 sha256（对比本地源码，防旧 colcon/旧投递副本）、两个轨迹话题与 `/controller_manager/joint_states` 的发布者名单，还有容器里的 `MACHINE_TYPE`（vendor 板卡节点构造时直接读它，未设就 KeyError）与 `ros2 node list` 里是否已有板卡节点在独占 UART——后两条是 2026-11-03 车端只读识别时踩到的两个真实阻塞，现在会提前点名；它们不计入"可以开始只读会话"的阻塞项，因为已验收的只读录制从不需要 vendor 栈。探针清单是白名单式的，里面出现任何写操作脚本会直接拒绝运行（`test_teleop_preflight.py` 钉住，包含把 `docker start` 塞进去必须被拦的变异用例）。它**不**起容器、不叫服务、不动板卡——那些仍是现场授权决定。退出码 0 才算"可以开始只读会话"，非 0 会逐条点名缺什么。

2026-11-04 修订：容器状态由 Pi **宿主机**的 `docker inspect` 读取，其余 ROS 探针才在容器内执行；反馈年龄必须是非负有限数且不超过 `--feedback-max-age`（默认 0.5 秒），发布者计数必须大于零，底盘锁定必须由车端显式上报。前置检查的退出码 0 **不是运动许可**：仍需逐项核实非阻塞的板卡节点、独占控制、急停与现场条件。上位机服务也必须是加载了当前源码的进程，旧进程即使在监听也可能没有 `/ros-mobile/teleop/status`。

A 层 `device_identity.usb_by_id` 和 `serial` 必须与操作者选中的 `/dev/serial/by-id/...` 一致；变更 A 层或更换 identity 后须关闭并重新打开主臂，才能申请双层租约。USB 串口身份只能识别适配器，**不能证明适配器后面连接的是哪支机械臂**，仍需现场核对主臂及其标定。结束本集后到下方“同步与 LeRobot 导出”选择 session；遥操作过程中不要同步。
0. 先摸清车端发布者，再决定白名单。车上 `ros2 topic info -v /lelab/arm/trajectory_request`、`/lelab/gripper/trajectory_request`，把 vendor 摇杆/App 的真实节点名记下来；只有确认常驻且不驱动舵机的名字才写进 `hardware-teleop-v1.teleop.exclusive_exempt_nodes`。白名单为空是安全默认：任何非本网关发布者都会让 `set_mode(leader_arm)` 以 `409 vendor_publisher_present` 拒绝使能。
1. 部署与起栈（需要板卡写入授权）。先 `PI=… PROFILE_NAME=hardware-teleop-v1 tools/ros2_collection/deploy_readonly_agent.sh --check`，再 `--render` 看清要投递的 wrapper，最后 `--apply`；起车端手臂栈用 `tools/ros2_collection/start_car_arm_stack.sh --apply --i-authorize-board-writes`（该脚本会写控制板，必须显式授权）。**判据**：`--check` 通过、`/status.profile` 是 `hardware_teleop_v1`、`/status.read_only` 为 false、`/status.control.teleop.hold_policy` 有文字且 `torque_control_offered` 为 false。
   `--apply --i-authorize-board-writes` **不是普通的无副作用启动命令**：加这个旗标之前，现场先确认急停可用、人员远离运动空间、臂的运动路径清空、初始姿态安全，再由现场人员决定是否授权。上位机这边无法替你判断这四件事，所以脚本故意不做任何"默认放行"。
2. 建隧道并开页面：`ssh -N -L 8110:127.0.0.1:8110 pi@<车 IP>`，打开 `/ros-mobile`。**判据**：只读入口与相机预览仍照常工作；遥操作 pill 显示真实车端状态（"车端独占 未检测/可用"、"车端手臂流 …"及其悬停的保持策略），不是"未知"残留。
3. A 层主臂标定（八.3）：在串口下拉里按 `/dev/serial/by-id` 序列选中主臂（不要凭 ttyACM 编号），填 `leader_identity`，**用"列出现有主臂标定 → 填入"复用现有 SO-101 向导写出的标定文件**（页面会显示它读的是哪个目录；列表为空说明这台服务进程的 `HF_LEROBOT_CALIBRATION` 没指到你标定时的目录，或还没标定过——不要凭记忆手抄 6 个电机 × 5 个字段），然后"打开主臂串口"，扳动主臂看读数与年龄。打开时会 `flock`+`TIOCEXCL` 独占，被占返回 `409 leader_serial_busy`；**若 A 层选中的标定与要轮询的电机 id 不一致，会以 `409 leader_calibration_does_not_match_this_arm: shoulder_pan:calibration_id=7!=bus_id=1` 这类点名原因被拒**（字段齐全但属于另一台臂的标定不会静默生效，因为它会给出连续、合理却错误的度数）。**判据**：读数连续、六轴齐全、夹爪是 0–100 百分比而不是角度；换端口前必须先"释放串口"。
4. B/C 层（从臂校准与主从映射）：分别"载入模板 → 编辑 → 保存"，B 层要写对原始脉冲→物理角的转换、方向、零位、软限位与夹爪 mimic 关系，C 层只做关节空间映射（不宣称末端轨迹相同）。模板里 `wrap` 只允许出现在主臂会真的越过 ±π 的连续关节（当前示例只有 joint5 / wrist_roll）；有限位的主臂关节必须 `wrap: false`，否则一次大幅主臂移动会被折成反向小幅移动。**判据**：就绪为"是"且给出 `calibration_version`；身份或关节顺序不匹配时页面点名报 `mapping binds leader=… but loaded …`，不会静默回落到默认值；预览表里每个关节的回绕标记（方向×比例后面的 `↺`）与你在 C 层填的一致；**"载入模板"后原样保存不算就绪**——三层各自会被点名为 `… layer still holds shipped example values at <具体 JSON 路径>`，必须把 `example_not_measured` / `derived_not_measured` 的值逐项换成实测读数（或去掉 `_example_not_measured` 标记并重写来源说明）才会变绿（2026-11-03 之前这一步是放行的，见 `docs/evidence/so101-mobile-teleop-wizard-click-faults-20261103.json`）。
5. 取得双层租约 → 无输出预览（八.4）：顺序是先开主臂串口、再上位机 `teleoperation` 租约、再车端 `leader_arm` 租约，任一失败整体回滚。预览表要能同时看到主臂读数、映射目标、从臂实测反馈、限幅标记与跟随误差，缺数据显示"缺失"。**判据**：预览阶段车端 `/control/arm` 计数不增长（`/status.control.teleop.last` 不变、车端日志无轨迹），也就是预览真的无执行器输出。
6. 逐关节低速小幅（八.5）：记录主/从参考 → 待命 → 接合，一次只动一个关节、在软限位内，确认急停按钮有效后再做多关节与夹爪跟随。**判据**：手臂跟的是"从臂实测位置 + 主臂相对变化"，不是一步跳到主臂绝对位姿；松开离合后不再下发，重新接合必须重新对齐。**保持策略**：停止发包后手臂应停在最后指令位置、没有掉落——这验证的是 `hold_policy` 那句话在真机上成立。
7. 故障注入与恢复（八.6）：分别做拔主臂 USB、杀隧道、让租约过期、在车上杀掉 agent 进程（**这一路本机已经用排练链做过，但要分两步**：`tools/ros2_collection/local_car_fault_injection.py` 只在 episode 正在录时对 agent 发 SIGKILL，并核上位机侧——状态落到 `paused`、原因点名失效门禁、发送序号不再增长、不会自动恢复；**它不重启 agent**，所以跑完它之后 `ros2 bag record` 还是孤儿在往旧 session 目录写（2026-11-03 17:03Z 实测：pid 1265954 仍在跑，包从 528K 继续长）。恢复是第二步：用同一个根里的 `<root>/run_local_agent.sh` 原路径把 agent 起回来，`recorder.recover_interrupted_sessions()` 按 `--output <session>/raw/bag` 精确匹配、只对那个进程组发 SIGINT（让 rosbag2 写完 metadata.yaml，绝不 SIGKILL），会话记成 `status=error / stop_reason=agent_restarted_mid_session / ready_eligible=false`。17:04Z 实测：日志点名 `stopped orphan recorder pids [1265954]`、孤儿进程 0、包停在 540K、`metadata.yaml` 6628 字节、会话目录里没有 manifest/READY 也不会有。**判据**就按这两条读：孤儿录包进程数为 0 且包体积不再增长；这个坏会话保留、不删也不参与同步。上位机停止发包这一路现在也能本机演练（见下面 7b），仍要真车的只剩拔主臂 USB、真 SSH 隧道断与真租约过期）。**判据**：每次都退出跟随并把页面降级为"车端状态未知 · 控制入口已禁用"，恢复后不自动接合、不复用旧目标（`last` 里的过期目标要被丢弃），车端按自己的 TTL/单调时钟停住；急停不等保存，录包收尾失败只记 `recording_close_error`。上位机退出（关 LeLab）也要走同一条收尾。两条页面细节一起看：`最近错误` 必须点名是哪一项失效（拔主臂应是 `gate_lost:leader_unhealthy`/`leader_not_connected`，发送被拒应是 `arm_send_failed:…`，而不是笼统的 `follow_error:…`），并且暂停之后上位机**不再以 command_hz 轮询车端**（车端 `/status` 访问频率应掉到页面轮询水平）。

   7b. **上位机停止发包、而车端仍在运行**（六 的另一种死法，本机可演练）：`app_ros_mobile/.venv/bin/python tools/ros2_collection/local_car_upper_stream_stall.py --root <排练根>` 用 `local_teleop_session_drive.py` 当发包侧，等 `/status.control.teleop.stream.live` 为真后对**驱动进程**发 SIGSTOP（agent 不冻、车端控制租约仍在、录包继续），再 SIGCONT。**判据**（2026-11-03 17:13Z 实测，8 项期望 `failed: []`）：冻结期间车端**自己**按单调时钟把臂流判过期（`ttl_s=0.6`、`age_s=0.264`、`remaining_s=-0.064`、`live:false`），`control.teleop.last` 一字节未变（没有顺手重放），`control.active` 仍 true，`torque_control_offered:false` 与 `hold_policy` 原样上报；SIGCONT 后流重新 live，且新的 `last` 的 `wall` 前进、载荷也变了（夹爪 50.377→52.859、`pre_clip_rad` 变化）——恢复是发新目标，不是把卡住那一刻的位姿重放；上位机侧照常 `raw_ready` 收尾，两段 episode 都在，也没留孤儿录包进程。**别照抄判据里的字段名**：第一版断言读 `last.seq`，而这个块没有 seq（只有 `pre_clip_rad/gripper_percent/clutch/calibration_version/wall`），于是一次真绿被判成假失败；现在用 `target_is_newer()`（`wall` 前进**且**载荷变化）并有 4 条用例钉住。限制：SIGSTOP 只等于"发包停了"，不等于真隧道半开 TCP；舵机是否真停在最后指令位置在这台机器上不可观测（无执行器、`acks` 为空）。取证 `docs/evidence/so101-mobile-teleop-upper-stream-stall-20261103.json`。

   7c. **主臂这一侧变暗**（同一条 §5.2，第三种死法，本机演练）：`app_ros_mobile/.venv/bin/python tools/ros2_collection/local_car_leader_loss_rehearsal.py --root <排练根>` 在会话 following 且正在录 episode 时让主臂快照变成 `connected=false / healthy=false`，车端 agent 不冻、租约仍在、录包继续。**判据**（2026-11-03 17:18Z 实测，10 项期望 `failed: []`）：上位机下一次读就是 `paused`，`last_error=gate_lost:leader_not_connected,leader_unhealthy`（不是笼统的 `follow_error`），`seq` 之后一动不动（注入到降级之间**最多允许一个在飞周期**：机械臂与夹爪各占一个 seq，10 Hz 下约 100 ms；实测本轮 0，前一轮 2）；车端在 `ttl_s=0.6` 的单调时钟上自己把流判过期（`age_s=2.146`、`live:false`），期间 `control.teleop.last` 一字节未变、`control.active` 仍 true；paused 下直接 engage 被拒 `engage refused: leader_not_connected,leader_unhealthy`；episode 照样收口 `raw_ready`、`success=unknown`。限制：合成 close() 不等于真拔 USB（真机先给读超时/串口错误，检测延迟未测），插回后重新接合这一路仍要上车。取证 `docs/evidence/so101-mobile-teleop-leader-loss-rehearsal-20261103.json`。

   7d. **episode 正在录时按急停**（五 与 六 的交叉点，本机演练）：`app_ros_mobile/.venv/bin/python tools/ros2_collection/local_car_estop_rehearsal.py --root <排练根>` 在会话 following、episode 录制中调 `coord.emergency_stop(…)`，再往锁存中的网关塞一条 `/control/arm`，再 `clear_estop`。**判据**（2026-11-03 17:24Z 实测，12 项期望 `failed: []`）：上位机状态直接进 `estop`、`last_error=estop`、`seq` 停在 43 且此后不动；车端 `estop_latched=true`、`control.active=false`、臂流按自己的单调 TTL 过期（`live:false`），锁存期间到 clear 之后 `control.teleop.last` 一字节未变（清锁存不等于恢复运动）；塞进去的命令被拒（本轮回答的是模式门禁 `409 mode: …`，因为 agent 的 `estop()` 会把 `control_mode` 打回 idle —— `estop_latched` 那道专用 409 只有单测覆盖，别把它写成这里命中了）；急停不等录包：episode 照常结束、会话 `raw_ready` 且有 READY，原始包 `/lelab/events` 41 条里 2 条含 estop（按下与解除）。两处判据坑也记进取证：会话侧 `events.jsonl` 的键是 `kind` 且急停事件只在话题/原始包里，以及"锁存后没再施加"的基线必须取降级那一刻（否则急停生效前那一个在飞周期会被读成重放）。限制：无执行器，真舵机停在哪、硬件急停回路与按钮有效性仍要上车。取证 `docs/evidence/so101-mobile-teleop-estop-rehearsal-20261103.json`。

   7e. **上位机在会话活跃时退出**（六 的第五种死法，本机演练）：`app_ros_mobile/.venv/bin/python tools/ros2_collection/local_car_upper_exit_rehearsal.py --root <排练根>` 在 following + 录制中调**生产入口** `MobileTeleopCoordinator.shutdown()`（服务关闭钩子用的就是它）。**判据**（17:35Z 实测 10 项全绿）：跟随线程不在、上位机租约与车端控制租约都交还（车端 `control.active:false`、`owner` 空、`teleop.stream` 变 `expected:false/ttl:null`）、主臂串口被释放、退出之后再调 `send_once()` 什么都不发、`control.teleop.last` 不再变化、会话收口 `raw_ready`+READY 保留。**这一条当场抓到一个真缺陷**：第一版 `shutdown()` 只停流、关包、还租约，**没有放主臂串口**（`leader.connected` 仍为 true，flock/TIOCEXCL 还占着），意味着退出钩子跑完后 LeLab 若不重启就把臂锁死；现在 `release()` 用 try/finally 包住并无论如何都调 `close_leader()`。注意两点边界：seq 从 43 走到 45 仍是关闭发生在周期边界那一个在飞周期（此后静默），以及本排练没穿 uvicorn 生命周期——真 SIGKILL（连 shutdown 都跑不到）只能靠车端 TTL/租约过期兜底，那条要上车。取证 `docs/evidence/so101-mobile-teleop-upper-exit-rehearsal-20261103.json`。

   7f. **车端控制租约自己过期**（六 的最后一格，本机可演练）：`app_ros_mobile/.venv/bin/python tools/ros2_collection/local_car_lease_expiry_rehearsal.py --root <排练根>` 拿到双层租约、接合、臂流 live 之后，让上位机停止续租，等过 `/status.control.expires_in_s` 加余量再看车端怎么处置——这就是上位机死机/断电/隧道长时间断时车端看到的形状。**判据**（17:41Z 实测，6 项全绿）：车端自己把 `control.active` 变 false、`teleop.stream` 变 `expected:false`；拿旧 token 再发命令被拒（实测先到的是 `403 control_token`，因为 token 已被清；不要把这条写成命中 `409 control_expired`）；恢复只能重来——重新 acquire 后 `client_id` 是新名字、必须重新 align/engage，臂流才再 live，且 `last` 是恢复后新产生的目标（不是过期前排队的爆发）。**这一条也照出一个真缺陷**：过期分支清了 token 与 gate，却漏了 `control_client`，于是 `active:false` 旁边仍挂着旧持有者名字，而页面/协调器的独占判定读的正是 `control.client_id`；修法是过期时一并置 None（主动 release 那条路径本来就清）。**改完部署侧代码必须重启被排练的进程**——第一次复跑仍看到旧名字，就是因为那个 agent 是在打补丁之前起来的。取证 `docs/evidence/so101-mobile-teleop-lease-expiry-rehearsal-20261103.json`。
8. 两段短 episode + 数据检查（八.7）：开始会话 → 开始本集 → （人工标注下拉选 成功/失败 或留 unknown）结束本集 → 重复一次 → 停止会话 → "校验同步" → 导出 → 在数据页读回。**判据**：结束本集后状态应**自己**回到"已暂停"（`/teleop/status` 的 `state` 为 `paused`，不需要额外点暂停），第二集必须重新对齐再显式接合；`GET /ros-mobile/export/status/<job>` 的报告显示 `export_schema.dims=6`、`chassis_included=false`、`success_labeling=manual_or_unknown_no_simulator_ground_truth`、逐帧 `action_semantics.sent/acknowledged/executed` 三种语义分开；数据页里相机是 `wrist_real`、state/action 每行 6 列，**并且每一集的播放器只播本集**：卡片标题显示 `from_s – to_s`，视频地址是 `#t=<起>,<止>`（LeRobot v3 把多集放进同一个 mp4，只给起点会让第 0 集连着第 1 集的画面播下去，操作员就没法把影像和曲线对上某一集）。**不要在遥操作进行中同步**（会占满隧道导致 503）。
9. 网页按钮实点击（八.8）：对上面每一步的按钮各制造一次失败（错身份、拔掉串口、车端关机、租约被 `inference` 占着），确认页面给出具体原因、状态 pill 回到"无/未检测"、并且能按提示恢复——只有真点过才算这一项通过。
10. 回退：`PI=… tools/ros2_collection/deploy_readonly_agent.sh --stop` 停 agent，再 `--rollback` 只删部署的代码与 profile；`sessions/` 与日志保留，已有的 invalid session 不删。之后 `--check` 应回到只读 profile 可再部署的状态。
   车端 agent 被 SIGTERM 停掉属于“计划内停止”：现在退出码 0，日志是一行 `agent stopping: ExternalShutdownException`，不再有 `rclpy._rclpy_pybind11.RCLError: failed to shutdown: rcl_shutdown already called` 的栈（2026-11-03 之前每次停止/回退都在车尾留下一个栈并把退出码写成 1，很容易被读成崩溃，而 `finally` 里那行之后的任何新代码也永远不会执行）。收尾顺序仍是被测的：先 `stop_session`，再发零速 `Twist`，再停 HTTP，最后才在 `rclpy.ok()` 仍为真时关 context（`tools/ros2_collection/tests/test_agent_exit_teardown.py`）。rclpy 自己那句 `Failed to publish log message to rosout: publisher's context is invalid` 还会出现——context 已经没了，日志发不到 rosout，属可接受的现场噪声，不是新的失败。

每完成一项，把车端 `/status`、`manifest.json` 片段、页面截图或导出报告存到 `docs/evidence/`（带日期），并区分"实测""仅页面显示""由后续反馈推断"三种措辞——页面 pill 显示"接受"不等于机械已经执行。


## 6. 同步与导出

1. 页面/API 结束 session，agent 写 `manifest.json` 和 `READY`。
2. `POST /ros-mobile/sync` 通过 HTTP 拉取 `.tar.gz`，在暂存目录解压，逐个比较 manifest 中的 size 和 sha256，然后原子发布到 `upper/sessions/<id>`；哈希不一致会返回 422 并保留原件。
3. `POST /ros-mobile/export` 先做只读/同步/字段预检，再启动独立 worker 进程；返回 `job_id`。查询 `GET /ros-mobile/export/status/<job_id>`，取消 `POST /ros-mobile/export/cancel/<job_id>`。worker 在子进程中读取上位机 bag，按 10 Hz 选择帧时刻之前最新且未过期的图像/关节/模型 odom，动作使用网关已发送目标的零阶保持（最长 3 s），禁止未来观测；尺寸/字段/缺失检查通过后才写 `LeRobotDataset.create → add_frame → save_episode → finalize`，并用本地 SDK 验证后原子发布。
4. 导出按 session metadata 里的 schema 分支，写死维度已取消：`export_schema=arm_gripper_6dof_phase1` + `runtime_mode=hardware_teleop` 的遥操作 session 导出 5 关节 + 1 夹爪共 6 维，action 取“限幅后实际发送的目标”（臂与夹爪分别取最近命令，任一侧过期即失败而不是延长旧值），state 取实测从臂反馈，成功标签留 `manual/unknown`；锁定底盘不补零成 9 维。仿真 session 仍导出原 9 维布局，字段名与既往一致。其余情况——非 `simulation`/遥操作 source、`training_export=false`、缺 action 或关节/底盘反馈、图像尺寸不符——返回 409，不生成训练数据；只读 session 只允许同步原始 bag/事件。数据集采样频率取整后再交给视频编码器（本机 torchcodec 缺 libavutil 会退回 pyav，而 pyav 不接受浮点 fps；真车腕相机声明 14.74 Hz 会在编码阶段整轮失败），报告同时保留 `declared_camera_fps` 与一条 warning，仿真 10.0→10 行为不变；`sample_image_shape` 现在按 session 自己的影像键读取（遥操作是 `observation.images.wrist_real`）。这条导出路径由 `app_ros_mobile/tests/test_ros_mobile_teleop_export.py` 用真实 LeRobotDataset 写到再读回（只替换录包读取器与首帧几何探测）。同一用例还直接把导出的数据集交给网页数据页所用的 `dataset_viewer.list_episodes / video_path / episode_timeseries`：相机名必须是 `wrist_real`、mp4 真实存在、state/action 序列每行正好 6 列（既不是补零的 9 列，也不是主臂读数）。
   **取整后的时间戳已单独核验（2026-11-03，`test_the_rounded_sampling_rate_keeps_dataset_and_video_timestamps_agreeing`）**：采样网格按 `1/round(fps)` 在 bag 时间里推进，LeRobot 又给每帧写 `frame_index/fps`，两者同一步长，所以逐帧时间戳与录制墙钟一致；mp4 用 pyav 读回后帧数等于数据集帧数、平均帧率正是 15、pts 步长恒定、片段长度等于 `frames/15`。代价是可量化的一项：按真车 67.84 ms 的相机周期喂 3 s 数据时得到 45 张影像、导出 46 帧，即约 2% 的帧复用同一张相机图，`image_age_s` 上界 0.0667 s（仍远小于 0.20 s 的新鲜度门禁，也不会取到未来的帧）。两次变异检查证明这些断言是活的：把网格改成按声明频率推进 ⇒ 网格断言立刻变红；把取图改成向前看 100 ms ⇒ "不允许未来的影像"变红。结论：取整不把时间轴说假，但它确实让约 2% 的真实相机时刻被复用；若以后要按真实 14.74 Hz 编码，需在隔离环境装好 libavutil/torchcodec 再验真实编码路径（只装系统库不保证编码器接受非整数帧率）。
5. 同步是单向上位机拉取：大 session 的 `.tar.gz` 会把 Wi-Fi/SSH 隧道带宽占满，此时 `/status`、心跳与命令都可能超时（表现为 `503 car agent timeout`）。遥操作或录制进行中不要同步。
6. 数据集默认写入 `HF_LEROBOT_HOME/lelab/...`，不执行 Hub 上传。

## 7. 已知限制（必须与本手册一起阅读）

- **底盘执行模型**：参考四球轮在 4 cm 轮径下负载分布不稳，实测纯轮地麦轮会固定对角漂移。当前用 Gazebo 平面 `VelocityControl` 执行底盘三自由度；`observation.state` 的底盘速度来自 `/lelab/base/model_odom`（实际模型位姿），`MecanumDrive` 的 `/lelab/base/odom` 只作独立诊断。不能据此声称已经验证真实麦轮接触力学。
- **频率**：`/joint_states` 由 Gazebo 每物理步发布（约 1 kHz），录制改由 agent 以 50 Hz 发布 `/lelab/recording/joint_states`。相机原始 RGB 仍未压缩，640×480/约 14 fps 约 0.8 GB/min；Pi 8.7 GiB 余量下只适合短片段，需依赖容量门禁和独立介质。
- **传感器**：腕部 RGB 640×480/约 14–15 Hz，深度 32FC1/约 15 Hz，camera_info 为 RGB 内参（深度 camera_info 未单独发布），激光 270 线/约 9.7 Hz；IMU 尚未作为可用模态验证。深度/激光只保留原始数据。
- **相机**：保留 `wrist_original`（0 rad）与 `wrist_demo_downward`（0.55 rad）两个 profile；由 launch 的 `camera_profile` 选择并写入 session/导出。实机安装与虚拟俯仰尚未校准，不能把 0.55 rad 当作实机外参。
- **专家**：`scripted_expert` 使用仿真真值做规划/目标确认/成功判定；真值不进入 state/action，显式标记 privileged。不是真人示教。箱顶任务检查支撑、释放后稳定和夹爪撤离，抬升阈值统一为 0.03 m。
- **时间/故障**：已验证只读无执行器输出、上机 backend 被杀后车端本地收尾、bag 提前退出不 READY、同步哈希校验、只读训练导出拒绝、导出 worker 状态/结果、遥操作会话的隧道超时/断流降级与页面 fail-closed 渲染。尚未完成：真实时钟回跳分段、Pi 断电级断连、HTTP 传输篡改端到端测试、真机遥操作中的失联停车。
- **遥操作标注与保持行为**：真机 episode 的 `success` 只可能是人工标注（结束本集时选 成功/失败）或 `unknown`，车端不套用 Gazebo 真值；episode 一旦关闭没有改判入口（manifest 写保护），未标注的集在导出里按“待标注数据”保留而不是当作成功集。车端 `hold_policy` 声明的是“位置模式保持最后指令位置、从不写力矩”，这套说法来自 vendor 侧代码与配置，真机上手臂断电/超期后到底停在哪、是否真的不掉，仍属八.5/八.6 待现场验证。
- **硬件**：实机只读录制（Aurora 相机帧、车端 bag、READY）已在 2026-11-02 取证，主臂 SO-101 串口只读已在 2026-11-03 取证（见 `docs/evidence/`）。仍未做：车端 controller/servo 真实从臂反馈、任何真机运动、真机急停与断力矩行为、车端遥操作 profile 部署。这些必须在获得明确运动授权后由操作员现场验证，不能用仿真或只读证据代替。

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

2026-11-03 遥操作轮次实测：`tools/ros2_collection/tests` 共 **146 passed**（含每控制周期只读一次车端状态的计数项、三层标定 schema 示例 9 项、最小启动入口脚本静态与 dryrun 9 项、会话协调器拒绝路径、映射/单位/回绕/超时/状态机、主臂串口独占 4 项、按部署版 `/status` 嵌套形状跑的门禁回归 4 项）；`app_ros_mobile` `-k "ros or leader or teleop"` 共 **54 passed**（含六维/九维导出分支、遥操作 HTTP 路由与“先开主臂再取租约”顺序、身份转发、`leader_identity`/标定拒绝、桥接超时与同步断流映射、按安装版 LeRobot 构造只读总线的 4 项、租约/反馈状态契约形状 5 项）。前端 `npx tsc --noEmit` 除既有 `Training.tsx` 报错外无新增，`npm run build` 通过。这些只覆盖软件路径，不等于实机遥操作验收。

状态契约轮次（同日，车已关机、主臂 USB 已拔）新增取证：`DeployedStatusShapeTests` 4 项确实会在“只读顶层字段”的旧实现下全红（临时回退验证过），修复后全绿；合成桩 `/tmp/lelab_stub_car_agent2.py` 已逐字段改成部署版形状（`control.active/client_id/expires_in_s/estop_latched/follower_feedback/teleop.chassis_locked`）。实机 18193 + 桩 18111 的 HTTP 与页面核验记录在 `docs/evidence/so101-mobile-teleop-software-rehearsal-20261103.json` 的 `status_contract_round`：未观测前 `chassis_locked=null`（页面显示“未知”），桩上报未锁定时页面显示“底盘 未锁定 · 观测 5.2s 前”且缺失项追加 `chassis_must_stay_locked_in_phase_1`，而本机 profile 仍是 `true` —— 页面不再把配置默认值当成车端状态。空闲时网络面板里 0 次 `/teleop/readiness`。车端录制行的契约也已补测：`teleop_state_payload()` 成为纯函数（`agent.py` 的发布路径只做取值转发），测试钉住 18 个字段、读数/映射前目标/实发目标三份互不混淆、`executed` 只能写“不可直接观测，仅由后续反馈推断”、无指令时留 None 不补零、反馈携带 source/age/脉冲原始值、ack 只保留最近三次；另加 profile 记录契约：导出器需要的动作源/状态源/事件/相机/遥操作状态话题都必须在 recording.topics 内、required ⊆ topics、`configs/` 与 `ros2_ws/.../config/` 两份 teleop profile 的 recording/teleop/limits/modes 必须一致，且只读 profile 的 `arm_sources` 不含 `leader_arm`（新增能力不得放宽已验收的只读 profile）。本轮一次全套里 `test_rosbridge_source.py::test_large_fragmented_message_and_ping_pong` 失败一次，单跑三次与随后的全套均通过 —— 仍是已知的本地时序敏感项。指令信封契约本轮补测：`tools/ros2_collection/tests/test_command_contract.py` 18 项直接跑 `contract.CommandGate` 与部署 profile，覆盖旧 session 拒绝、重放/乱序 `stale_seq` 拒绝、被拒命令不推进序号水位、`bad_seq`（含 `true`）、来源白名单、模式不符、关节角夹取与 clipped 回报、5 关节形状、非有限值、轨迹时长夹取、夹爪限位，以及 `hardware-teleop-v1` 的底盘锁定（`modes.base` 为空且三个速度上限为 0.0 → 任何速度指令 `bad_source`）。同时把 profile 里声明却无人执行的 `arm_command_ttl_s` 变成真实语义：车端按自己的单调时钟判断手臂流是否仍在时效内，`/status` 上报 `control.teleop.stream`，页面新增“车端手臂流”pill（在时效内 / 已超龄 / 尚未收到目标 / 未知）。这些仍是上位机测试，Pi 上生效需重新部署车端代码。同一轮还修掉控制环重复读取：`send_once` 原先每周期 `_gate()` + `preview_step()` 内再 `_gate()` 又自取 `/status`（20 Hz 下约 60 req/s 的额外 GET），现改为 `_status_snapshot()` 一次读取供门禁与预览共用，并有计数测试（临时退回旧写法时报 got 2 而失败）。本轮改动后的全套 `app_ros_mobile/tests` 完整重跑：**519 passed / 1 failed（733.84 s）**，唯一失败仍是上述 `test_remote_inference.py` 既有断言漂移（与遥操作无关，未改动）。

独占控制轮次（同日，车仍关机、主臂 USB 仍未接上位机）补上需求六里一直没有人执行的那句“确认 vendor joystick/app 等入口不会同时控制从臂；无法取得独占控制就拒绝使能”：审计确认 `set_mode` 原先只看 profile 是否启用 `leader_arm` 和反馈是否存在，拿到 HTTP 租约并不证明没有别的节点在直接驱动同一批舵机。现在 `contract.exclusivity_report()`（纯函数）按 `exclusive_guard_topics` 枚举发布者，只有本网关节点或 `exclusive_exempt_nodes` 白名单算干净，出现外来节点返回 `409 vendor_publisher_present`，某个话题枚举失败返回 `409 publisher_topology_unknown` —— 证据取不到同样拒绝，不默认放行；两种拒绝都只写 `command_rejected` 事件、不改 `control_mode`、不发轨迹。`/status` 的 `control.teleop` 增加 `exclusivity_required/exclusivity_guard_topics/exclusivity`，上位机在 `_gate()` 里顺带缓存车端结论并由 `/teleop/status` 以 `car_exclusivity` 透出（未观测前 `null`），被拒时 `acquire()` 回滚上位机租约、把车端原因原样交给页面；页面新增“车端独占”pill（未检测 / 可用 / 不可用并列出外来节点名）。取证：`tools/ros2_collection/tests/test_agent_exclusivity_guard.py` 17 项里 8 项用 `object.__new__(MobileAgent)` 直接跑**部署版** `set_mode`（上位机 Jazzy 环境可导入 rclpy 与消息包，只替换拓扑枚举），临时删除拒绝分支后其中 3 项变红，恢复后全绿；协调器侧新增被拒回滚与嵌套形状透出 3 项，`tools/ros2_collection/tests` 现为 **166 passed**，app 目标子集 **54 passed**（本轮未重跑 app 全套），前端 `tsc --noEmit` 干净并重建 dist。HTTP 与页面实测用嵌套形状合成桩 18112 + 后端 18194（独立临时标定 home，未碰车端 8110 与用户 18186 实例）：`car_exclusivity` 未观测前为 `null` → 桩上报 clean 后 `{clean: true}` → 桩上报 `joy_to_motor、mentorpi_app` 后 `{clean: false, reason: vendor_publisher_present}`，页面上 pill 实测文本依次是“车端独占 不可用（joy_to_motor、mentorpi_app）”和“车端独占 可用”。停机时踩到同一个坑：`nohup … &` 的 `$!` 是包装 shell，真实监听子进程仍在，按 `ss -ltnp` 的 pid 逐个停掉后端口才释放。仍未验证：真车上 vendor 摇杆/App 的实际节点名与是否常驻（上车后先 `ros2 topic info -v` 再决定白名单）、Humble 上 `get_publishers_info_by_topic` 的超时/未就绪形态，以及八.5/八.6 的实机运动与故障注入项；车端代码需重新部署到 Pi 才生效（Pi 跑的是 `deploy_readonly_agent.sh` 投递到 `/home/ubuntu/lelab_car/` 的那份，不是 `ros2_ws/install/` 副本）。

主臂实机只读已单独取证（2026-11-03，`docs/evidence/so101-leader-serial-read-20261103.json`）：`5B90149652`（右主臂）按 by-id 打开，`port_claim=flock+tioxcl`，3.01 s 内 88 次读取（实测 29.2 Hz，配置 30 Hz），帧年龄 0.008 s，六轴齐全、0 次失败，夹爪 2.48 %（0–100 开合量，不作角度）；期间第二个进程 `O_RDWR|O_EXCL` 打开被拒 `EBUSY`，`close` 后端口恢复可打开。全程只读：只调用 `connect`/`sync_read`，关闭用 `disconnect(disable_torque=False)`。车端从臂反馈与任何执行器输出仍未验证。

保持策略与成功标注两轮补测（同日，车仍关机）。保持策略：`hardware-teleop-v1.teleop.hold_policy` 写明位置模式保持最后指令位置、力矩从不作为停止手段，车端 `/status.control.teleop` 上报 `hold_policy` 与 `torque_control_offered: false`，上位机以 `car_hold_policy` 透出并作为页面“车端手臂流”pill 的悬停说明；`test_the_deployed_agent_never_writes_torque_state` 扫车端四个源文件的 torque 调用/键形状（命中即失败，注释散文不算），`test_the_deployed_status_block_declares_hold_and_torque_claims` 断言状态块四个键仍在——`status()` 需要真实节点才能执行，这两道静态守卫是此处可行的钉子。成功标注修掉一个会静默丢数据的缺陷：`_evaluate()` 原先只在 `mode == hardware_readonly` 时给 unknown，`hardware_teleop` 会落进 Gazebo 物理分支去读真机上永不存在的 cube/container 位姿，于是每条遥操作 episode 都记成 `success: false`，再被“只剔显式 false”的导出过滤丢光。现在判定来源写在 profile 里（`task.labels == "manual_or_unknown"` ⇒ `evaluation_policy.simulator_truth_available = false`），走 `manual_evaluation()`：默认 `unknown`，页面“结束本集”时人工选 成功/失败 + 备注，经 `/recording/end_episode` 原样进入 manifest 的 `evaluation` 与 `record_end` phase；只读分支字段不变，仿真仍走物理门且导出仍只留 `success is True`。另把“急停不等待保存”钉进原有用例：会话与 episode 都开着时触发 estop，其后不得再出现任何 `/recording/` 调用（只允许此前的 start_session/start_episode）。新增 `test_episode_success_labeling.py` 14 项（含真实 SessionRecorder 落盘：标注进 manifest、unknown 不算 false）+ 协调器透传 1 项；删掉策略分支后 5 项变红（报错正是缺 cube），删 profile hold_policy / 改 `car_hold_policy` 为 None / 删状态块字段各让 1 项变红，全部还原。计数：`tools/ros2_collection/tests` **194 passed**、app `-k "ros or leader or teleop or lease"` **88 passed**，前端 `tsc --noEmit` 干净并重建 dist。可验收范围据实收窄：真机上“手臂到底停在哪、断电/超期后舵机是否仍保持位置”属八.5/八.6 待实测；episode 关闭后没有改判入口（manifest 写保护），事后修订需要显式流程，本阶段未实现，因此 unknown 集要当作“待标注数据”而不是“成功集”看待。

六维导出的端到端演练（同日，车仍关机）：八.7 要求“符合 schema 的数据由本地 LeRobot SDK 读回”，此前这一层只在 schema 选择与 `_read_bag` 解析两个缝隙上测过。`app_ros_mobile/tests/test_ros_mobile_teleop_export.py` 7 项用真实 `LeRobotDataset` 走完 create→add_frame→save_episode→finalize→SDK 读回，只假掉录包读取器与首帧几何探测，其余（导出门禁、帧对齐、维度、成功标签过滤、重复导出保护、报告字段）都是生产代码；它钉住 action=限幅后实发目标且与主臂读数不等、state=实测反馈、逐帧保留 leader/pre_clip/clipped/seq/clutch/标定版本、`executed` 只写“由后续反馈推断”、unknown 与人工成功可导出而显式失败被剔、缺臂指令 `ExportRejected`；第 7 项接着走页面侧：直接调用数据集页面用的 `dataset_viewer.list_episodes / video_path / episode_timeseries`，断言相机列表是 `wrist_real`、mp4 真实存在、state/action 序列每行正好 6 列且数值等于限幅后目标与实测反馈（不是补零的 9 列，也不是主臂读数）。第一次运行就发现两处只有真写才会现形的缺陷：本机 torchcodec 缺 libavutil 导致编码退回 pyav，而 pyav 拒绝浮点 fps（导出器原本原样传 profile 声明的相机频率，真车约 14.74 Hz ⇒ 真机遥操作导出会在编码阶段整轮崩），现改为取整采样频率并在报告保留 `declared_camera_fps` + warning（仿真 10.0→10 行为不变）；验证块硬编码仿真影像键，遥操作 `observation.images.wrist_real` 的 `sample_image_shape` 一直静默为 null，现按 schema 自己的键读取。两处变异检查（还原浮点 fps ⇒ 3 项复现 `'float' object has no attribute 'numerator'`；把 action 换成 leader ⇒ 读回断言 5/6 元素不匹配）后全部还原。含本轮改动的 app 全套重跑为 **528 passed / 1 failed（796.90 s）**：唯一失败 `test_remote_inference.py::test_client_command_targets_the_tunnel` 是 HEAD（4beecee）就存在的断言漂移——`rollout._format_cameras_arg` 会给 linux opencv 相机追加 `backend: 200, fourcc: MJPG`，该用例仍期望没有这两个字段；`lelab/rollout.py` 和这条测试文件在工作区都没有改动，上一轮（**522 passed / 1 failed，794.28 s**）红的也是同一条。遥操作五个测试文件（coordinator / lease / export / routes / leader-bus）单独重跑 **37 passed**，因为本轮曾对协调器做过一次约 1 秒的变异-还原，为避免窗口污染不照抄全套结论。第三处变异检查针对页面读回：把 `dataset_viewer.camera_names` 的短名截取由 `key.split(".")[-1]` 改成 `[-2]` ⇒ 该用例以 `['images'] != ['wrist_real']` 失败，证明相机名解析被钉住；恢复后文件与备份 sha256 逐字一致（`git diff` 对该文件为空），7 passed 复现。若以后升级 ffmpeg/torchcodec 或 LeRobot，这条测试要重跑，因为它的通过依赖实际编码器可用。

跟随线程的自停与门失效原因（同日，车仍关机）：需求六 说"任一失效则退出跟随"，审计发现这句话在两个地方是空的。① `stop_follow_loop()` 会被跟随线程自己调用——`send_once` 里每次发送失败/预览出错都走 `demote_to_paused` → `stop_follow_loop` → `thread.join(自己)` → `RuntimeError: cannot join current thread`；异常在写 `last_error`、发 `invalidate` 之前抛出，所以真机上第一次故障不会记录原因，页面只能看到外层兜底的 `follow_error:RuntimeError:…`，状态名义上还停在 following（先用复现脚本证实再改，不是推测）。② 主臂拔线/读失败这类"门失效"更安静：`guard_following` 已经把状态降到 paused 并清空对齐，但 `send_once` 直接 `return None`——线程仍以 command_hz 轮询车端，真正缺的是"把发送流停下来"这一半（`gate_lost:<哪项>` 这个原因本来就由状态机的 `_on_clear_targets` 写进 `last_error`，页面"最近错误"有内容）。现在①不 join 当前线程，②门失效时真的停流。**这里本轮先写错了一次**：最初把②写成"页面根本没有原因可显示"，是变异检查逼出来的——把新增分支改成恒假后用例只在 `assertIsNone(coord.follow_thread)` 上失败、`last_error` 断言仍通过，于是删掉协调器里重复写 `last_error` 的代码，只保留停流。②改完带出一个次生风险：自停路径不能 join，旧线程可能还睡在 `wait()` 里，操作员快速重新接合就会有两个循环同时发指令，于是跟随循环加了世代号（`_loop_generation`），被取代的旧循环每周期开头自查后退出、一条指令都不发。新增 3 项测试并做反向变异（临时还原两处修复 → 前两项立刻以 `RuntimeError: cannot join current thread` 与缺失原因变红；第三项带 1 s 看门狗，保证守卫失效时是断言失败而不是挂住）。顺带修掉一条一直在和活跃线程赛跑的旧测试：`test_send_once_reads_the_car_status_once_per_control_cycle` 原先在 engage 起真实循环后数 `/status`，读到 1 只是运气，现在先 park 再测；单独探针确认生产路径本来就是一周期一次读，不是回归。计数：tools **203 passed**（含补测的"租约在跟随中过期"，该用例也钉住 `follow_thread` 必须为 None）、app 遥操五个文件 **37 passed**、app 全套 **529 passed / 1 failed（672.59 s）**，唯一失败仍是 HEAD 既有的 `test_remote_inference` 断言漂移；取证见 evidence 的 `follow_loop_parking_round`。仍未验证的是真机上"拔线到页面出现 gate_lost 有多久""暂停后车端访问频率是否真的掉下来"，属八.6。

"结束本集后暂停跟随"落到服务端（同日）：七 里这句此前只有页面在管——`MobileTeleopPanel` 的按钮先发一次 `{event: pause}`（仅当状态是 following）再发 `end_episode`，协调器与车端都不负责停。直接打 REST（脚本、复现、以后的自动化）或两次请求之间掉线/失败时，会出现"集已结束但手臂仍在跟随"。现在 `recording()` 在 `end_episode` 成功后若仍是 following 就走既有 `set_state("pause")`（停线程、清对齐、必须重新对齐+显式接合），页面那次点击变成冗余而不是唯一保证。新增 1 项测试钉住这条，变异检查（分支改成恒假）以 `assert 'following' != 'paused'` 变红后还原。顺带记一次工具性教训：第一次变异没生效是因为锚点里的中文引号与源码不一致——`assert` 拒绝了写入，之后一律先 grep 数锚点再改。计数：tools **200 passed**、app 遥操五个文件 **37 passed**；取证见 evidence 的 `episode_end_pause_round`。上车时第 8 步的判据也跟着变：结束本集后不需要额外点暂停，页面状态应自己回到"已暂停"，且 `/teleop/status` 的 `state` 是 `paused`。

映射模板的 `wrap` 标志纠正（同日）：`configs/mobile_teleop/mapping.example.json` 原先给 joint1（base yaw）也设了 `wrap: true`。回绕只让"主臂差值取最短路径"，对本来有机械限位的主臂关节是错的：`leader.example.json` 声明 shoulder_pan ±200°（3.49 rad > π），所以 ref=-3.3 → now=+3.3 这种合法大幅移动差值 6.6 rad 会被折成 -0.08 rad —— 预览上看起来"几乎没动"，映射目标却把底座往反方向带；`wrap: false` 时同一输入被夹到 +2.0，方向不变。依据来自用户给的参考库 `/home/liaozhengyan/Desktop/resource/lander_pi/src/driver/kinematics/kinematics/transform.py`（2026-11-03 查阅：joint1 与 joint5 都是 [-120.2, 120.2]），我们 profile 的 limits 也全是 ±2.09 rad。现在只有 joint5（主臂 wrist_roll 是连续舵机，会真的越过 ±π）保留 `wrap: true`，注释也改写为"回绕解决主臂读数环绕，不等于宣称从臂关节无限位，回绕之后仍按从臂机械/线缆限位夹取"。同时发现旧用例正是用 joint1 验证回绕，等于把错标志"背书"而不是发现它——测试选关节时没有对照硬件语义。现在回绕用例改用 joint5，并加两项守卫：示例里只允许 joint5 wrap；有限位关节的大差值必须夹向命令方向。`TeleopMapper` 算法没动（wrap 一直是按关节开关实现，选哪个关节属于 B/C 层标定），但页面"载入模板"读的就是这份 example，所以模板语义是这条链的入口。计数：tools **202 passed**；取证见 evidence 的 `mapping_wrap_flag_round`。真车上 wrist_roll 能否连续越过 ±π、joint5 的实际机械/线缆范围仍待操作员现场确认。

上一轮全套 `app_ros_mobile/tests` 为 **512 passed / 1 failed（794 s）**。修掉的那条 `test_train_server.py::test_status_endpoint_is_registered` 是 `{route.path for route in app.routes}` 撞上 FastAPI `_IncludedRouter`（自 4beecee 引入 include_router 起就存在），已按 `test_server.py` 的写法加 `hasattr(route, "path")` 守卫。仍失败且与本阶段无关的是 `test_remote_inference.py::test_client_command_targets_the_tunnel`：安装版 LeRobot 现在把相机配置序列化成 `{… fps: 30, backend: 200, fourcc: MJPG}`，测试期望字符串没有这两个字段；属远程推理链路的既有断言漂移，未在本轮改动。`ruff` 在 `app_ros_mobile/.venv` 与 ros_jazzy 环境均未安装，本轮没有执行 Ruff。

计数更正与时间戳核验（2026-11-03 上车前置核查那一轮，全部只读、车与主臂都未接）：① 早前几轮写的"app 遥操作五个文件 37 passed"这一轮复现不出来——同一组文件逐个实测是 leader-bus 4、app coordinator 7、export-schema 6、teleop-export 7、routes 7，合计 **31**（本轮加时间戳用例后 **32**），把 `test_hardware_lease_api` 全 12 项一起跑是 **43**，凑不出 37。以后按逐个文件计数写，不照抄历史数字。宽子集 `-k 'ros or leader or teleop or lease or export or sync'` **115 passed**，`tools/ros2_collection/tests` **203 passed**；app 全套唯一失败仍是 `test_remote_inference.py::test_client_command_targets_the_tunnel` 的既有断言漂移，按用户要求保留为已知问题。② 用户点名的"取整帧率下真实帧时间戳是否正确"已单独核验并钉进测试，结论与代价见 §6 第 4 条；取证 `docs/evidence/so101-teleop-export-timestamp-check-20261103.json`。③ 55 项改动已按功能分组提交到本地（未推送），只读审核记录见 `docs/evidence/so101-mobile-teleop-hardware-precondition-20261103.json`。

A 层标定复用与身份绑定（2026-11-03T12:08Z，上位机）：需求四 A 的"复用现有设备识别和标定逻辑"原先只是让操作员把 lerobot 已经写好的 6 电机 × 5 字段**手抄**进网页，而 `build_bus` 只校验字段齐不齐、不校验这份标定属于哪台臂——拿右主臂的标定打开左主臂会一路通过：读数连续、年龄正常、度数是错的，映射层与防跳变状态机都收不到任何信号。现在 ① `GET /ros-mobile/teleop/leader-calibrations` 只读枚举本机 `teleoperators/*/*.json`（root、sha256、电机 id、缺哪个电机/哪个字段、不可解析原因，只有 usable 的条目带回原文），页面"列出现有主臂标定 → 填入"复用原文且从不写回那棵树；列表为空时页面直接区分"没标定过"与"`HF_LEROBOT_CALIBRATION` 指错目录"——本机实测默认根 `~/.cache/huggingface/lerobot/calibration/teleoperators/so_leader` 是空目录，真实文件在 `~/.local/share/lelab-jetson-20261031/approot/hf/calibration/teleoperators/so_leader/{left,right}_rig.json`（两份 usable、id 序列 1..6，sha256 前缀 a3c121c5… / 4f4d2703…）。② `open_leader` 在建总线之前逐电机比对标定 id 与轮询 id，不一致以 `409 leader_calibration_does_not_match_this_arm: shoulder_pan:calibration_id=7!=bus_id=1` 拒绝，不创建 reader、不产生串口流量。计数：tools **212 passed**、app 遥操作五文件 **33 passed**、宽子集 **116 passed**、前端 tsc 干净并重建 dist；变异检查把守卫改成恒不触发后两项拒绝用例变红、正向"标定逐字交给总线"仍绿（还原后 sha256 `d92b15ea…` 与备份一致）。仍未验证：by-id 序列与"这台物理臂就是这个 rig 名"的对应仍要操作员肉眼确认，本改动只保证 id 层一致。取证 `docs/evidence/so101-leader-calibration-reuse-20261103.json`。

## 10. FrameLink 离线闭环的启动、停止与剩余门禁（2026-11-06 UTC 实测，车与主臂均未接）

上位机生产 facade（`frame_stream_v1`）→ 生产 agent/控制进程 → 真实 sqlite3 两集 → 校验同步 →
受限 6D 导出 → 本地 LeRobot SDK 与 DatasetViewer 逐轴读回，已在隔离 Humble 实例上闭环；
分阶段取证见 `docs/evidence/landerpi-facade-p1-web-closure-2026-11-05/README.md`（网页点击与负向流程）、
`docs/evidence/landerpi-facade-p2-fault-matrix-2026-11-05/README.md`（故障矩阵）、
`docs/evidence/landerpi-facade-p3-frame-benchmark-2026-11-05/README.md`（真实链路时序）、
`docs/evidence/landerpi-facade-p4-build-and-candidate-2026-11-05/README.md`（回归、ARM 尝试、候选包）。

最小 harness 已被生产 server 取代：`tools/ros2_collection/local_ros2/offline/facade_web.py` 起的是
`lelab.server:app` 全量路由，只把设备边界换成模拟（要求 agent 自报 `simulated=true` 且
profile `hardware_hybrid_manual_v1_offline6d`），未知 API 返回真实 404 JSON——不要把 HTML 200 当 API 通过。
启动/停止只走这一套环境变量与 `sg docker -c 'tools/ros2_collection/local_ros2/bin/docker up|down'`，
`--prepare` 复用冻结模拟标定（bundle `ae3bf41b…c62ca`），栈起来后不得重生成 bundle；
端口冲突或容器标签不属于本任务时应拒绝而不是接管。

三条以后不要重复调查的事实：① 帧模式急停/租约/恢复的语义在 2026-11-06 已闭合（取证见
`docs/evidence/landerpi-frame-safety-recovery-2026-11-06/README.md`）：显式急停必须同时让
agent `control.estop_latched` 与控制进程 `latched_fault` 置位，两者都算软件闭锁，不是硬件急停回路；
租约到期由 `_control_tick()` 计时器清理，不再依赖 legacy publisher 是否存在，
`/status.licence` 里"请求许可"（`frame_outputs_requested`）、"有效许可"（`frame_outputs_enabled`）
和"执行者自报"（`bridge.frame_process.outputs_enabled/latched_fault/age_s`）是三件事，读状态要分层；
清除急停要等**两层**都报告已清，否则上位写 `pending_or_failed` 并留在 estop，
到期/释放后桥接仍会把排队的清除送出（否则旧闭锁无人能解），清除只给恢复能力（`motion_granted=false`），
恢复必须走新会话→预览→对齐→待命→显式接合；② 任何流故障后 `_follow_fail` 会停租并置 `fault`，
恢复必须是"新会话"，页面在故障态里点对齐/接合是无效的；③ `sim_camera` 对 SIGTERM 不回话，
`container_ops.stop_recorded` 现在会按已核验身份升级 SIGKILL 并报 `killed_after_term_timeout`，
任何重启 fixture 的判据都要看它自己的计数器动没动。

重启隔离栈的两条环境判据：`docker down/up` 必须带上 `LROS_DOCKER_BUNDLE`（本任务 `ae3bf41b…c62ca`），
漏掉会让控制进程用默认 bundle 启动并立刻 `epoch_or_bundle_not_licensed`；容器内设置 PYTHONPATH
要写 `export PYTHONPATH=$C_SRC:${PYTHONPATH:-}`，直接覆盖会屏蔽 rclpy 造成成片假跳过。

候选包 `landerpi-frame-20261106T141033Z`（36 文件，sha256 `25c57aad…b6dfde`）是当前离线候选：
逐文件哈希与源码一致，车端 16 个模块在容器 Python 3.10 下全部导入成功，
`bin/package` 会拒绝"被导入却没进清单"的车端模块；旧的 `landerpi-frame-20261106T122111Z` 与
`frame-facade-offline-candidate` 已过时禁止部署。臂+夹爪这条发布不依赖新的轮子/固件协议增量：`hardware-hybrid-manual-v1-offline6d.json` 仍然只发既有
`/lelab_offline/ros_robot_controller/bus_servo/set_position`，上位 `begin_epoch` 只申请
`arm`/`gripper` 两个通道（`modes.exclusive=true`、`manual_hybrid_available=false`），
所以固件缺口不阻塞这条 Python/ROS 候选包，但也不等于可验收。
ARM64 与固件本轮只有失败证据（无交叉工具链、`exec format error`），没有任何二进制产物；
`deploy_readonly_agent.sh --apply/--stop/--rollback` 需要 Pi 可达，未执行。
真机门禁清单（含上面①②的处置决定）写在 P4 README 第 6 节，逐项确认之前不要宣称"只差上车"。
