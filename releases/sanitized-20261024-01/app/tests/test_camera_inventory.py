"""Camera inventory tests without opening V4L2 devices."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lelab import camera_inventory as ci
from lelab.utils import config as cfg


def _camera(index: int, key: str = "unassigned"):
    return {
        "id": f"/dev/v4l/by-id/usb-test-{index}-video-index0",
        "stable_source": "by-id",
        "by_id": f"/dev/v4l/by-id/usb-test-{index}-video-index0",
        "by_path": None,
        "device": f"/dev/video{index}",
        "index": index,
        "name": f"Test camera {index}",
        "present": True,
        "camera_key": key,
        "note": "",
    }


def test_save_camera_inventory_writes_stable_mapping(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "CAMERA_INVENTORY_PATH", str(tmp_path / "cameras"))
    cameras = [_camera(0), _camera(2)]
    monkeypatch.setattr(ci, "list_camera_ports", lambda: cameras)

    result = ci.save_inventory(
        {cameras[0]["id"]: "left_wrist", cameras[1]["id"]: "right_wrist"},
        {cameras[0]["id"]: "left arm"},
    )

    assert result["missing_recommended"] == ["front"]
    saved = json.loads(Path(result["saved_path"]).read_text())
    assert saved["cameras"][0]["camera_key"] == "left_wrist"
    assert saved["cameras"][0]["note"] == "left arm"
    assert saved["cameras"][1]["camera_key"] == "right_wrist"
    assert Path(result["latest_path"]).is_file()


def test_save_rejects_duplicate_camera_keys(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "CAMERA_INVENTORY_PATH", str(tmp_path / "cameras"))
    cameras = [_camera(0), _camera(2)]
    monkeypatch.setattr(ci, "list_camera_ports", lambda: cameras)

    with pytest.raises(ValueError, match="duplicate camera keys"):
        ci.save_inventory(
            {cameras[0]["id"]: "front", cameras[1]["id"]: "front"}
        )


def test_merge_preserves_saved_but_absent_camera(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "CAMERA_INVENTORY_PATH", str(tmp_path / "cameras"))
    latest = {
        "saved_at": "2026-11-02T00:00:00+00:00",
        "cameras": [_camera(0, "front"), _camera(9, "left_wrist")],
    }
    merged = ci.merge_inventory_cameras([_camera(0)], latest)

    assert len(merged) == 2
    assert {item["id"]: item["present"] for item in merged} == {
        _camera(0)["id"]: True,
        _camera(9)["id"]: False,
    }
    assert next(item for item in merged if item["index"] == 9)["camera_key"] == "left_wrist"


def test_camera_inventory_http_round_trip(
    client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cfg, "CAMERA_INVENTORY_PATH", str(tmp_path / "cameras"))
    cameras = [_camera(0), _camera(2)]
    monkeypatch.setattr(ci, "list_camera_ports", lambda: cameras)

    listed = client.get("/cameras/inventory")
    assert listed.status_code == 200
    assert len(listed.json()["cameras"]) == 2

    saved = client.post(
        "/cameras/inventory/save",
        json={
            "assignments": {
                cameras[0]["id"]: "front",
                cameras[1]["id"]: "right_wrist",
            },
            "notes": {cameras[0]["id"]: "desk view"},
        },
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["missing_recommended"] == ["left_wrist"]

    downloaded = client.get("/cameras/inventory/latest")
    assert downloaded.status_code == 200
    assert downloaded.json()["cameras"][0]["camera_key"] == "front"


def test_camera_inventory_rejects_invalid_key(
    client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cfg, "CAMERA_INVENTORY_PATH", str(tmp_path / "cameras"))
    camera = _camera(0)
    monkeypatch.setattr(ci, "list_camera_ports", lambda: [camera])

    response = client.post(
        "/cameras/inventory/save", json={"assignments": {camera["id"]: "1 bad key"}}
    )
    assert response.status_code == 400
    assert "must start with a letter" in response.json()["detail"]


def _saved_inventory(tmp_path, monkeypatch: pytest.MonkeyPatch, cameras):
    monkeypatch.setattr(cfg, "CAMERA_INVENTORY_PATH", str(tmp_path / "cameras"))
    monkeypatch.setattr(ci, "list_camera_ports", lambda: cameras)
    return cameras


def test_confirm_stores_picture_evidence(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cameras = [_camera(0), _camera(2)]
    _saved_inventory(tmp_path, monkeypatch, cameras)

    result = ci.save_inventory(
        {cameras[0]["id"]: "left_wrist"},
        labels={cameras[0]["id"]: "左手腕相机"},
        confirmations={cameras[0]["id"]: True},
    )

    saved = result["cameras"][0]
    assert saved["label"] == "左手腕相机"
    assert saved["confirmed_at"]


def test_confirmation_requires_the_device_and_a_field(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cameras = [_camera(0), _camera(2)]
    _saved_inventory(tmp_path, monkeypatch, cameras)

    with pytest.raises(ValueError, match="dataset field"):
        ci.save_inventory({}, confirmations={cameras[0]["id"]: True})

    absent = {**cameras[1], "present": False}
    monkeypatch.setattr(ci, "list_camera_ports", lambda: [cameras[0], absent])
    ci.save_inventory({absent["id"]: "front"})
    with pytest.raises(ValueError, match="not connected"):
        ci.save_inventory({}, confirmations={absent["id"]: True})


def test_unplugging_clears_the_picture_confirmation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cameras = [_camera(0)]
    _saved_inventory(tmp_path, monkeypatch, cameras)
    ci.save_inventory(
        {cameras[0]["id"]: "front"}, confirmations={cameras[0]["id"]: True}
    )

    offline = [{**cameras[0], "present": False}]
    monkeypatch.setattr(ci, "list_camera_ports", lambda: offline)

    binding = ci.collection_binding()

    assert binding["ready"] is False
    assert binding["missing"] == [
        {
            "camera_key": "front",
            "label": "front",
            "name": "Test camera 0",
            "id": cameras[0]["id"],
            "device": "/dev/video0",
            "index": 0,
            "confirmed_at": None,
        }
    ]
    assert binding["cameras"] == []


def test_collection_binding_prefers_the_stable_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cameras = [_camera(0), _camera(2)]
    _saved_inventory(tmp_path, monkeypatch, cameras)
    ci.save_inventory(
        {cameras[0]["id"]: "left_wrist", cameras[1]["id"]: "up"},
        labels={cameras[0]["id"]: "左手腕相机"},
        confirmations={cameras[0]["id"]: True},
    )

    binding = ci.collection_binding()

    assert binding["ready"] is False
    assert [row["camera_key"] for row in binding["cameras"]] == ["left_wrist", "up"]
    left = binding["cameras"][0]
    assert left["source"] == "/dev/v4l/by-id/usb-test-0-video-index0"
    assert left["label"] == "左手腕相机"
    assert left["ready"] is True
    assert binding["unconfirmed"][0]["camera_key"] == "up"
