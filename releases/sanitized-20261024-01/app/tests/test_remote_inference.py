"""Cloud inference: 0.7 command/config contracts and ref staging (no hardware)."""

from __future__ import annotations

import draccus
import pytest
from lerobot.inference import RemoteInferenceConfig
from lerobot.remote_inference import ServerConfig

from lelab.remote_inference import (
    _START_SERVER_SCRIPT,
    _build_client_command,
    _deployment_spec,
    _remote_env,
)
from lelab.rollout import InferenceRequest
from lelab.runners import ssh_ops
from lelab.utils.inference_070 import LEGACY_TOKENS, action_is_dispatchable


def _config():
    from lelab.remote_sync import TrainServerConfig

    return TrainServerConfig.from_env(
        {
            "LELAB_TRAIN_HOST": "192.0.2.10",
            "LELAB_TRAIN_PORT": "22",
            "LELAB_TRAIN_USER": "trainer",
            "LELAB_TRAIN_ROOT": "~/Desktop/lelab_server",
        }
    )


def _request(**kwargs) -> InferenceRequest:
    payload = dict(
        follower_port="/dev/serial/by-id/usb-follower",
        follower_config="follower_a",
        policy_ref="/remote/checkpoints/000050/pretrained_model",
        task="pick the red block",
        cameras={"wrist": {"type": "opencv", "camera_index": 0, "width": 640, "height": 480, "fps": 30}},
        duration_s=30,
        target="remote",
        remote_deployment="so101_red",
        remote_semantics="so101-degrees-gripper-percent-v1",
        remote_state_dim=6,
        remote_action_dim=6,
    )
    payload.update(kwargs)
    return InferenceRequest(**payload)


def test_client_command_targets_the_tunnel(monkeypatch) -> None:
    monkeypatch.setenv("LELAB_MAX_RELATIVE_TARGET", "5")
    monkeypatch.setenv("LELAB_MAX_POSITION_DEVIATION", "off")
    request = _request()
    cmd = _build_client_command(
        request,
        follower_id="follower_a",
        local_port=28081,
        remote_policy_path="/home/trainer/Desktop/lelab_server/models/pretrained/act_so101",
    )
    joined = " ".join(cmd)

    assert "lerobot.scripts.lerobot_rollout" in joined
    assert "--inference.type=remote" in joined
    assert "--inference.endpoint=tcp/127.0.0.1:28081" in joined
    assert "--inference.deployment=so101_red" in joined
    assert "--inference.semantics=so101-degrees-gripper-percent-v1" in joined
    assert "--robot.port=/dev/serial/by-id/usb-follower" in joined
    assert "--robot.id=follower_a" in joined
    assert "--robot.max_relative_target=5.0" in joined
    assert "--robot.max_position_deviation" not in joined
    assert "--task=pick the red block" in joined
    assert "wrist: {type: opencv, index_or_path: 0, width: 640, height: 480, fps: 30" in joined
    assert not [token for token in LEGACY_TOKENS if token in joined]
    # The checkpoint is owned by the server, never passed to the robot client.
    assert "act_so101" not in joined


def test_inference_flags_parse_with_the_real_070_config() -> None:  # noqa: C901
    request = _request(remote_refill_seconds=0.4, remote_action_timeout_s=3.0)
    cmd = _build_client_command(
        request,
        follower_id="follower_a",
        local_port=28081,
        remote_policy_path="/remote/checkpoints/000050/pretrained_model",
    )
    flags = [part for part in cmd if part.startswith("--inference.") and not part.startswith("--inference.type=")]
    config = draccus.parse(
        config_class=RemoteInferenceConfig,
        args=[f"--{part.removeprefix('--inference.')}" for part in flags],
    )
    assert config.type == "remote"
    assert config.deployment == "so101_red"
    assert config.semantics == "so101-degrees-gripper-percent-v1"
    assert config.endpoint == "tcp/127.0.0.1:28081"
    assert config.refill_seconds == 0.4
    assert config.action_timeout_s == 3.0


def test_client_command_omits_empty_task() -> None:
    request = _request(task="   ")
    cmd = _build_client_command(
        request,
        follower_id="follower_a",
        local_port=28081,
        remote_policy_path="/remote/checkpoints/000050/pretrained_model",
    )
    assert not any(part.startswith("--task=") for part in cmd)


