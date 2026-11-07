# 下一轮实施计划：上位机 Facade 接入 FrameLink 与三链路基准

日期：2026-11-05。范围：本地离线开发与验证；不连接Pi、真实SO101/相机或控制板。
本轮仅制定计划，未修改生产控制代码、未重跑 #9 验收。

## 1. 本轮目标和已知边界

下一轮必须让现有网页/Coordinator/Facade通过FrameLink完成整帧遥操作与两集录制，
不再仅由frame_drive直接驱动控制进程；之后完成有明确测量口径的三链路性能与故障比较。
设备边界使用模拟Reader、模拟从臂和相机，其余经过生产会话/授权/控制/录制实现。

复用用户交接的 #9 成果：生产agent唯一许可权威、独立控制进程、真实sqlite3录制、
受限模拟6D导出、SDK和网页读回。上述成功是上一轮证据，本轮不重复写成本次实测。
保持simulated_offline、training_eligible=false、底盘锁定和6D身份。

本次只读源码核对：

- `app_ros_mobile/lelab/hybrid_hardware.py`的工厂仍绑定CarHttpOutputDriver，worker仍调用
  observer.snapshot后分别dispatch arm/gripper。CarControlLease.valid也存在同步/status请求。
- `app_ros_mobile/lelab/hybrid_frame_link.py`已有FrameLink与CachedCarStatus，可复用；
  FrameLink目前使用attach_lease→engage生命周期，不能只把类实例赋给旧单通道driver就认为接通。
- CachedCarStatus默认0.5s轮询、1s缓存年龄，与原跟随反馈500ms门禁不能直接等同。
  状态缓存新鲜不代表缓存里的关节样本新鲜。
- `tools/ros2_collection/local_ros2/offline/bench_links.py`明确写明没有经过生产HTTP agent；
  sync_legacy是在同一socket用frame+两个ping重放请求数量，只算shape基准。
- 仍有torque断言冲突及上位侧未通过项。接管时记录两侧准确失败用例，不猜测它们相同或无关。

## 2. 实施顺序与文件职责

| 阶段 | 实施内容 | 主要文件 | 通过条件 |
| --- | --- | --- | --- |
| F0 基线 | 冻结相关源码哈希、#9证据路径、当前实例/缴械状态、失败用例 | 本轮证据与status | 能区分已有失败和新增回归，不覆盖历史证据 |
| F1 传输接入 | 显式选择、能力协商、生命周期与双租约回滚 | hybrid_hardware、hybrid_frame_link、必要的facade小接缝 | 同一会话只用一种传输，不静默回退或双发 |
| F2 控制循环 | Reader/反馈缓存/Mapper/发送解耦、异步回执 | hybrid_hardware、hybrid_frame_link及必要的小模块 | 逐周期没有远程HTTP；一份新输入最多一份六轴frame |
| F3 完整会话 | 网页/Coordinator驱动预览、接合、暂停、两集observe/grasp | 离线runner、现有会话/录制接口 | 不绕过生产facade；两集完成sync/export/SDK/真实网页读回 |
| F4 公平基准 | 真实legacy、同步整帧、异步整帧；负载与延迟注入 | bench_links及新增基准适配 | 有原始帧级数据、相同输入、可追溯测量端点 |
| F5 有界优化 | 相位、调度、窗口发放节奏、处理开销 | 测出瓶颈的最少文件 | 频率改善且不放宽动作/反馈/新鲜度安全预算 |
| F6 回归收尾 | 故障矩阵、最小UI指标、部署包/手册更新 | 相关tests、既有前端、专属文档 | 默认硬件配置不变，缴械、数据完整保留 |

F1–F3先完成，再做F4/F5。不要一开始同时重写Facade、Pi控制器和benchmark。
主智能体负责修改与验证；AGENTS允许的default子agent仅做只读核查，避免多会话同时编辑同一模块。

## 3. F1：传输、能力和生命周期

1. 增加显式传输配置，候选名称为`legacy_http`/`frame_stream_v1`（实施前核对现有配置命名）。
   保留当前硬件配置的默认选择；新的离线profile显式开启frame_stream_v1。
   真实硬件默认切换放到后续真机预览/低速验收，不因离线成功就发布新默认。
2. 绑定agent实际宣告的frame协议、stream入口、session/epoch/bundle和许可状态。
   缺能力、版本不符或frame模式不匹配时拒绝，不能退到旧apply后继续显示following。
   session_epoch的int/string规范化沿用 #9 契约，不再做独立且不一致的转换。
3. 复用原Coordinator获取的主臂租约和车端token，将token attach给FrameLink，不二次申请。
   acquire/epoch/hello/enable任一步失败回滚已获取资源，半完成状态也能release。
4. 生命周期逐项接线：acquire→preview→align→arm→engage、release_clutch/pause、
   estop/clear、episode结束、session停止、租约丢失、断连与异常退出。
   连接/hello不授予运动，许可enable不自动发送目标，显式engage后才启动发送。
