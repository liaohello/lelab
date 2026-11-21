# 候选包验收结论（App / ROS）

Run `20261120T025431Z-candidate-acceptance`，工作区 `8ac90fc`，分支 `codex/lerobot-070-integration`。
输入是两个 2026-11-17 归档的 `candidate.tar.gz`，清单均绑定 `9f1b73c`。未部署、未切 `current`、未推送。

## 1. 一句话结论

| profile | 归档完整性 | 归档能否按原样运行 | 结论 |
| --- | --- | --- | --- |
| App | **PASS**（1682 文件，逐文件哈希 + 篡改/删除拒绝 + 独立环境导入根全部命中包内） | **不能**：`ModuleNotFoundError: No module named 'lerobot.datasets'` | **FAIL** |
| ROS（兼容环境 g128） | **PASS**（1763 文件，同上） | **不能**：测试在收集阶段即失败（同一缺失）；补回后 833 项中 9 失败 | **FAIL** |
| ROS（目标环境，ROS 2） | 未执行 | 本机与 g128 均无 `rclpy` / `rosbag2_py` / `ros2` CLI | **BLOCKED** |

**根因不是网络、不是模型、不是环境**，而是打包脚本把 `lerobot` 的核心包目录排除了：
`source-excludes.txt` 里裸写的 `datasets` 作为路径分量匹配，命中
`releases/sanitized-20261024-01/lerobot/src/lerobot/datasets`，28 个文件全部没进包
（清单里该目录 0 条）。`lelab/server.py` 在 import 阶段就 `from . import record`，
`record.py` 再 `from lerobot.datasets import LeRobotDataset`，于是服务连启动都到不了。

反事实验证：把**恰好这 28 个文件**（`9f1b73c..HEAD` 对该目录零差异）放回解包树，
不改任何其他字节，App 运行时验收 **21/21 全通过**，ROS 全套从 1 个收集错误变成 833 项可执行。
这说明包内其余部分是好的，缺陷边界就是这一个目录（ROS 另有第二个，见 §4.2）。

## 2. 验收入口

`releases/sanitized-20261024-01/app/deploy/accept_candidate.py`（新增，241→约 520 行）。
复用 `package_lerobot070_candidate.py` 的 `PROFILES` 与 `_hash_file`，从既有 tar 出发而不是重新打包：

1. 归档 SHA-256、大小、mtime；
2. 与 **Git 中受版本控制**的 `MANIFEST.json`/`SHA256SUMS` 逐字节比对（可信锚点不取自归档自身）；
3. profile、candidate 名、`revision`、`file_count` 一致性；
4. **不解包**，直接在 tar 流里核对文件集合与 1682/1763 个逐文件哈希；
5. 解包到新目录后再验一遍（双向：缺失、篡改、多余都算失败）；
6. 单字节翻转 + 删除一个文件，两者都必须被拒，恢复后必须复验通过；
7. 按包内 `uv.lock` 建独立环境（`uv sync --frozen --no-dev --no-install-project --no-install-package lerobot`），
   清空 `PYTHONPATH` 后断言 `lelab`、`lerobot`、`frontend/dist` 都解析到解包目录；
8. **把 `import lelab.server` 纳入门槛**：哈希全对但服务起不来的包不叫可接受。

第 8 条是本轮唯一“超出打包脚本原有逻辑”的增补，也是这次能一眼定位缺陷的原因。

## 3. App profile

### 3.1 归档验收：PASS

`app/acceptance.json`：
- 归档 `78377a08…091fe`，`revision 9f1b73c`，1682 文件；
- 9 项清单检查全过（含与 Git 内清单逐字节相同）；
- 解包复验全过；篡改受害者为 `app/frontend/dist/assets/index-D1QziPbU.js`（浏览器真正加载的文件），
  删除受害者为 `app/.gitattributes`，两者都被拒、恢复后复验通过；
