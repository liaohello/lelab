"""Upper-host HYBRID integration: real routes, explicit adapter injection only."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lelab import mobile_teleop_coordinator as coordinator
from lelab import ros_mobile
from lelab.hybrid_facade import HybridPublicFacade
from lelab.hybrid_fixtures import build_fixture_facade
from lelab.hybrid_recording import HybridRecordingController


@pytest.fixture
def hybrid_route(monkeypatch):
    """Configure the production facade through the coordinator with explicit fixtures."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import t4_fixtures as fx

    seed = build_fixture_facade()
    monkeypatch.setattr(coordinator, "_hybrid_facade", coordinator._hybrid_facade)
    facade = coordinator.configure_hybrid_facade(
        upper=seed.upper,
        car=seed.car,
        observer=seed.observer,
        driver=seed.driver,
        clock_ns=seed.clock_ns,
        gamepad_deadzone=0.1,
        gamepad_timeout_ns=300_000_000,
    )
    facade.simulated_context = seed.simulated_context

    monkeypatch.setattr(ros_mobile, "_HYBRID_FACADE", None)
    monkeypatch.setattr(ros_mobile, "_HYBRID_RECORDING", None)
    car = fx.FakeCar()
    controller = HybridRecordingController(
        facade, car, clock_ns=lambda: 2_000_000_000,
        wall_ns=lambda: 2_000_000_001, background=False)
    facade.bind_recording_controller(controller)
    monkeypatch.setattr(ros_mobile, "_HYBRID_RECORDING", controller)

    app = FastAPI()
    app.include_router(ros_mobile.router)
    client = TestClient(app)
    return client, facade, controller, car


def _acquire(client: TestClient) -> tuple[str, int]:
    response = client.post("/ros-mobile/hybrid/session",
                           json={"action": "acquire", "owner": "integration-test"})
    assert response.status_code == 200, response.text
    payload = response.json()
    return payload["session_id"], payload["session_epoch"]


def _identity(session_id: str, epoch: int) -> dict[str, object]:
    return {"session_id": session_id, "session_epoch": epoch}


def test_production_routes_reject_old_identity_but_estop_can_preempt(hybrid_route):
    client, facade, _controller, car = hybrid_route
    assert isinstance(ros_mobile.get_hybrid_facade(), HybridPublicFacade)

    first_id, first_epoch = _acquire(client)
    preview = client.post("/ros-mobile/hybrid/control",
                          json={"action": "preview",
                                **_identity(first_id, first_epoch)})
    assert preview.status_code == 200, preview.text
    assert preview.json()["arm_state"] == "preview"
    assert preview.json()["outputs_enabled"] is False
    assert preview.json()["motion_commands_sent"] == 0
    assert preview.json()["command_submitted"] is False
    assert facade.driver.submitted == []

    released = client.post("/ros-mobile/hybrid/session",
                           json={"action": "release",
                                 **_identity(first_id, first_epoch)})
    assert released.status_code == 200, released.text
    second_id, second_epoch = _acquire(client)
    assert (second_id, second_epoch) != (first_id, first_epoch)

    old_identity = _identity(first_id, first_epoch)
    car_calls_before = len(car.calls)
    stale_control = client.post("/ros-mobile/hybrid/control",
                                json={"action": "arm", **old_identity})
    assert stale_control.status_code == 409
    stale_recording = client.post("/ros-mobile/hybrid/recording",
                                  json={"action": "start_session", **old_identity})
    assert stale_recording.status_code == 409
    stale_calibration = client.post(
        "/ros-mobile/hybrid/calibration",
        json={"action": "validate", "kind": "base", "payload": {}, **old_identity})
    assert stale_calibration.status_code == 409
    stale_release = client.post("/ros-mobile/hybrid/session",
                                json={"action": "release", **old_identity})
    assert stale_release.status_code == 409
    assert len(car.calls) == car_calls_before
    current = client.get("/ros-mobile/hybrid/status").json()
    assert (current["session_id"], current["session_epoch"]) == (second_id, second_epoch)
    assert current["state"] == "ready"

    missing_identity = client.post("/ros-mobile/hybrid/control",
                                   json={"action": "preview"})
    invalid_epoch = client.post("/ros-mobile/hybrid/control",
                                json={"action": "preview", "session_id": second_id,
                                      "session_epoch": True})
    assert missing_identity.status_code == 409
    assert invalid_epoch.status_code == 409
    missing_calibration_identity = client.post(
        "/ros-mobile/hybrid/calibration",
        json={"action": "validate", "kind": "base", "payload": {}})
    assert missing_calibration_identity.status_code == 409
    current_calibration = client.post(
        "/ros-mobile/hybrid/calibration",
        json={"action": "validate", "kind": "base", "payload": {},
              **_identity(second_id, second_epoch)})
    assert current_calibration.status_code == 200

    # E-stop is deliberately the one preemption path without a session identity.
    estop = client.post("/ros-mobile/hybrid/control",
                        json={"action": "estop", "reason": "stale-client-preemption"})
    assert estop.status_code == 200, estop.text
    assert estop.json()["estop"] is True
    status = client.get("/ros-mobile/hybrid/status").json()
    assert status["state"] == "estop"
    assert (status["session_id"], status["session_epoch"]) == (second_id, second_epoch)

    # Clearing a latch is not a preemption exception: a stale or absent identity
    # must not clear the current operator's stop.
    assert client.post("/ros-mobile/hybrid/control",
                       json={"action": "clear_estop", **old_identity}).status_code == 409
    assert client.post("/ros-mobile/hybrid/control",
                       json={"action": "clear_estop"}).status_code == 409
    cleared = client.post("/ros-mobile/hybrid/control",
                          json={"action": "clear_estop",
                                **_identity(second_id, second_epoch)})
    assert cleared.status_code == 200, cleared.text
    assert facade.estop_latched is False


