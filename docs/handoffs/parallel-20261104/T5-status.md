# T5 Wave A 状态（2026-11-04，America/Los_Angeles）

## 状态与范围

**模拟页面和源码可供集成；真实后端联调、设备控制及完整验收受阻于下述接口缺口。** 未连接任何设备、未 SSH、未启动/停止既有服务或容器，未训练。模拟结果不是真车闭环。仅测试本会话临时回环 HTTP 服务及合成数据。

- 修改 `app_ros_mobile/frontend/src/pages/RosMobile.tsx`（保留原有未提交变更）：挂载联合面板；旧控制页面强制只读，隐藏键盘/专家/直接执行器及旧只读训练导出按钮；旧只读采集和原始同步入口保留。旧 RGB 缺来源帧序号、年龄或状态过期时提示过期。
- 新增 `app_ros_mobile/frontend/src/components/control/HybridTeleopPanel.tsx`：设备/租约/反馈原样展示、六类标定 JSON、联合会话、控制事件、阶段和人工标注、同步/导出/取消、视频来源元数据与错误提示。浏览器不生成运动样本或安全心跳。正常轮询不会清除错误；连接/能力未知或旧 profile 时禁用联合操作。
- 修改 `app_ros_mobile/frontend/src/pages/DatasetViewer.tsx`：读取 T4 工作区提供的 `phases` 及 `video_ranges`，选择阶段后过滤时间序列并定位视频；缺少阶段视频范围时提示缺失。视频用 HTML 媒体事件限定区间，浏览器的时间更新并非逐帧精确。
- `MobileTeleopPanel.tsx` 的既有未提交差异系本会话开始前已有，保留未覆盖。本轮未改任何 `src/lib/` 文件；既有 `src/lib/apiClient.ts` 受根 `lib/` 规则忽略，未新增被忽略的 lib 路径。`frontend/dist/` 受根 `dist/` 忽略，最终构建在本地，需总协调按发布方式纳入/部署；未改忽略规则。

## 页面使用的 public API 和联调请求

- GET `/ros-mobile/hybrid/status`、`/capabilities`、`/recording/status`；前端并行轮询，4 秒请求超时并以最后成功时间做过期降级。
- POST `/ros-mobile/hybrid/session`：`{action:"acquire", profile, owner: clientId}` 或 `{action:"release",session_id,session_epoch}`。
- POST `/ros-mobile/hybrid/control`：`{action,session_id,session_epoch}`，action 为 preview、align、arm、engage、release_clutch、pause_all、estop、clear_estop、enable_base、lock_base；急停在其他面板操作忙时仍可点击。后端必须独立执行租约、有效期、设备与安全门禁，不能依赖按钮状态。
- POST `/ros-mobile/hybrid/calibration`：`{action:validate|save,kind,identity,payload}`，kind 六类。POST `/ros-mobile/hybrid/recording`：`{action,session_id,session_epoch,...}`；阶段发送 `{phase}`；结束发送 `{success:true|false|null,note}`。结束回合及停止录制前先发 `pause_all`，失败则不发结束请求。阶段切换本身只发 recording/set_phase。所谓暂停按钮是暂停**运动**，并非录制器暂停；契约没有录制暂停 action。
- `/ros-mobile/sync`、`/export` 使用 `{session_id}`，导出查询 `/export/status/{job_id}`、取消 `/export/cancel/{job_id}`。后端仍须按被选 session 的元数据拒绝旧只读导出与缺失九维来源；页面 profile 检查不能代替服务端。
- 阶段查看器使用现有 `/datasets/local/{repo_id}/episodes` 中每集 `phases[{phase,from_s,to_s,source,video_ranges}]`；时间序列使用现有 `/timeseries`。视频范围为分块视频绝对时间，曲线范围为该集相对时间。

**需总协调与 T3/T4 决定的接口差异（不是已变更的共享契约）：**

