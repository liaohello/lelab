# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for lelab.server — FastAPI app and ConnectionManager."""

from __future__ import annotations

import asyncio
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

# A browser sends an Accept header that prefers HTML on navigations/hard-reloads.
BROWSER_ACCEPT = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

REQUIRED_PATHS = {
    "/health",
    "/get-configs",
    "/move-arm",
    "/stop-teleoperation",
    "/teleoperation-status",
    "/joint-positions",
    "/start-recording",
    "/stop-recording",
    "/recording-status",
    "/start-calibration",
    "/stop-calibration",
    "/calibration-status",
    "/datasets",
    "/datasets/local",
    "/datasets/local/trash",
    "/datasets/local/{repo_id:path}/summary",
    "/datasets/local/{repo_id:path}/integrity",
    "/datasets/local/{repo_id:path}/sync-train",
    "/datasets/local/{repo_id:path}/sync-status",
    "/datasets/local/{repo_id:path}/trash",
    "/datasets/local/{repo_id:path}/restore",
    "/datasets/sync-status",
    "/system/status",
    "/system/train-server",
    "/storage/summary",
    "/datasets/local/{repo_id:path}/storage",
    "/hardware/lease",
    "/hardware/lease/acquire",
    "/hardware/lease/renew",
    "/hardware/lease/release",
    "/emergency-stop",
    "/jobs",
    "/available-ports",
    "/available-cameras",
    "/camera-preview/{camera_index}",
    "/hf-auth-status",
    "/inference/remote/server-status",
    "/inference/remote/server/stop",
    "/ws/joint-data",
    "/ws/system-status",
    "/ws/storage-status",
}


def test_app_exposes_required_endpoints() -> None:
    from lelab.server import app

    paths = {route.path for route in app.routes}
    missing = REQUIRED_PATHS - paths
    assert not missing, f"missing routes: {missing}"


