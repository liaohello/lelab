"""Offline checks for the hardware read-only采集 path.

Nothing here touches the car, a wireless link, a serial port or a camera: the
agent HTTP call and the JPEG fetch are stubbed, and snapshots are written into
`tmp_path`.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from lelab import ros_mobile


def _jpeg(width: int, height: int) -> bytes:
    """Smallest JPEG-shaped buffer that still carries a SOF0 frame size."""
    sof = (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + sof + b"\xff\xd9"


READONLY_STATUS = {
    "ok": True,
    "read_only": True,
    "profile": "hardware_readonly_v1",
    "runtime_mode": "hardware_readonly",
    "camera_profile": "wrist_original",
    "sensors": {
        "rgb": {
            "topic": "/ascamera/camera_publisher/rgb0/image",
            "fps": 8.59,
            "age_s": 0.21,
            "stale": False,
            "preview_seq": 412,
            "preview_backend": "web_video_server",
        },
        "depth": {"topic": "/ascamera/camera_publisher/depth0/image_raw", "encoding": "mono16", "width": 640, "height": 400, "age_s": 1.9},
        "lidar": {"topic": "/scan", "ranges": None, "age_s": None},
        "imu": {"topic": "/imu", "age_s": 0.11},
    },
    "topics": {"missing": [], "required_missing": []},
    "recording": {"active": False, "episodes": [], "session_id": None},
}


@pytest.fixture()
def hardware_root(monkeypatch, tmp_path):
    monkeypatch.setattr(ros_mobile, "HARDWARE_ROOT", tmp_path / "hardware")
    (tmp_path / "hardware").mkdir(parents=True, exist_ok=True)
    return tmp_path / "hardware"


def test_jpeg_dimensions_reads_sof0():
    assert ros_mobile.jpeg_dimensions(_jpeg(640, 400)) == (640, 400)
    assert ros_mobile.jpeg_dimensions(b"not a jpeg") == (None, None)


def test_repository_profile_is_readonly_and_refuses_training_export():
    loaded = ros_mobile.load_hardware_profile()
    profile = loaded["profile"]
    assert len(loaded["sha256"]) == 64
    assert profile["runtime"]["mode"] == "hardware_readonly"
    assert profile["runtime"]["actuator_outputs"] is False
    assert profile["training_export"]["enabled"] is False
    assert profile["sensors"]["wrist_rgb"]["rgb_topic"].startswith("/ascamera/")


def test_missing_profile_reports_the_path(monkeypatch, tmp_path):
    monkeypatch.setenv("LELAB_ROS_HARDWARE_PROFILE", str(tmp_path / "nope.json"))
    with pytest.raises(ros_mobile.BridgeError) as exc:
        ros_mobile.load_hardware_profile()
    assert exc.value.status == 404
    assert "nope.json" in exc.value.detail


def test_overview_merges_profile_with_live_readonly_status(monkeypatch):
    monkeypatch.setattr(ros_mobile, "_http", lambda *a, **k: dict(READONLY_STATUS))
    out = ros_mobile.hardware_overview()
    assert out["mode"] == "hardware_readonly"
    assert out["read_only"] is True
    assert out["agent_ok"] is True
    assert out["agent_error"] is None
    assert out["camera"]["expected_width"] == 640
    assert out["camera"]["expected_height"] == 400
    assert out["camera"]["fps"] == 8.59
    assert out["echoes"]["actuator_outputs"] is False
    assert out["echoes"]["training_export"] is False
    rows = {row["key"]: row for row in out["sensors"]}
    assert rows["rgb"]["required"] is True and rows["rgb"]["present"] is True
    assert rows["lidar"]["present"] is False


def test_overview_survives_an_unreachable_agent(monkeypatch):
    def boom(*args, **kwargs):
        raise ros_mobile.BridgeError(503, "car agent unavailable: timed out")

    monkeypatch.setattr(ros_mobile, "_http", boom)
    out = ros_mobile.hardware_overview()
    assert out["agent_ok"] is False
    assert "timed out" in out["agent_error"]
    # The profile is local, so the page can still say what *should* be real.
    assert out["mode"] == "hardware_readonly"
    assert out["camera"]["expected_width"] == 640


def test_snapshot_keeps_frame_bytes_and_matching_size(monkeypatch, hardware_root):
    frame = _jpeg(640, 400)
    monkeypatch.setattr(ros_mobile, "fetch_preview_jpeg", lambda *a, **k: frame)
    monkeypatch.setattr(ros_mobile, "_http", lambda *a, **k: dict(READONLY_STATUS))

    out = ros_mobile.hardware_snapshot()

    snapshot = hardware_root / out["snapshot_id"]
    assert (snapshot / "frame.jpg").read_bytes() == frame
    assert (snapshot / "capture.json").is_file()
    saved = json.loads((snapshot / "capture.json").read_text())
    assert saved["jpeg_sha256"] == hashlib.sha256(frame).hexdigest()
    assert (saved["jpeg_width"], saved["jpeg_height"]) == (640, 400)
    assert saved["size_matches_profile"] is True
    assert saved["camera_topic"] == "/ascamera/camera_publisher/rgb0/image"
    assert saved["has_frame"] is True
    assert saved["notes"] == []
    assert json.loads((snapshot / "agent_status.json").read_text())["read_only"] is True


def test_snapshot_records_a_size_mismatch_instead_of_hiding_it(monkeypatch, hardware_root):
    monkeypatch.setattr(ros_mobile, "fetch_preview_jpeg", lambda *a, **k: _jpeg(320, 240))
    monkeypatch.setattr(ros_mobile, "_http", lambda *a, **k: dict(READONLY_STATUS))
    out = ros_mobile.hardware_snapshot()
    assert out["size_matches_profile"] is False
    assert (out["jpeg_width"], out["jpeg_height"]) == (320, 240)


def test_snapshot_without_a_frame_still_saves_the_status(monkeypatch, hardware_root):
    def no_frame(*args, **kwargs):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(ros_mobile, "fetch_preview_jpeg", no_frame)
    monkeypatch.setattr(ros_mobile, "_http", lambda *a, **k: dict(READONLY_STATUS))

    out = ros_mobile.hardware_snapshot()

    snapshot = hardware_root / out["snapshot_id"]
    assert out["has_frame"] is False
    assert not (snapshot / "frame.jpg").exists()
    assert (snapshot / "agent_status.json").is_file()
    assert any("read timed out" in note for note in out["notes"])


def test_snapshots_are_listed_newest_first(monkeypatch, hardware_root):
    monkeypatch.setattr(ros_mobile, "fetch_preview_jpeg", lambda *a, **k: _jpeg(640, 400))
    monkeypatch.setattr(ros_mobile, "_http", lambda *a, **k: dict(READONLY_STATUS))
    first = ros_mobile.hardware_snapshot()
    second = ros_mobile.hardware_snapshot()

    listed = ros_mobile.hardware_snapshots(limit=10)
    ids = [item["snapshot_id"] for item in listed["snapshots"]]
    assert ids[0] == second["snapshot_id"]
    assert first["snapshot_id"] in ids
    assert listed["root"] == str(hardware_root)
