# 候选包验收（App / ROS）—— 2026-11-20

起点 `8ac90fc`（分支 `codex/lerobot-070-integration`），输入是 2026-11-17 归档的两个
`candidate.tar.gz`，两者清单都绑定 `9f1b73cc9ff8cdf08ba689dc4cccd9a0b6b403dc`。
本轮只做验收：未部署、未切 `current`、未推送、未连接硬件。

结论见 `REPORT.md`，命令与退出码见 `COMMANDS.md`。一句话：
**归档字节完好，但两个包都缺了 `lerobot/datasets`（28 个文件），App 因此无法启动、ROS 测试无法收集。**

## 0. 输入漂移（先报告，未改写）

| 项 | 值 |
| --- | --- |
| 工作区 HEAD | `8ac90fc`（比候选清单多 14 个提交） |
| 候选清单 `revision` | `9f1b73c` |
| 差集内容 | FastWAM LoRA 之后的 SO-101 训练、相对动作、评分器、证据文档 |
| 处置 | 本轮只验收归档字节；HEAD 之后的改动按约定另走重打包与回归 |

| profile | archive | SHA-256 | 文件数 |
| --- | --- | --- | --- |
| app | `lelab-app-070-20261117T160714Z/candidate.tar.gz` | `78377a084f8f61f144fc661b02f7a05736a0a6f8fb48b34680e313a7eef091fe` | 1682 |
| ros | `lelab-ros-070-20261117T160744Z/candidate.tar.gz` | `378ca03638e7a872a8a4b58828a209e354fdc8d5fa5e0dabacda9d0251fc9db1` | 1763 |

## 1. 目录

```
app/acceptance.json                           归档验收（清单/文件集合/逐文件哈希/篡改/环境/导入根）
app/runtime-archived.json                     原样启动：FAIL（import lerobot.datasets）
app/runtime-restored.json                     补回 28 文件后：21/21 PASS
app/server-*.log                              两次启动的服务端日志
app/rollback/rollback-app.json                服务级回退演练：16/16
ros/acceptance.json                           归档验收（env 未建，原因见 COMMANDS §4）
ros/ros-tests-{archived,targeted,full}.json   1 收集错误 / 85 项 1 失败 / 833 项 9 失败 2 跳过
ros/target-environment.json                   目标 ROS 2 环境：BLOCKED
ros/env-compat.json                           g128 的 Python/线程/地址空间/AV1 编码器事实
tools/                                        运行时、pytest、回退、环境探针等辅助脚本
```

## 2. 验收入口

本体在 `releases/sanitized-20261024-01/app/deploy/accept_candidate.py`，
复用 `package_lerobot070_candidate.py` 的 `PROFILES` 与 `_hash_file`，从既有 tar 出发：

```bash
python releases/sanitized-20261024-01/app/deploy/accept_candidate.py \
  --archive <candidate.tar.gz> --profile app|ros --dest <新目录> \
  --report <dir>/acceptance.json \
  --trusted-dir <Git 中的 MANIFEST.json/SHA256SUMS 目录> \
  --expect-commit 9f1b73cc9ff8cdf08ba689dc4cccd9a0b6b403dc --worktree .
```

它做八件事：归档 SHA-256 → 与受版本控制的清单逐字节比对 → profile/提交/文件数一致 →
在 tar 流内核对文件集合与逐文件哈希 → 解包后复验（缺失/篡改/多余都算失败）→
单字节翻转与删除都必须被拒并恢复 → 按包内 `uv.lock` 建独立环境并断言导入根 →
**`import lelab.server` 必须成功**。最后一条是本轮新增，也是这次定位缺陷的关键。

`--reuse-stack` / `--reuse-env` 让解包与建环境只做一次，之后重跑只校验（环境构建 16 分 48 秒）。

## 3. 三个必须记住的边界

- **反事实不是验收通过**：21/21 是在“归档 + 恰好补回 28 个文件”的树上得到的，
  用于界定缺陷范围；归档本身仍是 FAIL。
- **兼容环境不是目标环境**：ROS 的 833 项全部在无 ROS 的 g128 上跑，
  目标侧启动/节点/日志仍 BLOCKED。
- **服务级回退不是文件级恢复**：`acceptance.json` 的 tamper/restore 证明字节可校验，
  `rollback/` 里的演练证明流量可切回，两者分别记录。
