# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
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

"""Training-specific helpers: the request schema and the LeRobot CLI builder.

The actual job lifecycle (subprocess management, registry, log streaming)
lives in app/jobs.py.

中文说明：本文件是 LeLab 的“训练请求 → 训练命令”转换层。前端提交的训练表单
先被反序列化为 TrainingRequest（Pydantic 模型），再由 build_training_command()
拼接成 `python -m lerobot.scripts.lerobot_train ...` 参数列表。LeLab 只负责
构造训练命令并管理任务生命周期（见 jobs.py）；真正的模型训练由 LeRobot 的
lerobot_train.py 完成。
"""

import importlib
import json
import logging
import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, model_validator

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from lelab.jobs import JobTarget

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _resolve_video_backend(requested: str | None) -> str:
    """Return the video backend to pass to LeRobot.

    If the caller explicitly chose a backend, honour it.  Otherwise, probe
    whether ``torchcodec`` can actually load at runtime (it may be installed
    but broken on Windows when FFmpeg shared DLLs are missing) and fall back
    to ``pyav`` when it can't.

    中文说明：视频解码后端解析。用户显式指定则照用；否则探测 torchcodec 能否
    真正 import（Windows 上常因缺 FFmpeg DLL 而“装了却不可用”），失败时回退 pyav。
    """
    if requested:
        return requested
    if importlib.util.find_spec("torchcodec"):
        try:
            importlib.import_module("torchcodec")
            return "torchcodec"
        except (ImportError, OSError, RuntimeError) as exc:
            logger.warning(
                "torchcodec is installed but cannot be loaded (%s). Falling back to pyav.", exc
            )
            return "pyav"
    return "pyav"


