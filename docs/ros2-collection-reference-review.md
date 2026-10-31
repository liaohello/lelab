# ROS 小车采集：参考项目核查与首批实现

日期：2026-10-31。范围：3 个只读子智能体分别核查本地传感器/专家数据、ROS→LeRobot 数据链路、示教/网页预览项目；主智能体整合并实现本仓库的预检工具。

## 用户最新选择

- 麦轮底盘由键盘控制；机械臂采用主臂示教，先保留硬件适配接口。
- 用户确认本地暂无专家轨迹。后续待 Gazebo 模型与控制链路就绪再生成 scripted expert 数据用于联调，不把它标为真人示教；不以获取已有数据阻塞当前开发。
- 任务为靠近方块、抓取并放入容器；达到任务成功需检查物体运动和接触结果。
- 必需视角是机械臂腕部 RGB；参考已有 link4 挂载，采集可用的深度与激光原始信息。
- Gazebo/RViz/ROS 仍是本项目后续集成目标；mobileCar 的 MuJoCo 仅作源码与数据参考。

## 推荐组合

| 项目 | 适合借鉴 | 不能直接套用的部分 |
| --- | --- | --- |
| [LeROS2](https://github.com/ngres/leros2) | ROS topic/action 到 LeRobot 特征映射；bag 原件与离线转换；腕部/底盘相机和事件配置 | 没有本项目网页、麦轮动力学或统一安全控制；latest-value 缓存不等同严格同步 |
| [LeRobot LeKiwi](https://github.com/huggingface/lerobot/tree/main/src/lerobot/robots/lekiwi) | 键盘底盘＋主臂；移动操作 action/schema；host/client 分离与底盘 watchdog | 三轮/ZMQ 与本项目麦轮/ROS 不同，角速度单位也需转换 |
| [web_video_server ROS2](https://github.com/RobotWebTools/web_video_server/tree/ros2) | ROS Image → HTTP 快照/MJPEG，供 LeLab 页面展示 | 不负责控制、episode、原始录制或数据集导出；部署需限制可浏览图像范围 |
| [sacovo/lerobot_ros](https://github.com/sacovo/lerobot_ros) | episode 开始/结束/丢弃/保存服务，任务与片段标注 | 依赖固定 LeRobot 0.5.1，与本地 0.6.2 需适配；部分源码路径未成功读取 |
| [LG ROS2 collector](https://github.com/lge-ros2/ros2_robot_data_collector) | 有界缓存、时间窗、独立写入线程、原始时间旁路保存 | 单图像、action 三选一，不能直接采底盘＋主臂＋夹爪复合动作；许可元数据不一致 |
| [ALOHA](https://github.com/tonyzhaozh/aloha) | 主从示教、腕部相机、多相机 episode 组织 | ROS1/双臂/HDF5 方案，不是 Jazzy/Gazebo 移动机器人采集成品 |
| [Foxglove Bridge](https://github.com/foxglove/foxglove-sdk/tree/main/ros/src/foxglove_bridge) | 图像、scan、TF 和状态调试 | 调试工具，不能替代定制采集 UI 或其控制权管理 |

推荐借鉴 LeROS2 的原始 bag→离线转换架构、LeKiwi 的键盘＋主臂分工、web_video_server 的只读图像通路，保留 LeLab 的数据管理和安全控制。上述是功能取舍，不表示已安装或完成这些项目的本机兼容验证。

### 源码与版本证据

LeROS2 的 [`pyproject.toml`](https://github.com/ngres/leros2/blob/c8ca08b550a34db1a4e87edaad468f4ad829524a/pyproject.toml) 声明 Python ≥3.12、LeRobot ≥0.6.0,<0.7.0，并有 Jazzy 环境配置；与本地版本声明吻合。其 [`src/leros2/scripts/convert.py`](https://github.com/ngres/leros2/blob/c8ca08b550a34db1a4e87edaad468f4ad829524a/src/leros2/scripts/convert.py) 提供 task/event/clock topic、`DatasetConverter` 和 `convert`，通过本地 SDK 创建/写帧/finalize。适合参考接口，不应直接沿用其缓存策略来证明传感器时间已对齐。

LeKiwi 的 [`lekiwi_host.py`](https://github.com/huggingface/lerobot/blob/main/src/lerobot/robots/lekiwi/lekiwi_host.py)、[`lekiwi_client.py`](https://github.com/huggingface/lerobot/blob/main/src/lerobot/robots/lekiwi/lekiwi_client.py) 和[官方示教说明](https://github.com/huggingface/lerobot/blob/main/docs/source/lekiwi.mdx)展示了键盘与主臂输入及 host watchdog。实施时优先使用仓库中已配套的本地 0.6.2 源码，保留本地定制，不切换到在线 main。

sacovo 的 [`pyproject.toml`](https://github.com/sacovo/lerobot_ros/blob/ab3c971f2f93352a5cb9487f8c7df54c730a2820/pyproject.toml) 固定 LeRobot 0.5.1；其 [README](https://github.com/sacovo/lerobot_ros/blob/ab3c971f2f93352a5cb9487f8c7df54c730a2820/README.md)列出 `/new_dataset`、`/start_episode`、`/end_episode`、`/store_episodes` 服务流程，服务说明属于文档证据，未把读取失败的 recorder 源路径当作已核验。

LG 的 [`collector_node.cpp`](https://github.com/lge-ros2/ros2_robot_data_collector/blob/d12c2b54a90323abd8b0e403448d7c5de0dd67bb/src/collector_node.cpp)、[`sync_policy.hpp`](https://github.com/lge-ros2/ros2_robot_data_collector/blob/d12c2b54a90323abd8b0e403448d7c5de0dd67bb/include/ros2_robot_data_collector/sync_policy.hpp) 和 [`hdf5_to_lerobot.py`](https://github.com/lge-ros2/ros2_robot_data_collector/blob/d12c2b54a90323abd8b0e403448d7c5de0dd67bb/scripts/hdf5_to_lerobot.py)可参考时间窗口与写入隔离，但它用动作接收时刻配对，不能直接视为原始动作采样时刻。README 对单图像、动作类型选择和缺失模态有限制说明。

GitHub API 本次实际核查的分支头如下；提交日期仅是所核查提交的时间，不代表质量或兼容性保证：

| 仓库 | 核查提交 | 提交 UTC 时间 | 许可 |
| --- | --- | --- | --- |
| ngres/leros2 | `c8ca08b550a34db1a4e87edaad468f4ad829524a` | 2026-08-14T06:37:21Z | Apache-2.0 |
| sacovo/lerobot_ros | `ab3c971f2f93352a5cb9487f8c7df54c730a2820` | 2026-08-14T08:14:01Z | MIT |
| lge-ros2/ros2_robot_data_collector | `d12c2b54a90323abd8b0e403448d7c5de0dd67bb` | 2026-05-22T05:33:02Z | LICENSE 为 Apache-2.0；package.xml 写 MIT，复用前核对 |
| RobotWebTools/web_video_server，ros2 分支 | `5c60fad378bb439e9ca283de5a4e58f32e5384ef` | 2026-09-09T20:40:39Z | BSD-3-Clause |

ALOHA 根许可为 MIT，而包元数据另有 BSD 表述，需按具体文件和依赖核对；LeRobot 为 Apache-2.0，Foxglove bridge 为 MIT。未复制上述第三方实现源码到本项目，当前新增预检代码为独立实现。将来实际引入依赖时锁定版本并保留许可与来源。

## 本地相机与专家数据核查

`mobileCar` 的 Git HEAD 是 `a5cef52d932ba41424b4bcc134539914031fda39`；工作文件不一定都与该提交一致，本次以读到的文件为准。只读取参考源码，没有执行其测试、MuJoCo、渲染、导出或服务器任务，也没有采用其项目专属的运行/审批流程作为 LeLab 的规则。

相机沿 `link4 → camera_connect_link → depth_cam_link` 挂载，见 `assets/landerpi/source_description/urdf/arm.urdf.xacro:253,306`；不是 joint5/TCP 相机。MuJoCo RGB-D 获取与元信息在 `src/mobilecar/backends/mujoco/backend.py:4024`，原始 Episode 与资产/哈希组织在 `src/mobilecar/data/recorder.py`。普通 RGB-D profile 的标定存在占位值，不能当作已标定实机内参。

专家入口为 `src/mobilecar/expert/record.py`、`expert/adapter.py`、`expert/export_lerobot.py`。它与近场真人六维示教入口不同：

| 项目 | scripted-expert 数据 |
| --- | --- |
| 来源标记 | `scripted_oracle`，包含物体真值/接触等特权输入，不能标为 human |
| state | 16 维：4 轮角速度＋IMU 六轴＋5 关节位置＋夹爪宽度 |
| action | 9 维：机体 vx/vy/wz＋5 关节绝对 user 角度＋夹爪宽度 |
| 单位 | m/s、rad/s、user-space rad、夹爪 m |
| 相机 | link4 RGB 和已有 front RGB；导入时不能无说明丢弃额外相机 |
| 频率 | 参考 expert profile 为 5 Hz，导入保留源 fps，不伪装成新 10 Hz 实采 |
| 文件 | `meta.json`、`episode.npz`、RGB JPG、sidecar；depth 另存 |

参考专家 exporter 主要输出 RGB，不表示 metric depth 自动进入 LeRobot。其参数/导出 API 与本地 0.6.2 也要逐项核对。当前没有在参考根目录的 `runs/outputs/data` 路径发现原始专家数据；不能把源码存在当作真实 episode 已取得。

### Gazebo 当前声明与缺口

用本次新增工具对 `resource/uavociety/robot_gazebo/urdf/robot.gazebo.xacro` 实际运行静态核查，结果为：

| 模态 | 静态入口声明 | 含义 |
| --- | --- | --- |
| 腕部 RGB | 有，`camera`，640×400/30 Hz，`/depth_cam/depth_cam` | 随 depth_cam_link/link4 运动；运行画面未验证 |
| 激光 | 有，`gpu_lidar`，10 Hz，`scan` | 传感器已 include，运行话题未验证 |
| 深度 | 没有 `depth_camera/rgbd_camera` 声明 | 需要真正新增/迁移 RGB-D 传感器，不能凭 depth 字样判断 |
| IMU | 有单独 `imu.gazebo.xacro`，但当前入口没有 include 它 | 只有插件标识或 bridge 配置不能产生 IMU 样本 |

RGB-D 迁移可参考[官方 Jazzy RGB-D bridge 示例](https://github.com/gazebosim/ros_gz/blob/jazzy/ros_gz_sim_demos/launch/rgbd_camera_bridge.launch.py)和[bridge 配置文档](https://github.com/gazebosim/ros_gz/blob/jazzy/ros_gz_bridge/README.md)：分别桥接 image、depth_image、camera_info；光学坐标系、实际编码与源时间戳要运行核查。网页深度可视化与训练/原始米制深度应分开存储。

## 已落地的首批实现

- [`inspect_sensors.py`](../tools/ros2_collection/inspect_sensors.py)：递归读取可解析的 Xacro include，报告传感器类型、挂载、topic、规格和未求值条件；始终标记为静态证据。
- [`expert_preflight.py`](../tools/ros2_collection/expert_preflight.py)：只读核对 mobileCar 专家目录，保持 16/9 维结构和单位，检查有限数值、源时序、图像解码、帧名/数量及哈希；不使用 pickle，不执行控制。
- [`landerpi-expert-v1.json`](../configs/ros2_collection/landerpi-expert-v1.json)：记录键盘底盘、专家/未来主臂、link4 RGB 与可选 depth/laser 的配置契约；硬件驱动和标定仍为未配置，配置文件不代表服务已实现。
- [`预检测试`](../tools/ros2_collection/tests/test_preflight.py)：15 项合成 fixture 测试在本机通过，包括错序关节、夹爪单位错误、NaN、时间 reset、非因果顺序、缺帧/坏图、object 数组和路径逃逸。

运行方式见[工具说明](../tools/ros2_collection/README.md)。这些测试只验证新增工具，未运行参考项目，也未实际检查一个真实 expert episode、导出 LeRobot、启动 ROS/Gazebo 或证明物理任务成功。预检报告显式保留这些边界。

## 下一步的实现边界

1. 准备并执行本机 Jazzy/Harmonic 环境安装，处理已知的本机管理员交互限制。
2. 迁移四麦轮与五轴/夹爪执行模型，建立 link4 腕部 RGB-D、laser 和统一 `/clock`。
3. 接入只读图像预览，并用带来源/时间戳的状态返回 LeLab。
4. 主臂输入保留接口；用户确认暂无专家轨迹，待模型与控制链路就绪再实现/运行 scripted expert，记录脚本版本、种子及使用的特权信息。键盘仍负责人工驾驶，完整 scripted expert 可以独立测试，但不可与键盘同时争抢底盘。
5. 通过统一控制权、限幅和 watchdog 后再运行轨迹；相同数值维度不表示模型关节零位或夹爪单位相同，完成标定映射才可回放。
6. 实现 bag 原始录制→校验同步→离线对齐→本地 LeRobot 0.6.2/v3 导出，并验证抓取物理和数据完整性。

当前没有新建用户聊天、安装外部仓库、下载大数据集、运行训练或连接真实硬件。
