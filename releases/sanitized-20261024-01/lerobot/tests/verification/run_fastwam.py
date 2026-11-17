"""Real FastWAM verification on one GPU, stage-selectable and single-load.

Usage (on the GPU host, from the worktree venv):

    python tests/verification/run_fastwam.py --stages 1,2,3
    python tests/verification/run_fastwam.py --stages 4            # deps auto-run
    python tests/verification/run_fastwam.py --stages 6 --allow-training

Stages
  1_integrity   safetensors/LFS checks + component manifest (repo revisions)
  2_load        load once; registered vs frozen component accounting
  3_chunk       reset + predict_action_chunk with explicit noise; finite gate
  4_save_reload recompute before (reset), save, reload, recompute after (reset),
                compare full chunks at the declared tolerance
  5_queue       select_action consumes the same chunk rows in order
  6_backprop    optional: static budget, then one real optimizer update;
                OOM is recorded as `blocked` with the failing sub-step

Exit code: non-zero if any *required* stage is not `passed` (see harness.py).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import torch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from harness import (  # noqa: E402
    CHUNK_ATOL,
    CHUNK_RTOL,
    Stage,
    Verifier,
    compare,
    finite_gate,
)

FASTWAM_REPO = "lerobot/fastwam_base"
WAN_REPO = "Wan-AI/Wan2.2-TI2V-5B-Diffusers"
TOKENIZER_REPO = "google/umt5-xxl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hf_home() -> Path:
    return Path(os.environ.get("HF_HOME") or (Path.home() / ".cache/huggingface"))


def _snapshot_info(repo_id: str, *, allow_download: bool = True) -> dict:
    from huggingface_hub import snapshot_download

    kwargs = {} if allow_download else {"local_files_only": True}
    path = Path(snapshot_download(repo_id, **kwargs))
    commit = path.name
    refs = path.parent.parent / "refs" / "main"
    if refs.is_file():
        commit = refs.read_text().strip()
    files = [p for p in path.rglob("*") if p.is_file()]
    return {
        "repo_id": repo_id,
        "snapshot": str(path),
        "commit": commit,
        "files": len(files),
        "bytes": sum(p.stat().st_size for p in files),
    }


def _cache_dir_for(repo_id: str) -> Path:
    return _hf_home() / "hub" / ("models--" + repo_id.replace("/", "--"))


def _load_policy(ckpt: str, *, device: str = "cuda", text_encoder_device: str = "cpu",
                 gradient_checkpointing: bool = True):
    """Load with the checkpoint's declared dtype.

    The base checkpoint declares bfloat16. fp32 exceeds a 32 GB card; bf16 is
    the declared precision and halves both weights and activations. UMT5-XXL
    (~11 GB) is parked on CPU via the config's ``text_encoder_device``; the
    config object must be passed explicitly because stray kwargs to
    ``from_pretrained`` do not reach the dataclass.
    """
    from lerobot.policies.fastwam import FastWAMConfig, FastWAMPolicy

    cfg = FastWAMConfig.from_pretrained(ckpt)
    cfg.dtype = torch.bfloat16
    cfg.device = device
    cfg.text_encoder_device = text_encoder_device
    cfg.use_gradient_checkpointing = gradient_checkpointing
    return FastWAMPolicy.from_pretrained(ckpt, config=cfg)


def _infer_batch(policy, *, seed: int = 7) -> dict:
    """Deterministic native-space inference batch + the explicit noise.

    Returns (batch, noise). The same objects are reused for before/after so the
    comparison measures serialization, not input noise.
    """
    cfg = policy.config
    state_dim = int(cfg.input_features["observation.state"].shape[0])
    g = torch.Generator().manual_seed(seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    batch = {
        "observation.state": torch.randn(1, state_dim, generator=g).to(device),
        "task": ["carry the object forward"],
    }
    for name, feat in cfg.input_features.items():
        if name.startswith("observation.images"):
            h, w = feat.shape[1], feat.shape[2]
            batch[name] = torch.randint(
                0, 255, (1, 3, h, w), generator=g, dtype=torch.uint8
            ).to(device)
    g_noise = torch.Generator().manual_seed(seed + 101)
    horizon = int(getattr(cfg, "action_horizon", 1))
    noise = torch.randn(1, horizon, int(cfg.output_features["action"].shape[0]),
                        generator=g_noise, dtype=torch.float32)
    return batch, noise


def _object_counts(policy) -> dict:
    """Registered vs frozen accounting.

    `policy.parameters()` is *not* the whole story: the frozen Wan VAE and UMT5
    text encoder may sit outside the registered tree. Report both so the
    "6020.7M parameters" figure is never read as the total artifact size.
    """
    registered = list(policy.parameters())
    registered_ids = {id(p) for p in registered}
    model = policy.model
    components: dict[str, dict] = {}
    for name in ("vae", "text_encoder", "video_expert", "action_expert", "mot", "proprio_encoder"):
        obj = getattr(model, name, None)
        if not isinstance(obj, torch.nn.Module):
            continue
        params = list(obj.parameters())
        if not params:
            continue
        in_reg = sum(1 for p in params if id(p) in registered_ids)
        components[name] = {
            "numel_M": round(sum(p.numel() for p in params) / 1e6, 1),
            "registered_fraction": round(in_reg / len(params), 3),
            "dtypes": sorted({str(p.dtype).split(".")[-1] for p in params}),
            "devices": sorted({str(p.device) for p in params}),
        }
    return {
        "registered_params_M": round(sum(p.numel() for p in registered) / 1e6, 1),
        "trainable_params_M": round(
            sum(p.numel() for p in registered if p.requires_grad) / 1e6, 1),
        "components": components,
    }


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def stage_integrity(ctx: dict) -> dict:
    info = _snapshot_info(FASTWAM_REPO)
    root = Path(info["snapshot"])
    incomplete = [p.name for p in root.rglob("*.incomplete")]
    if incomplete:
        return {"gate": False, "failure_reason": f"incomplete downloads: {incomplete}"}

    import struct

    weights = [p for p in root.rglob("*.safetensors")]
    if not weights:
        return {"gate": False, "failure_reason": "no safetensors weights in snapshot"}
    checked = []
    for path in weights:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(9)
        (header_len,) = struct.unpack("<Q", head[:8])
        ok = head[8:9] == b"{" and header_len < size and size > 1_000_000
        if not ok:
            return {
                "gate": False,
                "failure_reason": (
                    f"{path.name} is not a real safetensors file "
                    f"(header_len={header_len}, byte9={head[8:9]!r}, size={size})"
                ),
            }
        checked.append({"file": path.name, "bytes": size})

    wan = _snapshot_info(WAN_REPO, allow_download=False)
    tokenizer_dir = _cache_dir_for(TOKENIZER_REPO)
    tokenizer_files = sorted(
        p.name for p in tokenizer_dir.rglob("*") if p.is_file() and p.suffix in
        {".json", ".model", ".txt"}
    ) if tokenizer_dir.exists() else []

    ctx["snapshot"] = info["snapshot"]
    ctx["manifest"] = {
        "policy": {"repo": FASTWAM_REPO, "commit": info["commit"],
                   "bytes": info["bytes"], "files": info["files"]},
        "wan_components": {"repo": WAN_REPO, "commit": wan["commit"],
                           "bytes": wan["bytes"], "files": wan["files"]},
        "tokenizer": {"repo": TOKENIZER_REPO, "cache_dir": str(tokenizer_dir),
                      "files": tokenizer_files[:12]},
    }
    return {
        "gate": True,
        "weights": checked,
        "manifest": ctx["manifest"],
    }


def stage_load(ctx: dict) -> dict:
    started = time.time()
    policy = _load_policy(ctx["snapshot"])
    ctx["policy"] = policy
    counts = _object_counts(policy)
    cfg = policy.config
    return {
        "gate": True,
        "load_seconds": round(time.time() - started, 2),
        "device": str(next(policy.parameters()).device),
        "config_dtype": str(cfg.dtype),
        "text_encoder_device": cfg.text_encoder_device,
        "gradient_checkpointing": cfg.use_gradient_checkpointing,
        "native_action_dim": int(cfg.output_features["action"].shape[0]),
        "native_state_dim": int(cfg.input_features["observation.state"].shape[0]),
        "action_horizon": int(getattr(cfg, "action_horizon", 1)),
        "n_action_steps": int(cfg.n_action_steps),
        **counts,
    }


def stage_chunk(ctx: dict) -> dict:
    policy = ctx["policy"]
    batch, noise = _infer_batch(policy)
    policy.reset()
    with torch.inference_mode():
        chunk = policy.predict_action_chunk({k: (v.clone() if torch.is_tensor(v) else v)
                                            for k, v in batch.items()}, noise=noise)
    chunk = chunk.detach().cpu().float()
    shape = finite_gate("chunk", chunk)
    expected = (1, int(getattr(policy.config, "action_horizon", 1)),
                int(policy.config.output_features["action"].shape[0]))
    if tuple(chunk.shape) != expected:
        return {"gate": False, "failure_reason": f"chunk shape {tuple(chunk.shape)} != {expected}"}
    ctx["batch"] = batch
    ctx["noise"] = noise.detach().cpu()
    ctx["chunk"] = chunk
    return {
        "gate": True,
        "chunk": shape,
        "abs_max": float(chunk.abs().max()),
        "sample": [round(float(v), 4) for v in chunk[0, 0, :4]],
    }


def stage_save_reload(ctx: dict) -> dict:
    policy = ctx["policy"]
    batch, noise = ctx["batch"], ctx["noise"].to(ctx["batch"]["observation.state"].device)

    def predict(target) -> torch.Tensor:
        target.reset()
        with torch.inference_mode():
            out = target.predict_action_chunk(
                {k: (v.clone() if torch.is_tensor(v) else v) for k, v in batch.items()},
                noise=noise,
            )
        return out.detach().cpu().float()

    before = predict(policy)
    determinism = compare("same_process_redeterminism", ctx["chunk"], before)

    free_gb = shutil.disk_usage(HERE).free / 1e9
    if free_gb < 15:
        return {"blocked": f"need ~15 GB for a 12 GB checkpoint write; free={free_gb:.1f} GB"}

    with tempfile.TemporaryDirectory(prefix="fastwam-roundtrip-") as tmp:
        policy.save_pretrained(tmp)
        saved = sorted(p.name for p in Path(tmp).rglob("*") if p.is_file())
        postproc = [n for n in saved if "processor" in n]
        if not any(n.endswith("config.json") for n in saved):
            return {"gate": False, "failure_reason": "save_pretrained wrote no config.json"}

        policy.to("cpu")
        torch.cuda.empty_cache()
        reloaded = _load_policy(tmp)
        after = predict(reloaded)
        reloaded_cfg = reloaded.config
        after_dtype = str(reloaded_cfg.dtype)
        after_te = reloaded_cfg.text_encoder_device
        reloaded.to("cpu")
        del reloaded
        torch.cuda.empty_cache()

    metrics = compare("save_reload_chunk", before, after)
    if metrics["max_abs_diff"] > CHUNK_ATOL + CHUNK_RTOL:
        return {"gate": False, "failure_reason": "chunk drift above tolerance", **metrics}
    return {
        "gate": True,
        "determinism": determinism,
        "comparison": metrics,
        "saved_files": saved,
        "saved_processor_files": postproc,
        "reloaded_dtype": after_dtype,
        "reloaded_text_encoder_device": after_te,
    }


def stage_queue(ctx: dict) -> dict:
    policy = ctx["policy"]
    # Stage 4 leaves the policy on CPU after the reload comparison; bring it back
    # so queue consumption runs on the same device as the recorded chunk.
    policy.to("cuda")
    batch, noise = ctx["batch"], ctx["noise"].to(ctx["batch"]["observation.state"].device)
    n = int(policy.config.n_action_steps)
    policy.reset()
    served = []
    with torch.inference_mode():
        for _ in range(n):
            served.append(policy.select_action(
                {k: (v.clone() if torch.is_tensor(v) else v) for k, v in batch.items()},
                noise=noise,
            ).detach().cpu().float())
    chunk = ctx["chunk"]
    for index, action in enumerate(served):
        expected = chunk[0, index].unsqueeze(0)
        try:
            compare(f"queue_row_{index}", expected, action, atol=0.0, rtol=0.0)
        except AssertionError as exc:
            return {"gate": False, "failure_reason": str(exc)}
    return {"gate": True, "rows_checked": n}


def stage_backprop(ctx: dict) -> dict:
    policy = ctx["policy"]
    cfg = policy.config
    trainable = [p for p in policy.parameters() if p.requires_grad]
    numel = sum(p.numel() for p in trainable)
    param_bytes = sum(p.numel() * p.element_size() for p in trainable)
    grad_bytes = param_bytes  # same dtype as the parameter
    adam_bytes = 2 * numel * 4  # exp_avg + exp_avg_sq in fp32
    free_b, total_b = torch.cuda.mem_get_info()
    budget = {
        "trainable_params_M": round(numel / 1e6, 1),
        "weights_GB": round(param_bytes / 1e9, 2),
        "grads_GB": round(grad_bytes / 1e9, 2),
        "adam_state_GB": round(adam_bytes / 1e9, 2),
        "static_lower_bound_GB": round((param_bytes + grad_bytes + adam_bytes) / 1e9, 2),
        "gpu_free_GB": round(free_b / 1e9, 2),
        "gpu_total_GB": round(total_b / 1e9, 2),
        "gradient_checkpointing": bool(cfg.use_gradient_checkpointing),
        "text_encoder_device": cfg.text_encoder_device,
    }
    if budget["static_lower_bound_GB"] >= budget["gpu_free_GB"]:
        return {"blocked": f"static training state alone needs "
                           f"{budget['static_lower_bound_GB']} GB > free "
                           f"{budget['gpu_free_GB']} GB", **budget}

    policy.to("cuda")
    policy.train()
    opt = torch.optim.AdamW(trainable, lr=1e-5)
    horizon = int(getattr(cfg, "action_horizon", 1))
    a_dim = int(cfg.output_features["action"].shape[0])
    state_dim = int(cfg.input_features["observation.state"].shape[0])
    g = torch.Generator().manual_seed(3)
    batch = {
        "observation.state": torch.randn(1, state_dim, generator=g).cuda(),
        "task": ["carry the object forward"],
        "action": torch.randn(1, horizon, a_dim, generator=g).cuda(),
    }
    for name, feat in cfg.input_features.items():
        if name.startswith("observation.images"):
            h, w = feat.shape[1], feat.shape[2]
            batch[name] = torch.randint(0, 255, (1, 3, h, w), generator=g,
                                        dtype=torch.uint8).cuda()

    probe = trainable[0]
    before_slice = probe.detach().flatten()[:8].clone()
    step = "forward"
    try:
        opt.zero_grad(set_to_none=True)
        out = policy.forward(batch)
        if isinstance(out, dict):
            loss = out["loss"]
        elif isinstance(out, tuple):
            loss = next(t for t in out if torch.is_tensor(t) and t.requires_grad)
        else:
            loss = out
        if not bool(torch.isfinite(loss)):
            return {"gate": False, "failure_reason": f"non-finite loss {float(loss)}", **budget}
        step = "backward"
        loss.backward()
        grads = [p.grad for p in trainable if p.grad is not None]
        if not grads:
            return {"gate": False, "failure_reason": "no gradients after backward", **budget}
        grad_finite = all(bool(torch.isfinite(g).all()) for g in grads)
        grad_nonzero = any(bool(g.abs().sum() > 0) for g in grads)
        step = "clip"
        gnorm = torch.nn.utils.clip_grad_norm_(trainable, 1e9)
        step = "optimizer_step"
        opt.step()
    except torch.cuda.OutOfMemoryError as exc:
        peak = torch.cuda.max_memory_allocated() / 1e9
        return {"blocked": f"OOM during {step}; peak allocated {peak:.2f} GB",
                "oom_step": step, "peak_allocated_GB": round(peak, 2), **budget}
    except RuntimeError as exc:
        if "out of memory" in str(exc).lower():
            peak = torch.cuda.max_memory_allocated() / 1e9
            return {"blocked": f"OOM during {step}; peak allocated {peak:.2f} GB",
                    "oom_step": step, "peak_allocated_GB": round(peak, 2), **budget}
        raise

    after_slice = probe.detach().flatten()[:8]
    param_changed = not bool(torch.equal(before_slice, after_slice))
    state = opt.state.get(trainable[0], {})
    state_finite = all(bool(torch.isfinite(v).all()) for v in state.values()
                       if torch.is_tensor(v) and v.is_floating_point())
    gate = bool(
        grad_finite and grad_nonzero and bool(torch.isfinite(gnorm))
        and param_changed and state_finite and len(state) > 0
    )
    return {
        "gate": gate,
        "failure_reason": None if gate else "post-update assertions failed",
        "loss": float(loss),
        "grad_norm": float(gnorm),
        "grad_finite": grad_finite,
        "grad_nonzero": grad_nonzero,
        "param_changed": param_changed,
        "optimizer_state_entries": len(state),
        "optimizer_state_finite": state_finite,
        "peak_allocated_GB": round(torch.cuda.max_memory_allocated() / 1e9, 2),
        **budget,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_verifier(report_path: Path) -> Verifier:
    stages = [
        Stage("1_integrity", stage_integrity),
        Stage("2_load", stage_load, requires=("1_integrity",)),
        Stage("3_chunk", stage_chunk, requires=("2_load",)),
        Stage("4_save_reload", stage_save_reload, requires=("3_chunk",)),
        Stage("5_queue", stage_queue, requires=("3_chunk",)),
        Stage("6_backprop", stage_backprop, requires=("2_load",), required=False,
              note="optional: needs training headroom; OOM is recorded as blocked"),
    ]
    return Verifier(
        stages=stages,
        report_path=report_path,
        metadata={
            "runner": "run_fastwam.py",
            "torch": torch.__version__,
            "cuda_available": torch.cuda.is_available(),
            "device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu",
            "tolerance": {"atol": CHUNK_ATOL, "rtol": CHUNK_RTOL},
            "python": sys.version.split()[0],
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stages", default=None,
                        help="comma-separated stage names or numbers, e.g. 1,2,3 or 4")
    parser.add_argument("--report", default=str(HERE / "fastwam-verification.json"))
    parser.add_argument("--allow-training", action="store_true",
                        help="include the optional backprop stage in the default selection")
    args = parser.parse_args(argv)

    verifier = build_verifier(Path(args.report))
    if args.stages:
        raw = [s.strip() for s in args.stages.split(",") if s.strip()]
        known = {s.name for s in verifier.stages}
        selected = []
        for token in raw:
            match = [name for name in known if name == token or name.startswith(token + "_")]
            if not match:
                raise SystemExit(f"unknown stage {token!r}; known: {sorted(known)}")
            selected.extend(match)
    else:
        selected = None
    code = verifier.run(selected)
    print(f"exit_code={code} report={args.report}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