- 独立环境：Python 3.12.3，`uv sync` 16 分 48 秒（本地缓存缺 `httpx2`/`pyarrow` 等，
  首次用清华镜像补齐 4 GB 级依赖后固化），导入根全部落在 `acceptance-run/app3/stack/payload/…`；
- `import lelab.server` → `ModuleNotFoundError: No module named 'lerobot.datasets'`。

### 3.2 原样启动：FAIL

`app/runtime-archived.json`（g128，回环 8020）：6 项进程级检查通过（解释器、入口、端口、
干净退出、无子进程、端口释放），`/health` 始终拒绝连接；子进程 `exit_code=1`，
日志尾部即上面的 `ModuleNotFoundError`。

本机另有一次同样结论的运行（`app/server.log`），但本机 4 GiB 地址空间上限会让
torch 在导入期直接 core dump，**不能**用来归因，故以 g128 的结果为准。

### 3.3 反事实（补回 28 文件后）：21/21 PASS

`app/runtime-with-datasets-restored.json` → 实际以 `runtime-restored.json` 为准（g128）：
- `/health` 200，启动 6.4 s；
- `/` 返回 `index.html`，`/assets/index-D1QziPbU.js`（1,890,419 B）与
  `/assets/index-p4FyEwDW.css`（64,590 B）的 SHA-256 **与清单逐字节相同**；
- `/training` 深链在 `Accept: text/html` 下回退到同一份 index（SPA 行为正确，
  非导航请求仍 404，这是设计而非缺陷）；
- 四类非法远程请求都被拒：缺 `remote_deployment`/`remote_semantics` → 422，
  缺维度 → 422，带退役的 `actions_per_chunk` → 422，6 维双臂契约 → **500**（拒绝正确、状态码类别不对，见 D-3）；
- 合法远程请求穿过校验、真的走到 SSH 投递：`Could not stage policy … ssh: connect to host 127.0.0.1 port 1: Connection refused`；
- 停止后 `exit_code=-15`，无残留子进程，端口释放；
- 运行时 `lelab` / `lerobot` 均来自解包树。

## 4. ROS profile

### 4.1 归档验收：PASS

`ros/acceptance.json`：归档 `378ca036…9db1`，`revision 9f1b73c`，1763 文件；
清单/文件集合/逐文件哈希/篡改（`app_ros_mobile/frontend/dist/assets/index-DyZMExHr.js`）/
删除（`app_ros_mobile/.gitattributes`）/恢复全过。包内确实含 `app_ros_mobile`、
`lelab_mobile_agent`（含 `launch/readonly.launch.py`）与 `lerobot`。

### 4.2 兼容环境回归：FAIL（可归因）

在 g128（Python 3.12.16，无 ROS，地址空间无限，线程余量 205 万）解包后运行包内测试：

| 阶段 | 结果 |
| --- | --- |
| 归档原样，急停文件单跑 | **收集错误** 1 项（`lerobot.datasets` 缺失） |
| 补回 28 文件后，定向（急停/远程分发/录制/AV1 导出） | 85 项：**84 passed / 1 failed**，25.4 s |
| 补回后，全套 `tests/` | 833 项：**824 passed / 9 failed / 2 skipped**，82.8 s |

9 个失败分三组：

| 组 | 数量 | 现象 | 归因 |
| --- | --- | --- | --- |
| A | 7 + 2 skipped | `configs/ros2_collection/hardware-readonly-v1.json`、`configs/mobile_teleop/leader.example.json` 不存在 | **D-2：ROS profile 没打 `configs/`**。`ros_mobile._repo_root()` 取 `parents[2]/configs`，包里没有该目录 |
| B | 1 | `test_estop_without_recording_does_not_wedge_next_dataset`：`'invalid' != 'active'` | **D-3：产品回归**，本机在 `HEAD 8ac90fc` 上 1.12 s 复现，与打包无关 |
| C | 1 | `test_snapshots_are_listed_newest_first`：两个同秒 id 顺序不稳定 | 测试设计缺陷（同秒 + 随机后缀），换机器也未必稳定 |

