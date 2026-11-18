"""Collect deterministic FastWAM prediction chunks on the frozen SO-101 anchors.

This is the only GPU-side evaluation step. It is deliberately dumb: the anchor
list, the noise seed and the candidate are given on the command line, and the
output `.npz` is scored by `score_predictions.py` with no model in the loop.

Anchor rule (pre-registered in EXPERIMENT.md §3): for each episode, take the
frames at 0, 1/4, 1/2 and 3/4 of its length. The same anchors and the same
seed are used for the random-head reference and for every trained candidate, so
differences come from weights, not from sampling.

Each episode gets its own `LeRobotDataset(episodes=[ep])`. Building one dataset
per episode avoids depending on how the loader orders a multi-episode subset:
with a single episode, index `i` is unambiguously frame `i`, and the returned
`episode_index` is asserted against the request.

Usage:
    python collect_predictions.py --view DIR --base-checkpoint CKPT \
        --role dev --out preds.npz --report collect.json [--adapter DIR] \
        [--episodes 0,50] [--device cuda]
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


def _episode_lengths(view: Path) -> dict[int, int]:
    import pyarrow.parquet as pq

    rows = pq.read_table(view / "meta/episodes/chunk-000/file-000.parquet").to_pylist()
    return {int(r["episode_index"]): int(r["length"]) for r in rows}


def anchor_indices(length: int, fractions=ANCHOR_FRACTIONS) -> list[int]:
    """Frame offsets used as evaluation anchors (pre-registered rule)."""
    if length <= 0:
        raise ValueError(f"episode length must be positive, got {length}")
    return [min(int(length * f), length - 1) for f in fractions]


def iter_anchor_samples(view: Path, episodes, fps: float, camera_keys, horizon: int,
                        video_backend: str = "pyav"):
    """Yield `(episode, frame_offset, sample, dataset)` for every anchor.

    One dataset is built per episode, so index `i` is unambiguously frame `i` of
    that episode and the returned `episode_index`/`frame_index` can be asserted
    against the request instead of trusting a multi-episode subset ordering.
    """
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    lengths = _episode_lengths(view)
    # FastWAM reads `num_video_frames = horizon + 1` frames at `action_video_freq_ratio`
    # stride, i.e. 9 frames spanning t=0..32 for a 32-step chunk. Off-by-one here
    # silently changes the model's temporal window.
    camera_slice = list(range(0, horizon + 1, 4))
    for ep in episodes:
        ep_len = lengths[int(ep)]
        ds = LeRobotDataset(
            repo_id=f"local/{view.name}",
            root=view,
            episodes=[int(ep)],
            delta_timestamps={
                **{k: [i / fps for i in camera_slice] for k in camera_keys},
                "action": [i / fps for i in range(horizon)],
            },
            video_backend=video_backend,
            return_uint8=True,
        )
        for offset in anchor_indices(ep_len):
            sample = ds[offset]
            got_ep = int(sample["episode_index"])
            if got_ep != int(ep):
                raise SystemExit(f"anchor lookup: asked episode {ep}, dataset returned {got_ep}")
            if int(sample["frame_index"]) != offset:
                raise SystemExit(
                    f"episode {ep}: asked frame {offset}, dataset returned {sample['frame_index']}"
                )
            yield int(ep), offset, sample, ds


def _load_policy(args, cfg):
    from lerobot.policies.fastwam import FastWAMPolicy

    base = FastWAMPolicy.from_pretrained(args.base_checkpoint, config=cfg)
    source = "base"
    adapter = args.adapter
    if adapter is None:
        # A run output directory may itself contain the adapter next to the
        # config; detect it rather than guessing from the path name.
        if (Path(args.base_checkpoint) / "adapter_config.json").is_file():
            adapter = Path(args.base_checkpoint)
    if adapter is not None:
        from peft import PeftModel

        base = PeftModel.from_pretrained(base, str(adapter), is_trainable=False)
        source = f"base+adapter({adapter})"
    base.eval()
    return base, source


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--view", required=True, type=Path)
    ap.add_argument("--base-checkpoint", required=True, type=Path)
    ap.add_argument("--adapter", type=Path, default=None)
    ap.add_argument("--role", choices=("train", "dev"), required=True)
    ap.add_argument("--episodes", default="", help="override: comma-separated episode indices")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--report", required=True, type=Path)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    from lerobot.lerobot_types import PolicyAction
    from lerobot.policies.fastwam import FastWAMConfig
    from lerobot.policies.fastwam.processor_fastwam import make_fastwam_pre_post_processors

    info = json.loads((args.view / "meta/info.json").read_text())
    fps = float(info["fps"])
    camera_keys = sorted(k for k in info["features"] if k.startswith("observation.images."))
    action_dim = int(info["features"]["action"]["shape"][0])
    state_dim = int(info["features"]["observation.state"]["shape"][0])

    splits = json.loads((args.view / "splits.json").read_text())
    if args.episodes:
        episodes = [int(x) for x in args.episodes.split(",") if x.strip()]
    else:
        episodes = sorted(int(e) for e in splits["train" if args.role == "train" else "validation"])
    lengths = _episode_lengths(args.view)

    cfg = FastWAMConfig(
        action_dim=action_dim,
        proprio_dim=state_dim,
        image_size=(224, 224 * len(camera_keys)),
        action_horizon=HORIZON,
        n_action_steps=HORIZON,
        dtype=torch.bfloat16,
        device=args.device,
    )
    cfg.set_dataset_feature_metadata(info["features"])

    raw_stats = json.loads((args.view / "meta/stats.json").read_text())
    stats = {
        key: {name: torch.tensor(value, dtype=torch.float32)
              for name, value in raw_stats[key].items()
              if name in {"mean", "std", "min", "max", "count"}}
        for key in ("observation.state", "action")
    }
    pre, post = make_fastwam_pre_post_processors(cfg, dataset_stats=stats)

    policy, source = _load_policy(args, cfg)
    device = torch.device(args.device)

    preds, gts, pads, states, eps_out, anchors_out = [], [], [], [], [], []
    per_anchor = []
    started = time.time()
    lengths = _episode_lengths(args.view)
    for ep, offset, sample, _ds in iter_anchor_samples(
        args.view, episodes, fps, camera_keys, HORIZON
    ):
        raw_gt = sample["action"].float().numpy().copy()
        pad = sample["action_is_pad"].numpy().astype(bool).copy()
        state = sample["observation.state"].float().numpy().copy()

        batch = pre(dict(sample))
        noise = torch.randn(
            1, HORIZON, action_dim,
            generator=torch.Generator().manual_seed(SEED),
            dtype=torch.float32,
        ).to(device)
        t0 = time.time()
        with torch.no_grad():
            chunk = policy.predict_action_chunk(batch, noise=noise)
        elapsed = time.time() - t0
        raw_pred = post(PolicyAction(chunk.float())).float().cpu().numpy()[0]

        preds.append(raw_pred)
        gts.append(raw_gt)
        pads.append(pad)
        states.append(state)
        eps_out.append(ep)
        anchors_out.append(offset)
        per_anchor.append({
            "episode": ep,
            "frame": offset,
            "episode_length": lengths[ep],
            "seconds": round(elapsed, 3),
            "padded_steps": int(pad.sum()),
            "pred_finite": bool(np.isfinite(raw_pred).all()),
            "pred_abs_max": float(np.abs(raw_pred).max()),
        })
        if not np.isfinite(raw_pred).all():
            raise SystemExit(f"non-finite prediction at episode {ep} frame {offset}")

    np.savez_compressed(
        args.out,
        pred=np.stack(preds),
        gt=np.stack(gts),
        pad=np.stack(pads),
        state=np.stack(states),
        episode=np.asarray(eps_out),
        anchor=np.asarray(anchors_out),
    )
    report = {
        "kind": "so101-anchor-predictions",
        "label": args.label,
        "role": args.role,
        "policy_source": source,
        "device": args.device,
        "action_dim": action_dim,
        "state_dim": state_dim,
        "camera_keys": camera_keys,
        "stacked_width": int(cfg.image_size[1]),
        "horizon": HORIZON,
        "noise_seed": SEED,
        "anchors_per_episode": list(ANCHOR_FRACTIONS),
        "episodes": episodes,
        "num_anchors": len(preds),
        "wall_seconds": round(time.time() - started, 1),
        "per_anchor": per_anchor,
    }
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in
                      ("label", "role", "policy_source", "num_anchors", "wall_seconds")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
