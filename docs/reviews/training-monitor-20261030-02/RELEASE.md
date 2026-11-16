# Training monitor, safe pause/resume, and read-only attach (2026-10-30)

This snapshot is a development release based on
`releases/sanitized-20261024-01`. It leaves that published snapshot and the
`current` symlink untouched. Activate it only through the normal release
process (for example, repoint `current` after review); do not overwrite the
published tree.

Follow-up snapshot `training-monitor-20261030-02` adds an SSH read-only attach
for a training process started outside LeLab. See
[`READONLY_MONITOR.md`](READONLY_MONITOR.md) for the local-only configuration
and attach steps. No trainer control action is exposed for an attached run.

## What is implemented

### 1. Structured, persistent monitoring

The trainer emits one-line JSON events (`@@LELAB_METRIC@@ ...`) containing:

| Event | Contents |
| --- | --- |
| `resolved_config` | actual policy/dataset/optimizer/scheduler configuration after `validate()`, the dataset split, parameter counts |
| `attempt_started` | absolute step, resumed flag, checkpoint path |
| `train` | real sampling step, train flow loss, LR (per param group), global pre-clip grad norm, samples/epochs, optional grouped losses |
| `validation` | independent validation loss, batch/sample counts, duration, RNG-isolated flag |
| `weight_norm` | global parameter L2 and trainable-only L2 (`sqrt(sum(p.float()**2))`, shared params deduped), timestamped overhead |
| `pause` | `saving` / `saved` / `failed`, checkpoint path, config hash |

NaN/Inf values are emitted as `null` and listed under `non_finite`, never
silently turned into zero. Events are persisted twice: in the ordinary job log
and in `<job_dir>/metrics/worker-events.jsonl`. LeLab normalizes them into an
append-only sidecar `<job_dir>/metrics/events.jsonl` with a monotonically
increasing cursor. History survives page refresh, network loss, browser tab
switches and LeLab service restarts. Legacy jobs without structured events
still fall back to their `step:... loss:...` text lines.

APIs:

* `GET /jobs/{id}/metrics-events?after=<cursor>&limit=&kinds=&attempt=`
* `GET /jobs/{id}/monitor` (capabilities, attempts, resolved config, pause
  checkpoint, validation status)
* `GET /jobs/{id}/metrics-history` (legacy-shaped points with the extra
  `kind/phase/attempt_id/event_id/validation_loss/weight_norm` fields)

`/jobs/{id}/metrics-history` no longer overwrites a repeated step: each point
carries its attempt id, so a pause/resume (or a deliberate rollback) is visible
rather than silently merged.

### 2. Effective learning rate and schedule

LeRobot's `validate()` used to replace `--optimizer.lr` with the policy preset.
This snapshot adds post-preset `optimizer_overrides` and
`scheduler_overrides` (unknown fields are rejected). LeLab sends requested
values through those overrides and displays both the requested and resolved
values on the monitoring page. Supported scheduler types:

* `cosine_decay_with_warmup`
* `cosine_annealing_with_warmup`
* `constant_with_warmup`

Resume is a minimal LeRobot invocation (`--resume true --config_path
<checkpoint>/pretrained_model --output_dir <job>/run`): optimizer, scheduler,
RNG, step and sampler state come from the checkpoint and are not silently
changed by the form.

### 3. Safe pause / resume (local and SSH single-process)

* Pause writes a bounded `pause.request` marker into the job's own `control/`
  directory; it never signals an arbitrary PID.
* The trainer checks the marker only after a completed optimizer update (and
  after logging/validation/periodic saves), writes the checkpoint into a hidden
  temporary directory, verifies required files, records SHA-256 hashes in
  `lelab_pause.json`, atomically publishes the checkpoint, then exits with code
  `75`.
* The control plane marks a job `paused` only after the checkpoint manifest
  verifies and the process has exited. A save failure is reported
  (`pause_failed`) and training continues; it is never reported as a pause.
* Resume verifies the checkpoint again (locally, or with a remote Python
  hashing script over SSH), starts a new attempt on the same job/output dir,
  and records `resumed_from_step`, the checkpoint path and config hash.
* Start/stop/pause/resume respect the existing local single-job mutex. Detached
  local workers write stdout to `stdout.log` (not a pipe) so they survive a
  LeLab restart; a restarted LeLab tails from a persisted byte offset and
  replays any events written while it was down.
* SSH operations verify the remote PID's cwd against the job directory before
  signalling or writing a pause marker; stop refuses foreign/reused PIDs.
* HF Cloud and imported models report pause/resume as unsupported instead of
  pretending.

Job states now include `pause_requested`, `paused`, `pause_failed` and
`resuming`, alongside `running/done/failed/interrupted`.

