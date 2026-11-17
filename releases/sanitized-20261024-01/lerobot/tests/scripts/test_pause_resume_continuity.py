#!/usr/bin/env python

# Copyright 2026 The LeLab authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Cooperative pause/resume continuity on a real CPU training run (TC-CKP-02/03).

FakeRunner state machines prove the app's bookkeeping; they cannot prove the
trainer really stops at an optimizer-update boundary, publishes a complete
checkpoint, and resumes to the same parameters. This module drives the real
``lerobot_train`` entry point twice on a tiny ACT policy over a real on-disk
dataset and compares:

  * run A: 4 consecutive optimizer updates
  * run B: 2 updates → pause request → restart → 2 more updates

The final parameters, optimizer/scheduler state and step counters must agree.

LeLab 说明：这里用真实训练入口跑 4 步（连续）与 2+暂停+2 的对照，验证暂停发生在
优化更新边界、checkpoint 完整且续训后参数/优化器/调度器/step 一致。FakeRunner
只能证明状态机，不能证明这件事。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import torch

pytest.importorskip("datasets", reason="datasets is required (install lerobot[dataset])")
pytest.importorskip("accelerate", reason="accelerate is required to run the training entry point")

from lerobot.datasets.lerobot_dataset import LeRobotDataset

pytestmark = [
    pytest.mark.dataset,
    pytest.mark.slow,
]

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"

FEATURES = {
    # ACT's validate_features() demands an image or an ENV-typed input; a bare
    # observation.state is STATE-typed and is rejected. Staying image-free keeps
    # the run CPU-cheap, so the tiny dataset carries an environment_state token.
    "observation.state": {"dtype": "float32", "shape": (2,), "names": None},
    "observation.environment_state": {"dtype": "float32", "shape": (2,), "names": None},
    "action": {"dtype": "float32", "shape": (2,), "names": None},
}

# Tiny transformer so four CPU updates take seconds, not minutes.
TINY_ACT = (
    "--policy.type=act",
    "--policy.dim_model=16",
    "--policy.n_heads=2",
    "--policy.dim_feedforward=32",
    "--policy.n_encoder_layers=1",
    "--policy.n_decoder_layers=1",
    "--policy.chunk_size=2",
    "--policy.n_action_steps=1",
    "--policy.device=cpu",
    # 0.7 keeps push_to_hub on the *policy* config; validate() would otherwise
    # demand a repo_id and refuse to start this purely local smoke run.
    "--policy.push_to_hub=false",
)


def _write_dataset(root: Path, episodes: int = 4, frames: int = 5) -> Path:
    ds_root = root / "local" / "pausetiny"
    ds = LeRobotDataset.create(
        repo_id="local/pausetiny", fps=10, features=FEATURES, root=ds_root, use_videos=False
    )
    generator = torch.Generator().manual_seed(0)
    for _ in range(episodes):
        for _ in range(frames):
            ds.add_frame(
                {
                    "observation.state": torch.randn(2, generator=generator),
                    "observation.environment_state": torch.randn(2, generator=generator),
                    "action": torch.randn(2, generator=generator),
                    "task": "tiny task",
                }
            )
        ds.save_episode()
    ds.finalize()
    return ds_root


