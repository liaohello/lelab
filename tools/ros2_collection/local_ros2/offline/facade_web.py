#!/usr/bin/env python3
"""OFFLINE ONLY: the production LeLab server with a simulated frame-facade boundary.

This is the complete `lelab.server` app -- every route the web page calls, including the
dataset read-back routes -- with the device edge replaced:

* the car is the isolated offline Docker agent, refused unless it declares
  `simulated=true` and the offline profile;
* leader, board and camera are the frozen simulated fixtures;
* the facade is the production `FrameStreamHardwareFacade` on the real frame transport.

So nothing here is a stand-in for an API the page needs, and a 200 from a route means the
production route answered it. It is still not hardware evidence: the dataset stays
`simulated_offline` and not training-eligible.
"""
from __future__ import annotations

import faulthandler
import os
import signal

from facade_smoke import (AGENT, STREAM_PORT, WORK, SyntheticUpper, car_status,
                          HybridCalibrationStore, calibration_assets_fixture,
                          FrameCarControlLease, StreamHardwareObserver,
                          FrameStreamHardwareFacade, FrameStreamDriver,
                          configure_hybrid_facade)

# Set before importing the route modules: they snapshot these paths at import time.
os.environ["LELAB_ROS_AGENT_URL"] = AGENT
os.environ["LELAB_ROS_COLLECTION_ROOT"] = str(WORK / "web-upper")
os.environ["HF_LEROBOT_HOME"] = str(WORK / "web-datasets")
os.environ.setdefault("LELAB_HYBRID_TRANSPORT", "frame_stream_v1")

from lelab import ros_mobile  # noqa: E402
from lelab import server as production  # noqa: E402
import uvicorn  # noqa: E402


def build_simulated_facade() -> FrameStreamHardwareFacade:
    status = car_status()  # validates profile and simulated identity before any lease
    if (status.get("control") or {}).get("active"):
        raise RuntimeError("offline_agent_already_has_a_lease")
    store = HybridCalibrationStore(WORK / "calibration")
    assets = calibration_assets_fixture(measured_at_utc="2026-11-04T00:00:00Z")
    ids = {kind: assets[kind]["identity"] for kind in ("leader", "follower", "mapping")}
    upper = SyntheticUpper(agent_url=AGENT, store=store, leader_identity=ids["leader"])
    car = FrameCarControlLease(AGENT, expected_profile="hardware_hybrid_manual_v1_offline6d")
    observer = StreamHardwareObserver(upper=upper, agent_url=AGENT, store=store,
        leader_identity=ids["leader"], follower_identity=ids["follower"],
        mapping_identity=ids["mapping"], simulated_source=True)
    car.observer = observer
    facade = FrameStreamHardwareFacade(upper=upper, car=car, observer=observer,
                                       calibration_store=store)
    facade.bind_driver(FrameStreamDriver(car, observer, AGENT, host="127.0.0.1", port=STREAM_PORT))
    facade.simulated_context = {"simulated": True, "data_identity": "simulated_offline",
                                "training_eligible": False}
    # The page reaches the car-side recorder through the production routes only when
    # this explicit offline recording flag is set; hardware profiles never see it.
    facade.offline_agent_recording = True
    return configure_hybrid_facade(facade=facade, replace=True)


def create_app():
    # The routes read the process-wide facade through ros_mobile.get_hybrid_facade(); the
    # coordinator keeps the same object so the legacy bridge and the hybrid routes share
    # one lease.
    ros_mobile._HYBRID_FACADE = build_simulated_facade()
    ros_mobile._HYBRID_RECORDING = None
    return production.app


if __name__ == "__main__":
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    uvicorn.run(create_app(), host="127.0.0.1",
                port=int(os.environ.get("LROS_WEB_PORT", "18288")))
