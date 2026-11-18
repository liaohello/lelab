"""Parse a `lerobot_train` command line without training anything.

Three L3 attempts died on argument parsing (`--peft.lora_dropout` does not
exist, `--policy.path` expands a checkpoint directory into the dataclass,
`push_to_hub` left on). All three were detectable offline in a second, so this
preflight exists: it runs draccus over the exact argv and prints the resolved
config. It never builds a model, opens a dataset or touches the GPU.

Usage:
    python preflight_train_args.py --policy.type=fastwam --policy.pretrained_path=... \
        --peft.r=16 ...
Exit code 0 means the argv is valid; non-zero means the trainer would have died
before its first step.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    import draccus

    # The `--policy.type` choices come from the policy registry, which is only
    # populated once `lerobot.policies` is imported. Importing `configs.train`
    # alone yields an empty choice list and every type looks invalid.
    import lerobot.policies  # noqa: F401
    from lerobot.configs.train import TrainPipelineConfig

    argv = sys.argv[1:]
    try:
        cfg = draccus.parse(config_class=TrainPipelineConfig, args=argv)
    except SystemExit as exc:            # draccus/argparse error path
        print(json.dumps({"parsed": False, "exit": exc.code}, indent=2))
        return int(exc.code or 2)
    except Exception as exc:  # noqa: BLE001 - the message is the point
        print(json.dumps({"parsed": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 2

    policy = cfg.policy
    summary = {
        "parsed": True,
        "policy_type": getattr(policy, "type", None),
        "pretrained_path": str(getattr(policy, "pretrained_path", None)),
        "use_peft": bool(getattr(policy, "use_peft", False)),
        "peft": {
            "method_type": cfg.peft.method_type if cfg.peft else None,
            "r": cfg.peft.r if cfg.peft else None,
            "lora_alpha": cfg.peft.lora_alpha if cfg.peft else None,
            "target_modules": cfg.peft.target_modules if cfg.peft else None,
        },
        "action_dim": getattr(policy, "action_dim", None),
        "proprio_dim": getattr(policy, "proprio_dim", None),
        "image_size": list(getattr(policy, "image_size", ()) or ()),
        "action_horizon": getattr(policy, "action_horizon", None),
        "n_action_steps": getattr(policy, "n_action_steps", None),
        "freeze_video_expert": getattr(policy, "freeze_video_expert", None),
        "push_to_hub": bool(getattr(policy, "push_to_hub", False)),
        "dataset": {
            "repo_id": cfg.dataset.repo_id,
            "root": str(cfg.dataset.root),
            "episodes": (len(cfg.dataset.episodes) if cfg.dataset.episodes else None),
        },
        "batch_size": cfg.batch_size,
        "num_workers": cfg.num_workers,
        "steps": cfg.steps,
        "save_freq": cfg.save_freq,
        "seed": cfg.seed,
        "output_dir": str(cfg.output_dir),
        "job_name": cfg.job_name,
    }
    print(json.dumps(summary, indent=2))
    # The checks below are the ones the failed attempts would have tripped.
    problems = []
    if summary["use_peft"] and not summary["pretrained_path"]:
        problems.append("use_peft requires a pretrained_path")
    if summary["policy_type"] == "fastwam" and (summary["action_dim"] or 0) in (0, None):
        problems.append("fastwam needs an explicit action_dim")
    if problems:
        print(json.dumps({"problems": problems}, indent=2))
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