def test_recording_controller_is_the_single_public_recording_source(hybrid_route):
    client, facade, controller, car = hybrid_route
    session_id, epoch = _acquire(client)
    identity = _identity(session_id, epoch)

    started = client.post("/ros-mobile/hybrid/recording",
                          json={"action": "start_session", **identity})
    assert started.status_code == 200, started.text
    episode = client.post("/ros-mobile/hybrid/recording",
                          json={"action": "start_episode", "episode_id": "ep-upper",
                                **identity})
    assert episode.status_code == 200, episode.text
    phase = client.post("/ros-mobile/hybrid/recording",
                        json={"action": "set_phase", "phase": "observe", **identity})
    assert phase.status_code == 200, phase.text

    public = client.get("/ros-mobile/hybrid/status").json()
    authoritative = controller.status()
    assert public["recording"] == authoritative
    assert public["recording"]["source"] == "hybrid_recording_controller"
    assert public["recording"]["episode_id"] == "ep-upper"
    assert public["recording"]["phases"][-1]["phase"] == "observe"
    assert not hasattr(facade, "_recording")
    assert facade.phase == "observe"
    assert [entry["phase"] for entry in car.phase_events] == ["observe"]

    reopened = client.post("/ros-mobile/hybrid/session",
                           json={"action": "release", "reason": "operator", **identity})
    assert reopened.status_code == 200, reopened.text
    assert controller.status()["status"] == "stopped"
    assert facade.status()["state"] == "disconnected"
    assert any(path == "/recording/end_episode" and body.get("discarded") is True
               for path, body in car.calls)
    assert any(path == "/recording/stop_session" for path, _body in car.calls)


def test_capabilities_expose_profile_actions_and_explicit_simulation(hybrid_route):
    client, _facade, _controller, _car = hybrid_route
    simulated = client.get("/ros-mobile/hybrid/capabilities").json()
    assert simulated["profile"] == "simulated_hybrid_manual_v1"
    assert simulated["profiles"] == ["simulated_hybrid_manual_v1"]
    assert simulated["simulated"] is True
    assert simulated["simulated_available"] is True
    assert simulated["hardware_fail_closed"] is False
    assert simulated["available"] is False
    assert simulated["outputs_enabled"] is False
    assert {"preview", "align", "arm", "engage", "start_session",
            "set_phase", "end_episode", "sync", "export"}.issubset(
                simulated["actions"])

    hardware = HybridPublicFacade().capabilities()
    assert hardware["profile"] == "hardware_hybrid_manual_v1"
    assert hardware["profiles"] == ["hardware_hybrid_manual_v1"]
    assert hardware["simulated"] is False
    assert hardware["simulated_available"] is False
    assert hardware["hardware_fail_closed"] is True
    assert hardware["available"] is False
    assert hardware["actions"] == ["estop"]
    assert "preview" not in hardware["actions"]


def test_hybrid_calibration_route_preserves_identity_and_payload(monkeypatch):
    calls = []

    class Facade:
        def validate_session_request_if_active(self, _body):
            return None

        def calibration(self, action, kind, body):
            calls.append((action, kind, body))
            return {"saved": True}

    monkeypatch.setattr(ros_mobile, "get_hybrid_facade", lambda: Facade())
    app = FastAPI()
    app.include_router(ros_mobile.router)
    response = TestClient(app).post("/ros-mobile/hybrid/calibration", json={
        "action": "save",
        "kind": "leader",
        "identity": "so101-current",
        "payload": {"measured": True},
    })
    assert response.status_code == 200
    assert calls == [("save", "leader", {
        "action": "save",
        "kind": "leader",
        "identity": "so101-current",
        "payload": {"measured": True},
    })]


