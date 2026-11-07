#!/usr/bin/env python3
"""OFFLINE ONLY - serve the production web app over the diagnostic homing facade.

The production ``/landerpi/teleop`` page is built against the hardware facade, which
binds a real SO-101 leader on acquire. This harness replaces only that leader edge
with the diagnostic's simulated lease and points the real FrameLink at the isolated
Humble car stack, so a browser click drives the same backend, frame link, control
process and simulated board as the continuous diagnostic.

Nothing here opens a serial port. The facade is explicitly simulated and the car
stack must already be running with the same bundle (see homing_offline_diagnostic.sh).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "app_ros_mobile"), str(ROOT / "tools/ros2_collection")]

from homing_offline_diagnostic import build_facade, prepare_store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-url", default="http://127.0.0.1:18131")
    parser.add_argument("--frame-port", type=int, default=18123)
    parser.add_argument("--calibration-root", required=True)
    parser.add_argument("--port", type=int, default=18328)
    args = parser.parse_args()
    calibration_root = Path(args.calibration_root).expanduser().resolve()
    os.chdir(ROOT / "app_ros_mobile")

    store, identities = prepare_store(calibration_root)
    facade, _observer, _upper = build_facade(args.agent_url, args.frame_port, store, identities)
    if facade.observer.calibration_readiness().get("ready") is not True:
        print("refusing: calibration fixture is not ready", file=sys.stderr)
        return 2

    from lelab import ros_mobile

    ros_mobile.get_hybrid_facade = lambda: facade  # noqa: E731 - explicit test seam
    import uvicorn
    from lelab.server import app

    print(f"serving web probe on http://127.0.0.1:{args.port} (simulated upper, "
          f"car={args.agent_url}, frame={args.frame_port})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning",
                access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())