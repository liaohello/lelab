# 下一轮：帧模式安全状态、急停恢复与发布门禁

日期：2026-11-06，America/Los_Angeles。
本文件是基于交付证据和当前源码的下一轮计划，本次未运行硬件、未复跑 P0–P4，也未修改控制代码。

## 1. 对当前成果的判断

基础闭环已经具备：生产 server + Firefox 两集 observe/grasp、真实 sqlite3、96 帧受限 6D 导出、SDK/数据页逐轴读回，以及帧模式 loopback 约 9.8Hz；来源见 `docs/evidence/landerpi-facade-p0-p4-offline-closeout-2026-11-05/README.md` 和各阶段 README。本次只读核对这些记录，相关测试数属于上一轮实测。

下一轮无需再重做一整套 P0–P4。应将“软件急停车端闭锁、过期许可状态、网页恢复、新旧连接隔离”作为一组未完成的安全/可用性问题闭合，再更新候选包。现有候选包是快照，不因解包/导入校验通过就具备发布资格。

以下属于源码线索，必须先做最小复现再作为根因交付：

- `agent.py::_control_tick()` 在没有 base_pub/arm_pub 时直接 return，租约到期清理位于 return 之后。帧模式刻意不创建这些旧 publisher，因此到期清理可能没有执行，符合许可标记滞留现象。
- `FrameStreamHardwareFacade.emergency_stop()` 先 abort 帧连接，随后父类走 `car.stop()`；普通 stop 与 agent 的 `estop()` 是两套语义，不能假定关闭流就会设置车端急停闩锁。
- `HybridSession.set_arm_state(clear_estop)` 清了 `estop_latched`，但没有同时清 `_estop_requested`；`_refresh()` 又优先根据后者回到 estop，符合网页清除后仍停在 estop 的现象。
- agent 普通 acquire 拒绝 estop_latched，而 clear_estop 要求有效 token；旧 token 到期时，恢复流程可能两端都被拒绝。
- `car_control_process` 在 hello 时匹配许可身份，但每帧准入和实际发布也需独立检查当前 mirror 身份。不能只靠 hello 和 TTL 证明重新绑定后的旧连接不再有效。

## 2. 本轮建议直接采用的语义

### 显式急停

应落实为车端软件闭锁，覆盖 agent 的授权状态和独立控制进程的执行闭锁。上位机本地立即停产/清队列，同时以优先停止路径通知车端；agent 是许可权威。网络中断时只能报告停止待确认/无法确认，不能把本地断流写为“车端已经急停”。

必须区分：本地请求、agent 已闭锁/撤销许可、控制进程已拒收/停发/hold 已发布、机械实际停止。软件闭锁不能称为硬件急停回路。急停不等待录包、导出、join 或普通请求的长锁。

### 租约到期与普通暂停

租约到期要撤销车端许可、清队列和旧身份、使实际输出不可再启用，并进入 fault/重新对齐流程；不必把每次租约超时都标成 operator estop。普通暂停关闭跟随，重新接合需新的参考；不得因 renew/reconnect 自动恢复。

`frame_outputs_enabled` 等字段若表示请求态，应明确命名/标注；若表示有效许可态，则必须含租约有效、未闭锁等条件。控制进程执行态单列并带年龄；缺少新鲜确认时显示 unknown，不能用 requested=false 伪造 driver stopped。状态 GET 不承担唯一的过期清理职责。

### 恢复

正常路径：显式清除车端软件闭锁 → 确认车端已清除且无输出 → 清空上位机旧参考/队列/worker → 释放旧会话 → 获取新会话 → 预览 → 对齐 → arm → 显式接合。

清除只授予恢复能力，不授予运动。旧 token 有效时可复用现有所有者权限；token 已失效时，必须通过现有控制权管理取得限定为清除/释放用途的恢复授权，不能新增无鉴权 clear 后门，也不能靠删除急停文件、强制改标记或重启绕过。具体字段/接口由实现前的鉴权审查决定，保持改动最小。agent/控制进程重启也必须从无输出状态开始。

## 3. 实施顺序和验收条件

