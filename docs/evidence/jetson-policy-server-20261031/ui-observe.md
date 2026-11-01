# Read-only observation UI evidence (2026-10-31)

- URL: `http://127.0.0.1:18186/observe` (client-side route from `/`).
- The app ran under `sg dialout`; port 18186.
- After clicking “开始观测”:
  - WebSocket `/ws/joint-data`: **connected**, 45 `joint_update` messages observed.
  - Joints (real follower, degrees/0-100): shoulder_pan 6.154, shoulder_lift -101.978,
    elbow_flex 96.967, wrist_flex 74.637, wrist_roll -67.429, gripper 0.484.
  - Both camera images loaded: 640×480 (`right`, `up`).
  - Camera frame age: ~3.6 ms; 45 frames while observing.
  - `ACTIONS_SENT`: **0**; all six `Torque_Enable` registers: **0**.
- Stop response: `active=false`, `phase=stopped`, `frames=108`, `actions_sent=0`, no fault.
- Camera JPEGs: `ui-camera-right.jpg`, `ui-camera-up.jpg`.
- Limitation: the arm was stationary during this check, so a physical joint *change* was not
  induced; live updates and frame ages were verified, not motion tracking.
