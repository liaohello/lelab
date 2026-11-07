# T2 状态 — 2026-11-04 Wave A（重写，覆盖 08:13 旧版）

## 状态

**离线交付完成，T1/T3 可消费：协议规范、13 个 golden vectors、独立候选 C 核心、哈希固定的三段补丁集（含板卡集成段）、原生编译 host 验证与独立契约测试全部通过；STM32 交叉构建受阻（本机无 ARM 工具链），未产出任何 hex/bin/axf，无任何烧录或实机验收。** 本轮没有 SSH、连接/打开任何设备或串口、启停服务/容器、烧录或训练。未修改参考树（只读）、ROS、上位机、前端、公共文档、部署脚本或 runbook。host 测试不证明停车、时序、IWDG 行为或真实编码器路径。

## 本会话文件范围（均属 T2 归属）

- `tools/ros2_collection/vendor_patches/firmware/`：
  - `PROTOCOL.md`（v1 线缆契约）、`golden_vectors.json`（13 个字节级向量，更新自旧版，新增 capability_query/release/wrong_length）
  - `t2_protocol.h` / `t2_protocol.c`（候选 C 核心，严格 `-std=c99 -Wall -Wextra -Werror -pedantic` 干净；快照发布用 seq 奇偶代际撕裂读防护；计数转换上界收紧为 `< ticks_overflow`）
  - `t2_host_harness.c` + `t2_crc_reference.h`（原生编译执行真实 C 逻辑，76 项检查）、`t2_host_build.sh`（可复现原生构建+证据归档）
  - `0001-encoder-timer-fixes.patch`（沿用，60001 模数与第 4 路 TIM3 启动）、`0002-standalone-candidate.patch`、**`0003-t2-board-integration.patch`（新增：packet_handle.c 回调注册、stm32f4xx_it.c TIM7 ISR 内采样快照+到期检查（中断内不做串口 I/O）、app.c 主循环节点、新胶水文件 `Hiwonder/System/t2_board.{c,h}`）**
  - `MANIFEST.json`、`baseline.sha256`（13 文件子集）、`baseline-source.sha256`（1159 源文件全列）；`t2_host_jit.py`、`t2_verify.py` 保留但**已声明被取代**（llvmlite 环境不存在，旧 JIT 路径从未可运行）
  - `README.md`（重写：文件清单、补丁应用/回退命令含 `patch -R -E`、验证结果、构建缺口）
- `tools/ros2_collection/tests/test_t2_firmware_protocol.py`：T2 独立命名契约测试（pytest）。
- `work/ros2_collection/firmware-development/`：`build_integration.py`（生成哈希固定补丁）、`verify_patches.py`（隔离目录 apply/revert/错基线拒绝，47 项）、`new_sources/t2_board.{c,h}`、`build/`（host 产物）、base/modified 隔离副本。
- `docs/handoffs/parallel-20261104/T2-status.md`（本文件）、`docs/evidence/parallel-20261104/T2/`（仅此处证据）。

未修改 T2 归属之外的任何文件；工作区其他会话未提交修改原样保留。

## Public 接口（T1/T3 消费）

- 线缆帧：原帧 `AA 55 func len payload crc8`；function `0x0C`（原枚举 0..11 全占用，旧固件对此无响应=不支持）；CRC-8 Dallas/Maxim poly 0x8c、初值 0，`crc8("123456789")=a1`；小端，float32 IEEE-754。
- 能力查询 `01` → 回复 type `81`：`<BB16s16sI`（版本、128-bit boot_id、16 字节 build_id、能力位 0/1/2 = 快照/超时锁存/epoch 门控）。查询无事务 ID，仅独占 UART + 有界读使用。
- 命令 type `02`：`<BB16sQIHB` + count×`<Bf`（action 1=ENABLE/2=UPDATE/3=STOP；四轮 id 0..3 唯一；TTL 1..500 ms；NaN/Inf/重复/缺位拒绝；STOP 需 count=0 且严格更新 seq；ENABLE 需停止/锁存态 + 更大 epoch + 零目标）。超时与 STOP 后必须「更大 epoch + 零速度 ENABLE」重新使能。
- 周期快照 type `82`：`<B16sIII` + 4×`<qff`（93 字节 payload / 98 字节整帧）：boot_id、board_tick_ms(uint32 可回绕)、样本 seq、faults（bit0=超时 bit1=停止），四轮 id 顺序 0..3 的 int64 累计计数、float32 实测 RPS、float32 实际生效目标 RPS。
- TTL 语义：MCU **收到时刻的剩余时长**，跨机转发只传剩余有效期；到期独立于流量周期性置零+锁存 TIMEOUT；被拒帧不刷新 deadline。
- 旧版/不支持判定：未知 function 无响应、超时、错帧 = 不支持/未知，不得推断支持；`valid` 由接收端基于能力+boot+新鲜度+有限性判定，不能由缺数据补零。映射共享契约 `wheel feedback` 字段：`counts/measured_rps/target_rps/board_tick/boot_id/seq/faults` 直接对应，`valid` 不上板、由消费者判定，`wheel_ids` 固定 0..3。

