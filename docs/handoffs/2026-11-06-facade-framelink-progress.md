# 上位机 FrameLink 接入：阶段进展

最新收尾、410 帧网页录制结果、仍未定位的租约失效、后续离线计划及完整 Goal Prompt 见 [本轮交接](2026-11-05-facade-offline-wrapup-plan-and-agent-goal.md)。以下保留各阶段历史结果，不能把早期未完成项或临时固定等待作为最终结论。

本次在独立 Humble 模拟实例验证。此文件记录实际结果，不代表真实小车运动验收。

## 本次变更

- 新增 `hybrid_stream_hardware.py`：显式选择 `frame_stream_v1`，复用双租约、原标定、状态机和反馈门禁；后台缓存车端状态，跟随线程按新的主臂样本发送五轴和夹爪整帧。设备模拟仅由显式调用方启用并核对车端身份。
- `hybrid_hardware.py` 保持 `legacy_http` 默认，仅通过 `LELAB_HYBRID_TRANSPORT=frame_stream_v1` 选择新路径；未知传输拒绝。新车端在 epoch 创建前未绑定驱动时，只允许有六路新鲜、来源和 ID 正确的读回进入现有 acquire 流程；epoch 创建后仍需通过原绑定和可信反馈门禁。
- `teleop_stream.py` 使缓存的发包窗口随本地单调时钟衰减，abort 清理待发队列并关闭连接；新增身份错误释放、主臂读数停滞不重放等测试。
- 隔离实例支持 `LROS_WORK_DIR`；`facade_smoke.py` 限定只连接 `simulated=true` 的本地离线 agent，使用冻结的模拟标定资产。打包白名单补充当前 agent 直接依赖、ROS 资源索引和上位机新增源文件。

## 本次验证

- 上位机 Python 3.12 定向测试：`app_ros_mobile/.venv/bin/python -m pytest app_ros_mobile/tests/test_hybrid_stream_hardware.py app_ros_mobile/tests/test_hybrid_hardware.py tools/ros2_collection/tests/test_hybrid_frame_link.py app_ros_mobile/tests/test_t4_hybrid_recording.py -q`，**44 passed**。录制用例有自身夹具，不能当作新 facade 的端到端录制证据。
- 独立 Docker：ROS Humble、Python 3.10 车端生产 agent，域 94，模拟板和相机；上位机使用生产 facade/FrameLink、模拟主臂。预览板输出 8→8；跟随提交 8 帧、准入 7 帧、拒绝 0；模拟板输出 8→14；暂停后 16→16；释放后 agent 无控制租约且帧输出关闭。报告：`work/ros2_collection/facade-integration/local_humble/facade-smoke/report.json`。过程中的失败报告与日志保留在同目录 `report-initial-failed.json` 和 `prior-attempt-2/`；原因分别是 epoch 前的驱动绑定检查、以及模拟资产重复保存后哈希改变，均修正后复测。
- `git diff --check` 与本轮 Python 文件 compileall 通过；项目虚拟环境未安装 ruff，未运行 Ruff。
- `bin/package` 生成 28 文件离线候选包及各文件 SHA256：`work/ros2_collection/facade-integration/package/frame-facade-offline-candidate.manifest.json`。仅补齐已核查的 Python 相对导入及 ROS 资源，未验证 ARM 构建、部署、启动或回退，不能称为可上车发布包。
- 收尾前只读查询：本地 18110 指向的真实 agent 显示 `hardware_hybrid_manual_v1`、`simulated=false`、`control.active=false`；旧 agent 没有新帧许可状态字段。18130 原离线实例无活动租约、帧输出关闭。仅停止了标签核验通过的本轮隔离容器 `lelab-humble-facade-integration`；旧离线容器和真实小车进程未停、未部署、未发运动指令。

## 未闭合，禁止当作完成

