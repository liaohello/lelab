# LanderPi 厂商底层舵机控制审查

日期：2026-11-06（America/Los_Angeles）。范围为本地参考源码静态审查和离线探针；
没有 SSH、连接串口、改变力矩、发送运动、构建 MCU 镜像或烧录。实际 Pi 导入版本和
板上固件不由本次审查确认，不能将这里的参考源码直接等同运行版本。

## 一、业务落在哪里

```text
LeLab 受控命令 / ROS SetBusServoState、ServosPosition
  → lander_pi/.../ros_robot_controller_node.py
  → ros_robot_controller_sdk.py
  → Pi→MCU: USART1 1 Mbps，AA 55 | function=0x05 | len | payload | CRC8
  → Hiwonder/Portings/packet_porting.c: DMA/FIFO/packet_rx_task
  → Hiwonder/Misc/packet.c: packet_recv，CRC通过后同步调用业务 handler
  → Hiwonder/System/packet_handle.c: packet_serial_servo_handle
  → Hiwonder/Peripherals/serial_servo.c: 组装智能舵机协议
  → Hiwonder/Portings/serial_servo_porting.c: serial_write_and_read
  → Core/Src/stm32f4xx_it.c: UART5 TXE/TC/RXNE
  → UART5 115200 半双工 → 各智能舵机内部位置/动力控制
```

回读经 UART5→同一事务信号量→packet_handle 生成上报→USART1 DMA→SDK queue→ROS。
STM32 这里主要转发智能舵机位置和动力命令；底盘电机的 TIM7/PID 不控制机械臂舵机
的位置闭环。不要通过改底盘 PID 解决 joint1 回漂。

| 业务 | MCU 外层 BUS_SERVO 子命令 | 主要文件/行 |
|---|---|---|
| 多轴位置和时长 | 0x01，duration_ms、count、ID/position 列表 | `packet_handle.c:196`、`serial_servo.c:59` |
| 停止 | 0x03，count、ID 列表 | `packet_handle.c:203`、`serial_servo.c:84` |
| 位置读回 | 0x05，单 ID | `packet_handle.c:210`、`serial_servo.c:72` |
| 电压读回 | 0x07，单 ID | `packet_handle.c:218`、`serial_servo.c:235` |
| 卸载动力 | 0x0B，单 ID | `packet_handle.c:234` |
| 加载动力 | 0x0C，单 ID | `packet_handle.c:239` |
| 动力状态读回 | 0x0D，单 ID | `packet_handle.c:244`、`serial_servo.c:193` |

上述文件均在 `/home/liaozhengyan/Desktop/resource/RosRobotControllerLite_ros_250811/`；
`packet_handle.c` 在 `Hiwonder/System/`、`serial_servo.c` 在 `Hiwonder/Peripherals/`。
外层 function=0x05 与子命令 0x05，以及外层扩展功能号 0x0C 与力矩子命令 0x0C
是不同命名空间，修改协议时不要混淆。

## 二、最优先的新发现：torque 布尔语义反了

参考 `lander_pi/src/driver/ros_robot_controller/ros_robot_controller/ros_robot_controller_sdk.py`
第 427–433 行：`enable=True` 编成 `[0x0B,id]`，False 编成 `[0x0C,id]`。
node 第 255–257 行直接透传 `i.enable_torque[1]`。
而参考 MCU 第 234–241 行：0x0B→`load_unload(...,0)`；0x0C→`load_unload(...,1)`。
`serial_servo.c:121` 将 load 原样写进智能舵机命令 31 的参数。

因此这两份参考代码组合有确定的启停语义反转。离线提取并执行原 SDK 方法 AST，
不导入或构造 Board，得到 True→`0b 01`→unload，False→`0c 01`→load。
证据 `docs/evidence/landerpi-firmware-servo-audit-2026-11-06/sdk-torque-result.json`。

建议固定一个公开含义：`enable=True` 必须是加载，SDK 真→0x0C，假→0x0B；node
不再暗中反转。先核对实际 import 路径、源码哈希和已部署固件，避免对已经做过反转
适配的版本再反转一次。最终用单 ID 写入后的真实 torque 读回确认。历史“ID1=1、
其余=0”不足以单独确认当前线上同样有此缺陷。

## 三、“一条 ROS 消息六轴，仅第一轴生效”应如何定位

参考 node 的 `set_bus_servo_state`（231–267 行）确实遍历全部 `msg.state`。
**位置**收集成一次 `bus_servo_set_position`，SDK:470–475 写入 count 和每项 `<BH>`，
MCU:198–199 遍历 count；这是支持多轴的路径，不是首项专用。
**力矩**则在循环中每 ID 调一次 SDK，每次是独立 AA55 帧，并非固件的批量力矩接口。
SDK:433 本来每条就 sleep 20 ms。因此单 ID 120 ms 可以作为定位期的保守节拍，
不能据此宣布根因已修复，尤其不能将其加入 10 Hz 控制循环。

