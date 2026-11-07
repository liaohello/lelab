# 本地 ROS 2 Humble 离线运行时（无容器引擎、无 sudo）

对应方案：`docs/handoffs/2026-11-05-landerpi-local-ros2-offline-development-plan.md`
当前状态与门禁结论：`docs/handoffs/2026-11-05-landerpi-local-ros2-offline-status.md`

## 为什么不是 docker

本机没有 docker/podman/`/opt/ros`/Python 3.10，`sudo` 需要密码，且 Docker Hub 直连与本地代理
均不可达（`CONNECT aborted`）。因此 `ros:humble-ros-base-jammy` 的 registry digest **未取得**，
`lock/env.lock.json` 里记为 `NOT_VERIFIED`，可追溯性改用官方 Ubuntu base rootfs 的 SHA256
（与发布目录自带 `SHA256SUMS` 核对）加固定 deb 版本清单。

实际隔离方式是两层，缺一不可：

```text
bwrap --dev /dev ... -- unshare --map-root-user --mount -- bash -c '<命令>'
```

- 单独 `unshare --map-root-user`：能拿到 uid 0，但本机内核/AppArmor 拒绝写字符设备，
  `echo x > /dev/null` 失败，dpkg postinst 直接崩。
- 单独 `bwrap`：`/dev` 可用，但进程仍是 uid 1000，apt/dpkg 装不了包。
- 嵌套两层同时得到可写 `/dev/null` 与 uid 0，且不开 PID namespace，
  因此 `run/*.json` 里记录的 PID 就是宿主机可见 PID，可用 `/proc/<pid>/cmdline` 与 `ss -ltnp` 核验。

其它被内核挡掉的路径（不要再试）：`mknod`（任何命名空间都 EPERM）、`newuidmap`（不存在，
所以只能单 uid 映射）、`mount -t proc`（需要 PID namespace；改用 `--rbind /proc`）、
绑定 `/proc` 的非 rbind 形式。

单 uid 映射的连带后果：任何 chown/chgrp 到其他 uid 都会 EINVAL。`dbus` 的 postinst 用
`dpkg-statoverride` 把 launch helper 交给 `messagebus`，因此卡住整条 gtk/dconf 依赖链。
`bin/prepare` 关闭 recommends 并在检测到卡死时清除该链——这些包 rclpy/rosbag2/colcon 都不需要。

## 一条命令

```bash
tools/ros2_collection/local_ros2/bin/doctor      # 只读体检；FAIL 行即阻塞门禁
tools/ros2_collection/local_ros2/bin/prepare     # 首次需联网：下载 rootfs + 装 Humble
tools/ros2_collection/local_ros2/bin/build       # 暂存 vendor 消息（逐文件 SHA256）并 colcon 构建
tools/ros2_collection/local_ros2/bin/up          # 起模拟板卡 + 独立控制进程 + 租约发放器
tools/ros2_collection/local_ros2/bin/accept      # 协议/跟随/故障/租约失效 全套验收
tools/ros2_collection/local_ros2/bin/record      # 生产 SessionRecorder + 真实 ros2 bag record
tools/ros2_collection/local_ros2/bin/down        # 只停自己记录的实例（无 pkill）
tools/ros2_collection/local_ros2/bin/package     # Pi 部署候选包 + 清单（排除离线夹具）
tools/ros2_collection/local_ros2/offline/bench_links.py   # 三种链路基准
```

运行产物全部在 `work/ros2_collection/local_humble/`（rootfs、ws、run、logs、state、data、
evidence、dist），不写发布快照，不动 `current`。

## 安全默认

- 离线 profile 固定 `simulated`，不随环境变量改变；`bin/up` 不接受真实输出入口。
- 运行时**没有安装 pyserial**，也没有 `/dev/rrc`、`/dev/tty*`、`/dev/video*`，
  真实总线/相机在结构上不可达，而不是靠约定。
- 所有 ROS 名称在 `/lelab_offline/` 命名空间下重映射，不复用真实板卡 topic。
- `/repo` 与 `/vendor` 以只读绑定进入，并写入探针强制校验，失败即中止（保护未提交修改）。
- 端口占用先检查，PID 用 pidfile + `/proc` 指纹双重确认后才发信号。

## 断网复跑

`bin/prepare --offline` 会跳过 apt 步骤，仅使用已缓存的 rootfs 与 rootfs 内已装好的包。
`--offline` **不**等于已验证的完全离线重建：apt 缓存已 `clean`，所以在全新机器上重建仍需
一次联网下载（这一点如实记录，不写成已通过）。真正可断网重建需要先导出 deb 缓存/镜像。

