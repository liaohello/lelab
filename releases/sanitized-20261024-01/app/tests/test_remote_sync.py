"""Tests for train-server profiles, sync planning and dry runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lelab.remote_sync import DatasetSyncError, DatasetSyncManager, TrainServerConfig
from lelab.utils.servers import resolved_env, train_policy_endpoint

TRAIN_ENV = {
    "LELAB_TRAIN_HOST": "192.0.2.10",
    "LELAB_TRAIN_PORT": "22",
    "LELAB_TRAIN_USER": "trainer",
    "LELAB_TRAIN_ROOT": "~/Desktop/lelab_server",
}


def _make_dataset(root: Path, repo_id: str) -> Path:
    path = root.joinpath(*repo_id.split("/"))
    (path / "meta").mkdir(parents=True, exist_ok=True)
    (path / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (path / "data" / "chunk-000" / "file-000.parquet").write_bytes(b"parquet")
    (path / "meta" / "info.json").write_text(
        json.dumps({"codebase_version": "v3.0", "fps": 30, "robot_type": "so101_follower"}),
        encoding="utf-8",
    )
    return path


def test_config_requires_host_user_and_root() -> None:
    with pytest.raises(DatasetSyncError) as excinfo:
        TrainServerConfig.from_env({"LELAB_TRAIN_HOST": "example.org"})
    assert "LELAB_TRAIN_USER" in str(excinfo.value)
    assert "LELAB_TRAIN_ROOT" in str(excinfo.value)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"LELAB_TRAIN_HOST": "-evil"}, "unsupported characters"),
        ({"LELAB_TRAIN_USER": "-root"}, "not a valid SSH user"),
        ({"LELAB_TRAIN_PORT": "0"}, "between 1 and 65535"),
        ({"LELAB_TRAIN_PORT": "ssh"}, "must be an integer"),
        ({"LELAB_TRAIN_ROOT": "Desktop/lelab_server"}, "absolute path"),
        ({"LELAB_TRAIN_ROOT": "~/Desktop/../etc"}, "normalized directory"),
    ],
)
def test_config_rejects_unsafe_values(overrides: dict[str, str], expected: str) -> None:
    with pytest.raises(DatasetSyncError) as excinfo:
        TrainServerConfig.from_env({**TRAIN_ENV, **overrides})
    assert expected in str(excinfo.value)


def test_config_accepts_a_tilde_root() -> None:
    config = TrainServerConfig.from_env(TRAIN_ENV)
    assert config.host == "192.0.2.10"
    assert config.port == 22
    assert config.root == "~/Desktop/lelab_server"
    assert config.endpoint == "trainer@192.0.2.10"


def test_profile_file_supplies_defaults_and_env_wins(tmp_path: Path) -> None:
    profile = tmp_path / "servers.toml"
    profile.write_text(
        "\n".join(
            [
                "[train_server]",
                'host = "10.0.0.5"',
                "port = 2222",
                'user = "trainer"',
                'root = "~/srv/lelab"',
                "policy_port = 28081",
                "",
                "[jetson]",
                'host = "192.168.1.50"',
                "port = 22",
                'user = "nvidia"',
            ]
        ),
        encoding="utf-8",
    )
    env = {"LELAB_SERVERS_CONFIG": str(profile)}

    values = resolved_env(env)
    assert values["LELAB_TRAIN_HOST"] == "10.0.0.5"
    assert values["LELAB_TRAIN_PORT"] == "2222"
    assert values["LELAB_JETSON_HOST"] == "192.168.1.50"

    overridden = resolved_env({**env, "LELAB_TRAIN_HOST": "example.org"})
    assert overridden["LELAB_TRAIN_HOST"] == "example.org"

    config = TrainServerConfig.from_env(env)
    assert config.port == 2222
    assert config.user == "trainer"

    assert train_policy_endpoint(env) == ("127.0.0.1", 28081)


def test_policy_endpoint_defaults_to_loopback_28081() -> None:
    assert train_policy_endpoint({}) == ("127.0.0.1", 28081)
    assert train_policy_endpoint({"LELAB_TRAIN_POLICY_PORT": "not-a-port"}) == (
        "127.0.0.1",
        28081,
    )


def test_dry_run_builds_parameter_arrays_and_launches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset_root = tmp_path / "datasets"
    dataset_root.mkdir()
    monkeypatch.setenv("HF_LEROBOT_HOME", str(dataset_root))
    _make_dataset(dataset_root, "local/pick_blocks")

    def explode(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("dry run must not spawn subprocesses")

    monkeypatch.setattr("lelab.remote_sync.subprocess.Popen", explode)
    manager = DatasetSyncManager(config=TrainServerConfig.from_env(TRAIN_ENV))

    status = manager.start("local/pick_blocks", dry_run=True)

    assert status["phase"] == "dry_run"
    commands = status["commands"]
    assert commands["check"][0] == "ssh"
    assert "-p" in commands["check"] and "22" in commands["check"]
    assert "BatchMode=yes" in commands["check"]
    assert commands["rsync"][0] == "rsync"
    assert "--append-verify" in commands["rsync"]
    assert commands["rsync"][-1].endswith("/")
    # The destination must keep a plain `~/...` (rsync quotes the argument
    # itself; a `"$HOME"` prefix makes the remote mkdir fail).
    destination = commands["rsync"][-1]
    assert destination.startswith(f"{TRAIN_ENV['LELAB_TRAIN_USER']}@")
    assert ":~/Desktop/lelab_server/datasets/.incoming/" in destination
    assert "$HOME" not in destination
    assert status["paths"]["target"].endswith("datasets/local/pick_blocks")
    assert status["paths"]["source"].replace("\\", "/").endswith("local/pick_blocks")


def test_sync_rejects_repo_ids_outside_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset_root = tmp_path / "datasets"
    dataset_root.mkdir()
    monkeypatch.setenv("HF_LEROBOT_HOME", str(dataset_root))
    manager = DatasetSyncManager(config=TrainServerConfig.from_env(TRAIN_ENV))

    with pytest.raises(DatasetSyncError):
        manager.start("../escape", dry_run=True)


def test_sync_requires_an_existing_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset_root = tmp_path / "datasets"
    dataset_root.mkdir()
    monkeypatch.setenv("HF_LEROBOT_HOME", str(dataset_root))
    manager = DatasetSyncManager(config=TrainServerConfig.from_env(TRAIN_ENV))

    with pytest.raises(DatasetSyncError) as excinfo:
        manager.start("local/missing", dry_run=True)
    assert "not found" in str(excinfo.value)


def test_repo_id_is_quoted_in_the_remote_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset_root = tmp_path / "datasets"
    dataset_root.mkdir()
    monkeypatch.setenv("HF_LEROBOT_HOME", str(dataset_root))
    _make_dataset(dataset_root, "local/my dataset")
    manager = DatasetSyncManager(config=TrainServerConfig.from_env(TRAIN_ENV))

    status = manager.start("local/my dataset", dry_run=True)

    finalize = status["commands"]["finalize"]
    assert any("local/my dataset" in argument or "'local/my dataset'" in argument for argument in finalize)
    # The path never becomes a raw shell fragment: it stays one argv element.
    assert "local/my dataset" in " ".join(finalize)
