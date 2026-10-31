# ROS 小车采集接入预检

这是 LeLab 新增的独立预检工具，不修改当前发布快照，也不导入或运行参考项目 mobileCar。当前实现只做源码传感器核查和 scripted-expert 数据入场检查，不发布控制指令。

## Gazebo 传感器源码核查

```bash
python3 tools/ros2_collection/inspect_sensors.py \
  --entry /path/to/uavociety/robot_gazebo/urdf/robot.gazebo.xacro \
  --package robot_gazebo=/path/to/uavociety/robot_gazebo \
  --package landerpi_description=/path/to/uavociety/landerpi_description
```

只读取 XML 和可解析的 include，不执行 Xacro 或 ROS。结果是声明清单，条件和宏不求值，不能作为 topic 已运行、插件可加载或硬件存在的证据。单独存在但未 include 的传感器文件不会计入入口清单。

## mobileCar scripted-expert 数据预检

```bash
python3 tools/ros2_collection/expert_preflight.py /path/to/attempt_000
```

需要 Pillow 解码源 JPEG；不要求安装 ROS、NumPy、LeRobot 或 mobileCar。工具只读取给定 episode，不修改原件、不训练、不执行仿真、不导出数据集。

接收 `meta.json`、`episode.npz` 与 `images/observation.images.link4_rgb/*.jpg`，并检查元数据声明的其他 RGB。核验原生 16 维 state、9 维 action 的名称/单位、有限数值、时序、帧数、实际图像解码和源哈希；NPZ 仅接受 float32/64 数值数组，不使用 pickle。所接收格式来自参考项目 `src/mobilecar/expert/record.py` 的公开数据结构，代码为本项目独立实现。

此工具保留夹爪宽度 m 和 user-space 关节 rad，拒绝自动改成 Gazebo 关节角或实机脉宽。复用轨迹前须显式提供关节零位/方向与夹爪标定映射。`preflight_passed` 只表示此处数据结构、图像和因果顺序检查通过；不会判定物理抓取成功、训练资格、真人示教或 ROS/Gazebo 联调通过。depth/laser 原始数据尚不在此预检范围。

测试全部使用本仓库生成的合成 fixture：

```bash
PYTHONPATH=tools/ros2_collection python3 -m unittest discover \
  -s tools/ros2_collection/tests -v
```

主臂硬件接入、Gazebo RGB-D 传感器迁移、ROS 网页预览、原始 bag recorder、同步及 LeRobot exporter 的实际实现仍按实施计划继续；本工具不是上述服务的替代。
