# 阶段 2–3：推理输入契约、计时，与相对动作数据路径

## 1. 推理输入契约（FW-EVAL-02）

采集侧只喂**当前观测**：三相机各 9 帧（t=0,4,…,32）的 float32 `[0,1]`、`observation.state`、
任务文本。标签（`action`）与 `action_is_pad` 在采集时**另存**，评分侧独立读取，不进入模型输入。

`collect_ab.py` 的 `--isolation-check` 在第一个锚点上做三次对照（同噪声）：

| 干扰 | 含义 | 期望 |
| --- | --- | --- |
| 未来帧（t=4…32）全部置零 | 若模型偷看未来，输出会变 | 差值应为 **0** |
| 把 `action` 标签置零 | 若标签混进输入，输出会变 | 差值应为 **0** |
| `observation.state` 平移 +100 | **正对照**：状态是输入，必须影响输出 | 差值应 **> 0** |

第三项是防止"前三项都为 0 只是因为模型根本不看输入"的假通过。实测值见 `EVAL-AB.md`。

同时记录生效配置（`collect-*.json`）：`use_relative_actions`、`relative_exclude_joints`、
`action_feature_names`、`image_size`、`horizon`、`noise_seed=2026`、`relative_step_bound`（相对锚点
是否真的绑定到策略的动作队列）。

## 2. 计时（本轮实测，g128 V100S）

| 项 | 实测 | 来源 |
| --- | --- | --- |
| 冷启动到第一步（加载 6028.7M 参数 + Wan VAE + CPU 文本编码器） | **144 s** | A 组时间线：wrapper 15:41:14 → 首步 15:43:38 |
| 稳态单步（100 updates） | **2.49 s** | `step_s` 列 |
| 保存 checkpoint + 进程退出 | **≈ 48 s** | 末步 → 进程结束 |
| 100 updates 全程 | **441 s** | `a-timeline/timeline.json` |
| 单锚点采集（三相机 9 帧解码 + 首帧 VAE + 去噪） | **13.5 s** | 上一轮 80 锚点 1084 s |
| 12 锚点采集（含加载与退出） | 估 ≈ 5.5 min | 144 + 12×13.5 + 退出 |

上一轮把"推理要编 9 帧、走 32 次去噪"当成主因，本轮按阶段拆开记录后：解码 27 帧约占 1/3，
首帧 VAE + 去噪占其余；`image_size` 三相机拼接 672 宽是主要成本项。**没有为此改动模型**，
因为改分辨率会同时改变 A/B 两个臂的可比性。

缓存策略：本轮不做跨锚点缓存。理由是每锚点观测都不同（不同帧、不同相机内容），
命中率接近 0；真正的重复成本是模型加载，已通过"一个进程跑完该臂全部锚点"摊薄。
若将来要缓存，键必须是 `(view, episode, frame, camera_keys, image_size, processor digest,
采样步数, noise seed)` —— 少一项就可能把不同输入当同一份。

## 3. 相对动作数据路径（FW-REL-01）

### 3.1 语义

`rel[t] = action[t] - state[anchor]`，**整个 32 步 chunk 共用生成时刻的原始状态**；
夹爪保持绝对百分比。维度按数据集 `meta/info.json` 的 `names` 逐维核对：

| 维 | 名称 | 单位 | 处理 |
| --- | --- | --- | --- |
| 0–4 | `left_{shoulder_pan,shoulder_lift,elbow_flex,wrist_flex,wrist_roll}.pos` | 度 | 转相对 |
| 5 | `left_gripper.pos` | 百分比 | **保持绝对** |
| 6–10 | `right_*` 关节 | 度 | 转相对（基本静止） |
| 11 | `right_gripper.pos` | 百分比 | **保持绝对** |

排除规则走 FastWAM 自带的 `relative_exclude_joints=["gripper"]`，由名字子串匹配成 mask
（`_build_mask`），不手写索引。

### 3.2 统计（train-only，horizon 32）

`tools/relative_stats.py`：复用 LeRobot 的 `compute_relative_action_stats`，并与一份独立实现逐值对比。

| 检查 | 结果 |
| --- | --- |
| 库与独立实现的 mean 差 | < 1e-4 |
| 库与独立实现的 std 差 | < 1e-4 |
| 夹爪维（5、11）仍为绝对 | ✓ |
| 相对关节均值接近 0 | ✓（最大 0.68°） |
| 活跃关节相对 std < 绝对 std | ✓（比值 0.21 / 0.33 / 0.35 / 0.40 / 0.22） |

71,031 个有效 chunk（窗口不跨集）、2,272,992 行。

**踩到的两个坑**（都记在代码注释里）：

1. 独立实现最初用 float32 累加 45 万行，std 漂移 0.039（≈1%），看起来像两个实现不一致；
   改成 float64 累加后差 < 1e-4。这条同时说明：**任何自写的统计都必须用 float64 累加**。
2. 夹爪维的统计是 **chunk 加权**（每帧被多少 chunk 覆盖），不等于逐帧绝对值统计
   （均值 16.60 vs 17.55）。最初拿它和逐帧绝对统计比，误判为"夹爪被转成相对了"。

### 3.3 训练视图

`tools/make_relative_view.py` 生成 `assets/so101_left_red_rel`：data/videos 软链、meta 复制、
**只把 `meta/stats.json` 的 action 段换成相对统计**（state 段不动）。6/6 检查通过，
真实样本过处理器 round-trip 误差 3.8e-6。

### 3.4 处理器契约（8 条 CPU 回归）

`tests/policies/fastwam/test_fastwam_relative_actions.py`：

1. 绝对→相对→绝对 round-trip 回到原值；
2. **物理零残差**还原为当前姿态（不是"归一化 0"，那是 mean 偏移后的非零物理量）；
3. 夹爪维不被转换（逐元素相等）；
4. chunk 在途时锚点不变，队列排空后重新取锚；
5. `reset()` 清空锚点并重新取；
6. 缺 `action_feature_names` 时**报错**而不是把 12 维全部转相对（本轮新增的保护，
   见 `processor_fastwam.py`：没有名字时 `_build_mask` 会静默返回全 True）；
7. 后处理器在没有缓存锚点时抛错；
8. 关闭相对动作时行为与纯绝对一致。