1. 第 3 节未规定能力 JSON 的结构。本页面暂要求 `capabilities.profiles: string[]` 与 `capabilities.actions: string[]` 明确列出 profile 和每个可用 action（包括 sync/export）；缺少即禁用，且当前 status.profile 必须是该列表中的 hybrid profile。当前工作区 T4 `/capabilities` 暂接 `HybridSession.calibration_capabilities()`，尚无这两个数组，因此真实后端目前不会开放联合按钮。请统一能力/操作级权限结构与 profile 激活语义；不要用默认健康值填充。
2. T3 `acquire_session` 要求 `owner`，页面已发送浏览器 clientId；确认 profile、标定身份与 bundle 的后续请求字段。当前 T4 路由把 control 的 `session_id/session_epoch` 丢弃，release 也只传 reason，**尚不能证明旧会话请求被拒绝**；必须在服务端绑定并核验会话/epoch，页面传值不能视作保护。预览路由当前调用纯 `preview()`，T3 状态机的 `align` 又要求先进入 preview 状态，须统一语义。
3. 标定 save 当前路由明确 503；录制适配 `recording()` 尚未装配，人工 `success` 编码及 phase 与 car 录制事件需端到端确认。暂停/停止在服务端也应先停且失败不得结束；页面顺序只作额外防护。同步/导出接口能力、原始校验和拒绝报告需联调。
4. 本面板暂按视频 `source,frame_seq,age_s,valid,stale` 读取；状态元数据和 `/preview.jpg` 响应尚无可验证的同帧绑定。即使元数据递增，页面仍覆盖“图像与来源帧尚未校验”，不声称 JPEG 新鲜。需提供可验证的 JPEG source/seq 和年龄绑定，再启用真正的视频操作验收。
5. 当前 UI 以显式能力为门禁，未在本轮构造旧 epoch/错来源/错帧对真实路由的请求，未证明服务端拒绝、TTL、急停物理效果或本机安全循环。与 T3/T4 的接口稳定后需运行他们的契约测试并在无硬件联调中重做取消、旧会话、缺数据、状态变化等路径。

## 本次验证与证据

- Node `/home/liaozhengyan/Desktop/lelab/work/tools/node/bin/node` v22.20.0，TypeScript 5.9.3，复用现有 node_modules。
- `PATH=.../work/tools/node/bin:$PATH ./node_modules/.bin/tsc --noEmit`（frontend 根 tsconfig）退出 0，但该根配置使用项目引用且无直接源文件，不能替代应用检查；`tsc --noEmit -p tsconfig.app.json` 退出 2，仅显示已有未改 `src/pages/Training.tsx:438` 两处 `JobRecord.remote_gpu` TS2551。最终页面文件未出现额外 TS 报错。
- `PATH=.../work/tools/node/bin:$PATH npm run build` 退出 0，Vite 8.0.16；有 bundle 大于 500 kB、Browserslist 过期、oxc/esbuild 配置提示。最终 `dist/assets/index-B321k9PX.js` SHA256 `52b09919d2f61b0993175792a1266bd54247886aa930301ff74ed655c703a6b0`；`dist/index.html` SHA256 `265acdd293b9fe95c1060ef0c5c875ad801240df7ffff1abd01bcf8399643e91`。`git diff --check -- app_ros_mobile/frontend/src` 通过（不包含未跟踪文件）。
- 模拟页面实际点击：获取/释放、preview、锁底盘、align、arm、engage/release_clutch、enable_base、estop/clear_estop，标定 validate/save、开始录制/回合、observe/grasp、暂停并结束、停止录制、同步、导出与取消。`fixture-final-run.json` 显示 phase 两次调用之间没有运动请求，结束和停止各先 `pause_all`；最终构建的 `fixture-final-build.json` 显示人工 success 发送为 true。初次模拟 pause_all 拒绝后没有 end_episode，重试成功后才结束；结构化错误未显示 `[object Object]`，轮询及后续 preview 未清除错误。
- 将模拟 status 返回 503，页面显示状态未知且联合按钮禁用；恢复并切换 old-readonly 后仍禁用；冻结 source/frame_seq 超过 2 秒后显示过期。测试合成相机 JPEG 即便 metadata 更新仍显示未经帧绑定提示。
- 合成数据查看器点击 grasp：视频 URI `#t=1,2`，元数据加载后 currentTime=1，曲线为 1、1.5 秒；observe 曲线为 0、0.5 秒，视频播放在约 1.05 秒暂停，存在浏览器事件精度误差。仅证实合成内容呈现，不代表真实导出视频/曲线已验证。
- 独立 fixture、请求日志、截图及合成视频均在 `docs/evidence/parallel-20261104/T5/`；`simulated-error.jpg` 中同步/导出错误包含首版 fixture 空 action 判断缺陷，已修正并在 `fixture-final-run.json` 重测，勿当作生产错误。`simulated-frozen-error.jpg` 与 `simulated-phase-viewer.jpg` 分别为故障和阶段查看证据。
- 服务通过 `python3 -u docs/evidence/parallel-20261104/T5/mock_server.py` 启动，内核分配空闲 `127.0.0.1` 端口。各轮 PID/端口为 2613210/41359、2712300/35755、2723943/39889、2726625/36191；均由本会话停止，结束时 `ps` 无上述 PID。复现时以启动输出记录的新 PID 执行 `kill <PID>`；未占用 8000/18186。