依次检查：

1. ROS 每项 `present_id`/`enable_torque` 是否符合 `[启用字段标志,值]` 的数组契约。
   node 没有预验证长度，首项已发送后第二项索引异常可能留下部分写入；“第一条”现象
   需要排查这一分支，不断言已发生。
2. 运行 SDK 实际发出的六条子命令、ID 和 polarity；只记录限速摘要，不逐帧 print。
3. MCU 是否接收六条、每次 UART5 TX 是否完成；现有写函数返回 void、丢弃底层
   `serial_write_and_read` 错误（`serial_servo.c:121–127`），上层看不到失败。
4. 对 ID 1/2/3/4/5/10 分别读回加载状态，不以 ROS publish 作为整组成功。查其他
   控制入口是否随后卸载、重启或覆盖。设置部分失败时不可按统一“已加载”继续运动。

`torque_state=1` 是加载状态位，不是力矩传感器，也不证明供电足够、实际输出力矩
充足或没有机械阻力。六轴状态确认后，仍需 joint1 单轴小步位置、电压、温度、限位
和实际进度的有界检查。

## 四、固件回读匹配缺失，比上位机 queue 修复更深一层

- `serial_servo.h:117–164` 接收状态机校验帧头/长度/校验和，却没有期望 servo ID、
  命令或参数数目字段。
- `stm32f4xx_it.c:381–384` 任何返回 0 的有效帧都会释放事务信号量。
- `serial_servo.c:72–80` 直接把 rx args 当 position；没有确认该帧是刚请求的舵机和命令。
- `packet_handle.c:185–189,210–215` 上报 ID 和 sub-command 来自请求，再填入上述数据。
  因此 MCU 有可能把错误来源的数据标成预期 ID；上位机 bounded-read 的 ID/命令匹配
  无法识别 MCU 已误标的数据。

改进：在唯一 UART5 事务 owner 中记录 expected_id、expected_cmd、expected_argc、
本地代际、phase 和 deadline；只接受匹配的帧。每次开始要清除前一完成状态；TX
完成与 READ 完成区分，不允许无事务帧释放本事务。超时进入隔离/清收状态，统计并
丢弃无主、错 ID/命令、坏校验与过期帧。

舵机协议没有事务 ID：即便增加 MCU↔Pi request_seq，仍无法从协议上区分同一
ID/命令的旧舵机回包与新回包。新 seq 只能改善外层相关性；不得宣称解决了内层歧义。
超时后需在可证明的最大回包延迟条件下隔离/恢复，缺乏这个边界则保持故障而非立即
重试相同查询并标为可信。

## 五、固件其他确定缺陷与条件性风险

### 接收状态机边界（已离线编译实际头文件复现）

`serial_servo.h:135–147` 只拒绝 length>7，没有拒绝 length<3；判断无参数用的是
`length==6`，应为协议所定义的 `length==3`。length6 实际表示三个参数。
`150–153` 写 `args[rx_args_index++]` 前没有容量检查。

本次 GCC16.2 原生执行原头文件：正常 torque/position 和四参数帧可完成，零参数与
三参数帧不能正确完成；length2 可使索引耗尽 args[8] 仍未拒绝。探针刻意在下一次越界
写前停止，没有触发未定义写入。接受其他 ID/命令有效帧也已复现。
这些错误是确定的解析器缺陷；零/三参数场景不是现有位置/torque 回包的直接解释，
不要将其写成 joint1 回漂根因。

### 超时、串行化和 ISR

`serial_servo_porting.c:12–43` 用共享 tx/rx 状态与一个信号量；`47–52` 配置等待
5 RTOS tick；`FreeRTOSConfig.h:68` 是 1kHz，因此约5ms。MCU 这一层是有界等待，
无限等待位于旧 Python SDK queue。参考活跃调用集中在单个 packet_rx task，当前
本来就有串行效果；代码没有独立互斥/忙状态，未来加后台扫描或第二任务调用会破坏
共享事务，不能声称当前已复现多任务冲突。

UART5 ISR:388–406 在 TC 切向/完成，TXE 逐字节发，115200 8N1；每个位置包10字节
线速时间约0.87ms。六轴写入至少约5.2ms；应测 turn-around 和尾延迟而不是任意增大
超时。不要仅凭5Hz扫描跨度18–20ms推断反馈“必定无错配”。

### PC/Pi DMA/FIFO 与错误传播

