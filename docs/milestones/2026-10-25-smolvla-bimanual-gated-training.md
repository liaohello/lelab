# SmolVLA bimanual gated training: stopped at the first checkpoint

Recorded: 2026-10-25, America/Los_Angeles.

## Result and scope

A single independently initialized run was planned for at most 2,000 updates
from the pretrained base. The run **stopped after 250 updates** because the first
scheduled checkpoint evaluation triggered a pre-registered gripper-extrapolation
rule. This is an early-stopped offline experiment, not a completed 2,000-update
run. No second run was started and no hyperparameter sweep was performed.

The tested checkpoint received decision **B** relative to the base and constant
baselines, but it was excluded from candidate selection by the same gate. No
stage-4 checkpoint qualified as the best offline candidate. The retained
reference remains the earlier 500-update pilot.

This is offline action prediction on a development validation set. It is not an
independent test, simulation success rate, demonstrated task completion or
physical safety assessment.

## Metric clarification

The earlier event diagnostic counted every adjacent-step change inside
overlapping prediction windows. Those counts are retained but are not unique
episode events and are not task success.

A supplemental diagnostic now merges consecutive same-direction changes into
segments and matches them one-to-one within a fixed time tolerance. It is still
explicitly window-based: overlapping windows duplicate ground-truth segments,
and no stitched "real execution trajectory" is claimed. On the earlier 500-update
pilot this produced about 3-4% segment precision and 81-97% segment recall,
which describes fragmented predicted changes rather than controller quality.

The primary metric remains episode-equal per-dimension scaled and raw MAE over a
fixed 119-start panel. Distribution limits are training statistics, not
mechanical limits, and physical gripper direction has not been confirmed.

## Symmetric checkpoint gate

Both grippers are now monitored with the same fields: below-minimum and
above-maximum rates (episode-equal and pooled), negative and positive overshoot
magnitudes, segment errors, and merged-change diagnostics.

Before training, the following were fixed: the metric version, protocol hashes,
the 500-update pilot baseline, both per-gripper thresholds, the evaluation
schedule (every 250 updates), and continue/stop rules. After each checkpoint was
fully written, training blocked while a separate evaluation process scored the
fixed panel and a gate decision was read. Evaluation failure also stopped the
run. Synthetic tests covered continue, left/right single-point stops,
two-consecutive stops, evaluation failure, missing-right-gripper regression and
segment matching.

The run stopped at checkpoint 250 because left-gripper predictions below the
training minimum rose 18.6 percentage points relative to the pilot baseline,
while mean negative overshoot increased about fivefold. Right-gripper
extrapolation decreased over the same interval. The asymmetric movement is the
reason the old left-only rule was insufficient.

## Development-validation snapshot at 250 updates

Primary inference-noise seed 1234; scaled MAE uses training-action standard
deviations.

| Model | Episode-equal scaled MAE | Episode-equal raw MAE |
|---|---:|---:|
| Pretrained base, training-only statistics | 0.8259 | 19.1767 |
| Training action-mean baseline | 0.7336 | 17.9813 |
| Earlier 500-update pilot | 0.4471 | 10.1360 |
| Gated run, 250 updates | 0.5615 | 12.8551 |

The 250-update checkpoint improved over both baselines on all 10 development
episodes, but it remained worse than the earlier pilot. Under inference-noise
seeds 1234/2026/7 its raw MAE was 12.8551 / 13.0658 / 13.0804. These noise seeds
are not independent training runs.

The checkpoint loaded strictly with 500/500 keys, matched training-only
processor statistics, left frozen VLM tensors unchanged and produced finite
12-dimensional predictions. Its optimizer, scheduler, RNG and step state were
saved.

## Limits and next work

- The run ended at the first scheduled checkpoint; no 500-2,000-update
  checkpoints exist and convergence was not evaluated.
- The development validation episodes informed model selection and are not an
  independent test set.
- The gate compares a mid-schedule checkpoint with a completed shorter pilot;
  the fixed rule stopped the run, and it was not relaxed afterwards.
- Merged segment precision remains low; predicted change segments are
  fragmented.
- Training-reserved GPU memory was not separately instrumented.
- No simulation, controller execution, robot task success or hardware safety
  was evaluated.

The next minimal step would be a separately authorized protocol review before
any further training. This milestone does not initiate one.

## References

- [SmolVLA documentation](https://huggingface.co/docs/lerobot/smolvla)
- [ARMNetBench transfer-cube dataset](https://huggingface.co/datasets/villekuosmanen/armnetbench_transfer_cube)

Full weights, source datasets, private connection details and raw runtime logs
remain outside this repository.
