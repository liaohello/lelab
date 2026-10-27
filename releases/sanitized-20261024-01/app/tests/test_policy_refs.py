"""Policy ref parsing shared by local and cloud inference."""

from __future__ import annotations

import pytest

from lelab.utils.policy_refs import PolicyRefError, parse_policy_ref


def test_absolute_path_is_local() -> None:
    ref = parse_policy_ref("/home/nvidia/models/act/pretrained_model")
    assert ref.kind == "local"
    assert ref.path == "/home/nvidia/models/act/pretrained_model"
    assert ref.allow_patterns is None


def test_home_relative_path_is_local() -> None:
    ref = parse_policy_ref("~/Desktop/lelab_server/models/pretrained/act_so101")
    assert ref.kind == "local"
    assert ref.path == "~/Desktop/lelab_server/models/pretrained/act_so101"


def test_checkpoint_tree_ref() -> None:
    ref = parse_policy_ref("user/act_so101@checkpoints/000050")
    assert ref.kind == "hub_tree"
    assert ref.repo_id == "user/act_so101"
    assert ref.step_dir == "000050"
    assert ref.allow_patterns == ["checkpoints/000050/pretrained_model/*"]
    assert ref.local_subpath() == "checkpoints/000050/pretrained_model"


def test_repo_root_ref() -> None:
    ref = parse_policy_ref("lerobot/act_aloha_sim_transfer_cube_human@root")
    assert ref.kind == "hub_root"
    assert ref.repo_id == "lerobot/act_aloha_sim_transfer_cube_human"
    assert ref.allow_patterns is None


@pytest.mark.parametrize("bad", ["", "   ", "not-a-ref", "user/repo@checkpoints/abc"])
def test_unparsable_refs_raise(bad: str) -> None:
    with pytest.raises(PolicyRefError):
        parse_policy_ref(bad)
