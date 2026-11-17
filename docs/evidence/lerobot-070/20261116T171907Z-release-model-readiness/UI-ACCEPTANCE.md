# 生产 bundle 界面与 API 验收（R2 / TC-UI-01、TC-UI-02）

源码版本：bundle 由 `b9b9a95` 前的源码构建（构建收据 `../20261116T145909Z-m4m6/vite-build-remote.txt`，
`BUILD_EXIT=0`，输出 `index-BRxloIhH.js` / `index-OI39ixo9.css` 与 dist 实际文件一致）。
本轮验证起点 `5daa788`。验收期间无前端源码改动（`find src -newer dist/index.html` 为空），
bundle 与源码保持一致；tsc 空输出通过（`../20261116T145909Z-m4m6/tsc.txt`）。

## 1. 构建来源链路（TC-UI-02，复用 + 核对）

| 环节 | 证据 | 结论 |
| --- | --- | --- |
| 构建命令 | vite v8.0.16，g128 node v24.22.0（本机 4 GiB RLIMIT_AS 无法跑 undici Wasm） | 收据完整 |
| 输出哈希 | dist `index-BRxloIhH.js`/`index-OI39ixo9.css` 与收据一致 | 一致 |
| 输入未变 | 无 src 文件新于 dist | 无需重建 |
| bundle 含新契约 | `policy_action_dim`/`policy_proprio_dim`/`remote_deployment`/`remote_semantics`/`remote_action_dim`/fastwam/fineart_vla 均在 bundle 中 | 已含 |

## 2. 表单 → API 线级验收（TC-UI-01 的可自动化部分）

方法：`configToRequest` 从 `src/pages/Training.tsx` **逐字节提取**（提取件 diff 校验
`byte-identical: True`），经 tsc 转译后与前端自身的 `startTrainingJob`（`src/lib/jobsApi.ts`）
一起，在 g128 node v24（本机 Node 22 的 undici Wasm 受 4 GiB 地址空间限制无法启动）
驱动工作站上的 harness（`ui_harness.py`）。harness 用**真实** `StartTrainingBody.from_legacy`
（即生产 `/jobs/training` 的入口）+ 真实 FastWAM 启动门禁（`build_training_command`）+
**真实** `InferenceRequest` schema 校验；fake runner，从不 spawn。远程推理场景与
`InferenceModal.tsx` 268–285 行的字段逐一对应（含 `target === "remote"` 条件展开）。
两机之间仅经 SSH 回环转发（`ssh -R 127.0.0.1:8137`）。

| # | 场景 | 预期 | 实测 |
| --- | --- | --- | --- |
| 1 | FastWAM 6/6 维 | 201，维度入 config | **201**，`policy_action_dim=6` |
| 2 | FastWAM 缺维度 | 拒绝，门禁原文 | **400**："fastwam requires explicit dimensions … missing: ['policy_action_dim','policy_proprio_dim']" |
| 3 | 维度配到 act | configToRequest 应剥离维度 | **201 且请求中无维度字段**（后门禁另有 app 套件覆盖） |
| 4 | FineART 配置 | 201 | **201**（训练契约本无自由文本 task；中文 task 属 rollout 请求，见 #6） |
| 5 | FastWAM + ssh_remote | 201，target 携带 | **201**，`target.runner="ssh_remote"` |
| 6 | 远程推理完整字段（deployment/单位语义/6维/refill/timeout/中文外 task） | 200 | **200**，echo：`deployment=armA action_dim=6 task=pick the red block` |
| 7 | 远程推理缺 deployment/semantics | 拒绝 | **422**："Remote inference requires remote_deployment and remote_semantics; the 0.7 server never g…" |

原始记录：`driver-report.txt`（客户端视角）、`ui_requests.jsonl`（harness 收到的逐请求
body + 状态）、`driver.mjs`、`ui_harness.py`。

## 3. 浏览器 DOM 交互（TC-UI-01 的其余部分）——blocked

本机无可用浏览器自动化：Playwright/Chromium 未安装且 Chromium 在 4 GiB
`RLIMIT_AS` 下无法运行；系统 Firefox 为 snap 包，在沙箱能力集下无法启动
（"snap-confine … cap_dac_override not found"）。已通过的等价覆盖：

- 表单状态 → 请求体：`configToRequest` 逐字节提取 + 前端自身 API 客户端（上表）；
- 请求体 → 后端契约：真实 pydantic schema 校验（上表 #1–#7）；
- 加载渲染与人工交互：harness 常驻 `127.0.0.1:8137` 服务生产 bundle（`GET /` 200，
  `GET /assets/index-BRxloIhH.js` 200），可通过 WorkBuddy 内置浏览器预览人工操作。

最小下一步：在无地址空间限制的机器上安装 Playwright+Chromium，把
`ui_harness.py` 指向真实后端路由，按上表场景加 DOM 断言（约半日工作量）。
