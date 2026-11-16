"""Override precedence, explicit eval split and monitor helper tests."""

from __future__ import annotations

import pathlib
import tempfile
from types import SimpleNamespace

import pytest
import torch

from lerobot.configs.default import DatasetConfig
from lerobot.configs.train import TrainPipelineConfig
from lerobot.datasets import factory as dataset_factory
from lerobot.optim.optimizers import AdamWConfig
from lerobot.optim.schedulers import CosineDecayWithWarmupSchedulerConfig
from lerobot.utils import lelab_monitor


class StubPolicyConfig:
    type = "smolvla"
    pretrained_path = None
    push_to_hub = False
    repo_id = None

    def get_optimizer_preset(self):
        return AdamWConfig(
            lr=1e-4,
            betas=(0.9, 0.95),
            eps=1e-8,
            weight_decay=1e-10,
            grad_clip_norm=10.0,
        )

    def get_scheduler_preset(self):
        return CosineDecayWithWarmupSchedulerConfig(
            num_warmup_steps=1000,
            num_decay_steps=30000,
            peak_lr=1e-4,
            decay_lr=2.5e-6,
        )


def _cfg(tmp_path: pathlib.Path, dataset: DatasetConfig | None = None, **kwargs) -> TrainPipelineConfig:
    return TrainPipelineConfig(
        dataset=dataset or DatasetConfig(repo_id="a/b"),
        policy=StubPolicyConfig(),
        output_dir=tmp_path / "run",
        **kwargs,
    )


def test_preset_is_overridden_by_explicit_lr(tmp_path: pathlib.Path) -> None:
    config = _cfg(
        tmp_path,
        optimizer_overrides={"lr": 5e-5, "weight_decay": "1e-9", "betas": [0.9, 0.95]},
    )
    config.validate()
    assert config.optimizer.lr == 5e-5
    assert config.optimizer.weight_decay == 1e-9
    assert config.optimizer.betas == (0.9, 0.95)


def test_scheduler_override_builds_and_coerces_string_numbers(tmp_path: pathlib.Path) -> None:
    config = _cfg(
        tmp_path,
        scheduler_overrides={
            "type": "cosine_decay_with_warmup",
            "num_warmup_steps": "500",
            "num_decay_steps": "15000",
            "peak_lr": "1e-4",
            "decay_lr": "1e-5",
        },
    )
    config.validate()
    assert config.scheduler.type == "cosine_decay_with_warmup"
    assert config.scheduler.num_warmup_steps == 500
    assert config.scheduler.num_decay_steps == 15000
    assert config.scheduler.peak_lr == pytest.approx(1e-4)
    assert config.scheduler.decay_lr == pytest.approx(1e-5)


def test_lr_override_scales_cosine_floor_when_needed(tmp_path: pathlib.Path) -> None:
    config = _cfg(tmp_path, optimizer_overrides={"lr": 1e-6})
    config.validate()
    assert config.scheduler.peak_lr == pytest.approx(1e-6)
    assert config.scheduler.decay_lr <= 1e-6


def test_unknown_override_fields_are_rejected(tmp_path: pathlib.Path) -> None:
    config = _cfg(tmp_path, optimizer_overrides={"momentum": 0.9})
    with pytest.raises(ValueError, match="Unsupported optimizer_overrides"):
        config.validate()
    config = _cfg(tmp_path, scheduler_overrides={"type": "mystery"})
    with pytest.raises(ValueError, match="Unsupported scheduler type"):
        config.validate()


def test_eval_steps_requires_explicit_split(tmp_path: pathlib.Path) -> None:
    config = _cfg(tmp_path, eval_steps=10)
    with pytest.raises(ValueError, match="eval_episodes"):
        config.validate()
    config = _cfg(tmp_path, eval_steps=10, dataset=DatasetConfig(repo_id="a/b", eval_episodes=[0, 1]))
    config.validate()


def test_dataset_config_rejects_ambiguous_split() -> None:
    with pytest.raises(ValueError, match="either eval_split"):
        DatasetConfig(repo_id="a/b", eval_split=0.2, eval_episodes=[0])
    with pytest.raises(ValueError, match="overlap"):
        DatasetConfig(repo_id="a/b", episodes=[1, 2], eval_episodes=[2])


class _FakeMeta:
    def __init__(self, count: int) -> None:
        self.episodes = {"tasks": [["task"]] * count}
        self.camera_keys = []
        self.depth_keys = []
        self.stats = {}


class _FakeDataset:
    def __init__(self, episodes=None, num_frames: int = 40) -> None:
        self.episodes = episodes
        self.num_frames = num_frames
        self.num_episodes = len(episodes) if episodes else 4
        self.meta = _FakeMeta(4)


class _FakeBuiltDataset(_FakeDataset):
    def __init__(self, repo_id, root=None, episodes=None, **kwargs) -> None:
        super().__init__(episodes=episodes, num_frames=len(episodes or []) * 10)


def test_explicit_eval_episodes_drive_the_split(monkeypatch, tmp_path: pathlib.Path) -> None:
    full = _FakeDataset(episodes=None, num_frames=40)
    full.episodes = [0, 1, 2, 3]
    monkeypatch.setattr(dataset_factory, "make_dataset", lambda cfg: full)
    monkeypatch.setattr(dataset_factory, "LeRobotDataset", _FakeBuiltDataset)
    monkeypatch.setattr(dataset_factory, "resolve_delta_timestamps", lambda *args, **kwargs: {})
    config = SimpleNamespace(
        dataset=DatasetConfig(repo_id="a/b", eval_episodes=[0]),
        rename_map={},
        tolerance_s=1e-4,
        trainable_config=object(),
    )
    train, evaluation = dataset_factory.make_train_eval_datasets(config)
    assert train.episodes == [1, 2, 3]
    assert evaluation.episodes == [0]

    config.dataset = DatasetConfig(repo_id="a/b", eval_episodes=[9])
    with pytest.raises(ValueError, match="not part of the loaded dataset"):
        dataset_factory.make_train_eval_datasets(config)


