"""Collect deterministic prediction chunks from a FastWAM PEFT checkpoint.

Unlike the round-1 collector, the config and the processors come from the
**checkpoint itself** (`config.json`, `policy_preprocessor.json`,
`policy_postprocessor.json`), not from the dataset view. That is what the app
does when it loads a policy, and it is the only way arm B is evaluated with the
relative-action statistics it was trained with — re-deriving them on the eval
side would silently evaluate a different normalisation.

The dataset is the same absolute view for both arms: observations are identical,
only the weights and the processors differ.

Usage:
    python collect_ab.py --checkpoint CKPT_DIR --view DIR --role dev \
        --out preds.npz --report collect.json [--episodes 0,50]
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import torch

SEED = 2026
ANCHOR_FRACTIONS = (0.0, 0.25, 0.5, 0.75)
HORIZON = 32


def episode_lengths(view: Path) -> dict[int, int]:
    import pyarrow.parquet as pq

    rows = pq.read_table(view / "meta/episodes/chunk-000/file-000.parquet").to_pylist()
    return {int(r["episode_index"]): int(r["length"]) for r in rows}


def anchor_indices(length: int, fractions=ANCHOR_FRACTIONS) -> list[int]:
    if length <= 0:
        raise ValueError(f"episode length must be positive, got {length}")
    return [min(int(length * f), length - 1) for f in fractions]


def load_policy(checkpoint: Path, device: str, base_override: Path | None = None):
    """Base weights + LoRA adapter, exactly as the checkpoint directory stores them."""
    from lerobot.policies.fastwam import FastWAMConfig, FastWAMPolicy

    cfg = FastWAMConfig.from_pretrained(str(checkpoint))
    cfg.device = device
    cfg.dtype = torch.bfloat16
    cfg.text_encoder_device = "cpu"

    base_dir = Path(base_override) if base_override else Path(getattr(cfg, "pretrained_path", "") or "")
    if not (base_dir / "model.safetensors").exists():
        # A PEFT checkpoint directory only holds the adapter; the base has to be
        # named explicitly rather than guessed.
        raise SystemExit(
            f"{checkpoint} has no base weights; pass --base-checkpoint "
            f"(config pointed at {base_dir})"
        )
    policy = FastWAMPolicy.from_pretrained(str(base_dir), config=cfg)
    if (checkpoint / "adapter_config.json").is_file():
        from peft import PeftModel

        policy = PeftModel.from_pretrained(policy, str(checkpoint), is_trainable=False)
    policy.eval()
    return cfg, policy


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", required=True, type=Path)
    ap.add_argument("--base-checkpoint", type=Path, default=None)
    ap.add_argument("--view", required=True, type=Path)
    ap.add_argument("--role", choices=("train", "dev"), required=True)
    ap.add_argument("--episodes", default="")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--report", required=True, type=Path)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--label", default="")
    ap.add_argument("--app-root", type=Path, default=None,
                    help="app source root; when given, the checkpoint's own contract is run "
                         "through the app's validate_bimanual_policy_contract")
    ap.add_argument("--isolation-check", action="store_true",
                    help="re-run the first anchor with the future frames zeroed and with the "
                         "label replaced, to prove neither reaches the model")
    args = ap.parse_args()

    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from lerobot.lerobot_types import PolicyAction
    from lerobot.policies.fastwam.processor_fastwam import (
        make_fastwam_pre_post_processors_from_pretrained,
    )
    from lerobot.processor.relative_action_processor import bind_relative_anchor

    # The checkpoint's own `pretrained_path` names the base; `--base-checkpoint`
    # only overrides it in memory. The artefact on disk is never rewritten.
    cfg, policy = load_policy(args.checkpoint, args.device, args.base_checkpoint)

    app_contract = {}
    if args.app_root is not None:
        import sys as _sys
        _sys.path.insert(0, str(args.app_root))
        from lelab.remote_inference import validate_bimanual_policy_contract

        contract = {
            "state_dim": int(cfg.proprio_dim),
            "action_dim": int(cfg.action_dim),
            "camera_keys": sorted(k for k in (cfg.input_features or {}) if k.startswith("observation.images.")),
            "normalization_mapping": dict(getattr(cfg, "normalization_mapping", {}) or {}),
            "processor": "fastwam-v1",
            "action_semantics": "sent_target_after_local_safety_clamps",
            "use_relative_actions": bool(cfg.use_relative_actions),
        }
        cameras = [k.split("observation.images.")[-1] for k in contract["camera_keys"]]
        try:
            validate_bimanual_policy_contract(contract, cameras)
            app_contract = {"accepted": True, "contract": contract, "cameras": cameras}
        except Exception as exc:  # noqa: BLE001 - the message is the evidence
            app_contract = {"accepted": False, "error": f"{type(exc).__name__}: {exc}",
                            "contract": contract, "cameras": cameras}
    pre, post = make_fastwam_pre_post_processors_from_pretrained(cfg, args.checkpoint)
    relative_step = bind_relative_anchor(policy, pre)

    info = json.loads((args.view / "meta/info.json").read_text())
    fps = float(info["fps"])
    camera_keys = sorted(k for k in info["features"] if k.startswith("observation.images."))
    lengths = episode_lengths(args.view)
    if args.episodes:
        episodes = [int(x) for x in args.episodes.split(",") if x.strip()]
    else:
        splits = json.loads((args.view / "splits.json").read_text())
        key = "train" if args.role == "train" else ("validation" if "validation" in splits else "dev")
        episodes = sorted(int(e) for e in splits[key])

    preds, gts, pads, states, eps_out, anchors_out, rows = [], [], [], [], [], [], []
    isolation = {}
    started = time.time()

    def predict(sample, video_frames=None):
        batch = pre(dict(sample))
        if video_frames is not None:
            # Keep only the first frame of every camera window; the action-only
            # path is supposed to encode the current frame and nothing else.
            for key in camera_keys:
                if key in batch:
                    # Images are [B, T, C, H, W]: keep t=0, zero the rest.
                    keep = batch[key][:, :1]
                    rest = torch.zeros_like(batch[key][:, 1:])
                    batch[key] = torch.cat([keep, rest], dim=1)
        noise = torch.randn(1, HORIZON, int(cfg.action_dim),
                            generator=torch.Generator().manual_seed(SEED),
                            dtype=torch.float32).to(args.device)
        with torch.no_grad():
            chunk = policy.predict_action_chunk(batch, noise=noise)
        return post(PolicyAction(chunk.float())).float().cpu().numpy()[0]
    for ep in episodes:
        ds = LeRobotDataset(
            repo_id=f"local/{args.view.name}", root=args.view, episodes=[ep],
            delta_timestamps={**{k: [i / fps for i in range(0, HORIZON + 1, 4)] for k in camera_keys},
                              "action": [i / fps for i in range(HORIZON)]},
            video_backend="pyav", return_uint8=False,
        )
        for offset in anchor_indices(lengths[ep]):
            sample = ds[offset]
            if int(sample["episode_index"]) != ep or int(sample["frame_index"]) != offset:
                raise SystemExit(f"anchor mismatch at ep {ep} frame {offset}")
            raw_gt = sample["action"].float().numpy().copy()
            pad = sample["action_is_pad"].numpy().astype(bool).copy()
            state = sample["observation.state"].float().numpy().copy()

            t0 = time.time()
            raw_pred = predict(sample)
            elapsed = time.time() - t0

            if args.isolation_check and not isolation:
                future_masked = predict(sample, video_frames="first-only")
                label_swapped = dict(sample)
                label_swapped["action"] = torch.zeros_like(sample["action"])
                label_zeroed = predict(label_swapped)
                state_swapped = dict(sample)
                state_swapped["observation.state"] = sample["observation.state"] + 100.0
                state_moved = predict(state_swapped)
                isolation = {
                    "anchor": {"episode": ep, "frame": offset},
                    "max_abs_diff_future_frames_zeroed": float(
                        np.abs(future_masked - raw_pred).max()),
                    "max_abs_diff_label_zeroed": float(np.abs(label_zeroed - raw_pred).max()),
                    # Positive control: the state IS an input, so this one must be
                    # large. If it were zero, the model would be ignoring the pose.
                    "max_abs_diff_state_shifted": float(np.abs(state_moved - raw_pred).max()),
                }
            if not np.isfinite(raw_pred).all():
                raise SystemExit(f"non-finite prediction at ep {ep} frame {offset}")

            preds.append(raw_pred); gts.append(raw_gt); pads.append(pad); states.append(state)
            eps_out.append(ep); anchors_out.append(offset)
            rows.append({"episode": ep, "frame": offset, "seconds": round(elapsed, 3),
                         "padded_steps": int(pad.sum())})

    np.savez_compressed(args.out, pred=np.stack(preds), gt=np.stack(gts), pad=np.stack(pads),
                        state=np.stack(states), episode=np.asarray(eps_out),
                        anchor=np.asarray(anchors_out))
    report = {
        "kind": "ab-anchor-predictions",
        "label": args.label,
        "checkpoint": str(args.checkpoint),
        "role": args.role,
        "device": args.device,
        "action_dim": int(cfg.action_dim),
        "proprio_dim": int(cfg.proprio_dim),
        "use_relative_actions": bool(cfg.use_relative_actions),
        "relative_exclude_joints": list(cfg.relative_exclude_joints or []),
        "action_feature_names": list(cfg.action_feature_names or []),
        "relative_step_bound": relative_step is not None,
        "camera_keys": camera_keys,
        "image_size": list(cfg.image_size),
        "horizon": HORIZON,
        "noise_seed": SEED,
        "episodes": episodes,
        "num_anchors": len(preds),
        "wall_seconds": round(time.time() - started, 1),
        "per_anchor": rows,
        "isolation": isolation,
        "app_contract": app_contract,
    }
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in
                      ("label", "role", "num_anchors", "use_relative_actions",
                       "relative_step_bound", "wall_seconds")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
