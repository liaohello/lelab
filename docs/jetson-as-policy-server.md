# Jetson 作为远程 PolicyServer：实现与分阶段验收

日期：2026-10-31  
工作副本：`/home/liaozhengyan/Desktop/lelab-bimanual`（分支 `feat/local-bimanual`）  
应用：`releases/sanitized-20261024-01/app`；LeRobot：同一快照内 `releases/sanitized-20261024-01/lerobot`  
状态：局域网直连、真实 ACT 加载、单臂真实观测/影子、真实前端只读观测、5 分钟持续运行与断连恢复已完成；四台串口角色已确认并写入双臂记录，**双臂只读观测链路已开发但尚未实机启动，其余三台标定与实机执行未完成**。

## 1. 结论先行

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| 局域网链路 | **通过** | 本地直连 `192.168.31.204:28081`，无 SSH 隧道；PolicyServer 绑定 `0.0.0.0:28081` |
| 真实 ACT 加载 | **通过** | Jetson 容器加载 6 维单臂 ACT（两路相机 `right`/`up`），返回 100 步动作块 |
| 真实 ACT 影子推理 | **通过（单臂，不执行）** | 真实关节状态 + 真实相机 + Jetson 真实 ACT；本地只记录动作，`actions_dispatched=0` |
| 真实观测（Stage B） | **单臂通过；双臂通信通过（左臂未标定）** | 单臂为已标定 degree；双臂同时返回 12 通道，左臂明确标记 `raw_steps`，两路相机和 `ACTIONS_SENT=0` 已验证 |
| 双臂/其余三臂 | **角色已确认，标定未完成** | 四台已由拔插确认左/右 follower/leader 并写入 `left_rig`/`right_rig`；右 follower 已有标定，左 follower 和两个 leader 仍缺 |
| 单臂/双臂实机执行 | **未进行** | 无 12 维双臂策略，未获实机运动授权；没有激活 execute 门禁 |
| π | **仅条件评估** | 未加载、未验证；Jetson 上有约 12 GB openpi 资产，但未核对契约 |
| 合成策略 | 仅用于链路测试 | 不计入真实模型或实机验收 |

真实 ACT 影子推理已通过，但这只是“观测上传 + 动作返回”，不是实机闭环执行。Jetson 是 Orin NX 16 GB，其测量值不能外推为其他 GPU 服务器性能。

## 2. 运行边界

```text
本地主机（设备所有者）                         Jetson Orin NX 16GB（仅推理）
  LeLab 后端/前端                                PolicyServer 容器
  ttyACM0..3（4 个 USB 串口）                    192.168.31.204:28081
  /dev/video0,2,4,6（3 个相机）                  只接收观测，返回动作块
  只读状态/相机 → 观测上传
  动作接收 → 本地 TTL/契约/门禁 → 才可能执行
```

- 没有把机械臂/相机转接到 Jetson，也没有在 Jetson 部署串口/相机/前端依赖。
- 本地保留串口、相机、标定、安全限幅、E-stop、动作 TTL 与重连责任。
- PolicyServer 是 LeRobot 的未加密 gRPC + pickle 协议；按用户授权只在可信局域网直连，未增加公网暴露或证书系统。

## 3. 实际资产与角色核对

### 本地串口

`/dev/serial/by-id` 中实际存在 4 个 CH340 适配器：

| 适配器序列 | 当前节点 | 说明 |
| --- | --- | --- |
| `5B91045575` | `/dev/ttyACM0` | **右从臂（`right_follower`）**，Jetson 旧 ACT 日志中使用的 `my_robot` |
| `5B90149339` | `/dev/ttyACM1` | **左从臂（`left_follower`）**，2026-10-31 由用户拔插确认 |
| `5B90149723` | `/dev/ttyACM3` | **左主臂（`left_leader`）**，2026-10-31 由用户拔插确认 |
| `5B90149652` | `/dev/ttyACM2` | **右主臂（`right_leader`）**，2026-10-31 由用户拔插确认 |