| 阶段 | 主要工作 | 最小验收 |
| --- | --- | --- |
| S0 冻结与红灯复现 | 保存相关哈希、读取真实状态；复现急停未落闩锁、租约标记滞留和网页恢复失败 | 每个问题有原始请求/响应、状态变化和失败判据；不重复无关全量测试 |
| S1 车端权威一致性 | 急停两层软件闭锁；将租约生命周期清理从旧输出 publisher 条件中解耦；统一状态语义 | 帧/legacy 都能显式急停；lease 到期无旧身份/许可；断上位机后车端仍闭锁/停发；停止不依赖查询/status |
| S2 网页恢复 | 修状态标志、车端 clear 回执及失效 token 恢复流程，给 UI 明确下一步 | 有效 token、已过期 token、释放后清除三条分支均实际点击；至少连续2次 stop/clear；清除/恢复期间不发布新运动目标，最终回到可预览的新会话；显式接合前始终无输出 |
| S3 在线旧连接隔离 | 在准入和真正发出目标前核对当前许可身份/代际；完成真实 socket 测试 | 旧 socket/在途帧/迟到 worker/旧回执不影响新会话；拒绝不消耗新序号、不改变目标/队列/发布计数；新连接显式接合后正常工作 |
| S4 有界回归与候选包 | 对受影响测试/网页流程复验、冻结新 manifest，更新运行手册及候选包 | 新包逐文件哈希/导入闭合正确，源码与 dist 一致，完整停止/恢复说明与明确剩余门禁 |

S1–S3 顺序推进；S0 的源码/证据查阅可并行。主智能体统一修改，共用文件不并行覆盖。

### S1 的额外边界

- agent 的租约清理不能依赖旧伺服 publisher、相机、录制、HTTP/status 被调用。
- agent / licence mirror / control process 三层身份与许可一致；保留单调时钟TTL，不把跨进程/跨主机时间戳当可直接比较。
- 任何一层失去可信状态，要求显式重新接合；上位机或agent恢复不能重新播放旧队列。
- 保留 source/session/epoch/bundle/seq/时效门禁，帧 TTL≤200ms、反馈≤500ms；不得扩大限位/时效让测试通过。

### S2 的额外边界

- 不要求清除急停后直接 following。页面可停在 disconnected/待新会话/preview，但必须给可完成的操作路径。
- 缺少车端清除确认显示 pending/failed，保留错误原因；不只清前端 pill 或本地标记。
- 录制与导出收尾可继续，旧/invalid session 保留；急停时录制锁不能阻止停产与车端撤权。
- 测量本地停产、车端停收/停发和确认时间，沿用前轮相同环境检查是否显著劣化；0.135s 是前轮单次实测，不作为本轮或真机承诺。

### S3 的额外边界

先核对 StreamServer 是否只允许一个活动控制连接。不要为让第二路 hello 成功而放开串口或输出独占。可在第一连接仍开着时撤销/更换 agent epoch，再发送有界旧帧，验证立即拒绝；随后显式关闭旧连接并建立新连接。如果第二连接不被支持，应快速明确拒绝而非无限等待。

必须覆盖许可身份变化但 token 仍有效，以及旧帧在队列中准入后、发出前发生许可变化。只验证 reconnect 后旧 stream_epoch 无效，不能替代这两项。若确实不能构造在线场景，交付精确阻塞条件，保留未完成标记。

## 4. 独立构建支线与真机界线

ARM64/固件工具链可与 S1–S3 独立推进，但不让工具链安装阻塞安全语义修复。

- ARM64：优先使用隔离的用户态 QEMU/容器环境或可用的原生构建机；禁止未经说明修改宿主全局 binfmt/特权配置。区分 Python语法校验、消息包交叉构建、ARM64真实执行；保留平台 digest 和日志。
- FreeRTOS：先明确当前仅机械臂+夹爪候选发布是否实际依赖新固件。需要时按厂商 Keil 工程使用匹配且合法可用的工具链；无 ARMCC 应列具体缺口，不把移植 GCC 当作顺手修复，也不以 host harness 冒充固件二进制。无固件产物不必阻塞不依赖它的 Python/ROS候选包，但相关能力仍不得宣布可验收。
- Pi 的 --apply/--stop/--rollback、原生 ARM64部署、真实串口/回读/保持/急停属于后续实机窗口；本轮只做本地可验证的渲染、打包和 dry-run，不能把未连 Pi 当作软件测试通过。

## 5. 回归与交付要求

用户要求优先速度：复用现有 fixture、故障矩阵、web_closure 和性能工具。新增测试只覆盖确实发现的行为缺口。先跑受影响单元/契约和一个隔离生产栈，再跑真实浏览器恢复流程；有失败才扩大。已稳定的两集导出仅做一次相关回归，前端有改动才重建/类型检查。不为增加数字重做变异测试、整套长基准或全量训练相关测试。

交付新增证据目录，记录版本、真实命令、退出码、首次失败与修复复验；既有README是历史证据，不覆盖。状态按“本轮通过 / 已实现未验证 / 明确受阻 / 真机待验”列出，不能使用“有未完成记录所以已经完成”替代验收。