def test_grad_norm_is_pre_clip_and_does_not_modify_gradients() -> None:
    model = torch.nn.Sequential(torch.nn.Linear(4, 4), torch.nn.Linear(4, 2))
    (model(torch.ones(2, 4)) ** 2).mean().backward()
    before = [parameter.grad.detach().clone() for parameter in model.parameters()]
    value = lelab_monitor.grad_norm(model.parameters())
    after = [parameter.grad for parameter in model.parameters()]
    assert value is not None and value > 0
    for original, current in zip(before, after, strict=True):
        assert torch.equal(original, current)


def test_weight_norm_deduplicates_shared_parameters() -> None:
    shared = torch.nn.Linear(3, 3)
    model = torch.nn.Sequential(shared, shared)
    norms = lelab_monitor.parameter_norms(model)
    expected = sum(
        float(parameter.detach().float().pow(2).sum()) for parameter in set(model.parameters())
    ) ** 0.5
    assert norms["weight_norm"] == pytest.approx(expected)
    # Shared parameters are counted once: 3x3 weight + 3 bias.
    assert norms["total_parameters"] == 12


def test_isolated_rng_does_not_advance_training_stream() -> None:
    torch.manual_seed(123)
    expected = torch.randn(3)
    torch.manual_seed(123)
    with lelab_monitor.isolated_rng():
        torch.randn(100)
    assert torch.equal(torch.randn(3), expected)


def test_action_group_validation() -> None:
    with pytest.raises(ValueError, match="more than one group"):
        lelab_monitor.parse_action_groups('{"a": [0], "b": [0]}')
    with pytest.raises(ValueError, match="outside"):
        lelab_monitor.configure_action_groups(
            torch.nn.Linear(1, 1), 2, {"a": [2]}
        )
    groups = lelab_monitor.configure_action_groups(
        torch.nn.Linear(1, 1), 12, {"joints": [0, 1], "grippers": [5, 11]}
    )
    assert groups == {"joints": [0, 1], "grippers": [5, 11]}


def test_emit_sanitizes_non_finite_metrics(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setenv("LELAB_RUN_ID", "run")
    monkeypatch.setenv("LELAB_ATTEMPT_ID", "a1")
    events_file = tmp_path / "worker-events.jsonl"
    monkeypatch.setenv("LELAB_EVENTS_PATH", str(events_file))
    lelab_monitor.emit(
        "train",
        {"step": 2, "loss": float("nan"), "lr": 1e-4, "grad_norm": float("inf")},
    )
    output = capsys.readouterr().out
    event = lelab_monitor.parse_event_line(output)
    assert event is not None
    assert event["loss"] is None and event["grad_norm"] is None
    assert event["lr"] == pytest.approx(1e-4)
    assert set(event["non_finite"]) == {"loss", "grad_norm"}
    assert "NaN" not in output and "Infinity" not in output
    assert "NaN" not in events_file.read_text(encoding="utf-8")


def test_grouped_loss_is_an_exact_decomposition() -> None:
    losses = torch.tensor(
        [
            [[1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0]],
            [[2.0, 4.0, 6.0, 8.0], [10.0, 12.0, 14.0, 16.0]],
        ]
    )
    groups = {"joints": [0, 1, 2], "gripper": [3]}
    means = lelab_monitor.grouped_loss_means(losses, None, groups)
    total = float(losses.mean())
    reconstructed = (means["joints"] * 3 + means["gripper"] * 1) / 4
    assert reconstructed == pytest.approx(total)
    assert means["gripper"] == pytest.approx(losses[..., 3].mean().item())

    pad = torch.tensor([[False, True], [False, False]])
    masked = lelab_monitor.grouped_loss_means(losses, pad, groups)
    expected = float((losses * (~pad).unsqueeze(-1)).sum() / (3 * 4))
    assert (masked["joints"] * 3 + masked["gripper"]) / 4 == pytest.approx(expected)


def test_pause_manifest_round_trip() -> None:
    with tempfile.TemporaryDirectory() as directory:
        checkpoint = pathlib.Path(directory) / "000004"
        files = {
            "pretrained_model/model.safetensors": b"weights",
            "pretrained_model/config.json": b"{}",
            "pretrained_model/train_config.json": b'{"steps": 10}',
            "training_state/training_step.json": b'{"step": 4}',
            "training_state/rng_state.safetensors": b"rng",
            "training_state/optimizer_state.safetensors": b"optimizer",
            "training_state/scheduler_state.json": b"{}",
        }
        for name, payload in files.items():
            path = checkpoint / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        manifest = lelab_monitor.build_checkpoint_manifest(
            checkpoint, step=4, cfg=SimpleNamespace(steps=10)
        )
        assert manifest["complete"] is True
        lelab_monitor.write_json_atomic(checkpoint / "lelab_pause.json", manifest)
        ok, reason = lelab_monitor.manifest_matches(checkpoint, manifest)
        assert ok, reason
        (checkpoint / "training_state" / "rng_state.safetensors").write_bytes(b"rnX")
        ok, reason = lelab_monitor.manifest_matches(checkpoint, manifest)
        assert not ok and "sha256 mismatch" in (reason or "")
