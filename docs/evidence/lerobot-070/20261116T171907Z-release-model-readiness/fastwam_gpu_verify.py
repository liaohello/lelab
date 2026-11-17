"""FastWAM real-model minimal verification (R3, TC-REAL-01 subset).

Runs on g128 with the synced worktree venv (Python 3.12, CUDA torch, V100S).

Stages, each separately evidenced and non-fatal to the next:
  1. asset integrity   — safetensors magic, no *.incomplete, snapshot commit
  2. real load         — checkpoint's own native feature space, GPU fp32
  3. offline action    — fixed-seed forward on native dims; finite/shape recorded
  4. save/reload       — same fixed noise reproduces the same action
  5. short backprop    — 1-3 optimizer updates on synthetic native-space batches
                         (clearly labelled: NOT fine-tuning evidence)

Native 7/8-dim operation here says nothing about SO-101 6/12-dim controllability;
that is reported separately in RELEASE-READINESS.md.
"""
import json
import os
import struct
import sys
import time
from pathlib import Path

import torch

HF_HOME = Path("/home/lfy/lelab-rmr/hf-cache")
os.environ.setdefault("HF_HOME", str(HF_HOME))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

WORKTREE = Path("/home/lfy/lelab-rmr")
LEROBOT = WORKTREE / "releases/sanitized-20261024-01/lerobot"
sys.path.insert(0, str(LEROBOT / "src"))

REPORT: dict[str, object] = {"stages": {}}


def stage(name):
    def deco(fn):
        def wrapped(*a, **k):
            entry: dict = {}
            t0 = time.time()
            try:
                out = fn(*a, **k)
                entry.update(out if isinstance(out, dict) else {})
                entry["ok"] = True
            except Exception as exc:  # noqa: BLE001 — every stage reports, none masks
                entry["ok"] = False
                entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["seconds"] = round(time.time() - t0, 2)
            REPORT["stages"][name] = entry
            print(f"[{name}] ok={entry['ok']} {entry.get('error', '')} ({entry['seconds']}s)", flush=True)
            return entry
        return wrapped
    return deco



def _load_policy(ckpt: str):
    """Load with the checkpoint's declared dtype (bfloat16) on GPU, UMT5 on CPU.

    fp32 exceeds the 32 GB card; the checkpoint itself declares bfloat16, and the
    config's `text_encoder_device='cpu'` keeps the ~11 GB UMT5-XXL off the GPU
    (this host has 492 GB RAM). The config object is passed explicitly because
    stray kwargs to from_pretrained do not reach the dataclass.
    """
    from lerobot.policies.fastwam import FastWAMConfig, FastWAMPolicy
    cfg = FastWAMConfig.from_pretrained(ckpt)
    cfg.dtype = torch.bfloat16
    cfg.device = "cuda"
    cfg.text_encoder_device = "cpu"
    # Training activations exceed the 32 GB card without checkpointing; inference
    # runs fine either way. Enabled globally so stage 5 can hold 1 update in memory.
    cfg.use_gradient_checkpointing = True
    return FastWAMPolicy.from_pretrained(ckpt, config=cfg)


def _snapshot_dir(repo_id: str) -> Path:
    from huggingface_hub import snapshot_download
    return Path(snapshot_download(repo_id))


@stage("1_integrity")
def stage1() -> dict:
    import safetensors  # noqa: F401
    repo = _snapshot_dir("lerobot/fastwam_base")
    files = [p for p in repo.rglob("*") if p.is_file()]
    incomplete = [p.name for p in repo.rglob("*.incomplete")]
    assert not incomplete, f"incomplete downloads: {incomplete}"
    sha_files = [p for p in files if p.suffix == ".safetensors"]
    assert sha_files, "no safetensors weights found"
    for p in sha_files:
        with open(p, "rb") as fh:
            head = fh.read(9)
        # safetensors = 8-byte little-endian header length, then a JSON header.
        # A git-lfs pointer is ASCII text starting with 'version https://'.
        (hlen,) = struct.unpack("<Q", head[:8])
        if not (head[8:9] == b"{" and hlen < p.stat().st_size):
            raise AssertionError(
                f"{p.name} is not a safetensors file (header len {hlen}, byte9={head[8:9]!r})"
            )
        size = p.stat().st_size
        assert size > 1e6, f"{p.name} suspiciously small ({size} B)"
    commit = (repo / "refs" / "main").read_text().strip() if (repo / "refs/main").exists() else \
        Path(str(repo)).name
    return {"snapshot": str(repo), "files": len(files), "commit": commit,
            "weight_files": [p.name for p in sha_files]}


@stage("2_load")
def stage2(integrity: dict) -> dict:
    ckpt = integrity["snapshot"]
    policy = _load_policy(ckpt)
    params = sum(p.numel() for p in policy.parameters())
    dtypes = sorted({str(p.dtype).split(".")[-1] for p in policy.parameters()})
    return {"params_M": round(params / 1e6, 1), "dtypes": dtypes,
            "device": str(next(policy.parameters()).device),
            "native_action_dim": int(policy.config.output_features["action"].shape[0]),
            "native_state_dim": int(
                policy.config.input_features["observation.state"].shape[0])}


