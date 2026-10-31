# LIBERO 环境与 π₀.₅ 评估

此部署复用训练服务器现有的 `openpi-server` 和 `openpi-client` 镜像，以及
`pi05_libero` 的 OpenPI/JAX checkpoint。执行目录为
`~/Desktop/lelab_server/libero`，与 LeLab 的训练 Python 环境分开。

`scripts/run-libero.sh` 读取执行目录中的 `libero.env`，其中固定本次验证的镜像 ID。
原有镜像、容器、模型文件和 LeLab Python 依赖不需要修改。

## 运行

```bash
ssh -p 22 trainer@192.0.2.10
cd ~/Desktop/lelab_server/libero

# 检查安装、四个任务集、固定初始状态、动作执行与 EGL 相机渲染。
bash scripts/run-libero.sh probe

# 加载服务器上已有的 π₀.₅ LIBERO 权重，默认使用 GPU 2。
bash scripts/run-libero.sh start
docker logs -f lelab-libero-policy
# 日志出现 “server listening” 后，Ctrl-C 退出日志查看。

# 单任务、单 episode 检查完整链路。
bash scripts/run-libero.sh eval \
  --suite libero_spatial --task-ids 0 --episodes 1 --seed 7 --replan-steps 10

# 单个 suite 的 10 个任务，各评估 10 个固定初始状态。
bash scripts/run-libero.sh eval \
  --suite libero_spatial --task-ids all --episodes 10 --seed 7 --replan-steps 10

# 回收本次部署的模型进程和显存，保留文件与镜像。
bash scripts/run-libero.sh stop
```

其他标准 suite：`libero_object`、`libero_goal`、`libero_10`。
不同 suite/seed 分别执行，结果写入独立目录。单任务冒烟结果不能作为正式 benchmark 成绩。

## 结果与接口

- `results/environment/environment.json`：环境版本、任务集数量、观测维度和渲染检查。
- `results/environment/*image.png`：任务相机图像。
- `results/<run>/evaluation.json`：任务语言、初始状态编号、成功率、步数、推理耗时。
- `results/<run>/*.mp4`：每个 episode 的仿真录像。
- `results/policy-server-last.log`：停止服务器时保存的模型日志。
- `deployment.json`：镜像、checkpoint、源码与配置记录。

评估沿用镜像中 OpenPI 示例的任务构建、四元数转换和图像处理：相机画面旋转
180°，缩放填充至 224×224；状态为末端位置、轴角、夹爪位置共 8 维；动作是
末端增量与夹爪控制共 7 维；每次推理显式发送当前任务的 `prompt`。
初始状态按 episode 编号选取，前 10 步用于场景稳定，默认每 10 步重新规划。

模型服务与客户端通过独立 Docker 内部网络连接，没有发布主机端口。
容器用 `lelab.instance_id` 和 `lelab.component` 标记归属，停止操作仅处理本部署的容器。
镜像中的 Python 位于 `/root/.local/share/uv`，因此客户端沿用镜像的 root 用户运行；
其可写持久化挂载为本部署目录，结果文件可由主机用户读取。

这里验证的是 **OpenPI 的 π₀.₅/LIBERO 路径**。当前 LeLab 的
`app/.venv` 尚未安装 `hf-libero`，且 OpenPI 的 JAX checkpoint 不能直接作为
LeRobot `lerobot-eval --policy.path` 的模型文件使用。若采用 LeRobot 路线，需要其
支持的 checkpoint 格式与独立的 LIBERO 依赖环境。

参考：[LeRobot LIBERO](https://huggingface.co/docs/lerobot/libero)、
[OpenPI LIBERO](https://github.com/Physical-Intelligence/openpi/tree/main/examples/libero)。
