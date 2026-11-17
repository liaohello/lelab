"""FineART-VLA asset inventory and budget (FART-01).

Reads the *actual* source in this worktree (class names, defaults, processor
modules) and the *actual* Hub metadata for every component the policy loads,
then emits a component manifest plus a disk/VRAM budget. No weights are
downloaded by this script; `--probe-bytes N` measures mirror throughput with a
range request so the download decision is based on a measurement, not a guess.

Usage:
    python fineart_inventory.py [--endpoint https://hf-mirror.com] [--probe-bytes 33554432]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[3] if (Path(__file__).resolve().parents[3] / "releases").is_dir() else None
for parent in [Path(__file__).resolve().parent, *Path(__file__).resolve().parents]:
    if (parent / "releases/sanitized-20261024-01/lerobot").is_dir():
        WORKTREE = parent
        break
if WORKTREE is None:
    raise SystemExit("cannot locate worktree with releases/sanitized-20261024-01/lerobot")

LEROBOT = WORKTREE / "releases/sanitized-20261024-01/lerobot"
POLICY_DIR = LEROBOT / "src/lerobot/policies/fineart_vla"

CHECKPOINT_ALIAS = "lerobot/fineart_vla_base"
TEXT_TOKENIZER = "google/paligemma-3b-pt-224"
FAST_TOKENIZER = "physical-intelligence/fast"
KERNELS = [
    ("flashrt/flashrt-adarms-train", "v1"),
    ("flashrt/flashrt-flex-attention-train", "v1"),
]


def _get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "lelab-fineart-inventory"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def _meta(endpoint: str, repo: str) -> dict:
    url = f"{endpoint}/api/models/{repo}?blobs=true"
    try:
        return json.loads(_get(url))
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}", "id": repo}


def _fq(repo: str, endpoint: str) -> str:
    """Resolve the canonical id when the friendly alias redirects."""
    try:
        _get(f"{endpoint}/api/models/{repo}")
        return repo
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 307, 308):
            loc = exc.headers.get("Location") or exc.url
            m = re.search(r"/api/models/([^?]+)", str(loc))
            if m:
                return m.group(1)
        return repo


def _source_facts() -> dict:
    cfg = (POLICY_DIR / "configuration_fineart_vla.py").read_text(encoding="utf-8")
    proc = (POLICY_DIR / "processor_fineart_vla.py").read_text(encoding="utf-8")
    model = (POLICY_DIR / "modeling_fineart_vla.py").read_text(encoding="utf-8")
    init = (POLICY_DIR / "__init__.py").read_text(encoding="utf-8")
    classes = sorted(set(re.findall(r"^class (\w+)", model, re.M)))
    defaults = dict(re.findall(r"^    (\w+): (?:str|int|float|bool) = (.+)$", cfg, re.M))
    return {
        "module": str(POLICY_DIR.relative_to(WORKTREE)),
        "classes": classes,
        "policy_class": "FineARTVLAPolicy",
        "registered_type": "fineart_vla" if 'register_subclass("fineart_vla")' in cfg else "MISSING",
        "base_config": "PI05Config" if "class FineARTVLAConfig(PI05Config)" in cfg else "MISSING",
        "text_tokenizer_default": "google/paligemma-3b-pt-224"
        if 'text_tokenizer_name: str = "google/paligemma-3b-pt-224"' in cfg
        else "MISSING",
        "fast_tokenizer_default": "physical-intelligence/fast"
        if 'action_tokenizer_name: str = "physical-intelligence/fast"' in cfg
        else "MISSING",
        "processor_class": "FineARTVLAProcessor" if "class FineARTVLAProcessor" in proc else "MISSING",
        "exports": re.findall(r'"(\w+)"', init),
        "flash_kernels": sorted(set(re.findall(r'get_kernel\(\s*"([^"]+)"\s*,\s*revision="([^"]+)"', model))),
        "selected_defaults": {k: defaults[k] for k in (
            "recipe", "text_loss_weight", "flow_loss_weight", "enable_fast_action_loss",
            "max_action_tokens", "fast_skip_tokens", "knowledge_insulation",
        ) if k in defaults},
    }


def _files(meta: dict) -> list[dict]:
    out = []
    for sib in meta.get("siblings", []):
        size = int(sib.get("size") or 0)
        out.append({"path": sib.get("rfilename"), "bytes": size, "sha256": sib.get("lfs", {}).get("sha256")})
    return sorted(out, key=lambda f: -f["bytes"])


def _probe(endpoint: str, repo: str, path: str, nbytes: int) -> dict:
    url = f"{endpoint}/{repo}/resolve/main/{path}"
    req = urllib.request.Request(url, headers={"Range": f"bytes=0-{nbytes - 1}"})
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310
            data = resp.read()
    except Exception as exc:  # noqa: BLE001 - the probe result is the evidence
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    elapsed = max(time.time() - start, 1e-6)
    return {
        "ok": True,
        "bytes": len(data),
        "seconds": round(elapsed, 2),
        "mib_per_s": round(len(data) / 1048576 / elapsed, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="https://hf-mirror.com")
    ap.add_argument("--probe-bytes", type=int, default=0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    facts = _source_facts()
    canonical = _fq(CHECKPOINT_ALIAS, args.endpoint)
    ckpt_meta = _meta(args.endpoint, canonical)
    tok_meta = _meta(args.endpoint, TEXT_TOKENIZER)
    fast_meta = _meta(args.endpoint, FAST_TOKENIZER)

    ckpt_files = _files(ckpt_meta)
    ckpt_bytes = sum(f["bytes"] for f in ckpt_files)
    tokenizer_names = {
        "tokenizer.model", "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json", "added_tokens.json",
    }
    tokenizer_files = [f for f in _files(tok_meta) if f["path"] in tokenizer_names]
    tokenizer_bytes = sum(f["bytes"] for f in tokenizer_files)
    fast_bytes = sum(f["bytes"] for f in _files(fast_meta))

    probe = None
    biggest = next((f for f in ckpt_files if f["path"].endswith(".safetensors")), None)
    if args.probe_bytes and biggest:
        probe = _probe(args.endpoint, canonical, biggest["path"], args.probe_bytes)
        probe["target"] = biggest["path"]

    budget = {
        "checkpoint_bytes": ckpt_bytes,
        "text_tokenizer_bytes": tokenizer_bytes,
        "fast_tokenizer_bytes": fast_bytes,
        "download_total_bytes": ckpt_bytes + tokenizer_bytes,
        "training_extra_bytes": fast_bytes,
        "download_total_gib": round((ckpt_bytes + tokenizer_bytes) / 2**30, 2),
        "inference_weights_bf16_gib": round(ckpt_bytes / 2**30, 2),
        "note": "download_total = checkpoint + tokenizer files; FAST is training-only; "
                "VRAM for activations is not derived from file size and is not claimed here.",
    }
    if probe and probe.get("ok") and probe["mib_per_s"] > 0:
        budget["projected_download_minutes"] = round(
            (ckpt_bytes + tokenizer_bytes) / 1048576 / probe["mib_per_s"] / 60, 1
        )

    embedded_action_tokenizer = [
        f["path"] for f in ckpt_files if f["path"].startswith("action_tokenizer/")
    ]

    report = {
        "kind": "fineart-asset-inventory",
        "case": "FART-01",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "worktree": str(WORKTREE),
        "endpoint": args.endpoint,
        "source": facts,
        "checkpoint": {
            "requested": CHECKPOINT_ALIAS,
            "canonical": canonical,
            "revision": ckpt_meta.get("sha"),
            "last_modified": ckpt_meta.get("lastModified"),
            "gated": ckpt_meta.get("gated"),
            "file_count": len(ckpt_files),
            "bytes": ckpt_bytes,
            "largest": ckpt_files[:6],
            "files": ckpt_files,
        },
        "text_tokenizer": {
            "repo": TEXT_TOKENIZER,
            "revision": tok_meta.get("sha"),
            "files": tokenizer_files,
            "bytes": tokenizer_bytes,
        },
        "fast_tokenizer": {
            "repo": FAST_TOKENIZER,
            "revision": fast_meta.get("sha"),
            "bytes": fast_bytes,
            "note": "training-only; inference does not need it",
        },
        "checkpoint_embedded_action_tokenizer": embedded_action_tokenizer,
        "budget": budget,
        "probe": probe,
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
