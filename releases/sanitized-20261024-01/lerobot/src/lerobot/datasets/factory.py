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
import json
import logging
import math
from pathlib import Path
from pprint import pformat
from typing import Literal, cast

import torch

from lerobot.configs import PreTrainedConfig
from lerobot.configs.rewards import RewardModelConfig
from lerobot.configs.train import TrainPipelineConfig
from lerobot.transforms import ImageTransforms
from lerobot.utils.constants import ACTION, IMAGENET_STATS, OBS_IMAGE, OBS_PREFIX, OBS_STATE, REWARD

from .dataset_metadata import LeRobotDatasetMetadata
from .lerobot_dataset import LeRobotDataset
from .multi_dataset import MultiLeRobotDataset
from .storage import DEFAULT_STORAGE_FORMAT, is_bucket_root, load_dataset_metadata
from .streaming_dataset import DEFAULT_STREAMING_SEED, StreamingLeRobotDataset
from .utils import resolve_episode_indices

logger = logging.getLogger(__name__)


def resolve_delta_timestamps(
    cfg: PreTrainedConfig | RewardModelConfig,
    ds_meta: LeRobotDatasetMetadata,
    rename_map: dict[str, str] | None = None,
) -> dict[str, list[float]] | None:
    """Resolves delta_timestamps by reading from the 'delta_indices' properties of the config.

    Args:
        cfg (PreTrainedConfig | RewardModelConfig): The config to read delta_indices from. Both
            ``PreTrainedConfig`` and concrete ``RewardModelConfig`` subclasses expose the
            ``{observation,action,reward}_delta_indices`` properties used below.
        ds_meta (LeRobotDatasetMetadata): The dataset from which features and fps are used to build
            delta_timestamps against.

    Returns:
        dict[str, list[float]] | None: A dictionary of delta_timestamps, e.g.:
            {
                "observation.state": [-0.04, -0.02, 0]
                "observation.action": [-0.02, 0, 0.02]
            }
            returns `None` if the resulting dict is empty.
    """
    # Only policies that opt into modality-specific history (currently Pi05 with MEM)
    # define these; everything else falls back to the shared observation indices.
    explicit_image_indices = getattr(cfg, "image_observation_delta_indices", None)
    image_indices = (
        explicit_image_indices if explicit_image_indices is not None else cfg.observation_delta_indices
    )
    explicit_state_indices = getattr(cfg, "state_observation_delta_indices", None)
    state_indices = (
        explicit_state_indices if explicit_state_indices is not None else cfg.observation_delta_indices
    )

    delta_timestamps: dict[str, list[float]] = {}
    matched_image_keys: list[str] = []
    for key in ds_meta.features:
        policy_key = (rename_map or {}).get(key, key)
        if policy_key == REWARD and cfg.reward_delta_indices is not None:
            delta_timestamps[key] = [i / ds_meta.fps for i in cfg.reward_delta_indices]
        if policy_key == ACTION and cfg.action_delta_indices is not None:
            delta_timestamps[key] = [i / ds_meta.fps for i in cfg.action_delta_indices]
        # `OBS_IMAGE` matches both the `observation.image` and `observation.images.<cam>`
        # conventions; matching `OBS_IMAGES` alone would silently give singular-key
        # datasets no image history at all.
        if policy_key.startswith(OBS_IMAGE):
            indices = image_indices
            matched_image_keys.append(key)
        elif policy_key == OBS_STATE:
            indices = state_indices
        else:
            indices = cfg.observation_delta_indices if policy_key.startswith(OBS_PREFIX) else None
        if indices is not None:
            delta_timestamps[key] = [i / ds_meta.fps for i in indices]

    # A policy asking for an image history that no dataset key can supply would train
    # on single frames without any error, so fail instead of degrading silently.
    if explicit_image_indices is not None and len(explicit_image_indices) > 1 and not matched_image_keys:
        raise ValueError(
            f"{type(cfg).__name__} requests {len(explicit_image_indices)} history frames per camera, but no "
            f"dataset feature maps to an image key. Dataset features: {sorted(ds_meta.features)}. "
            "Image keys must be named `observation.image*` after applying `--rename_map`."
        )

    if len(delta_timestamps) == 0:
        return None

    return delta_timestamps