最终应有：急停/普通暂停/lease失效/clear/recover 的状态表；两端身份与代际说明；网页恢复截图和接口时序；旧连接在线拒绝证据；受影响测试；候选包SHA与源文件manifest；启动/停止/回退说明。仍未实现的工具链/真机项单列。

## 6. Agent Goal Prompt

```text
在 /home/liaozhengyan/Desktop/lelab 完成下一轮仅离线开发：关闭 FrameLink 模式的安全状态、急停恢复与旧连接隔离缺口，更新可审查候选包。

先读 AGENTS.md、docs/handoffs/2026-11-06-frame-safety-recovery-plan-and-agent-goal.md、docs/evidence/landerpi-facade-p0-p4-offline-closeout-2026-11-05/README.md，以及P1/P2/P4各自README和runbook第10节。先核验实际源码/依赖/进程/端口，历史测试数不算本轮实测。HEAD历史为e4312f0，大量生产模块未跟踪；保留所有未提交修改和旧/invalid sessions，不提交推送、不切current。

已知基础：生产server+Firefox两集96帧、受限模拟6D导出、SDK/数据页逐轴读回和约9.8Hz loopback帧链路已验；普通/export仍拒绝，数据始终simulated_offline/training_eligible=false。上一轮候选包为landerpi-frame-20261106T122111Z，改源码后必须重建。

本轮按计划S0→S4推进：
1. 最小复现三个红灯：帧模式急停没有车端estop_latched、lease到期agent许可标记仍true而执行进程false、网页clear/release/reacquire恢复失败。
2. 显式急停必须落到agent与独立控制进程的软件闭锁；本地立即停产清队列，车端撤权/拒收停发不等待录包。分开报告request/agent-confirmed/process-stopped/hold，缺确认就pending/failed；不声称物理急停。
3. 租约过期清理独立于legacy publisher是否存在；撤销有效许可、清队列/旧身份，输出请求态与有效许可/driver状态分开并带年龄；普通pause或lease fault不自动冒充operator estop。
4. 修复网页恢复：核对_estop_requested与estop_latched、car clear回执、过期token与acquire禁用的死路。清除只允许恢复操作，不授予运动。通过现有控制权管理处理有效/过期/已释放token；禁止无鉴权clear、直接改状态文件或重启绕过。恢复必须新会话、preview、align、arm、显式engage；连续两次急停恢复实际点击并留证，期间无自动输出。
5. 完成在线旧socket/迟到帧隔离：在每帧准入和真正发布前验证当前许可session/epoch/bundle/代际，不只hello检查。保持单活动控制连接；先撤销/更换epoch再从旧连接发旧帧，拒绝不得消耗新seq/修改队列/发目标。再关旧连接、建立新连接并显式接合。第二路hello如果协议不支持，应明确有界拒绝，不为测试放宽独占。覆盖已准入但未发出时许可切换。
6. 对相关控制/录制/stream/lease测试与真实浏览器恢复流程做定向回归，保留两集模拟6D、普通导出拒绝与数据身份。无需重跑整套P0–P4和无关全量测试；只有变更/失败/实际风险才扩大检查。
7. 可独立尝试隔离ARM64/固件构建，不操作真实设备、不烧录。区分语法/host harness/ARM产物/真实执行；无合法匹配ARMCC就给精确缺口，不擅自移植固件或改宿主全局binfmt。先确认臂+夹爪发布是否确实依赖固件增量，分别列门禁。
8. 更新版本化包、哈希/导入闭合、源文件manifest、状态表和启动停止回退手册；Pi apply/stop/rollback保持真机待验。

先验证的源码线索（不是预先认定的根因）：agent._control_tick在无base_pub/arm_pub时提前return；FrameStream emergency_stop只abort后走普通stop；HybridSession.clear_estop未同步清_estop_requested；agent.acquire拒绝estop而clear要求有效token；car_control_process的旧连接身份需在准入和发出两处重新核对。

主智能体统一修改和验证；可按AGENTS用最多3个default只读子agent做独立检索，不修改文件、不运行写入测试、不操作设备、不派生子agent，避免并行覆盖。只管理标签和/proc身份明确属于本任务的进程，禁止泛用pkill。保持TTL≤200ms、反馈≤500ms、限位/标定/双租约/单串口owner，不放宽保护让测试变绿。不训练、不推理、不连真实车/主臂/相机/串口。

完成交付必须区分本轮通过、已实现未验证、明确受阻、真机待验。提供急停/lease/恢复状态表、网页真实点击与截图、旧连接在线拒绝证据、关键时序、相关测试和新候选包。若某项仍失败，保留原始错误并列具体最小下一步，不能把“已留失败证据”称为该项已完成。结束时撤销许可、释放reader和租约、停本任务栈/服务、保留数据和旧证据。
```