class TrainingRequest(BaseModel):
    """前端“训练配置”表单对应的 Pydantic 模型（LeLab 的请求 schema）。

    字段分组与前端表单一一对应：
    - 数据集：dataset_repo_id / dataset_repo_ids（多数据集→组合数据集）、采样
      权重模式（frames/equal/custom）、revision/root/episodes 子集、图像增强、
      视频解码后端；
    - 策略：policy_type（act/diffusion/smolvla/groot...）与可选的
      policy_pretrained_path（提供时为微调，否则按类型初始化新策略）；
    - 核心训练参数：steps 训练步数、batch_size、seed、num_workers；
    - 日志与 checkpoint：log_freq / save_freq / save_checkpoint；
    - 输出：output_dir、resume（断点续训）、job_name；
    - 其余：W&B 上报、环境评估 env/eval、优化器 type/lr/weight_decay/
      grad_clip_norm、GR00T 专属选项等。
    LeLab 只用这些字段描述“想跑什么训练”；真正消费它们的是 LeRobot 的
    TrainPipelineConfig（由 build_training_command 生成的 CLI 参数解析而来）。
    """
    # Dataset configuration
    dataset_repo_id: str
    #: Two or more members turn this request into a composite-dataset run: the
    #: JobRegistry resolves it to a derived ``local/composite_<hash>`` dataset
    #: built on the training server, and replaces `dataset_repo_id` with it.
    dataset_repo_ids: list[str] | None = None
    #: Sampling weights across the members: frame-proportional (default),
    #: equal per dataset, or explicit per-dataset values.
    dataset_weights_mode: Literal["frames", "equal", "custom"] = "frames"
    dataset_weights: list[float] | None = None
    #: Per-episode sampling weight used by the weighted sampler. Each value is
    #: the relative weight of every *frame* of that episode, in composite
    #: episode order (member order, member episodes in order).
    dataset_episode_weights: list[float] | None = None
    #: Filled in when a multi-dataset request is resolved to its composite.
    dataset_composite_hash: str | None = None
    #: Job-relative weights file written by the runner for the weighted sampler.
    dataset_weights_file: str | None = None
    dataset_revision: str | None = None
    dataset_root: str | None = None
    dataset_episodes: list[int] | None = None
    dataset_image_transforms_enable: bool = False
    dataset_video_backend: str | None = None  # "torchcodec" or "pyav"; auto-detected when None

    # Policy configuration
    policy_type: str = "act"
    # Optional local directory or Hugging Face model id used to initialize a
    # fine-tuning run. LeRobot treats --policy.path and --policy.type as
    # mutually exclusive, so the command builder emits only one of them.
    policy_pretrained_path: str | None = None

    # Core training parameters
    steps: int = 10000
    batch_size: int = 8
    seed: int | None = 1000
    num_workers: int = 4

    # Logging and checkpointing
    log_freq: int = 250
    save_freq: int = 1000
    env_eval_freq: int = 0
    save_checkpoint: bool = True
    # Validation loss cadence and held-out episodes. The explicit episode list
    # is the auditable split (e.g. Scene1 validation 0..11); eval_split is the
    # optional ratio-based alternative. Only one may be set.
    eval_steps: int = 0
    max_eval_samples: int = 0
    dataset_eval_split: float | None = None
    dataset_eval_episodes: list[int] | None = None
    # Low-frequency model/trainable weight L2 sampling (0 disables). 250 keeps
    # the all-parameter pass off the hot path while still showing a curve.
    weight_norm_freq: int = 250
    # Optional named action-dimension groups for SmolVLA grouped loss
    # diagnostics (e.g. 10 joints + 2 grippers for Scene1).
    action_loss_groups: dict[str, list[int]] | None = None

    # Output configuration
    output_dir: str = "outputs/train"
    resume: bool = False
    job_name: str | None = None

    # Weights & Biases
    wandb_enable: bool = False
    wandb_project: str | None = None
    wandb_entity: str | None = None
    wandb_notes: str | None = None
    wandb_run_id: str | None = None
    wandb_mode: str | None = "online"
    wandb_disable_artifact: bool = False

    # FastWAM declares its own action/state width. LeRobot's defaults (7 / 8)
    # describe a different body, so a 6-DOF SO-101 or a 12-dim bimanual dataset
    # would silently train against the wrong shape. The app therefore requires
    # both values explicitly for `fastwam` and never forwards them to a policy
    # whose config has no such field.
    policy_action_dim: int | None = None
    policy_proprio_dim: int | None = None

    # LoRA / PEFT fine-tuning. Full fine-tuning of a 6B backbone needs ~72 GB of
    # training state (weights + grads + Adam moments), so a single 32-48 GB card
    # can only fine-tune with adapters. The policy supplies its own defaults for
    # target modules and for the layers the base checkpoint does not contain.
    use_peft: bool = False
    peft_r: int | None = None
    peft_alpha: int | None = None
    peft_dropout: float | None = None

    # Environment / evaluation
    env_type: str | None = None
    env_task: str | None = None
    eval_n_episodes: int = 10
    eval_batch_size: int = 50
    eval_use_async_envs: bool = False

    # Policy-specific
    policy_device: str | None = "cuda"
    policy_use_amp: bool = False
    # Hub upload (set by HfCloudJobRunner; not exposed in the form)
    policy_push_to_hub: bool = False
    policy_repo_id: str | None = None

    # GR00T-specific policy options (only emitted when policy_type == "groot";
    # these flags don't exist on other policy configs, so draccus would reject
    # them). All optional — unset fields fall back to the groot config defaults.
    policy_base_model_path: str | None = None
    policy_embodiment_tag: str | None = None
    policy_chunk_size: int | None = None
    policy_n_action_steps: int | None = None
    policy_use_relative_actions: bool | None = None
    policy_relative_exclude_joints: list[str] | None = None
    policy_use_bf16: bool | None = None

    # Optimizer
    optimizer_type: str | None = "adam"
    optimizer_lr: float | None = None
    optimizer_weight_decay: float | None = None
    optimizer_grad_clip_norm: float | None = None

    # Scheduler. These are explicit overrides applied *after* the policy
    # preset (LeRobot's validate); empty fields keep the preset/checkpoint
    # values. Supported types: cosine_decay_with_warmup,
    # cosine_annealing_with_warmup, constant_with_warmup.
    scheduler_type: str | None = None
    scheduler_warmup_steps: int | None = None
    scheduler_decay_steps: int | None = None
    scheduler_peak_lr: float | None = None
    scheduler_floor_lr: float | None = None

    # Advanced
    use_policy_training_preset: bool = True
    config_path: str | None = None
    #: Set only by the resume flow: a checkpoint `pretrained_model` directory
    #: (local absolute, or server-relative for ssh_remote). When present the
    #: command is the minimal LeRobot resume invocation and all optimizer/
    #: scheduler/split settings come from the checkpoint.
    resume_config_path: str | None = None

    @model_validator(mode="after")
    def _normalize_dataset_selection(self) -> "TrainingRequest":
        """配置校验（Pydantic 模型级校验器）：实例化后统一归一化数据集选择。

        - 单/多数据集互相同步：给了 dataset_repo_ids 就清洗去重并回填
          dataset_repo_id=第一个成员；否则把 dataset_repo_id 包成单元素列表；
        - 非 frames 的权重模式（equal/custom）至少需要两个数据集；
        - custom 模式要求 dataset_weights 与成员一一对应且全部为正。
        校验失败抛 ValueError——这是前端表单提交被拒绝的主要原因来源之一。
        """
        if self.dataset_repo_ids is not None:
            selected = [str(item).strip() for item in self.dataset_repo_ids if str(item).strip()]
            if not selected:
                raise ValueError("dataset_repo_ids must contain at least one dataset")
            if len(set(selected)) != len(selected):
                raise ValueError("dataset_repo_ids must not contain duplicates")
            self.dataset_repo_ids = selected
            self.dataset_repo_id = selected[0]
        else:
            if not str(self.dataset_repo_id).strip():
                raise ValueError("dataset_repo_id is required")
            selected = [self.dataset_repo_id.strip()]
            self.dataset_repo_ids = selected

        if self.dataset_weights_mode != "frames" and len(selected) < 2:
            raise ValueError(
                f"dataset_weights_mode={self.dataset_weights_mode!r} needs at least two datasets"
            )
        if self.dataset_weights_mode == "custom":
            weights = self.dataset_weights
            if weights is None or len(weights) != len(selected):
                raise ValueError(
                    f"dataset_weights needs {len(selected)} values, "
                    f"got {0 if weights is None else len(weights)}"
                )
            if any(weight is None or float(weight) <= 0 for weight in weights):
                raise ValueError("dataset_weights must all be greater than 0")
        return self

    @model_validator(mode="after")
    def _validate_monitoring_options(self) -> "TrainingRequest":
        if self.eval_steps < 0:
            raise ValueError("eval_steps must be >= 0")
        if self.max_eval_samples < 0:
            raise ValueError("max_eval_samples must be >= 0")
        if self.dataset_eval_split is not None and self.dataset_eval_episodes:
            raise ValueError("Set either dataset_eval_split or dataset_eval_episodes, not both")
        if self.dataset_eval_split is not None and not (0.0 < float(self.dataset_eval_split) < 1.0):
            raise ValueError("dataset_eval_split must be in (0, 1)")
        if self.dataset_eval_episodes is not None:
            if not self.dataset_eval_episodes:
                raise ValueError("dataset_eval_episodes must not be empty")
            if any(int(ep) < 0 for ep in self.dataset_eval_episodes):
                raise ValueError("dataset_eval_episodes must be non-negative")
            if len(set(self.dataset_eval_episodes)) != len(self.dataset_eval_episodes):
                raise ValueError("dataset_eval_episodes contains duplicates")
            if self.dataset_episodes and set(self.dataset_episodes) & set(self.dataset_eval_episodes):
                raise ValueError(
                    "dataset_eval_episodes must not overlap dataset_episodes; "
                    "leave dataset_episodes empty so the training split is derived"
                )
        if self.eval_steps > 0 and not (self.dataset_eval_split or self.dataset_eval_episodes):
            raise ValueError(
                "eval_steps > 0 requires dataset_eval_episodes or dataset_eval_split so validation "
                "data is explicit"
            )
        if self.weight_norm_freq < 0:
            raise ValueError("weight_norm_freq must be >= 0")
        if self.action_loss_groups is not None:
            if not isinstance(self.action_loss_groups, dict) or not self.action_loss_groups:
                raise ValueError("action_loss_groups must be a non-empty object")
            used: set[int] = set()
            for name, indices in self.action_loss_groups.items():
                if not str(name).strip():
                    raise ValueError("action_loss_groups names must be non-empty")
                if not indices:
                    raise ValueError(f"action_loss_groups[{name!r}] must not be empty")
                for index in indices:
                    if int(index) < 0:
                        raise ValueError(f"action_loss_groups[{name!r}] has a negative index")
                    if int(index) in used:
                        raise ValueError(f"action dimension {index} appears in more than one group")
                    used.add(int(index))
        supported = {"cosine_decay_with_warmup", "cosine_annealing_with_warmup", "constant_with_warmup"}
        if self.scheduler_type is not None and self.scheduler_type not in supported:
            raise ValueError(
                f"Unsupported scheduler_type {self.scheduler_type!r}. Supported: {sorted(supported)}"
            )
        for name in ("scheduler_warmup_steps", "scheduler_decay_steps"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be >= 0")
        for name in ("scheduler_peak_lr", "scheduler_floor_lr"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be >= 0")
        return self

    @property
    def dataset_ids(self) -> list[str]:
        """The selected members, always normalized to a non-empty list.

        中文：选中的数据集成员列表；单数据集时也归一化为长度 1 的列表，
        供 jobs.py 判断是否走组合数据集流程。
        """
        return list(self.dataset_repo_ids or [self.dataset_repo_id])


_UNMAPPED_TRACKING_FIELDS: tuple[str, ...] = (
    "wandb_entity",
    "wandb_notes",
    "wandb_run_id",
    "wandb_mode",
)

# Request fields that only exist when the local monitor patch is present in the
# installed LeRobot. `request_attr` is the TrainingRequest field that decides
# whether the user actually asked for the feature.
_MONITOR_ONLY_FLAGS: tuple[tuple[str, str], ...] = (
    ("weight_norm_freq", "weight_norm_freq"),
    ("action_loss_groups", "action_loss_groups"),
)


def _training_config_field_names() -> frozenset[str]:
    """Field names of the installed ``TrainPipelineConfig`` (empty if unavailable).

    Probed per call rather than cached at import so a venv switch inside the
    same process — or a partially imported lerobot — cannot pin a stale answer.
    """
    try:
        import dataclasses

        from lerobot.configs.train import TrainPipelineConfig
    except Exception:  # pragma: no cover - lerobot missing is a broken install
        return frozenset()
    return frozenset(field.name for field in dataclasses.fields(TrainPipelineConfig))


def unsupported_training_fields(request: TrainingRequest) -> list[str]:
    """Monitoring flags the request asked for but the installed LeRobot cannot take.

    LeRobot 0.7.0 dropped ``weight_norm_freq`` / ``action_loss_groups``; emitting
    them makes the trainer abort on an unknown option, so they are omitted and
    surfaced here for the API/UI to explain the degraded monitoring.
    """
    fields = _training_config_field_names()
    missing: list[str] = []
    for flag, attr in _MONITOR_ONLY_FLAGS:
        if flag in fields:
            continue
        value = getattr(request, attr, None)
        # weight_norm_freq has a non-zero default, so only a changed value counts
        # as "asked for"; the list flag is None when unset.
        if attr == "weight_norm_freq" and value in (None, 0, 250):
            continue
        if value:
            missing.append(flag)
    return missing


def unmapped_tracking_fields(request: TrainingRequest) -> list[str]:
    """Request fields that have no faithful LeRobot 0.7 Trackio counterpart.

    They are accepted so older clients keep working, but forwarding them would
    either abort the run (unknown draccus flag) or silently mean something else
    (a Space repo is not an entity, a display name is not a run id). Callers
    surface this list in the API response / UI instead.
    """
    return [name for name in _UNMAPPED_TRACKING_FIELDS if getattr(request, name, None)]


def build_training_command(
    request: TrainingRequest,
    output_dir: str,
    python_executable: str = "python",
    job_target: "JobTarget | None" = None,
) -> list[str]:
    """Build the argv list to invoke `<python_executable> -m lerobot.scripts.lerobot_train`.

    `output_dir` is supplied separately from the request so the caller (the
    JobRegistry) can pin it to the per-job directory rather than relying on
    request.output_dir, which the frontend doesn't even send in the new world.

    `python_executable` defaults to "python" for the cloud runner (whose
    container has lerobot on PATH); the local runner must pass sys.executable
    so the subprocess uses the same interpreter as lelab itself — otherwise
    PATH lookup picks up a different env (uv tool venv, miniforge3 base, etc.)
    that lacks lerobot.

    中文说明：这是“网页配置 → 训练命令”的核心转换。把 TrainingRequest 的每个
    字段映射为 LeRobot CLI 的 draccus 长参数，例如 batch_size → --batch_size、
    数据集 → --dataset.repo_id、优化器 → --optimizer.lr、W&B → --wandb.enable
    等；最终由 LocalJobRunner / SshRemoteJobRunner 以
    `python -m lerobot.scripts.lerobot_train <args>` 的形式启动 LeRobot 训练。
    要点：
    - 多数据集请求必须先在训练机上解析为组合数据集（dataset_composite_hash），
      否则直接报错拒绝；
    - --policy.path（微调）与 --policy.type（按类型初始化）互斥，只输出其一；
    - HF 云端运行（hf_cloud）不输出 --output_dir 与 --policy.push_to_hub/repo_id
      （由远端 pod 的 submit_to_hf 接管），并通过 --job.target=<flavor> 调度上云。
    """
    cmd: list[str] = [python_executable, "-m", "lerobot.scripts.lerobot_train"]

    is_cloud = job_target is not None and job_target.runner == "hf_cloud"

    # Resume of a paused LeLab job: the checkpoint's train_config.json is the
    # authoritative source for optimizer, scheduler, steps, dataset split and
    # policy. Only the output dir and the explicit checkpoint are passed, so a
    # resume cannot silently change the learning-rate schedule.
    if request.resume and request.resume_config_path and not is_cloud:
        cmd.extend(["--resume", "true"])
        cmd.extend(["--config_path", request.resume_config_path])
        cmd.extend(["--output_dir", output_dir])
        return cmd

    # Dataset
    effective_repo_id = request.dataset_repo_id
    if len(request.dataset_ids) > 1:
        if not request.dataset_composite_hash:
            raise ValueError(
                "Multi-dataset training must be resolved to a composite dataset first "
                "(dataset_composite_hash is missing)"
            )
        # The composite id is a pure function of the hash, so it stays correct
        # even if `dataset_repo_id` still names the first member.
        from .composites import composite_repo_id

        effective_repo_id = composite_repo_id(request.dataset_composite_hash)
    cmd.extend(["--dataset.repo_id", effective_repo_id])
    if request.dataset_weights_mode != "frames":
        if not request.dataset_weights_file:
            raise ValueError(
                f"dataset_weights_mode={request.dataset_weights_mode!r} requires a weights file "
                "(--dataset.sample_weights_file)"
            )
        cmd.extend(["--dataset.sample_weights_file", request.dataset_weights_file])
    if request.dataset_revision:
        cmd.extend(["--dataset.revision", request.dataset_revision])
    if request.dataset_root:
        cmd.extend(["--dataset.root", request.dataset_root])
    if request.dataset_episodes:
        cmd.extend(["--dataset.episodes"] + [str(ep) for ep in request.dataset_episodes])
    if request.dataset_eval_episodes:
        # draccus parses list[int] from a single JSON token.
        cmd.extend(["--dataset.eval_episodes", json.dumps(request.dataset_eval_episodes)])
    if request.dataset_eval_split is not None:
        cmd.extend(["--dataset.eval_split", str(request.dataset_eval_split)])
    if request.dataset_image_transforms_enable:
        cmd.extend(["--dataset.image_transforms.enable", "true"])

    # Video backend – auto-detect when not explicitly set (fixes torchcodec/FFmpeg
    # DLL loading failures on Windows where the package is installed but broken).
    video_backend = _resolve_video_backend(request.dataset_video_backend)
    cmd.extend(["--dataset.video_backend", video_backend])

    # Policy. A pretrained source selects fine-tuning; otherwise initialize a
    # fresh policy of the requested type. LeRobot rejects path + type together.
    if request.policy_pretrained_path:
        cmd.extend(["--policy.path", request.policy_pretrained_path])
    else:
        cmd.extend(["--policy.type", request.policy_type])

    # Core training params
    cmd.extend(["--steps", str(request.steps)])
    cmd.extend(["--batch_size", str(request.batch_size)])
    cmd.extend(["--num_workers", str(request.num_workers)])
    if request.seed is not None:
        cmd.extend(["--seed", str(request.seed)])

    # Policy device / AMP / hub
    if request.policy_device:
        cmd.extend(["--policy.device", request.policy_device])
    cmd.extend(["--policy.use_amp", "true" if request.policy_use_amp else "false"])
    # On HF Cloud, lerobot's submit_to_hf owns the model repo and sets push_to_hub on
    # the pod itself; _pod_forwarded_args drops any --policy.push_to_hub/--policy.repo_id
    # we'd pass, so we must not emit them. Local runs keep the existing behavior:
    # LeRobot defaults push_to_hub=True and demands --policy.repo_id when so.
    if not is_cloud:
        cmd.extend(["--policy.push_to_hub", "true" if request.policy_push_to_hub else "false"])
        if request.policy_push_to_hub and request.policy_repo_id:
            cmd.extend(["--policy.repo_id", request.policy_repo_id])

    # GR00T-specific policy flags. These options only exist on the groot config,
    # so emit them only for --policy.type=groot; sending them to another policy
    # would make draccus abort the run. Each is skipped when unset so the groot
    # config's own defaults apply.
    if request.policy_type == "groot":
        if request.policy_base_model_path:
            cmd.extend(["--policy.base_model_path", request.policy_base_model_path])
        if request.policy_embodiment_tag:
            cmd.extend(["--policy.embodiment_tag", request.policy_embodiment_tag])
        if request.policy_chunk_size is not None:
            cmd.extend(["--policy.chunk_size", str(request.policy_chunk_size)])
        if request.policy_n_action_steps is not None:
            cmd.extend(["--policy.n_action_steps", str(request.policy_n_action_steps)])
        if request.policy_use_relative_actions is not None:
            cmd.extend(
                ["--policy.use_relative_actions", "true" if request.policy_use_relative_actions else "false"]
            )
        if request.policy_relative_exclude_joints is not None:
            # draccus parses list values from a single JSON token, e.g. '["gripper"]'.
            cmd.extend(
                ["--policy.relative_exclude_joints", json.dumps(request.policy_relative_exclude_joints)]
            )
        if request.policy_use_bf16 is not None:
            cmd.extend(["--policy.use_bf16", "true" if request.policy_use_bf16 else "false"])

    # FastWAM-specific policy flags. `action_dim`/`proprio_dim` exist only on the
    # fastwam config, and its defaults (7 action / 8 state) describe a different
    # robot body. For an SO-101 arm (6) or a bimanual pair (12) inheriting those
    # defaults would train the wrong shape without any error, so both must be
    # stated explicitly and are never forwarded to another policy type.
    if request.policy_type == "fastwam":
        missing = [
            name
            for name, value in (
                ("policy_action_dim", request.policy_action_dim),
                ("policy_proprio_dim", request.policy_proprio_dim),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                "fastwam requires explicit dimensions so the 7-action/8-state defaults "
                f"are not applied to a 6- or 12-channel dataset; missing: {missing}"
            )
        cmd.extend(["--policy.action_dim", str(request.policy_action_dim)])
        cmd.extend(["--policy.proprio_dim", str(request.policy_proprio_dim)])
    elif request.policy_action_dim is not None or request.policy_proprio_dim is not None:
        raise ValueError(
            "policy_action_dim/policy_proprio_dim only apply to fastwam; "
            f"policy_type={request.policy_type!r} has no such config field"
        )

    # PEFT / LoRA. LeRobot refuses adapter training without a pretrained policy
    # (`--policy.path`), so that precondition is enforced here with a message the
    # operator can act on instead of a trainer traceback.
    if request.use_peft:
        if not request.policy_pretrained_path:
            raise ValueError(
                "LoRA fine-tuning needs a pretrained policy checkpoint "
                "(policy_pretrained_path); training adapters from scratch is refused"
            )
        if request.peft_r is not None and request.peft_r <= 0:
            raise ValueError(f"peft_r must be positive, got {request.peft_r}")
        if request.peft_alpha is not None and request.peft_alpha <= 0:
            raise ValueError(f"peft_alpha must be positive, got {request.peft_alpha}")
        if request.peft_dropout is not None and not 0.0 <= request.peft_dropout < 1.0:
            raise ValueError(f"peft_dropout must be in [0, 1), got {request.peft_dropout}")
        cmd.extend(["--policy.use_peft", "true"])
        if request.peft_r is not None:
            cmd.extend(["--peft.r", str(request.peft_r)])
        if request.peft_alpha is not None:
            cmd.extend(["--peft.lora_alpha", str(request.peft_alpha)])
        if request.peft_dropout is not None:
            cmd.extend(["--peft.lora_dropout", str(request.peft_dropout)])
    elif any(v is not None for v in (request.peft_r, request.peft_alpha, request.peft_dropout)):
        raise ValueError("peft_r/peft_alpha/peft_dropout require use_peft=true")

    # Logging / checkpointing
    cmd.extend(["--log_freq", str(request.log_freq)])
    cmd.extend(["--save_freq", str(request.save_freq)])
    cmd.extend(["--env_eval_freq", str(request.env_eval_freq)])
    cmd.extend(["--save_checkpoint", "true" if request.save_checkpoint else "false"])

    # Output. On HF Cloud the pod, not this host, runs the trainer: an absolute host
    # output_dir (e.g. ~/.cache/.../outputs/train) is baked into the staged config and
    # the pod crashes trying to mkdir it under /Users. Checkpoints land on the Hub repo
    # anyway, so we omit it for cloud and let lerobot pick its in-pod default.
    if not is_cloud:
        cmd.extend(["--output_dir", output_dir])
    cmd.extend(["--resume", "true" if request.resume else "false"])
    if request.job_name:
        cmd.extend(["--job_name", request.job_name])

    # Tracking. LeRobot 0.7.0 dropped the `wandb` config section in favour of
    # `trackio` (local SQLite run store, optional HF Space sync). draccus rejects
    # unknown flags, so `--wandb.*` is never emitted — not even a disabled one —
    # and only the trackio options with a real counterpart are forwarded.
    # `entity`, `notes`, `run_id` and `mode` are W&B-only concepts: Trackio's
    # `space_id` is a Space repo, `run_name` is a display name and there is no
    # online/offline switch, so mapping them would silently change meaning.
    # They stay accepted by the API for backward compatibility and are reported
    # by `unmapped_tracking_fields()` instead.
    if request.wandb_enable:
        cmd.extend(["--trackio.enable", "true"])
        if request.wandb_project:
            cmd.extend(["--trackio.project", request.wandb_project])
        cmd.extend(
            ["--trackio.disable_artifact", "true" if request.wandb_disable_artifact else "false"]
        )

    # Env
    if request.env_type:
        cmd.extend(["--env.type", request.env_type])
    if request.env_task:
        cmd.extend(["--env.task", request.env_task])

    # Eval
    cmd.extend(["--eval.n_episodes", str(request.eval_n_episodes)])
    cmd.extend(["--eval.batch_size", str(request.eval_batch_size)])
    cmd.extend(["--eval.use_async_envs", "true" if request.eval_use_async_envs else "false"])
    # Offline validation loss (separate from simulation env_eval_freq).
    cmd.extend(["--eval_steps", str(request.eval_steps)])
    cmd.extend(["--max_eval_samples", str(request.max_eval_samples)])

    # Monitoring instrumentation. These two flags belong to the local training
    # monitor patch. LeRobot 0.7.0 no longer declares them, and draccus aborts on
    # unknown options, so each is emitted only when the installed library really
    # has the field. Otherwise the request is reported by
    # `unsupported_training_fields()` instead of being silently dropped.
    supported_fields = _training_config_field_names()
    if "weight_norm_freq" in supported_fields:
        cmd.extend(["--weight_norm_freq", str(request.weight_norm_freq)])
    if "action_loss_groups" in supported_fields and request.action_loss_groups:
        cmd.extend(["--action_loss_groups", json.dumps(request.action_loss_groups)])

    # Optimizer. With the policy preset active, plain `--optimizer.lr` is
    # silently replaced by `get_optimizer_preset()` inside LeRobot's validate();
    # explicit values therefore travel as post-preset overrides. Without the
    # preset the classic construction flags still apply.
    if request.optimizer_type:
        cmd.extend(["--optimizer.type", request.optimizer_type])
    optimizer_overrides: dict[str, float] = {}
    if request.optimizer_lr is not None:
        optimizer_overrides["lr"] = float(request.optimizer_lr)
    if request.optimizer_weight_decay is not None:
        optimizer_overrides["weight_decay"] = float(request.optimizer_weight_decay)
    if request.optimizer_grad_clip_norm is not None:
        optimizer_overrides["grad_clip_norm"] = float(request.optimizer_grad_clip_norm)
    if request.use_policy_training_preset and not request.resume:
        if optimizer_overrides:
            cmd.extend(["--optimizer_overrides", json.dumps(optimizer_overrides)])
    else:
        if request.optimizer_lr is not None:
            cmd.extend(["--optimizer.lr", str(request.optimizer_lr)])
        if request.optimizer_weight_decay is not None:
            cmd.extend(["--optimizer.weight_decay", str(request.optimizer_weight_decay)])
        if request.optimizer_grad_clip_norm is not None:
            cmd.extend(["--optimizer.grad_clip_norm", str(request.optimizer_grad_clip_norm)])

    # Scheduler overrides are applied after the preset for the same reason.
    # Only *field* overrides are legal: the preset's scheduler type belongs to the
    # policy recipe, so asking for a different type while keeping the preset is a
    # contradiction that LeRobot 0.7 rejects. Refuse it here, before a job exists.
    scheduler_overrides: dict[str, object] = {}
    if request.scheduler_type and request.use_policy_training_preset and not request.resume:
        raise ValueError(
            "scheduler_type cannot be combined with use_policy_training_preset: "
            "disable the preset to choose a scheduler type"
        )
    if request.scheduler_warmup_steps is not None:
        scheduler_overrides["num_warmup_steps"] = int(request.scheduler_warmup_steps)
    if request.scheduler_decay_steps is not None:
        scheduler_overrides["num_decay_steps"] = int(request.scheduler_decay_steps)
    if request.scheduler_peak_lr is not None:
        scheduler_overrides["peak_lr"] = float(request.scheduler_peak_lr)
    if request.scheduler_floor_lr is not None:
        scheduler_overrides["decay_lr"] = float(request.scheduler_floor_lr)
    if scheduler_overrides and not request.resume:
        cmd.extend(["--scheduler_overrides", json.dumps(scheduler_overrides)])

    # Advanced
    cmd.extend(["--use_policy_training_preset", "true" if request.use_policy_training_preset else "false"])
    if request.config_path:
        cmd.extend(["--config_path", request.config_path])

    # HF Jobs: --job.target=<flavor> dispatches the run remotely (lerobot commit #3856).
    # Image/timeout use lerobot's JobConfig defaults. lelab tags its jobs; lerobot always
    # adds a "lerobot" tag too. A pod's local checkpoints die with it, so push each one to
    # the model repo's checkpoints/<step>/ tree (the native replacement for lelab's old
    # in-pod uploader) — that's what makes the trained checkpoints reachable afterwards.
    if is_cloud and job_target.flavor:
        cmd.extend(["--job.target", job_target.flavor])
        cmd.extend(["--job.tags", '["lelab"]'])
        # save_checkpoint_to_hub needs policy.repo_id, which submit_to_hf only sets on the
        # fresh-run path; on a resume it isn't set before validate(), so the flag would
        # abort the submit. A resume already pushes back to its source repo, so skip it.
        if request.save_checkpoint and not request.resume:
            cmd.extend(["--save_checkpoint_to_hub", "true"])

    return cmd