def test_health_endpoint_returns_200(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_health_endpoint_returns_dict(client: TestClient) -> None:
    response = client.get("/health")
    body = response.json()
    assert isinstance(body, dict)


def test_start_inference_dispatches_remote_target(client: TestClient, monkeypatch) -> None:
    """target=remote must go to the cloud handler, never the local rollout."""
    from lelab import server

    # Previews must be released before any inference target starts (see below).
    monkeypatch.setattr(
        server, "_release_camera_previews", lambda timeout_s=5.0: {"requested": 0, "released": 0, "remaining": 0}
    )

    calls: dict[str, object] = {}

    def fake_remote(request):
        calls["remote"] = request.policy_ref
        return {"success": True, "message": "Cloud inference started", "log_path": "/tmp/x.log"}

    def fake_local(request):  # pragma: no cover - must not run
        raise AssertionError("local rollout must not start for target=remote")

    monkeypatch.setattr(server, "handle_start_remote_inference", fake_remote)
    monkeypatch.setattr(server, "handle_start_inference", fake_local)
    monkeypatch.setenv("LELAB_HARDWARE_LEASE_REQUIRED", "0")

    response = client.post(
        "/start-inference",
        json={
            "follower_port": "/dev/serial/by-id/does-not-exist",
            "follower_config": "follower_a",
            "policy_ref": "/remote/checkpoints/000050/pretrained_model",
            "task": "pick",
            "cameras": {},
            "duration_s": 30,
            "target": "remote",
        },
    )

    assert response.status_code == 200
    assert calls["remote"] == "/remote/checkpoints/000050/pretrained_model"


def test_start_inference_releases_camera_previews_first(client: TestClient, monkeypatch) -> None:
    """A held preview must block the start with 409, not let the rollout fail."""
    from lelab import server

    monkeypatch.setattr(
        server,
        "_release_camera_previews",
        lambda timeout_s=5.0: {"requested": 2, "released": 1, "remaining": 1},
    )
    called: list[str] = []
    monkeypatch.setattr(server, "handle_start_inference", lambda request: called.append("local") or {})
    monkeypatch.setenv("LELAB_HARDWARE_LEASE_REQUIRED", "0")

    response = client.post(
        "/start-inference",
        json={
            "follower_port": "/dev/serial/by-id/x",
            "follower_config": "follower_a",
            "policy_ref": "/tmp/model",
            "cameras": {},
            "duration_s": 10,
            "target": "local",
        },
    )

    assert response.status_code == 409
    assert "预览" in response.json()["detail"]
    assert called == []


def test_remote_policy_server_status_endpoint(client: TestClient, monkeypatch) -> None:
    from lelab import server

    monkeypatch.setattr(
        server,
        "handle_remote_policy_server_status",
        lambda: {"configured": True, "running": False, "pid": None},
    )
    response = client.get("/inference/remote/server-status")
    assert response.status_code == 200
    assert response.json()["configured"] is True


def test_system_status_endpoint_exposes_dashboard_aliases(client: TestClient, monkeypatch) -> None:
    from lelab import server

    monkeypatch.setattr(
        server,
        "system_snapshot",
        lambda: {
            "timestamp": "now",
            "platform": {"hostname": "jetson"},
            "uptime_seconds": 12,
            "cpu": {"logical_count": 8, "load_average": {"one_minute": 1.25}},
            "memory": {"total_bytes": 10, "used_bytes": 4, "available_bytes": 6, "percent": 40},
            "disk": {"total_bytes": 100, "used_bytes": 20, "free_bytes": 80, "percent": 20},
            "temperatures": [],
            "gpu": {"available": False},
            "network": {},
        },
    )

    body = client.get("/system/status").json()
    assert body["host"] == "jetson"
    assert body["cpu"]["count"] == 8
    assert body["cpu"]["load_1m"] == 1.25
    assert body["memory"]["available"] == 6
    assert body["disk"]["free"] == 80


def test_storage_summary_endpoint_marks_critical_capacity(client: TestClient, monkeypatch) -> None:
    from lelab import server

    monkeypatch.setattr(
        server,
        "storage_summary",
        lambda: {
            "root": "/datasets",
            "disk": {"total_bytes": 100, "used_bytes": 96, "free_bytes": 4, "percent": 96},
            "dataset_storage": {"size_bytes": 75},
            "dataset_count": 1,
            "datasets": ["lab/demo"],
        },
    )

    body = client.get("/storage/summary").json()
    assert body["warning_level"] == "critical"
    assert body["recording_allowed"] is False
    assert body["total_dataset_bytes"] == 75


def test_recording_is_blocked_below_five_percent_free_space(client: TestClient, monkeypatch) -> None:
    from lelab import server

    monkeypatch.setattr(
        server,
        "dataset_disk_capacity",
        lambda: {"total_bytes": 100, "used_bytes": 96, "free_bytes": 4, "percent": 96},
    )
    start = MagicMock(return_value={"success": True})
    monkeypatch.setattr(server, "handle_start_recording", start)
    response = client.post(
        "/start-recording",
        json={
            "leader_port": "mock-leader",
            "follower_port": "mock-follower",
            "leader_config": "leader.json",
            "follower_config": "follower.json",
            "dataset_repo_id": "lab/demo",
            "single_task": "test",
            "test_mode": True,
        },
    )
    start.assert_not_called()
    assert response.status_code == 507
    assert "less than 5%" in response.json()["detail"]


def test_release_camera_previews_waits_for_device_release() -> None:
    from lelab import server

    session = server._register_camera_preview()

    def finish_after_stop() -> None:
        assert session.stop_requested.wait(timeout=1)
        server._finish_camera_preview(session)

    worker = threading.Thread(target=finish_after_stop)
    worker.start()
    result = server._release_camera_previews(timeout_s=1)
    worker.join(timeout=1)

    assert result == {"requested": 1, "released": 1, "remaining": 0}
    assert not worker.is_alive()


@pytest.mark.asyncio
async def test_cancelled_camera_preview_releases_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    from lelab import server

    opened = threading.Event()
    released = threading.Event()

    class FakeCapture:
        def __init__(self, *_args) -> None:
            opened.set()

        def isOpened(self) -> bool:  # noqa: N802 - mirrors the OpenCV API
            return True

        def set(self, *_args) -> bool:
            return True

        def release(self) -> None:
            released.set()

    fake_cv2 = SimpleNamespace(
        CAP_V4L2=1,
        CAP_ANY=0,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5,
        CAP_PROP_FOURCC=6,
        VideoCapture=FakeCapture,
        VideoWriter_fourcc=lambda *_args: 0,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            await asyncio.Event().wait()
            return False

    generator = server._standalone_camera_preview_frames(ConnectedRequest(), 0)
    pending_frame = asyncio.create_task(anext(generator))
    for _ in range(100):
        if opened.is_set():
            break
        await asyncio.sleep(0.01)

    pending_frame.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending_frame

    assert released.is_set()
    assert server._release_camera_previews(timeout_s=0) == {
        "requested": 0,
        "released": 0,
        "remaining": 0,
    }


def test_dataset_storage_endpoint_rejects_traversal(client: TestClient) -> None:
    response = client.get("/datasets/local/%2E%2E/secret/storage")
    assert response.status_code in {400, 404}


def test_unknown_route_returns_404(client: TestClient) -> None:
    response = client.get("/this-does-not-exist")
    assert response.status_code == 404


@pytest.mark.parametrize("unsafe_name", ["evil..name", "..config", "back\\door"])
def test_delete_calibration_config_rejects_unsafe_name(client: TestClient, unsafe_name: str) -> None:
    """A config name with path-traversal characters is rejected before any
    filesystem access — distinct from the "not found" path, so the guard is
    proven to fire. The validator also blocks "/" and "\\"."""
    response = client.delete(f"/calibration-configs/teleop/{unsafe_name}")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert "Invalid configuration name" in body["message"]


def _spa_mounted(client: TestClient) -> bool:
    return any(getattr(route, "name", None) == "frontend" for route in client.app.routes)


def test_spa_deep_link_serves_index_html(client: TestClient) -> None:
    """A browser hard-reload of a client-side route returns the SPA shell, not a 404."""
    if not _spa_mounted(client):
        pytest.skip("frontend/dist not built; SPA not mounted")
    response = client.get("/recording", headers=BROWSER_ACCEPT)
    assert response.status_code == 200
    assert response.text.lstrip().lower().startswith("<!doctype html")


def test_spa_fallback_does_not_mask_api_404(client: TestClient) -> None:
    """Non-HTML clients (XHR, curl, API typos) still get a real 404, not the SPA shell."""
    response = client.get("/recording", headers={"accept": "application/json"})
    assert response.status_code == 404


def test_spa_fallback_respects_explicit_html_refusal(client: TestClient) -> None:
    """`text/html;q=0` is an explicit refusal — it must not get the SPA shell."""
    response = client.get("/recording", headers={"accept": "application/json,text/html;q=0"})
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", True),
        ("text/html", True),
        ("text/html;q=0.5", True),
        ("application/json", False),
        ("*/*", False),
        ("", False),
        ("text/html;q=0", False),
        ("application/json,text/html;q=0", False),
        ("text/html;q=bogus", False),
    ],
)
def test_accepts_html(accept: str, expected: bool) -> None:
    from lelab.server import _accepts_html

    assert _accepts_html(accept) is expected


