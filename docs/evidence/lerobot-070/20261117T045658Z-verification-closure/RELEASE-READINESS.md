# 发布准入结论（从 7b1667d 起的验证收口）

Run：`20261117T045658Z-verification-closure`。分支 `codex/lerobot-070-integration`。
起点 `7b1667d`，本轮提交：`fa23bc7`（验证器）、`64527ac`（ROS 急停）、`e9671f3`（打包 profile），
证据与文档另起一次提交。所有状态按实际执行记录：**partial / blocked 不算 passed**，
`current` 未切换、未部署、未推送。

## 1. 各维度结论（分开结算）

| 维度 | 结论 | 依据 / 边界 |
| --- | --- | --- |
| 框架迁移（0.7.0） | **passed**（沿用，不重跑） | 前几轮证据与提交；本轮未改迁移代码 |
| FastWAM 加载 | **passed** | `2_load`：6020.7M 参数、bf16、cuda，text encoder 在 CPU，143 s；`fastwam-verification-final.json` |
| FastWAM 有限动作 | **passed（仅管线）** | `3_chunk`：`[1,32,7]`、finite、abs_max 3.59。**注意**：该 checkpoint 不含动作头，动作来自新建的随机层，见 `STAGE5-MEMORY.md` §3.1 |
| FastWAM 保存/重载 | **passed** | `4_save_reload`：同输入/同显式噪声/两侧 reset，`max_abs_diff=0.0`、`bitwise_identical=true`，容差 0.0919；旧的 1.6016 是 select_action 队列位置错误，已修复 |
| FastWAM 短反传/更新 | **passed（冻结配置）/ blocked（原样配置）** | 冻结：1 次更新完成，峰值 **21.67 GB** / 34.08 GB；原样（6020.7M 全可训练）训练态下界 **72.3 GB**，34/40 GB 都不够，未做必败试跑 |
| FastWAM LoRA 微调 | **passed（1 次更新）** | r=16/alpha=32：可训练 **7.92M**、训练态下界 0.13 GB、峰值 **16.12 GB**，梯度与优化器状态有限；表单/后端/DOM 已接通（37 条断言含 3 条 LoRA） |
| FineART 资产与加载 | **passed** | 8.71 GiB 权重哈希一致；tokenizer 经 ModelScope 镜像（字节一致）进离线缓存；`FineARTVLAPolicy` 4143.4M 参数加载 153 s，处理器构建成功 |
| FineART 动作 | **passed（管线）** | 真实 tokenize 指令 → `[1,50,14]` finite，1.08 s；保存/重载 `max_abs_diff=0.0` 逐位相同 |
| 语言规划 | **未开始** | 只有任务字段透传；无语言标注数据与权重验证 |
| SO-101 6/12 维适配 | **未验证** | 表单/后端门禁到位（本轮 DOM 已验证拒绝路径）；无 6/12 维权重、无实机 |
| ROS 安全（急停降级） | **passed（契约层）** | 26 tests；闩锁先于锁与线程，响应有界，迟到保存不污染新会话；物理停止未验证 |
| 生产 bundle DOM | **passed** | 31/31 断言，真实 Chromium 对 28 文件产物；构建 28 文件逐字节可复现 |
| 候选包 | **app 可审查 / ROS 可审查（附条件）** | app 1681 文件、ROS 1762 文件，解包/校验/篡改拒绝/恢复全部通过；两包均不自动激活 |
| 实机 | **blocked** | 无设备观测/运动授权（既有约束，未扩大） |
| 生产激活 | **未执行** | 仅本地候选与演练 |

## 2. 本轮修正的四个旧结论

1. **stage4「1.6016 但不一致也 ok」是 harness 缺陷，不是模型问题。**
   `stage()` 把任何未抛异常的返回写成 `ok=true`；比较时 before 侧用了非空 `select_action`
   队列、after 侧是新对象。改为两侧 `reset()` + 显式 `noise` 比较完整 chunk 后，
   实测 `max_abs_diff=0.0`（逐位相同）。旧 JSON 保留在
   `../20261116T171907Z-release-model-readiness/fastwam-verification.json`。