---

# 追加：真正的 Docker 路径（2026-11-05 晚些时候，已装 docker）

用户授权后安装了 `docker.io 29.1.3`（apt），并配置了 daemon 代理——Docker Hub 在本机
**只有经 `127.0.0.1:7890` 才可达**，直连超时：

```text
/etc/systemd/system/docker.service.d/proxy.conf   # 仅 HTTP(S)_PROXY + NO_PROXY，不含任何凭据
usermod -aG docker liaozhengyan                    # 之后无需 sudo，组权限即可
```

镜像按 digest 固定（不是靠 tag）：

```text
ros:humble-ros-base-jammy@sha256:a3f4de78e42cca693a543ece5798aedcf1cae0171fe71c334a047332cc72ef93
  amd64 清单 10 层 / 压缩 264 MB / Ubuntu 22.04.5 / Python 3.10.12 / ROS_DISTRO=humble / 构建于 2026-10-27
  arm64 清单 sha256:48435c01f287… （Pi 那一侧的架构，供上机核对）
  注意：按 tag 拉取时 docker 报告的是 index digest（sha256:d9bcf2e6…），与上面的平台清单 digest 不同；
        构建用 FROM <平台清单 digest>，不要只信 tag。
```

一条命令：

```bash
tools/ros2_collection/local_ros2/bin/docker build          # 造 lelab/humble-dev:local
tools/ros2_collection/local_ros2/bin/docker up             # vendor 消息构建 + 模拟板 + 相机 + 独立控制进程 + 生产 agent（默认缴械）
tools/ros2_collection/local_ros2/bin/docker arm true|false # 经 agent HTTP：取租约→绑 epoch→（显式）给输出权限
tools/ros2_collection/local_ros2/bin/docker renew          # 续租约（上位侧周期性做的事）
tools/ros2_collection/local_ros2/bin/docker accept         # 容器内单测 + 唯一发放者核验 + preview/follow/faults + 旧入口拒绝
tools/ros2_collection/local_ros2/bin/docker faults [--kill-agent]  # 现场故障矩阵（含租约到期、agent SIGKILL）
tools/ros2_collection/local_ros2/bin/docker record         # 一次会话两集：真实 sqlite3 录包→归档→同步→受限模拟 6D 导出
tools/ros2_collection/local_ros2/bin/docker disarm         # 先在帧流上暂停，再由 agent 撤销输出权限
tools/ros2_collection/local_ros2/bin/docker doctor         # 安全形状、实例归属、进程与许可状态快照
tools/ros2_collection/local_ros2/bin/docker down           # 只停/删标签归属核验通过的自己的容器
```

授权权威只有一个：`up` 启动的是生产 `lelab_mobile_agent.agent`（`--ros-args -p
licence_socket:=… -p control_status_path:=…`），许可镜像与录制上下文都由它经
`agent_licence_bridge` 在同一 0600 unix socket 上发布；`offline/licence_agent.py` 只剩
协议单测夹具，启动器不再执行它，`assert-single-issuer` 会扫 `/proc` 证明没有第二个发放者。
`record` 的导出走 `POST /export/offline-simulated-6d` 那条受限入口
（契约 `simulated_offline_6d_v1`）：它不放宽任何门禁，普通 `/export` 对同一会话仍然拒绝，
产物始终 `data_identity=simulated_offline`、`training_eligible=false`。

形状与 Pi 的 `MentorPi` 对照：host 网络、Humble/Python 3.10.12；**刻意不同**：不 privileged、
不映射 `/dev`、`cap_drop ALL` + 最小 cap、`no-new-privileges`、仓库只读挂载、镜像内**没有 pyserial**。
`ROS_DOMAIN_ID=93`、帧端口 127.0.0.1:18122，与上面 bwrap 运行时（92/18121）互不冲突。

本路径实测（`work/ros2_collection/local_humble/docker/evidence/`）：容器内 65 项测试通过；
preview 4/4（板卡输出 0→0）；follow 8/8（118/119 准入、109 次发布、板卡收 110 条、每条 6 位置、
有效 8.93 Hz、反馈年龄 36.7 ms、往返 p50 100.2 / p95 100.3 ms）；faults 10/10；
真实 sqlite3 录包 `READY` 且 `raw_ready`，bag 内实测：
`recording/teleop` 39 行、`set_position` 39 条、`servo_readback` 21 次扫描、相机 image/camera_info 各 64。

仍未闭合：6D 导出/LeRobot SDK/页面读回（需要生产 `agent.py` 在容器内提供归档 HTTP 接口）、
三链路基准、arm64 与真机项。
