"""FineART-VLA real-asset verification (FART-01 / TC-REAL-01).

Uses the correct class (`FineARTVLAPolicy`), the checkpoint's own config and the
PaliGemma tokenizer to run four stages on real weights:

  1_integrity   file sizes + SHA-256 against the Hub manifest
  2_load        FineARTVLAPolicy.from_pretrained(local dir, bf16, cuda) + processors
  3_action      tokenize a real instruction, predict_action_chunk with explicit noise
  4_save_reload save_pretrained, reload, compare the same chunk with the same noise

Stage 4 needs ~9 GB of free disk next to the checkpoint and is marked blocked
(not passed) when the host cannot provide it.

Usage:
    python run_fineart.py --checkpoint-dir DIR --tokenizer-dir DIR \
        --manifest fineart-inventory.json --report fineart-verification.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import torch

for _parent in Path(__file__).resolve().parents:
    _verif = _parent / "releases/sanitized-20261024-01/lerobot/tests/verification"
    if (_verif / "harness.py").is_file():
        sys.path.insert(0, str(_verif))
        break
from harness import CHUNK_ATOL, CHUNK_RTOL, Stage, Verifier, compare, finite_gate  # noqa: E402

CTX: dict = {}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def stage_integrity(ctx: dict) -> dict:
    ckpt = Path(CTX["checkpoint_dir"])
    manifest = json.loads(Path(CTX["manifest"]).read_text())
    files = {f["path"]: f for f in manifest["checkpoint"]["files"]}
    missing, wrong_size = [], []
    for rel, meta in files.items():
        target = ckpt / rel
        if not target.is_file():
            missing.append(rel)
        elif meta["bytes"] and target.stat().st_size != meta["bytes"]:
            wrong_size.append({"path": rel, "expected": meta["bytes"], "actual": target.stat().st_size})
    big = next((rel for rel, meta in files.items() if rel.endswith(".safetensors") and meta.get("sha256")), None)
    digest = _sha256(ckpt / big) if big else None
    expected = files[big]["sha256"] if big else None
    if missing or wrong_size or (expected and digest != expected):
        raise AssertionError(f"integrity failed: missing={missing} size={wrong_size} "
                             f"sha_match={expected == digest}")
    return {
        "checkpoint_files": len(files),
        "checkpoint_bytes": sum(f["bytes"] for f in files.values()),
        "weights_sha256": digest,
        "weights_sha256_source": "hub lfs metadata",
    }


def stage_tokenizer(ctx: dict) -> dict:
    """Text tokenizer is a *separate, gated* repo — record its real status."""
    tok = Path(CTX["tokenizer_dir"])
    tok_files = sorted(p.name for p in tok.iterdir() if p.is_file()) if tok.is_dir() else []
    if "tokenizer.model" not in tok_files:
        return {
            "blocked": "google/paligemma-3b-pt-224 is a gated repo; its files return 403 "
                       "without an accepted licence on this host",
            "tokenizer_dir": str(tok),
            "files": tok_files,
            "declared_by_config": "google/paligemma-3b-pt-224 @ 35e4f46485b4d07967e7e9935bc3786aad50687c",
        }
    return {"files": tok_files, "tokenizer_dir": str(tok)}


def stage_load(ctx: dict) -> dict:
    from lerobot.policies.factory import make_pre_post_processors
    from lerobot.policies.fineart_vla import FineARTVLAConfig
    from lerobot.policies.fineart_vla.modeling_fineart_vla import FineARTVLAPolicy

    ckpt = CTX["checkpoint_dir"]
    t0 = time.time()
    cfg = FineARTVLAConfig.from_pretrained(ckpt)
    cfg.dtype = torch.bfloat16
    cfg.device = "cuda"
    policy = FineARTVLAPolicy.from_pretrained(ckpt, config=cfg)
    policy.eval()
    load_s = time.time() - t0

    params = list(policy.parameters())
    breakdown = {}
    for name, mod in policy.named_children():
        ps = list(mod.parameters())
        if not ps:
            continue
        breakdown[name] = {
            "numel_M": round(sum(p.numel() for p in ps) / 1e6, 1),
            "dtypes": sorted({str(p.dtype).replace("torch.", "") for p in ps}),
            "devices": sorted({str(p.device) for p in ps}),
        }
    pre, post = make_pre_post_processors(cfg, pretrained_path=ckpt)
    if pre is None or post is None:
        raise AssertionError("processors did not load from the checkpoint")
    CTX.update(policy=policy, cfg=cfg, pre=pre, post=post)
    return {
        "class": type(policy).__name__,
        "config_class": type(cfg).__name__,
        "registered_type": getattr(cfg, "type", None),
        "load_seconds": round(load_s, 1),
        "params_M": round(sum(p.numel() for p in params) / 1e6, 1),
        "param_dtypes": sorted({str(p.dtype).replace("torch.", "") for p in params}),
        "components": breakdown,
        "chunk_size": int(getattr(cfg, "chunk_size", 0)),
        "n_action_steps": int(getattr(cfg, "n_action_steps", 0)),
        "image_resolution": getattr(cfg, "image_resolution", None),
        "input_features": {k: list(v.shape) for k, v in cfg.input_features.items()},
        "output_features": {k: list(v.shape) for k, v in cfg.output_features.items()},
        "processor_steps": {
            "pre": [type(s).__name__ for s in getattr(pre, "steps", [])],
            "post": [type(s).__name__ for s in getattr(post, "steps", [])],
        },
        "gpu_allocated_GB": round(torch.cuda.memory_allocated() / 1e9, 2),
    }


def _language_tokens() -> tuple[torch.Tensor, torch.Tensor, str]:
    """Real tokenizer when the gated repo is reachable, otherwise placeholder ids.

    `google/paligemma-3b-pt-224` is a gated repo; on a host without an accepted
    licence the download is 403. Feeding seeded placeholder ids still exercises
    the real weights and the action path, but it is NOT a language-conditioned
    result and the report says so.
    """
    from lerobot.utils.constants import OBS_LANGUAGE_ATTENTION_MASK, OBS_LANGUAGE_TOKENS
    from transformers import AutoTokenizer

    text = "put the red cube into the box"
    g = torch.Generator().manual_seed(21)
    try:
        tok = AutoTokenizer.from_pretrained(CTX["tokenizer_dir"])
        enc = tok(text, return_tensors="pt", padding="max_length", max_length=32, truncation=True)
        return (enc["input_ids"].cuda(), enc["attention_mask"].bool().cuda(),
                f"real tokenizer ({CTX['tokenizer_dir']})")
    except Exception as exc:  # noqa: BLE001 - the fallback is the evidence
        ids = torch.randint(0, 1000, (1, 32), generator=g)
        mask = torch.ones(1, 32, dtype=torch.bool)
        return ids.cuda(), mask.cuda(), f"placeholder ids: {type(exc).__name__}: {str(exc)[:160]}"


def _batch_and_noise(policy, cfg) -> tuple[dict, torch.Tensor]:
    from lerobot.utils.constants import OBS_LANGUAGE_ATTENTION_MASK, OBS_LANGUAGE_TOKENS

    ids, mask, token_source = _language_tokens()
    text = "put the red cube into the box"
    g = torch.Generator().manual_seed(21)
    batch = {
        OBS_LANGUAGE_TOKENS: ids,
        OBS_LANGUAGE_ATTENTION_MASK: mask,
    }
    for name, feat in cfg.input_features.items():
        shape = [1, *list(feat.shape)]
        if "images" in name:
            batch[name] = (torch.rand(shape, generator=g) * 255).to(torch.uint8).cuda()
        else:
            batch[name] = torch.randn(shape, generator=g).cuda()
    out_dim = int(cfg.output_features["action"].shape[0])
    chunk = int(getattr(cfg, "chunk_size", 1) or 1)
    noise = torch.randn(1, chunk, out_dim, generator=g).cuda()
    CTX.update(batch=batch, noise=noise, text=text, token_source=token_source)
    return batch, noise


def stage_action(ctx: dict) -> dict:
    policy, cfg = CTX["policy"], CTX["cfg"]
    batch, noise = _batch_and_noise(policy, cfg)
    t0 = time.time()
    with torch.no_grad():
        chunk = policy.predict_action_chunk(batch, noise=noise)
    elapsed = time.time() - t0
    metrics = finite_gate("fineart_chunk", chunk)
    CTX["chunk"] = chunk.detach().clone()
    return {
        "action_dim": metrics["shape"][-1],
        "chunk_shape": metrics["shape"],
        "dtype": metrics["dtype"],
        "finite": True,
        "seconds": round(elapsed, 2),
        "task": CTX["text"],
        "token_source": CTX["token_source"],
        "language_conditioning": "not exercised" if CTX["token_source"].startswith("placeholder") else "real tokens",
        "noise_seed": 21,
    }


def stage_save_reload(ctx: dict) -> dict:
    from lerobot.policies.fineart_vla import FineARTVLAConfig
    from lerobot.policies.fineart_vla.modeling_fineart_vla import FineARTVLAPolicy

    ckpt_bytes = sum(p.stat().st_size for p in Path(CTX["checkpoint_dir"]).glob("*.safetensors"))
    free = shutil.disk_usage(CTX["checkpoint_dir"]).free
    if free < ckpt_bytes * 1.2:
        return {"blocked": f"need {ckpt_bytes / 1e9:.1f} GB free to re-save, have {free / 1e9:.1f} GB"}

    policy = CTX["policy"]
    out = Path(tempfile.mkdtemp(prefix="fineart-reload-", dir=CTX["scratch"]))
    t0 = time.time()
    policy.save_pretrained(out)
    saved = sorted(p.name for p in out.iterdir())
    # Free the GPU before loading the copy: one model at a time.
    policy.to("cpu")
    torch.cuda.empty_cache()
    cfg = FineARTVLAConfig.from_pretrained(str(out))
    cfg.dtype = torch.bfloat16
    cfg.device = "cuda"
    reloaded = FineARTVLAPolicy.from_pretrained(str(out), config=cfg)
    reloaded.eval()
    reloaded.to("cuda")
    with torch.no_grad():
        chunk2 = reloaded.predict_action_chunk(CTX["batch"], noise=CTX["noise"])
    result = compare("fineart_chunk_reload", CTX["chunk"], chunk2,
                     atol=CHUNK_ATOL, rtol=CHUNK_RTOL)
    result.update({
        "saved_files": saved,
        "save_and_reload_seconds": round(time.time() - t0, 1),
        "tolerance": {"atol": CHUNK_ATOL, "rtol": CHUNK_RTOL},
    })
    CTX["policy"] = reloaded
    shutil.rmtree(out, ignore_errors=True)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint-dir", required=True)
    ap.add_argument("--tokenizer-dir", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--scratch", default="/home/lfy/lelab-rmr")
    ap.add_argument("--stages", default="", help="comma-separated subset, e.g. 1_integrity,2_load")
    args = ap.parse_args()

    CTX.update(checkpoint_dir=args.checkpoint_dir, tokenizer_dir=args.tokenizer_dir,
               manifest=args.manifest, scratch=args.scratch)
    verifier = Verifier(
        stages=[
            Stage("1_integrity", stage_integrity, required=True),
            Stage("1b_tokenizer", stage_tokenizer, required=False),
            Stage("2_load", stage_load, required=True, requires=("1_integrity",)),
            Stage("3_action", stage_action, required=True, requires=("2_load",)),
            Stage("4_save_reload", stage_save_reload, required=True, requires=("3_action",)),
        ],
        report_path=Path(args.report),
        metadata={
            "kind": "fineart-real-verification",
            "host": __import__("os").uname().nodename,
            "torch": torch.__version__,
            "cuda": torch.version.cuda,
            "gpu": torch.cuda.get_device_name(0),
            "checkpoint_dir": args.checkpoint_dir,
            "tokenizer_dir": args.tokenizer_dir,
        },
    )
    selected = [s.strip() for s in args.stages.split(",") if s.strip()] or None
    return verifier.run(selected)


if __name__ == "__main__":
    raise SystemExit(main())