def test_homing_routes_require_current_identity_and_forward_only_start_cancel(monkeypatch):
    calls = []

    class Facade:
        def validate_session_request(self, body, *, allow_estop=False):
            assert allow_estop is False
            if body.get("session_id") != "current" or body.get("session_epoch") != 7:
                raise RuntimeError("session_epoch_mismatch")

        def home_status(self):
            return {"state": "idle"}

        def start_home(self, body):
            calls.append(("start", body))
            return {"state": "ready", "homing": {"state": "moving"}}

        def cancel_home(self, body):
            calls.append(("cancel", body))
            return {"state": "ready", "homing": {"state": "cancelled"}}

    monkeypatch.setattr(ros_mobile, "get_hybrid_facade", lambda: Facade())
    app = FastAPI()
    app.include_router(ros_mobile.router)
    client = TestClient(app)
    stale = client.post("/ros-mobile/hybrid/home", json={
        "action": "start", "session_id": "old", "session_epoch": 6})
    assert stale.status_code == 409
    started = client.post("/ros-mobile/hybrid/home", json={
        "action": "start", "session_id": "current", "session_epoch": 7})
    assert started.status_code == 200
    assert started.json()["homing"]["state"] == "moving"
    cancelled = client.post("/ros-mobile/hybrid/home", json={
        "action": "cancel", "session_id": "current", "session_epoch": 7})
    assert cancelled.status_code == 200
    assert cancelled.json()["homing"]["state"] == "cancelled"
    assert client.get("/ros-mobile/hybrid/home").json() == {"state": "idle"}
    assert [name for name, _body in calls] == ["start", "cancel"]


def test_car_request_bodies_carry_the_authoritative_identity(hybrid_route):
    client, _facade, _controller, car = hybrid_route
    session_id, epoch = _acquire(client)
    identity = _identity(session_id, epoch)
    assert client.post("/ros-mobile/hybrid/recording",
                       json={"action": "start_session", **identity}).status_code == 200
    assert client.post("/ros-mobile/hybrid/recording",
                       json={"action": "start_episode", **identity}).status_code == 200
    for path in ("/recording/start_session", "/recording/start_episode"):
        body = next(body for called, body in car.calls if called == path)
        assert body["session_id"] == session_id
        assert body["session_epoch"] == epoch
        assert isinstance(body["session_epoch"], int)
        assert not isinstance(body["session_epoch"], bool)


def test_estop_preempts_a_blocked_recording_request(hybrid_route):
    client, facade, controller, car = hybrid_route
    session_id, epoch = _acquire(client)
    entered = threading.Event()
    release = threading.Event()
    original_post = car.post

    def blocking_post(path, body):
        if path == "/recording/start_session":
            entered.set()
            assert release.wait(2.0)
        return original_post(path, body)

    car.post = blocking_post
    errors = []

    def start_recording():
        try:
            controller.dispatch("start_session", _identity(session_id, epoch))
        except Exception as exc:  # surfaced after the test unblocks the car
            errors.append(exc)

    worker = threading.Thread(target=start_recording, daemon=True)
    worker.start()
    assert entered.wait(2.0)

    started = time.monotonic()
    result = controller.estop("blocked-recording")
    elapsed = time.monotonic() - started
    assert result["estop"] is True
    assert result["preempted_recording_lock"] is True
    assert elapsed < 0.5
    assert facade.state == "estop"
    assert facade.estop_latched is True

    release.set()
    worker.join(2.0)
    deadline = time.monotonic() + 2.0
    while controller._estop_worker_started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not worker.is_alive()
    assert not controller._estop_worker_started
    assert not errors
    assert controller.status()["status"] == "invalid"


def test_env_opt_in_builds_the_production_route_with_simulated_fixtures(monkeypatch):
    monkeypatch.setenv("LELAB_HYBRID_SIMULATED", "1")
    monkeypatch.setattr(coordinator, "_hybrid_facade", coordinator._hybrid_facade)
    monkeypatch.setattr(ros_mobile, "_HYBRID_FACADE", None)
    monkeypatch.setattr(ros_mobile, "_HYBRID_RECORDING", None)

    app = FastAPI()
    app.include_router(ros_mobile.router)
    client = TestClient(app)

    capabilities = client.get("/ros-mobile/hybrid/capabilities").json()
    assert capabilities["profile"] == "simulated_hybrid_manual_v1"
    assert capabilities["profiles"] == ["simulated_hybrid_manual_v1"]
    assert capabilities["simulated"] is True
    assert capabilities["simulated_available"] is True
    assert capabilities["outputs_enabled"] is False
    assert {"preview", "start_session", "set_phase", "sync", "export"}.issubset(
        capabilities["actions"])

    status = client.get("/ros-mobile/hybrid/status").json()
    assert status["profile"] == "simulated_hybrid_manual_v1"
    assert isinstance(ros_mobile.get_hybrid_facade(), HybridPublicFacade)
    session_id, epoch = _acquire(client)
    identity = _identity(session_id, epoch)
    assert client.post("/ros-mobile/hybrid/control",
                       json={"action": "preview", **identity}).status_code == 200

    controller = ros_mobile.get_hybrid_recording()
    assert controller.car.simulated is True
    assert controller.car.transport == "in_memory"
    assert client.post("/ros-mobile/hybrid/recording",
                       json={"action": "start_session", **identity}).status_code == 200
    assert client.post("/ros-mobile/hybrid/recording",
                       json={"action": "start_episode", **identity}).status_code == 200
    assert client.post("/ros-mobile/hybrid/recording",
                       json={"action": "set_phase", "phase": "observe",
                             **identity}).status_code == 200
    public = client.get("/ros-mobile/hybrid/status").json()
    assert public["recording"] == controller.status()
    assert public["recording"]["phases"][-1]["phase"] == "observe"


