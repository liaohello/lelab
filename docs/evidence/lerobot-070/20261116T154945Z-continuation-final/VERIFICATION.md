# LeRobot 0.7.0 migration — independent final verification

Run: `20261116T154945Z-continuation-final`. Worktree `/home/liaozhengyan/Desktop/lelab-lerobot070`,
branch `codex/lerobot-070-integration`, HEAD `360d4e9`.

This directory records **re-runs performed independently after all commits landed**, not a new plan.
It complements (does not replace) `../20261116T145909Z-m4m6/`, whose `results.json` holds the
40-case ID mapping and full logs.

## Commits verified (final HEAD `360d4e9`)

| Commit | Scope |
| --- | --- |
| `3ac2327` | N1 — `sample_weights_file` / `eval_episodes` through config → factory → loader |
| `7340e7a` | N2 — `lelab_monitor`, metrics, pause/resume on 0.7 checkpoints |
| `d7eef1d` | N3 — homing sign, ROS CLI Trackio, FastWAM form dimensions |
| `b9b9a95` | N4 — LeLab remote rollout migrated to LeRobot 0.7 Zenoh (`inference_070.py`) |
| `78f1baa` | N5 — frontend rebuild, candidate package + rollback rehearsal, M4/M5 evidence |
| `360d4e9` | N5 — final 40-case suite accounting |

## Independent re-runs on final HEAD

All Python commands: interpreter `<worktree>/.venv/bin/python` (Python 3.12.3),
`OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1`,
`env -u PYTHONPATH`, `-p no:xdist`. Import paths are the editable-installed worktree
(`lerobot 0.7.0` from `releases/sanitized-20261024-01/lerobot/src`).

| Check | Command (abbrev.) | Result | Exit |
| --- | --- | --- | --- |
| App strict collection | `cd app && pytest tests --collect-only -q` | 591 collected, **0 collection errors** | 0 |
| ROS app strict collection | `cd app_ros_mobile && pytest tests --collect-only -q` | 827 collected, **0 collection errors** | 0 |
| App full suite | `cd app && pytest tests -q` | **591 passed, 1 skipped** | 0 |
| N1+N2 continuity/weights | `cd lerobot && pytest tests/scripts/test_pause_resume_continuity.py tests/configs/test_training_monitor_overrides.py tests/datasets/test_split_and_weights.py -q` | **34 passed** | 0 |
| Library remote/inference | `pytest tests/remote_inference tests/inference tests/test_remote_rollout.py -q` | **295 passed, 2 skipped** | 0 |
| FastWAM offline | `pytest tests/policies/fastwam -q -k 'not vae_adapter...'` | **13 passed** (1 VAE blocked by RLIMIT_AS) | 0 |
| FineART offline | `pytest tests/policies/fineart_vla -q` | **16 passed, 1 skipped** (CUDA-only) | 0 |
| N4 adapter assertions | inline: server/rollout argv, dispatch gate, server config | Zenoh endpoint + legacy-token guard + width/NaN/stale rejection all OK | 0 |

Strict collection uses **no** `--continue-on-collection-errors`. The single skip in the app suite
is the legacy gRPC `test_jetson_policy.py`; its replacement guard tests run in
`test_inference_070_jetson.py` (4 passed).

## Environment blockers (recorded, not masked)

- CPU allocator: `ulimit -v` is hard-capped at 4 GiB (`RLIMIT_AS`). The FastWAM VAE shape test
  (~113 MB tensor) and local Vite/undici Wasm build cannot run here. Vite was built on a host
  without the cap (`vite-build-remote.txt`, exit 0) and dist was synced back.
- ROS app: 9 failures remain and are logged as failures in `../.../ros-app-suite.txt`:
  5× libsvtav1 encoder OOM, 2× TestClient thread creation, 1 missing historical evidence
  fixture, 1 estop 409 collision. None are in the migrated inference path.
- Real weights / real robot / deployment: intentionally not executed (see delivery report).

## Re-run / rollback

See `../20261116T145909Z-m4m6/STATUS.md` §4–5. Re-run commands there were validated during
this independent pass. Rollback baseline before N4/N5 is `d7eef1d`.
