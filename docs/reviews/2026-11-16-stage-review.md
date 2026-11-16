# 2026-11-16 阶段开发与代码 review 入口

本次交付把此前未提交的业务源码、测试、配置模板和生产前端构建发布到独立分支，便于从其他机器检出审查。三个分支各自基于原有实现，尚未互相合并；不能把三个分支的验证理解为一次集成验收。main 不在本次更新范围内。

## 分支与代码位置

| 分支 | 本阶段范围 | 主要代码位置 | 本阶段差异 |
| --- | --- | --- | --- |
| [feat/local-bimanual](https://github.com/liaohello/lelab/tree/feat/local-bimanual) | 四臂准备、单臂遥操作测试、相机身份确认和只读观测 | releases/sanitized-20261024-01/app/；配套 ../lerobot | [相对 805357d](https://github.com/liaohello/lelab/compare/805357d...feat/local-bimanual) |
| [codex/ros2-mobile-collection](https://github.com/liaohello/lelab/tree/codex/ros2-mobile-collection) | LanderPi 分步采集、DDS 控制与反馈、安全门禁和 6D 导出 | app_ros_mobile/、ros2_ws/、tools/ros2_collection/、configs/ros2_collection/ | [相对 803652e](https://github.com/liaohello/lelab/compare/803652e...codex/ros2-mobile-collection) |
| [codex/training-monitor-review](https://github.com/liaohello/lelab/tree/codex/training-monitor-review) | 持久化训练指标、安全暂停恢复、验证集与优化器覆盖、SSH 只读监控 | releases/sanitized-20261024-01/{app,lerobot}/ | [相对 07216e0](https://github.com/liaohello/lelab/compare/07216e0...codex/training-monitor-review) |

检出后先阅读对应 pyproject.toml 和安装说明。应用要求 Python 3.12+，LeRobot 必须使用仓库内的同级可编辑源码；不以 PyPI 包替代。ROS 车端/接口包另用其声明的 ROS 2 环境。本次没有启动服务、连接硬件、部署到实机或切换 current。

## 双臂采集本阶段

- /hardware 增加四角色设备绑定、标定状态与逐台标定入口，标定完成后能进入所选侧的遥操作测试。
- /teleop-test 按已保存机器人记录解析端口和标定；记录原始示教目标、限幅后下发目标、从臂实测、跟随误差和限幅次数。操作者确认的测试结论绑定端口、标定内容 SHA-256 和生效限幅；身份变化后显示 stale。
- /cameras 使用稳定设备路径维护数据字段名、显示标签、备注与画面确认。采集准备页汇总设备、测试、相机和采集配置，并能从确认结果生成采集相机配置。单臂测试状态是提示，不是所有采集入口的硬门禁。
- /observe 支持只连接选定 follower 的只读观测；区分原始数值和 URDF 弧度，展示数据年龄、趋势、停更和相机状态。全局状态栏展示服务地址、租约与有效限幅。
- 新增模块、测试、前端 lib/ 文件和重新生成的 dist/ 均随代码提交。

历史实机记录（2026-11-02）显示 5 episodes、1912 frames、三路相机及 10 次停顿；这是历史流程证据，不是本次稳定性通过结论。本次未执行真实跟随、急停或长时采集验收。跨后端进程争用同一设备的协调仍未实现。

## LanderPi / ROS 采集本阶段

- 新增 CaptureOrchestrator，把预览、对齐、接合、录制与结束状态统一到后端编排，保留会话身份、操作幂等和 REC 身份判断。
- 前端增加采集工作区、标定、数据与诊断页面，补充相机新帧检测和状态流。
- 新增 DDS 控制帧、回执、健康与测量反馈接口，以及上位桥接、车端接收和反馈映射；认证、时效、会话代际和输出许可继续参与准入。
- 输出使能前核对设备绑定的六轴反馈范围；硬件 6D 导出校验反馈来源、时间、标定和设备身份，不把模拟数据改称实测。
- 保留实时探测/受控测试工具、厂商补丁及守卫；提交被通用 lib/ 规则漏掉的 local_ros2/lib/{common.sh,enter.sh,stage_vendor.py}，并纳入生产前端 dist/。
- live_stack 的机器目录可由 LELAB_CAR_ROOT、LELAB_ROS_WS、LELAB_THIRD_PARTY_WS 配置；LAN=1 时要求明确给出 PEER。远程测试命令采用 shell 参数引用，不内嵌私网地址。

完整真机流程、安全响应和 DDS 图通信仍需要对应实机/ROS 运行时验收。现有硬件 profile 保留其设备绑定和保护参数；不要把示例或历史 profile 当作其他机器的可用标定。

## 训练监控代码来源与范围

原始 releases/training-monitor-20261030-01 和 -02 是本地未跟踪快照。-02 较新，包含 -01 的指标/暂停恢复功能，并增加只读 attach。本次把 -02 相对基线的业务差异复制到独立 review 工作副本，以标准 app/lerobot 路径提交；原始快照及运行入口均保留。没有把两份完整重复快照、环境或模型上传。

- @@LELAB_METRIC@@ 结构化事件、追加式持久化指标、游标分页、attempt 区分及旧日志兼容。
- 优化器/调度器显式覆盖、独立验证集配置、SmolVLA 分组损失与权重范数诊断。
- 仅在优化更新边界暂停；校验 checkpoint 清单、原子发布、退出状态后才能报告 paused；恢复重新校验并保留训练状态。
- 本地/SSH 任务控制与 SSH 外部进程只读 attach；只读任务不提供训练控制。
- 详细的历史设计说明见 docs/reviews/training-monitor-20261030-02/。它们描述原开发阶段，不能替代下面的本次检查。

## 本次验证（2026-11-16）

| 范围 | 本次结果 | 边界 |
| --- | --- | --- |
| 双臂相关 9 个测试模块 | 99 passed | 单元、假硬件、合成图像及真实 LeRobot 写入器；不是实机运动 |
| 双臂改动 Python Ruff | 通过 | 10 个改动/新增 Python 文件；整理了三处测试导入 |
| 双臂前端 | TypeScript 与生产构建通过；改动文件 ESLint 无错误、1 条警告 | 没有重新执行浏览器交互 |
| ROS 应用 8 个测试模块 | 114 passed | 离线/fixture；不是实机 |
| ROS 工具测试子集 | 235 passed、9 skipped、3 failed | 3 个失败都在导入 agent 时缺少 rclpy；另两个依赖 ROS 的模块未能有效执行，不能宣称工具全量通过 |
| ROS 静态检查 | E9/F821/F822/F823 通过；完整 Ruff 未通过；ESLint 未通过 | 完整 Ruff 报告 242 项；前端 ESLint 108 errors、4 warnings，保留供 review，没有关闭规则 |
| ROS 前端 | TypeScript 与生产构建通过 | 构建通过不能替代 lint 或实机验收 |
| 训练监控应用测试 | 27 passed | 假 runner/本地临时 checkpoint 和指标文件 |
| LeRobot 监控配置测试 | 14 passed | CPU、配置及辅助函数；没有新训练或模型评测 |
| 训练监控前端 | TypeScript、改动文件 ESLint、生产构建通过 | 没有浏览器或远端 SSH 集成复验 |
| 训练监控 Ruff | 除 UP042 外通过 | configs/train.py 既有 CheckpointFormat 的 str/Enum 写法仍被新 Ruff 提示；不为发布快照改写枚举行为 |
| 打包入口 | shell/Python 语法检查、Git 差异空白检查 | 未运行会启动硬件或服务的脚本 |

训练应用和 LeRobot 的测试分别执行，避免两个 tests/conftest.py 的同名包冲突。前端构建使用现有 node_modules 和本地 Node 运行时；没有安装或升级依赖。Vite 提示浏览器数据库较旧及单 bundle 偏大，不影响本次构建退出码。

复核命令（在相应分支的仓库根目录，解释器/Node 由已有项目环境提供）：

~~~bash
# 双臂；app 与 lerobot 是仓库内符号链接
PYTHONPATH=app:lerobot/src .venv/bin/python -m pytest \
  app/tests/test_server.py app/tests/test_hardware_lease_api.py \
  app/tests/test_teleop_test.py app/tests/test_teleoperate.py app/tests/test_observe.py \
  app/tests/test_rig_bindings.py app/tests/test_camera_inventory.py \
  app/tests/test_bimanual_recording.py app/tests/test_smoke.py -q

# ROS 应用；进入 app_ros_mobile 后
PYTHONPATH=.:../lerobot/src .venv/bin/python -m pytest \
  tests/test_hybrid_capture.py tests/test_camera_live_preview.py \
  tests/test_emitter_enable_preflight.py tests/test_hybrid_dds_transport.py \
  tests/test_hybrid_hardware.py tests/test_hybrid_stream_hardware.py \
  tests/test_t4_hybrid_recording.py tests/test_ros_mobile_teleop_export.py -q

# 训练监控；两组分别执行
PYTHONPATH=app:lerobot/src .venv/bin/python -m pytest \
  app/tests/test_pause_resume_state.py app/tests/test_train_monitor_options.py \
  app/tests/test_training_metrics_store.py -q
PYTHONPATH=app:lerobot/src .venv/bin/python -m pytest \
  lerobot/tests/configs/test_training_monitor_overrides.py -q

# 每套前端目录均执行；完整脚本见 package.json
node node_modules/typescript/bin/tsc --noEmit
node node_modules/vite/bin/vite.js build
~~~

## 提交边界

包含业务源码、相关测试、配置模板、文档和匹配的生产前端构建。原始实机日志、截图/视频、串口/相机身份、真实校准、数据集、权重、凭据、环境、node_modules 和原始重复快照继续留本地。新增原始 docs/evidence/ 目录未直接发布；以上本次结果来自实际命令退出状态和日志摘要。旧草稿工作副本的核心双臂行为已由新版覆盖，没有用旧草稿覆盖最新源码。