def test_preview_headers_are_exact_monotonic_and_status_bound(monkeypatch):
    ros_mobile.reset_preview_frame_state()
    facade = HybridPublicFacade()
    facade._facts = {"video": {"available": True, "preview_seq": 7}}
    assert facade.status()["video"]["frame_seq"] == 7
    facade._facts = {"video": {"available": False}}
    assert facade.status()["video"]["frame_seq"] is None

    status_seq = {"value": 7}
    frames = [
        (b"frame-7", {"X-LeLab-Frame-Seq": "0007",
                      "X-LeLab-Source-Stamp-Ns": "0000000000000000100"}),
        (b"cached-frame-7", {"x-lelab-frame-seq": "7",
                             "x-lelab-source-stamp-ns": "100"}),
        (b"wrong-frame", {"X-LeLab-Frame-Seq": "7",
                          "X-LeLab-Source-Stamp-Ns": "200"}),
        (b"stamp-did-not-advance", {"X-LeLab-Frame-Seq": "8",
                                    "X-LeLab-Source-Stamp-Ns": "100"}),
        (b"frame-9", {"X-LeLab-Frame-Seq": "9",
                      "X-LeLab-Source-Stamp-Ns": "101"}),
        (b"missing-stamp", {"X-LeLab-Frame-Seq": "10"}),
    ]
    monkeypatch.setattr(ros_mobile, "_current_video_frame_seq",
                        lambda: status_seq["value"])
    monkeypatch.setattr(
        ros_mobile, "fetch_preview_frame",
        lambda *args, **kwargs: frames.pop(0))

    app = FastAPI()
    app.include_router(ros_mobile.router)
    client = TestClient(app)

    first = client.get("/ros-mobile/preview.jpg?frame_seq=7")
    assert first.status_code == 200, first.text
    assert first.content == b"frame-7"
    assert first.headers["X-LeLab-Frame-Seq"] == "0007"
    assert first.headers["X-LeLab-Source-Stamp-Ns"] == "0000000000000000100"
    assert "X-LeLab-Frame-Seq" in first.headers["Access-Control-Expose-Headers"]
    assert "X-LeLab-Source-Stamp-Ns" in first.headers["Access-Control-Expose-Headers"]

    frozen = client.get("/ros-mobile/preview.jpg?frame_seq=7")
    assert frozen.status_code == 409
    assert "stale_frame_seq" in frozen.text

    # The JPEG header and server status/request identity must agree.
    status_seq["value"] = 8
    mismatched = client.get("/ros-mobile/preview.jpg?frame_seq=8")
    assert mismatched.status_code == 409
    assert "frame_seq_mismatch" in mismatched.text

    non_advancing_stamp = client.get("/ros-mobile/preview.jpg?frame_seq=8")
    assert non_advancing_stamp.status_code == 409
    assert "frame_header_inconsistent" in non_advancing_stamp.text

    status_seq["value"] = 9
    recovered = client.get("/ros-mobile/preview.jpg?frame_seq=9")
    assert recovered.status_code == 200
    assert recovered.content == b"frame-9"

    status_seq["value"] = 10
    missing_stamp = client.get("/ros-mobile/preview.jpg?frame_seq=10")
    assert missing_stamp.status_code == 502
    assert "missing_frame_header" in missing_stamp.text
    ros_mobile.reset_preview_frame_state()

    # The legacy bytes-only helper remains callable.
    monkeypatch.setattr(
        ros_mobile, "fetch_preview_frame",
        lambda *args, **kwargs: (b"legacy-bytes", {
            "X-LeLab-Frame-Seq": "11",
            "X-LeLab-Source-Stamp-Ns": "102",
        }))
    assert ros_mobile.fetch_preview_jpeg() == b"legacy-bytes"
