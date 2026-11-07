# 从臂离线标定：证据（2026-11-04 / 2026-11-05 UTC）

范围：源码、测试、构建尝试与 HTTP fixture。**没有**连接 Pi、没有打开 `/dev/rrc`、
没有发布 ROS 话题、没有机械臂运动。所有 fixture 结果都带 `simulated: true`。

## 文件

| 文件 | 内容 |
| --- | --- |
| `tools-follower-job-tests.log` | 车端标定任务单测：`49 passed, 13 subtests passed` |
| `app-follower-api-tests.log` | 上位机路由/资产/失效 + 相关既有套件：`91 passed` |
| `app-tests-per-file.log` | `app_ros_mobile/tests` 逐文件运行结果（见下方环境说明） |
| `tsc.log` | `tsc --noEmit -p tsconfig.app.json`，exit 0 |
| `vite-build-attempts.log` | 早期环境受限时的三次构建尝试；已保留为失败历史 |
| `vite-build-final4.log` | 本轮最终正式构建，exit 0 |
| `fixture-smoke.log` | HTTP fixture 冒烟（start → 采样 → target → 只发 ID10） |
| `browser-check.json` | Firefox fixture 最终测试，全部检查通过 |

## 结果

```text
车端任务        49 passed, 13 subtests
上位机相关      91 passed
tsc             exit 0
vite build      exit 0（最终复验）
firefox fixture 全部 PASS
```

### app 全套逐文件（45/57 文件通过）

13 个文件在导入阶段崩溃或超时，原因全部是本次会话的内存/线程压力，与本轮改动无关：

- `cv2` / `torch` 导入时段错误：`test_dataset_management`、`test_record`、`test_rollout`、
  `test_server`、`test_smoke`、`test_status_websockets`、`test_teleoperate`、
  `test_train_server`、`test_sealing`、`test_composite_api`、`test_hardware_lease_api`、
  `test_arm_bindings`、`test_utils_system`；
- `test_ros_mobile_teleop_export`（8 failed）：`RuntimeError: can't start new thread`；
- `test_t4_hybrid_9d_export`（3 failed）：`av.error.MemoryError: Cannot allocate memory`。

同一会话中 `SwapFree` 仅 76 kB、`Committed_AS` 63 GB，属于机器级内存耗尽；
`test_ros_mobile_teleop_routes`、`test_t3_wave_a`、`test_t4_hybrid_recording`、
`test_hybrid_hardware`、`test_follower_calibration_api` 等标定相关套件全部通过。

## 早期构建阻断与最终结果

`RLIMIT_AS = 4294967296`（硬上限，无法提升）。Node 22 在此空间内无法为 wasm trap-handler
或 V8 CodeRange 预留地址：

1. `npm run build` → `WebAssembly.instantiate(): Out of memory`（undici/fetch 触发）；
2. 用 CJS stub 屏蔽 fetch 仍会加载 undici；`--no-experimental-fetch` 无效；
3. `--disable-wasm-trap-handler` 可以完成 `2429 modules transformed`，随后
   `vite:prepare-out-dir` 复制 `public/favicon.ico` 到 `dist/` 时被工作区 shim 拒绝（EPERM）。

该早期尝试清空了 `frontend/dist`。本轮已在完整权限环境中重新构建成功，
产物为 `dist/assets/index-Cl520GAP.js`，`dist` 已恢复。

```bash
cd /home/liaozhengyan/Desktop/lelab/app_ros_mobile/frontend && npm run build
cd /home/liaozhengyan/Desktop/lelab && app_ros_mobile/.venv/bin/python \
  tools/ros2_collection/follower_calibration_browser_check.py
```

## fixture 冒烟（已通过）

```text
start 200 manual_range fad14f8d41907e6a
arm_valid True {'joint1': 117, 'joint2': 86, 'joint3': 84, 'joint4': 116, 'joint5': 80}
target 200 True 426 1
commands [(10, 426)]
removed step route -> 404 not_found
```

`target_permille=420` 在安全窗 `[300,600]` 上映射为 raw `426`；命令记录里只有
`servo_id=10`；已删除的 `/calibration/follower/step` 返回 404。

## Firefox 最终结果

`follower_calibration_browser_check.py` 最终 11 项检查全部通过：

- 未拖动前端点标记被阻止并显示原因；
- 滑轨实际发送 3 个目标；
- 目标只发送给 `r_joint / ID10`；
- 松手后不再发送新目标；
- 状态显示受控运动而非“执行输出关闭”；
- settle 后端点标记按钮可用；
- 闭端标记写入证据；
- 标记后按钮再次禁用；
- 浏览器脚本错误为空。

安全窗在真实页面中不再预填 `[300,600]`。浏览器夹具在测试代码中显式填写该测试值；
真实页面必须由操作员输入并勾选“已现场确认”后才能启动。
