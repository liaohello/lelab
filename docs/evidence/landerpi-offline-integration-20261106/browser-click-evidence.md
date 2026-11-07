# 浏览器点击证据：only_joint 回中（离线模拟）

日期：2026-11-06。环境：生产构建 `app_ros_mobile/frontend/dist`（tsc 干净、build 成功）+
`homing_web_probe.py`（生产 FastAPI 路由 + FrameStreamHardwareFacade，仅 leader 边缘为模拟）
+ 隔离 Humble 装置（production agent + standalone control process + `sim_board_owner.py`，
domain 94，agent 18131 / frame 18123）。模拟身份 `simulated_offline`，无物理串口。

浏览器真实操作序列（页面 `/landerpi/teleop`）：

1. 点击“开始无输出联动”：`POST /ros-mobile/hybrid/session` body 含
   `{"action":"acquire","profile":"hardware_hybrid_manual_v1","owner":"Linux x86_64-d4bf354a"}`；
   会话进入 `ready · aligned`。
2. 将“回中轴”下拉从默认 `joint1` 改为 `joint2`（原生 setter + change 事件）。
3. 点击“开始从臂回中（真实运动）”，捕获实际请求体：

```json
{"action":"start","session_id":"6ea66c1fefce485c87571af19b8ab1ae",
 "session_epoch":3232811014408367,
 "calibration_bundle_hash":"da49d6ce96ccecbb022594dad0da0411672c0456b691df1ff5f9fe91482e6284",
 "only_joint":"joint2"}
```

4. 4 秒后服务端 `/ros-mobile/hybrid/status`：

```json
{"state":"ready","homing":"moving","only_joint":"joint2","torque":"verified",
 "rows":{"1":"verified","2":"verified","3":"verified","4":"verified","5":"verified","10":"verified"},
 "feedback_age_s":0.0339,"publisher_instance":250258251,"publisher_generation":0,
 "current_joint":"joint2","step_target_raw":511,
 "observed_raw":{"joint1":527,"joint2":517,"joint3":76,"joint4":118,"joint5":907,"r_joint":457}}
```

5. 页面文本（渲染结果，节选）：

```text
joint2 已到位
回中范围  [joint2]
单轴回中只移动所选轴；其余轴按实测保持，并与任务起始位置独立比较漂移。
100.0% · 反馈 seq 422 · 年龄 49 ms · 发布者 #250258251 / g0
逐轴力矩加载确认（requested → verified / failed）  六轴已读回
ID1 verified  ID2 verified  ID3 verified  ID4 verified  ID5 verified  ID10 verified
joint1 误差 27 raw（目标 500 / 真实 527）
joint2 误差 1 raw（目标 500 / 真实 501）
joint3 误差 424 raw（真实 76） … r_joint 误差 107 raw（真实 457）
joint2 已到达目标容差；其余轴保持任务起始位置，未参与运动。…
```

6. 点击“释放会话”后：
   - agent `/status`：`control.active=false`，`frame_outputs_enabled=false`，`estop_latched=false`；
   - 上位 `/ros-mobile/hybrid/status`：`state=disconnected`，`session_id=null`，`outputs_enabled=false`。

结论（仅限离线模拟）：网页 only_joint 选择、逐轴加载确认、目标/实测、反馈年龄与 owner 代际、
单轴完成语义均按预期工作；释放后车端不保持控制。未验证真实运动、真实停止或物理安全。
