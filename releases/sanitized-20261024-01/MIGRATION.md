# Sanitized source stack

Source snapshot prepared on 2026-10-24 (America/Los_Angeles).

app/: current LeLab working-tree source, deployment helpers, tests and built frontend.
lerobot/: accompanying local LeRobot 0.6.2 source and tests/examples.
SOURCE_MANIFEST.sha256: per-file verification (does not list itself).

No original Git history, credentials, environments, datasets, weights, calibration,
private backups, machine-specific agent instructions or memory are included.
Addresses are supplied separately through private configuration. Functional default
ports and public upstream URLs remain in source.

Enter current/app for setup using the existing installation runbook. The editable
../lerobot dependency resolves to the included source. Do not substitute PyPI.
The source transfer does not install dependencies or start services.

For this first release no prior current exists. Recovery consists of unlinking only
current; original root repository/files and the immutable release remain intact.
For later releases restore the preceding current symlink instead.
