"""K4 — isolated FastWAM training-memory profile.

One model, one process, four explicit phases (budget → forward → backward →
one optimizer step). Every phase appends an atomic JSON record *before* the next
one starts, so an OOM still leaves evidence of which phase died and how much had
been allocated at that point.

The static budget is computed from the tensors that actually carry
``requires_grad`` in this process — not from a hardcoded 6B or from the file
size — because ``freeze_video_expert`` changes the trainable set by ~5B and the
checkpoint does not set it.

Usage:
    python stage5_profile.py --policy-path lerobot/fastwam_base \
        --freeze-video-expert --out fastwam-stage5-freeze.json
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import torch


def _alloc_gb() -> float:
    return round(torch.cuda.memory_allocated() / 1e9, 3)


def _reserved_gb() -> float:
    return round(torch.cuda.memory_reserved() / 1e9, 3)


def _peak_gb() -> float:
    return round(torch.cuda.max_memory_allocated() / 1e9, 3)


def _meminfo() -> tuple[float, float]:
    free_b, total_b = torch.cuda.mem_get_info()
    return round(free_b / 1e9, 2), round(total_b / 1e9, 2)


def _atomic_write(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _component_breakdown(policy) -> dict:
    """numel / trainable / dtype / device per registered sub-module."""
    out = {}
    for name, mod in policy.named_children():
        params = list(mod.parameters())
        if not params:
            continue
        out[name] = {
            "numel_M": round(sum(p.numel() for p in params) / 1e6, 1),
            "trainable_M": round(sum(p.numel() for p in params if p.requires_grad) / 1e6, 1),
            "dtypes": sorted({str(p.dtype).replace("torch.", "") for p in params}),
            "devices": sorted({str(p.device) for p in params}),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy-path", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--freeze-video-expert", action="store_true")
    ap.add_argument("--steps", type=int, default=1)
    ap.add_argument("--text-encoder-device", default="cpu")
    ap.add_argument("--no-gradient-checkpointing", action="store_true")
    ap.add_argument("--budget-only", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="run the phases even when the static state alone exceeds the device")
    args = ap.parse_args()

    from lerobot.policies.fastwam import FastWAMConfig, FastWAMPolicy

    out_path = Path(args.out)
    report = {
        "kind": "fastwam-stage5-profile",
        "case": "VFY-04 / K4",
        "policy_path": args.policy_path,
        "host": os.uname().nodename,
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(0),
        "freeze_video_expert": args.freeze_video_expert,
        "gradient_checkpointing": not args.no_gradient_checkpointing,
        "text_encoder_device": args.text_encoder_device,
        "phases": [],
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    def phase(name: str, **extra) -> None:
        free, total = _meminfo()
        report["phases"].append({
            "phase": name,
            "t": round(time.time() - t0, 1),
            "allocated_GB": _alloc_gb(),
            "reserved_GB": _reserved_gb(),
            "peak_allocated_GB": _peak_gb(),
            "free_GB": free,
            "total_GB": total,
            **extra,
        })
        _atomic_write(out_path, report)

    t0 = time.time()
    phase("start")

    cfg = FastWAMConfig.from_pretrained(args.policy_path)
    cfg.dtype = torch.bfloat16
    cfg.device = "cuda"
    cfg.text_encoder_device = args.text_encoder_device
    cfg.use_gradient_checkpointing = not args.no_gradient_checkpointing
    cfg.freeze_video_expert = args.freeze_video_expert
    policy = FastWAMPolicy.from_pretrained(args.policy_path, config=cfg)
    phase("loaded")

    params = list(policy.parameters())
    trainable = [p for p in params if p.requires_grad]
    n_all = sum(p.numel() for p in params)
    n_train = sum(p.numel() for p in trainable)
    dtype_bytes = {str(p.dtype): p.element_size() for p in params}
    weight_bytes = sum(p.numel() * p.element_size() for p in params)
    train_bytes = sum(p.numel() * p.element_size() for p in trainable)
    grad_dtype_bytes = max((p.element_size() for p in trainable), default=4)
    # AdamW keeps two fp32 moments per trained parameter (torch allocates them in
    # the parameter dtype only for fused/bf16-state variants, which this recipe
    # does not request), so budget the worst case: 8 bytes per trainable element.
    budget = {
        "params_all_M": round(n_all / 1e6, 1),
        "params_trainable_M": round(n_train / 1e6, 1),
        "weights_all_GB": round(weight_bytes / 1e9, 2),
        "trainable_weights_GB": round(train_bytes / 1e9, 2),
        "grad_GB": round(n_train * grad_dtype_bytes / 1e9, 2),
        "adam_moments_GB_fp32": round(n_train * 8 / 1e9, 2),
        "static_lower_bound_GB": round(
            (train_bytes + n_train * grad_dtype_bytes + n_train * 8) / 1e9, 2
        ),
        "param_dtypes": dtype_bytes,
        "components": _component_breakdown(policy),
    }
    free, total = _meminfo()
    budget["gpu_free_GB_after_load"] = free
    budget["gpu_total_GB"] = total
    # The caching allocator keeps freed blocks reserved, so mem_get_info "free"
    # is not what torch can still hand out. Compare the static state with the
    # device total and let the phases below prove the activation part; reserving
    # a block only to release it again is not a reason to skip the run.
    budget["reserved_GB_after_load"] = _reserved_gb()
    budget["allocator_free_GB"] = round(total - _reserved_gb(), 2)
    budget["fits_static_lower_bound"] = budget["static_lower_bound_GB"] < total * 0.98
    report["budget"] = budget
    phase("budgeted", static_lower_bound_GB=budget["static_lower_bound_GB"])

    if args.budget_only or (not budget["fits_static_lower_bound"] and not args.force):
        report["verdict"] = (
            "blocked: the training state alone exceeds this GPU"
            if not budget["fits_static_lower_bound"]
            else "budget only (short update not requested)"
        )
        report["exit"] = "blocked" if not budget["fits_static_lower_bound"] else "budget_only"
        _atomic_write(out_path, report)
        print(json.dumps({k: report[k] for k in ("verdict", "exit")}, indent=2))
        return 0 if budget["fits_static_lower_bound"] else 0

    policy.to("cuda")
    policy.train()
    for mod in policy.modules():
        if hasattr(mod, "gradient_checkpointing_enable") and report["gradient_checkpointing"]:
            mod.gradient_checkpointing_enable()
    torch.cuda.reset_peak_memory_stats()
    phase("train_mode")

    opt = torch.optim.AdamW([p for p in policy.parameters() if p.requires_grad], lr=1e-5)
    horizon = int(getattr(cfg, "action_horizon", 1))
    a_dim = int(cfg.output_features["action"].shape[0])
    s_dim = int(cfg.input_features["observation.state"].shape[0])
    g = torch.Generator().manual_seed(11)
    batch = {
        "observation.state": torch.randn(1, s_dim, generator=g).cuda(),
        "task": ["carry the object forward"],
        "action": torch.randn(1, horizon, a_dim, generator=g).cuda(),
    }
    for name, feat in cfg.input_features.items():
        if name.startswith("observation.images"):
            h, w = feat.shape[1], feat.shape[2]
            batch[name] = torch.randint(0, 255, (1, 3, h, w), generator=g, dtype=torch.uint8).cuda()
    report["batch_shapes"] = {
        k: (list(v.shape) if torch.is_tensor(v) else str(v)) for k, v in batch.items()
    }

    step_name = "forward"
    try:
        for i in range(args.steps):
            opt.zero_grad(set_to_none=True)
            step_name = f"forward[{i}]"
            out = policy.forward(batch)
            loss = out["loss"] if isinstance(out, dict) else (
                next(t for t in out if torch.is_tensor(t) and t.requires_grad) if isinstance(out, tuple) else out
            )
            phase(step_name, loss=float(loss) if torch.is_tensor(loss) else None)
            step_name = f"backward[{i}]"
            loss.backward()
            phase(step_name)
            step_name = f"clip[{i}]"
            gnorm = torch.nn.utils.clip_grad_norm_(
                [p for p in policy.parameters() if p.requires_grad], 1e9
            )
            phase(step_name, grad_norm=float(gnorm))
            step_name = f"optimizer_step[{i}]"
            opt.step()
            phase(step_name)
    except torch.cuda.OutOfMemoryError as exc:
        report["verdict"] = f"OOM during {step_name}"
        report["oom"] = {
            "phase": step_name,
            "message": str(exc).splitlines()[0][:300],
            "peak_allocated_GB": _peak_gb(),
            "reserved_GB": _reserved_gb(),
        }
        report["exit"] = "oom"
        _atomic_write(out_path, report)
        print(json.dumps(report["oom"], indent=2))
        return 2
    except RuntimeError as exc:
        if "out of memory" in str(exc).lower():
            report["verdict"] = f"OOM during {step_name}"
            report["oom"] = {"phase": step_name, "message": str(exc)[:300],
                             "peak_allocated_GB": _peak_gb()}
            report["exit"] = "oom"
            _atomic_write(out_path, report)
            return 2
        raise

    state = opt.state.get(next(p for p in policy.parameters() if p.requires_grad), {})
    report["optimizer_state_entries"] = len(state)
    report["optimizer_state_finite"] = all(
        bool(torch.isfinite(v).all()) for v in state.values() if torch.is_tensor(v) and v.is_floating_point()
    )
    report["grad_finite"] = all(
        bool(torch.isfinite(p.grad).all()) for p in policy.parameters() if p.grad is not None
    )
    report["verdict"] = f"{args.steps} update(s) completed"
    report["exit"] = "passed"
    phase("done")
    print(json.dumps({"verdict": report["verdict"], "peak_allocated_GB": _peak_gb()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
