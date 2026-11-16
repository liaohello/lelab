# Copyright 2026 The LeLab authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""Cooperative monitoring and pause hooks used by LeLab.

Every hook in this module is inert unless LeLab sets the corresponding
``LELAB_*`` environment variable on the training process:

* ``LELAB_RUN_ID`` / ``LELAB_ATTEMPT_ID`` identify the logical run and the
  current attempt in structured metric events.
* ``LELAB_CONTROL_DIR`` enables cooperative pause: the trainer only checks for
  ``pause.request`` inside that directory (a bounded, explicit control marker),
  never a signal to an arbitrary PID.
* ``LELAB_WEIGHT_NORM_FREQ`` enables low-frequency weight-norm sampling.
* ``LELAB_ACTION_GROUPS`` enables optional SmolVLA per-group loss diagnostics.

Structured events are single JSON lines prefixed with ``@@LELAB_METRIC@@``.
They travel through the ordinary stdout log stream, so local and SSH runners
persist them with the same mechanism and LeLab can rebuild history from the
append-only ``log.jsonl`` even after a service restart.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import random
import sys
import time
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import torch

EVENT_PREFIX = "@@LELAB_METRIC@@"
PAUSE_EXIT_CODE = 75
PAUSE_REQUEST_FILE = "pause.request"
PAUSE_STATE_FILE = "pause.state.json"
PAUSE_MANIFEST_FILE = "lelab_pause.json"
MONITOR_VERSION = 1


# ---------------------------------------------------------------------------
# Environment / event emission
# ---------------------------------------------------------------------------


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def run_id() -> str | None:
    return _env("LELAB_RUN_ID")


def attempt_id() -> str | None:
    return _env("LELAB_ATTEMPT_ID")


def control_dir() -> Path | None:
    raw = _env("LELAB_CONTROL_DIR")
    return Path(raw).expanduser() if raw else None


def events_path() -> Path | None:
    raw = _env("LELAB_EVENTS_PATH")
    return Path(raw).expanduser() if raw else None


def monitoring_enabled() -> bool:
    """True when LeLab asked this process to emit structured events."""
    return run_id() is not None or attempt_id() is not None


