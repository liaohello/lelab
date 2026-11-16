# M0 origin state — recorded 2026-11-16T09:49Z

## Original workspace (untouched, read-only)

- path: /home/liaozhengyan/Desktop/lelab
- branch: codex/ros2-mobile-collection
- HEAD: 47d17546d035dbbc401cd80c4d8768269d709826
- upstream tracking: origin/codex/ros2-mobile-collection
- untracked entries: 27 (evidence dirs, `docs/migrations/`, `docs/reviews/2026-11-16-*.md`,
  `releases/training-monitor-20261030-01/`, `releases/training-monitor-20261030-02/`)
- no tracked modifications at capture time

## Tracked entry points (mode 120000 = symlink, relative targets)

| link | blob | target |
| --- | --- | --- |
| `app` | 054c3eb | `current/app` |
| `lerobot` | 94cbf1d | `current/lerobot` |
| `current` | 6f79dd7 | `releases/sanitized-20261024-01` |

Because the targets are relative, a new worktree keeps `app` / `lerobot` / `current`
inside that worktree. `releases/sanitized-20261024-01` is tracked (1296 files, 947 of
them under `lerobot/`), so no copy step is needed to obtain the 0.6.2 baseline source.

## Pre-existing worktrees (not modified)

| path | HEAD | branch |
| --- | --- | --- |
| /home/liaozhengyan/Desktop/lelab | 47d1754 | codex/ros2-mobile-collection |
| /home/liaozhengyan/.codex/worktrees/so101-local-bimanual/lelab | 07216e0 | codex/so101-local-bimanual |
| /home/liaozhengyan/Desktop/lelab-bimanual | 7461286 | feat/local-bimanual |
| /tmp/lelab-review-20261116-dpU4RY/training-worktree | 3cd1840 | codex/training-monitor-review |

Notes:
- `codex/training-monitor-review` lives in `/tmp` — volatile; its commits are on the branch,
  the working tree copy can disappear. Integration must replay from the branch, not the dir.
- `feat/local-bimanual` tracks `origin/feat/local-bimanual` (pushed).

## New migration worktree

- path: /home/liaozhengyan/Desktop/lelab-lerobot070
- branch: codex/lerobot-070-integration (created from 47d1754)
- resolved entry points inside the worktree:
  - `app` -> `<wt>/releases/sanitized-20261024-01/app`
  - `lerobot` -> `<wt>/releases/sanitized-20261024-01/lerobot`
  - `current` -> `<wt>/releases/sanitized-20261024-01`
- none of them resolve back into /home/liaozhengyan/Desktop/lelab

## Upstream source

- path: /home/liaozhengyan/Desktop/lerobot-0.7.0
- `pyproject.toml` name=lerobot version=0.7.0
- **no `.git` directory** — cannot resolve an upstream commit from the archive itself;
  provenance is the archive hash manifest only (see `upstream-lerobot-0.7.0.sha256`)
- files hashed: 1354 (excluding `*.pyc`)
- python files: 1084

## Local 0.6.2 snapshot

- version: 0.6.2
- python files under `lerobot/`: 822
- extras declared by app: `lerobot[core_scripts,feetech,training]>=0.6.2,<0.7.0`

## Host capabilities (affects TC-REAL / TC-HW)

- OS: Linux, 22 GiB RAM (≈6 GiB available during M0)
- GPU: **none** — `nvidia-smi` absent; only AMD Radeon Vega (Cezanne) iGPU
- CPU torch 2.11.0+cpu is the only viable torch build here
- interpreter for migration: `<wt>/.venv/bin/python` (CPython 3.12.3, `/usr/bin/python3.12`)
