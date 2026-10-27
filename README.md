# lelab
A web-based LeRobot workbench for robot calibration, teleoperation, dataset recording, training, and policy inference.

The tracked application source is in
[releases/sanitized-20261024-01/app](releases/sanitized-20261024-01/app):
[FastAPI backend](releases/sanitized-20261024-01/app/lelab),
[React frontend](releases/sanitized-20261024-01/app/frontend),
[tests](releases/sanitized-20261024-01/app/tests), and
[deployment tools](releases/sanitized-20261024-01/app/deploy).
The matching, locally customized
[LeRobot source](releases/sanitized-20261024-01/lerobot) is included alongside it.
The root `current`, `app`, and `lerobot` entries are relative symlinks to this
source stack. Keep the application and LeRobot directories as siblings so the
editable `../lerobot` dependency resolves to this source, including its SO-101
safety limits and weighted dataset sampling.

See the [installation guide](releases/sanitized-20261024-01/app/INSTALL.md) and
[deployment runbook](releases/sanitized-20261024-01/app/deploy/README.md).
Python 3.12 or newer is required. Production serves the included
`frontend/dist/`; frontend source changes require rebuilding that bundle.
The referenced SO-101 STL meshes are included in both public and built assets;
their [source and checksums](releases/sanitized-20261024-01/app/frontend/public/so-101-urdf/meshes/SOURCE.md)
and Apache-2.0 license accompany the files.
The nested application workflows are retained as source references; they are
not installed as this repository's root GitHub Actions workflows.

Real server settings, keys, datasets, model weights, and local `work/` and
`plans/` records are excluded, as are the local `proj_state.md` and
`run_note.md` machine inventories. Use the committed deployment templates with
private configuration outside the checkout; see the deployment runbook for
`LELAB_SERVERS_CONFIG` and environment-variable overrides.
Some upstream LeRobot test assets are Git LFS pointers; their binary contents
are not bundled here. If Git LFS is installed, clone with
`GIT_LFS_SKIP_SMUDGE=1` until those upstream test fixtures are supplied.
Migration notes describe the original source transfer,
not a completed installation. Its original byte-level manifest and archive
checksum stay local because Git normalizes text line endings; generate a fresh
manifest from the exact exported files before a deployment transfer.

Research progress: [milestone index](docs/RESEARCH_PROGRESS.md).

The published research milestones document an offline SmolVLA bimanual
training pilot, a later gated run that stopped early under pre-registered
symmetric gripper monitoring, a retrospective diagnostics/inference-step
analysis, and a 10,000-update offline fine-tune with development scaled MAE
0.3105. The fine-tune completed after two pre-registered GPU-conflict pauses,
improved on the earlier pilot, and still did not meet the original A gate.
These records do not establish simulation task success or physical robot
safety. Model weights, datasets and private runtime configuration are not
included.
