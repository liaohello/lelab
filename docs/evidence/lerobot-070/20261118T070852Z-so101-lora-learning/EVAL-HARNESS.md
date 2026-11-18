# 开发集评估链路（L5 / L6 的代码，GPU 部分等 g128）

本轮 g128 中途失联（`BLOCKERS.md` B-1），L3 的实际执行状态是 unknown。在等它回来的这段时间里，
把"拿到 checkpoint 之后怎么判定"这一环全部做完并在 CPU 上验证过，避免恢复后再现写判定逻辑。

## 组成

| 文件 | 职责 | 能否在本机跑 |
| --- | --- | --- |
| `tools/preflight_train_args.py` | 只解析 `lerobot_train` 的 argv，不建模型 | 能（已验证） |
| `tools/collect_predictions.py` | GPU：在固定锚点、固定噪声下产 `pred/gt/pad/state` 的 npz | 否（要 6B 权重） |
| `tools/score_predictions.py` | 纯 numpy：算指标与三条门槛，退出码即结论 | 能（已验证） |
| `tools/check_anchor_logic.py` | 用本地 v3 数据集跑 `collect_predictions` 的取帧代码 | 能（已验证） |
| `tools/test_score_logic.py` | 9 条 tiny/fake 断言，锁死判定逻辑 | 能（9 passed） |
| `tools/smoke_score_cli.py` | 用真实 12 维统计驱动 CLI 冒烟 | 能（exit 0） |

## 已经证明的（本轮，CPU）

**取帧与形状**：在本地 v3 数据集 `stage7_ik_dev_pilot_002`（5 集 / 578 帧 / 12 维 / 三相机）上
`check_anchor_logic.py` 8/8 通过：

- 锚点 `[0, 144, 289, 433]` 严格递增、落在区间内；
- `LeRobotDataset(episodes=[ep])[i]` 返回的 `episode_index` 与 `frame_index` **等于请求值**
  （不依赖多集子集的枚举顺序）；
- action chunk `[32, 12]`、state `[12]`、每相机 9 帧 uint8 `[9, 3, 360, 640]`；
- 尾窗（第 577 帧）`action_is_pad` 为 31/32，且**首步不被标记为 pad**。

这条是"评估会不会读错帧"的关键前置：锚点算错一位，整份评估就静默失效而且看不出来。

**判定**：`test_score_logic.py` 9/9。其中包括四条"必须判不通过"的用例（保持姿态、只赢静态维、
NaN、破坏静态维），一条"必须判通过"（完美预测），以及 std=0 维度不得进入缩放指标
（真实数据第 6、10 维 std 正好 0.0，直接相除会变 inf）。

**CLI**：`smoke_score_cli.py` 用真实 `train_stats.json` 跑通，输出的活跃维是 `[0..5]`
（左臂），静态维 std `[0.0, 0.094, 0.1175, 0.0309, 0.0, 0.0008]`，注入 40% 噪声的候选得到
`improvement_active_scaled=0.506` 并判 passed。

## 尚未证明的（必须等 g128）

- 真实 FastWAM 在这些锚点上的输出：没有权重就没有 `pred`；
- adapter 的挂载路径（`PeftModel.from_pretrained(base, dir)`）是否与 LeRobot 保存的目录结构一致；
- 训练是否真的下降：`L3/L4/L5` 的 loss 序列一个都没拿到；
- 断言"优于 `state_copy`"是否可达成：这是经验问题，只能在真实数据上答。

## 一个不能省的分工

产出预测和判定预测是**两个进程**：`collect_predictions.py` 只写 npz，`score_predictions.py`
不 import torch。这样"写分"的代码没有机会顺着模型输出调整，反之亦然。所有候选（step0、
L3、L5）共用同一份 `train_stats.json`、同一锚点、同一噪声 seed 2026。
