"""Integrity tests for the verified upper-host session copy."""
from __future__ import annotations

import json
import shutil
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
