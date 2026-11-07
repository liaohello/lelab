"""Upper-host teleop HTTP surface: routing, error mapping and the no-output preview claim.

No car, no serial port and no actuator is touched: the coordinator and the store root are stubbed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lelab import mobile_leader_reader, mobile_teleop, mobile_teleop_coordinator, ros_mobile


class StubCoordinator:
    def __init__(self):
        self.calls = []
        self.state_events = []

    def status(self):
        self.calls.append("status")
        return {"state": "disconnected", "leader": {"connected": False}, "preview": {}}

    def preview_step(self):
        self.calls.append("preview_step")
        return {"outputs_actuators": False, "targets": {"joint1": 0.0}, "feedback": {"available": False}}

    def readiness_report(self):
        return {"ready": False, "reasons": ["leader_serial_lease_missing"]}

    def set_state(self, event):
        self.state_events.append(("set_state", event))
        if event == "engage":
            raise mobile_teleop_coordinator.BridgeError(409, "car_lease_invalid")
        return {"state": "armed"}

    def align(self):
        self.state_events.append(("align", None))
        raise mobile_teleop_coordinator.BridgeError(409, "align blocked: follower_feedback_missing")

    def emergency_stop(self, reason):
        self.state_events.append(("estop", reason))
        return {"state": "estop", "sent_estop": True}

    def heartbeat(self):
        self.state_events.append(("heartbeat", None))
        return {"ok": True}

    def acquire(self, owner, leader_identity, follower_identity, mapping_identity):
        self.calls.append(("acquire", owner, leader_identity, follower_identity, mapping_identity))
        raise mobile_teleop_coordinator.BridgeError(409, "acquire reached the coordinator")

    def open_leader(self, port, ids=None, leader_identity=None):
        self.calls.append(("open_leader", port, ids, leader_identity))
        connected = port.startswith("/dev/")
        return {"leader": {"connected": connected, "port": port,
                           "reason": None if connected else "no such device"}}

    def close_leader(self):
        self.calls.append(("close_leader", None, None))
        return {"leader": {"connected": False}}

    def recording(self, action, payload=None):
        self.calls.append(("recording", action, json.dumps(payload or {})))
        return {"session_id": "ros_session", "episode_id": "ep_1"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    stub = StubCoordinator()
    monkeypatch.setattr(mobile_teleop_coordinator, "get_mobile_teleop_coordinator", lambda: stub)
    monkeypatch.setattr(mobile_teleop, "default_store_root", lambda: tmp_path / "cal")
    monkeypatch.setattr(mobile_leader_reader, "list_leader_ports",
                        lambda base="/dev/serial/by-id": [{"by_id": "/dev/serial/by-id/usb-1a86_X-if00",
                                                           "device": "/dev/ttyACM0", "serial": "X"}])
    app = FastAPI()
    app.include_router(ros_mobile.router)
    return TestClient(app), stub


def test_preview_route_reports_no_actuator_output(client):
    http, stub = client
    response = http.get("/ros-mobile/teleop/preview")
    assert response.status_code == 200
    assert response.json()["outputs_actuators"] is False
    assert "preview_step" in stub.calls


def test_devices_route_lists_leader_ports_and_states_serial_ownership(client):
    http, _ = client
    body = http.get("/ros-mobile/teleop/devices").json()
    # The page renders one option per by-id entry, so the contract is a list of records.
    assert body["ports"] == [{"by_id": "/dev/serial/by-id/usb-1a86_X-if00", "device": "/dev/ttyACM0",
                              "serial": "X"}]
    assert "not network reachability" in body["note"]


def test_leader_calibrations_route_reuses_the_existing_so101_wizard_files(client, tmp_path, monkeypatch):
    """Layer A has to be fillable from the calibration the existing wizard already wrote.

    The route reads this host's lerobot calibration tree, so the shape pinned here is the one
    lerobot actually produces (five fields per motor, six names) rather than a convenient subset.
    """
    from lelab import mobile_leader_reader
    from lelab.utils import config as lelab_config

    motors = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]
    shape = {name: {"id": index + 1, "drive_mode": 0, "homing_offset": 1467 - index,
                    "range_min": 796, "range_max": 3216} for index, name in enumerate(motors)}
    tree = tmp_path / "teleoperators" / "so_leader"
    tree.mkdir(parents=True)
    rig = tree / "right_rig.json"
    rig.write_text(json.dumps(shape), encoding="utf-8")
    monkeypatch.setattr(lelab_config, "CALIBRATION_BASE_PATH_TELEOP", str(tmp_path / "teleoperators"))

    http, _ = client
    body = http.get("/ros-mobile/teleop/leader-calibrations").json()
    assert body["root"].endswith("teleoperators") and body["exists"] is True
    entry = body["entries"][0]
    assert entry["robot_type"] == "so_leader" and entry["usable"] is True
    assert entry["calibration"] == shape, "the page fills the block the wizard produced, unchanged"
    assert entry["motors"] == {name: body_["id"] for name, body_ in shape.items()}

    # A partial file is listed but never offered, so the operator cannot save it as layer A.
    (tree / "left_rig.json").write_text(json.dumps({k: v for k, v in shape.items() if k != "gripper"}),
                                        encoding="utf-8")
    listed = {e["name"]: e for e in http.get("/ros-mobile/teleop/leader-calibrations").json()["entries"]}
    assert listed["left_rig"]["usable"] is False and "calibration" not in listed["left_rig"]
    assert listed["left_rig"]["missing_motors"] == ["gripper"]
    assert (tmp_path / "teleoperators" / "so_leader" / "left_rig.json").read_text(encoding="utf-8") == \
        json.dumps({k: v for k, v in shape.items() if k != "gripper"}), "the route must stay read-only"

def test_leader_open_and_close_are_routed(client):
    http, stub = client
    opened = http.post("/ros-mobile/teleop/leader",
                       json={"action": "open", "port": "/dev/ttyACM0", "leader_identity": "so101-leader-5B90149652"})
    assert opened.status_code == 200 and opened.json()["leader"]["connected"] is True
    assert stub.calls[0] == ("open_leader", "/dev/ttyACM0", None, "so101-leader-5B90149652"), stub.calls
    assert http.post("/ros-mobile/teleop/leader", json={"action": "nope"}).status_code == 400
    assert http.post("/ros-mobile/teleop/leader", json={"action": "close"}).json()["leader"]["connected"] is False


def test_bridge_errors_carry_their_status_and_reason(client):
    http, stub = client
    engage = http.post("/ros-mobile/teleop/state", json={"event": "engage"})
    assert engage.status_code == 409
    assert engage.json()["detail"] == "car_lease_invalid"
    align = http.post("/ros-mobile/teleop/state", json={"event": "align"})
    assert align.status_code == 409 and "follower_feedback_missing" in align.json()["detail"]
    estop = http.post("/ros-mobile/teleop/state", json={"event": "estop", "reason": "operator"})
    assert estop.json() == {"state": "estop", "sent_estop": True}
    assert stub.state_events[-1] == ("estop", "operator")
    acquire = http.post("/ros-mobile/teleop/acquire",
                        json={"owner": "op", "leader_identity": "l", "follower_identity": "f",
                              "mapping_identity": "m"})
    assert acquire.status_code == 409 and "acquire reached" in acquire.json()["detail"]


def test_leader_is_opened_before_any_lease_is_taken(client):
    """A session must never hold both leases while the leader cannot be read."""
    http, stub = client
    missing = http.post("/ros-mobile/teleop/acquire",
                        json={"owner": "op", "port": "not-a-port", "leader_identity": "l",
                              "follower_identity": "f", "mapping_identity": "m"})
    assert missing.status_code == 409
    assert "leader_open_failed" in missing.json()["detail"]
    assert [c[0] for c in stub.calls] == ["open_leader"], stub.calls
    http.post("/ros-mobile/teleop/acquire",
              json={"owner": "op", "port": "/dev/ttyACM0", "leader_identity": "l",
                    "follower_identity": "f", "mapping_identity": "m"})
    assert [c[0] for c in stub.calls][-2:] == ["open_leader", "acquire"], stub.calls
    assert stub.calls[0][1] == "not-a-port"
    # The acquire path must forward the leader identity, otherwise the reader
    # cannot bind the calibration it is about to open.
    assert [c[3] for c in stub.calls if c[0] == "open_leader"] == ["l", "l"], stub.calls


def test_recording_joins_the_existing_teleop_session_route(client):
    http, stub = client
    body = http.post("/ros-mobile/teleop/recording", json={"action": "start_episode", "task": "teleop"}).json()
    assert body["episode_id"] == "ep_1"
    entry = [c for c in stub.calls if c[0] == "recording"][-1]
    assert entry[1] == "start_episode"
    assert "task" in entry[2] and "action" not in entry[2], "the route action must not leak into the payload"


def test_calibration_roundtrip_through_the_routes(client):
    http, _ = client
    assert http.post("/ros-mobile/teleop/calibration",
                     json={"kind": "nonsense", "identity": "a", "payload": {}}).status_code == 400
    assert http.post("/ros-mobile/teleop/calibration",
                     json={"kind": "leader", "identity": "", "payload": {}}).status_code == 400
    saved = http.post("/ros-mobile/teleop/calibration", json={
        "kind": "leader", "identity": "leader_A",
        "payload": {"joint_order": ["shoulder_pan"], "device_identity": {"serial": "X"}}}).json()
    assert saved["version"] and saved["sha256"]
    loaded = http.get("/ros-mobile/teleop/calibration", params={"kind": "leader", "identity": "leader_A"}).json()
    assert loaded["data"]["device_identity"]["serial"] == "X"
    assert http.get("/ros-mobile/teleop/calibration",
                    params={"kind": "bogus", "identity": "leader_A"}).status_code == 400
    # the missing layers are reported, never defaulted
    ready = http.get("/ros-mobile/teleop/calibration/readiness",
                     params={"leader": "leader_A", "follower": "car_A", "mapping": "map_A"}).json()
    assert ready["ready"] is False and ready["reasons"]
    tampered = Path(loaded["path"])
    payload = json.loads(tampered.read_text())
    payload["device_identity"]["serial"] = "forged"
    tampered.write_text(json.dumps(payload))
    assert http.get("/ros-mobile/teleop/calibration",
                    params={"kind": "leader", "identity": "leader_A"}).status_code == 409