Jetson 旧日志（`~/Desktop/lelab/logs`）记录 ACT 客户端使用：

```text
robot.port=/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B91045575-if00
robot.id=my_robot
robot.cameras={right: index 0, up: index 2}
```

因此 `5B91045575` 能把旧 ACT 标定/相机语义对应到本机物理右从臂。2026-10-31 用户已通过逐根拔插把另外三台分别确认到左从臂、左主臂和右主臂；角色绑定文件位于 `docs/evidence/jetson-policy-server-20261031/hardware-inventory-20261031.json`。

### 本地角色确认（`/hardware`）

前端顶部新增“硬件角色”页（`frontend/src/pages/HardwareInventory.tsx`）。它只读枚举
`/dev/serial/by-id` 与 sysfs，不打开串口、不写标定、不改变力矩。操作方式：一次拔一根
USB 线，消失的串口会保留为“未连接”并记录拔出事件；按物理臂选择
`左/右 × follower/leader`，插回后确认恢复“已连接”，最后保存。

保存文件（时间戳副本 + latest）：

```text
<LELAB_BIMANUAL_HOME>/hf/hardware/hardware-inventory-latest.json
```

JSON 含序列号、设备节点、VID/PID、USB 路径、角色、备注、缺失角色和是否齐全。
角色文件只是后续标定/双臂配置的输入，不代表已标定，也不授权动作。

页面保存后，“应用到双臂记录”只把左右 follower/leader 的 stable by-id 路径写入既有
`left_rig`/`right_rig` 机器人记录，保留标定配置字段，不打开串口。新增
`ReadOnlyBimanualFollowerSource` 和 `POST /observe/bimanual/start`：依次读左、右两台
follower 的 `Present_Position`，组合为固定 12 维
`left_*`→`right_*`；两个总线的 write/sync_write/torque/calibration 方法均被守卫，断开时
`disable_torque=False`，WebSocket 始终报告 `actions_sent=0`。

### 本地标定

默认目录 `~/.cache/huggingface/lerobot/calibration` 为空；本机没有四臂标定 JSON。Jetson 仅有一份 follower 标定：

```text
~/Desktop/lelab/data/calibration/robots/so_follower/my_robot.json
sha256 c12c40e7da7021d42f8c91015f815ec26b56976d432eb9b5a2e2b8baa8c35226
```

该文件已复制到隔离根 `~/.local/share/lelab-jetson-20261031/calibration/`，**未覆盖** Jetson 原文件。2026-10-31 又按确认角色复制到应用实际读取的：

```text
~/.local/share/lelab-jetson-20261031/approot/hf/calibration/robots/so_follower/right_rig.json
```

因此右从臂可在双臂只读观测中使用已验证标定；原 `my_robot.json` 仍保留。左从臂和左右 leader 仍需现场逐台标定，本任务未在后台自动标定。

### 本地相机

实际枚举到 3 个 USB 相机（Azurewave、Sonix、ZSKJ 集成相机）。旧 ACT 使用 index 0/2；本次实测：

- index 0：640×480，10 帧约 0.53 s，保存 `camera-0.jpg`
- index 2：640×480，10 帧约 0.61 s，保存 `camera-2.jpg`

相机设备节点有用户 ACL，可读；串口节点没有。

### 串口权限（已解决）

初始只读打开 `/dev/ttyACM0` 被拒绝：

```text
PermissionError: [Errno 13] Permission denied:
/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B91045575-if00
```

用户提供了本机授权口令；本任务只用它执行一次 `sudo usermod -aG dialout liaozhengyan`，口令未写入源码、命令参数、日志、报告或提交。随后通过 `sg dialout` 在当前会话直接运行观测/后端，无需重新登录。`getent group dialout` 已包含该用户；未修改 udev 或 `/etc` 其他内容。

## 4. Jetson 实际环境

