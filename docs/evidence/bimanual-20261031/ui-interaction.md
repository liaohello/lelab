# Browser checks — 2026-10-31

Environment: loopback only (`127.0.0.1:18181` real app, `127.0.0.1:18182` UI mock),
isolated root `/tmp/lelab-bimanual-test`, four fake calibration JSON files.
No serial port, camera, robot or LeRobot writer was opened by these browser checks.

## Bimanual setup (real app)

- `/bimanual` loaded with `left_rig` / `right_rig` records. Page shows device ports and
  calibration filenames; camera configuration stays collapsed until
  “配置并预览相机” is clicked.
- “核验配置” displayed four devices and sha256 prefixes (left/right leader/follower).
- Removing `right_follower.json` and re-running validation displayed:
  `配置错误 — Missing right follower calibration file: .../right_follower.json. Calibrate that arm first.`
- With zero cameras the “开始双臂采集” button stayed disabled; the page copy states
  selection/task/camera requirements. This is the start-protection check.
- Camera scan/preview is explicit: no `/available-cameras` request occurred on page
  load; clicking “配置并预览相机” mounted `Camera Configuration` and produced exactly
  one `GET /available-cameras` request (no physical camera opened because the isolated
  host has none).

## Recording page (mock API; UI-state verification only)

Mock server simulated a bimanual session with same-cycle left/right joint data,
three camera health entries, and the backend phase machine. Observed:

1. Initial: `RECORDING EPISODE 2`, “左工作臂 同一周期 #5”, “右工作臂 同一周期 #5”,
   six degree values per arm, `front: 13 ms`, `left_wrist: 28 ms`, `right_wrist: 31 ms`,
   primary button `保存本集并继续`.
2. Save click → `RESET — GET READY`, button `进入下一集`.
3. Menu “丢弃本集并重录” click → mock log recorded
   `POST /recording-rerecord-episode 200` (the discard path).
4. Stop (`POST /stop-recording`) → `STOPPING — SAVING CURRENT SEGMENT`,
   then `VERIFYING DATASET`; only after `finalize_state=completed` did the page
   navigate to `/upload` with “Episodes Recorded: 2”.
5. 3D URDF canvases showed `Disconnected` in this hidden browser viewport (the mock
   WebSocket was not attached); same-cycle numeric pose came from `/recording-status`
   and was verified. Real WebSocket broadcast is implemented but not hardware-tested.

## Backend validation (real app)

`POST /bimanual/validate` returned `success: true` with `mode=bimanual`,
`sync=software_synchronized_sequential_bus_io`, 12 fixed joint names, and four
device entries with role-correct calibration paths and sha256. Cameras were empty
in this check; the page then kept the start button disabled.

## Limits

- Mock recording page validates UI state transitions, not LeRobot writing or hardware.
- Real writer behavior is covered separately by the synthetic `pytest` case.
- Real four-arm motion, camera USB bandwidth, E-stop and lease-under-failure remain
  unverified and require authorized hardware.