@stage("3_offline_action")
def stage3(policy) -> dict:
    """Fixed-seed forward in the checkpoint's own native space."""
    torch.manual_seed(0)
    cfg = policy.config
    state_dim = int(cfg.input_features["observation.state"].shape[0])
    batch = {
        "observation.state": torch.randn(1, state_dim, device="cuda"),
        "task": ["carry the object forward"],
        "seed": 0,
    }
    for name, feat in cfg.input_features.items():
        if name.startswith("observation.images"):
            h, w = feat.shape[1], feat.shape[2]
            batch[name] = torch.randint(0, 255, (1, 3, h, w), device="cuda", dtype=torch.uint8)
    policy.reset()
    with torch.inference_mode():
        action = policy.select_action(batch)
    arr = action.detach().cpu()
    finite = bool(torch.isfinite(arr).all())
    return {"action_shape": list(arr.shape), "dtype": str(arr.dtype),
            "finite": finite, "sample": [round(v, 4) for v in arr[0, :4].tolist()]}


@stage("4_save_reload")
def stage4(policy) -> dict:
    import tempfile
    torch.manual_seed(7)
    cfg = policy.config
    state_dim = int(cfg.input_features["observation.state"].shape[0])
    batch = {
        "observation.state": torch.randn(1, state_dim, device="cuda"),
        "task": ["carry the object forward"],
        "seed": 0,
    }
    for name, feat in cfg.input_features.items():
        if name.startswith("observation.images"):
            h, w = feat.shape[1], feat.shape[2]
            batch[name] = torch.randint(0, 255, (1, 3, h, w), device="cuda", dtype=torch.uint8)
    with torch.inference_mode():
        before = policy.select_action(dict(batch)).detach().cpu().clone()
    with tempfile.TemporaryDirectory() as td:
        policy.save_pretrained(td)
        # A second full copy cannot co-reside on the 32 GB card: move the loaded
        # one to CPU first and empty the cache before the reload.
        policy.to("cpu")
        torch.cuda.empty_cache()
        reloaded = _load_policy(td)
        reloaded.reset()
        with torch.inference_mode():
            after = reloaded.select_action(dict(batch)).detach().cpu()
    max_diff = float((before - after).abs().max())
    return {"max_abs_diff": max_diff, "identical_within_1e_4": bool(max_diff < 1e-4)}


@stage("5_short_backprop")
def stage5(policy) -> dict:
    """1 optimizer update on a synthetic native-space batch (labelled, not fine-tuning)."""
    opt = torch.optim.AdamW(policy.parameters(), lr=1e-5)
    policy.to("cuda")
    policy.train()
    cfg = policy.config
    state_dim = int(cfg.input_features["observation.state"].shape[0])
    B = 1
    horizon = int(getattr(cfg, "action_horizon", 1))
    a_dim = int(cfg.output_features["action"].shape[0])
    batch = {
        "observation.state": torch.randn(B, state_dim, device="cuda"),
        "task": ["carry the object forward"],
        "action": torch.randn(B, horizon, a_dim, device="cuda"),
    }
    for name, feat in cfg.input_features.items():
        if name.startswith("observation.images"):
            h, w = feat.shape[1], feat.shape[2]
            batch[name] = torch.randint(0, 255, (B, 3, h, w), device="cuda", dtype=torch.uint8)
    losses = []
    for _ in range(1):
        opt.zero_grad(set_to_none=True)
        out = policy.forward(batch)
        if isinstance(out, dict):
            loss = out["loss"]
        elif isinstance(out, tuple):
            loss = next(t_ for t_ in out if torch.is_tensor(t_) and t_.requires_grad)
        else:
            loss = out
        loss.backward()
        gnorm = torch.nn.utils.clip_grad_norm_(policy.parameters(), 1e9)
        opt.step()
        losses.append(float(loss))
    mem = torch.cuda.max_memory_allocated() / 1e9
    return {"loss": losses, "grad_norm_finite": bool(torch.isfinite(gnorm)),
            "cuda_peak_GB": round(mem, 2)}


if __name__ == "__main__":
    print("device:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU-ONLY",
          "| torch", torch.__version__, flush=True)
    REPORT["device"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    REPORT["torch"] = torch.__version__
    s1 = stage1()
    policy = None
    if s1.get("ok"):
        s2 = stage2(s1)
        if s2.get("ok"):
            from lerobot.policies.fastwam import FastWAMPolicy  # noqa: F401
            # rebuild the handle outside the stage for reuse
            policy = _load_policy(s1["snapshot"])
            s3 = stage3(policy)
            if s3.get("ok"):
                stage4(policy)
            stage5(policy)
    out = Path(__file__).resolve().parent / "fastwam-verification.json"
    out.write_text(json.dumps(REPORT, indent=2, ensure_ascii=False))
    print("REPORT ->", out, flush=True)
