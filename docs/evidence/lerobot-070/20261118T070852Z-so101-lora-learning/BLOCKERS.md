# 阻塞点（本轮）

## B-1 g128 从 2026-11-18T14:4xZ 起不可达 → L3–L6 全部 blocked

L3 的启动命令已发出（见 `L3-COMMAND.md`），但**没有拿到任何训练日志回读**就失去连接。
在恢复之前，L3 的状态只能是 `unknown`，不能写成 running 或 passed。

### 证据（按时间顺序，命令与原始输出）

| # | 命令 | 输出 |
| --- | --- | --- |
| 1 | `ssh lfy@g128 'tail -20 .../runs/so101-lora/l3.log'` | `Exit Code: 137 / Signal: SIGTERM`（本地命令超时被 SIGTERM，**没有**拿到远端输出） |
| 2 | `ssh -o ConnectTimeout=15 lfy@g128 'tail -12 .../l3.log'` | `Connection timed out during banner exchange` |
| 3 | `ssh -vv -o ConnectTimeout=20 lfy@g128 'echo ok'` | `debug1: Executing proxy command: exec nc -X connect -x 222.20.98.74:1080 192.168.6.128 22` → `Connection timed out during banner exchange` |
| 4 | `nc -z -X connect -x 222.20.98.74:1080 192.168.6.128 22` | 无输出（超时，无 "succeeded"） |
| 5 | `ssh -o ConnectTimeout=25 g128-lan '...'` | `Connection closed by 192.168.31.54 port 22128` |
| 6 | `bash -c 'exec 3<>/dev/tcp/192.168.31.54/22128'` | `relay54_tcp_ok`（**局域网中继本身端口是通的**） |
| 7 | `ssh hust125 '...'`（同一校园网的另一入口 `192.168.31.122:22022`） | `connect to host 192.168.31.122 port 22022: Connection timed out` |
| 8 | `curl https://pypi.tuna.tsinghua.edu.cn/simple/` | `pypi=200 time=1.2s`（本机外网正常） |

### 判读（只写能站住的部分）

- 本机网络与外网出口正常（#8）。
- `192.168.31.54:22128` 的 TCP 握手成功但立刻被关闭（#5/#6），说明中继进程活着、**它的上游
  192.168.6.128:22 不通**。
- 另一个校园网入口 192.168.6.125 也不通（#7）。
- 两个互相独立的入口同时指向"校园侧目标不可达"，而不是本机或单一中继故障。
- **无法区分**是 g128 关机/崩溃，还是校园网侧网络中断；本轮没有带外通道（IPMI/串口）可查。

### 影响

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| L0–L2 | 已完成（`6089599`） | 数据契约、冻结配方、tiny LoRA 闭环 19/19，均在本机 CPU |
| L3 真实短程 | **unknown** | 命令已发，日志未能回读；恢复后必须先读 `l3.log` 判定是 0 步还是跑完 |
| L4 拟合 / L5 pilot / L6 离线加载 | **blocked** | 依赖 g128 的 V100S；本机无 GPU 且被 4 GiB 地址空间限制（6B 权重放不下） |
| 开发集评估代码 | 本轮补齐并在 CPU 上用 tiny/fake 自测 | 恢复后一条命令即可跑 |

### 恢复后的第一件事

```bash
ssh lfy@g128 'tail -40 ~/lelab-rmr/runs/so101-lora/l3.log; ls -l ~/lelab-rmr/runs/so101-lora/l3/checkpoints 2>/dev/null'
```
- 若日志里已有 `step:20` 与 checkpoint → L3 按真实结果结算（不重跑）。
- 若日志停在加载阶段或为空 → 判 `aborted_before_first_step`，在**同一预算**内重跑一次，
  并把这次计入"不另起 run 绕过上限"的例外说明里。