5. 不把FrameLink套回逐通道submit_intent/dispatch而再次拆帧；定义明确的整帧提交接缝，
   保留上位机许可、标定、反馈门禁，车端再独立复验，不以绕过原router实现“接入”。
6. 停止先禁止新输入、清待发、抢占/撤销车端许可，再等待线程/录制收尾；
   socket发送、HTTP请求和join不能在持有主状态锁时阻塞急停。
   远端未确认停止时显示pending/failed，不能仅因本地线程结束就报告机械已停。
7. 每次重连/换标定/清故障/重获租约更换本地worker代际，销毁旧FrameLink和参考。
   旧线程迟到、旧回执、旧窗口不得更新新会话或发送目标；恢复必须重新对齐和显式接合。

## 4. F2：快照、Mapper和异步语义

### 4.1 控制路径

目标路径：新Leader样本→读取本地反馈缓存→校验绑定和年龄→持久Mapper整帧计算→
FrameLink一次send_snapshot。它不执行HTTP/status/lease.valid、磁盘资产加载、同步ACK等待或六路扫描。

- Reader数据、反馈获取、映射推进、网络发送分别明确所有者。
  当前HardwareObserver的网络查询与映射逻辑需要拆开复用，不能只把snapshot改名而保留网络等待。
- 活跃会话Mapper只由控制worker推进，整帧使用共同dt；网页status/preview读取不可变结果。
  页面轮询频率不得改变滤波、限速状态或导致同一主臂样本重复推进。
- 复用实际Reader的sample_seq；无新样本不生成伪新sample_seq，不把输入缓存重复发算成新目标。
  队列latest-wins、有界，missed tick不补发旧动作、不进行catch-up burst。
- 三层资产在会话边界校验并加载；变更使会话失效并要求重新获取/对齐，不能热替换后继续运动。

### 4.2 两种缓存不能混为一谈

- 会话/租约状态可通过后台HTTP缓存或stream状态更新；每份快照保留请求开始、响应完成、缓存年龄、身份。
  不能只凭“control.active=true”判断本会话仍持有许可，需要对session/epoch/bundle/代际核验。
- 关节反馈的有效年龄至少覆盖源样本年龄、传输不确定性和本地缓存停留时间。
  不把慢HTTP返回的旧样本重新盖为now，也不把CachedCarStatus的1s默认值当成反馈有效期。
- 反馈逐轴年龄、扫描跨度、ID、来源和校准绑定继续检查，保持原500ms门槛或更严格。
  如果现有状态轮询无法支撑门槛，选择明确的后台反馈频率/stream推送方案；不得放宽门槛掩盖断流。
- 反馈/窗口/回执监测应独立于“本周期是否有新输入”，否则主臂停止更新时故障可能无人处理。

### 4.3 异步提交不等于发送成功

分别记录input_sampled、mapped、sender_queued、socket_written、car_admitted、
driver_published、superseded/refused/expired以及hold。不能把send_snapshot返回sent=true当driver_sent。

定义需要暂停/故障的拒绝：身份/标定/许可错误、反馈不可信、传输关闭立即退出跟随；
暂时无新窗口/本地覆盖可以跳过，但必须有“最后有效准入/发送”的有界时效门禁，不能无限显示following。
区分admitted回执、实际发布事件和控制器接受/机械执行；后两项缺证据仍unavailable。

## 5. F3：从网页到数据的回归闭环

1. 离线工厂只替换设备边界Reader与真实设备数据，不把整个生产facade换成fixture。
   构造、acquire和恢复时都不能探测/打开真实串口。
2. 实际点击网页主要按钮，经现有Coordinator/Facade和生产agent获得同一份许可，
   完成preview零输出、对齐、显式接合、相对增量多轴与夹爪、松离合/重新摆放/重新接合。
3. 同一session录两集observe/grasp，仍由生产agent/recorder生成原始sqlite3与阶段上下文。
   复用 #9 的sync/受限模拟6D导出、SDK和DatasetViewer验收，不再新增第二条导出通路。
4. 确认主臂raw/标定值、pre_clip、sent_target的来源真实来自此次Mapper，
   不是测试驱动硬填固定值；逐帧引用sample/frame/episode身份，保留限幅证据。
5. 保持simulated_offline、training_eligible=false；普通export/训练门禁继续拒绝。
   真实浏览器截图、网络/API与曲线值验证单列；DOM/单测不替代浏览器。

## 6. F4：三链路基准的真实含义

当前bench_links.py的frame+ping组合保留为`shape_emulation`，不能用来声称真实HTTP路径性能。
优先比较下表，不为凑三栏新增不必要的生产HTTP控制接口：

