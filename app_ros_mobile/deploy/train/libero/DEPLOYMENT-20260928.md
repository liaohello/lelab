> Public copy: connection addresses, account names and machine labels below are anonymized examples. Configure your own local deployment settings.

# 192.0.2.10：LIBERO / π₀.₅ 环境检查与部署记录

检查日期：2026-09-28。SSH：`trainer@192.0.2.10:22`。

## 结论

服务器已有可用的 OpenPI LIBERO 镜像与 `pi05_libero` checkpoint。
本次复用现有环境，部署独立运行脚本与配置，并完成语言条件策略的仿真闭环检查。

远程目录：`/home/trainer/Desktop/lelab_server/libero`。
运行说明：[README.md](README.md)。模型服务使用 Docker 内部网络，无主机端口发布。

## 已核对的环境

| 项目 | 结果 |
| --- | --- |
| GPU | 4 × RTX 4090，驱动 570.86.10；测试使用 GPU 2 |
| 磁盘 | 检查时约 5.1 TiB 可用 |
| 推理镜像 | `openpi-server:latest`，固定 ID `405af5205168…` |
| 仿真镜像 | `openpi-client:latest`，固定 ID `5dc5543f91a6…` |
| 推理 Python | 3.11.9；JAX 0.5.3；PyTorch 2.7.1 |
| 仿真 Python | 3.8.20；MuJoCo 3.2.3；robosuite 1.4.1；NumPy 1.22.4 |
| 渲染 | EGL，无需桌面/X11；主视角与腕部相机均为 256×256 RGB |
| 状态/动作 | 状态 8 维；末端增量与夹爪动作 7 维 |
| 任务集 | Spatial、Object、Goal、Long 各 10 个任务 |
| 模型 | 已缓存约 12 GiB 的 OpenPI `pi05_libero`，归一化参数和 tokenizer 可本地读取 |

模型目录：

```text
/data/openpi/openpi_deploy_package/openpi_assets/openpi-assets/checkpoints/pi05_libero
```

完整镜像 ID、源码 SHA-256、归一化参数 SHA-256、部署参数保存于远程
`deployment.json`。`libero.env` 固定本次验证的镜像。

## 实际评估结果

每个 suite 取 task 0 的第 0 个固定初始状态，seed 7，1 个 episode，场景稳定 10 步，
每 10 步重新规划。每次策略调用传入真实任务语言、两路图像和机器人状态。

| Suite | 任务 | 结果 | 动作步数 |
| --- | --- | --- | ---: |
| `libero_spatial` | 将指定黑碗放到盘子上 | 成功 | 77 |
| `libero_object` | 将字母汤罐放入篮子 | 成功 | 156 |
| `libero_goal` | 打开柜子中间抽屉 | 成功 | 112 |
| `libero_10` | 将字母汤罐与番茄酱罐放入篮子 | 成功 | 259 |

首轮 Spatial 包含约 21 秒的 JAX 首次推理编译；后续任务平均单次策略调用约
0.087 秒。该数据仅描述本次检查，不是完整 benchmark 的性能统计。

四个 episode 均生成了 `evaluation.json` 与 MP4 录像。环境检查另保存了相机 PNG
和 `environment.json`。本地副本位于项目 `.smoke/libero-20260928/results/`。

最终版本的启动脚本另经过一次完整启动、Spatial 评估、停止的检查，结果仍为成功、77 步；
脚本语法检查通过。测试模型容器已停止并移除，GPU 2 恢复约 24,073 MiB 空闲显存。
后续评估按运行说明重新执行 `start` 即可。

这四次成功证明已检查的任务能完成模型与仿真的闭环，**不能据此宣称完整 LIBERO
成功率为 100%**。完整评估需要运行全部任务、指定 episode 数与种子，且固定实验协议。

## 本次部署文件

- `scripts/probe_libero.py`：安装、任务、初始状态、动作执行、渲染检查。
- `scripts/evaluate_openpi_libero.py`：语言条件评估、进度 JSON、成功判据、录像。
- `scripts/run-libero.sh`：启动、评估、状态、停止；按实例标签核对容器归属。
- `libero.env`：经过验证的镜像 ID 与默认 GPU。
- `.lelab-libero.json`：本部署实例标记。
- `deployment.json`：部署参数和验证记录。

## 与 LeLab / LeRobot 的关系

当前 LeLab 的 `/home/trainer/Desktop/lelab_server/app/.venv` 未安装
LIBERO/MuJoCo/robosuite。本次使用服务器已有的 OpenPI 服务端与客户端环境，
没有改变该 Python 环境。

现有 checkpoint 为 OpenPI/JAX 格式。LeRobot 文档中的 `lerobot-eval` 需要其支持的
policy checkpoint，不能直接指向此模型目录。本次运行已经覆盖语言、图像、状态、动作与
任务成功反馈；尚未增加 LeLab 网页评估入口，也没有进行模型训练或完整 benchmark。

参考：[LeRobot LIBERO 文档](https://huggingface.co/docs/lerobot/libero)、
[OpenPI LIBERO 示例](https://github.com/Physical-Intelligence/openpi/tree/main/examples/libero)。
