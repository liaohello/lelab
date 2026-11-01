# 硬件角色确认页检查（2026-10-31）

- 应用：`/home/liaozhengyan/Desktop/lelab-bimanual`，隔离数据根
  `~/.local/share/lelab-jetson-20261031/approot`，`127.0.0.1:18186`。
- 日志：`/tmp/lelab-hardware-18186.log`。
- 页面：顶部“硬件角色”，路由 `/hardware`。

## 实时枚举

`GET /hardware/inventory` 返回 4 个串口，均为 `present=true`、角色未分配：

| 序列号 | 设备 | USB 路径 | VID:PID |
| --- | --- | --- | --- |
| `5B91045575` | `/dev/ttyACM0` | `usb-3-1.1` | `1a86:55d3` |
| `5B90149339` | `/dev/ttyACM1` | `usb-3-1.4.2` | `1a86:55d3` |
| `5B90149723` | `/dev/ttyACM2` | `usb-3-1.4.3` | `1a86:55d3` |
| `5B90149652` | `/dev/ttyACM3` | `usb-3-1.4.4` | `1a86:55d3` |

保存目标：

```text
~/.local/share/lelab-jetson-20261031/approot/hf/hardware/hardware-inventory-latest.json
```

## 页面检查

- 页面显示 4 行“已连接”、每行串口/VID:PID/USB 路径、角色下拉框和备注框。
- 选择角色后“必需角色”计数即时更新；重复选择同一必需角色会被拒绝并提示
  “角色已分配给 5B90149339”。
- 用浏览器内 fetch 拦截模拟“移除 5B90149339”（仅 UI 测试，未拔真实线）：
  页面变为 3 行“已连接”+1 行“未连接”，事件区记录“拔出 5B90149339”。
  刷新页面后恢复真实 4 行。
- 浏览器控制台无报错；`/hardware/inventory` 轮询均为 200。

## 边界

- 本页只读枚举 `/dev/serial/by-id` 与 sysfs；不打开串口、不写标定、不改变力矩。
- 检查期间未保存角色、未生成 latest 文件，未进行任何真实动作。
- 真实插拔与角色分配由用户在现场完成；保存后的 JSON 应另行归档到证据目录。