| 模式 | 实际执行链路 | 报告名称与限制 |
| --- | --- | --- |
| A | 实际legacy facade + 生产/status及arm/gripper HTTP控制路径 | production_legacy_http；独立epoch/profile，不能在frame活跃时放开旧入口 |
| B | 同一完整帧协议/车端控制核心，每周期同步等待其真实准入回执 | synchronous_whole_frame；若走TCP就注明TCP，不能称“合帧HTTP” |
| C | 生产facade + FrameLink异步整帧、同一车端控制核心 | production_facade_frame_stream；目标优化路径 |

如果A在当前离线profile不能真实运行，报告N/A和原因，另列shape_emulation；
不能用ping替代其HTTP端点后把“真实三链路基准”标为完成。
各模式串行运行，每次重新获取/释放许可、复位统计基线；不同时启用两个输出路径。

### 6.1 公平条件与记录

- 相同模拟输入轨迹/新样本频率、五轴/夹爪映射、反馈与相机负载、CPU配额、限位/速度、TTL/窗口预算。
  轨迹平滑且在合法范围内，不使用阶跃/锯齿复位导致额外限速拒绝污染传输对比。
- 每种正常场景预热后60秒×3；保存原始帧级数据、统计区间和状态快照，报告重复间差异。
  追加HTTP慢、视频负载、反馈延迟，以及本地用户态代理注入网络延迟/抖动/半开/背压；
  不修改宿主全局tc/网络配置来做本地实验。
- 指标：新输入Hz、映射Hz、queued/socket_written/admitted/emitted Hz、按原因拒绝/覆盖/过期、
  每帧端到端本地阶段时间、RTT、实际发布间隔p50/p95/p99/max、反馈年龄、CPU和deadline miss。
- legacy模式一周期两条servo消息与stream一条六轴消息不能直接比消息数；
  按同一新sample的完整六维目标组归一化，并明确legacy不具备整帧原子准入语义。
- 停止/hold消息单列；每帧从准入到emitted/superseded/expired均可解释，不能重复计数。
- 发包时间在实际socket写出点记录，回执到达在接收点记录；
  后续collect/网页轮询的读取延迟单列，不把100ms轮询节奏解释成网络RTT。
- 不直接比较不同主机的monotonic；本地容器基准与未来跨SSH/WiFi结果分开，离线不能证明真机网络性能。

## 7. F5：优化顺序与门禁

先保留现有TTL/窗口/控制频率跑基线，再针对数据证明的瓶颈一次改一个变量：

1. 清除控制路径同步等待、长锁、重复序列化和回执收集延迟。
2. 采用基于绝对deadline的调度，避免sleep漂移和超时后突发补帧。
3. 对齐最新输入发送与新鲜度窗口通知，必要时将窗口发放节奏与10Hz执行tick解耦；
   不通过重复采样、重放目标或扩大安全预算制造频率。
4. 确有依据再调整局部调度频率；动作TTL上限200ms、反馈门槛500ms和已冻结新鲜度上限不放宽。
   任何另需安全预算变化的方案单独列证据和裁决，不能为过验收静默修改。

初始工程目标：10Hz输入配置下，正常负载有效新六轴发布达到9–10Hz，
发布间隔p95<=150ms、p99<=200ms；相同场景三次复验且保持限速/时效门禁。
所有目标都是待验证条件，不预先承诺达标。负载/故障场景应有界降级/停止，不要求强行维持频率。
停止指标分为本地停产、车端停收/停发、hold发布；离线不声称机械停止。

## 8. F6：回归、页面和发布前缺口

- 原#9的accept/record/faults复验，再跑facade/FrameLink/Reader/Mapper/lease/录制相关现有测试。
  前端有改动才做正式构建，并用真实浏览器验证；不为文档更改重跑全部环境。
- 故障覆盖：Reader失去新样本、缓存过期、慢/status、许可撤销、旧epoch回执、发送背压、
  stream半开、worker晚退出、映射切换、重复停止、慢录包、连续两次急停与恢复不自动接合。
- 网页最小增量：transport、采样/准入/发布Hz、反馈有效年龄、拒绝原因、许可/离合，
  数据曲线标签使用已有meta关节名和单位，不能将索引硬编码映射成错误轴名。
- torque失败作为独立安全裁决项：明确teleop/stop绝不以断力矩替代停止；
  若保留校准扭矩能力，应有独占、显式动作、profile和硬件能力边界及行为测试。
  不以删除regex、改函数名或搬移调用位置掩盖冲突；未裁决前不能宣布真机发布门禁全通过。
  上位侧另一失败也要列准确用例/原因，不默认沿用历史“无关”结论。
- 更新包含未跟踪源码的manifest、打包/启动/停止/回退说明，保留旧数据和invalid session。
  只操作本任务实例，不提交/推送/切current。阶段收尾撤销许可、停worker、冻结新目标计数，交接缴械状态。

最终交付：源码差异、显式配置示例、完整facade会话/两集数据证据、三链路真实/shape口径、
优化前后原始报告、测试与真实浏览器证据、失败项和精确复跑/停止命令。
下一阶段才做arm64构建与Pi只读核验，再进入无输出预览和当次授权的逐轴低速联调。
