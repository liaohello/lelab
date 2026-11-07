#!/usr/bin/env python3
"""六's last locally-reachable case: the car-side control lease expires on its own.

The requirement says the car must run its own TTL/monotonic stop logic, and that "the upper computer
stopped sending" is not evidence that the arm stopped. Every earlier rehearsal kept the lease alive
(a few seconds of stall against a 30 s lease). This one waits for the lease to actually die while
the agent stays running, which is what a dead laptop or a killed SSH session looks like from the
car's side, and checks the three things that have to hold afterwards: the car stops treating anyone
as the controller, the targets that were still queued at expiry are flushed instead of bursting out
on the next valid tick, and a command carrying the expired token is refused.

Run `local_car_rehearsal.py --root <root>` first, then:

    app_ros_mobile/.venv/bin/python tools/ros2_collection/local_car_lease_expiry_rehearsal.py --root <root>

Leader and follower feedback are synthetic and nothing is attached, so no motion can happen here;
this measures the gateway's bookkeeping, not servo behaviour. Exits non-zero with a named reason.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for path in (REPO / "tools" / "ros2_collection", REPO / "app_ros_mobile"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from local_teleop_session_drive import SyntheticLeader, seed  # noqa: E402

from lelab.hardware_lease import HardwareLeaseManager  # noqa: E402
from lelab.mobile_teleop import CalibrationStore  # noqa: E402
from lelab.mobile_teleop_coordinator import (BridgeError, CoordinatorConfig,  # noqa: E402
                                             MobileTeleopCoordinator)

PORT = "/dev/serial/by-id/synthetic-if00"
IDENTITIES = {"leader": "leader_local", "follower": "car_local", "mapping": "map_local"}


def car_control(url: str) -> dict:
    with urllib.request.urlopen(url + "/status", timeout=2.0) as response:
        body = json.loads(response.read())
    control = body.get("control") or {}
    teleop = control.get("teleop") or {}
    return {"active": control.get("active"), "client_id": control.get("client_id"),
            "mode": control.get("control_mode"), "expires_in_s": control.get("expires_in_s"),
            "stream": teleop.get("stream"), "last": teleop.get("last"),
            "rejected": teleop.get("rejected")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/lelab_car_rehearsal")
    ap.add_argument("--margin-seconds", type=float, default=2.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    root = Path(args.root)
    marker = json.loads((root / "READY_FOR_DRIVER.json").read_text(encoding="utf-8"))
    url = f"http://127.0.0.1:{marker['port']}"
    profile = json.loads((REPO / "configs" / "ros2_collection" / "hardware-teleop-v1.json")
                         .read_text(encoding="utf-8"))
    scratch = root / "upper"
    (scratch / "cal").mkdir(parents=True, exist_ok=True)
    store = CalibrationStore(scratch / "cal")
    seed(store, IDENTITIES)
    leader = SyntheticLeader(PORT)
    coord = MobileTeleopCoordinator(
        CoordinatorConfig(agent_url=url, command_hz=10.0, lease_ttl_s=60.0, control_ttl_s=30.0,
                          request_timeout_s=5.0, leader={"read_hz": 30.0}),
        profile, store=store, leader_reader=leader, lease_manager=HardwareLeaseManager())

    steps: dict = {"agent_url": url}
    coord.open_leader(PORT, leader_identity=IDENTITIES["leader"])
    coord.acquire(owner="lease-expiry-rehearsal", leader_identity=IDENTITIES["leader"],
                  follower_identity=IDENTITIES["follower"], mapping_identity=IDENTITIES["mapping"])
    coord.set_state("enter_preview")
    coord.align()
    coord.set_state("arm")
    coord.set_state("engage")
    coord.start_follow_loop()
    deadline = time.monotonic() + 15.0
    live = car_control(url)
    while time.monotonic() < deadline and not (live.get("stream") or {}).get("live"):
        time.sleep(0.1)
        live = car_control(url)
    with coord.lock:
        token = coord.token
    steps["while_leased"] = {"state": coord.status()["state"], "seq": coord.status()["seq"],
                             "car": live}

    # Stop renewing: no follow loop, no heartbeat. The car decides, on its own clock, that nobody
    # is in control any more.
    coord.stop_follow_loop()
    ttl = live.get("expires_in_s") or 5.0
    steps["waited_s"] = round(ttl + args.margin_seconds, 2)
    time.sleep(ttl + args.margin_seconds)
    expired = car_control(url)
    steps["after_expiry"] = {"car": expired, "upper_state": coord.status()["state"],
                             "upper_seq": coord.status()["seq"]}

    late: dict = {}
    try:
        late = {"result": coord._transport("/control/arm", "POST", {
            "session_id": token, "seq": 999999, "source": "lease_expiry_rehearsal",
            "mode": "leader_arm", "ttl_s": 0.5, "arm": {"positions": [0.0] * 5}}, token)}
    except BridgeError as exc:
        late = {"raised": f"{exc.status}:{exc.detail}"}
    steps["command_with_the_expired_token"] = late

    # Recovery has to be explicit: re-acquire and re-align, and the arm stream may only come back
    # with a target produced after that - never a flush of what was queued when the lease died.
    coord.release(reason="lease-expiry rehearsal")
    coord.acquire(owner="lease-expiry-rehearsal-2", leader_identity=IDENTITIES["leader"],
                  follower_identity=IDENTITIES["follower"], mapping_identity=IDENTITIES["mapping"])
    coord.set_state("enter_preview")
    coord.align()
    coord.set_state("arm")
    coord.set_state("engage")
    after_resume_last = (car_control(url).get("last") or {})
    coord.start_follow_loop()
    time.sleep(1.2)
    resumed = car_control(url)
    steps["after_reacquire"] = {"state": coord.status()["state"], "seq": coord.status()["seq"],
                                "car": resumed, "last_before_following": after_resume_last}
    coord.stop_follow_loop()
    coord.release(reason="lease-expiry rehearsal done")
    coord.shutdown(reason="lease-expiry rehearsal exit")

    expectations = {
        "the_lease_was_live_before_it_expired": live.get("active") is True
                                               and (live.get("stream") or {}).get("live") is True,
        "the_car_dropped_control_on_its_own_clock": expired.get("active") is False
                                                    and expired.get("client_id") is None,
        "the_expired_lease_took_the_arm_stream_with_it": (expired.get("stream") or {}).get("expected") is False
                                                          and (expired.get("stream") or {}).get("live") is False,
        # Either answer proves the car will not take it: the token is gone (403) or the session
        # is known to be expired (409). Which one answers is not the property - being refused is.
        "the_car_refused_a_command_with_the_expired_token": str(late.get("raised", ""))[:3] in ("403", "409"),
        # Recovery was only possible by re-acquiring under a new client and re-aligning: the
        # lease is gone, so the previous holder cannot keep sending.
        "recovery_needed_a_fresh_acquire": expired.get("client_id") is None
                                          and resumed.get("client_id") == "lease-expiry-rehearsal-2",
        "nothing_queued_burst_out_on_recovery": json.dumps(resumed.get("last"), sort_keys=True, default=str)
                                                != json.dumps(expired.get("last"), sort_keys=True, default=str)
                                                and (resumed.get("stream") or {}).get("live") is True,
    }
    expectations["failed"] = [k for k, v in expectations.items() if v is False]
    out = {"steps": steps, "expectations": expectations}
    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 1 if expectations["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