def _train_env(tmp_path: Path, control_dir: Path | None, run_id: str) -> dict[str, str]:
    """Isolated env: no PYTHONPATH surprises, thread-capped BLAS, own HF home."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(
        {
            "PYTHONPATH": str(SRC_ROOT),
            "HF_LEROBOT_HOME": str(tmp_path / "hf"),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "LELAB_RUN_ID": run_id,
            "LELAB_ATTEMPT_ID": "1",
            "WANDB_MODE": "disabled",
            # Deterministic CPU maths: fixed seed plus no TF32/cudnn variance.
            "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
        }
    )
    if control_dir is not None:
        env["LELAB_CONTROL_DIR"] = str(control_dir)
    return env


def _base_argv(ds_root: Path, output_dir: Path, steps: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "lerobot.scripts.lerobot_train",
        f"--dataset.repo_id=local/pausetiny",
        f"--dataset.root={ds_root}",
        *TINY_ACT,
        "--batch_size=2",
        "--num_workers=0",
        "--persistent_workers=false",
        "--seed=0",
        "--steps",
        str(steps),
        "--save_freq=0",
        "--log_freq=1",
        "--eval_steps=0",
        "--env_eval_freq=0",
        f"--output_dir={output_dir}",
        # 0.7 replaced W&B with Trackio: there is no --wandb.* family any more,
        # so the tracker must be switched off through --trackio.enable.
        "--trackio.enable=false",
    ]


def _run(argv: list[str], env: dict[str, str], cwd: Path, timeout: int = 900):
    return subprocess.run(
        argv,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run_and_pause_after(argv, env, cwd, control_dir: Path, after_step: int, timeout: int = 900):
    """Run the trainer and drop the pause marker once it has logged `after_step` updates.

    The trainer only reads the marker at an update boundary, so the marker's arrival
    time decides *which* boundary is used. Writing it after N logged updates makes the
    pause land as close to step N as the process scheduling allows; the returned
    `paused_step` is read back from the emitted event, never assumed.
    """
    process = subprocess.Popen(
        argv,
        env=env,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    seen_steps: list[int] = []
    events: list[dict] = []
    requested = False
    assert process.stdout is not None
    for line in process.stdout:
        if "@@LELAB_METRIC@@" not in line:
            continue
        event = json.loads(line.split("@@LELAB_METRIC@@", 1)[1])
        events.append(event)
        if event.get("kind") == "train":
            seen_steps.append(int(event["step"]))
            if not requested and len(seen_steps) >= after_step:
                (control_dir / "pause.request").write_text("{}", encoding="utf-8")
                requested = True
    stderr = process.stderr.read() if process.stderr is not None else ""
    process.wait(timeout=timeout)
    return {
        "returncode": process.returncode,
        "stdout": "",
        "stderr": stderr,
        "events": events,
        "steps_seen": seen_steps,
    }


def _load_state(checkpoint_dir: Path) -> dict:
    """Load the 0.7 training_state payload plus the model weights.

    0.7 replaced the pickled ``optimizer_state.pt``/``scheduler_state.pt`` pair with a
    single safetensors blob keyed ``state/<group_param_index>/<field>``, and it keeps
    the learning-rate/weight-decay knobs in ``optimizer_param_groups.json``. ACT has no
    scheduler preset, so there is no scheduler state to compare here.
    """
    state_dir = checkpoint_dir / "training_state"
    step_payload = json.loads((state_dir / "training_step.json").read_text(encoding="utf-8"))
    from safetensors.torch import load_file

    model = load_file(checkpoint_dir / "pretrained_model" / "model.safetensors")
    optimizer = load_file(state_dir / "optimizer_state.safetensors")
    param_groups = json.loads((state_dir / "optimizer_param_groups.json").read_text(encoding="utf-8"))

    moments: dict[int, dict[str, torch.Tensor]] = {}
    for key, tensor in optimizer.items():
        head, index, field = key.split("/")
        assert head == "state", f"unexpected optimizer key {key!r}"
        moments.setdefault(int(index), {})[field] = tensor

    scheduler_path = state_dir / "scheduler_state.pt"
    scheduler = (
        torch.load(scheduler_path, map_location="cpu", weights_only=False)
        if scheduler_path.is_file()
        else None
    )
    return {
        "step": step_payload,
        "model": model,
        "param_groups": param_groups,
        "state": moments,
        "scheduler": scheduler,
    }


def _assert_same_state(left: dict, right: dict) -> None:
    assert left["step"] == right["step"], f"step counters differ: {left['step']} vs {right['step']}"
    assert set(left["model"]) == set(right["model"])
    for key, value in left["model"].items():
        torch.testing.assert_close(value, right["model"][key], rtol=1e-6, atol=1e-7, msg=f"model::{key}")
    # Optimizer: same number of param groups, identical knobs, identical moments.
    left_groups, right_groups = left["param_groups"], right["param_groups"]
    assert len(left_groups) == len(right_groups)
    for lg, rg in zip(left_groups, right_groups, strict=True):
        assert lg["lr"] == pytest.approx(rg["lr"]), "learning rate diverged across the pause"
        assert lg["weight_decay"] == pytest.approx(rg["weight_decay"])
        assert lg["params"] == rg["params"], "param-group membership diverged across the pause"
    assert set(left["state"]) == set(right["state"]), "optimizer touched a different parameter set"
    for index in left["state"]:
        assert set(left["state"][index]) == set(right["state"][index])
        for field, tensor in left["state"][index].items():
            if torch.is_tensor(tensor):
                torch.testing.assert_close(
                    tensor, right["state"][index][field], rtol=1e-6, atol=1e-7,
                    msg=f"optimizer::{index}::{field}",
                )
    if left["scheduler"] is not None and right["scheduler"] is not None:
        assert left["scheduler"].get("last_epoch") == right["scheduler"].get("last_epoch")


@pytest.fixture()
def dataset_root(tmp_path: Path) -> Path:
    return _write_dataset(tmp_path)


def test_pause_at_update_boundary_matches_an_uninterrupted_run(
    tmp_path: Path, dataset_root: Path
) -> None:
    """N continuous updates == pause after 2 + resume for the remaining updates.

    The run is deliberately longer than the pause point. The cooperative pause is
    observed at the *next* update boundary after the marker is written (the trainer
    logs step N before it checks for a pause), so with ``steps=4`` a pause requested
    after two logged updates could land on the final step and resume would perform
    zero optimizer updates — a false pass. Six steps leave at least three updates
    after any early pause, and the assertions below require a real post-resume
    ``train`` event rather than inferring continuity from the final step counter.
    """
    total_steps = 6
    pause_after = 2
    # --- reference: all updates in one process ---------------------------------------------
    ref_out = tmp_path / "run-continuous"
    ref = _run(
        _base_argv(dataset_root, ref_out, steps=total_steps),
        _train_env(tmp_path, None, "continuous"),
        tmp_path,
    )
    assert ref.returncode == 0, f"continuous run failed:\n{ref.stdout[-4000:]}\n{ref.stderr[-4000:]}"
    ref_state = _load_state(ref_out / "checkpoints" / "last")
    assert ref_state["step"]["step"] == total_steps

    # --- interrupted: ask for a pause once two updates have been logged --------------------
    first_out = tmp_path / "run-paused"
    control = tmp_path / "control"
    control.mkdir()
    first = _run_and_pause_after(
        _base_argv(dataset_root, first_out, steps=total_steps),
        _train_env(tmp_path, control, "paused"),
        tmp_path,
        control,
        after_step=pause_after,
    )
    assert first["returncode"] == 75, (
        f"expected pause exit code 75, got {first['returncode']}:\n{first['stderr'][-4000:]}"
    )

    # The pause must have been taken at a completed update, and the checkpoint it
    # published must be complete and self-consistent.
    pause_events = [event for event in first["events"] if str(event.get("kind", "")).startswith("pause")]
    assert pause_events, f"no pause events: {[e.get('kind') for e in first['events']]}"
    saved = [event for event in pause_events if event.get("state") == "saved"]
    assert saved, f"pause never reached 'saved': {pause_events}"
    paused_step = int(saved[-1]["step"])
    # The pause lands at a completed update boundary strictly before the end of the
    # run, so resume is guaranteed to have updates left to perform.
    assert pause_after <= paused_step < total_steps, (
        f"pause landed at step {paused_step} of {total_steps}: {first['steps_seen']}"
    )

    pause_ckpt = Path(saved[-1]["checkpoint"])
    manifest = json.loads((pause_ckpt / "lelab_pause.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is True, manifest
    assert not manifest["missing"], manifest["missing"]
    assert manifest["step"] == paused_step

    # --- resume for the remaining updates --------------------------------------------------
    # 0.7 dropped --checkpoint_path: --resume now takes the checkpoint's own
    # train_config.json through --config_path and reads the weights/optimizer
    # from the sibling training_state. The flags after --config_path re-assert the
    # run shape, since resuming otherwise reuses the checkpoint's config verbatim.
    resumed = _run(
        [
            *_base_argv(dataset_root, first_out, steps=total_steps),
            "--resume=true",
            f"--config_path={pause_ckpt / 'pretrained_model' / 'train_config.json'}",
        ],
        _train_env(tmp_path, control, "resumed"),
        tmp_path,
    )
    assert resumed.returncode == 0, f"resume failed:\n{resumed.stdout[-4000:]}\n{resumed.stderr[-4000:]}"
    # Require at least one actual optimizer update after resume; a matching final
    # step counter alone cannot prove that the resumed process did any work.
    resumed_steps = [
        int(json.loads(line.split("@@LELAB_METRIC@@", 1)[1])["step"])
        for line in resumed.stdout.splitlines()
        if "@@LELAB_METRIC@@" in line
        and json.loads(line.split("@@LELAB_METRIC@@", 1)[1]).get("kind") == "train"
    ]
    assert resumed_steps and min(resumed_steps) > paused_step, (
        f"resume reported no post-pause update: steps={resumed_steps}, paused={paused_step}"
    )
    resumed_state = _load_state(first_out / "checkpoints" / "last")
    assert resumed_state["step"]["step"] == total_steps

    # The uninterrupted run must have taken the same number of optimizer updates from the
    # same initial weights; only then is a parameter comparison meaningful.
    assert ref_state["step"]["step"] == resumed_state["step"]["step"]

    # --- the interrupted run must end where the continuous run did -------------------------
    _assert_same_state(ref_state, resumed_state)


def test_pause_never_publishes_an_incomplete_checkpoint(tmp_path: Path, dataset_root: Path) -> None:
    """The checkpoint directory behind a 'saved' event must be complete on disk."""
    out = tmp_path / "run-atomic"
    control = tmp_path / "control"
    control.mkdir()
    result = _run_and_pause_after(
        _base_argv(dataset_root, out, steps=3),
        _train_env(tmp_path, control, "atomic"),
        tmp_path,
        control,
        after_step=1,
    )
    assert result["returncode"] == 75, result["stderr"][-3000:]

    saved = [
        event
        for event in result["events"]
        if event.get("kind") == "pause" and event.get("state") == "saved"
    ]
    assert saved
    checkpoint_dir = Path(saved[-1]["checkpoint"])
    # Both halves of a usable checkpoint are present, and no staging directory leaked.
    assert (checkpoint_dir / "pretrained_model" / "model.safetensors").is_file()
    assert (checkpoint_dir / "training_state" / "training_step.json").is_file()
    staging = sorted(p.name for p in checkpoint_dir.parent.glob(".pause-tmp-*"))
    assert staging == [], f"staging directories left behind: {staging}"


def test_resume_rejects_a_corrupted_pause_checkpoint(tmp_path: Path, dataset_root: Path) -> None:
    """A tampered checkpoint must be rejected by the manifest check that gates resume.

    The hash verification lives in the app's pause path (`verify_local_pause_checkpoint`
    → `lelab_monitor.manifest_matches`); the trainer itself trusts the checkpoint it is
    pointed at. This test drives the real trainer to produce a real pause checkpoint,
    then proves the integrity gate refuses it once a byte changes — so a corrupt
    checkpoint can never reach the trainer through the pause/resume flow.
    """
    out = tmp_path / "run-corrupt"
    control = tmp_path / "control"
    control.mkdir()
    first = _run_and_pause_after(
        _base_argv(dataset_root, out, steps=3),
        _train_env(tmp_path, control, "p1"),
        tmp_path,
        control,
        after_step=1,
    )
    assert first["returncode"] == 75, first["stderr"][-3000:]

    pause_ckpt = out / "checkpoints" / "last"
    manifest = json.loads((pause_ckpt / "lelab_pause.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is True

    from lerobot.utils.lelab_monitor import manifest_matches

    ok, reason = manifest_matches(pause_ckpt, manifest)
    assert ok, f"a freshly written pause checkpoint did not verify: {reason}"

    # Flip one byte inside the model weights.
    weights = pause_ckpt / "pretrained_model" / "model.safetensors"
    payload = bytearray(weights.read_bytes())
    payload[len(payload) // 2] ^= 0xFF
    weights.write_bytes(bytes(payload))

    ok, reason = manifest_matches(pause_ckpt, manifest)
    assert not ok, "a tampered checkpoint passed the manifest check"
    # The gate must reject it for an integrity reason, not by accident.
    assert reason and ("sha256" in reason.lower() or "missing" in reason.lower()), reason
    assert "model.safetensors" in reason, reason

    # Deleting a required state file must be caught the same way.
    (pause_ckpt / "training_state" / "training_step.json").unlink()
    ok, reason = manifest_matches(pause_ckpt, manifest)
    assert not ok, "a checkpoint with a deleted state file passed the manifest check"
