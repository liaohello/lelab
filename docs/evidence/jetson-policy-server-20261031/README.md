# jetson-policy-server-20261031 evidence

All results are dated 2026-10-31. The Jetson server and the local observe app were stopped
after the checks; port 28081/18186 were released.

## Real hardware

| File | What it is |
| --- | --- |
| `ui-observe.md`, `ui-observe-status.json`, `ui-observe-stop.json`, `ui-observe-stopped.json` | actual LeLab `/observe` page: WebSocket, real joints, two 640×480 cameras, frame ages, `ACTIONS_SENT=0`, torque 0 |
| `ui-camera-right.jpg`, `ui-camera-up.jpg` | camera frames served by the observe API |
| `stageC-real.jsonl`, `stageC-real-summary.json`, `stageC-real.log` | real single-arm follower + real cameras + real ACT, 49 obs / 45 chunks / 4500 actions / 0 dispatched |
| `stageE-real-5min-summary.json`, `stageE-real-5min.jsonl`, `stageE-real-5min.log` | 5-minute real-observation shadow, 1440 obs / 1435 chunks / 143500 actions / 0 dispatched / 0 faults |
| `stageFault-reconnect.log`, `stageFault-reconnect.jsonl`, `stageFault-reconnect-summary.json` | stop real server mid-run → fault + queue cleared → restart → epoch 1 new session resumes |
| `resources-real-5min-summary.json`, `resources-real-5min.jsonl`, `resources-real-5min.raw.txt`, `tegrastats-real-5min.log` | Jetson container/GPU/RAM sampling |
| `env-local.txt`, `env-jetson.txt`, `contract.json`, `source-manifest.json` | environments, real ACT contract, 513-file source consistency |
| `four-arm-inventory.json` | read-only enumeration of all four serial buses: IDs 1–6 respond, torque 0; only `5B91045575` has a verified role/calibration |
| `checkpoint-12d-audit.json` | 12-D ACT candidate audit: complete 234-tensor weights / `[12]` action head, but dataset root, joint names, units and action semantics missing → compatibility **not verified**, not used |
| `hardware-roles-ui-check.md` | new `/hardware` role-confirmation page: live 4-port inventory, role-select/conflict check, simulated unplug (not physical), no save yet |
| `hardware-inventory-20261031.json` | user-saved role inventory: left/right followers and leaders confirmed |
| `hardware-role-bindings-20261031.md` | role ports applied to `left_rig`/`right_rig`, right-follower calibration imported, bimanual read-only observe development and remaining calibrations |
| `stageB2-bimanual-real-stop.json` | actual dual-follower observation: 78 frames, 12 values, two camera ages, 12 torque registers 0, `actions_sent=0`, stopped/fault-null/lease released |
| `pytest-hardware-roles-and-bimanual-observe-20261031.txt` | 61 related backend tests after role-binding and bimanual-observe changes |
| `ruff-hardware-roles-20261031.txt` | full Ruff check after these changes |

## Synthetic / protocol-only

| File | What it is |
| --- | --- |
| `pytest-jetson-policy.txt`, `pytest-observe.txt`, `pytest-lease.txt`, `pytest-lease-api.txt` | 15 policy tests + 2 observe tests + 18 lease tests + 9 lease API tests |
| `ruff-final.txt` | static checks |
| `stageC-synthetic-summary.json`, `stageC-synthetic.jsonl`, `stageC-synthetic.log` | real ACT + synthetic observations, protocol comparison only |
| `stageB-observe-blocked.txt` | historical serial-permission failure before the user granted `dialout` |
| `camera-0.jpg`, `camera-2.jpg`, `camera-capture.json` | earlier standalone camera capture |
| `server.json`, `server-stop.txt`, `server-stop-final.txt` | container/image/port/start/stop/cleanup |
| `frontend-build.txt`, `frontend-build-observe.txt` | production frontend builds |

## Labels

- “real” means the actual `/dev/ttyACM0` follower (`5B91045575`, `my_robot` calibration),
  real cameras 0/2, and the real ACT checkpoint on Jetson.
- `actions_dispatched` is 0 in every shadow run; no action was sent to a robot.
- The arm was stationary during observation/shadow, so live joint *change* was not induced.
- Only one follower was verified; the other three arms have no verified role/calibration.
- Stage D execution was not attempted: no 12-dim bimanual policy and no motion authorization.

## Re-run

See `docs/jetson-as-policy-server.md` §14. All lifecycle scripts operate only on
`lelab-policy-server-20261031`; observation requires `dialout` (use `sg dialout`).
