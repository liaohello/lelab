# 急停原 owner 保留与恢复校验

日期：2026-11-07（America/Los_Angeles）。

## 现场与结果

最初车端 agent estop_latched=true、emitter latched_fault=null、outputs=false。
有两条 operator estop 事件，后一条晚于前一条约 39 秒，且控制租约已失效。
最初经上位 clear-estop API 返回 HTTP 200，但 car_clear=failed/403，不能算解除成功。

排查期间另一条恢复操作已完成清除；清除后一次只读核验：control.active=false、
estop_latched=false、emitter outputs=false、latched_fault=null，上位 disconnected。
本次主审没有重新接合、发送 torque/位置命令或部署重启 agent，不能把其他操作路径
完成的清除宣称为本轮 prior_owner_recovery 实测成功。

交付前再次只读查询时，其他控制端已重新获取并使能：car control.active=true、emitter
outputs=true、上位 ready/outputs=true，急停仍为 false。本轮没有发出这些使能请求，
也没有为取得不同结果而抢占外部操作者。运行状态会变化，应以操作时的新检查为准。

## 确认的源码缺陷与本轮修复

`MobileAgent.estop()` 原来每次将 estop_owner_client 设为 control_client；重复急停发生
在租约过期后时，control_client 已为空，因而抹掉原 owner。现改为只在首次锁存记录
owner 和时间；重复急停继续撤权和发停止公告，但保留首个锁存上下文。

新增 `_estop_owner_from_events()`，为旧实例丢失 owner 的情况，只从当前尚未 clear 的
第一条 estop 之前的本机事件链恢复原 owner。历史截断、越过已 clear 的旧锁存、或
acquire 后已经释放的记录均不作为授权。clear 仍要求 caller client_id 匹配，仍不授予运动。

工作树另一并行实现加入了 `acquire(recovery=True)`。主审将其补上同样的原 owner 校验；
不能让任意 caller 仅凭 recovery=True 取得可清急停的 token。原 owner 不可证明时拒绝，
不新增匿名清除入口。上位临时恢复租约仍只能借现有流程，源身份检查必须在车端成立。

只修改当前 agent 的对应方法与两个定向测试文件；其他未提交修改保留。没有修改或
部署 STM32 SDK/固件，没有修改 ROS 消息或跨机传输协议。

## 本轮验证

```text
app_ros_mobile/.venv/bin/python -m pytest tools/ros2_collection/tests/test_estop_owner_identity.py -q
11 passed（Python 3.12；执行生产方法 AST，不导入/初始化硬件）

work/ros2_collection/envs/ros_jazzy/bin/python -m pytest \
  tools/ros2_collection/tests/test_agent_lease_reaper.py \
  tools/ros2_collection/tests/test_estop_owner_identity.py -q
22 passed, 2 warnings（Jazzy/Python 3.12；包含实际导入的 MobileAgent 方法）
```

覆盖重复停止后 owner/首次时间保持、旧 owner 从事件恢复、陌生/匿名 caller 拒绝、
已释放或已清旧事件拒绝、普通 acquire 在急停时拒绝，以及 recovery acquire 的 owner 校验。
不将这些结果当作 Humble/3.10 或实机恢复验收。

## 未完成

本轮源码修复尚未部署。需要在车端无控制会话、两层 outputs=false 的情况下制作新候选，
做 Humble/3.10 检查并部署，再验证“首次急停→租约失效→重复急停→原 owner 清除”。
错误 owner 必须仍为403，清除后输出必须关闭。不得用 agent 重启使锁存消失来冒充清除。

事件历史本身无法证明原 owner 时继续 fail closed；本轮没有新增急停持久化/checkpoint
恢复机制。未使用未验证的管理恢复脚本。

当前跨机仍为 FrameLink/HTTP 经 SSH，Pi 内控制进程/owner 与传感器之间为 ROS 2 DDS。
ROS_LOCALHOST_ONLY=1 保持车内图通信；这次未切换跨机 DDS。
