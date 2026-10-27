"""Tests for composite datasets: planning, compatibility, build and remote ops."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from lelab.dataset_storage import clear_stats_cache
from lelab.scripts import build_composite
from tests.test_dataset_management import _make_dataset


@pytest.fixture()
def dataset_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "datasets"
    root.mkdir()
    monkeypatch.setenv("HF_LEROBOT_HOME", str(root))
    clear_stats_cache()
    yield root
    clear_stats_cache()


def _seal(repo_id: str, root: Path) -> dict:
    from lelab.sealing import build_seal_manifest, seal_manifest_path, write_seal_manifest

    manifest = build_seal_manifest(repo_id, root)
    write_seal_manifest(seal_manifest_path(repo_id, root), manifest)
    return manifest


def _plan(repo_ids: list[str], root: Path, **kwargs):
    from lelab.composites import plan_composite

    return plan_composite(repo_ids, root, **kwargs)


def _blocker_codes(plan) -> set[str]:
    return {blocker["code"] for blocker in plan.blockers}


def test_plan_requires_at_least_two_members(dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/one")
    _seal("local/one", dataset_root)

    plan = _plan(["local/one"], dataset_root)

    assert plan.ok is False
    assert "member-count" in _blocker_codes(plan)


def test_plan_requires_sealed_members(dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/a")
    _make_dataset(dataset_root, "local/b")
    _seal("local/a", dataset_root)

    plan = _plan(["local/a", "local/b"], dataset_root)

    assert plan.ok is False
    assert "not-sealed" in _blocker_codes(plan)
    assert plan.composite_hash is None


def test_plan_accepts_matching_sealed_members(dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/a")
    _make_dataset(dataset_root, "local/b")
    _seal("local/a", dataset_root)
    _seal("local/b", dataset_root)

    plan = _plan(["local/a", "local/b"], dataset_root)

    assert plan.ok is True, plan.blockers
    assert plan.composite_hash
    assert plan.composite_repo_id == f"local/composite_{plan.composite_hash}"
    assert plan.estimated_size_bytes > 0
    payload = plan.to_plan_payload()
    assert payload["merge"] == {"concatenate_videos": False, "concatenate_data": False}
    assert [member["repo_id"] for member in payload["members"]] == ["local/a", "local/b"]


def test_plan_rejects_fps_mismatch(dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/a", fps=30)
    _make_dataset(dataset_root, "local/b", fps=15)
    _seal("local/a", dataset_root)
    _seal("local/b", dataset_root)

    plan = _plan(["local/a", "local/b"], dataset_root)

    assert plan.ok is False
    assert "fps" in _blocker_codes(plan)


def test_plan_rejects_camera_key_mismatch(dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/a")
    path_b = _make_dataset(dataset_root, "local/b")
    info_path = path_b / "meta" / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["features"]["observation.images.wrist"] = info["features"].pop("observation.images.front")
    info_path.write_text(json.dumps(info), encoding="utf-8")
    _seal("local/a", dataset_root)
    _seal("local/b", dataset_root)

    plan = _plan(["local/a", "local/b"], dataset_root)

    assert plan.ok is False
    assert "feature-keys" in _blocker_codes(plan)
    message = " ".join(blocker["message"] for blocker in plan.blockers)
    assert "observation.images.front" in message


def test_plan_rejects_feature_shape_mismatch(dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/a")
    path_b = _make_dataset(dataset_root, "local/b")
    info_path = path_b / "meta" / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["features"]["observation.state"]["shape"] = [5]
    info_path.write_text(json.dumps(info), encoding="utf-8")
    _seal("local/a", dataset_root)
    _seal("local/b", dataset_root)

    plan = _plan(["local/a", "local/b"], dataset_root)

    assert plan.ok is False
    assert "feature-shape" in _blocker_codes(plan)


def test_plan_rejects_codebase_version_mismatch(dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/a")
    path_b = _make_dataset(dataset_root, "local/b")
    info_path = path_b / "meta" / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["codebase_version"] = "v2.1"
    info_path.write_text(json.dumps(info), encoding="utf-8")
    _seal("local/a", dataset_root)
    _seal("local/b", dataset_root)

    plan = _plan(["local/a", "local/b"], dataset_root)

    assert plan.ok is False
    assert "codebase-version" in _blocker_codes(plan)


def test_plan_hash_depends_on_member_order(dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/a")
    _make_dataset(dataset_root, "local/b")
    _seal("local/a", dataset_root)
    _seal("local/b", dataset_root)

    forward = _plan(["local/a", "local/b"], dataset_root)
    backward = _plan(["local/b", "local/a"], dataset_root)

    assert forward.ok and backward.ok
    assert forward.composite_hash != backward.composite_hash


def test_plan_normalizes_weights(dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/a")
    _make_dataset(dataset_root, "local/b")
    _make_dataset(dataset_root, "local/c")
    for repo_id in ("local/a", "local/b", "local/c"):
        _seal(repo_id, dataset_root)

    equal = _plan(["local/a", "local/b", "local/c"], dataset_root, weights_mode="equal")
    custom = _plan(
        ["local/a", "local/b", "local/c"],
        dataset_root,
        weights_mode="custom",
        weights=[2, 1, 1],
    )
    bad = _plan(["local/a", "local/b"], dataset_root, weights_mode="custom", weights=[1])

    assert equal.weights == (pytest.approx(1 / 3),) * 3
    assert custom.weights == (0.5, 0.25, 0.25)
    assert "weights-length" in _blocker_codes(bad)


def test_plan_does_not_change_with_weights(dataset_root: Path) -> None:
    _make_dataset(dataset_root, "local/a")
    _make_dataset(dataset_root, "local/b")
    _seal("local/a", dataset_root)
    _seal("local/b", dataset_root)

    frames = _plan(["local/a", "local/b"], dataset_root)
    equal = _plan(["local/a", "local/b"], dataset_root, weights_mode="equal")

    assert frames.composite_hash == equal.composite_hash


# ---------------------------------------------------------------------------
# Real merge (tiny datasets, no videos)
# ---------------------------------------------------------------------------


@pytest.fixture()
def lerobot_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "lerobot_home"
    home.mkdir()
    monkeypatch.setenv("HF_LEROBOT_HOME", str(home))
    monkeypatch.setattr("lerobot.utils.constants.HF_LEROBOT_HOME", home)
    monkeypatch.setattr("lerobot.datasets.dataset_metadata.HF_LEROBOT_HOME", home)
    clear_stats_cache()
    yield home
    clear_stats_cache()


def _record_tiny(repo_id: str, *, episodes: int = 2, frames: int = 4, fps: int = 30) -> None:
    from lerobot.datasets import LeRobotDataset

    features = {
        "action": {"dtype": "float32", "shape": (2,), "names": ["a", "b"]},
        "observation.state": {"dtype": "float32", "shape": (2,), "names": ["a", "b"]},
    }
    dataset = LeRobotDataset.create(repo_id, fps=fps, features=features, use_videos=False)
    for episode in range(episodes):
        for frame in range(frames):
            values = np.full(2, episode * frames + frame, dtype=np.float32)
            dataset.add_frame({"action": values, "observation.state": values, "task": "composite"})
        dataset.save_episode()
    dataset.finalize()


def _record_tiny_video(repo_id: str, *, episodes: int = 2, frames: int = 3, fps: int = 30) -> None:
    from lerobot.datasets import LeRobotDataset

    features = {
        "action": {"dtype": "float32", "shape": (2,), "names": ["a", "b"]},
        "observation.state": {"dtype": "float32", "shape": (2,), "names": ["a", "b"]},
        "observation.images.cam": {"dtype": "video", "shape": (32, 32, 3), "names": ["h", "w", "c"]},
    }
    dataset = LeRobotDataset.create(repo_id, fps=fps, features=features, use_videos=True)
    for episode in range(episodes):
        for frame in range(frames):
            values = np.full(2, episode * frames + frame, dtype=np.float32)
            dataset.add_frame(
                {
                    "action": values,
                    "observation.state": values,
                    "observation.images.cam": np.full((32, 32, 3), 60 + frame * 20, dtype=np.uint8),
                    "task": "composite",
                }
            )
        dataset.save_episode()
    dataset.finalize()


def test_build_composite_merges_and_leaves_sources_untouched(lerobot_home: Path) -> None:
    _record_tiny("local/one")
    _record_tiny("local/two")
    _seal("local/one", lerobot_home)
    _seal("local/two", lerobot_home)

    source_snapshot = {}
    for repo_id in ("local/one", "local/two"):
        path = lerobot_home.joinpath(*repo_id.split("/"))
        source_snapshot[repo_id] = {
            item.relative_to(path).as_posix(): (item.stat().st_size, item.stat().st_mtime_ns)
            for item in path.rglob("*")
            if item.is_file()
        }

    plan = _plan(["local/one", "local/two"], lerobot_home)
    assert plan.ok, plan.blockers
    messages: list[dict] = []
    result = build_composite.build(
        plan.to_plan_payload(),
        lerobot_home,
        build_id="pytest",
        python_executable=None,
        progress_cb=messages.append,
        skip_disk_check=True,
    )

    composite_dir = lerobot_home.joinpath(*result["composite_repo_id"].split("/"))
    assert (composite_dir / ".ready").is_file()
    marker = json.loads((composite_dir / "meta" / "lelab-composite.json").read_text("utf-8"))
    assert marker["repo_ids"] == ["local/one", "local/two"]
    assert [entry["phase"] for entry in messages][-1] == "ready"

    from lerobot.datasets import LeRobotDataset

    merged = LeRobotDataset(result["composite_repo_id"], root=composite_dir)
    assert merged.num_episodes == 4
    assert merged.num_frames == 16

    for repo_id, snapshot in source_snapshot.items():
        path = lerobot_home.joinpath(*repo_id.split("/"))
        after = {
            item.relative_to(path).as_posix(): (item.stat().st_size, item.stat().st_mtime_ns)
            for item in path.rglob("*")
            if item.is_file()
        }
        assert after == snapshot, f"{repo_id} was modified by the merge"

    # A second build is a no-op that reports the cached copy.
    again = build_composite.build(
        plan.to_plan_payload(), lerobot_home, build_id="pytest-2", skip_disk_check=True
    )
    assert again["cached"] is True


def test_build_refuses_when_member_metadata_changed(lerobot_home: Path) -> None:
    _record_tiny("local/one")
    _record_tiny("local/two")
    _seal("local/one", lerobot_home)
    _seal("local/two", lerobot_home)
    plan = _plan(["local/one", "local/two"], lerobot_home)

    info_path = lerobot_home / "local" / "two" / "meta" / "info.json"
    info = json.loads(info_path.read_text("utf-8"))
    info["total_frames"] = int(info["total_frames"]) + 1
    info_path.write_text(json.dumps(info), encoding="utf-8")

    with pytest.raises(build_composite.CompositeBuildError) as excinfo:
        build_composite.build(plan.to_plan_payload(), lerobot_home, build_id="pytest", skip_disk_check=True)

    assert "differs from the sealed copy" in str(excinfo.value)


def test_build_composite_with_videos_decodes_every_episode(lerobot_home: Path) -> None:
    """Videos are stream-copied and re-timestamped; both members must stay readable."""
    from lerobot.datasets import LeRobotDataset

    _record_tiny_video("local/one")
    _record_tiny_video("local/two")
    _seal("local/one", lerobot_home)
    _seal("local/two", lerobot_home)
    plan = _plan(["local/one", "local/two"], lerobot_home)
    assert plan.ok, plan.blockers

    result = build_composite.build(
        plan.to_plan_payload(), lerobot_home, build_id="pytest-video", skip_disk_check=True
    )
    composite_dir = lerobot_home.joinpath(*result["composite_repo_id"].split("/"))
    merged = LeRobotDataset(result["composite_repo_id"], root=composite_dir)

    assert merged.num_episodes == 4
    assert merged.num_frames == 12
    assert merged.meta.stats
    # Reading the first frame of the first episode of each member proves the
    # concatenated video indices and timestamps survived the merge.
    first = merged[0]
    assert first["observation.images.cam"].shape == (3, 32, 32)
    member_2_offset = 6
    second = merged[member_2_offset]
    assert second["observation.images.cam"].shape == (3, 32, 32)


# ---------------------------------------------------------------------------
# Remote inventory / trash / restore (SSH stubbed)
# ---------------------------------------------------------------------------


def test_list_remote_composites_parses_python_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from lelab import composites

    payload = [{"repo_id": "local/composite_abc", "members": ["local/a", "local/b"], "ready": True}]
    monkeypatch.setattr(
        composites.ssh_ops,
        "run_python",
        lambda *args, **kwargs: (0, "noise\n" + json.dumps(payload) + "\n"),
    )
    config = composites.TrainServerConfig(host="h", port=22, user="u", root="~/lelab_server")

    assert composites.list_remote_composites(config=config) == payload


def test_trash_remote_composite_rejects_foreign_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    from lelab import composites

    with pytest.raises(composites.CompositeError):
        composites.trash_remote_composite("local/not-a-composite")


def test_trash_remote_composite_reports_missing_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from lelab import composites

    monkeypatch.setattr(composites.ssh_ops, "run_script", lambda *args, **kwargs: (3, ""))
    config = composites.TrainServerConfig(host="h", port=22, user="u", root="~/lelab_server")

    with pytest.raises(composites.CompositeError) as excinfo:
        composites.trash_remote_composite("local/composite_abc", config=config)

    assert "不存在或未就绪" in str(excinfo.value)


def test_build_manager_streams_the_builder_over_ssh(
    dataset_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remote build path: source on stdin, plan in argv, progress parsed."""
    import time

    from lelab import composites

    _make_dataset(dataset_root, "local/a")
    _make_dataset(dataset_root, "local/b")
    _seal("local/a", dataset_root)
    _seal("local/b", dataset_root)
    plan = _plan(["local/a", "local/b"], dataset_root)
    assert plan.ok, plan.blockers

    captured: dict = {}

    class FakeStdin:
        def write(self, payload: bytes) -> None:
            captured["source"] = payload

        def close(self) -> None:
            captured["closed"] = True

    class FakeProcess:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            self.stdin = FakeStdin()
            self.stdout = iter(
                [
                    b"LELAB_PROGRESS {\"phase\": \"verifying\", \"progress\": 0.02}\n",
                    b"loading datasets...\n",
                    b"LELAB_PROGRESS {\"phase\": \"ready\", \"progress\": 1.0}\n",
                ]
            )

        def terminate(self) -> None:  # pragma: no cover - not cancelled here
            captured["terminated"] = True

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(composites.subprocess, "Popen", FakeProcess)
    manager = composites.CompositeBuildManager()
    config = composites.TrainServerConfig(
        host="train.example", port=22, user="lab", root="~/lelab_server"
    )

    manager.start(plan, runner="ssh_remote", config=config, python="python3")
    status = manager.status(plan.composite_hash)
    for _ in range(500):
        if status["state"] in {"ready", "failed", "cancelled"}:
            break
        time.sleep(0.01)
        status = manager.status(plan.composite_hash)

    assert status["state"] == "ready"
    source = captured["source"].decode("utf-8")
    assert "Standalone composite-dataset builder" in source
    assert captured["closed"] is True
    remote_command = captured["command"][-1]
    assert "--plan-json" in remote_command
    assert "--datasets-root '~/lelab_server/datasets'" in remote_command
    assert captured["kwargs"].get("stdin") is not None
    # The Python source is streamed as bytes so Windows cannot rewrite it to CRLF.
    assert b"\r\n" not in captured["source"]
    # Only the progress lines are parsed; ordinary output lands in the log tail.
    assert status["log_tail"] == ["loading datasets..."]


def test_build_manager_local_runner_produces_a_ready_composite(lerobot_home: Path) -> None:
    """The in-process runner used by tests/CLI: full build through the manager."""
    import time

    from lelab import composites

    _record_tiny("local/one")
    _record_tiny("local/two")
    _seal("local/one", lerobot_home)
    _seal("local/two", lerobot_home)
    plan = _plan(["local/one", "local/two"], lerobot_home)
    assert plan.ok, plan.blockers

    manager = composites.CompositeBuildManager()
    manager.start(plan, runner="local")
    status = manager.status(plan.composite_hash)
    for _ in range(2000):
        if status["state"] in {"ready", "failed", "cancelled"}:
            break
        time.sleep(0.05)
        status = manager.status(plan.composite_hash)

    assert status["state"] == "ready", status
    assert status["progress"] == 1.0
    composite_dir = lerobot_home / "local" / f"composite_{plan.composite_hash}"
    assert (composite_dir / ".ready").is_file()
    assert (composite_dir / "meta" / "lelab-composite.json").is_file()
