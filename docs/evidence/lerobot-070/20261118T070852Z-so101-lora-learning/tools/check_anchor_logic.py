"""Exercise the anchor / dataset code path of `collect_predictions.py` on CPU.

`collect_predictions.py` cannot run without the 6B weights, but the part most
likely to break is the one before the model: which frame each anchor points at,
whether a per-episode `LeRobotDataset(episodes=[ep])` really returns that frame,
and whether the chunk/pad/mask shapes are what the scorer assumes. That part
runs fine on any local v3 dataset.

This checker imports the *real* functions from `collect_predictions` (not a
copy), so a fix here is a fix there.

Usage:
    python check_anchor_logic.py --view <local dataset root> --out anchor-check.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch

import collect_predictions as cp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--view", required=True, type=Path)
    ap.add_argument("--episodes", default="")
    ap.add_argument("--horizon", type=int, default=cp.HORIZON)
    ap.add_argument("--tail-anchor", action="store_true",
                    help="also read the last frame of the first episode to exercise tail padding")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    info = json.loads((args.view / "meta/info.json").read_text())
    fps = float(info["fps"])
    camera_keys = sorted(k for k in info["features"] if k.startswith("observation.images."))
    action_dim = int(info["features"]["action"]["shape"][0])
    state_dim = int(info["features"]["observation.state"]["shape"][0])

    lengths = cp._episode_lengths(args.view)
    episodes = ([int(x) for x in args.episodes.split(",") if x.strip()]
                if args.episodes else sorted(lengths)[:3])

    checks: dict = {}
    rows = []
    # Anchor rule is arithmetic and must be strictly increasing and in range.
    mismatched_anchors = []
    for ep in episodes:
        want = cp.anchor_indices(lengths[ep])
        if not (all(0 <= a < lengths[ep] for a in want) and want == sorted(set(want))):
            mismatched_anchors.append((ep, lengths[ep], want))
        rows.append({"episode": ep, "length": lengths[ep], "anchors": want})
    checks["anchors_in_range_and_increasing"] = not mismatched_anchors

    seen = []
    shapes_ok = True
    pad_ok = True
    images_ok = True
    detail = []
    for ep, offset, sample, _ds in cp.iter_anchor_samples(
        args.view, episodes, fps, camera_keys, args.horizon
    ):
        # iter_anchor_samples already asserts episode_index/frame_index; record them.
        seen.append((ep, offset, int(sample["frame_index"]), int(sample["episode_index"])))
        action = sample["action"]
        state = sample["observation.state"]
        pad = sample["action_is_pad"].numpy().astype(bool)
        if tuple(action.shape) != (args.horizon, action_dim) or tuple(state.shape) != (state_dim,):
            shapes_ok = False
        if tuple(pad.shape) != (args.horizon,):
            pad_ok = False
        images = {}
        for key in camera_keys:
            frame = sample[key]
            # `frame.dtype` is a torch dtype; comparing it to np.uint8 is always
            # unequal and would report a false failure.
            images[key] = {"shape": list(frame.shape), "dtype": str(frame.dtype)}
            expected_frames = len(range(0, args.horizon + 1, 4))
            # float32 [0,1] is the training-parity dtype; uint8 breaks the model's
            # bilinear resize inside _stack_video_from_images.
            if frame.shape[0] != expected_frames or frame.dtype != torch.float32:
                images_ok = False
        # A window that starts near the end must be padded, an interior one must not.
        remaining = lengths[ep] - offset
        expect_pad = remaining < args.horizon
        if bool(pad.any()) != expect_pad:
            pad_ok = False
        detail.append({
            "episode": ep,
            "frame": offset,
            "remaining_frames": remaining,
            "padded_steps": int(pad.sum()),
            "expect_pad": expect_pad,
            "action_shape": list(action.shape),
            "state_shape": list(state.shape),
            "images": images,
        })

    checks["episode_and_frame_match_request"] = all(
        ep == got_ep and off == got_frame for ep, off, got_frame, got_ep in seen
    )
    checks["chunk_shapes_ok"] = shapes_ok
    checks["pad_mask_present_and_explained"] = pad_ok
    checks["nine_video_frames_float"] = images_ok
    checks["anchors_unique"] = len({(e, f) for e, f, _, _ in seen}) == len(seen)

    tail = None
    if args.tail_anchor:
        # The four pre-registered anchors avoid the tail; this extra read proves
        # the padding branch works, because the scorer relies on action_is_pad.
        first = episodes[0]
        last = lengths[first] - 1
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        ds = LeRobotDataset(
            repo_id=f"local/{args.view.name}", root=args.view, episodes=[first],
            delta_timestamps={
                **{k: [i / fps for i in range(0, args.horizon + 1, 4)] for k in camera_keys},
                "action": [i / fps for i in range(args.horizon)],
            },
            video_backend="pyav", return_uint8=False,
        )
        sample = ds[last]
        pad = sample["action_is_pad"].numpy().astype(bool)
        tail = {"episode": first, "frame": last, "episode_length": lengths[first],
                "padded_steps": int(pad.sum()), "first_step_padded": bool(pad[0]),
                "frame_index": int(sample["frame_index"])}
        checks["tail_window_is_padded"] = 0 < int(pad.sum()) < args.horizon
        checks["tail_window_first_step_not_padded"] = not bool(pad[0])

    report = {
        "kind": "anchor-logic-check",
        "view": str(args.view),
        "fps": fps,
        "camera_keys": camera_keys,
        "action_dim": action_dim,
        "state_dim": state_dim,
        "horizon": args.horizon,
        "episodes": episodes,
        "planned_anchors": rows,
        "anchors_evaluated": len(seen),
        "checks": checks,
        "detail": detail,
        "tail": tail,
        "status": "passed" if all(checks.values()) else "failed",
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "checks": checks,
                      "anchors": [(d["episode"], d["frame"], d["padded_steps"]) for d in detail]},
                     indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