| 项目 | 实测 |
| --- | --- |
| 主机 | `nvidia-desktop`，aarch64 |
| OS | Ubuntu 22.04.5 LTS |
| L4T/JetPack | R36.4.3（JetPack 6.2），kernel `5.15.148-tegra` |
| 内存/磁盘 | 15 GiB RAM，7.6 GiB swap；`/` 233 GB，约 108 GB 可用 |
| GPU 探测 | `nvidia-smi` 可用但显存/利用率显示 `N/A`（Orin）；改用 `tegrastats`、`docker stats`、`/sys/devices/gpu.0/load` |
| 管理入口 | SSH `192.168.31.204:22`，用户 `nvidia`，任务专用密钥 `~/.ssh/lelab-jetson` |
| 推理端口 | `28081` 启动前空闲；没有占用其他服务 |

### 环境选择（重要）

| 环境 | 结果 |
| --- | --- |
| Jetson 系统 Python 3.10 | 无 torch，且 LeRobot 0.6.2 要求 Python ≥3.12，不能直接用 |
| `~/Desktop/lelab/app/.venv` Python 3.12 + torch 2.11.0+cu128 | CUDA 可用但推理报 `CUDA error: no kernel image is available for execution on the device`，**不兼容 Orin** |
| `~/lerobot_edge` Python 3.10 + torch 2.8.0+cu126 | CUDA 矩阵乘法可用，但 Python 3.10 不能解析 0.6.2 源码 |
| `nvcr.io/nvidia/pytorch:25.06-py3-igpu` | Python 3.12.3 + torch 2.8.0a0（CUDA 12.9），实测 CUDA 矩阵乘法成功；**采用** |

没有复制本地 venv。以已有 NGC 容器为基础，只补装 PolicyServer 所需最小依赖（`grpcio`、`protobuf`、`draccus`、`huggingface_hub`、`av`、`einops`、`jsonlines`、`scipy`、`deepdiff` 等），提交为独立镜像：

```text
lelab-policy-server:20261031
container: lelab-policy-server-20261031
source: /home/nvidia/jetson-policy-server-20261031/lerobot/src
PYTHONPATH: 上述 source
checkpoint: /home/nvidia/Desktop/lelab/models/act_local_red_bowl_20260919_152702_2026-09-19_19-55-49_6cf4e08d/checkpoints/010000/pretrained_model
```

容器内已核对导入路径为 `/work/lerobot/src/lerobot/__init__.py`，版本 0.6.2。容器使用 `--network host --ipc=host --runtime nvidia`；只挂载隔离根和只读模型目录。

## 5. 源码一致性

本地与 Jetson 使用同一份 **未提交** LeRobot 源码树（513 个 `.py`）：

```text
local  digest = 8811aca34a481ea666aa1fbe53ad8e0823197d30b05946cbe5688d4777abe5c9
remote digest = 8811aca34a481ea666aa1fbe53ad8e0823197d30b05946cbe5688d4777abe5c9
files = 513 / 513
```

同步命令：

```bash
cd /home/liaozhengyan/Desktop/lelab-bimanual
ROOT=$PWD/releases/sanitized-20261024-01
PYTHONPATH=$ROOT/app:$ROOT/lerobot/src \
  .venv/bin/python -m lelab.jetson_policy \
  --ssh-key ~/.ssh/lelab-jetson --local-source $ROOT/lerobot/src sync
```

同步使用 rsync `--checksum --delete` 到隔离目录，并在远端重新计算清单；不是仅凭基线 commit 声称一致。

## 6. checkpoint / processor / 相机契约

从 checkpoint 元数据实际解析（`docs/evidence/jetson-policy-server-20261031/contract.json`）：

```text
policy_type: act
state: observation.state, shape [6]
images: observation.images.right, observation.images.up, shape [3,480,640]
action: action, shape [6]
chunk_size / n_action_steps: 100 / 100
normalization: VISUAL=MEAN_STD, STATE=MEAN_STD, ACTION=MEAN_STD
preprocessor: rename_observations → to_batch → device → normalizer
postprocessor: unnormalizer → device
metadata sha256: 5d428f5ff783e20665774a737325ba19e929d27b59788346546ee842d1fe6661
```

