"""Self-tests for the verification harness (VFY-01/VFY-02).

These run on CPU with tiny fake policies. They exist because the first FastWAM
verifier reported `ok=true` for a save/reload stage whose comparison had failed
(`max_abs_diff=1.6015625`) and compared actions from different `select_action`
queue positions. The tests below lock in:

* status/exit-code semantics: false/NaN/mismatch/exception must be `failed`,
  blockers must be `blocked`, and a required non-passed stage exits non-zero;
* atomic report writing on failure;
* the queue-position failure mode, and the fixed comparison (same reset, same
  explicit noise, full chunk) that is insensitive to it.
"""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch

from harness import (
    BLOCKED,
    CHUNK_ATOL,
    CHUNK_RTOL,
    FAILED,
    NOT_RUN,
    PASSED,
    GateFailure,
    Stage,
    Verifier,
    compare,
    finite_gate,
)


class TinyQueuePolicy:
    """Minimal stand-in with the real queue semantics of FastWAMPolicy.

    `select_action` pops from `_action_queue` when non-empty and only predicts
    when the queue is empty — exactly the behaviour that made the old
    save/reload comparison read a different chunk element before and after.
    """

    def __init__(self, dim: int = 3, horizon: int = 4, n_action_steps: int = 2, drift: float = 0.0):
        self.config = SimpleNamespace(
            n_action_steps=n_action_steps, action_horizon=horizon, action_dim=dim
        )
        g = torch.Generator().manual_seed(1234)
        self.weights = torch.randn(horizon, dim, generator=g) * 0.1
        self.drift = drift
        self._action_queue: deque[torch.Tensor] = deque(maxlen=n_action_steps)

    def reset(self) -> None:
        self._action_queue = deque(maxlen=self.config.n_action_steps)

    def predict_action_chunk(self, batch: dict, *, noise: torch.Tensor | None = None) -> torch.Tensor:
        obs = batch["obs"]  # [B, dim]
        chunk = obs.unsqueeze(1) + self.weights.unsqueeze(0)  # [B, horizon, dim]
        if noise is not None:
            chunk = chunk + 0.0 * noise  # accepted, deterministic core
        return chunk + self.drift

    def select_action(self, batch: dict, *, noise: torch.Tensor | None = None) -> torch.Tensor:
        if len(self._action_queue) == 0:
            chunk = self.predict_action_chunk(batch, noise=noise)[:, : self.config.n_action_steps]
            self._action_queue.extend(chunk.transpose(0, 1))
        return self._action_queue.popleft()

    def save(self) -> dict:
        return {"weights": self.weights.clone()}

    def load_copy(self, state: dict) -> "TinyQueuePolicy":
        clone = TinyQueuePolicy(
            dim=self.config.action_dim,
            horizon=self.config.action_horizon,
            n_action_steps=self.config.n_action_steps,
        )
        clone.weights = state["weights"].clone()
        return clone


def _batch(seed: int = 7) -> dict:
    g = torch.Generator().manual_seed(seed)
    return {"obs": torch.randn(1, 3, generator=g)}


def _noise(seed: int = 11) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    return torch.randn(1, 4, 3, generator=g)


def _run(tmp_path: Path, stages: list[Stage], selected=None) -> tuple[int, dict]:
    verifier = Verifier(stages=stages, report_path=tmp_path / "report.json",
                        metadata={"test": "selfcheck"})
    code = verifier.run(selected)
    report = json.loads((tmp_path / "report.json").read_text())
    return code, report


# ---------------------------------------------------------------------------
# VFY-01: status and exit-code semantics
# ---------------------------------------------------------------------------


def test_nan_gate_marks_stage_failed_and_exits_nonzero(tmp_path: Path) -> None:
    def nan_stage(_ctx):
        out = torch.tensor([1.0, float("nan")])
        metrics = finite_gate("action", out)  # must raise
        return metrics

    code, report = _run(tmp_path, [Stage("nan", nan_stage)])
    assert code == 1
    assert report["stages"]["nan"]["status"] == FAILED
    assert "non-finite" in report["stages"]["nan"]["failure_reason"]
    assert report["exit_code"] == 1
    assert report["required_not_passed"] == ["nan"]


