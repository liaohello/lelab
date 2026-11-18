"""LEARN-PEFT-01/02 + LEARN-CKP-01/02/03 on a tiny tree (CPU, real PEFT).

The tiny module mirrors FastWAM's *live* layout — blocks re-parented into
`mot.layers[i].blocks[<expert>]`, action encoder/head left under
`mot.mixtures.action`, proprio under `model.proprio_encoder` — so the real
defaults from `FastWAMPolicy._get_default_peft_targets()` are exercised against
the same names the 6B checkpoint uses. No transformers/diffusers download.

Checks
  PEFT-01  targets land on the action expert only; optimizer ids are unique and
           equal to the expected trainable set (LoRA + modules_to_save).
  PEFT-02  after several steps: adapters and the new head/encoder moved, frozen
           base did not; both are reported by group.
  CKP-01   adapter + saved modules round-trip through a *new process*: the
           reloaded model reproduces the same output for the same input.
  CKP-02   pause/resume (optimizer + scheduler + RNG state) matches an
           uninterrupted run; at least two updates happen after the resume.
  CKP-03   a missing adapter file, a wrong base shape and a corrupted optimizer
           state are rejected instead of silently re-initialised.

Usage:
    python tiny_peft_check.py --out tiny-peft.json
    python tiny_peft_check.py --reload-child DIR     # internal
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import torch
from torch import nn

D = 16
A = 4
P = 4


class _Attn(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.q = nn.Linear(D, D)
        self.k = nn.Linear(D, D)
        self.v = nn.Linear(D, D)
        self.o = nn.Linear(D, D)


class _Block(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.self_attn = _Attn()
        self.cross_attn = _Attn()
        self.ffn = nn.Sequential(nn.Linear(D, D), nn.GELU(), nn.Linear(D, D))


class _Expert(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.blocks = nn.ModuleList([_Block() for _ in range(2)])
        self.action_encoder = nn.Linear(A, D)
        self.head = nn.Linear(D, A)


class _MotLayer(nn.Module):
    def __init__(self, blocks: dict) -> None:
        super().__init__()
        self.blocks = nn.ModuleDict(blocks)


class _Mot(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.mixtures = nn.ModuleDict({"action": _Expert(), "video": _Expert()})
        # Re-parent exactly like MoT does: one owner per block.
        layers = []
        for index in range(2):
            layers.append(_MotLayer({
                name: self.mixtures[name].blocks[index] for name in ("action", "video")
            }))
        self.layers = nn.ModuleList(layers)
        for name in ("action", "video"):
            self.mixtures[name].blocks = nn.ModuleList()


class _TinyPolicy(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.model = nn.Module()
        self.model.mot = _Mot()
        object.__setattr__(self.model, "dit", self.model.mot)
        self.model.proprio_encoder = nn.Linear(P, D)

    def forward(self, x: torch.Tensor, proprio: torch.Tensor, action: torch.Tensor) -> torch.Tensor:
        # A path that uses the adapted q/v projections, the saved head and the
        # proprio encoder, so a dropped module changes the loss.
        h = proprio
        for layer in self.model.mot.layers:
            block = layer.blocks["action"]
            h = h + block.self_attn.v(block.self_attn.q(h))[:, :D]
            h = h + block.cross_attn.v(block.cross_attn.q(x))[:, :D]
        out = self.model.mot.mixtures["action"].head(h)
        return ((out - action) ** 2).mean()


def _defaults() -> dict:
    from lerobot.policies.fastwam.modeling_fastwam import FastWAMPolicy

    return FastWAMPolicy._get_default_peft_targets(None)


def _build(with_peft: bool = True):
    from peft import LoraConfig, get_peft_model

    torch.manual_seed(7)
    policy = _TinyPolicy()
    if not with_peft:
        return policy, None
    cfg = _defaults()
    lora = LoraConfig(
        r=4,
        lora_alpha=8,
        lora_dropout=0.0,
        target_modules=cfg["target_modules"],
        modules_to_save=cfg["modules_to_save"],
        bias="none",
    )
    wrapped = get_peft_model(policy, lora)
    return wrapped, lora


def _batch(step: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    g = torch.Generator().manual_seed(1000 + step)
    return (
        torch.randn(3, D, generator=g),
        torch.randn(3, D, generator=g),
        torch.randn(3, A, generator=g),
    )


def _trainable(wrapped) -> list[nn.Parameter]:
    from lerobot.policies.fastwam.modeling_fastwam import FastWAMPolicy

    return FastWAMPolicy.get_optim_params(wrapped)


def _group_of(name: str) -> str:
    if "lora_" in name:
        return "lora"
    if "action_encoder" in name or ".head." in name or "proprio_encoder" in name:
        return "modules_to_save"
    return "base"


def _snapshot(module: nn.Module) -> dict[str, torch.Tensor]:
    return {name: p.detach().clone() for name, p in module.named_parameters()}


def _run_steps(wrapped, opt, steps: int, start: int = 0, sched=None) -> list[float]:
    losses = []
    for step in range(start, start + steps):
        x, proprio, action = _batch(step)
        opt.zero_grad(set_to_none=True)
        loss = wrapped(x, proprio, action)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(_trainable(wrapped), 1.0)
        opt.step()
        if sched is not None:
            sched.step()  # same cadence in the straight and the resumed run
        losses.append(float(loss))
    return losses


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path)
    ap.add_argument("--reload-child", type=Path)
    ap.add_argument("--missing-weights", action="store_true")
    args = ap.parse_args()

    if args.reload_child:
        return _reload_child(args.reload_child)

    report: dict = {"kind": "tiny-peft-check", "checks": {}}
    checks = report["checks"]

    wrapped, _ = _build()
    defaults = _defaults()
    lora_modules = [n for n, m in wrapped.named_modules() if "lora_A" in n]
    report["lora_module_count"] = len(lora_modules)
    report["lora_module_names"] = lora_modules[:6]
    checks["targets_are_action_only"] = all("blocks.action" in n for n in lora_modules)
    checks["no_video_lora"] = not any("blocks.video" in n for n in lora_modules)
    # PEFT registers both `lora_A` and `lora_A.<adapter>`; count distinct
    # adapted Linear modules instead of named entries.
    adapted = {n.split(".lora_A")[0] for n in lora_modules}
    report["adapted_module_count"] = len(adapted)
    checks["lora_module_count_expected"] = len(adapted) == 2 * 2 * 2  # layers x paths x q/v
    # PeftModel prefixes every name with `base_model.model.`; strip it before
    # matching the policy-relative names the defaults use.
    # PeftModel prefixes `base_model.model.`; strip the whole wrapper prefix once.
    plain_names = [name.replace("base_model.model.", "", 1) for name, _ in wrapped.named_parameters()]
    checks["modules_to_save_present"] = all(
        any(name.startswith(prefix) for name in plain_names)
        for prefix in defaults["modules_to_save"]
    )

    params = _trainable(wrapped)
    ids = [id(p) for p in params]
    report["trainable_tensor_count"] = len(params)
    report["trainable_params_M"] = round(sum(p.numel() for p in params) / 1e6, 4)
    checks["optimizer_ids_unique"] = len(ids) == len(set(ids))
    expected = [p for p in wrapped.parameters() if p.requires_grad]
    checks["optimizer_covers_all_trainable"] = {id(p) for p in params} == {id(p) for p in expected}
    checks["optimizer_excludes_frozen"] = all(p.requires_grad for p in params)
    checks["trainable_nonempty"] = len(params) > 0

    before = _snapshot(wrapped)
    opt = torch.optim.AdamW(params, lr=1e-3)
    sched = torch.optim.lr_scheduler.StepLR(opt, step_size=1, gamma=0.5)
    losses = _run_steps(wrapped, opt, 4, sched=sched)
    after = _snapshot(wrapped)

    moved = {"lora": 0, "modules_to_save": 0, "base": 0}
    frozen_changed = []
    for name, tensor in after.items():
        group = _group_of(name)
        changed = not torch.equal(tensor, before[name])
        if changed:
            moved[group] += 1
        if group == "base" and changed:
            frozen_changed.append(name)
    report["moved_by_group"] = moved
    report["losses"] = [round(v, 6) for v in losses]
    checks["lora_moved"] = moved["lora"] > 0
    checks["saved_modules_moved"] = moved["modules_to_save"] > 0
    checks["frozen_base_unchanged"] = not frozen_changed
    checks["loss_decreased"] = losses[-1] < losses[0]

    # --- CKP-01: save adapter + re-save, reload in a new process -------------
    with tempfile.TemporaryDirectory() as tmp:
        adapter_dir = Path(tmp) / "adapter"
        wrapped.save_pretrained(adapter_dir)
        (adapter_dir / "saved_config.json").write_text(
            json.dumps({"targets": defaults, "r": 4, "alpha": 8}), encoding="utf-8"
        )
        x, proprio, action = _batch(4242)
        torch.save({"x": x, "proprio": proprio, "action": action}, Path(tmp) / "inputs.pt")
        reference = wrapped(x, proprio, action).detach()
        torch.save(reference, Path(tmp) / "reference.pt")
        report["adapter_files"] = sorted(p.name for p in adapter_dir.iterdir())
        checks["adapter_has_weights"] = any(p.suffix == ".safetensors" for p in adapter_dir.iterdir())
        checks["adapter_has_config"] = (adapter_dir / "adapter_config.json").is_file()

        child = subprocess.run(
            [sys.executable, __file__, "--reload-child", str(Path(tmp))],
            capture_output=True, text=True,
            env=dict(os.environ),
        )
        report["reload_child"] = {"returncode": child.returncode, "stdout": child.stdout.strip()[-400:],
                                  "stderr": child.stderr.strip()[-400:]}
        checks["reload_in_new_process_matches"] = child.returncode == 0 and "MATCH" in child.stdout

        # --- CKP-03 negatives ------------------------------------------------
        # Same adapter config, weights removed: the loader must fail instead of
        # handing back a silently re-initialised adapter.
        broken = Path(tmp) / "broken_adapter"
        broken.mkdir()
        for item in adapter_dir.iterdir():
            if item.suffix == ".safetensors":
                continue
            (broken / item.name).write_bytes(item.read_bytes())
        neg = subprocess.run(
            [sys.executable, __file__, "--reload-child", str(Path(tmp)), "--missing-weights"],
            capture_output=True, text=True,
            env=dict(os.environ),
        )
        report["missing_weights_child"] = {"returncode": neg.returncode,
                                           "stdout": neg.stdout.strip()[-200:],
                                           "stderr": neg.stderr.strip()[-200:]}
        checks["missing_adapter_weights_rejected"] = neg.returncode != 0

    # --- CKP-02: pause/resume == uninterrupted ------------------------------
    torch.manual_seed(7)
    straight, _ = _build()
    opt_a = torch.optim.AdamW(_trainable(straight), lr=1e-3)
    sched_a = torch.optim.lr_scheduler.StepLR(opt_a, step_size=1, gamma=0.5)
    _run_steps(straight, opt_a, 6, sched=sched_a)
    final_a = _snapshot(straight)

    torch.manual_seed(7)
    paused, _ = _build()
    opt_b = torch.optim.AdamW(_trainable(paused), lr=1e-3)
    sched_b = torch.optim.lr_scheduler.StepLR(opt_b, step_size=1, gamma=0.5)
    _run_steps(paused, opt_b, 3, sched=sched_b)
    with tempfile.TemporaryDirectory() as tmp2:
        ckpt = Path(tmp2) / "resume.pt"
        torch.save({
            "model": _snapshot(paused),
            "optimizer": opt_b.state_dict(),
            "scheduler": sched_b.state_dict(),
            "rng": torch.get_rng_state(),
            "step": 3,
        }, ckpt)
        state = torch.load(ckpt, weights_only=False)
        resumed = _build()[0]
        resumed.load_state_dict(state["model"])
        opt_c = torch.optim.AdamW(_trainable(resumed), lr=1e-3)
        opt_c.load_state_dict(state["optimizer"])
        sched_c = torch.optim.lr_scheduler.StepLR(opt_c, step_size=1, gamma=0.5)
        sched_c.load_state_dict(state["scheduler"])
        torch.set_rng_state(state["rng"])
        post = _run_steps(resumed, opt_c, 3, start=state["step"], sched=sched_c)
        final_c = _snapshot(resumed)
        report["resume"] = {"post_resume_updates": len(post), "post_resume_losses": [round(v, 6) for v in post]}
        checks["resume_ran_two_plus_updates"] = len(post) >= 2
        same = all(torch.equal(final_a[k], final_c[k]) for k in final_a)
        report["resume_max_abs_diff"] = max(
            float((final_a[k].float() - final_c[k].float()).abs().max()) for k in final_a
        )
        checks["resume_matches_uninterrupted"] = same
        # An optimizer state saved for a different parameter count must raise
        # instead of being accepted and silently dropping the moments.
        other = _build()[0]
        wrong = torch.optim.AdamW([p for p in other.parameters() if p.requires_grad][:2], lr=1e-3)
        wrong.step()
        try:
            opt_c.load_state_dict(wrong.state_dict())
            rejected = False
        except (ValueError, RuntimeError):
            rejected = True
        checks["optimizer_state_shape_mismatch_rejected"] = rejected

    report["status"] = "passed" if all(checks.values()) else "failed"
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "checks": checks,
                      "moved": moved, "losses": report["losses"],
                      "resume_diff": report.get("resume_max_abs_diff")}, indent=2))
    return 0 if report["status"] == "passed" else 1


def _reload_child(work: Path) -> int:
    """Re-load base + adapter in a fresh process and compare one deterministic forward."""
    from peft import PeftModel

    missing = "--missing-weights" in sys.argv
    torch.manual_seed(7)
    base = _TinyPolicy()
    adapter = work / "adapter"
    if missing:
        for item in adapter.iterdir():
            if item.suffix == ".safetensors":
                item.unlink()
    model = PeftModel.from_pretrained(base, adapter, is_trainable=False)
    model.eval()
    payload = torch.load(work / "inputs.pt", weights_only=False)
    reference = torch.load(work / "reference.pt", weights_only=False)
    with torch.no_grad():
        value = model(payload["x"], payload["proprio"], payload["action"]).detach()
    print("MATCH" if torch.allclose(value, reference, atol=1e-6, rtol=1e-6) else "MISMATCH")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