def test_connection_manager_tracks_connect_and_disconnect() -> None:
    from lelab.server import ConnectionManager

    mgr = ConnectionManager()
    fake_ws = MagicMock()
    fake_ws.accept = AsyncMock()

    import asyncio

    asyncio.run(mgr.connect(fake_ws))
    assert fake_ws in mgr.active_connections

    mgr.disconnect(fake_ws)
    assert fake_ws not in mgr.active_connections


def test_connection_manager_broadcast_sync_does_not_block_without_loop() -> None:
    from lelab.server import ConnectionManager

    mgr = ConnectionManager()
    # Should enqueue without raising even if there are no consumers.
    mgr.broadcast_joint_data_sync({"shoulder_pan.pos": 1.0})


def _install_fake_pygrabber(monkeypatch: pytest.MonkeyPatch, filter_graph_cls) -> None:
    import sys
    import types

    module = types.ModuleType("pygrabber.dshow_graph")
    module.FilterGraph = filter_graph_cls
    monkeypatch.setitem(sys.modules, "pygrabber", types.ModuleType("pygrabber"))
    monkeypatch.setitem(sys.modules, "pygrabber.dshow_graph", module)


def test_windows_cameras_uses_real_directshow_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows path returns pygrabber's real device names in index order so
    the frontend can match each camera to its browser deviceId (issues #12/#16).
    """
    from lelab import server

    class _FakeGraph:
        def get_input_devices(self) -> list[str]:
            return ["USB2.0_CAM1", "ASUS FHD webcam"]

    _install_fake_pygrabber(monkeypatch, _FakeGraph)

    assert server._windows_cameras() == [
        {"index": 0, "name": "USB2.0_CAM1", "available": True},
        {"index": 1, "name": "ASUS FHD webcam", "available": True},
    ]


def test_windows_cameras_falls_back_when_pygrabber_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If pygrabber is missing or its COM init fails, enumeration degrades to the
    generic cv2 probe instead of erroring."""
    from lelab import server

    class _BoomGraph:
        def __init__(self) -> None:
            raise RuntimeError("DirectShow/COM unavailable")

    _install_fake_pygrabber(monkeypatch, _BoomGraph)
    sentinel = [{"index": 0, "name": "Camera 0", "available": True}]
    monkeypatch.setattr(server, "_generic_cv2_cameras", lambda backend: sentinel)

    assert server._windows_cameras() == sentinel


def test_v4l2_camera_name_reads_sysfs(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from lelab import server

    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO("HD Pro Webcam C920\n"))
    assert server._v4l2_camera_name(0) == "HD Pro Webcam C920"


def test_v4l2_camera_name_returns_none_when_missing() -> None:
    from lelab import server

    # No such sysfs node (also the case on non-Linux): graceful None, not error.
    assert server._v4l2_camera_name(999999) is None


def test_import_model_route_returns_record(client, monkeypatch) -> None:
    from lelab import server

    fake = {
        "id": "act_imported_x",
        "name": "Imported · model",
        "state": "done",
        "config": {"dataset_repo_id": "(imported)", "policy_type": "act"},
        "output_dir": "/tmp/model",
        "started_at": 1.0,
        "ended_at": 1.0,
        "runner": "imported",
        "hf_repo_id": None,
    }
    from lelab.jobs import JobRecord

    monkeypatch.setattr(
        server.job_registry,
        "register_imported",
        lambda source, name=None: JobRecord(**fake),
    )
    resp = client.post("/jobs/import", json={"source": "/tmp/model"})
    assert resp.status_code == 201
    assert resp.json()["runner"] == "imported"


def test_import_model_route_maps_value_error_to_400(client, monkeypatch) -> None:
    from lelab import server

    def boom(source, name=None):
        raise ValueError("No usable model at '/tmp/x'")

    monkeypatch.setattr(server.job_registry, "register_imported", boom)
    resp = client.post("/jobs/import", json={"source": "/tmp/x"})
    assert resp.status_code == 400
    assert "No usable model" in resp.json()["detail"]