1. F3 网页 → 同一 facade → 两集真实 sqlite3 → 同步 → 受限 6D 导出 → SDK/页面读回未完成。当前 `get_hybrid_recording()` 对模拟 facade 选择内存录制客户端，`HybridRecordingController.start_session()` 请求 9D schema，与离线 6D 生产 agent 配置不一致；需要显式、安全的离线生产 agent 录制路径，保持 `simulated_offline` 和不可训练。
2. 尚未跑真实的三链路公平基准、负载与半开故障矩阵、相位和频率优化。原 `bench_links.py` 的 frame+ping 属 shape 模拟，不能报告为真实 legacy HTTP 测量。
3. 未运行完整 #9 accept/record/faults、本轮全量测试、真实浏览器交互或 ARM64 构建；历史 torque guard 和上位机测试失败须分别复核，不能当作已解决。
4. 真实小车当前部署的是旧 agent；新 FrameLink 无硬件输出验证。机械保持、急停、断连及独占控制仍需依实际设备和本次现场条件逐项验证；停止发包不是物理停止的证明。

## 独立实例复跑

先确保本机 Docker 和原离线镜像可用，环境变量按以下设置；只能操作本实例。`--prepare` 若无资产会创建模拟标定，已有资产会复用并校验，不能在启动后重生成 bundle。

```bash
export LROS_WORK_DIR=/home/liaozhengyan/Desktop/lelab/work/ros2_collection/facade-integration/local_humble
export LROS_CONTAINER=lelab-humble-facade-integration
export LROS_DOCKER_DOMAIN_ID=94 LROS_DOCKER_STREAM_PORT=18123 LROS_DOCKER_AGENT_PORT=18131
export LROS_DOCKER_BUNDLE="$(app_ros_mobile/.venv/bin/python tools/ros2_collection/local_ros2/offline/facade_smoke.py --prepare)"
sg docker -c 'tools/ros2_collection/local_ros2/bin/docker up'
app_ros_mobile/.venv/bin/python tools/ros2_collection/local_ros2/offline/facade_smoke.py
sg docker -c 'tools/ros2_collection/local_ros2/bin/docker down'
```

下一步先完成 F3 的模拟生产录制身份/schema 接线与同会话两集端到端验证，再做 F4/F6 和上车门禁。不要把本次候选包直接覆盖容器的原厂源码或当前运行目录。

## 后续增量：两集离线生产录制和 6D 导出

- 生产 `HybridRecordingController` 支持调用方显式指定仅用于 `simulated=true` 且 profile 为 `hardware_hybrid_manual_v1_offline6d` 的 6D 录制；默认 9D 行为保持不变。状态额外公开 agent 返回的 `car_recording_id`，与控制会话 ID 分开。
- 新增 `offline/facade_record.py`，只接受明确模拟的本地 agent，使用生产 facade、生产录制控制器及 agent/rosbag2。两集各提交 29 帧，集间均显式重新对齐接合；结束后控制租约释放。真实 sqlite3 通过生产同步验证 7 个文件，受限 `simulated_offline_6d_v1` 导出 2 集共 58 帧；SDK 读回 58 帧且 state/action 均 6 维，身份仍为模拟、不可训练。证据为 `facade-smoke/record-report.json`、`sync-export.json` 与 `sdk-readback.log`。
- 修复暂停后 agent 撤销通道许可导致第二次接合拒绝的问题：下一次显式接合先以相同身份重新绑定 epoch，清掉旧队列，等待完整新反馈；网关仍对新的许可进行判断。必要的离线测试 `test_hybrid_stream_hardware.py` **9 passed**，另有录制回归 **28 passed**（后者早于最后新增的身份测试）。
- 首次连续跟随时切换阶段，生产导出因旧帧携带旧阶段而越过 manifest 的新边界，正确拒绝；报告保留在 `sync-export-attempt1.json`。成功运行在每次阶段切换前主动暂停、随后等待上下文并重新对齐接合。该等待是离线流程的暂行操作，不证明持续运动时阶段切换可靠；正式网页流程需增加有确认的阶段屏障，不能依靠固定等待后宣称问题已修复。
- 这次未通过生产网页录制入口运行，模拟网页默认仍用内存客户端；未做网页点击、浏览器数据读回、三链路基准或真车输出。上面较早生成的部署候选包在本节源码变更前生成，哈希已过时，重新打包和 ARM/实机验证前不得部署。
