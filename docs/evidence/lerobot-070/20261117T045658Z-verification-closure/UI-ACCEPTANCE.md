# 生产 bundle DOM 验收（UI-DOM-01 / K5）

本轮把上一轮"线级等价覆盖"升级为**真实浏览器对生产构建产物的交互**。上一轮结论
（`../20261116T171907Z-release-model-readiness/UI-ACCEPTANCE.md`）是在本机无法启动浏览器时
用逐字节搬运的请求构造器做的替代，本轮不再是替代。

## 1. 环境与产物

| 项 | 值 |
| --- | --- |
| 浏览器 | Chromium（Playwright 1.49.1，headless），主机 g128 |
| 被测产物 | `releases/sanitized-20261024-01/app/frontend/dist`（28 个文件） |
| 服务 | `ui_harness.py`（FastAPI）在 `127.0.0.1:8138` 提供 dist，并按真实 pydantic 模型校验提交 |
| 构建输入 | 169 个文件，清单 `frontend-inputs.sha256`，根摘要 `4dcbc7c3…092f`（build-2，含 LoRA） |
| 构建产物 | 28 个文件，清单 `dist-manifest.sha256`，根摘要 `3881c759…28ec`，入口 `assets/index-D1QziPbU.js` |

## 2. 构建可复现性

同一份输入在 **g128** 上 `npm ci`（镜像源，lockfile 强制完整性）+ `tsc --noEmit`（干净）
+ `npx vite build`（vite 8.0.16，node 24.22.0）重建，28 个文件与仓库内 dist **逐字节相同**
（两份清单 SHA-256 都是 `3881c759…28ec`）。上一版（无 LoRA）的清单根是 `77028d17…fb46`，
其输入根为 `ae1ad7cf…b50c6`，记录在 `build-inputs.json` 的 build-1 条目里。本机因 4 GiB 地址空间限制
（`WebAssembly.instantiate` OOM）无法重建，已如实记录在 `build-inputs.json`。

线上返回字节也逐一核对（`index.html`、主 JS、CSS、favicon），与 dist 清单一致。

## 3. DOM 断言（37/37 通过，`dom-acceptance.json`）

1. 落地页渲染 → 点 Training 进入 `/training`（真实路由，非 hash）。
2. 默认策略 ACT；切到 FastWAM 后**维度输入出现且为空**（不静默继承 7/8）。
3. 未选数据集时 Start 禁用。
4. 填 6/6 维 + 选择本地数据集后 Start 可用。
5. 点击提交后 POST `/jobs/training`：`policy_type=fastwam`、`policy_action_dim=6`、
   `policy_proprio_dim=6`、`dataset_repo_id=so101/pick_cube_local`，201 接受，跳转作业页。
6. 作业页显示 fake job；有 checkpoint 后 `Run on robot` 可用。
7. 推理弹窗：切到 Train server 后出现 Deployment/Semantics/State dim/Action dim/Refill/Timeout；
   维度默认 6/6；未填身份时 Start Inference 禁用。
8. 填 `so101_red`、`so101-degrees-gripper-percent-v1`、中文任务
   「把红色方块放进盒子里」、timeout 6.5 后可提交；POST `/start-inference` 200，
   中文任务原样到达，身份/单位/维度/timeout 全部与 DOM 一致，`policy_type=fastwam`，
   follower 端口来自所选机器人。
9. 硬导航 `/training` 返回 SPA 外壳（本轮修了 harness 的 SPA 回退；此前该路由返回 JSON）。
10. 清空 action dim 再提交 → 后端 400，提示
    `fastwam requires explicit dimensions … missing: ['policy_action_dim', 'policy_proprio_dim']`
    在页面上可见，且停留在表单页。

11. 打开 LoRA 开关后出现 rank/alpha 输入，默认 16/32；表单提示需要预训练策略。
12. 未填预训练路径提交 → 后端 400「LoRA fine-tuning needs a pretrained policy checkpoint」，
    提示在页面可见。
13. 填 `lerobot/fastwam_base` 再提交 → 201，wire 上 `use_peft=true`、`peft_r=16`、
    `peft_alpha=32`、`policy_pretrained_path` 原样到达。

控制台噪声只剩两类且已分类：harness 未实现的 `/ws/joint-data`（403）与**故意**制造的 400；
没有未捕获 JS 异常。

## 4. 边界

- 后端是 stub：用真实 `StartTrainingBody` / `InferenceRequest` 校验，但不启动 runner、
  不碰设备、不写数据集。它证明的是**前端契约**，不是训练/推理能跑。
- `requires_task=true` 与 6/6 维来自 stub 的 policy-config，不是从真实 checkpoint 读的。
- 未覆盖：上传页、回放页、标定页、多数据集组合；这些仍只有线级测试。