这是**单臂 6 维** ACT，不能复制、补零或拼接成双臂 12 维策略。双臂执行必须另找/训练与当前 12 维契约匹配的 checkpoint；本轮没有。

### 12 维 ACT 候选审计（未验证兼容，不用于执行）

本机存在一个完整的 12 维 ACT checkpoint：

```text
/home/liaozhengyan/Desktop/embodiedIntelligenceProj/bimanual_proj/outputs/
  multi_agent/g2_goal_20261009_1550/training/job1/official/checkpoints/002000/pretrained_model
```

审计结果（`checkpoint-12d-audit.json`）：

- 权重 `model.safetensors` 206,748,912 B，234 个张量，action head 形状 12；state/action 12 维。
- 相机 `central`、`left_wrist`、`right_wrist`，480×640；chunk 100 / n_action_steps 10；MEAN_STD。
- 训练数据 root 不存在；`bimanual_input_contract.json` 只记录形状/相机/fps，**没有关节名、单位或动作语义**。
- 因此不能证明其 12 维顺序、单位、相机视角、动作定义与当前四臂匹配；没有把它用于真实或影子执行。

四个串口的只读盘点（`four-arm-inventory.json`）：四个 CH340 均响应 ID 1–6，Torque_Enable 全 0；仅 `5B91045575` 有 `my_robot` 标定和角色证据，其余三台角色未知。该盘点不改变“只有单臂已验证”的结论。

## 7. 三种模式与保护

新增 `app/lelab/jetson_policy.py`：

- `observe`：只读关节/相机；不创建 gRPC stub、不发送观测、没有动作 sink。不是用零动作或保持动作冒充。
- `shadow`：上传观测、接收并校验动作、写指标；从不调用动作 sink。
- `execute`：只有 `ExecuteGate` 五项全部为真且提供 `BoundedActionSink` 才允许构造；TTL、形状、有限值、`max_abs`/`max_delta` 在本地再检查。当前没有对真实硬件启用。

关键实现：

- 每条观测有本地 `seq`；`TimedObservation.timestep=seq`，墙钟只用于服务端日志/FPS。
- 动作块按 `seq` 关联本地单调时间；TTL 用 `time.monotonic()`，不使用两机墙钟差。
- 第一块使用 `startup_ttl`（默认 5 s，本次 90 s）覆盖模型首次加载/首次推理；后续块使用正常 TTL。
- 旧会话/旧 epoch、乱序、未知观测、超时、NaN/inf、错误维度一律拒绝并进入 fault；fault 清空队列，重连需新建会话（`restart_session()`），不自动恢复旧队列。
- 服务器/客户端退出、队列饥饿、Stop 都会停止接收并显示明确 fault；不通过关闭保护使测试通过。
- 只读源在构造后把 `write/sync_write/configure_motors/enable_torque/disable_torque/write_calibration/...` 全部替换为抛错 guard；`disconnect(disable_torque=False)` 只关端口。假总线测试验证无写调用。

### 与现有 `remote_inference.py` 的关系

现有 `RobotClient` 在 `__init__` 中无条件 `make_robot_from_config(...).connect()`，SO follower 的 `connect()` 会 `configure()`（电机写入、力矩/参数）并在未标定时进入标定；因此**不能**直接用于 observe/shadow。本实现复用了：

- LeRobot `async_inference` 的 gRPC 消息、`TimedObservation`/`TimedAction`、`RemotePolicyConfig`、PolicyServer 与分块传输；
- 原有 SSH/远端目录/会话标记的隔离思路；
- 本地串口/安全/租约由 LeLab 继续拥有。

没有改动现有 `remote_inference.py` 的训练服务器隧道路径；Jetson 直连是独立入口，避免同时启动直连和隧道。

## 8. 阶段 A：保护测试

命令：

