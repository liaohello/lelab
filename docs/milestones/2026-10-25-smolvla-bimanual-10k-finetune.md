# SmolVLA bimanual 10,000-update fine-tune

Recorded: 2026-10-25, America/Los_Angeles.

## Scope

This milestone records one independent offline fine-tuning run of the bimanual
SmolVLA pipeline. The run started from the previously validated train-only 12D
base, used fixed training episodes 0-39 and development validation episodes
40-49, and completed 10,000 updates. The validation episodes had already been
used for development and selection, so they are not an independent test set.

No simulation closed loop, physical robot command, serial connection, torque
enable or emergency-stop interaction was performed. Model weights, datasets,
private runtime configuration and raw server logs are not published.

## Execution

The formal run was pre-registered before training with fixed data, statistics,
base weights, source hashes, metrics, candidate rules and stop rules. The
effective configuration was batch 4, accumulation 1, bf16, frozen VLM, existing
action-expert training scope, peak LR 1e-4, floor 1e-5, warmup 500, decay
10,000, gradient clipping 10, seed 1000, and full checkpoints every 1,000
updates.

Two GPU-ownership conflicts triggered the pre-registered immediate stop rule:

| Stop | Update | Exit code | Resolution |
|---|---:|---:|---|
| 1 | 3,000 | 14 | preserved evidence; same run later resumed |
| 2 | 6,000 | 14 | preserved evidence; same run later resumed |

No other process was stopped or shared. The run resumed from its own checkpoints
and completed 10,000 updates with exit code 0. The final checkpoint passed
strict reload (500/500 keys), train-only processor statistics, unchanged frozen
VLM tensors (0 of 345), changed action-expert tensors, and training step 10,000.

The runner script was edited after the pre-registration freeze to fix logging
and failure-summary handling. Its executed hash therefore differs from the
pre-registration; the training configuration, data, statistics, metrics, stop
rules and checkpoint hook were unchanged. This deviation is recorded in the
full experiment report.

## Offline results

Episode-equal metrics on 119 fixed 50-step development windows. Lower is better.

| Model | Scaled MAE | Raw MAE |
|---|---:|---:|
| T11 base | 0.8259 | 19.1767 |
| Static mean | 0.7336 | 17.9813 |
| Earlier 500-update pilot | 0.4471 | 10.1360 |
| This run, update 5,000 | 0.3324 | 7.4048 |
| This run, update 10,000 | **0.3105** | **6.8727** |

The final update improved scaled MAE by 30.6% and raw MAE by 32.2% relative to
the earlier pilot. Most of the remaining gain arrived before 8,000 updates:
8,000 to 10,000 changed scaled MAE by only -0.06% and raw MAE by +0.13%.
A 9,000-update checkpoint showed a small regression, then 10,000 recovered.

Three candidate checkpoints were re-evaluated with three inference seeds each:

| Update | Role | Mean scaled MAE | Range |
|---:|---|---:|---|
| 10,000 | best main metric | 0.3110 | 0.3105-0.3116 |
| 7,000 | best error/out-of-range balance | 0.3229 | 0.3225-0.3232 |
| 5,000 | best gripper dynamics | 0.3319 | 0.3305-0.3328 |

The final checkpoint happened to be the best main-metric candidate, but it was
selected by the pre-registered rule rather than assumed best by default.

## Gripper behavior

Training reduced, but did not eliminate, the earlier extrapolation and
fragmentation:

- Left gripper below-training-min rate: 27.98% for the base model and 15.56% for
  the earlier pilot, down to 0.19% at 10,000 updates. Right gripper: 21.46% for
  the pilot, down to 2.26%. Validation ground truth itself contained no
  out-of-range gripper values, so these rates measure model extrapolation.
- Left total variation relative to ground truth: 4.63x for the pilot, 2.13x at
  10,000. Right: 4.94x to 2.50x.
- Left predicted change-segment count: 2,191 for the pilot, 698 at 10,000,
  versus 91 ground-truth segments. Right: 2,529 to 1,197, versus 104.
- Merged-event precision at 10,000 was only 8.7% (left) and 6.4% (right), with
  recall 67.0% and 74.0%. Matched events were close in time (about 1 step), but
  many predicted events were spurious.
- Right-gripper dynamic-segment errors remained higher than left, and matched
  right events had a median amplitude ratio of 0.44 versus ground truth.

These are offline numerical diagnostics, not execution success or physical
gripper jitter.

## Legacy gate and readiness

The original A gate was reported without relaxation. The main metric was below
both the base and static mean, all 10 validation episodes were lower than both,
the key-dimension check passed, and the left below-min increase stayed within
the allowed range. However, the dynamic gripper segments were not below the
static segment error, so the original A gate was **not met**.

The 10,000-update checkpoint is therefore an offline exploratory candidate. It
does not receive the original A qualification, simulation-success credit or
physical-robot eligibility.

## Simulation assessment

No closed-loop simulation was run because no matching asset was available. The
existing dual-arm MuJoCo project differed in:

- control rate: 50 Hz policy protocol versus 20 Hz dataset;
- observations: central/overview cameras versus top/left-wrist/right-wrist;
- task and success metric: P3/P4 classification/transfer versus the transfer_cube
  task;
- action semantics: joint units, gripper mapping, home offsets, limits and
  calibration chain not verified against the dataset;
- environment: no importable MuJoCo in the training environment.

Substituting a different robot or task was outside the pre-registered scope.
Real-time chunking (RTC) was not evaluated; it remains a later same-weights
comparison of synchronous, asynchronous and RTC execution.

## Interpretation

More training clearly improved offline validation through 8,000 updates, with
diminishing returns afterward. The remaining errors are structured rather than
uniform: the right arm, contact/transfer phases and gripper dynamics dominate
the worst windows. This points to data, action representation and timing
limitations rather than an obvious optimization failure. Evidence is still
missing for an independent test set, same-metric training-set error, simulation
success and physical hardware behavior.

Recommended next work: audit right-arm and gripper timing against the recorded
data, establish a matching 20 Hz three-camera simulation with a success
predicate, and only then compare synchronous, asynchronous and RTC execution
with identical weights and scenarios.
