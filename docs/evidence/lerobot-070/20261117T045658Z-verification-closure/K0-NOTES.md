# K0 — 运行时差异与活动预览的处置

- 起点 `7b1667d` 时，`docs/evidence/lerobot-070/20261116T171907Z-release-model-readiness/ui_requests.jsonl`
  已有 11 行未提交的运行时追加（该文件被 `127.0.0.1:8137` 上的生产 bundle 预览持续写入）。
- 本轮**没有** reset、checkout 或覆盖它；提交记录里也不包含该文件的新内容。
- 为可复核，已把当时内容冻结为本目录的 `preview-8137-ui_requests.snapshot.jsonl`：
  19 行，SHA-256 `29433f4f3de3f3cd9198b681225f2fb1c90717a90791b1f8b223882912f46583`。
- 本轮自己的浏览器验收改用隔离运行目录：`ui_harness.py` 把请求写到
  `docs/evidence/lerobot-070/20261117T045658Z-verification-closure/ui_requests.jsonl`
  （g128 上为 `/home/lfy/lelab-rmr/closure/ui_requests.jsonl`），不再写已冻结的旧证据目录。
- `127.0.0.1:8137` 的预览进程与用户页面全程未被停止。
