# 2026-11-06 LanderPi 真机预检与无输出预览

本次在 `pi@<private-ip>` 的现有 MentorPi 容器进行。控制板最小栈按已有授权启动，板卡初始化包含写入、加载 offset 和零底盘速度，不能称为纯只读；未启动整车 bringup。未执行机械臂或夹爪运动、扭矩调整、标定写入或 episode 录制。用户确认了现场固定、空旷和急停可用；物理停车/保持、断连保护效果及软限位是否与真实机械边界一致仍未验证。

- Pi aarch64，Humble/Python 3.10.12；六个真实脉冲扫描 ID 为 1、2、3、4、5、10，读取和观察详情见 `arm-stack-apply.log`、`ros-observation-retry.log`。ROS `/controller_manager/joint_states` 是命令缓存，不是实测。
- 主机读取 USB 身份 `5B90149723` 对应既有左主臂标定；三个采样中五轴及夹爪可用，读取后释放串口。其他三台 USB 串口未操作。
- 构建的 `landerpi-frame-20261106-preview-gated` 包 SHA256 `b83bc7cc4176f13f06162166075c65c9b94194b62d6463330cbac7d17d4e5cc9`，在 shared 候选目录校验 36 文件。Pi 上启动的车端 agent 使用该包，另在 state 目录使用 `hardware_hybrid_manual_v1` 的无输出配置副本，唯一配置差异为 `output_transport=frame_stream`；既有部署路径和 sessions 未覆盖。此前另一个单运行时候选 36 文件及 16 个模块已在 ARM64/Python 3.10 中验证哈希与导入；不等于新控制进程的物理运行验收。
- agent 的 `/hybrid/enable_output` 对当前配置返回 HTTP 409 `hybrid_no_output`。没有启动输出控制进程；新 agent 不创建自身舵机输出 publisher。
- `frame-preview.json`：两端获取/释放租约、5 次未对齐的预览；该阶段映射显示 `preview_not_advanced`，不作为映射预览通过。
- `aligned-frame-preview.json`：对齐后 73 次映射可用，26 秒内记录的主臂第 1 轴变化为零，不能作为动态方向证明；同时 ROS 话题观察时窗内舵机目标消息为 0。之后网页观察到主臂读数和预览目标变化、其他关节有触限显示，但没有留存同步的动态方向量化证据。
- 真实浏览器实际点击设备探测、获取预览会话、释放会话；相机、实时读数和六路反馈可见。网页新增显示每轴 `pre_clip_rad` 和是否限幅，生产 Vite 构建与 TypeScript 检查 exit 0；先前 dist/index 备份为 `frontend-index-before.html`，旧 assets 保留。网页会话最终释放。
- 修复：车端控制进程重复构造；无输出 profile 帧模式使能门禁；帧观察预览使用独立映射器，不推进跟随映射器；接合前刷新并重建相对参考、验证首目标连续且在限位内；状态脚本不再把控制器命令缓存标为实测。Humble 车端定向 43 passed；上位机定向 25 passed。模拟 acceptance 日志的 nonfinite 项显示因 stale_window 拒绝，不能据此宣称真实检查了 nonfinite 原因。

运行状态：真实最小栈及相机仍在运行；预览 agent 容器 PID 2536，状态目录 `/home/ubuntu/shared/lelab_car/state/preview-gated-20261106`；上位机本任务 web 服务 `127.0.0.1:18287`。本任务隔离的离线 Docker 实例已停止，未触及其他实例。停止真实进程前需按 PID、cmdline 和启动时间重新核验，先停止此 agent 再按 `arm-stack-apply.log` 对应的 STATE_DIR 使用脚本 `--stop`；不得泛用 pkill。

安全边界：保留现有软限位，尤其当前 joint5 约 1.705 rad，已有上限 1.717404 rad；该余量仅为配置数值，不证明实际机械安全。新 profile 明确禁止输出。下一阶段须验证每轴单位、转换和运动范围、独占控制、物理急停及保持/失联表现，配置专用低速输出和必要的车端保护，才能按授权进行逐轴小幅运动。没有进行真实 FrameLink 输出、真实录制/导出或故障注入。

## 后续网页预览与相机恢复

- 启动前发现 RGB 预览已停更约 17 分钟，但相机信息消息仍新鲜；独立 ROS 订阅 6 秒收到 35 帧 RGB、90 条 camera_info，因此不能把故障简单归因于相机不发布。原因未确定。
- 确认车端控制租约与输出均关闭后，仅按 PID、cmdline 与 profile 路径核验并停止本任务的预览 agent，使用相同候选和无输出配置重启；新 PID 10984。RGB 状态恢复新鲜，但尚无长时稳定性验证。未重启控制板、舵机控制器或相机节点。
- 发现重启后新的 session 尚未绑定标定，尽管六路原始回读新鲜，网页要求预先 trusted 导致无法发起获取。网页现只在六个名称、ID、回读来源、数值和年龄符合条件且错误含 `calibration_not_configured` 时允许尝试获取，并显示“六路待绑定”；车端/上位机租约和可信反馈门禁未放松。TypeScript 和 Vite 构建退出码均为 0，真实浏览器验证了重启后成功获取会话、绑定反馈并进入对齐预览。
- `live-mapping-retry.json` 为 52 个状态采样：采样间 SO101 1 轴变化 0.64136 rad，映射前值同步变化 0.64136 rad，方向 +1，六路反馈和相机参与的是当前硬件路径；与这一阶段重叠的 25 秒板卡舵机目标话题监听为 0 条，所有采样 `outputs_enabled` 均为 false。这只证明无输出映射，不证明机械轴方向或跟随。
- 网页已操作释放会话；保留服务供后续检查。用户表示暂时无法继续移动主臂，真实运动、硬件停止与掉电保持均未测试。
