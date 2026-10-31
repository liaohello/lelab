"""Tests for dataset listing, inspection, trash/restore and sync wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lelab.dataset_storage import clear_stats_cache


def _make_dataset(
    root: Path,
    repo_id: str,
    *,
    finalized: bool = True,
    episodes: int = 2,
    fps: int = 30,
    frames: int = 60,
    with_video: bool = True,
) -> Path:
    """Create the smallest on-disk structure LeRobot tooling recognizes."""
    path = root.joinpath(*repo_id.split("/"))
    (path / "meta").mkdir(parents=True, exist_ok=True)
    (path / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (path / "data" / "chunk-000" / "file-000.parquet").write_bytes(b"parquet")
    if finalized:
        (path / "meta" / "episodes" / "chunk-000").mkdir(parents=True, exist_ok=True)
        (path / "meta" / "episodes" / "chunk-000" / "file-000.parquet").write_bytes(b"index")
    features: dict[str, object] = {
        "observation.state": {"dtype": "float32", "shape": [6], "names": ["j1"]},
        "action": {"dtype": "float32", "shape": [6]},
    }
    if with_video:
        features["observation.images.front"] = {
            "dtype": "video",
            "shape": [480, 640, 3],
            "info": {"video.codec": "h264", "video.fps": fps},
        }
        (path / "videos" / "chunk-000" / "observation.images.front").mkdir(
            parents=True, exist_ok=True
        )
        (
            path / "videos" / "chunk-000" / "observation.images.front" / "file-000.mp4"
        ).write_bytes(b"mp4")
    info = {
        "codebase_version": "v3.0",
        "robot_type": "so101_follower",
        "fps": fps,
        "total_episodes": episodes,
        "total_frames": frames,
        "total_tasks": 1,
        "total_videos": 1 if with_video else 0,
        "chunks_size": 1000,
        "data_path": "data/chunk-{episode_chunk:03d}/file-{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/file-{episode_index:06d}.mp4",
        "features": features,
    }
    (path / "meta" / "info.json").write_text(json.dumps(info), encoding="utf-8")
    return path


@pytest.fixture()
def dataset_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "datasets"
    root.mkdir()
    monkeypatch.setenv("HF_LEROBOT_HOME", str(root))
    clear_stats_cache()
    yield root
    clear_stats_cache()


def test_local_listing_carries_summary_fields(
    client: TestClient, dataset_root: Path
) -> None:
    _make_dataset(dataset_root, "local/pick_blocks")

    items = client.get("/datasets/local").json()

    assert len(items) == 1
    item = items[0]
    assert item["repo_id"] == "local/pick_blocks"
    assert item["total_episodes"] == 2
    assert item["total_frames"] == 60
    assert item["fps"] == 30
    assert item["robot_type"] == "so101_follower"
    assert item["camera_count"] == 1
    assert item["duration_seconds"] == pytest.approx(2.0)
    assert item["finalized"] is True
    assert item["size_bytes"] is not None and item["size_bytes"] > 0
    assert item["sync"]["state"] == "not-synced"


def test_listing_ignores_hidden_trash_directory(
    client: TestClient, dataset_root: Path
) -> None:
    _make_dataset(dataset_root / ".trash" / "20260101T000000Z-local__x", "local/x")
    assert client.get("/datasets/local").json() == []


def test_summary_endpoint_reports_unfinalized_dataset(
    client: TestClient, dataset_root: Path
) -> None:
    _make_dataset(dataset_root, "local/broken", finalized=False)

    response = client.get("/datasets/local/local/broken/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["finalized"] is False
    assert body["camera_count"] == 1
    assert body["path"].replace("\\", "/").endswith("local/broken")


def test_summary_endpoint_rejects_traversal(client: TestClient, dataset_root: Path) -> None:
    response = client.get("/datasets/local/..%2Fsecret/summary")
    assert response.status_code in {400, 404}


def test_summary_endpoint_404_for_missing_dataset(
    client: TestClient, dataset_root: Path
) -> None:
    response = client.get("/datasets/local/local/nope/summary")
    assert response.status_code == 404


def test_integrity_flags_missing_episode_index(
    client: TestClient, dataset_root: Path
) -> None:
    _make_dataset(dataset_root, "local/unfinished", finalized=False)

    body = client.get("/datasets/local/local/unfinished/integrity").json()

    assert body["status"] == "error"
    checks = {check["name"]: check for check in body["checks"]}
    assert checks["episodes-index"]["status"] == "error"
    assert checks["data"]["status"] == "ok"


def test_integrity_ok_for_complete_dataset(client: TestClient, dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/good")
    body = client.get("/datasets/local/local/good/integrity").json()
    assert body["status"] == "ok"


def test_schema_endpoint_exposes_features(client: TestClient, dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/good")

    body = client.get("/datasets/local/local/good/schema").json()

    names = {feature["name"] for feature in body["features"]}
    assert {"observation.state", "action", "observation.images.front"} <= names
    video = next(f for f in body["features"] if f["name"] == "observation.images.front")
    assert video["storage"] == "video"
    assert video["shape"] == [480, 640, 3]


def test_files_endpoint_returns_templates(client: TestClient, dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/good")

    body = client.get("/datasets/local/local/good/files").json()

    assert "data_path" in body["storage_templates"]
    assert body["file_tree"]["tree"]["type"] == "directory"
    assert body["storage"]["file_count"] >= 3


def test_trash_and_restore_round_trip(client: TestClient, dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/pick_blocks")

    trashed = client.post("/datasets/local/local/pick_blocks/trash", json={"reason": "test"})

    assert trashed.status_code == 200
    payload = trashed.json()["trashed"]
    assert payload["repo_id"] == "local/pick_blocks"
    assert payload["reason"] == "test"
    assert client.get("/datasets/local").json() == []

    entries = client.get("/datasets/local/trash").json()["entries"]
    assert len(entries) == 1
    assert entries[0]["repo_id"] == "local/pick_blocks"

    restored = client.post("/datasets/local/local/pick_blocks/restore")

    assert restored.status_code == 200
    assert restored.json()["restored"]["repo_id"] == "local/pick_blocks"
    assert client.get("/datasets/local/trash").json()["entries"] == []
    assert len(client.get("/datasets/local").json()) == 1
    assert (dataset_root / "local" / "pick_blocks" / "meta" / "info.json").is_file()


def test_restore_refuses_to_overwrite(client: TestClient, dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/pick_blocks")
    client.post("/datasets/local/local/pick_blocks/trash")
    _make_dataset(dataset_root, "local/pick_blocks")

    response = client.post("/datasets/local/local/pick_blocks/restore")

    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]


def test_trash_is_refused_while_recording(
    client: TestClient, dataset_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import server

    _make_dataset(dataset_root, "local/pick_blocks")
    monkeypatch.setattr(server._record, "recording_active", True, raising=False)
    monkeypatch.setattr(server._record, "recording_config", None, raising=False)

    response = client.post("/datasets/local/local/pick_blocks/trash")

    assert response.status_code == 409
    assert (dataset_root / "local" / "pick_blocks").is_dir()


def test_trash_is_refused_while_training(
    client: TestClient, dataset_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import server
    from lelab.jobs import JobRecord
    from lelab.train import TrainingRequest

    _make_dataset(dataset_root, "local/pick_blocks")
    record = JobRecord(
        id="job-1",
        name="pick",
        state="running",
        config=TrainingRequest(dataset_repo_id="local/pick_blocks"),
        output_dir=str(dataset_root / "out"),
        started_at=0.0,
    )
    monkeypatch.setattr(server.job_registry, "list", lambda limit=10: [record])

    response = client.post("/datasets/local/local/pick_blocks/trash")

    assert response.status_code == 409
    assert "job-1" in response.json()["detail"]


def test_sync_dry_run_lists_commands_without_transferring(
    client: TestClient, dataset_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_dataset(dataset_root, "local/pick_blocks")
    monkeypatch.setenv("LELAB_TRAIN_HOST", "192.0.2.10")
    monkeypatch.setenv("LELAB_TRAIN_PORT", "22")
    monkeypatch.setenv("LELAB_TRAIN_USER", "trainer")
    monkeypatch.setenv("LELAB_TRAIN_ROOT", "~/Desktop/lelab_server")

    response = client.post(
        "/datasets/local/local/pick_blocks/sync-train", json={"dry_run": True}
    )

    assert response.status_code == 202
    commands = response.json()["sync"]["commands"]
    assert commands["rsync"][0] == "rsync"
    assert "22" in commands["check"]
    assert response.json()["sync"]["paths"]["target"].endswith("datasets/local/pick_blocks")

    status = client.get("/datasets/local/local/pick_blocks/sync-status").json()
    assert status["active"] is False
    assert status["state"]["state"] == "not-synced"


def test_sync_without_configuration_fails_cleanly(
    client: TestClient, dataset_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_dataset(dataset_root, "local/pick_blocks")
    for name in (
        "LELAB_TRAIN_HOST",
        "LELAB_TRAIN_USER",
        "LELAB_TRAIN_ROOT",
        "LELAB_SERVERS_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)
    # Point the profile lookup at an empty directory so a developer's own
    # ~/.config/lelab/servers.toml cannot make this test pass by accident.
    monkeypatch.setenv("LELAB_SERVERS_CONFIG", str(dataset_root / "missing.toml"))

    response = client.post("/datasets/local/local/pick_blocks/sync-train")

    assert response.status_code == 400
    assert "Missing training-server configuration" in response.json()["detail"]
