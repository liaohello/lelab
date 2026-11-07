# 再次反馈过期：已确认车端先失效

日期：2026-11-07，America/Los_Angeles。本轮仅只读排查，不重新接合，不发送执行器命令。

## 两次故障瞬间的证据

| 项 | fault-second.json | fault-third.json |
| --- | ---: | ---: |
| 六轴扫描序号 | 13263 | 14962 |
| 车端原始 stale 年龄 | 0.504641158 s | 0.858809095 s |
| 上位投影年龄 | 0.561781568 s | 0.870619977 s |
| 已成功扫描的跨度 | 0.024943 s | 0.022324 s |
| 上位缓存年龄 | 0.107 s | 0.016 s |
| 最近 poll 耗时 | 0.060 s | 0.015 s |

两次 owner 都是 1065580317、generation=0。六轴各自 error 都包含
`sample_invalid,sample_stale,feedback_stale:<车端年龄>`。因此原始车端快照本身就已过期，
第二次并非仅由上位缓存过久或显示错误造成。

仍不能用这些数据区分“板卡 owner 没产生新扫描”与“DDS/agent 回调未及时交付扫描”。
不能用当前最新反馈恢复 fresh 推翻故障瞬间的旧数据，也不能把本次证据倒推为上次故障
的确定根因。

## 运行版本与日志（SSH 只读核查）

当前唯一板卡 owner PID 398，命令 `python3 -m ros_robot_controller.ros_robot_controller_node`。
其 PYTHONPATH 首位指向版本化目录：

```text
/home/ubuntu/shared/lelab_car/vendor/ros_robot_controller_t1_20261107/
```

本次读取的文件 SHA256：

```text
node acd1aa8ea482bc068b5f802b498e0040232507f973a5e95b6b2bd7b96d749828
sdk  cdc04f097beaf4cfaa0ebc739dd21aec476f0b75709f7aa868db07ddb8747b2b
```

SDK 是有界等待修复版，读取 `queue.get(timeout=remaining)`，不再是参考树的无限等待。
node 哈希与历史 vendor v3 的 8da2f4b7… 不同；此次实际 node 含
`recover_bus_servo_reads(quiet_s=0.2)`，不应只凭目录名宣称等于旧包。

agent/control 使用 `landerpi-arm-follow-v22-20261107` 的候选代码。RMW 为
rmw_cyclonedds_cpp，ROS_LOCALHOST_ONLY=1，ROS_DOMAIN_ID=0；本轮订阅探针使用相同环境。

owner 日志 `/home/ubuntu/lelab_car/car_arm_stack_20261107/owner-live.log` 记录过：

```text
measured readback failed at servo 10; failures=1
measured readback failed at servo 5; failures=25
measured readback failed at servo 10; failures=50
measured readback failed at servo 5; failures=75
```

这些是该 owner 生命周期内已有告警，不是全部发生在本次故障时刻。日志仅首个和每
25 次失败打印，当前不能用日志将单次 transaction 与故障精确对齐。

## 已确认的恢复调度特征

当前 node 的 `servo_readback_loop`（运行文件第 322 行）每轮开始先
`stop.wait(0.2)`，逐 ID 读取后只有完整成功才发布和递增 seq。任一读失败则增加内部
计数，调用恢复函数，再进入下一轮。

`recover_bus_servo_reads`（运行文件第 353 行）持读锁静默等待 0.2s；下一轮循环又等
0.2s。加上读取超时（SDK 默认 0.1s）、已执行的查询及后续完整扫描，一次读错误就可能
让成功扫描间隔越过 0.5s。此时保护停机符合现有契约。

这个机制解释了“偶发读失败为何容易变成整个跟随故障”，但未证实本次失败的原始诱因
是串口超时、错帧、锁竞争、DDS 或 executor 延迟。源 SDK 将多类失败压成 None，当前
缺少逐事务原因和最后成功/恢复时间线，不能进一步归因 STM32。

## 补充只读订阅探针

产物：`readonly-topic-car-25s.json`。

- 在 Pi 容器中建立仅订阅的临时 ROS 节点，监听实际 readback topic；未打开串口。
- 连续 25s 收到 113 帧；最大到达间隔 0.231761s，没有超过 0.4s 的间隔。
- 同期读取 agent 本地 HTTP 100 次，最大反馈年龄 0.262028s、无效 0 次。
- 这段采样期间已有其他操作端接合跟随，执行器发布计数 392→516；本排查未接合或发送
  运动、torque、清急停命令。不能把该探针称为“始终无输出环境”。
- 探针结束后再次捕获 fault-third；当前回包不能替代故障时同步 topic 记录。
- 最后检查上位 fault、follow active=false、上位与 emitter outputs=false，反馈已恢复。

## 应改哪里

优先处理 Python/ROS owner 的采样周期、完整事务仲裁和读失败诊断。周期应按 deadline
调度，恢复保留迟到回包隔离要求；所有等待都计入总体预算。若考虑提高扫描频率，先做
受控只读吞吐/扫描跨度测试，不能直接盲调。

新增每次失败的 ID、命令、原因、锁等待、耗时、恢复阶段及成功扫描间隔/序号，向上报
健康状态；区分 owner 采集停顿与 agent DDS 消费延迟。agent 的 failed_scans 统计无效
收到帧，不统计 owner 没发布的失败轮次，所以它为 0 不代表底层零失败。

保持原始 0.5s 新鲜度门禁、TTL、急停和真实状态来源；不把缺帧补成新测量，不缩短无
依据的隔离静默时间，不用自动重接合掩盖恢复。此轮未改部署源码、未重启 owner、未改
STM32 SDK/固件、未提交、未推送、未切 current。