## 剩余及最小下一步

先由总协调统一能力结构、会话 epoch 验证、preview 状态、录制/标定/视频关联后，在独立模拟网关与 T3/T4 实际实现上重跑页面。再由授权人员安排真实相机、双租约、手柄、SO101、五轴及 r_joint、急停、断线、真实 sqlite3 同步和六/九维导出的分阶段验收。本会话没有测试硬件、真实反馈、执行器或实体急停。主会话运行时仅标识 GPT-6，未提供可核实的具体型号/推理强度；无法确认项目要求的 GPT-6.1 Sol Max 已生效。辅助检索使用项目指定 default 只读角色。

---

## 追加：T5 Wave A 并行实现会话增量（2026-11-04 08:20–08:55）

> 本节由另一个并行的 T5 实现会话追加，**不替换上文**。两轮都实现了 `HybridTeleopPanel.tsx`，存在**同名未跟踪文件的覆盖冲突**，必须由总协调先裁决再合并。

### A. 冲突与保留（重要）

- 本会话在 08:22 把当时的 `HybridTeleopPanel.tsx`（87 行骨架）备份到 `docs/evidence/parallel-20261104/T5/baseline/`，随后重写为 494 行完整实现。若上文会话在同一文件里有独立实现，其源码已被覆盖，且该文件从未被 git 跟踪，无法从 git 恢复；请上文会话核对是否需重新落盘，或由总协调以功能并集合并。
- 未触碰 `DatasetViewer.tsx`、`MobileTeleopPanel.tsx`，也未删除上文任何证据（`simulated-*.jpg`、`synthetic-video.mp4`、`fixture-final-*.json`、`mock_server.py` 均保留）。
- `HybridTeleopPanel.tsx`、`HybridDataReview.tsx`、`hybridContract.ts` 仍未被 git 跟踪；`src/lib/*` 依然 0 个文件被跟踪。

### B. 本会话文件范围

| 路径 | 行数 | 说明 |
| --- | --- | --- |
| `src/components/control/hybridContract.ts` | 534 | 新增：能力/会话/动作/反馈/视频/阶段/错误的纯函数门禁 |
| `src/components/control/HybridDataReview.tsx` | 438 | 新增：同步、导出、取消、报告阶段切片、按 episode/phase 的视频与曲线 |
| `src/components/control/HybridTeleopPanel.tsx` | 494 | 重写：设备/能力/版本、六类标定、控制、反馈来源、视频、observe/grasp、录制、数据检查 |
| `src/pages/RosMobile.tsx` | 433 | 修改：删除浏览器 120 ms 键盘速度循环与 keydown/keyup；错误关闭按钮；只读提示明确浏览器不承担实时循环 |
| `frontend/dist/` | — | 新增 `assets/index-t5wavea.{js,css}` 并改 `index.html` 指向；08:16 旧产物保留未删 |

### C. 本会话验证（全部为模拟 / DOM 级，非实机、非真实浏览器）