def make_dataset(cfg: TrainPipelineConfig) -> LeRobotDataset | StreamingLeRobotDataset:
    """Handles the logic of setting up delta timestamps and image transforms before creating a dataset.

    Args:
        cfg (TrainPipelineConfig): A TrainPipelineConfig config which contains a DatasetConfig and a PreTrainedConfig.

    Raises:
        NotImplementedError: The MultiLeRobotDataset is currently deactivated.

    Returns:
        LeRobotDataset | StreamingLeRobotDataset: a StreamingLeRobotDataset when `cfg.dataset.streaming` is set.
    """
    # On the "gpu" backend the workers only decode; lerobot-train augments each batch on the policy device.
    image_transforms_cfg = cfg.dataset.image_transforms
    image_transforms = (
        ImageTransforms(image_transforms_cfg)
        if image_transforms_cfg.enable and image_transforms_cfg.backend == "dataloader"
        else None
    )

    if isinstance(cfg.dataset.repo_id, str):
        root, repo_type_name = cfg.dataset.root, cfg.dataset.repo_type
        if cfg.dataset.streaming and is_bucket_root(cfg.dataset.repo_id, root):
            # Streaming reads default-format buckets directly, so a bucket URI root selects
            # bucket mode instead of the object-store localization used by other formats.
            root, repo_type_name = None, "bucket"
        repo_type = cast(Literal["dataset", "bucket"], repo_type_name)
        # Storage-aware loader: same as LeRobotDatasetMetadata(...), plus support
        # for datasets whose root is an object-store URI (e.g. ``hf://``).
        ds_meta = load_dataset_metadata(
            cfg.dataset.repo_id,
            root=root,
            revision=cfg.dataset.revision,
            repo_type=repo_type,
        )
        delta_timestamps = resolve_delta_timestamps(cfg.trainable_config, ds_meta, cfg.rename_map)
        episodes = resolve_episode_indices(
            cfg.dataset.episodes, ds_meta.total_episodes, cfg.dataset.exclude_episodes
        )
        if cfg.dataset.streaming and ds_meta.storage_format != DEFAULT_STORAGE_FORMAT:
            raise ValueError(
                f"dataset.streaming=True is not supported for storage_format="
                f"{ds_meta.storage_format!r}: StreamingLeRobotDataset only reads the default "
                f"{DEFAULT_STORAGE_FORMAT!r} layout. Note that some formats (e.g. 'lance') "
                "support remote map-style access without streaming mode."
            )
        if not cfg.dataset.streaming:
            if repo_type == "bucket" and ds_meta.storage_format == DEFAULT_STORAGE_FORMAT:
                raise ValueError(
                    f"repo_type='bucket' is streaming-only for the default {DEFAULT_STORAGE_FORMAT!r} "
                    "storage format: set dataset.streaming=true to train from an HF Storage Bucket."
                )
            dataset = LeRobotDataset(
                cfg.dataset.repo_id,
                root=cfg.dataset.root,
                episodes=episodes,
                delta_timestamps=delta_timestamps,
                image_transforms=image_transforms,
                revision=cfg.dataset.revision,
                video_backend=cfg.dataset.video_backend,
                return_uint8=True,
                depth_output_unit=cfg.dataset.depth_output_unit,
                tolerance_s=cfg.tolerance_s,
                repo_type=repo_type,
                video_decoder_cache_size=cfg.dataset.video_decoder_cache_size,
            )
        else:
            dataset = StreamingLeRobotDataset(
                cfg.dataset.repo_id,
                root=root,
                episodes=episodes,
                delta_timestamps=delta_timestamps,
                image_transforms=image_transforms,
                revision=cfg.dataset.revision,
                seed=cfg.seed if cfg.seed is not None else DEFAULT_STREAMING_SEED,
                max_num_shards=max(1, cfg.num_workers),
                tolerance_s=cfg.tolerance_s,
                return_uint8=True,
                depth_output_unit=cfg.dataset.depth_output_unit,
                video_backend=cfg.dataset.video_backend,
                episode_pool_size=cfg.dataset.streaming_episode_pool_size,
                sampling_strategy=cfg.dataset.streaming_sampling_strategy,
                prefetch_episodes=cfg.dataset.streaming_prefetch_episodes,
                byte_budget_gb=cfg.dataset.streaming_byte_budget_gb,
                decode_threads=cfg.dataset.streaming_decode_threads,
                decoded_queue_size=cfg.dataset.streaming_decoded_queue_size,
                video_decoder_cache_size=cfg.dataset.video_decoder_cache_size,
                native_http_connections=cfg.dataset.streaming_native_http_connections,
                native_http_subranges=cfg.dataset.streaming_native_http_subranges,
                sidecar_lock_timeout_s=cfg.dataset.streaming_sidecar_lock_timeout_s,
                repeat=True,
                repo_type=repo_type,
            )
    else:
        raise NotImplementedError("The MultiLeRobotDataset isn't supported for now.")
        dataset = MultiLeRobotDataset(
            cfg.dataset.repo_id,
            # TODO(aliberts): add proper support for multi dataset
            # delta_timestamps=delta_timestamps,
            image_transforms=image_transforms,
            video_backend=cfg.dataset.video_backend,
        )
        logger.info(
            "Multiple datasets were provided. Applied the following index mapping to the provided datasets: "
            f"{pformat(dataset.repo_id_to_index, indent=2)}"
        )

    if cfg.dataset.use_imagenet_stats:
        for key in dataset.meta.camera_keys:
            if key in dataset.meta.depth_keys:
                continue  # Exclude depth keys from ImageNet stats
            dataset.meta.stats.setdefault(key, {})
            for stats_type, stats in IMAGENET_STATS.items():
                dataset.meta.stats[key][stats_type] = torch.tensor(stats, dtype=torch.float32)

    return dataset