def _sanitize_non_finite(value: Any, path: str, bad: list[str]) -> Any:
    """Replace NaN/Inf with null and record their field paths.

    A non-finite metric must be distinguishable from a real zero; the event
    keeps the gap plus a ``non_finite`` list instead of writing invalid JSON.
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            bad.append(path or "value")
            return None
        return value
    if isinstance(value, dict):
        return {
            key: _sanitize_non_finite(item, f"{path}.{key}" if path else str(key), bad)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_non_finite(item, f"{path}[{index}]", bad)
            for index, item in enumerate(value)
        ]
    return value


def emit(kind: str, payload: Mapping[str, Any] | None = None) -> None:
    """Write one structured event line to stdout and flush it.

    The prefix is deliberately a single line so the existing line-oriented log
    tailing (local pipe and SSH ``tail``) carries it unchanged.
    """
    if not monitoring_enabled():
        return
    event: dict[str, Any] = {
        "v": MONITOR_VERSION,
        "kind": kind,
        "run_id": run_id(),
        "attempt_id": attempt_id(),
        "timestamp": time.time(),
        "pid": os.getpid(),
    }
    if payload:
        event.update(dict(payload))
    non_finite: list[str] = []
    event = _sanitize_non_finite(event, "", non_finite)
    if non_finite:
        event["non_finite"] = non_finite
    line = EVENT_PREFIX + " " + json.dumps(event, separators=(",", ":")) + "\n"
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except Exception:  # pragma: no cover - logging must never break training
        pass
    # Durable sidecar: survives a parent process crash and lets LeLab rebuild
    # history even when the tailed stdout line was never consumed.
    path = events_path()
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
        except OSError:
            pass


def emit_pause(state: str, **payload: Any) -> None:
    emit("pause", {"state": state, **payload})


def parse_event_line(line: str) -> dict[str, Any] | None:
    """Return the decoded event when ``line`` is a structured event."""
    marker = line.find(EVENT_PREFIX)
    if marker < 0:
        return None
    raw = line[marker + len(EVENT_PREFIX) :].strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------------------
# Pause control marker
# ---------------------------------------------------------------------------


def pause_enabled() -> bool:
    return control_dir() is not None


def pause_requested() -> bool:
    directory = control_dir()
    if directory is None:
        return False
    return (directory / PAUSE_REQUEST_FILE).is_file()


def clear_pause_request() -> None:
    directory = control_dir()
    if directory is None:
        return
    with contextlib.suppress(FileNotFoundError):
        (directory / PAUSE_REQUEST_FILE).unlink()


def write_pause_state(state: str, **payload: Any) -> None:
    """Atomically publish the trainer-side pause state (best effort)."""
    directory = control_dir()
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    body = {
        "v": MONITOR_VERSION,
        "state": state,
        "run_id": run_id(),
        "attempt_id": attempt_id(),
        "timestamp": time.time(),
        **payload,
    }
    target = directory / PAUSE_STATE_FILE
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Norms
# ---------------------------------------------------------------------------


def _unique_parameters(parameters: Iterable[torch.nn.Parameter]) -> list[torch.nn.Parameter]:
    seen: set[int] = set()
    unique: list[torch.nn.Parameter] = []
    for parameter in parameters:
        if id(parameter) in seen:
            continue
        seen.add(id(parameter))
        unique.append(parameter)
    return unique


@torch.no_grad()
def parameter_norms(module: torch.nn.Module) -> dict[str, Any]:
    """Global L2 norm of all parameters and of the trainable subset.

    Definition: ``sqrt(sum(p.float() ** 2))`` over deduplicated parameters, in
    FP32. Shared parameters are counted once. Returns ``None`` for a norm that
    cannot be computed (non-floating dtype or non-finite value) instead of
    silently substituting zero.
    """
    started = time.perf_counter()
    total_sq: torch.Tensor | None = None
    trainable_sq: torch.Tensor | None = None
    total_count = 0
    trainable_count = 0
    unsupported: str | None = None
    for parameter in _unique_parameters(module.parameters()):
        value = parameter.detach()
        if not torch.is_floating_point(value):
            unsupported = f"non-floating parameter dtype {value.dtype}"
            continue
        # Accumulate on the parameter's device (one scalar per tensor) and only
        # transfer the two totals to the host at the end.
        square = value.to(torch.float32).pow(2).sum()
        total_sq = square if total_sq is None else total_sq + square
        total_count += value.numel()
        if parameter.requires_grad:
            trainable_sq = square if trainable_sq is None else trainable_sq + square
            trainable_count += value.numel()
    total = float(total_sq.sqrt().item()) if total_sq is not None else None
    trainable = float(trainable_sq.sqrt().item()) if trainable_sq is not None else None
    if total is not None and not (total == total and abs(total) != float("inf")):
        total = None
    if trainable is not None and not (trainable == trainable and abs(trainable) != float("inf")):
        trainable = None
    return {
        "weight_norm": total,
        "trainable_weight_norm": trainable,
        "total_parameters": total_count,
        "trainable_parameters": trainable_count,
        "dtype": "float32-accum",
        "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "unsupported": unsupported,
    }


@torch.no_grad()
def grad_norm(parameters: Iterable[torch.nn.Parameter]) -> float | None:
    """Total gradient L2 norm without modifying gradients.

    Used when gradient clipping is disabled, where LeRobot would otherwise
    reuse the previous value (or report nothing). Returns ``None`` when no
    gradient is present so callers can distinguish "not sampled" from zero.
    """
    unique = _unique_parameters(parameters)
    total_sq: torch.Tensor | None = None
    for parameter in unique:
        gradient = parameter.grad
        if gradient is None or not torch.is_floating_point(gradient):
            continue
        square = gradient.detach().to(torch.float32).pow(2).sum()
        total_sq = square if total_sq is None else total_sq + square
    if total_sq is None:
        return None
    value = float(total_sq.sqrt().item())
    return value if value == value and abs(value) != float("inf") else None


# ---------------------------------------------------------------------------
# RNG isolation for validation
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def isolated_rng() -> Iterator[None]:
    """Run a block without advancing the training random streams.

    Validation draws flow-matching noise and may seed DataLoader workers; both
    consume the global RNGs. Saving and restoring CPU/CUDA/Python/NumPy state
    around the evaluation keeps the training trajectory identical with or
    without validation enabled.
    """
    cpu_state = torch.get_rng_state()
    cuda_states = None
    if torch.cuda.is_available() and torch.cuda.is_initialized():
        cuda_states = torch.cuda.get_rng_state_all()
    python_state = random.getstate()
    numpy_state = None
    try:
        import numpy as np  # local import keeps the dependency optional

        numpy_state = np.random.get_state()
    except Exception:  # pragma: no cover - numpy is a LeRobot dependency
        pass
    try:
        yield
    finally:
        torch.set_rng_state(cpu_state)
        if cuda_states is not None:
            torch.cuda.set_rng_state_all(cuda_states)
        random.setstate(python_state)
        if numpy_state is not None:
            import numpy as np

            np.random.set_state(numpy_state)


# ---------------------------------------------------------------------------
# Action loss groups (SmolVLA diagnostics)
# ---------------------------------------------------------------------------


def parse_action_groups(raw: str | None) -> dict[str, list[int]] | None:
    """Parse ``LELAB_ACTION_GROUPS`` into ``{name: [dim, ...]}``.

    Returns ``None`` when unset. Raises ``ValueError`` on malformed input so a
    typo cannot silently degrade into an ungrouped run.
    """
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LELAB_ACTION_GROUPS is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise ValueError("LELAB_ACTION_GROUPS must be a non-empty object of name -> [dims]")
    groups: dict[str, list[int]] = {}
    used: set[int] = set()
    for name, indices in payload.items():
        if not isinstance(name, str) or not name:
            raise ValueError("LELAB_ACTION_GROUPS keys must be non-empty strings")
        if not isinstance(indices, list) or not indices:
            raise ValueError(f"LELAB_ACTION_GROUPS[{name!r}] must be a non-empty list")
        parsed: list[int] = []
        for value in indices:
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"LELAB_ACTION_GROUPS[{name!r}] contains a non-negative index")
            if value in used:
                raise ValueError(f"action dimension {value} appears in more than one group")
            used.add(value)
            parsed.append(value)
        groups[name] = parsed
    return groups


@torch.no_grad()
def grouped_loss_means(
    losses: torch.Tensor,
    action_is_pad: torch.Tensor | None,
    groups: Mapping[str, list[int]],
) -> dict[str, float]:
    """Per-group means of the same masked per-element flow loss.

    ``losses`` is ``(B, T, D)``. The group means are an exact decomposition of
    the total mean when the groups partition the action dimensions: multiplying
    each group mean by its dimension share and summing recovers ``losses.mean``
    over valid entries. Invalid/padded entries are excluded here even if the
    caller already zeroed them.
    """
    if losses.ndim != 3:
        raise ValueError(f"losses must be (B, T, D), got {tuple(losses.shape)}")
    if action_is_pad is not None:
        if action_is_pad.shape != losses.shape[:2]:
            raise ValueError(
                f"action_is_pad must be {tuple(losses.shape[:2])}, got {tuple(action_is_pad.shape)}"
            )
        losses = losses * (~action_is_pad).unsqueeze(-1)
        valid_entries = int((~action_is_pad).sum().item())
    else:
        valid_entries = losses.shape[0] * losses.shape[1]
    valid_entries = max(valid_entries, 1)
    result: dict[str, float] = {}
    for name, indices in groups.items():
        index = torch.tensor(indices, device=losses.device, dtype=torch.long)
        group_sum = losses.index_select(-1, index).sum()
        result[name] = float((group_sum / (valid_entries * len(indices))).item())
    return result


def configure_action_groups(
    policy: torch.nn.Module,
    action_dim: int | None,
    groups: Mapping[str, list[int]] | None = None,
) -> dict[str, list[int]] | None:
    """Attach validated loss groups to a policy instance.

    The attribute (not the persisted config) is read by SmolVLA's forward, so
    enabling the diagnostic never changes a checkpoint's serialized config or
    the training objective. ``groups`` takes precedence when provided (the
    config field); otherwise ``LELAB_ACTION_GROUPS`` is parsed.
    """
    if groups is None:
        groups = parse_action_groups(os.environ.get("LELAB_ACTION_GROUPS"))
    else:
        groups = {str(name): [int(index) for index in indices] for name, indices in groups.items()}
    if groups is None:
        return None
    if action_dim is None:
        raise ValueError("LELAB_ACTION_GROUPS requires a policy with a known action dimension")
    for name, indices in groups.items():
        out_of_range = [index for index in indices if index >= action_dim]
        if out_of_range:
            raise ValueError(
                f"LELAB_ACTION_GROUPS[{name!r}] indexes {out_of_range} outside the "
                f"{action_dim}-dimensional action space"
            )
    policy._lelab_action_groups = groups
    return groups


# ---------------------------------------------------------------------------
# Files / checkpoint manifest
# ---------------------------------------------------------------------------


def sha256_file(path: Path, *, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _file_record(path: Path, *, hash_contents: bool = True) -> dict[str, Any]:
    record: dict[str, Any] = {"name": path.name, "size": path.stat().st_size}
    if hash_contents:
        record["sha256"] = sha256_file(path)
    return record


def build_checkpoint_manifest(
    checkpoint_dir: Path,
    *,
    step: int,
    cfg: Any,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe and hash a completed checkpoint for pause/resume verification.

    Required artifacts are model weights, config, train config, and the
    training-state files that carry optimizer/scheduler/RNG/step. Processor
    files and any action-representation manifest are recorded when present.
    """
    pretrained = checkpoint_dir / "pretrained_model"
    state = checkpoint_dir / "training_state"
    required = [
        pretrained / "model.safetensors",
        pretrained / "config.json",
        pretrained / "train_config.json",
        state / "training_step.json",
        state / "rng_state.safetensors",
        state / "optimizer_state.safetensors",
        state / "scheduler_state.json",
    ]
    missing = [str(path.relative_to(checkpoint_dir)) for path in required if not path.is_file()]
    files = []
    for path in required:
        if path.is_file():
            files.append({"path": str(path.relative_to(checkpoint_dir)), **_file_record(path)})
    optional: list[dict[str, Any]] = []
    for pattern in (
        "pretrained_model/policy_preprocessor*",
        "pretrained_model/policy_postprocessor*",
        "pretrained_model/action_representation.json",
        "training_state/ema_state.pt",
    ):
        for path in sorted(checkpoint_dir.glob(pattern)):
            if path.is_file():
                optional.append(
                    {
                        "path": str(path.relative_to(checkpoint_dir)),
                        **_file_record(path, hash_contents=path.stat().st_size < 64 * 1024 * 1024),
                    }
                )
    train_config = pretrained / "train_config.json"
    config_sha = sha256_file(train_config) if train_config.is_file() else None
    return {
        "v": MONITOR_VERSION,
        "kind": "pause_checkpoint",
        "run_id": run_id(),
        "attempt_id": attempt_id(),
        "step": int(step),
        "created_at": time.time(),
        "checkpoint_dir": str(checkpoint_dir),
        "config_sha256": config_sha,
        "total_steps": int(getattr(cfg, "steps", 0) or 0),
        "required_files": files,
        "optional_files": optional,
        "missing": missing,
        "complete": not missing,
        "extra": dict(extra or {}),
    }


