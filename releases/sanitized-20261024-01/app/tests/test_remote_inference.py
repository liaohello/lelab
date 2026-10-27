"""Cloud inference: command building + ref staging (all remote calls faked)."""

from __future__ import annotations

from lelab.remote_inference import _build_client_command, _remote_env
from lelab.remote_sync import TrainServerConfig
from lelab.rollout import InferenceRequest
from lelab.runners import ssh_ops


def _config() -> TrainServerConfig:
    return TrainServerConfig.from_env(
        {
            "LELAB_TRAIN_HOST": "192.0.2.10",
            "LELAB_TRAIN_PORT": "22",
            "LELAB_TRAIN_USER": "trainer",
            "LELAB_TRAIN_ROOT": "~/Desktop/lelab_server",
        }
    )


def test_client_command_targets_the_tunnel(monkeypatch) -> None:
    monkeypatch.setenv("LELAB_MAX_RELATIVE_TARGET", "5")
    monkeypatch.setenv("LELAB_MAX_POSITION_DEVIATION", "off")
    request = InferenceRequest(
        follower_port="/dev/serial/by-id/usb-follower",
        follower_config="follower_a",
        policy_ref="/remote/checkpoints/000050/pretrained_model",
        task="pick the red block",
        cameras={"wrist": {"type": "opencv", "camera_index": 0, "width": 640, "height": 480, "fps": 30}},
        duration_s=30,
        target="remote",
        policy_type="act",
        actions_per_chunk=25,
    )

    cmd = _build_client_command(
        request,
        follower_id="follower_a",
        local_port=28081,
        remote_policy_path="/home/trainer/Desktop/lelab_server/models/pretrained/act_so101",
    )
    joined = " ".join(cmd)

    assert "lerobot.async_inference.robot_client" in joined
    assert "--server_address=127.0.0.1:28081" in joined
    assert "--robot.port=/dev/serial/by-id/usb-follower" in joined
    assert "--robot.id=follower_a" in joined
    assert "--policy_type=act" in joined
    assert "--actions_per_chunk=25" in joined
    assert "--task=pick the red block" in joined
    # Local safety limits travel with the robot config, cloud or not.
    assert "--robot.max_relative_target=5.0" in joined
    assert "--robot.max_position_deviation" not in joined
    assert "wrist: {type: opencv, index_or_path: 0, width: 640, height: 480, fps: 30}" in joined


def test_client_command_omits_empty_task() -> None:
    request = InferenceRequest(
        follower_port="/dev/ttyACM0",
        follower_config="follower_a",
        policy_ref="/remote/checkpoints/000001/pretrained_model",
        duration_s=10,
        target="remote",
    )
    cmd = _build_client_command(
        request,
        follower_id="follower_a",
        local_port=28081,
        remote_policy_path="/remote/checkpoints/000001/pretrained_model",
    )
    assert not any(part.startswith("--task=") for part in cmd)


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
    # The interpreter is invoked with `-` so the script travels on stdin.
    assert " - " in calls[0][-1]
    assert '{"kind": "hub_root", "repo_id": "l-e-n/act_so101", "step_dir": null}' in calls[0][-1]
    assert "snapshot_download" in scripts[0]


def test_stage_policy_reports_failure() -> None:
    def fake_ssh(argv, input_text=None, **kwargs):
        return 1, "ModuleNotFoundError: No module named 'huggingface_hub'"

    from lelab.utils.policy_refs import parse_policy_ref

    try:
        ssh_ops.stage_policy_on_server(_config(), parse_policy_ref("/missing/dir"), ssh=fake_ssh)
    except RuntimeError as exc:
        assert "Could not stage policy" in str(exc)
    else:  # pragma: no cover - the call must fail
        raise AssertionError("stage_policy_on_server should have raised")
