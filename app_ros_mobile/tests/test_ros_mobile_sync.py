"""Integrity tests for the verified upper-host session copy."""
from __future__ import annotations

import json
import shutil
import urllib.error
from pathlib import Path

import pytest

from lelab import ros_mobile


SOURCE = Path(
    "/home/liaozhengyan/Desktop/lelab/work/ros2_collection/"
    "upper_hwprofile_test/upper/sessions/ros_20261031T172216Z_8f66db5c"
)


@pytest.fixture()
def session_copy(tmp_path):
    if not SOURCE.is_dir():
        pytest.skip("hardware-profile rehearsal evidence is not present")
    target = tmp_path / SOURCE.name
    shutil.copytree(SOURCE, target)
    return target


def _manifest(root: Path) -> dict:
    return json.loads((root / "manifest.json").read_text())


def test_tampered_payload_is_rejected(session_copy):
    manifest = _manifest(session_copy)
    victim = session_copy / manifest["files"][0]["path"]
    victim.write_bytes(victim.read_bytes() + b"tamper")
    with pytest.raises(ros_mobile.BridgeError) as exc:
        ros_mobile._verify_synced_session(session_copy, manifest)
    assert exc.value.status == 422
    assert "size mismatch" in exc.value.detail or "sha256 mismatch" in exc.value.detail


def test_tampered_ready_marker_is_rejected(session_copy):
    manifest = _manifest(session_copy)
    ready = json.loads((session_copy / "READY").read_text())
    ready["manifest_sha256"] = "0" * 64
    (session_copy / "READY").write_text(json.dumps(ready))
    with pytest.raises(ros_mobile.BridgeError) as exc:
        ros_mobile._verify_synced_session(session_copy, manifest)
    assert exc.value.status == 422
    assert "manifest hash mismatch" in exc.value.detail


class _StallingArchive:
    """Behaves like a tunnel that accepts the request then stops mid-transfer."""

    def __init__(self, chunks=3):
        self.chunks = chunks
        self.sent = 0

    def read(self, size=-1):
        if self.sent >= self.chunks:
            raise TimeoutError("timed out")
        self.sent += 1
        return b"x" * size

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _staging_dirs(root: Path):
    return [p for p in root.iterdir() if p.name.startswith(".")]


@pytest.fixture()
def isolated_upper(tmp_path, monkeypatch):
    upper = tmp_path / "upper" / "sessions"
    upper.mkdir(parents=True)
    monkeypatch.setattr(ros_mobile, "UPPER_SESSIONS", upper)
    monkeypatch.setattr(ros_mobile, "_http", lambda *a, **k: {"status": "raw_ready", "ready_eligible": True, "files": []})
    return upper


def test_stalled_transfer_is_a_defined_503_and_publishes_nothing(isolated_upper, monkeypatch):
    monkeypatch.setattr(ros_mobile.urllib.request, "urlopen", lambda *a, **k: _StallingArchive())
    with pytest.raises(ros_mobile.BridgeError) as exc:
        ros_mobile.sync_session("ros_stall_test")
    assert exc.value.status == 503
    assert "archive transfer interrupted" in exc.value.detail
    assert "3145728 bytes" in exc.value.detail
    assert not (isolated_upper / "ros_stall_test").exists()
    assert _staging_dirs(isolated_upper) == []


def test_failure_before_the_file_opens_does_not_mask_the_real_error(isolated_upper, monkeypatch):
    def refuse(*a, **k):
        raise urllib.error.URLError("tunnel is down")

    monkeypatch.setattr(ros_mobile.urllib.request, "urlopen", refuse)
    with pytest.raises(ros_mobile.BridgeError) as exc:
        ros_mobile.sync_session("ros_refuse_test")
    assert exc.value.status == 503
    assert "tunnel is down" in exc.value.detail
    assert _staging_dirs(isolated_upper) == []


def test_session_list_survives_a_dropped_tunnel(isolated_upper, monkeypatch):
    (isolated_upper / "ros_local_only").mkdir()
    (isolated_upper / "ros_local_only" / "manifest.json").write_text(json.dumps({"status": "raw_ready", "episodes": [], "files": []}))

    def explode(*a, **k):
        raise ros_mobile.BridgeError(503, "car agent timeout after 5.0s")

    monkeypatch.setattr(ros_mobile, "_http", explode)
    out = ros_mobile.sessions()
    assert out["agent_error"].startswith("car agent timeout")
    assert [s["session_id"] for s in out["sessions"]] == ["ros_local_only"]
    assert out["sessions"][0]["status"] == "local-only"


def test_agent_sessions_are_merged_with_local_copies(isolated_upper, monkeypatch):
    (isolated_upper / "ros_both").mkdir()
    (isolated_upper / "ros_both" / "manifest.json").write_text(json.dumps({"status": "raw_ready", "episodes": [{"a": 1}], "files": []}))
    monkeypatch.setattr(ros_mobile, "_http", lambda *a, **k: {"sessions": [{"session_id": "ros_both", "status": "raw_ready"}, {"session_id": "ros_car_only", "status": "recording"}]})
    out = ros_mobile.sessions()
    assert "agent_error" not in out
    by_id = {s["session_id"]: s for s in out["sessions"]}
    assert by_id["ros_both"]["upper"]["episodes"] == 1
    assert by_id["ros_car_only"]["upper"] is None
