"""Local model store: retention, trash/restore, purge (no SSH/rsync)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lelab import model_store


def _make_model(root: Path, job_id: str, *, pulled_at: float, step: int = 10, size: int = 4) -> None:
    target = root / job_id
    pretrained = target / "checkpoints" / f"{step:06d}" / "pretrained_model"
    pretrained.mkdir(parents=True, exist_ok=True)
    (pretrained / "model.safetensors").write_bytes(b"x" * size)
    (target / "model.json").write_text(
        json.dumps({"job_id": job_id, "step": step, "pulled_at": pulled_at, "source": "train"}),
        encoding="utf-8",
    )
    (target / ".ready").write_text("ok\n", encoding="utf-8")


def test_list_is_newest_first_and_reports_size(tmp_path: Path) -> None:
    _make_model(tmp_path, "old", pulled_at=1000.0, size=2048)
    _make_model(tmp_path, "new", pulled_at=2000.0)

    models = model_store.list_local_models(tmp_path)

    assert [m["job_id"] for m in models] == ["new", "old"]
    assert models[1]["size_mb"] == pytest.approx(2048 / 1024 / 1024, abs=0.01)


def test_retention_moves_old_models_to_trash(tmp_path: Path) -> None:
    for index in range(4):
        _make_model(tmp_path, f"job{index}", pulled_at=1000.0 + index)

    moved = model_store.enforce_retention(keep=2, root=tmp_path)

    assert len(moved) == 2
    remaining = [m["job_id"] for m in model_store.list_local_models(tmp_path)]
    assert remaining == ["job3", "job2"]
    trashed = model_store.list_trashed_models(tmp_path)
    assert {entry["job_id"] for entry in trashed} == {"job0", "job1"}
    # Nothing is deleted by the retention pass, and `model.json` records when.
    for entry in trashed:
        assert Path(entry["path"]).is_dir()
        assert entry["trashed_at"]


def test_restore_and_purge_round_trip(tmp_path: Path) -> None:
    _make_model(tmp_path, "job", pulled_at=1.0)
    trash_name = model_store.trash_model("job", root=tmp_path)
    assert not (tmp_path / "job").exists()

    restored = model_store.restore_model(trash_name, root=tmp_path)
    assert restored == "job"
    assert (tmp_path / "job" / ".ready").is_file()

    trash_name = model_store.trash_model("job", root=tmp_path)
    model_store.purge_model(trash_name, root=tmp_path)
    assert not (tmp_path / ".trash" / trash_name).exists()
    assert model_store.list_trashed_models(tmp_path) == []


def test_restore_refuses_to_overwrite(tmp_path: Path) -> None:
    _make_model(tmp_path, "job", pulled_at=1.0)
    trash_name = model_store.trash_model("job", root=tmp_path)
    _make_model(tmp_path, "job", pulled_at=2.0)
    with pytest.raises(FileExistsError):
        model_store.restore_model(trash_name, root=tmp_path)


def test_keep_and_root_are_configurable() -> None:
    assert model_store.keep_local_models({}) == 10
    assert model_store.keep_local_models({"LELAB_LOCAL_MODEL_KEEP": "3"}) == 3
    assert model_store.keep_local_models({"LELAB_LOCAL_MODEL_KEEP": "junk"}) == 10
    configured = model_store.model_root({"LELAB_MODEL_ROOT": "/tmp/models"})
    assert configured.name == "models" and configured.parent.name == "tmp"


def test_pull_state_starts_idle() -> None:
    assert model_store.pull_state()["phase"] in {"idle", "completed", "failed"}