```bash
cd /home/liaozhengyan/Desktop/lelab-bimanual
ROOT=$PWD/releases/sanitized-20261024-01
PYTHONPATH=$ROOT/app:$ROOT/lerobot/src .venv/bin/python -m pytest \
  $ROOT/app/tests/test_jetson_policy.py -q
```

结果：**15 passed**（2026-10-31）。覆盖：

- 契约解析（真实 ACT 元数据、6/6、right/up、MEAN_STD、processor 步骤、sha256）
- 错误状态/维度、NaN/inf
- observe 模式不建 gRPC、不发动作
- shadow 收到动作但不 dispatch
- TTL 过期、乱序、旧 epoch
- execute 缺门禁/缺 sink 拒绝；有门禁时只 dispatch 校验后动作
- 服务断连 fault + 清队
- 重复 Stop 幂等
- 只读源无电机写、`disconnect(disable_torque=False)`

这组测试是合成的，不证明实机安全。

## 9. 阶段 B：真实观测（单臂通过）

用户已授权本机口令，用于一次 `usermod -aG dialout`；口令未写入源码、命令参数、日志、报告或提交。通过 `sg dialout` 在当前会话直接运行只读观测，无需重启登录。

实际 LeLab `/observe` 页面（`http://127.0.0.1:18186/observe`，应用以 `sg dialout` 启动）验证：

```text
WebSocket /ws/joint-data: connected，收到 45 次 joint_update
关节（真实 follower）：shoulder_pan 6.154, shoulder_lift -101.978,
  elbow_flex 96.967, wrist_flex 74.637, wrist_roll -67.429, gripper 0.484
相机：right/up 均 640×480，真实 JPEG；帧年龄约 3.6 ms
frames: 45（观测中）/108（Stop 时）
ACTIONS_SENT: 0
Torque_Enable: 六个电机全部为 0（前后一致）
phase: observing -> stopped；无 fault
```

证据：`ui-observe.md`、`ui-observe-status.json`、`ui-observe-stop.json`、`ui-observe-stopped.json`、`ui-camera-right.jpg`、`ui-camera-up.jpg`。应用已停止，18186 端口已释放。

限制：观测期间机械臂保持静止，没有人工诱导关节变化；因此“实时更新、真实读数、相机帧年龄、零动作”已验证，**运动中的状态变化未验证**。此外只验证了 `5B91045575` 这一台 follower；其余三台机械臂没有已核实的角色/标定，不能冒充双腕 WebSocket 验证。

### 双臂只读链路（已开发，待实机启动）

2026-10-31 用户已完成四台角色拔插确认并保存。`/observe` 已增加双臂模式：

- 左、右分别选择 stable by-id 路径和可选标定文件。
- 后端按左→右读取两个 follower 的 `Present_Position`，形成固定 12 维顺序。
- 两个 Feetech 总线实例的 write/sync_write/configure/torque/calibration 方法全部被拒绝。
- 断开使用 `disable_torque=False`；页面和 WebSocket 始终显示 `actions_sent=0`。
- 左 follower 当前没有标定文件。首次实机启动暴露出“未标定总线不能按 degree 归一化”的问题；现按每台标定状态读取：已标定返回 degree，未标定返回 `raw_steps` 并在前端明确标记，不再伪造物理单位。

本轮发现并修复未标定左总线不能按 degree 归一化的问题；修复后实际启动 78 帧双臂只读观测并正常停止，两路相机约 3.7 ms，12 个 Torque_Enable 全为 0，`actions_sent=0`。左臂因尚无标定显示 `raw_steps`，因此这证明通信、12 通道和零动作，不证明左右关节物理单位已可比。双臂臂体保持静止，运动中的状态变化仍未验证。

### 早期阻塞记录（已解决）

在授权前，`ReadOnlyFollowerSource` 打开 `/dev/ttyACM0` 被拒绝：

```text
PermissionError: [Errno 13] Permission denied:
/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B91045575-if00
```

该历史记录保留在 `stageB-observe-blocked.txt`；解决方式是加入 `dialout` 组，而不是关闭保护或伪造状态。

