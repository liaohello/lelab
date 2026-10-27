# Publishing a sanitized LeLab snapshot

The publication snapshot contains the current source files and existing local code changes, without the original Git history, local credentials, runtime data, or camera test photographs.

## Deployment configuration

Copy `deploy/config/servers.example.toml` to `deploy/config/servers.toml` and replace the documentation addresses, user names and key paths. The actual file is ignored by Git. Addresses in `192.0.2.0/24` are examples and cannot identify the original machines.

PowerShell and shell connection scripts accept explicit parameters or `LELAB_TRAIN_HOST`, `LELAB_TRAIN_USER`, `LELAB_TRAIN_PORT`, `LELAB_JETSON_HOST`, and `LELAB_JETSON_USER`. The LIBERO synchronizer also needs `LELAB_LIBERO_ROOT`. Set `LELAB_JETSON_TARGET=user@host` for the legacy Jetson sync script and `LELAB_TRAIN_DATASET_ROOT` for the dataset upload script. These standalone scripts do not automatically load the TOML profile. Customize the example systemd users and paths before installation.

## Upload

Use the separate sanitized publication directory or ZIP. Create a new empty GitHub repository and upload only that snapshot. For a command-line upload, initialize a new Git repository inside the publication directory, commit the files, then add your own GitHub repository as the remote.

The original development checkout retains its old history. A normal commit followed by push from that checkout can still publish old connection details. Do not copy its `.git` directory, ignored configuration files, private backups, or runtime folders into the publication snapshot.

GitHub Actions that require secrets must be configured separately. Review `.github/workflows/sync_space.yml` before providing an HF token: it targets the upstream Hugging Face Space. Public model identifiers and third-party author/license metadata have been preserved.

The editable `../lerobot` dependency is still required as documented in `CLAUDE.md`; the publication snapshot does not bundle that separate checkout.
