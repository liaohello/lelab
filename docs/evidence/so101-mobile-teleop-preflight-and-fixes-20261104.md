# SO-101 小车遥操作：只读前置与本地修复（2026-11-04）

本轮小车已由用户上电，但未收到本次控制板写入或运动授权。历史 `pi@<private-ip>` 的 SSH 两次连接超时；本机无 `/dev/serial/by-id`、`ttyACM` 或 `ttyUSB` 条目，未监听 8110。不能由此断言车端关机或确定主臂的实际归属。既有 8000 上位机进程能响应，但其 OpenAPI 没有 `/ros-mobile/teleop/status`，并非加载当前遥操作源码的进程。本次未停止该进程或 18186 服务。

只读前置报告为 `/tmp/lelab-teleop-preflight-20261104.json`，执行了 `python3 tools/ros2_collection/teleop_preflight.py --pi pi@<private-ip> --agent-url http://127.0.0.1:8110 --app-url http://127.0.0.1:8000 --timeout 2 --json /tmp/lelab-teleop-preflight-20261104.json`，退出码 1：串口、车端 agent、上位机遥操作 API、容器状态、源码哈希及从臂反馈均不能通过；远端检查因 SSH 超时没有得到数据。报告中的 `car=ssh:` 是旧版错误摘要，不能当成远端哈希。实际硬件反馈、独占性和控制板状态均未测得。

本地源码修复：

- 不完整或非有限的主臂读数不能报告健康，夹爪缺失不会沿用上一轮；A 层强制匹配选中的 USB by-id 与序列号，标定版本或 identity 变更后必须重新打开。USB 标识不能代替机械臂本体身份核对。
- 双层租约在车端模式设置失败时尝试释放已获取的车端租约，失败时报告并依靠车端过期机制；释放会话与结束 episode 先停跟随再做远端收尾，暂停和急停不先读取远端状态。
- 前置检查改为在车宿主机检查容器，缺少底盘锁定、没有发布者或反馈年龄无效时不再判通过。
- 遥操作面板的同步入口改为跳转到已有 session 同步/导出区；操作错误不被后台轮询立即清除。

本次验证：`PYTHONPATH=tools/ros2_collection work/ros2_collection/envs/ros_jazzy/bin/python -m pytest tools/ros2_collection/tests -q` 为 **283 passed、5 subtests passed**；`app_ros_mobile/tests/test_mobile_teleop_coordinator.py` 为 **7 passed**。测试进程忽略 SIGHUP 以允许伪终端独占测试结束。前端 `tsc --noEmit` 与 Vite 生产构建通过；本地 LeRobot 导入解析到 `releases/sanitized-20261024-01/lerobot/src/lerobot/__init__.py`。

网页检查使用独立的 18195 LeLab 进程和 18197 的**模拟** HTTP 网关：真实点击“前往同步与导出”后地址定位到对应区块；空标定时点“取得双层租约”显示 `calibration not ready`，轮询后仍可见，并能点击关闭。模拟网关没有控制写入接口；这些结果不是车端或硬件验收。

仍需：确认车的当前地址和连接、接入主臂并核对身份；按现场授权部署最小臂栈、核验真实反馈、急停、无输出预览，再在单独授权及安全条件满足时做低速小幅运动和故障注入。两段真机 episode、同步导出与页面读回尚未完成。现有 invalid session 和未跟踪的训练 release 未改动。