## 10. 阶段 C：Jetson 远程影子推理（单臂真实观测通过）

服务直连 `192.168.31.204:28081`，真实 ACT，真实 follower 关节 + 两路真实相机，5 fps，100 步动作块，`actions_dispatched=0`。

短程真实观测影子（`stageC-real-summary.json`）：

```text
observations_sent: 49
chunks_received: 45
actions_received: 4500
actions_dispatched: 0
chunks_rejected: 0
first_chunk_after_load_s: 0.724
upload_ms: p50 58.6 / p90 72.5 / p99 277.9
roundtrip_ms: p50 193.0 / p90 313.8 / p99 718.6
action_age_ms: p50 198.5 / p90 319.1 / p99 724.0
faults: []
```

真实观测细节：相机帧年龄约 65–83 ms；六个 `Torque_Enable` 在运行前后均为 0；首块动作有限（6 维）；服务端 0 个 ERROR。5 分钟持续真实观测影子见 §12：1440 帧、1435 个动作块、143500 个动作、0 拒绝/0 故障。

合成观测影子仍保留为协议测试（`stageC-synthetic-summary.json`），只用于对比链路/延迟，不计入真实观测验收。该阶段没有执行任何动作；不能把它写成实机闭环。

## 10.1 服务退出与重连

`stageFault-reconnect.log`：运行中的真实服务被停止后，客户端进入 fault（`observation_upload_failed: UNAVAILABLE / Stream removed`），`queued_actions=0`，没有 dispatch；重启服务后 `restart_session()` 建立 epoch 1 新会话，重新收到动作块（58 个），旧队列未恢复，fault 清除。该测试使用合成观测以复用总线外的确定性输入；故障与重连逻辑不依赖硬件。

## 11. 阶段 D：受限实机执行

未进行。缺少：

1. 与当前双臂 12 维状态/动作、相机键、processor 匹配的策略（现有 ACT 是 6 维单臂；不能复制/补零/拼接）；
2. 除 `my_robot` 外的三台机械臂角色/标定；
3. 明确的实机运动授权、现场支撑和 E-stop 配合；
4. 运动中的真实状态变化与方向/单位/限幅检查。

因此没有启用 `execute` 门禁，也没有向任何 follower 发送动作。当前 `execute` API 只有代码级门禁与假 sink 测试；`run_shadow.py` 不暴露 execute。

## 12. 阶段 E：持续联调

真实观测影子（`stageE-real-5min-summary.json`）：墙钟 300 s，连接后稳定段 289.3 s，5 fps，真实关节+两路相机。

```text
observations_captured/sent: 1440 / 1440
chunks_received: 1435
chunks_rejected: 0
actions_received: 143500
actions_dispatched: 0
faults: []
first_chunk_after_load_s: 0.301
upload_ms: p50 56.0 / p90 76.7 / p99 101.0
roundtrip_ms: p50 184.7 / p90 216.2 / p99 304.9
action_age_ms: p50 190.6 / p90 222.9 / p99 334.3
capture_ms: p50 5.33 / p90 8.02 / p99 12.51
```

Jetson 资源（`resources-real-5min-summary.json`）：

```text
container memory: p50 1.14 GiB, max 1.17 GiB
container CPU: p50 71.1%, max 75.2%
system RAM used: p50 3185 MB, max 3237 MB
GPU load: mean 35.3%, p90 99%, max 99% (1 s sampling)
```

服务端 1480 个动作块（含随后短程会话）总耗时 p50 121.9 ms / p90 130.2 ms / p99 144.9 ms，ERROR 0。真实服务停止→故障清队→重启→新会话的测试见 §10.1。观测期间机械臂静止，因此这是“真实传感器输入”的持续运行，不是运动跟踪测试。

## 13. π 评估

Jetson 上存在 `~/sensor/openpi/openpi_assets`（约 12 GB）和 openpi 源码，但：

