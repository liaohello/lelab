"""HTTP-level tests for the hardware-lease endpoints and start guards."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def lease_client(client: TestClient) -> TestClient:
    """Client whose app-level lease starts from a clean, idle state."""
    from lelab import server

    server.hardware_lease.preempt(reason="test-setup")
    yield client
    server.hardware_lease.preempt(reason="test-teardown")


def test_lease_lifecycle_over_http(lease_client: TestClient) -> None:
    idle = lease_client.get("/hardware/lease").json()
    assert idle["state"] == "idle"

    acquired = lease_client.post(
        "/hardware/lease/acquire",
        json={"task": "recording", "owner": "operator-1", "ttl_seconds": 30},
    )
    assert acquired.status_code == 200
    body = acquired.json()
    assert body["success"] is True
    token = body["lease"]["token"]
    assert body["lease"]["task"] == "recording"

    renewed = lease_client.post("/hardware/lease/renew", json={"token": token, "ttl_seconds": 45})
    assert renewed.status_code == 200
    assert renewed.json()["lease"]["ttl_seconds"] == 45.0

    released = lease_client.post("/hardware/lease/release", json={"token": token})
    assert released.status_code == 200
    assert released.json()["success"] is True
    assert lease_client.get("/hardware/lease").json()["state"] == "idle"


def test_conflicting_acquire_reports_the_holder(lease_client: TestClient) -> None:
    lease_client.post("/hardware/lease/acquire", json={"task": "teleoperation", "owner": "windows"})

    conflict = lease_client.post(
        "/hardware/lease/acquire", json={"task": "recording", "owner": "jetson-ui"}
    )

    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["holder"]["task"] == "teleoperation"
    assert detail["holder"]["owner"] == "windows"


def test_observer_cannot_acquire_over_http(lease_client: TestClient) -> None:
    response = lease_client.post(
        "/hardware/lease/acquire",
        json={"task": "inference", "owner": "phone", "role": "observer"},
    )
    assert response.status_code == 403
    assert lease_client.get("/hardware/lease").json()["state"] == "idle"


def test_start_endpoint_refuses_when_another_task_holds_the_lease(
    lease_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import server

    called = {"value": False}

    def fake_start(request, websocket_manager=None):  # pragma: no cover - must not run
        called["value"] = True
        return {"success": True}

    monkeypatch.setattr(server, "handle_start_teleoperation", fake_start)
    lease_client.post("/hardware/lease/acquire", json={"task": "recording", "owner": "operator-1"})

    response = lease_client.post(
        "/move-arm",
        json={
            "leader_port": "COM3",
            "follower_port": "COM4",
            "leader_config": "leader",
            "follower_config": "follower",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["holder"]["task"] == "recording"
    assert called["value"] is False


def test_failed_start_releases_the_lease(
    lease_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import server

    monkeypatch.setattr(
        server,
        "handle_start_teleoperation",
        lambda request, websocket_manager=None: {"success": False, "message": "no serial port"},
    )

    response = lease_client.post(
        "/move-arm",
        json={
            "leader_port": "COM3",
            "follower_port": "COM4",
            "leader_config": "leader",
            "follower_config": "follower",
        },
    )

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert lease_client.get("/hardware/lease").json()["state"] == "idle"


def test_stop_endpoint_releases_the_lease(
    lease_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import server

    monkeypatch.setattr(
        server, "handle_stop_teleoperation", lambda: {"success": True, "message": "stopped"}
    )
    lease_client.post("/hardware/lease/acquire", json={"task": "teleoperation", "owner": "ui"})

    response = lease_client.post("/stop-teleoperation")

    assert response.status_code == 200
    assert lease_client.get("/hardware/lease").json()["state"] == "idle"


def test_start_uses_client_headers_as_the_owner(
    lease_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import server

    monkeypatch.setattr(
        server,
        "handle_start_teleoperation",
        lambda request, websocket_manager=None: {"success": True},
    )

    lease_client.post(
        "/move-arm",
        json={
            "leader_port": "COM3",
            "follower_port": "COM4",
            "leader_config": "leader",
            "follower_config": "follower",
        },
        headers={"X-LeLab-Client": "windows-chrome", "X-LeLab-Role": "operator"},
    )

    status = lease_client.get("/hardware/lease").json()
    assert status["state"] == "held"
    assert status["owner"] == "windows-chrome"


def test_observer_header_blocks_start(
    lease_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import server

    monkeypatch.setattr(
        server,
        "handle_start_teleoperation",
        lambda request, websocket_manager=None: {"success": True},
    )

    response = lease_client.post(
        "/move-arm",
        json={
            "leader_port": "COM3",
            "follower_port": "COM4",
            "leader_config": "leader",
            "follower_config": "follower",
        },
        headers={"X-LeLab-Role": "observer"},
    )

    assert response.status_code == 403
    assert lease_client.get("/hardware/lease").json()["state"] == "idle"


def test_emergency_stop_preempts_and_stops_every_task(lease_client: TestClient) -> None:
    lease_client.post("/hardware/lease/acquire", json={"task": "recording", "owner": "ui"})

    response = lease_client.post("/emergency-stop", json={"reason": "operator"})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["preempted"]["task"] == "recording"
    assert set(body["stopped"]) == {"calibration", "teleoperation", "recording", "inference"}
    assert lease_client.get("/hardware/lease").json()["state"] == "idle"
