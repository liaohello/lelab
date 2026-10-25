# SmolVLA bimanual diagnostics: gate replay and inference-step check

Recorded: 2026-10-25, America/Los_Angeles.

## Scope

This milestone is a retrospective offline analysis of the earlier bimanual
SmolVLA pilot and the gated run that stopped after 250 updates. No training run
was performed, no optimizer was created, no weights were updated, and the stopped
run was not resumed or reclassified.

The work used saved predictions, a fixed 119-start development panel, fixed
training-only normalization, and a bounded 30-start no-gradient inference
comparison. It is not an independent test, simulation, task-success or hardware
evaluation.

## Gate replay

Using the original left-gripper single-point rule with the 500-update pilot as
the baseline, the rule would also have stopped the pretrained base and the
earlier 250-update pilot:

| Model | Left below-min change | Left mean negative overshoot | Rule |
|---|---:|---:|---|
| Pretrained base | +12.42 pp | 0.3043 | would stop |
| Earlier 250-update pilot | +15.92 pp | 0.3123 | would stop |
| Earlier 500-update pilot | 0 | 0.0965 | baseline |
| Stopped gated run, 250 updates | +18.62 pp | 0.5049 | stopped |

This replay does not restore the stopped checkpoint as a candidate. It shows that
the original rule is a conservative budget monitor relative to the best earlier
reference, not proof that only the stopped run suffered a catastrophic
regression.

At the same 250-update count, the stopped run had raw MAE 12.8551 versus 11.7349
for the earlier pilot, with higher left/right below-min rates and overshoot. The
learning-rate histories differed substantially (about 9.66e-5 versus 5.6e-5 at
that step), and sampler order was not logged, so this is an association rather
than a causal result.

## Gripper sequence findings

Across saved predictions, the learned models showed:

- a low-value/high-value compression bias: predictions above ground truth in the
  lowest training decile and below ground truth in the highest decile;
- negative overshoot concentrated in static ground-truth segments (about 96% of
  left overshoot mass and 90% of right overshoot mass for the 500-update pilot);
- much more fragmented trajectories than ground truth: total variation about
  4.6-7.4 times larger, second differences about 12-21 times larger, direction
  reversals about 36-62 times more frequent, and merged change segments around
  1.3 steps versus 3.7-4.3 for ground truth;
- material within-start sensitivity to inference noise (mean standard deviation
  about 2.3-2.8 data units across three fixed noise seeds), which is not training
  seed variance.

These are offline numerical diagnostics. Physical gripper units and open/close
semantics remain unconfirmed.

## Integration-step comparison

On the fixed 30-start panel, changing Euler integration from 10 to 20 steps did
not improve either checkpoint:

- raw MAE increased by about 0.4% for the 500-update pilot and 1.7-1.8% for the
  stopped run;
- only 1-4 of 10 episodes improved, depending on model and noise seed;
- negative overshoot and trajectory variation generally worsened;
- median inference latency increased by roughly 71-79%.

The runtime `num_steps` value was verified, checkpoint/config/processor hashes
were unchanged before and after, and all calls used no-gradient inference. The
20-step configuration is not supported as a fix.

## Next recommendation (not executed)

One matched 500-update single-factor learning-rate scale control: the earlier
500-step schedule with peak LR 5e-5 instead of 1e-4, all other settings fixed.
Acceptance would require both grippers' below-min rates and overshoot not worse
than the earlier 500-update pilot at 250 and 500 updates, main MAE within 5% of
that pilot, at least 8/10 episodes not worse, and all integrity checks passing.
Failure would stop the line rather than trigger more steps or an LR sweep.

A prospective protocol separates training-budget monitoring, terminal quality
and candidate eligibility. Mid-schedule checkpoints must be compared with
same-phase references, not with a completed terminal pilot. The stopped run
remains excluded under its original pre-registration.

## Limits

- Development validation episodes, not an independent test set.
- No training, convergence, task-success, simulation or hardware-safety claim.
- The 30-start panel is a bounded diagnostic, not a full-panel re-evaluation.
- Inference-noise seeds are not training seeds.
- The cause of the low-value/static bias remains unidentified; learning-rate
  schedule, data distribution, action representation and input semantics are not
  separated.

## References

- [SmolVLA documentation](https://huggingface.co/docs/lerobot/smolvla)
- [ARMNetBench transfer-cube dataset](https://huggingface.co/datasets/villekuosmanen/armnetbench_transfer_cube)

Full weights, source datasets, private connection details and raw runtime logs
remain outside this repository.