2. **「32 GB OOM ⇒ 需要 ≥40 GB」不成立。**
   OOM 发生在 **未冻结** 配置（6020.7M 全可训练，训练态 72.3 GB）；SO-101 微调用的
   **冻结** 配置在 34.08 GB 卡上完成 1 次更新，峰值 21.67 GB。40 GB 是否够仍取决于
   激活规模，没有被本轮证明，也不再被当作结论。
3. **「FineART 被镜像限速卡死」不成立。**
   单流 1.16–2.46 MiB/s 是真，但 16 路分块后 **32.5 MiB/s**，8.71 GiB 用 4 分 37 秒下完，
   且 SHA-256 与 Hub 一致。真实阻碍是 **PaliGemma tokenizer 仓库门禁（403）**。
4. **「浏览器 DOM 需要另找机器/半日」已过期。**
   g128 上 Playwright + Chromium 可用，本轮完成 31 条真实交互断言；本机仍受 4 GiB
   地址空间限制（记录在 `build-inputs.json`）。

## 3. 明确不能声称的事

- 不能声称 FastWAM 能控制 SO-101：没有 6/12 维权重，也没有动作头训练证据。
- 不能把 FineART 的 `[1,50,14]` 当作可控策略：只证明了加载/动作管线/序列化，没有语言规划、动作质量或 SO-101 适配。
- 不能声称 ROS 急停"物理上停住了"：只证明调用与状态机；力矩/编码器证据在实机。
- 不能声称候选包可上线：未切 `current`、未部署、未做真实设备回归。
- 不能把 DOM 验收里的 fake runner 结果当作训练/推理可跑。

## 4. 候选包

| profile | ID | 文件数 | 演练 |
| --- | --- | --- | --- |
| app | `lelab-app-070-20261117T160714Z` | 1682 | 解包 / 校验 / 篡改拒绝 / 恢复 全 passed（含 LoRA 改动） |
| ros | `lelab-ros-070-20261117T160744Z` | 1763 | 同上；含 `app_ros_mobile` + `ros2_ws/src/lelab_mobile_agent` |

早前一对（`…T070209Z`/`…T070237Z`，1681/1762）被这组取代，目录保留以便对比。
`MANIFEST.json` 含 branch/commit/profile/requires/excludes/sha256；
`SHA256SUMS` 可逐文件复核。tarball 与 payload 是再生工件，按 `.gitignore` 不入库
（与既有的训练监控快照处理一致）。

## 5. 剩余阻碍（精确、可复跑）

1. **FineART**：接受 `google/paligemma-3b-pt-224` 许可并配 `HF_TOKEN`（或把 tokenizer
   落到本地并同步改 `config.json` 与 `policy_preprocessor.json` 的引用）。
   之后 `run_fineart.py` 四阶段可直接跑；保存/重载还需再腾 9 GB。
2. **FastWAM 未冻结配置**：需 ≥72 GB 训练态，单卡不可行；要么冻结视频专家（已证 34 GB 可更新），
   要么分片/ZeRO。
3. **FastWAM 动作头**：base checkpoint 缺 `action_encoder/head/proprio_encoder`，
   要做真实动作质量评估必须先有一次 SO-101 微调（数据 + 6/12 维配置）。
4. **实机**：仍需设备与运动授权，本轮未申请扩大。
5. **LoRA 只在 FastWAM 上验证**：其他策略的默认 targets 未审；`modules_to_save` 可训练量
   的拆分统计受 `base_model.model.` 前缀影响（总量 7.92M 为权威值）。
6. **镜像**：`hf-mirror.com` 可用但 gated 403；`modelscope.cn` 的 PaliGemma 副本与 HF
   LFS 摘要逐位一致，仅用于无 token 环境继续验证，**不替代**许可证。见 `MIRRORS.md`。