def load_episode_weights(
    path: str | Path | None,
    episode_indices: list[int] | None = None,
    total_episodes: int | None = None,
) -> list[float] | None:
    """Read a per-episode sampling-weight file written by LeLab.

    The file is ``{"episode_weights": [...]}`` in dataset episode order (each
    value is the relative weight of every frame of that episode); a bare JSON
    list is accepted too. Relative paths resolve against the working directory,
    which is the job directory for remote runs.

    ``EpisodeAwareSampler`` expects one weight per episode of the *underlying*
    dataset, before any episode filtering. When the training split only uses a
    subset (e.g. explicit ``eval_episodes``), callers therefore pass
    ``episode_indices`` so the weights are projected back onto the full episode
    axis instead of being silently re-indexed onto the filtered subset — that
    would attach Scene1's weight to validation episodes.

    LeLab 说明：读取按 episode 的采样权重文件。sampler 需要"过滤前"的全量
    episode 轴，所以当训练集只是子集时按 episode id 投影回去，而不是把权重
    顺序对齐到子集。
    """
    if not path:
        return None
    weights_path = Path(path).expanduser()
    if not weights_path.is_absolute():
        weights_path = Path.cwd() / weights_path
    if not weights_path.is_file():
        raise FileNotFoundError(f"sample_weights_file not found: {weights_path}")
    try:
        payload = json.loads(weights_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{weights_path} is not valid JSON: {exc}") from exc
    weights = payload.get("episode_weights") if isinstance(payload, dict) else payload
    if not isinstance(weights, list) or not weights:
        raise ValueError(f"{weights_path} must contain a non-empty 'episode_weights' list")
    try:
        values = [float(weight) for weight in weights]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{weights_path} contains a non-numeric episode weight: {exc}") from exc

    if episode_indices is not None:
        axis = (
            total_episodes
            if total_episodes is not None
            else (max(episode_indices) + 1 if episode_indices else 0)
        )
        if len(values) == axis:
            # One weight per dataset episode: keep the full axis so the sampler
            # applies the right weight to the episodes that actually train.
            return values
        if len(values) == len(episode_indices):
            projected = [0.0] * axis
            for episode, weight in zip(episode_indices, values, strict=True):
                if episode >= axis:
                    raise ValueError(
                        f"{weights_path} cannot be projected onto a {axis}-episode axis: "
                        f"training episode {episode} is out of range."
                    )
                projected[episode] = weight
            return projected
        raise ValueError(
            f"{weights_path} holds {len(values)} weights but the dataset has {axis} episodes "
            f"and the training split uses {len(episode_indices)}; provide weights for the full "
            "episode axis (or exactly the training episodes) so they cannot be mis-assigned."
        )
    return values


def make_train_eval_datasets(
    cfg: TrainPipelineConfig,
) -> tuple[LeRobotDataset | StreamingLeRobotDataset, LeRobotDataset | None]:
    """Create train and optional eval datasets by splitting episodes.

    With ``dataset.eval_episodes`` the split is the explicit, auditable episode
    list (every other loaded episode trains; Scene1 uses validation 0..11).
    Otherwise the last ``ceil(n_episodes * eval_split)`` episodes per task are
    held out. If neither is set, returns ``(full_dataset, None)``.
    """
    full_dataset = make_dataset(cfg)

    explicit_eval = cfg.dataset.eval_episodes
    if cfg.dataset.eval_split == 0.0 and not explicit_eval:
        return full_dataset, None

    base_episodes = (
        full_dataset.episodes if full_dataset.episodes is not None else list(range(full_dataset.num_episodes))
    )

    if explicit_eval is not None:
        base_set = set(base_episodes)
        missing = sorted(set(explicit_eval) - base_set)
        if missing:
            raise ValueError(
                f"eval_episodes {missing} are not part of the loaded dataset episodes "
                f"(available: {len(base_episodes)} episodes)."
            )
        eval_set = set(explicit_eval)
        # Preserve dataset episode order for deterministic evaluation.
        train_episodes = [ep for ep in base_episodes if ep not in eval_set]
        eval_episodes = [ep for ep in base_episodes if ep in eval_set]
        split_label = f"eval_episodes={eval_episodes}"
    else:
        episode_tasks = full_dataset.meta.episodes["tasks"]
        task_to_episodes: dict[str, list[int]] = {}
        for ep_idx in base_episodes:
            task_key = episode_tasks[ep_idx][0] if episode_tasks[ep_idx] else ""
            task_to_episodes.setdefault(task_key, []).append(ep_idx)

        train_episodes, eval_episodes = [], []
        for eps in task_to_episodes.values():
            n_eval = math.ceil(len(eps) * cfg.dataset.eval_split)
            train_episodes.extend(eps[: len(eps) - n_eval])
            eval_episodes.extend(eps[len(eps) - n_eval :])
        split_label = f"eval_split={cfg.dataset.eval_split}, {len(task_to_episodes)} tasks"

    if not train_episodes:
        raise ValueError(
            f"The requested validation split leaves 0 training episodes from {len(base_episodes)} total."
        )
    if not eval_episodes:
        raise ValueError("The requested validation split holds out 0 evaluation episodes.")

    logger.info(
        f"Train/eval split: {len(train_episodes)} train, {len(eval_episodes)} eval ({split_label})"
    )

    delta_timestamps = resolve_delta_timestamps(cfg.trainable_config, full_dataset.meta, cfg.rename_map)

    # On the "gpu" backend the workers only decode; lerobot-train augments each batch on the policy device.
    image_transforms_cfg = cfg.dataset.image_transforms
    train_image_transforms = (
        ImageTransforms(image_transforms_cfg)
        if image_transforms_cfg.enable and image_transforms_cfg.backend == "dataloader"
        else None
    )

    train_dataset = LeRobotDataset(
        cfg.dataset.repo_id,
        root=cfg.dataset.root,
        episodes=train_episodes,
        delta_timestamps=delta_timestamps,
        image_transforms=train_image_transforms,
        depth_output_unit=cfg.dataset.depth_output_unit,
        revision=cfg.dataset.revision,
        video_backend=cfg.dataset.video_backend,
        return_uint8=True,
        tolerance_s=cfg.tolerance_s,
        repo_type=cfg.dataset.repo_type,
        video_decoder_cache_size=cfg.dataset.video_decoder_cache_size,
    )

    eval_dataset = LeRobotDataset(
        cfg.dataset.repo_id,
        root=cfg.dataset.root,
        episodes=eval_episodes,
        delta_timestamps=delta_timestamps,
        image_transforms=None,
        depth_output_unit=cfg.dataset.depth_output_unit,
        revision=cfg.dataset.revision,
        video_backend=cfg.dataset.video_backend,
        return_uint8=True,
        tolerance_s=cfg.tolerance_s,
        repo_type=cfg.dataset.repo_type,
        video_decoder_cache_size=cfg.dataset.video_decoder_cache_size,
    )

    if cfg.dataset.use_imagenet_stats:
        for ds in (train_dataset, eval_dataset):
            for key in ds.meta.camera_keys:
                if key in ds.meta.depth_keys:
                    continue
                ds.meta.stats.setdefault(key, {})
                for stats_type, stats in IMAGENET_STATS.items():
                    ds.meta.stats[key][stats_type] = torch.tensor(stats, dtype=torch.float32)

    return train_dataset, eval_dataset
