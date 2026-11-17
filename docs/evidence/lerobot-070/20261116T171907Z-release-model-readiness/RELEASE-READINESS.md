# 发布准入结论（RELEASE-READINESS）

Run：`20261116T171907Z-release-model-readiness`。分支 `codex/lerobot-070-integration`。
本轮起点 `0bcc598`（工作区干净），本轮修复提交：`cc935fe`（R0）、`5daa788`（R1）、
`d11b8e6`（R0–R2 证据）。解释器/导入路径、逐条命令与退出码见
`case-ledger.json`、`FAILURE-TRIAGE.md`、`UI-ACCEPTANCE.md`、`fastwam-verification.json`。
所有状态按实际执行记录；partial/blocked 不算 passed。

## 1. 各范围准入结论

| 范围 | 结论 | 依据 |
| --- | --- | --- |
| **app-only 候选** | **可审查（推荐进入发布评审）** | app 全量 591 passed/1 skip（严格收集，本机复验）；前端 tsc/构建/来源链路完整；生产表单 7 场景线级验收通过（含 FastWAM 维度门禁拒绝、远程身份门禁拒绝）；Jetson 旧入口拒绝矩阵已记录。真实模型状态与限制见下 |
| **ROS 候选** | **可审查，附 2 项环境条件** | 急停 409 根因定位并修复（5daa788，非放宽预期）；9 项环境失败在 g128 兼容环境 **57 passed** 全绿（`g128-rerun.log`）；本机全量仍有线程/内存压力类失败（环境，已分类）。条件：①发布包需包含 `app_ros_mobile` + `ros2_ws/src/lelab_mobile_agent`（现有候选不含）；②上线环境需 ≥8k 线程余量或更轻的并发 |
| **FastWAM 真实推理** | **通过（最小验证）** | `lerobot/fastwam_base` + Wan2.2 组件真实权重（44 GB，safetensors 校验、无 LFS 指针）→ V100S bf16 真实加载（6.02B 参数全部 bfloat16）→ 离线动作 finite/[1,7]（native 7/8 维）→ 保存/重载成功。`fastwam-verification.json` |
| **FastWAM 短程反传** | **未验证（资源）** | 训练激活超出 32 GB（`use_gradient_checkpointing=True` 仍 OOM）；需 ≥40 GB 卡或缩分辨率/批。非代码缺陷 |
| **FineART 真实推理** | **blocked** | 磁盘预算（97% 满盘）+ 镜像限速；离线契约 16 passed 不等于真实权重验证。`fineart-BLOCKED.md` 有完整复跑命令 |
| **训练/微调（两模型）** | 未验证 | FastWAM 见上；FineART 未开始 |
| **语言规划** | 未开始 | 只有任务字段透传与动作模式预检；无语言标注数据与权重验证 |
| **实机（TC-HW）** | blocked | 无设备观测/运动授权（既有约束，未扩大） |
| **生产激活** | 未执行 | 仅候选材料与本地演练；活动 `current` 未切换 |

## 2. 关键事实与修复

- **R0**：连续性测试原来允许暂停落在最终 step（恢复后 0 次更新的假通过）；
  实测暂停实际落在下一边界（3/4）。已改为 6 步 + 强制 `2<=paused_step<final` +
  必须观测到恢复后的真实 `train` 事件（`cc935fe`）。
- **R1**：ROS 急停 409 = 线程耗尽下 `estop()` 内 `thread.start()` 失败逃逸 +
  `_hybrid_guard` 把 RuntimeError 一律映射 409（已 latched 的急停被报成"冲突"，
  忙锁回退路径 latch 甚至丢失）。修复保持保护契约：latch 优先、spawn 失败内联
  兜底、`save_mode` 如实上报（`5daa788`）。缺历史 fixture 改为版本受控的生成式
  example fixture（拒绝契约 `unmeasured_example` 保持）。
- **R2**：本机无可用浏览器自动化（Chromium 受 4 GiB 地址空间限制不可运行，
  Firefox 为 snap 无法在沙箱启动）。等价覆盖：逐字节提取的 `configToRequest` +
  前端自身 `startTrainingJob`，经真实 `StartTrainingBody`/`InferenceRequest` 校验，
  7 场景全部符合预期（`UI-ACCEPTANCE.md`）。DOM 交互列为最小下一步。
- **R3**：真实模型验证在 g128（已授权、GPU 空闲）执行。环境修复链：torch
  cu132→cu126（驱动 535 兼容）、torchvision 配对、fastwam extras、bfloat16 为
  checkpoint 声明精度、`text_encoder_device='cpu'`（显式 config 传入）。
- **重要边界**：FastWAM 原生 7/8 维可运行**不**代表 SO-101 6/12 维可控制；
  6/12 维需显式维度配置（表单/后端门禁已就位）+ 动作头适配/微调 + 实机验收。

## 3. 候选包范围

| 项 | 现状 |
| --- | --- |
| 现有候选 `20261116T153120Z` | 仅 app+lerobot（1678 文件，解包/校验/篡改拒绝/恢复演练通过）；**不能**声称覆盖 ROS |
| 新候选 | payload 有变化（R1 修复 + R0 测试），建议生成新 ID；本轮未自动生成——见"剩余阻碍"③ |
| ROS 发布物 | 尚无：需另建含 `app_ros_mobile` + `lelab_mobile_agent` 的打包清单与对应回归 |

## 4. 剩余阻碍（精确）

1. FastWAM 短程反传：需 ≥40 GB 显存卡（如 A100 40G/4090 48G）或显式降分辨率/批
   （后者需另证不掩盖断言）。
2. FineART 下载：`hf-mirror.com` 对大分片限速/停滞（umt5-xxl 11 GB >1h 停在 65 KB）；
   需网络窗口或可达 huggingface.co 的通道；磁盘需先清出 ≥40 GB。
3. 新候选包：生成脚本已有（`app/deploy/package_lerobot070_candidate.py`），新 ID
   生成 + 演练约 10 分钟，等发布评审决定打包范围（是否纳入 ROS）后执行。
4. 浏览器 DOM 交互：需在无地址空间限制的机器装 Playwright+Chromium（约半日）。

## 5. 最小下一步

1. 发布评审确认打包范围 → 生成新候选 ID 并演练（脚本就绪）。
2. 在有 ≥40 GB 显存的已授权卡上跑 `fastwam_gpu_verify.py` 的 stage5（脚本就绪）。
3. 网络窗口期执行 `fineart-BLOCKED.md` 的复跑序列。
4. 无地址空间限制机器上补 Playwright DOM 断言（`ui_harness.py` 可直连真实后端）。
5. ROS 环境条件确认（线程余量/打包范围）后启动 ROS 发布物清单。

## 6. 回退

| 目标 | 操作 |
| --- | --- |
| 仅回退本轮 R0–R4 | 基线 `0bcc598`（本轮修复未合 main，未推送） |
| 回退到 0.7 升级前 | `git reset --hard pre-lerobot070-port`（会丢弃全部 0.7 迁移） |
| 活动入口 | 始终未切换；`current` 保持 `releases/sanitized-20261024-01` |
| g128 临时产物 | `~/lelab-rmr/`（源码、venv、44 GB hf-cache）可整目录删除回收 44+ GB |
