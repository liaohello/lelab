# FineART-VLA 真实模型最小验证 — BLOCKED（资源与时间预算）

状态：**blocked**，不算 passed。本文件给出已完成的准备、具体阻碍与可复跑命令。

## 已完成的准备

1. **代码路径**：`lerobot/fineart_vla` 离线契约测试 16 passed（TC-MOD-02：配置、
   processor、loader、recipe 分支）；预检依赖门禁存在（transformers）。
2. **repo 固定**：`lerobot/fineart_vla_base`（备选 `lerobot/pi05_base`），未 gated。
3. **精度策略**（按计划 §4.1）：PI05 继承校验只接受 **float32 / bfloat16**，不得为
   硬件改用 fp16。V100S(sm70)：float32 原生、bfloat16 仿真可用（1-3 个 update 可接受）。
4. **验证脚本骨架**：与 `fastwam_gpu_verify.py` 同构（integrity → load → offline
   action → save/reload → 1-3 updates），可直接改造复用。

## 具体阻碍（本轮实测）

| 阻碍 | 证据 |
| --- | --- |
| 磁盘预算 | g128 根分区 97% 满；FastWAM 路径已用 44 GB（cache）+ umt5 tokenizer，剩 ~36 GB。再下 ~12-15 GB 的 fineart 权重 + 验证峰值会逼近下限；一次只验证一个模型是既定策略 |
| 镜像速率 | `hf-mirror.com` 对 `google/umt5-xxl` 的 11 GB 下载 >1h 停滞在 65 KB（`fastwam-verify.log` 时间线）；fineart 若含同类大分片，同样存在不可预测的停滞风险 |
| 会话时长 | 本轮已在 FastWAM 上消耗完整的下载→环境修复→验证周期；FineART 完整周期预估还需 1-2 小时下载 + 验证 |

## 可复跑命令（资源恢复后，按序执行）

```bash
ssh g128
cd ~/lelab-rmr && source .venv/bin/activate
export HF_ENDPOINT=https://hf-mirror.com HF_HOME=/home/lfy/lelab-rmr/hf-cache
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
# 1. 下载（约 12-15 GB；确认 df -h / 剩余 >40 GB）
python -c "from huggingface_hub import snapshot_download; print(snapshot_download('lerobot/fineart_vla_base'))"
# 2. 完整性：safetensors 头 + 无 *.incomplete + LFS 拒绝（复用 fastwam_gpu_verify.py stage1 逻辑）
# 3. 逐段验证：复制 fastwam_gpu_verify.py，把 repo 换成 fineart_vla_base、policy 类换成
#    FineARTPolicy；dtype 用配置声明的 float32/bfloat16；task 传中文与空串各一次
#    （动作模式按 TC-CLI-04 的 requires_task 契约）。
# 4. 1-3 个 optimizer update 用合成 native-space batch（明确标注：非微调证据）。
```

## 通过标准（到达时）

- 完整组件（policy 权重 + processor + tokenizer）真实加载，无 LFS 指针冒充；
- 离线动作 finite/shape 正确（native 特征空间）；
- 保存/重载后同噪声同动作（固定 seed）；
- 1-3 update 的 loss/梯度有限并出 checkpoint；
- 以上每项独立记录耗时/显存；**原生可运行 ≠ SO-101 6/12 维可部署**（另需动作头
  适配/微调与实机验收）。