def test_remote_request_requires_explicit_identity_and_dimensions() -> None:
    with pytest.raises(ValueError, match="remote_deployment and remote_semantics"):
        _request(remote_deployment="", remote_semantics="")
    with pytest.raises(ValueError, match="remote_state_dim/remote_action_dim"):
        _request(remote_state_dim=None, remote_action_dim=None)
    with pytest.raises(ValueError, match="chunk_size_threshold"):
        _request(actions_per_chunk=25)


def test_server_config_is_accepted_by_the_real_070_server_config(tmp_path) -> None:
    request = _request()
    spec = _deployment_spec(request, "/remote/checkpoints/000050/pretrained_model")
    config_path = spec.write_config(tmp_path / "server.yaml")
    assert config_path.read_text().strip().startswith("{")
    config = draccus.parse(config_class=ServerConfig, args=[f"--config_path={config_path}"])
    assert config.deployment == "so101_red"
    assert config.semantics == request.remote_semantics
    assert config.model.repo_or_path.endswith("pretrained_model")
    assert config.zenoh.listen_endpoints == ["tcp/127.0.0.1:28081"]
    assert config.features[0].shape == (6,)
    assert config.action_feature.shape == (6,)


def test_start_server_script_uses_only_the_070_entry_point() -> None:
    assert "lerobot.scripts.lerobot_policy_server" in _START_SERVER_SCRIPT
    assert "--config_path=" in _START_SERVER_SCRIPT
    assert "server.yaml" in _START_SERVER_SCRIPT
    assert not [token for token in LEGACY_TOKENS if token in _START_SERVER_SCRIPT]


def test_stale_wrong_or_nonfinite_actions_are_not_dispatchable() -> None:
    import numpy as np

    assert action_is_dispatchable(np.zeros(6, dtype=np.float32), width=6, capture_age_s=0.05, ttl_s=0.1)
    assert not action_is_dispatchable(np.zeros(5, dtype=np.float32), width=6, capture_age_s=0.05, ttl_s=0.1)
    assert not action_is_dispatchable(np.zeros(6, dtype=np.float32), width=6, capture_age_s=0.15, ttl_s=0.1)
    bad = np.zeros(6, dtype=np.float32)
    bad[2] = float("nan")
    assert not action_is_dispatchable(bad, width=6, capture_age_s=0.01, ttl_s=0.1)


def test_remote_env_carries_mirror_proxy_and_token(monkeypatch) -> None:
    monkeypatch.setenv("LELAB_TRAIN_HF_ENDPOINT", "https://hf-mirror.com")
    monkeypatch.setenv("LELAB_TRAIN_PROXY", "http://127.0.0.1:17890")
    monkeypatch.setattr("huggingface_hub.get_token", lambda: "hf_test_token")

    env = _remote_env(_config())

    assert env["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:17890"
    assert env["HF_TOKEN"] == "hf_test_token"
    assert env["HF_HOME"].endswith("lelab_server/models/huggingface")


def test_stage_policy_parses_the_remote_path() -> None:
    calls: list[list[str]] = []
    scripts: list[str] = []

    def fake_ssh(argv, input_text=None, **kwargs):
        calls.append(argv)
        scripts.append(input_text or "")
        return 0, "some download output\nPOLICY_PATH=/home/u/lelab_server/models/hf/act\n"

    from lelab.utils.policy_refs import parse_policy_ref

    path = ssh_ops.stage_policy_on_server(
        _config(),
        parse_policy_ref("l-e-n/act_so101@root"),
        python="/home/u/lelab_server/app/.venv/bin/python",
        ssh=fake_ssh,
    )

    assert path == "/home/u/lelab_server/models/hf/act"
    assert " - " in calls[0][-1]
    assert '{"kind": "hub_root", "repo_id": "l-e-n/act_so101", "step_dir": null}' in calls[0][-1]
    assert "snapshot_download" in scripts[0]


def test_stage_policy_reports_failure() -> None:
    def fake_ssh(argv, input_text=None, **kwargs):
        return 1, "ModuleNotFoundError: No module named 'huggingface_hub'"

    from lelab.utils.policy_refs import parse_policy_ref

    with pytest.raises(RuntimeError, match="Could not stage policy"):
        ssh_ops.stage_policy_on_server(_config(), parse_policy_ref("/missing/dir"), ssh=fake_ssh)