### 4. Validation split and SmolVLA diagnostics

* `dataset.eval_episodes` holds out an explicit validation episode list; every
  other loaded episode trains. For Scene1: validation `0..11`, train
  `12..63` (leave `dataset.episodes` empty so the train split is derived).
  `dataset.eval_split` remains available as a ratio alternative and cannot be
  combined with the explicit list.
* Validation runs under an RNG-isolated context (CPU/CUDA/Python/NumPy), so
  enabling it does not change the training trajectory. This was verified by
  comparing uninterrupted and pause/resumed runs bit-for-bit.
* Optional `action_loss_groups` (e.g. ten relative joints and two absolute
  grippers) adds per-group means of the **same masked flow loss**; it never
  changes the objective. This path is opt-in and is not exercised by the
  synthetic ACT test (no SmolVLA weights are available locally).

## Activation and commands

The development environment used for verification (isolated under `work/`):

```bash
cd <repo>
uv pip install --python work/training_contract_audit_20261027/env/bin/python \
  -e releases/training-monitor-20261030-01/app \
  -e releases/training-monitor-20261030-01/lerobot --no-deps
```

Frontend:

```bash
export PATH=<repo>/work/training-monitor-20261030/node/bin:$PATH
cd releases/training-monitor-20261030-01/app/frontend
npm ci
npm run lint     # changed files are clean; repo has pre-existing unrelated errors
npm run build    # updates dist/
```

Run the app (do not point it at real hardware for monitoring tests):

```bash
LELAB_OUTPUT_ROOT=<job root> \
HF_LEROBOT_HOME=<dataset cache> \
CUDA_VISIBLE_DEVICES='' \
python -m uvicorn lelab.server:app --host 127.0.0.1 --port 18080
```

Pause/resume from the UI: open `/training/<job_id>`, use **Pause** while
running and **Resume** once the status is `paused`. The page shows the pause
checkpoint, attempt boundaries, resolved/requested LR and all five metric
curves.

## Verification performed

All data is from the isolated CPU environment; this is not SmolVLA training,
convergence, simulation success or physical-robot validation.

| Check | Result |
| --- | --- |
| `app/tests` | 501 passed, 2 pre-existing environment/renderer failures (also fail on the published snapshot: missing `pyserial`, camera backend string) |
| New backend tests | metric store/pagination/legacy, command mapping, pause state machine/restart recovery, SSH runner (14 tests) |
| LeRobot targeted tests | 100 passed, 1 skipped (overrides, grouped-loss decomposition, NaN sanitization, checkpoint save/resume, train utils, normalization, batch preprocessing) |
| Trainer pause/resume vs uninterrupted | 6-step ACT, same seed: identical train/val losses, grad norms, LR schedule and final weight SHA-256; pause at step 4, resume to 6 |
| LeLab registry E2E | start → step 4 pause → verified checkpoint → resume as attempt 2 → step 6 done, one train event per step |
| Service restart | 60-step detached local run survived LeLab kill/restart; reattached same attempt, events 1..60 continuous, final done |
| Paused across restart | paused at step 6, restart → still paused with checkpoint, resume → attempt 2 → done at step 30 |
| Browser | all five metrics, 2 loss lines, 4 charts, pause/resume boundaries, resolved config, attempts, refresh and job switch verified via DOM; live Pause/Resume buttons exercised |
| SSH | protocol/ownership/control scripts covered by fake-SSH unit tests; no live server config (`servers.toml`) or deployment of this snapshot to a GPU host was available, so a real SSH pause/resume E2E was **not** run |

Evidence files are under `work/training-monitor-20261030/`:
`e2e_result.json`, `lelab_e2e_result.json`, `e2e_paused.log`,
`e2e_resumed.log`, `lelab_e2e.log`, and the browser/UI summaries.

## Known limits

* Pause is implemented for single-process, non-sharded `local` and
  `ssh_remote` jobs. Multi-GPU/sharded runs report pause as unsupported;
  weight norm is not reported for sharded models.
* The SSH trainer must be running this snapshot's LeRobot (new CLI fields and
  `LELAB_*` environment hooks). That deployment was deliberately not performed.
* SmolVLA grouped-loss instrumentation is code-complete but not exercised with
  real SmolVLA weights; Scene1 data/statistics and its frozen experiment were
  not modified.
* The generic LeLab path uses dataset metadata statistics; the Scene1
  experiment's train-only statistics remain the responsibility of its injected
  processor.
* Frontend lint reports pre-existing errors in unchanged files
  (`command.tsx`, `tailwind.config.ts`, `vite.config.ts` type-check).
* No commits, pushes, `current` switches, service deployment or changes to the
  protected Scene1 experiment were made.