- 未找到可直接复用的 π PolicyServer 契约/权重启动记录；
- 未核对模型类型、动作维度、相机键、相对动作/padding/后处理；
- 未加载到内存，未跑推理，未测延迟；
- 15 GB 统一内存需要同时容纳权重、激活和 CUDA 上下文，是否可行未验证；
- 不下载大模型、不训练、不使用付费服务器。

结论：π **仅完成条件评估**，没有实际加载/验证。不能在报告中写“π 已运行”。

## 14. 复现入口

### 源码同步与契约

```bash
cd /home/liaozhengyan/Desktop/lelab-bimanual
ROOT=$PWD/releases/sanitized-20261024-01
export PYTHONPATH=$ROOT/app:$ROOT/lerobot/src
.venv/bin/python -m lelab.jetson_policy \
  --ssh-key ~/.ssh/lelab-jetson --local-source $ROOT/lerobot/src sync
.venv/bin/python -m lelab.jetson_policy \
  --ssh-key ~/.ssh/lelab-jetson \
  --contract-dir ~/.local/share/lelab-jetson-20261031/checkpoints contract
```

### PolicyServer 容器

镜像由 `app/deploy/jetson/Dockerfile` 定义（基于已有 NGC 镜像，只补最小依赖；不替换 Jetson torch）。首次构建：

```bash
ROOT=$PWD/releases/sanitized-20261024-01
scp -i ~/.ssh/lelab-jetson \
  $ROOT/app/deploy/jetson/Dockerfile \
  nvidia@192.168.31.204:~/jetson-policy-server-20261031/Dockerfile
ssh -i ~/.ssh/lelab-jetson nvidia@192.168.31.204 \
  'cd ~/jetson-policy-server-20261031 && docker build -t lelab-policy-server:20261031 .'
```

启动/停止（只操作固定名称的容器）：

```bash
ROOT=$PWD/releases/sanitized-20261024-01
$ROOT/app/deploy/jetson/policy_server_docker.sh start
$ROOT/app/deploy/jetson/policy_server_docker.sh status
$ROOT/app/deploy/jetson/policy_server_docker.sh stop
```

- 只启动/停止固定名称 `lelab-policy-server-20261031`；不会停止其他容器。
- 日志：`docker logs lelab-policy-server-20261031`；会话 JSON：Jetson `~/jetson-policy-server-20261031/logs/container.json`。
- 服务绑定 `0.0.0.0:28081`，仅用于可信局域网；没有同时启动 SSH 隧道。

### 观察 / 影子

```bash
# 仅观测：只读串口+相机，不建 gRPC、不发动作（需 dialout）
PYTHONPATH=$ROOT/app:$ROOT/lerobot/src .venv/bin/python \
  $ROOT/app/deploy/jetson/run_shadow.py --mode observe --source follower --duration 30

# 影子：上传观测、接收动作、绝不执行
PYTHONPATH=$ROOT/app:$ROOT/lerobot/src .venv/bin/python \
  $ROOT/app/deploy/jetson/run_shadow.py --mode shadow --source follower --duration 300
```

`run_shadow.py` 不暴露 execute 模式；execute 只能通过受门禁保护的 API 构造。

### LeLab 页面（真实单臂只读观测）

隔离 venv 需要 `websockets`（uvicorn 的 WebSocket 实现）：

```bash
uv pip install --python .venv/bin/python websockets
sg dialout -c "cd $PWD && LELAB_PORT=18186 \
  LELAB_BIMANUAL_HOME=$HOME/.local/share/lelab-jetson-20261031/approot \
  bash releases/sanitized-20261024-01/app/deploy/local/run-bimanual.sh"
# 打开 http://127.0.0.1:18186/ ，点击顶部“只读观测”，再点“开始观测”
```

页面使用默认串口/标定/契约路径，显示真实关节、`/ws/joint-data`、两路相机、帧年龄和 `ACTIONS_SENT=0`；只有“开始/停止观测”，没有动作按钮。实测证据见 `ui-observe.md`。

观察/影子 CLI 需要在 `dialout` 组内运行：

