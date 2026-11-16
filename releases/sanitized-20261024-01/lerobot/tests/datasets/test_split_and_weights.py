#!/usr/bin/env python

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Dataset split + per-episode weight contracts (TC-DAT-02, TC-DAT-01, TC-CKP-02).

These tests drive the *real* config object and the *real* dataset factory on a
temporary on-disk dataset, so they fail if the config field is accepted but
never reaches the sampler, or if the sampler but not the loader is wired.

LeLab 说明：本模块用真实 DatasetConfig / make_train_eval_datasets / 真实落盘
数据集，验证"配置→权重文件→loader→sampler"与显式验证集切分，而不是只测
sampler 的单元行为。
"""

import json
from pathlib import Path

import pytest
import torch

pytest.importorskip("datasets", reason="datasets is required (install lerobot[dataset])")

from lerobot.configs.default import DatasetConfig
from lerobot.configs.train import TrainPipelineConfig
from lerobot.datasets.factory import load_episode_weights, make_train_eval_datasets
from lerobot.datasets.lerobot_dataset import LeRobotDataset

pytestmark = pytest.mark.dataset

FEATURES = {
    "observation.state": {"dtype": "float32", "shape": (2,), "names": None},
    "action": {"dtype": "float32", "shape": (2,), "names": None},
}
FPS = 10


def _write_dataset(root: Path, repo_id: str, episode_lengths: list[int], tasks: list[str] | None = None):
    """Create a real on-disk dataset with one task per episode (or a single shared task)."""
    dataset_root = root / repo_id
    ds = LeRobotDataset.create(
        repo_id=repo_id, fps=FPS, features=FEATURES, root=dataset_root, use_videos=False
    )
    for ep, length in enumerate(episode_lengths):
        task = tasks[ep] if tasks is not None else "shared task"
        for _ in range(length):
            ds.add_frame(
                {
                    "observation.state": torch.zeros(2),
                    "action": torch.zeros(2),
                    "task": task,
                }
            )
        ds.save_episode()
    ds.finalize()
    return dataset_root


def _weights_file(tmp_path: Path, name: str, payload) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _pipeline_cfg(dataset: DatasetConfig, **overrides) -> TrainPipelineConfig:
    # `validate()` rejects a pipeline without a policy/reward model before it
    # reaches the dataset guards, so give it a real (uninstantiated) ACT config.
    # Importing the module registers "act" with PreTrainedConfig. push_to_hub is
    # disabled so validate() reaches the dataset guards instead of the hub check.
    from lerobot.policies.act.configuration_act import ACTConfig

    return TrainPipelineConfig(dataset=dataset, policy=ACTConfig(push_to_hub=False), **overrides)


# ── config-level rejection (TC-DAT-02: invalid values fail loudly) ────────────


def test_eval_episodes_and_eval_split_are_mutually_exclusive():
    with pytest.raises(ValueError, match="either eval_split"):
        DatasetConfig(repo_id="u/r", eval_split=0.1, eval_episodes=[0])


def test_eval_episodes_rejects_empty_negative_and_duplicate():
    with pytest.raises(ValueError, match="must not be empty"):
        DatasetConfig(repo_id="u/r", eval_episodes=[])
    with pytest.raises(ValueError, match="non-negative"):
        DatasetConfig(repo_id="u/r", eval_episodes=[-1])
    with pytest.raises(ValueError, match="duplicates"):
        DatasetConfig(repo_id="u/r", eval_episodes=[1, 1])


def test_eval_episodes_rejects_overlap_with_training_episodes():
    with pytest.raises(ValueError, match="overlap"):
        DatasetConfig(repo_id="u/r", episodes=[0, 1, 2], eval_episodes=[2])


def test_eval_and_weights_are_refused_with_streaming_rather_than_ignored():
    with pytest.raises(ValueError, match="streaming"):
        DatasetConfig(repo_id="u/r", streaming=True, eval_episodes=[0])
    with pytest.raises(ValueError, match="streaming"):
        DatasetConfig(repo_id="u/r", streaming=True, sample_weights_file="w.json")


def test_eval_steps_requires_a_held_out_split():
    """eval_steps must be backed by eval_split *or* explicit eval_episodes.

    The guard lives in ``TrainPipelineConfig.validate()`` (run by the CLI before the
    job starts), not in ``__post_init__``, so this exercises the same entry point.
    """
    no_split = _pipeline_cfg(DatasetConfig(repo_id="u/r"), eval_steps=10)
    with pytest.raises(ValueError, match="eval_split > 0.0 or dataset.eval_episodes"):
        no_split.validate()
    # Explicit episodes satisfy the guard: the config with a held-out split is valid.
    _pipeline_cfg(DatasetConfig(repo_id="u/r", eval_episodes=[0]), eval_steps=10).validate()


# ── weight file loading (TC-DAT-02) ──────────────────────────────────────────


def test_load_episode_weights_reports_missing_short_and_non_finite(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(FileNotFoundError, match="sample_weights_file not found"):
        load_episode_weights(str(missing))

    assert load_episode_weights(None) is None

    empty = _weights_file(tmp_path, "empty.json", {"episode_weights": []})
    with pytest.raises(ValueError, match="non-empty"):
        load_episode_weights(str(empty))

    non_numeric = _weights_file(tmp_path, "bad.json", {"episode_weights": ["a"]})
    with pytest.raises(ValueError, match="non-numeric"):
        load_episode_weights(str(non_numeric))

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_episode_weights(str(broken))


def test_load_episode_weights_length_must_match_the_episode_axis(tmp_path):
    # 3 weights, 4 episodes, 2 training episodes: neither the full axis nor the
    # training episodes, so it is genuinely ambiguous and must be refused.
    path = _weights_file(tmp_path, "w.json", {"episode_weights": [1.0, 1.0, 1.0]})
    with pytest.raises(ValueError, match="holds 3 weights but the dataset has 4 episodes"):
        load_episode_weights(str(path), episode_indices=[1, 2], total_episodes=4)


def test_load_episode_weights_projects_a_subset_back_onto_the_full_axis(tmp_path):
    """Weights for the *training* episodes must land on their own episode ids."""
    path = _weights_file(tmp_path, "w.json", {"episode_weights": [5.0, 7.0]})
    projected = load_episode_weights(str(path), episode_indices=[1, 3], total_episodes=4)
    assert projected == [0.0, 5.0, 0.0, 7.0]

    # Full-axis weights pass through untouched.
    full = _weights_file(tmp_path, "full.json", {"episode_weights": [1, 2, 3, 4]})
    assert load_episode_weights(str(full), episode_indices=[0, 1], total_episodes=4) == [1, 2, 3, 4]

    # A bare list is accepted too.
    bare = _weights_file(tmp_path, "bare.json", [1.5, 2.5])
    assert load_episode_weights(str(bare)) == [1.5, 2.5]


# ── the real factory split (TC-DAT-02: no leakage) ──────────────────────────


def test_explicit_eval_episodes_split_covers_disjoint_episode_sets(tmp_path):
    root = _write_dataset(tmp_path, "local/split", [4, 4, 4, 4])
    cfg = _pipeline_cfg(DatasetConfig(repo_id="local/split", root=str(root), eval_episodes=[0, 1]))

    train, eval_ = make_train_eval_datasets(cfg)

    assert train.episodes == [2, 3]
    assert eval_.episodes == [0, 1]
    assert set(train.episodes).isdisjoint(eval_.episodes)
    # Frame counts follow the episode selection, so validation frames cannot leak in.
    assert train.num_frames == 8
    assert eval_.num_frames == 8


def test_explicit_eval_episodes_rejects_ids_outside_the_loaded_dataset(tmp_path):
    root = _write_dataset(tmp_path, "local/oob", [3, 3])
    cfg = _pipeline_cfg(DatasetConfig(repo_id="local/oob", root=str(root), eval_episodes=[0, 9]))
    with pytest.raises(ValueError, match="are not part of the loaded dataset episodes"):
        make_train_eval_datasets(cfg)


def test_eval_split_fraction_still_holds_out_per_task(tmp_path):
    root = _write_dataset(tmp_path, "local/frac", [3, 3, 3, 3], tasks=["a", "a", "b", "b"])
    cfg = _pipeline_cfg(DatasetConfig(repo_id="local/frac", root=str(root), eval_split=0.5))
    train, eval_ = make_train_eval_datasets(cfg)
    assert set(train.episodes).isdisjoint(eval_.episodes)
    assert sorted(eval_.episodes) == [1, 3]
    assert sorted(train.episodes) == [0, 2]


def test_no_split_returns_the_full_dataset_and_no_eval(tmp_path):
    root = _write_dataset(tmp_path, "local/none", [2, 2])
    cfg = _pipeline_cfg(DatasetConfig(repo_id="local/none", root=str(root)))
    train, eval_ = make_train_eval_datasets(cfg)
    assert eval_ is None
    assert train.num_frames == 4


# ── config → file → loader → sampler (TC-DAT-02 / TC-DAT-01) ────────────────


def test_weights_reach_the_training_sampler_through_the_real_loader(tmp_path):
    """The weight file configured on DatasetConfig must shape the real DataLoader."""
    from lerobot.scripts.lerobot_train import make_dataloaders
    from lerobot.distributed import ParallelDims

    # Two 10-frame episodes; weight the second one 5x heavier.
    root = _write_dataset(tmp_path, "local/weighted", [10, 10])
    weights = _weights_file(tmp_path, "w.json", {"episode_weights": [1.0, 5.0]})
    cfg = _pipeline_cfg(
        DatasetConfig(repo_id="local/weighted", root=str(root), sample_weights_file=str(weights)),
        batch_size=8,
        num_workers=0,
        seed=0,
    )
    dataset, eval_dataset = make_train_eval_datasets(cfg)
    assert eval_dataset is None

    loader, _ = make_dataloaders(cfg, dataset, eval_dataset, step=0, parallel_dims=ParallelDims(dp_replicate=1, dp_shard=1, ring=1, ulysses=1, world_size=1, device_type="cpu"))

    sampler = loader.sampler
    probs = sampler._frame_probabilities
    assert probs is not None, "sample_weights_file did not reach EpisodeAwareSampler"
    # Per-episode totals: 10 frames x 1 vs 10 frames x 5 -> 1/6 vs 5/6.
    assert probs[:10].sum() == pytest.approx(1 / 6)
    assert probs[10:].sum() == pytest.approx(5 / 6)
    # The batches really come from the weighted distribution.
    episode_of = torch.as_tensor(dataset.hf_dataset["episode_index"])
    drawn = [int(episode_of[i]) for batch in loader for i in batch["index"].tolist()]
    assert drawn.count(1) > drawn.count(0)


def test_weights_are_absent_from_the_sampler_without_the_option(tmp_path):
    from lerobot.scripts.lerobot_train import make_dataloaders
    from lerobot.distributed import ParallelDims

    root = _write_dataset(tmp_path, "local/plain", [6, 6])
    cfg = _pipeline_cfg(
        DatasetConfig(repo_id="local/plain", root=str(root)), batch_size=4, num_workers=0, seed=0
    )
    dataset, _ = make_train_eval_datasets(cfg)
    loader, _ = make_dataloaders(cfg, dataset, None, step=0, parallel_dims=ParallelDims(dp_replicate=1, dp_shard=1, ring=1, ulysses=1, world_size=1, device_type="cpu"))
    assert loader.sampler._frame_probabilities is None
    # Unweighted path keeps the plain permutation semantics: one pass, no duplication.
    indices = [i for batch in loader for i in batch["index"]]
    assert len(indices) == dataset.num_frames
    assert len(set(indices)) == len(indices)


def test_weights_follow_the_training_subset_when_eval_episodes_are_explicit(tmp_path):
    """For a filtered training set the weight file must be projected onto the full axis."""
    from lerobot.scripts.lerobot_train import make_dataloaders
    from lerobot.distributed import ParallelDims

    root = _write_dataset(tmp_path, "local/subset", [8, 8, 8, 8])
    # Weights for the *training* episodes only (2 and 3).
    weights = _weights_file(tmp_path, "w.json", {"episode_weights": [1.0, 4.0]})
    cfg = _pipeline_cfg(
        DatasetConfig(
            repo_id="local/subset",
            root=str(root),
            eval_episodes=[0, 1],
            sample_weights_file=str(weights),
        ),
        batch_size=8,
        num_workers=0,
        seed=0,
    )
    train, eval_ = make_train_eval_datasets(cfg)
    assert train.episodes == [2, 3]

    loader, _ = make_dataloaders(cfg, train, eval_, step=0, parallel_dims=ParallelDims(dp_replicate=1, dp_shard=1, ring=1, ulysses=1, world_size=1, device_type="cpu"))
    probs = loader.sampler._frame_probabilities
    assert probs is not None
    assert len(probs) == train.num_frames, "probability vector must cover only sampled (training) frames"
    total = probs.sum()
    # Episode 3 is weighted 4x episode 2, and held-out episodes contribute nothing.
    assert probs[:8].sum() / total == pytest.approx(1 / 5)
    assert probs[8:].sum() / total == pytest.approx(4 / 5)


def test_weighted_sampler_resume_is_the_exact_suffix(tmp_path):
    """Mid-epoch resume with weights must replay the tail of the original order."""
    from lerobot.datasets import EpisodeAwareSampler, compute_sampler_state

    root = _write_dataset(tmp_path, "local/resume", [5, 5])
    weights = _weights_file(tmp_path, "w.json", {"episode_weights": [1.0, 3.0]})
    ds = LeRobotDataset(repo_id="local/resume", root=str(root))

    def _sampler():
        return EpisodeAwareSampler(
            ds.meta.episodes["dataset_from_index"],
            ds.meta.episodes["dataset_to_index"],
            episode_indices_to_use=ds.episodes,
            shuffle=True,
            seed=7,
            absolute_to_relative_idx=ds.absolute_to_relative_idx,
            episode_weights=load_episode_weights(str(weights)),
        )

    full = list(_sampler())
    cut = compute_sampler_state(1, len(full), batch_size=4, num_processes=1)

    resumed_sampler = _sampler()
    resumed_sampler.load_state_dict(cut)
    resumed = list(resumed_sampler)

    assert resumed == full[cut["start_index"] :]
    assert cut["start_index"] == 4
