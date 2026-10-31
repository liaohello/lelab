"""Tests for the /system and /storage status streams."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_system_status_websocket_pushes_a_snapshot(client: TestClient) -> None:
    with client.websocket_connect("/ws/system-status") as websocket:
        payload = websocket.receive_json()

    assert "platform" in payload
    assert "cpu" in payload
    assert "memory" in payload
    assert "disk" in payload


def test_storage_status_websocket_pushes_a_summary(client: TestClient) -> None:
    with client.websocket_connect("/ws/storage-status") as websocket:
        payload = websocket.receive_json()

    assert "disk" in payload
    assert "warning_level" in payload
    assert "recording_allowed" in payload
