"""Tests for dataset sealing: async seal, immutability enforcement, verify."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lelab.dataset_storage import clear_stats_cache
from tests.test_dataset_management import _make_dataset


@pytest.fixture()
def dataset_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "datasets"
    root.mkdir()
    monkeypatch.setenv("HF_LEROBOT_HOME", str(root))
    clear_stats_cache()
    yield root
    clear_stats_cache()


def _seal_and_wait(client: TestClient, repo_id: str, *, timeout: float = 10.0) -> dict:
    response = client.post(f"/datasets/local/{repo_id}/seal")
    assert response.status_code == 202, response.text
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/datasets/local/{repo_id}/seal").json()["seal"]
        if status.get("state") in {"sealed", "failed", "cancelled"}:
            return status
        time.sleep(0.02)
    raise AssertionError(f"sealing {repo_id} did not finish within {timeout}s")


def test_summary_reports_unsealed_by_default(client: TestClient, dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/pick_blocks")

    summary = client.get("/datasets/local/local/pick_blocks/summary").json()

    assert summary["sealed"] is False
    assert summary["seal_state"] == "unsealed"


def test_seal_hashes_payload_and_marks_dataset_sealed(
    client: TestClient, dataset_root: Path
) -> None:
    path = _make_dataset(dataset_root, "local/pick_blocks")
    (path / ".ready").write_text("sync-id\n", encoding="utf-8")

    status = _seal_and_wait(client, "local/pick_blocks")

    assert status["state"] == "sealed"
    manifest = json.loads((path / "meta" / ".lelab-sealed.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "sealed"
    assert manifest["manifest_sha256"]
    listed = {entry["path"] for entry in manifest["files"]}
    assert "data/chunk-000/file-000.parquet" in listed
    assert "meta/info.json" in listed
    # Transfer markers and the seal itself are not dataset payload.
    assert ".ready" not in listed
    assert "meta/.lelab-sealed.json" not in listed

    summary = client.get("/datasets/local/local/pick_blocks/summary").json()
    assert summary["sealed"] is True
    assert summary["seal_manifest_sha256"] == manifest["manifest_sha256"]

    listing = client.get("/datasets/local").json()
    entry = next(item for item in listing if item["repo_id"] == "local/pick_blocks")
    assert entry["sealed"] is True


def test_seal_is_idempotent(client: TestClient, dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/pick_blocks")
    first = _seal_and_wait(client, "local/pick_blocks")

    second = client.post("/datasets/local/local/pick_blocks/seal").json()["seal"]

    assert second["state"] == "sealed"
    assert second["manifest_sha256"] == first["manifest_sha256"]


def test_seal_refused_for_unfinalized_dataset(client: TestClient, dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/broken", finalized=False)

    response = client.post("/datasets/local/local/broken/seal")

    assert response.status_code == 409
    assert "integrity check failed" in response.json()["detail"]


def test_sealed_dataset_cannot_be_trashed_or_deleted(
    client: TestClient, dataset_root: Path
) -> None:
    _make_dataset(dataset_root, "local/pick_blocks")
    _seal_and_wait(client, "local/pick_blocks")

    trash = client.post("/datasets/local/local/pick_blocks/trash")
    delete = client.post("/delete-dataset", json={"dataset_repo_id": "local/pick_blocks"})

    assert trash.status_code == 409
    assert "sealed" in trash.json()["detail"]
    assert delete.status_code == 409
    assert (dataset_root / "local" / "pick_blocks").is_dir()


def test_unseal_restores_mutability(client: TestClient, dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/pick_blocks")
    _seal_and_wait(client, "local/pick_blocks")

    unseal = client.post("/datasets/local/local/pick_blocks/unseal")
    assert unseal.status_code == 200
    assert unseal.json()["sealed"] is False

    status = client.get("/datasets/local/local/pick_blocks/seal").json()["seal"]
    assert status["state"] == "unsealed"
    trashed = client.post("/datasets/local/local/pick_blocks/trash")
    assert trashed.status_code == 200


def test_verify_detects_changed_payload(client: TestClient, dataset_root: Path) -> None:
    path = _make_dataset(dataset_root, "local/pick_blocks")
    _seal_and_wait(client, "local/pick_blocks")

    ok = client.post("/datasets/local/local/pick_blocks/seal/verify").json()
    assert ok["ok"] is True

    (path / "data" / "chunk-000" / "file-000.parquet").write_bytes(b"tampered")
    changed = client.post("/datasets/local/local/pick_blocks/seal/verify").json()

    assert changed["ok"] is False
    assert changed["changed"] == ["data/chunk-000/file-000.parquet"]


def test_resume_recording_into_sealed_dataset_is_refused(
    client: TestClient, dataset_root: Path
) -> None:
    from lelab import record
    from lelab.record import RecordingRequest

    _make_dataset(dataset_root, "local/pick_blocks")
    _seal_and_wait(client, "local/pick_blocks")

    request = RecordingRequest(
        leader_port="/dev/null",
        follower_port="/dev/null",
        leader_config="leader.json",
        follower_config="follower.json",
        dataset_repo_id="local/pick_blocks",
        single_task="pick",
        resume=True,
    )
    result = record.handle_start_recording(request)

    assert result["success"] is False
    assert "sealed" in result["message"]
    assert record.recording_active is False


def test_sync_is_still_allowed_for_sealed_dataset(
    client: TestClient, dataset_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_dataset(dataset_root, "local/pick_blocks")
    _seal_and_wait(client, "local/pick_blocks")
    monkeypatch.setenv("LELAB_TRAIN_HOST", "192.0.2.10")
    monkeypatch.setenv("LELAB_TRAIN_PORT", "22")
    monkeypatch.setenv("LELAB_TRAIN_USER", "trainer")
    monkeypatch.setenv("LELAB_TRAIN_ROOT", "~/Desktop/lelab_server")

    response = client.post(
        "/datasets/local/local/pick_blocks/sync-train", json={"dry_run": True}
    )

    assert response.status_code == 202
