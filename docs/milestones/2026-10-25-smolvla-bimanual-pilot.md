# SmolVLA bimanual offline pilot

Recorded: 2026-10-25, America/Los_Angeles. The retained experiment report dates
the pilot to 2026-10-24 in the same time zone.

## Result and scope

One independent 500-update training pilot completed with finite losses and
gradients, strict checkpoint loading, verified training-only normalization and
fresh-process reloads. The model received decision **B: learning trend, below the
pre-registered A criteria**.

This is offline action prediction on a development validation set. It is not an
independent test, simulation success rate, demonstrated robot task completion or
physical safety assessment. The publication is a progress record, not a portable
training release.

## Protocol

- Model: SmolVLA; controlled LeRobot 0.6.2 source.
- Dataset: ARMNetBench transfer-cube demonstrations, 50 episodes, 23,068 frames,
  20 Hz, 12-dimensional bimanual state/action, three camera views.
- Training episodes: 0–39; development validation episodes: 40–49.
- Normalization: state/action statistics computed exclusively from training rows.
- Evaluation: 119 fixed starting points covering all 10 validation episodes;
  padded target steps masked; episode-equal main metrics. Overlapping prediction
  windows are not independent samples.
- Training: batch 4, accumulation 1, bf16, frozen VLM, peak LR 1e-4,
  warmup 25, cosine schedule 500 updates, floor LR 1e-5, training seed 1000.
- Checkpoints: 250 and 500 updates; optimizer, scheduler, RNG and step state saved.
- Inference-noise seeds: 1234, 2026 and 7. These are not separate training seeds.

## Main development-validation results

Primary inference-noise seed: 1234. Scaled MAE uses training action standard
deviations. Raw MAE retains dataset units, whose physical interpretation still
requires confirmation.

| Model | Episode-equal scaled MAE | Episode-equal raw MAE |
|---|---:|---:|
| Pretrained base, training-only statistics | 0.8259 | 19.1767 |
| Training action-mean baseline | 0.7336 | 17.9813 |
| Pilot, 250 updates | 0.5141 | 11.7349 |
| Pilot, 500 updates | 0.4471 | 10.1360 |

The 500-update checkpoint improved raw MAE by approximately 47.1% relative to
the base and 43.6% relative to the constant baseline. All 10 validation episodes
improved. Its raw MAE under the three inference-noise seeds was
10.1360 / 10.2567 / 10.2182.

## Why the result remains B

Both grippers' numerical downward-change segments did not beat the constant
baseline: left MAE 14.16 versus 10.35; right 12.78 versus 11.84.

Right-gripper predictions below the training minimum rose from approximately 0%
for the base to 4.4% at 250 updates and 21.5% at 500 updates. Left-gripper
out-of-range predictions decreased from 32.1% to 15.6%. These are distribution
diagnostics over valid prediction-window steps, not mechanical-limit violations.

The existing event precision measurements use thresholded adjacent-step changes
inside overlapping windows. They are retained as development diagnostics and
must not be interpreted as unique episode-level gripper-event or task success.

## Integrity and next work

Both checkpoints loaded strictly with 500/500 keys, matching training-only
processor statistics and finite 12D predictions. Frozen VLM tensors remained
unchanged. Source snapshots and original datasets were not modified.

Next: retain the fixed protocol, add symmetric left/right gripper monitoring,
clarify event aggregation, and run at most one independently initialized
2000-update experiment with checkpoint review and early-stop conditions.
Robot units, action semantics, physical camera identity and limits remain to be
confirmed before any hardware execution.

## References

- [SmolVLA documentation](https://huggingface.co/docs/lerobot/smolvla)
- [ARMNetBench transfer-cube dataset](https://huggingface.co/datasets/villekuosmanen/armnetbench_transfer_cube)

Full weights, source datasets, private connection details and raw server logs
remain outside this repository.