def manifest_matches(checkpoint_dir: Path, manifest: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Verify a manifest against the checkpoint currently on disk."""
    if not manifest.get("complete"):
        return False, "checkpoint manifest is incomplete"
    for record in manifest.get("required_files") or []:
        path = checkpoint_dir / str(record.get("path"))
        if not path.is_file():
            return False, f"missing {record.get('path')}"
        if int(path.stat().st_size) != int(record.get("size", -1)):
            return False, f"size mismatch for {record.get('path')}"
        if record.get("sha256") and sha256_file(path) != record["sha256"]:
            return False, f"sha256 mismatch for {record.get('path')}"
    expected_config = manifest.get("config_sha256")
    if expected_config:
        train_config = checkpoint_dir / "pretrained_model" / "train_config.json"
        if not train_config.is_file():
            return False, "missing pretrained_model/train_config.json"
        if sha256_file(train_config) != expected_config:
            return False, "train_config.json changed since the pause manifest was written"
    return True, None


# ---------------------------------------------------------------------------
# Resolved configuration snapshot
# ---------------------------------------------------------------------------


def _optimizer_snapshot(optimizer: torch.optim.Optimizer) -> dict[str, Any]:
    # accelerate wraps the optimizer; the inner object keeps the original
    # class name and param groups.
    inner = getattr(optimizer, "optimizer", optimizer)
    groups = []
    for index, group in enumerate(inner.param_groups):
        groups.append(
            {
                "index": index,
                "lr": float(group.get("lr", 0.0)),
                "weight_decay": float(group.get("weight_decay", 0.0)) if group.get("weight_decay") is not None else None,
                "betas": list(group["betas"]) if group.get("betas") is not None else None,
                "eps": float(group["eps"]) if group.get("eps") is not None else None,
                "parameter_count": len(group.get("params") or []),
            }
        )
    return {"class": type(inner).__name__, "param_groups": groups}


def resolved_config_payload(
    cfg: Any,
    *,
    policy: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler.LRScheduler | None,
    dataset: Any,
    eval_dataset: Any,
    world_size: int,
    device: str,
    action_groups: Mapping[str, list[int]] | None,
) -> dict[str, Any]:
    """Snapshot of what the trainer is actually using after ``validate()``."""
    from dataclasses import asdict, is_dataclass

    dataset_cfg = cfg.dataset
    scheduler_snapshot: dict[str, Any] | None = None
    if scheduler is not None:
        inner = getattr(scheduler, "scheduler", None) or getattr(scheduler, "lr_scheduler", None)
        scheduler_snapshot = {
            "type": type(inner if inner is not None else scheduler).__name__,
            "base_lrs": [
                float(value)
                for value in getattr(inner if inner is not None else scheduler, "base_lrs", [])
            ],
            "last_epoch": int(
                getattr(inner if inner is not None else scheduler, "last_epoch", -1)
            ),
        }
        if inner is not None and is_dataclass(inner):
            snapshot = asdict(inner)
            snapshot = {key: value for key, value in snapshot.items() if key not in {"optimizer"}}
            scheduler_snapshot["config"] = snapshot
    total_parameters = sum(parameter.numel() for parameter in policy.parameters())
    trainable_parameters = sum(parameter.numel() for parameter in policy.parameters() if parameter.requires_grad)
    return {
        "policy_type": getattr(getattr(policy, "config", None), "type", None),
        "steps": int(cfg.steps),
        "batch_size": int(cfg.batch_size),
        "seed": cfg.seed,
        "num_workers": int(cfg.num_workers),
        "grad_accum_steps": int(getattr(cfg.accelerator.gradient_accumulation, "steps", 1)),
        "world_size": int(world_size),
        "device": str(device),
        "mixed_precision": str(getattr(cfg.accelerator, "mixed_precision", "no")),
        "dataset": {
            "repo_id": str(dataset_cfg.repo_id),
            "root": dataset_cfg.root,
            "revision": dataset_cfg.revision,
            "requested_episodes": list(dataset_cfg.episodes) if dataset_cfg.episodes is not None else None,
            "eval_split": float(dataset_cfg.eval_split),
            "eval_episodes_requested": list(getattr(dataset_cfg, "eval_episodes", None) or [])
            if getattr(dataset_cfg, "eval_episodes", None)
            else None,
            "train_episodes": list(getattr(dataset, "episodes", None) or []),
            "eval_episodes": list(getattr(eval_dataset, "episodes", None) or []) if eval_dataset is not None else [],
            "num_train_frames": int(getattr(dataset, "num_frames", 0)),
            "num_eval_frames": int(getattr(eval_dataset, "num_frames", 0)) if eval_dataset is not None else 0,
            "stats_source": "dataset metadata (train-only stats come from the experiment processor when injected)",
        },
        "optimizer": {
            **_optimizer_snapshot(optimizer),
            "config_type": getattr(cfg.optimizer, "type", None),
            "config": asdict(cfg.optimizer) if is_dataclass(cfg.optimizer) else None,
            "overrides": dict(cfg.optimizer_overrides or {}),
        },
        "scheduler": {
            "config_type": getattr(cfg.scheduler, "type", None),
            "config": asdict(cfg.scheduler) if is_dataclass(cfg.scheduler) else None,
            "overrides": dict(cfg.scheduler_overrides or {}),
            "runtime": scheduler_snapshot,
        },
        "use_policy_training_preset": bool(cfg.use_policy_training_preset),
        "eval_steps": int(cfg.eval_steps),
        "max_eval_samples": int(cfg.max_eval_samples),
        "weight_norm_freq": int(getattr(cfg, "weight_norm_freq", 0) or 0),
        "action_loss_groups": dict(action_groups or {}),
        "total_parameters": int(total_parameters),
        "trainable_parameters": int(trainable_parameters),
    }
