"""Model-verification harness with honest stage statuses and atomic reports.

Why this exists
---------------
The first FastWAM verifier (still kept under
`docs/evidence/lerobot-070/20261116T171907Z-release-model-readiness/`) marked a
stage `ok=true` whenever its callable returned without raising. As a result a
save/reload comparison with `max_abs_diff=1.6015625` and
`identical_within_1e_4=false` was reported as a success, and the process always
exited 0. It also compared `select_action` outputs taken from *different queue
positions* (the pre-save call reused a non-empty action queue, the post-reload
call started from a fresh queue), so that diff did not even measure
serialization.

This module makes the acceptance explicit:

* every stage declares what "passed" means; returning a payload is not enough;
* statuses are `passed` / `failed` / `not_run` / `blocked`; `blocked` never
  counts as `passed`;
* a required stage that is not `passed` makes the run exit non-zero;
* the report is written atomically (tmp + ``os.replace``) even when a stage
  fails or the process is interrupted;
* numeric comparisons use pre-declared tolerances — see ``CHUNK_ATOL`` /
  ``CHUNK_RTOL`` below — and record max/mean diff plus bitwise equality so a
  future reader can judge the margin instead of trusting a boolean.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PASSED = "passed"
FAILED = "failed"
NOT_RUN = "not_run"
BLOCKED = "blocked"

# Pre-declared save/reload tolerance for bfloat16 weights on one GPU.
#
# bfloat16 has 8 explicit mantissa bits: the spacing between representable
# numbers at magnitude 1.0 is 2**-8 = 3.9e-3. Two computations that use the
# same weights, the same inputs and the same explicit noise can still differ
# by a few ULPs if a kernel selects a different accumulation order after the
# model object is rebuilt. 2e-2 is ~5 ULPs at |a| <= 1 — loose enough for that
# reordering, tight enough that a genuine defect fails loudly: the queue-
# position bug in the old harness produced 1.60, i.e. ~80x the tolerance.
CHUNK_ATOL = 2e-2
CHUNK_RTOL = 2e-2


class GateFailure(AssertionError):
    """A stage ran but its acceptance condition is false."""


def finite_gate(tag: str, tensor: Any) -> dict[str, Any]:
    """Fail unless every element is finite; return shape/dtype metrics."""
    if tensor is None:
        raise GateFailure(f"{tag}: expected a tensor, got None")
    if not hasattr(tensor, "isfinite"):
        raise GateFailure(f"{tag}: expected a tensor-like object, got {type(tensor).__name__}")
    total = int(tensor.numel())
    n_finite = int(tensor.isfinite().sum())
    if n_finite != total:
        raise GateFailure(f"{tag}: {total - n_finite}/{total} non-finite values")
    return {
        "shape": list(tensor.shape),
        "dtype": str(tensor.dtype),
        "numel": total,
    }


def compare(
    tag: str,
    expected: Any,
    actual: Any,
    *,
    atol: float = CHUNK_ATOL,
    rtol: float = CHUNK_RTOL,
) -> dict[str, Any]:
    """Fail unless ``actual`` matches ``expected`` within the declared tolerance.

    Raises :class:`GateFailure` on shape mismatch, non-finite values, or a diff
    above ``atol + rtol * max|expected|``. Always returns the measured margins
    (max/mean diff, bitwise equality) so reports carry evidence, not just a
    boolean.
    """
    shape_e, shape_a = list(getattr(expected, "shape", ())), list(getattr(actual, "shape", ()))
    if shape_e != shape_a:
        raise GateFailure(f"{tag}: shape mismatch {shape_e} vs {shape_a}")
    if not bool(actual.isfinite().all()):
        raise GateFailure(f"{tag}: non-finite values in the compared tensor")
    diff = (expected.float() - actual.float()).abs()
    max_diff = float(diff.max()) if diff.numel() else 0.0
    mean_diff = float(diff.mean()) if diff.numel() else 0.0
    peak = float(expected.float().abs().max()) if expected.numel() else 0.0
    tol = float(atol + rtol * peak)
    bitwise = bool((expected == actual).all())
    metrics = {
        "max_abs_diff": max_diff,
        "mean_abs_diff": mean_diff,
        "reference_peak": peak,
        "tolerance": tol,
        "bitwise_identical": bitwise,
    }
    if not bitwise and max_diff > tol:
        raise GateFailure(f"{tag}: max_abs_diff {max_diff:.6g} > tolerance {tol:.6g}")
    return metrics


@dataclass
class Stage:
    """One verification stage.

    ``fn`` receives a shared context dict and returns a payload. It may set:
      * ``gate`` (bool, default True) — the stage's acceptance result;
      * ``blocked`` (str) — a resource/permission blocker (status ``blocked``).
    Any exception marks the stage ``failed``.
    """

    name: str
    fn: Callable[[dict], dict | None]
    requires: tuple[str, ...] = ()
    required: bool = True
    note: str = ""


@dataclass
class Verifier:
    stages: list[Stage]
    report_path: Path
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- reporting ---------------------------------------------------------
    def _write_atomic(self, report: dict[str, Any]) -> None:
        target = Path(self.report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)

    # -- execution ---------------------------------------------------------
    def _expand(self, selected: Iterable[str] | None) -> set[str]:
        names = {s.name for s in self.stages}
        if selected is None:
            return names
        chosen: set[str] = set()
        work = list(selected)
        by_name = {s.name: s for s in self.stages}
        while work:
            name = work.pop()
            if name not in by_name:
                raise KeyError(f"unknown stage {name!r}; known: {sorted(names)}")
            if name in chosen:
                continue
            chosen.add(name)
            work.extend(by_name[name].requires)
        return chosen

    def run(self, selected: Iterable[str] | None = None) -> int:
        chosen = self._expand(selected)
        results: dict[str, dict[str, Any]] = {}
        ctx: dict[str, Any] = {}
        report: dict[str, Any] = {
            "metadata": dict(self.metadata),
            "stages": results,
            "selected": sorted(chosen),
        }
        exit_code = 1
        try:
            for stage in self.stages:
                if stage.name not in chosen:
                    results[stage.name] = {
                        "status": NOT_RUN,
                        "reason": "not selected",
                        "required": stage.required,
                        "seconds": 0.0,
                    }
                    continue
                unmet = {
                    dep: results.get(dep, {}).get("status") for dep in stage.requires
                    if results.get(dep, {}).get("status") != PASSED
                }
                if unmet:
                    blocked = any(v == BLOCKED for v in unmet.values())
                    results[stage.name] = {
                        "status": BLOCKED if blocked else NOT_RUN,
                        "reason": f"prerequisite not passed: {unmet}",
                        "required": stage.required,
                        "seconds": 0.0,
                    }
                    print(f"[{stage.name}] {results[stage.name]['status']} ({results[stage.name]['reason']})",
                          flush=True)
                    continue

                started = time.time()
                entry: dict[str, Any] = {"required": stage.required, "note": stage.note}
                try:
                    payload = stage.fn(ctx) or {}
                    if not isinstance(payload, dict):
                        raise TypeError(f"stage returned {type(payload).__name__}, expected dict")
                    gate = bool(payload.pop("gate", True))
                    blocked_msg = payload.pop("blocked", None)
                    failure_reason = payload.pop("failure_reason", None)
                    if blocked_msg:
                        entry["status"] = BLOCKED
                        entry["blocked_reason"] = str(blocked_msg)
                    elif gate:
                        entry["status"] = PASSED
                    else:
                        entry["status"] = FAILED
                        entry["failure_reason"] = failure_reason or "gate returned False"
                    entry["data"] = payload
                except GateFailure as exc:
                    entry["status"] = FAILED
                    entry["failure_kind"] = "gate"
                    entry["failure_reason"] = str(exc)
                except Exception as exc:  # noqa: BLE001 - every failure must be recorded
                    entry["status"] = FAILED
                    entry["failure_kind"] = type(exc).__name__
                    entry["failure_reason"] = str(exc)
                entry["seconds"] = round(time.time() - started, 2)
                results[stage.name] = entry
                print(f"[{stage.name}] {entry['status']}"
                      f"{' ' + entry.get('failure_reason', '') if entry.get('failure_reason') else ''}"
                      f" ({entry['seconds']}s)", flush=True)

            required_failed = [
                name for name, res in results.items()
                if res.get("required") and name in chosen and res.get("status") != PASSED
            ]
            exit_code = 1 if required_failed else 0
            report["required_not_passed"] = required_failed
            report["exit_code"] = exit_code
            return exit_code
        finally:
            # Atomic, unconditional: a crash mid-run still leaves a readable report.
            report.setdefault("metadata", dict(self.metadata))
            report["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._write_atomic(report)