## 接口变更请求 / 待总协调裁决

1. **function `0x0C` 分配**：原枚举 0..11 全部占用，本提案将其用于 T2 能力协商。旧固件不响应该值，无兼容性冲突，但需总协调确认后 T1/T3 才可将 `0x0C` 作为探测值。能力查询处理在 0003 中接线，原版固件不支持。
2. **板端无来源认证**：候选包不携带也不验证逻辑 `source`/`session_id`，串口本身不鉴权，**不能宣称板端拒绝错误来源**。T1/T3 必须在唯一串口 owner 之前执行会话/来源门禁；错误 boot/epoch/seq 已在候选 C 中拒绝，但不等于来源认证。请总协调决定是否需要板端携带来源标识。
3. **boot_id 跨掉电唯一性、产生失败后的禁用行为**尚无已验证板端实现；查询无 transaction ID，T1 必须有界接收、独占 owner、处理迟到包；写串口成功不是 controller ACK。
4. 快照 98 字节布局如上；无其他公共字段变更请求。

## 测试命令与结果（2026-11-04 实测）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 原生编译+host harness | `bash tools/ros2_collection/vendor_patches/firmware/t2_host_build.sh`（conda gcc 16.2.0，`-std=c99 -Wall -Wextra -Werror -pedantic -O1`） | exit 0，**76/76 PASS** |
| golden 一致性 | 编译 C 序列化 vs `golden_vectors.json` | **13/13 字节一致**，DIFF 0 |
| 协议契约测试 | `work/ros2_collection/envs/ros_jazzy/bin/python -m pytest tools/ros2_collection/tests/test_t2_firmware_protocol.py -v` | **10 passed + 25 subtests** |
| 补丁应用/回退/错基线 | `/usr/bin/python3 work/ros2_collection/firmware-development/verify_patches.py` | **47/47 PASS**（0001 与既有补丁字节一致；0003 错基线 dry-run 拒绝） |

## 证据（仅 docs/evidence/parallel-20261104/T2/）

`t2_host_report.json`、`t2_golden_consistency.txt`、`t2_pytest_output.txt`、`t2_patch_verify.json`、`t2_toolchain_probe.txt`（ARM 工具链缺失记录）、`MANIFEST.json`、`baseline.sha256`、`t2_deliverable_sha256.txt`、`verification.txt`（旧版，被上述取代）。基线与源码清单见 `tools/ros2_collection/vendor_patches/firmware/README.md`；`baseline-source.sha256` 1159 条全部通过校验，参考 HEAD `a72a60d331c614b32ef438b6fb89ac86be52c512` 仅为定位信息，参考树原本大量 dirty。

## 构建缺口与未验证硬件行为

- Keil 工程（STM32F407VETx / ARMCC 5.06u6 / STM32F4xx_DFP 2.16.0）所需交叉工具链本机不存在（`arm-none-eabi-*`、`armcc`、`cc`/`gcc` 均缺，仅项目隔离 conda gcc 可做原生构建）；未安装/替换任何全局工具链。参考树既有 `.hex` 非本次产物。
- **静态代码风险（待集成验证）**：参考实现 `app.c` 启动即调用 `set_chassis_type()`，而 `motors[]` 直到首个 MOTOR 回调才分配，存在空指针风险；旧 MOTOR 路径目前能绕过候选状态机（0003 接管后需在集成构建中确认封堵）。IWDG 不等于通信超时。
- 未在目标板上验证：ISR 时序与串口调度延迟、RX 排队时间与 TTL 的相互影响、真实编码器计数/符号/轮序、pending overflow 与 64 位累加极限、停车与复位行为、boot_id 唯一性、能力位真实置位、运动限幅接入。host harness 为逻辑验证，不等价于实机验收。
- 补丁集针对记录的精确源码哈希生成；应用前必须重跑哈希校验；禁止把补丁当作可烧录固件。

## 最小下一步

1. 总协调裁决上文接口变更请求（`0x0C` 分配、来源标识、快照布局确认）。
2. 提供含 ARM 工具链的环境后执行 `0001→0002→0003` 补丁链的可复现交叉构建（先 `verify_patches.py`，再编译；禁止直接烧录），并在集成构建中确认 `set_chassis_type()` 时序与旧 MOTOR 路径封堵。
3. 实机验收按 PROTOCOL.md 剩余 gate：能力协商、TTL 过期实停、快照计数跨回绕连续性、急停抢占；需已授权现场窗口、已校验基线镜像与回退方案。