def test_false_gate_marks_failed_not_ok(tmp_path: Path) -> None:
    """A stage returning a payload is not enough; `gate=False` must fail it."""

    def gate_stage(_ctx):
        return {"gate": False, "failure_reason": "close check failed", "max_abs_diff": 1.6}

    code, report = _run(tmp_path, [Stage("close", gate_stage)])
    assert code == 1
    assert report["stages"]["close"]["status"] == FAILED
    assert report["stages"]["close"]["failure_reason"] == "close check failed"


def test_mismatch_beyond_tolerance_fails(tmp_path: Path) -> None:
    def mismatch_stage(_ctx):
        before = torch.zeros(1, 4, 3)
        after = torch.full((1, 4, 3), 1.6015625)
        return compare("chunk", before, after)

    code, report = _run(tmp_path, [Stage("mismatch", mismatch_stage)])
    assert code == 1
    assert report["stages"]["mismatch"]["status"] == FAILED
    assert "tolerance" in report["stages"]["mismatch"]["failure_reason"]


def test_exception_marks_failed(tmp_path: Path) -> None:
    def boom(_ctx):
        raise RuntimeError("CUDA out of memory")

    code, report = _run(tmp_path, [Stage("boom", boom)])
    assert code == 1
    entry = report["stages"]["boom"]
    assert entry["status"] == FAILED
    assert entry["failure_kind"] == "RuntimeError"


def test_dependency_failure_makes_dependent_not_run(tmp_path: Path) -> None:
    def fail(_ctx):
        raise AssertionError("no weights")

    def dependent(_ctx):
        raise AssertionError("must not run")

    code, report = _run(
        tmp_path,
        [Stage("a", fail), Stage("b", dependent, requires=("a",))],
    )
    assert code == 1
    assert report["stages"]["a"]["status"] == FAILED
    assert report["stages"]["b"]["status"] == NOT_RUN
    assert "prerequisite" in report["stages"]["b"]["reason"]


def test_blocked_dependency_propagates_blocked(tmp_path: Path) -> None:
    def blocked(_ctx):
        return {"blocked": "no GPU"}

    def dependent(_ctx):
        raise AssertionError("must not run")

    code, report = _run(
        tmp_path,
        [Stage("hw", blocked, required=False), Stage("model", dependent, requires=("hw",))],
    )
    assert report["stages"]["hw"]["status"] == BLOCKED
    assert report["stages"]["model"]["status"] == BLOCKED
    # the only required stage was blocked -> non-zero
    assert code == 1


def test_blocked_optional_stage_keeps_exit_zero(tmp_path: Path) -> None:
    def ok(_ctx):
        return {"gate": True}

    def oom(_ctx):
        return {"blocked": "OOM at backward"}

    code, report = _run(
        tmp_path,
        [Stage("load", ok), Stage("train", oom, requires=("load",), required=False)],
    )
    assert code == 0
    assert report["stages"]["load"]["status"] == PASSED
    assert report["stages"]["train"]["status"] == BLOCKED
    assert report["stages"]["train"]["blocked_reason"] == "OOM at backward"


def test_required_blocked_stage_exits_nonzero(tmp_path: Path) -> None:
    code, report = _run(tmp_path, [Stage("load", lambda _c: {"blocked": "no weights"})])
    assert code == 1
    assert report["stages"]["load"]["status"] == BLOCKED


def test_stage_selection_pulls_dependencies(tmp_path: Path) -> None:
    calls: list[str] = []

    def make(name):
        def fn(_ctx):
            calls.append(name)
            return {"gate": True}
        return fn

    code, report = _run(
        tmp_path,
        [
            Stage("load", make("load")),
            Stage("chunk", make("chunk"), requires=("load",)),
            Stage("other", make("other")),
        ],
        selected=["chunk"],
    )
    assert code == 0
    assert calls == ["load", "chunk"]
    assert report["stages"]["other"]["status"] == NOT_RUN


