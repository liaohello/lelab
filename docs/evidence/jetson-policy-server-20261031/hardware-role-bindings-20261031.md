# 硬件角色绑定与双臂观测开发记录（2026-10-31）

## 用户确认的角色

保存文件：

```text
~/.local/share/lelab-jetson-20261031/approot/hf/hardware/hardware-inventory-latest.json
```

本目录归档副本：`hardware-inventory-20261031.json`。

| 序列号 | 设备 | 角色 |
| --- | --- | --- |
| `5B90149339` | `/dev/ttyACM1` | `left_follower`（左从臂） |
| `5B91045575` | `/dev/ttyACM0` | `right_follower`（右从臂，对应 `my_robot`） |
| `5B90149723` | `/dev/ttyACM3` | `left_leader`（左主臂） |
| `5B90149652` | `/dev/ttyACM2` | `right_leader`（右主臂） |

## 应用到机器人记录

`/hardware/rig-bindings/apply` 已只更新端口，不改变标定配置：

- `left_rig.follower_port` → `5B90149339`
- `left_rig.leader_port` → `5B90149723`
- `right_rig.follower_port` → `5B91045575`
- `right_rig.leader_port` → `5B90149652`

右从臂此前已验证的 `my_robot.json` 已复制到隔离应用目录：

```text
~/.local/share/lelab-jetson-20261031/approot/hf/calibration/robots/so_follower/right_rig.json
```

原文件保留。右从臂配置现在可用；左从臂和两个 leader 尚未标定。

## 新增只读双臂观测

后端：

- `POST /observe/bimanual/start`
- `GET /observe/bimanual/status`
- `POST /observe/stop`（沿用单/双臂共同会话与 `observe` 硬件租约）
- `ReadOnlyBimanualFollowerSource` 按左→右顺序执行 `sync_read("Present_Position")`
- 两个总线上的全部写方法均替换为拒绝调用；断开使用 `disable_torque=False`
- 每帧校验 12 个关节值均为有限数值，并广播 `actions_sent=0`

前端 `/observe`：

- “双臂 / 单臂（已验证）”模式切换
- 左、右 follower 串口和标定路径输入
- “从硬件角色填充”只读取已保存角色和机器人记录，不打开串口
- 左从臂缺少标定文件时提示“原始位置”，不会伪造为已标定
- WebSocket 同时提供完整 12 维值和 `arms.left` / `arms.right`

## 验证

- 后端 5 项 observe 测试通过：单臂回归、双臂状态拆分、两总线写保护、相机释放、重复端口拒绝。
- Ruff、前端 ESLint、生产构建通过。
- 浏览器实际加载 `/observe` 并点击“从硬件角色填充”：
  - 左填充 `5B90149339`
  - 右填充 `5B91045575`
  - 右标定自动填充为 `right_rig.json`
  - 左标定留空并显示“原始位置”提示
- 本次没有点击“开始双臂观测”，因此双臂硬件读数尚未实机验证。

## 下一步

1. 在 `/observe` 点击“开始双臂观测”，先只验证 12 维读数、力矩仍为 0 和 `actions_sent=0`。
2. 通过现有标定页逐台标定：
   - `left_rig`：左 leader、左 follower
   - `right_rig`：右 leader
3. 四台标定齐全后，双臂只读观测的关节值才有可比较的物理语义。
4. 实机执行仍需要匹配的 12 维策略、方向/单位验证、E-stop 现场检查和明确运动授权。

## 首次实机启动发现与修复

用户点击开始双臂观测后，未标定的左从臂报错：

```text
FeetechMotorsBus ... has no calibration registered.
```

原因是只读实现无条件调用
`sync_read("Present_Position")`，LeRobot 默认尝试把 encoder step 归一化为
degree/range，而无标定文件时不能归一化。该次启动没有写电机或改变力矩。

修复：左/右各自按标定状态选择
`normalize=self.calibrated`。未标定总线返回原始 12-bit encoder step，并在
`status.bindings.<side>.unit="raw_steps"` 和前端表格标题明确标记；已标定总线继续返回
degree。应用重启释放串口。

修复后再次实际启动双臂只读观测并正常停止：

- `frames=78`，WebSocket `joint_update=78`
- 12 个左右 follower 位置全部返回
- 左臂 `unit=raw_steps`，右臂 `unit=degrees`
- 两路相机年龄约 3.7 ms
- 12 个 Torque_Enable 全部为 0
- `actions_sent=0`、`fault=null`、`phase=stopped`
- `observe` 硬件租约显示 task-finished 并释放

证据：`stageB2-bimanual-real-stop.json`。双臂机械臂在此期间保持静止，未诱导关节运动；
未标定左臂只能证明通信和原始读数，不能证明物理角度语义。
