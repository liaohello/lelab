"""API tests for the composite-dataset endpoints."""

from __future__ import annotations

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


def _seal(repo_id: str, root: Path) -> None:
    from lelab.sealing import build_seal_manifest, seal_manifest_path, write_seal_manifest

    write_seal_manifest(seal_manifest_path(repo_id, root), build_seal_manifest(repo_id, root))


def _sealed_pair(root: Path) -> list[str]:
    _make_dataset(root, "local/a")
    _make_dataset(root, "local/b")
    _seal("local/a", root)
    _seal("local/b", root)
    return ["local/a", "local/b"]


def test_compatibility_reports_missing_seal(
    client: TestClient, dataset_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import composites

    _make_dataset(dataset_root, "local/a")
    _make_dataset(dataset_root, "local/b")
    _seal("local/a", dataset_root)
    monkeypatch.setattr(
        composites, "remote_status", lambda repo_id, **kwargs: {"state": "missing"}
    )

    response = client.post(
        "/datasets/compatibility", json={"repo_ids": ["local/a", "local/b"]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert {blocker["code"] for blocker in body["blockers"]} == {"not-sealed"}


def test_compatibility_reports_cached_composite(
    client: TestClient, dataset_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import composites

    repo_ids = _sealed_pair(dataset_root)
    monkeypatch.setattr(
        composites, "remote_status", lambda repo_id, **kwargs: {"state": "ready", "repo_id": repo_id}
    )

    body = client.post("/datasets/compatibility", json={"repo_ids": repo_ids}).json()

    assert body["ok"] is True
    assert body["cached"] is True
    assert body["remote_state"] == "ready"
    assert body["composite_repo_id"].startswith("local/composite_")
    assert body["members"][0]["sealed"] is True


def test_compatibility_survives_unreachable_train_server(
    client: TestClient, dataset_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import composites

    repo_ids = _sealed_pair(dataset_root)

    def boom(*args, **kwargs):
        raise composites.CompositeError("训练机未配置")

    monkeypatch.setattr(composites, "remote_status", boom)

    body = client.post("/datasets/compatibility", json={"repo_ids": repo_ids}).json()

    assert body["ok"] is True
    assert body["remote_state"] == "unknown"
    assert "训练机未配置" in body["remote_error"]


def test_build_refuses_blocked_selection(client: TestClient, dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/a")

    response = client.post("/datasets/composite", json={"repo_ids": ["local/a"]})

    assert response.status_code == 409
    assert "至少需要选择 2 个数据集" in response.json()["detail"]


def test_build_starts_on_training_server(
    client: TestClient, dataset_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import composites

    repo_ids = _sealed_pair(dataset_root)
    started: dict = {}

    class FakeManager:
        def start(self, plan, **kwargs):
            started["plan"] = plan
            started["runner"] = kwargs.get("runner")
            return {
                "composite_hash": plan.composite_hash,
                "composite_repo_id": plan.composite_repo_id,
                "state": "verifying",
                "progress": 0.0,
            }

    monkeypatch.setattr(composites, "train_server_config", lambda: object())
    monkeypatch.setattr(
        composites, "remote_status", lambda repo_id, **kwargs: {"state": "missing"}
    )
    monkeypatch.setattr(composites, "get_composite_manager", lambda: FakeManager())

    response = client.post(
        "/datasets/composite", json={"repo_ids": repo_ids, "weights_mode": "frames"}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["cached"] is False
    assert body["build"]["state"] == "verifying"
    assert started["runner"] == "ssh_remote"


def test_build_reuses_ready_composite(
    client: TestClient, dataset_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import composites

    repo_ids = _sealed_pair(dataset_root)

    class ExplodingManager:
        def start(self, *args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("a cached composite must not be rebuilt")

    monkeypatch.setattr(composites, "train_server_config", lambda: object())
    monkeypatch.setattr(
        composites, "remote_status", lambda repo_id, **kwargs: {"state": "ready", "repo_id": repo_id}
    )
    monkeypatch.setattr(composites, "get_composite_manager", lambda: ExplodingManager())

    response = client.post("/datasets/composite", json={"repo_ids": repo_ids})

    assert response.status_code == 202
    assert response.json()["cached"] is True


def test_composite_status_returns_build_and_remote(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import composites

    class FakeManager:
        def status(self, key):
            return {"composite_hash": key, "state": "merging", "progress": 0.4}

    monkeypatch.setattr(composites, "get_composite_manager", lambda: FakeManager())
    monkeypatch.setattr(
        composites, "remote_status", lambda repo_id, **kwargs: {"state": "missing", "repo_id": repo_id}
    )

    body = client.get("/datasets/composite/status", params={"key": "local/composite_abc"}).json()

    assert body["build"]["state"] == "merging"
    assert body["remote"]["state"] == "missing"


def test_composite_list_surfaces_remote_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import composites

    monkeypatch.setattr(composites, "list_remote_composites", lambda: [{"repo_id": "local/composite_x"}])
    assert client.get("/datasets/composites").json()["composites"] == [
        {"repo_id": "local/composite_x"}
    ]

    def boom(*args, **kwargs):
        raise composites.CompositeError("训练机不可达")

    monkeypatch.setattr(composites, "list_remote_composites", boom)
    response = client.get("/datasets/composites")
    assert response.status_code == 409


def test_trash_composite_is_refused_while_training(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import server
    from lelab.jobs import JobRecord
    from lelab.train import TrainingRequest

    record = JobRecord(
        id="job-1",
        name="composite run",
        state="running",
        config=TrainingRequest(
            dataset_repo_id="local/composite_abc", dataset_composite_hash="abc"
        ),
        output_dir="/tmp/out",
        started_at=0.0,
        dataset_repo_ids=["local/composite_abc"],
    )
    monkeypatch.setattr(server.job_registry, "list", lambda limit=10: [record])

    response = client.post(
        "/datasets/composites/trash", json={"repo_id": "local/composite_abc"}
    )

    assert response.status_code == 409
    assert "job-1" in response.json()["detail"]


def test_trash_and_restore_composite(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lelab import composites

    monkeypatch.setattr(
        composites,
        "trash_remote_composite",
        lambda repo_id, **kwargs: {"repo_id": repo_id, "entry": "2026-local__composite_abc"},
    )
    monkeypatch.setattr(
        composites,
        "restore_remote_composite",
        lambda repo_id, entry, **kwargs: {"repo_id": repo_id, "entry": entry, "restored": True},
    )

    trashed = client.post(
        "/datasets/composites/trash", json={"repo_id": "local/composite_abc"}
    )
    restored = client.post(
        "/datasets/composites/restore",
        json={"repo_id": "local/composite_abc", "entry": "2026-local__composite_abc"},
    )

    assert trashed.status_code == 200
    assert trashed.json()["trashed"]["entry"] == "2026-local__composite_abc"
    assert restored.status_code == 200
    assert restored.json()["restored"]["restored"] is True