def test_report_written_atomically_on_failure(tmp_path: Path) -> None:
    def boom(_ctx):
        raise RuntimeError("kaboom")

    _run(tmp_path, [Stage("boom", boom)])
    report_path = tmp_path / "report.json"
    assert report_path.is_file()
    assert not report_path.with_suffix(".json.tmp").exists()
    payload = json.loads(report_path.read_text())
    assert payload["stages"]["boom"]["status"] == FAILED


def test_compare_reports_margins_and_bitwise_flag() -> None:
    a = torch.ones(1, 2, 3)
    b = a.clone()
    metrics = compare("same", a, b)
    assert metrics["bitwise_identical"] is True
    assert metrics["max_abs_diff"] == 0.0


def test_compare_rejects_shape_mismatch() -> None:
    with pytest.raises(GateFailure, match="shape mismatch"):
        compare("shape", torch.zeros(1, 2), torch.zeros(1, 3))


# ---------------------------------------------------------------------------
# VFY-02: the old comparison read the wrong queue position
# ---------------------------------------------------------------------------


def test_old_queue_position_comparison_is_detected_as_mismatch() -> None:
    """Reproduce the old harness: pre-save read after a prior select_action,
    post-reload read from a fresh queue. The diff must exceed tolerance."""
    policy = TinyQueuePolicy()
    batch, noise = _batch(), _noise()

    policy.reset()
    _ = policy.select_action(batch, noise=noise)   # fills queue, pops element 0
    old_before = policy.select_action(batch, noise=noise)  # pops element 1 (no reset!)

    reloaded = policy.load_copy(policy.save())
    reloaded.reset()  # fresh queue -> element 0
    old_after = reloaded.select_action(batch, noise=noise)

    diff = float((old_before - old_after).abs().max())
    assert diff > CHUNK_ATOL + CHUNK_RTOL, (
        f"queue positions must differ observably; diff={diff}"
    )
    with pytest.raises(GateFailure):
        compare("old_harness", old_before, old_after)


def test_fixed_comparison_uses_same_reset_and_explicit_noise() -> None:
    """Reset both sides, compare full `predict_action_chunk` with fixed noise."""
    policy = TinyQueuePolicy()
    batch, noise = _batch(), _noise()

    policy.reset()
    before = policy.predict_action_chunk(batch, noise=noise)

    reloaded = policy.load_copy(policy.save())
    reloaded.reset()
    after = reloaded.predict_action_chunk(batch, noise=noise)

    metrics = compare("chunk", before, after)
    assert metrics["bitwise_identical"] is True
    assert metrics["max_abs_diff"] == 0.0


def test_fixed_comparison_still_catches_real_drift() -> None:
    """A saved copy that differs must fail; tolerance must not hide it."""
    policy = TinyQueuePolicy()
    batch, noise = _batch(), _noise()

    policy.reset()
    before = policy.predict_action_chunk(batch, noise=noise)

    corrupted = policy.load_copy(policy.save())
    corrupted.drift = 0.5
    corrupted.reset()
    after = corrupted.predict_action_chunk(batch, noise=noise)

    with pytest.raises(GateFailure):
        compare("drift", before, after)


def test_queue_consumption_matches_the_predicted_chunk() -> None:
    """select_action must hand out chunk rows in order after one reset."""
    policy = TinyQueuePolicy(n_action_steps=2)
    batch, noise = _batch(), _noise()

    policy.reset()
    chunk = policy.predict_action_chunk(batch, noise=noise)[:, : policy.config.n_action_steps]
    policy.reset()
    served = [policy.select_action(batch, noise=noise) for _ in range(policy.config.n_action_steps)]

    # select_action returns [B, dim]; the chunk row is [dim].
    assert torch.equal(served[0][0], chunk[0, 0])
    assert torch.equal(served[1][0], chunk[0, 1])


def test_queue_exhaustion_repredicts_and_keeps_ordering() -> None:
    policy = TinyQueuePolicy(n_action_steps=2)
    batch, noise = _batch(), _noise()
    policy.reset()
    first = [policy.select_action(batch, noise=noise) for _ in range(2)]
    second = [policy.select_action(batch, noise=noise) for _ in range(2)]
    # same deterministic core -> the second chunk equals the first
    assert torch.equal(first[0], second[0])
    assert torch.equal(first[1], second[1])
    assert not torch.equal(first[0], first[1])