- `packet_porting.c:170–172` 在 UART TX 完成中断回调直接 `lwmem_free`。此工程
  `Third_Party/Lw/lwmem_opts.h:44` 开启 OS 互斥；`lwmem_sys_cmsis_os.c:54–55`
  调用 `osMutexAcquire(...,osWaitForever)`，但本地 CMSIS实现
  `Middlewares/Third_Party/FreeRTOS/Source/CMSIS_RTOS_V2/cmsis_os2.c:1397–1398`
  在 ISR 中返回 `osErrorISR`。`lwmem.c:978–982` 不检查加锁结果仍执行 `prv_free`。
  这是确定的中断上下文/堆互斥使用缺陷，并发下可能损坏堆；没有实机复现堆损坏。
  建议中断只投递完成事件，帧回收与队列消费由 TX task完成，或改静态帧池和适合 ISR
  的有界队列；不能只在错误返回处增加无限等待。
- `packet_porting.c:86–93` 忽略 `lwrb_write` 的实际写入数及 DMA restart 返回值，
  FIFO 溢出/启动失败时可能静默丢数据。补计数、明确重置和错误状态。
- `packet_porting.c:151–178` 未检查 `HAL_UART_Transmit_DMA` 返回值；启动失败且
  无完成回调时可能耗尽 tx idle 链路。应处理 busy/error 并回收帧、恢复发送状态。
- `packet.c:107–112` 发送入队失败未释放已分配帧，可能泄漏；应明确所有权与队列
  满策略。不要在 ISR 内引入新的阻塞重试。
- `packet_handle.c:192–325` 缺少每个子命令 payload 长度和 count 的检查，错误但
  CRC 正确的数据会读取超出声明 payload 的内容；增加逐子命令的严格长度验证。
- torque/ID 读失败时局部变量未初始化（244–261），失败上报也会携带不确定值。
  所有上报零初始化并明确成功/失败，失败值不得变为可信状态。
- `serial_servo.c:62` 只钳制大于1000，缺下界/ID/duration验证。建议入口拒绝非法值，
  不静默修正。位于合法数值区间也不等同实际机械范围安全。

### watchdog 范围

`Hiwonder/System/app.c:124–126` 持续喂 IWDG，没有基于舵机命令新鲜度的停止逻辑。
IWDG 能覆盖某些 MCU 故障，不能证明 Pi断联时机械臂停止。若需要板端命令TTL与停止，
应作为独立、版本化安全增量实现，并与仓库既有 T2 协议/功能号分配核对；不能把
Python/ROS watchdog 的测试结果算成板级或机械急停验收。

## 六、推荐落地次序

1. **先不刷固件，修 ROS/SDK 契约**：核实实际版本；修 voltage/torque 方法名；
   校正 torque polarity；预验证全部 msg.state；服务异常返回逐ID错误而不是退出驱动；
   去逐帧 print。保留 bounded read 和单一串口 owner。单ID设置＋读回逐轴验收；
   120ms只作可配置诊断间隔。不要盲目翻转已工作的部署代码。
2. **固件小补丁**：长度边界、ID/命令/参数匹配、事务状态和超时隔离、所有写操作
   返回结果、FIFO/DMA/队列错误传播。保持旧正常帧兼容，不改变已验证录制格式。
3. **版本化协议增强**：逐ID requested/tx_complete/readback_verified 位图、错误码、
   boot/seq/时间戳、超时及错帧计数；若有外层request_seq明确它的边界。回读反馈与
   目标缓存分开；`execution_observed` 只由位置读回变化判定。
4. **再做运动与机械判别**：六轴动力状态均真实确认后，以joint1有界小步检查指令
   发送、位置变化、误差、恢复与保持；无进展则停止，不扩大步长或限位掩盖。下一步
   检查实际供电/压降、舵机模式、限位、联轴/支撑和机械阻力；MCU这里没有可供调节的
   机械臂位置 PID。短目标＋持续刷新时长也应与实际伺服响应一起测，不直接归因网络。

每阶段只做相关离线向量、ARM目标编译及必要实机验证，独立打补丁、固定基线哈希并
可回退。host探针成功只表示缺陷复现，不表示修复、ARM构建或实机保护通过。

## 七、证据与复跑

证据目录：`docs/evidence/landerpi-firmware-servo-audit-2026-11-06/`。

```bash
python3 docs/evidence/landerpi-firmware-servo-audit-2026-11-06/sdk_probe.py
work/ros2_collection/envs/ros_jazzy/bin/x86_64-conda-linux-gnu-gcc \
  -std=c99 -Wall -Wextra -Werror \
  -I /home/liaozhengyan/Desktop/resource/RosRobotControllerLite_ros_250811/Hiwonder/Portings \
  docs/evidence/landerpi-firmware-servo-audit-2026-11-06/parser_probe.c \
  -o work/ros2_collection/firmware-servo-audit-20261106/parser_probe
work/ros2_collection/firmware-servo-audit-20261106/parser_probe
```

本次两条探针执行退出码均为0，表示报告中的参考源码行为已复现。源指纹另见同目录
`source-manifest.json`。原厂源码和部署源码均未修改。
