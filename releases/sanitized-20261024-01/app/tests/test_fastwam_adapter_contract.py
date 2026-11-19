"""FW-APP-01: the real L5 FastWAM adapter passes the 12-dim contract gate.

The `policy_contract` in `lelab.rollout.InferenceRequest` exists so a 6-dim
single-arm checkpoint cannot silently drive two arms. This test feeds it the
values that the **actual** L5 checkpoint declares (`config.json` +
`policy_preprocessor.json`, both read from the artefact, see
`ADAPTER_CONTRACT` in the accompanying evidence), then checks that:

  * the real contract is accepted;
  * a 6-dim contract, a camera-set mismatch and a wrong action semantics are
    rejected with a message an operator can act on;
  * the *local* bimanual route still short-circuits with 400 before any device
    object is created — `SO101Follower.__init__` is patched to explode, so a
    device access anywhere in that path fails the test instead of opening a
    serial port.

Model loading itself (base + adapter + saved processors -> `[1,32,12]`) is a GPU
step and is recorded separately in `app-load/`; this file is CPU-only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

CONTRACT = {
    "state_dim": 12,
    "action_dim": 12,
    "camera_keys": [
        "observation.images.central",
        "observation.images.left_wrist",
        "observation.images.right_wrist",
    ],
    "normalization_mapping": {"VISUAL": "IDENTITY", "STATE": "MEAN_STD", "ACTION": "MEAN_STD"},
    "processor": "fastwam-v1",
    "action_semantics": "sent_target_after_local_safety_clamps",
}
CAMERAS = ["central", "left_wrist", "right_wrist"]


def test_real_l5_contract_is_accepted():
    from lelab.remote_inference import validate_bimanual_policy_contract

    assert validate_bimanual_policy_contract(CONTRACT, CAMERAS) == CONTRACT


def test_contract_matches_the_checkpoint_that_was_trained():
    """Guard against the contract drifting away from the artefact on disk.

    Skipped when the checkpoint is not mounted (the CI/CPU host); the GPU host
    runs it for real.
    """
    checkpoint = Path("/home/lfy/lelab-rmr/runs/so101-lora/l5/checkpoints/last/pretrained_model")
    if not (checkpoint / "config.json").exists():
        pytest.skip("L5 checkpoint not present on this host")

    cfg = json.loads((checkpoint / "config.json").read_text())
    pre = json.loads((checkpoint / "policy_preprocessor.json").read_text())
    assert cfg["action_dim"] == CONTRACT["action_dim"]
    assert cfg["proprio_dim"] == CONTRACT["state_dim"]
    assert cfg["normalization_mapping"] == CONTRACT["normalization_mapping"]
    assert sorted(k for k in cfg["input_features"] if k.startswith("observation.images.")) == \
        sorted(CONTRACT["camera_keys"])
    step_names = [s.get("registry_name") for s in pre["steps"]]
    assert "normalizer_processor" in step_names


def test_six_dim_single_arm_contract_is_rejected():
    from lelab.remote_inference import RemoteInferenceError, validate_bimanual_policy_contract

    bad = dict(CONTRACT, state_dim=6, action_dim=6)
    with pytest.raises(RemoteInferenceError, match="state_dim=12"):
        validate_bimanual_policy_contract(bad, CAMERAS)


def test_missing_contract_is_rejected():
    from lelab.remote_inference import RemoteInferenceError, validate_bimanual_policy_contract

    with pytest.raises(RemoteInferenceError, match="explicit policy contract"):
        validate_bimanual_policy_contract(None, CAMERAS)


def test_camera_set_mismatch_is_rejected():
    from lelab.remote_inference import RemoteInferenceError, validate_bimanual_policy_contract

    with pytest.raises(RemoteInferenceError, match="camera keys"):
        validate_bimanual_policy_contract(CONTRACT, ["central", "left_wrist"])


def test_wrong_action_semantics_is_rejected():
    from lelab.remote_inference import RemoteInferenceError, validate_bimanual_policy_contract

    bad = dict(CONTRACT, action_semantics="raw_joint_positions")
    with pytest.raises(RemoteInferenceError, match="action semantics"):
        validate_bimanual_policy_contract(bad, CAMERAS)


def test_missing_processor_is_rejected():
    from lelab.remote_inference import RemoteInferenceError, validate_bimanual_policy_contract

    bad = dict(CONTRACT, processor=None)
    with pytest.raises(RemoteInferenceError, match="processor"):
        validate_bimanual_policy_contract(bad, CAMERAS)


def test_local_bimanual_is_rejected_before_any_device_is_touched(monkeypatch):
    """The 400 must be produced without constructing a follower.

    `SO101Follower.__init__` is replaced with a tripwire: if the local path ever
    reaches device construction, this test fails with that exception instead of
    opening a serial port.
    """
    import lerobot.robots.so_follower.so_follower as so_follower

    def _explode(*args, **kwargs):  # pragma: no cover - only on failure
        raise AssertionError("local bimanual path constructed a SO101Follower")

    monkeypatch.setattr(so_follower.SO101Follower, "__init__", _explode, raising=False)

    from lelab.rollout import InferenceRequest, handle_start_inference

    request = InferenceRequest(
        policy_ref="lelab://local/model",
        cameras={name: {"index_or_path": "/dev/null", "width": 640, "height": 480, "fps": 30}
                 for name in CAMERAS},
        # Four distinct ports and distinct calibration names, as the rig schema
        # requires; the paths are never opened because the local bimanual branch
        # returns before any device work.
        bimanual={
            "left": {"leader_port": "/dev/ttyUSB0", "leader_config": "left_leader",
                     "follower_port": "/dev/ttyUSB1", "follower_config": "left_follower"},
            "right": {"leader_port": "/dev/ttyUSB2", "leader_config": "right_leader",
                      "follower_port": "/dev/ttyUSB3", "follower_config": "right_follower"},
        },
        policy_contract=CONTRACT,
    )
    result = handle_start_inference(request)
    assert result["success"] is False
    assert result["status_code"] == 400
    assert "Local bimanual" in result["message"]
