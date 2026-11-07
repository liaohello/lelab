# LanderPi 重连与真实设备无输出预览

执行开始：2026-11-06 01:41 UTC（用户当地2026-11-05晚间）；各次采样时间见JSON。
目标：`pi@<private-ip>`，采用已有SSH密钥、正常host key校验；未使用或保存密码。

## 结论

连接真实Pi成功；恢复现有最小臂栈及已部署agent，完成左SO101读取、六路舵机真实反馈、
相机消息和后端无输出预览。预览正常获取/释放两端租约，16.044秒内控制板目标消息为0。
本轮没有arm/engage、关节目标发送、扭矩操作、主/从臂标定作业或episode录制。
启动板卡节点做了已审查的初始化写入（包括加载offset，可能写舵机EEPROM），不称为纯只读。

这不是新版FrameLink/独立控制进程的真机验收；Pi仍是历史部署副本，上位机仍是legacy控制工厂。
本轮只通过HTTP实际驱动后端preview，没有进行浏览器按钮/截图验收。

## 连接前现场

- 主机raspberrypi，aarch64，MentorPi已运行约14小时，Python3.10.12/Humble。
- 镜像本地ID：`sha256:4863da8b74e6aa185bfd5f6a40ad4462e8fd6b5b66411881ed2be6dbccdb4898`。
- 持久挂载仍为 `/home/pi/docker/tmp -> /home/ubuntu/shared`。
- `start_app_node.service` inactive；没有ROS节点，也没有进程占用 `/dev/rrc -> /dev/ttyACM0`。
- 18110隧道已连接当前Pi地址，agent未启动时返回connection reset，非SSH不可达。
- SO101左主臂by-id尾号`5B90149723`存在；其他三条USB设备均未操作。

初次只读清单查找了旧预检工具假设的嵌套包路径，结果为不存在。
随后确认实际包是 `/home/ubuntu/lelab_car/lelab_mobile_agent/agent.py`，不是再嵌一层lelab_mobile_agent。
正确路径和逐文件哈希见 `running-state.json`，不能用初次路径探测结果声称车端未部署agent。

## 启动动作与范围

先运行 `start_car_arm_stack.sh --check`，确认无串口owner、无其他vendor控制者及参数路径。
随后复用已有授权启动最小栈：

```bash
PI=pi@<private-ip> \
STATE_DIR=/home/ubuntu/shared/lelab_car/state/arm-stack-reconnect-20261106T014256Z \
ROBOT_CONTROLLER_PYTHONPATH=/home/ubuntu/lelab_car/vendor \
START_CAMERA=1 \
tools/ros2_collection/start_car_arm_stack.sh --apply --i-authorize-board-writes
```

板卡节点初始化会enable_reception、加载offset、设置机型/LED和底盘零速；不包含init_pose。
启动controller_manager与单独Aurora相机；使用原隔离bounded-read SDK/node，没有覆盖原厂源码。
初始化服务finite wait通过；输出栈check/apply命令均exit0，详见两份log。

agent复用既有 `/home/ubuntu/lelab_car/run_readonly.sh`，日志写新状态目录的agent.log。
该脚本虽然名含readonly，实际profile是hardware_hybrid_manual_v1、read_only=false；
本轮确认actuator_outputs=false、无租约、无使能，不能将profile身份改称旧只读profile。
没有用最新本地源码覆盖车端、没有迁移或删除sessions、没有切换current。

## 实测

- 单一UART owner：PID1649的补丁版ros_robot_controller；其他程序未另开串口。
- 真舵机ID1/2/3/4/5/10，最后一轮raw为536/590/69/162/907/456。
- `acquire -> preview -> 5次status -> release`均成功；会话ready、arm preview、commands_sent=0。
  左主臂五轴rad与夹爪读数见hardware-preview-probe.json，未修改A/B/C资产。
- 同时订阅实际板卡输出topic与相机：16.044秒，command_messages=0；125帧图像，640×400 bgr8，
  这段测得约7.79Hz，不沿用历史14Hz。未进行图像画质、同帧JSON绑定或视频页面验收。
- 释放后18轮health/status查询无异常，control_active_any=false、outputs_enabled_any=false。
  反馈最大年龄0.20065s，最大scan_span0.024617s；最后累计702次成功扫描、0次失败。
- 隧道请求延迟（小样本，非三链路基准）：health p50 12.108ms、p95/max157.082ms；
  status p50 12.021ms、p95/max54.1ms。不能据此归因为GIL或声称10Hz跟随已通过。

特别注意：旧栈status脚本输出的positions_rad全0来自controller缓存，虽然它同时输出
`feedback_is_measured=true`。该标志不能覆盖缓存的来源；本轮只把独立bus_servo_pulses和
`servo_position_readback`当作真实位置。释放后反馈因calibration_not_configured而valid=false是
无epoch绑定的门禁状态，不以零值或旧目标替代。

## 保留的运行状态与停止范围

保留最小栈、相机、agent，控制租约已经释放，输出关闭；未留下跟随worker。
进程PID、cmdline、/proc start ticks、唯一串口owner及部署哈希记录于running-state.json。
实际state_dir位于shared持久挂载，源码和sessions仍在历史容器目录，尚未做发布迁移。

只允许停止本轮拥有且指纹仍匹配的进程。先核对并停止agent，再用上面相同PI/STATE_DIR/
ROBOT_CONTROLLER_PYTHONPATH/START_CAMERA参数运行 `start_car_arm_stack.sh --stop`。
先停agent是为了避免board重启时旧读请求触发协议锁存。禁止泛用pkill或根据过期PID直接发信号。

页面入口：<http://127.0.0.1:18286/landerpi/teleop>。
无活动租约时会话显示disconnected不等于Pi/agent断网，需同时看硬件探测和车端状态。

## 下一步硬门禁

1. 完成上位机Facade显式接入FrameLink，保持生产会话与设备身份门禁，回归 #9 数据链。
2. 生成完整包含agent_licence_bridge等新依赖的候选包，在arm64/Humble核验导入与实际消息版本；
   不能用仅有旧ALLOW_LIST或amd64产物直接覆盖Pi。持久版本化部署单独校验，保留旧包与sessions。
3. 候选配置使用明确的低速范围并重新核对真实B/C标定。旧车端profile的max_step_rad_s=3.0，
   不应直接用作下一步逐轴小幅验收的低速参数。
4. 裁决未通过的torque保护测试、独占控制、真实hold/急停与失联效果；
   新版只读/无输出预览通过后再做授权范围内的逐轴低速测试。

附件：read-only-probe.json、stack-check.log、stack-apply.log、agent-status-sample.json、
hardware-preview-probe.json、latency-feedback-check.json、running-state.json。