```bash
sg dialout -c "cd $PWD && PYTHONPATH=$ROOT/app:$ROOT/lerobot/src \
  .venv/bin/python $ROOT/app/deploy/jetson/run_shadow.py \
  --mode observe --source follower --duration 30 --cameras right=0,up=2"
```



## 15. 剩余限制与下一步

1. **双臂/其余三臂**：只核实了 `5B91045575` 的 `my_robot` follower。其余三台需逐台确认 follower/leader、左右和标定；不要沿用 `my_robot` 到其他臂。
2. **真实状态变化**：本次观测/影子期间机械臂静止；已证明真实读数、WebSocket 与帧年龄，但没有运动中的状态跟踪证据。需要在安全支撑下人工轻移关节复测。
3. **真实相机视图**：确认 index 0/2 与 ACT 训练视图一致；当前只证明能取帧并送入模型。
4. **12 维双臂策略**：需要与当前 12 维契约匹配的 checkpoint；单臂 ACT 不能拼接。已审计的 12 维 ACT 候选权重完整（`checkpoint-12d-audit.json`），但数据集根目录、关节名、单位、动作语义缺失，兼容性**未验证**，未用于推理或执行。没有匹配策略则双臂只能做只读观测。
5. **Stage D**：真实观测影子已通过，但仍需 12 维策略、其余三臂标定、现场支撑、方向/单位/限幅/E-stop 检查和明确运动授权；当前没有执行。
6. **π**：只做条件评估；先确认 checkpoint 类型/动作契约/Jetson 内存，再决定是否加载。不要直接下载大模型。
7. **安全**：gRPC 仍是未加密 pickle，只在可信局域网；保持本地 TTL、限幅、租约和 E-stop。服务/页面已停止，端口已释放。

## 16. 证据索引

`docs/evidence/jetson-policy-server-20261031/`：

- 环境/契约：`env-local.txt`、`env-jetson.txt`、`contract.json`、`source-manifest.json`
- 硬件/模型审计：`four-arm-inventory.json`（四串口只读盘点）、`checkpoint-12d-audit.json`（12 维 ACT 未验证兼容）
- 用户确认角色与绑定：`hardware-inventory-20261031.json`、`hardware-roles-ui-check.md`、`hardware-role-bindings-20261031.md`
- 保护测试：`pytest-jetson-policy.txt`、`pytest-observe.txt`（单臂阶段 2 项，后续双臂扩展为 5 项）、`pytest-lease.txt`（18 项）、`pytest-lease-api.txt`（9 项）、`ruff-final.txt`
- 真实观测前端：`ui-observe.md`、`ui-observe-status.json`、`ui-observe-stop.json`、`ui-observe-stopped.json`、`ui-camera-right.jpg`、`ui-camera-up.jpg`
- 真实观测影子：`stageC-real.jsonl`、`stageC-real-summary.json`、`stageC-real.log`
- 5 分钟真实影子：`stageE-real-5min-summary.json`、`stageE-real-5min.jsonl`、`stageE-real-5min.log`
- 资源：`resources-real-5min-summary.json`、`resources-real-5min.jsonl`、`resources-real-5min.raw.txt`、`tegrastats-real-5min.log`
- 服务退出/重连：`stageFault-reconnect.log`、`stageFault-reconnect.jsonl`
- 服务端日志摘要：`server-log-summary.json`
- 合成协议测试：`stageC-synthetic-summary.json`、`stageC-synthetic.jsonl`、`stageC-synthetic.log`
- 早期串口阻塞：`stageB-observe-blocked.txt`
- 容器：`server.json`、`server-stop.txt`、`server-stop-final.txt`
- 相机静态帧：`camera-0.jpg`、`camera-2.jpg`、`camera-capture.json`
- 前端构建：`frontend-build.txt`、`frontend-build-observe.txt`

原始服务日志在 Jetson 容器内：`docker logs lelab-policy-server-20261031`；资源采样在 Jetson `~/jetson-policy-server-20261031/logs/`。
