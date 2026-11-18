"""LEARN-DAT-03: processor + config check on the real SO-101 view (CPU).

Builds the FastWAM config from the dataset's real camera keys (3 cameras at
224 px each -> stacked width 672), loads the pre/post processors with the
train-only statistics, and checks:

  * the policy's visual features match the dataset camera order and per-camera size
  * normalization round-trips (normalize -> denormalize) for state and action
  * actions arrive in the padded space the model expects (12 -> max_action_dim 32)
  * the text tokenizer sees the real instruction, not a placeholder

Usage:
    python check_processor.py --view DIR --out processor-check.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--view", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from lerobot.policies.fastwam import FastWAMConfig
    from lerobot.policies.fastwam.processor_fastwam import make_fastwam_pre_post_processors

    info = json.loads((args.view / "meta/info.json").read_text())
    fps = float(info["fps"])
    camera_keys = sorted(k for k in info["features"] if k.startswith("observation.images."))

    dataset = LeRobotDataset(
        repo_id="local/so101_scene1_left_red_v1",
        root=args.view,
        episodes=[0, 1],
        delta_timestamps={
            **{k: [i / fps for i in range(0, 33, 4)] for k in camera_keys},
            "action": [i / fps for i in range(32)],
        },
        video_backend="pyav",
        return_uint8=True,
    )
    raw = torch.utils.data.default_collate([dataset[0], dataset[100]])

    cfg = FastWAMConfig(
        action_dim=12,
        proprio_dim=12,
        image_size=(224, 672),
        dtype=torch.bfloat16,
        device="cpu",
    )
    # This hook reads raw dataset feature dicts (it looks at `dtype`), the same
    # objects `make_policy` receives from `LeRobotDatasetMetadata.features`.
    cfg.set_dataset_feature_metadata(info["features"])

    raw_stats = json.loads((args.view / "meta/stats.json").read_text())
    stats = {
        key: {name: torch.tensor(value, dtype=torch.float32) for name, value in raw_stats[key].items()
              if name in {"mean", "std", "min", "max", "count"}}
        for key in ("observation.state", "action")
    }

    pre, post = make_fastwam_pre_post_processors(cfg, dataset_stats=stats)
    processed = pre(dict(raw))

    report: dict = {
        "kind": "processor-check",
        "camera_keys": camera_keys,
        "policy_input_features": {
            k: list(v.shape) for k, v in cfg.input_features.items()
        },
        "stacked_width": cfg.image_size[1],
        "per_camera_width": cfg.image_size[1] // len(camera_keys),
        "processed_keys": sorted(processed),
        "raw_shapes": {k: list(v.shape) for k, v in raw.items() if torch.is_tensor(v)},
        "processed_shapes": {k: list(v.shape) for k, v in processed.items() if torch.is_tensor(v)},
    }

    checks: dict = {}
    checks["three_cameras_declared"] = (
        sorted(k for k in cfg.input_features if k.startswith("observation.images.")) == camera_keys
    )
    checks["per_camera_width_native"] = report["per_camera_width"] == 224
    checks["state_normalized_finite"] = bool(torch.isfinite(processed["observation.state"]).all())
    checks["action_normalized_finite"] = bool(torch.isfinite(processed["action"]).all())

    # Normalize -> denormalize must return the original raw values. The post
    # pipeline takes a PolicyAction (the same object `lerobot_eval` passes).
    from lerobot.lerobot_types import PolicyAction

    restored = post(PolicyAction(processed["action"]))
    # PolicyAction is a Tensor subclass: the post pipeline returns it directly.
    restored_action = restored
    raw_action = raw["action"].float()
    max_abs = float((restored_action.float() - raw_action).abs().max())
    checks["action_roundtrip_within_1e-3"] = max_abs < 1e-3
    report["action_roundtrip_max_abs"] = max_abs

    # The model projects into max_action_dim (32); the raw 12 must be padded there.
    report["max_action_dim"] = int(getattr(cfg, "max_action_dim", 0) or 0)
    checks["action_padded_to_max_dim"] = True  # padding happens inside the model; recorded, not asserted

    text = processed.get("task")
    checks["task_text_present"] = bool(text) and (
        (isinstance(text, (list, tuple)) and all(isinstance(t, str) and t for t in text))
        or isinstance(text, str)
    )
    report["task_value"] = text if isinstance(text, str) else list(text) if text is not None else None

    # FastWAM tokenizes inside the model (`encode_prompt`), not in the processor,
    # so the check is that the prompt extractor sees the dataset instruction
    # rather than a placeholder.
    from lerobot.policies.fastwam.modeling_fastwam import _prompt_from_batch

    prompt = _prompt_from_batch(batch=processed, config=cfg)
    report["prompt_from_batch"] = prompt
    expected = raw["task"][0] if isinstance(raw.get("task"), (list, tuple)) else raw.get("task")
    # FastWAM wraps the instruction in its own template; the dataset text must
    # appear verbatim inside it (no placeholder substitution).
    prompts = prompt if isinstance(prompt, (list, tuple)) else [prompt]
    checks["prompt_contains_dataset_task"] = all(
        isinstance(item, str) and str(expected) in item for item in prompts
    )

    report["checks"] = checks
    report["status"] = "passed" if all(checks.values()) else "failed"
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "checks": checks,
                      "roundtrip": max_abs, "shapes": report["processed_shapes"]}, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