| 检查 | 命令 | 结果 | 证据 |
| --- | --- | --- | --- |
| 契约与安全纯函数 | `node docs/evidence/parallel-20261104/T5/contract-tests.mjs` | **37/37，exit 0** | `contract-tests-result.json` |
| DOM 真实点击 | `node docs/evidence/parallel-20261104/T5/dom-clicks.cjs` | **57/57，exit 0** | `dom-clicks-result.json`、`dom-clicks-server-log.json`、`dom-snapshot.html` |
| dist 可服务性 | `node docs/evidence/parallel-20261104/T5/dist-serve-check.cjs` | **4/4，exit 0** | `dist-serve-check-result.json`、`dist-build-sha256.txt` |
| 类型检查 | `./node_modules/.bin/tsc -p tsconfig.app.json --noEmit` | 非 Training 错误 **0**；既有 `Training.tsx:438` 两处未修 | 同上文 §4.10 |
| Vite build | Node 22.22.2 与 `work/tools/node` 22.20.0 均复现 WASM OOM | **失败** | `--no-experimental-fetch` 也无效 |
| esbuild 降级构建 | `esbuild 0.28.2` + `tailwindcss` CLI | **成功** | `index-t5wavea.js` sha256 `9055f0b4…1221`、`index-t5wavea.css` `229c1e6e…12d9`、`index.html` `f3400341…0a31` |

DOM 点击覆盖：获取会话；10 个控制按钮逐个点击并核对后端收到 `action` 与会话字段；`observe`/`grasp` 只产生 phase 事件、无 control 请求；结束顺序 `pause_all` → `end_episode` 且 `success:true` 透传；同步；导出报告 observe/grasp 切片；阶段视频 `#t=0.000,4.000`；曲线渲染；导出被拒后错误经 2.6 s 轮询仍在；慢导出取消；视频冻结 2.8 s 判过期、`stale` 判不可用、恢复；能力撤回后运动禁用而急停仍可点；断网 5.2 s 后连急停也禁用；旧 epoch 报错且下一轮轮询不清除。

### D. 与上文的字段差异（需总协调裁决）

1. `end_episode`：本会话已按 T4 `test_t4_hybrid_recording.py` 改为 `{success: true|false|null, note}`，与上文一致；T3 `recording()` 记录 action，T4 再把 `success` 转成布尔。
2. `acquire`：`{action, profile, owner: clientId}`，与上文一致。
3. 阶段显示：本会话兼容 `recording.phase` 与 `recording.phases[-1].phase`（T3 实际返回 `phases` 数组，无顶层 `phase`）。
4. `stop_session`：本会话**不**先发 `pause_all`（T3 `recording()` 在 `stop_session` 分支内 `_stop`，T4 路由对 `stop_session` 也先 `pause_all`）。上文称停止录制前也先 `pause_all`；若要求前端也发，需补一行并重测。
5. 阶段视频：本会话用整集 mp4 的 `#t=from,to`；上文用 HTML media 事件限定区间。两者都不是后端阶段切片，UI 均已注明。

### E. 接口现状更新（对 4.1/4.4 的修正）

- `HybridSession.capabilities()`（`hybrid_session.py:335`）与 `recording()`（`:380`）**已经存在**，上文 4.1/4.4 的“方法缺失”已过期。
- 但 `capabilities()` 返回 `available:false`、`reason:"no_trusted_hardware_driver_bound"`，且**没有 `profiles/actions` 数组**；本会话前端因此保持全禁用（预期 fail closed）。请 T3 在硬件可用时补 `profiles/actions`，或由总协调把 `channels.*.permitted` 定为正式门禁。
- `recording().start_session` 要求 `state=="ready"`；`set_phase("grasp")` 会 `base_enabled=False`；`calibration("save")` 要求 `session_id is None`（前端已加提示）。
- 视频仍需可验证的 `frame_seq`/`preview_seq`、`valid/stale/source/age_s` 与 JPEG 同帧绑定；当前只有元数据。

### F. 环境限制

- 本会话的 Vite 构建与 `playwright install chromium`、`node --experimental-strip-types` 均因沙箱 `WebAssembly.instantiate(): Out of memory` 失败；上文 08:16 的 Vite 成功记录保留，但当前环境无法复跑。
- **没有真实浏览器截图**；本轮全部点击发生在 jsdom（真实 React 组件 + 真实 DOM 事件）。上文的 `simulated-*.jpg` 属于另一轮，不作为本节证据。
- 未验证：Vite 官方产物、真实浏览器渲染、真实后端联调（当前 `available:false`）、任何实机行为。