A 组里 2 个 skipped 的原因是 “hardware-profile rehearsal evidence is not present”，
同样来自缺失的 `configs/`。

AV1 导出：定向集合包含 `test_t4_hybrid_9d_export.py`（真实写出 LeRobot 数据集并读回视频），
通过。**编码器事实**：系统 `ffmpeg` 只有 `libaom-av1`，没有 `libsvtav1`；
但 LeRobot 0.7 走 PyAV，`av.codecs_available` 含 `libsvtav1`，所以默认 AV1 编码可用
（`ros/env-compat.json`）。

### 4.3 目标环境：BLOCKED

`ros/target-environment.json`：`rclpy`、`rosbag2_py`、`ros2` CLI 三者皆无；
`lelab_mobile_agent` 与 `launch/readonly.launch.py` 在包内。目标侧启动、节点列表、
日志检查因此**未执行**，不拿兼容环境结果替代。

## 5. 运行时回退演练：PASS（机制层）

`app/rollback/rollback-app.json`，16/16。临时发布目录 + 测试 `current` 指针：

| 步骤 | 结果 |
| --- | --- |
| 旧版（`lelab-app-070-20261117T070209Z`，`e9671f3`）健康、服务自己的 dist（按清单哈希） | passed |
| 切 `current` → 候选（`…T160714Z`，`9f1b73c`），健康、boot 报告指向 candidate、dist 与旧版**确实不同**（`index-D1QziPbU.js` vs 旧版资源） | passed |
| 注入可控故障：`SIGSTOP`（进程活着、端口仍占、不再应答）→ 连续 3 次健康检查全部失败 | passed |
| 切回旧版 → 指针、实际加载的 `lelab.__file__`、被服务的字节三者都回到旧版，健康恢复 | passed |
| 结束端口释放 | passed |

**与归档级恢复的区别**：`acceptance.json` 里的 tamper/restore 证明“字节可校验”；
这里证明“流量可切回”，两者不能互相替代。

**限制**：两侧都用了“归档 + 补回 28 文件”的树（否则起不来），报告里记为
`--old-dir/--candidate-dir` 且标注 `archive_sha256` 仅供溯源。

## 6. 缺陷与修复建议

| 编号 | 影响 | 修复 |
| --- | --- | --- |
| **D-1** | 两个 profile 都缺 `lerobot/src/lerobot/datasets`（28 文件），服务无法 import | `source-excludes.txt` 的 `data`/`dataset`/`datasets`/`runs` 等裸词应加前缀锚定（如 `./data`、`./runs`），或改成只匹配顶层；打包后增加“`import lelab.server` 必须成功”的门槛 |
| **D-2** | ROS profile 缺 `configs/`，7 个测试失败 + 2 个跳过 | `PROFILES["ros"]["components"]` 增加 `configs`；或让 `_repo_root()` 可配置 |
| **D-3** | 急停后打开新数据集，状态仍为 `invalid` | 新会话开始时清除 `_estop_latched_request`/待确认闩锁；该用例本就是为这条契约写的 |
| **D-4** | 双臂 6 维契约拒绝返回 500 而非 400 | 在 `handle_start_remote_inference` 里把 `RemoteInferenceError` 映射成 400，其余仍 500 |
| **D-5** | 同秒快照排序断言不稳定 | 快照 id 加单调序号，测试按序号断言 |

## 7. 发布决策还缺什么

1. 修 D-1/D-2 后**重打包**（同一个 `package_lerobot070_candidate.py`），再用本轮入口重跑：
   `accept_candidate.py` + `run_app_acceptance.py` + `run_pytest_suite.py` 三条命令即可复现全部结论。
2. D-3 修复后重跑 `test_hybrid_capture.py`；D-4 修复后重跑 `run_app_acceptance.py` 的第 4 类拒绝。
3. 目标 ROS 2 环境（`rclpy`+`rosbag2_py`+agent launch），用模拟/无运动配置做启动、节点、日志检查。
4. 实机仍无授权：本轮未连接任何设备，未发送任何动作。
