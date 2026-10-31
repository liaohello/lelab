# LeLab Jetson-first deployment runbook

This runbook deploys the project with the Jetson Super as the primary edge host and the GPU
server as an on-demand training and fallback-inference host. Every supplied bootstrap, source
sync, tunnel, and dataset transfer script is a dry-run unless `--apply` or `-Apply` is given.
No script contains or accepts the passwords from `remote_server.md`.

### Sanitized workstation migration

Use a reviewed staging tree containing `app/` (LeLab) and `lerobot/` (the matching
editable 0.6.2 source). This preserves the `../lerobot` dependency without copying
the workstation environment or Git history. Include the existing `frontend/dist/`.
Exclude machine-specific `AGENTS.md`, `.workbuddy/`, private configurations, keys,
runtime data, and the downloaded reference tree using `source-excludes.txt`.

`windows/sync-source.ps1` accepts `-IdentityFile` for both SSH and SCP. Run it first
without `-Apply`, inspect the staging file list and privacy scan, then use `-Apply`.
The archive uses that same file list. A staging-root `SOURCE_MANIFEST.sha256`, when
present, is checked after extraction and before the `current` link is switched.
Keep the original source hashes in the manifest; exclude the manifest itself.

The resulting entry point is `<stack>/current/app`, with LeRobot next to it at
`<stack>/current/lerobot`. This operation transfers source only: it does not install
dependencies, start services, configure credentials, or move robot hardware.
Existing root files are retained. Roll back by restoring the previous `current`
symlink, or remove the new symlink if no previous release existed; keep release
directories until their contents are no longer needed.

> **Code-level status:** [IMPLEMENTATION-STATUS.md](IMPLEMENTATION-STATUS.md) maps each part of
> the architecture (hardware lease, dataset sync API, SSH training runner, status streams) to
> the module or endpoint that implements it, and lists what is still open.
>
> **Train-server profile:** copy [config/servers.example.toml](config/servers.example.toml) to
> `~/.config/lelab/servers.toml` (or set `LELAB_SERVERS_CONFIG`). `LELAB_TRAIN_*` environment
> variables override it, and the profile holds no credentials. The fallback PolicyServer port is
> `28081` on both ends of the tunnel and must stay on loopback.

## 1. Target architecture

```text
SO-101 + USB cameras
        |
        | local USB only; never forwarded over SSH
        v
Jetson Super 192.0.2.22:22 (nvidia)
  ~/Desktop/lelab/current                 immutable LeLab source release
  ~/Desktop/lelab/lerobot/current         immutable LeRobot source release
  ~/Desktop/lelab/shared/datasets         dataset master copy
  ~/Desktop/lelab/shared/calibration      calibration master copy
  localhost:8000                          LeLab API + built frontend
  localhost:8080                          preferred local PolicyServer
  localhost:28081                         SSH tunnel to remote fallback PolicyServer
        |                                      ^
        | staged rsync + SHA-256 + atomic ready|
        v                                      |
Train server 192.0.2.10:22 (trainer)
  ~/Desktop/lelab_server/current          immutable LeLab source release
  ~/Desktop/lelab_server/lerobot/current  immutable LeRobot source release
  ~/Desktop/lelab_server/shared/datasets  training copies, never the master
  ~/Desktop/lelab_server/shared/models    checkpoints/exported policies
  localhost:28081                         fallback PolicyServer (not public)

Windows workstation
  source/build/control fallback only
  http://127.0.0.1:8000 -> SSH -> Jetson localhost:8000
```

Windows cannot operate USB hardware still plugged into Jetson. A Windows fallback requires a
physical USB reconnect, the matching calibration copy, and a new read-only device audit.

### Network exposure

| Endpoint | Binding | Purpose |
| --- | --- | --- |
| Jetson `22/tcp` | LAN `192.0.2.22` | SSH administration/source transfer |
| Jetson `8000/tcp` | `127.0.0.1` | UI/API through Windows SSH tunnel |
| Jetson `8080/tcp` | `127.0.0.1` | preferred local inference |
| Jetson `28081/tcp` | `127.0.0.1` | local end of train fallback tunnel |
| Train `22/tcp` | server address | SSH only |
| Train `28081/tcp` | `127.0.0.1` | remote PolicyServer through SSH tunnel |

Do not expose the current gRPC PolicyServer directly: it is unencrypted and uses Python pickle
messages. Keep it on loopback and carry it only inside SSH.

## 2. Safety and privacy gates

Before any service that can create motor commands is enabled:

1. Put the arm in a mechanically safe pose with clearance, an accessible power cut-off, and no
   person in its workspace.
2. Keep `LELAB_START_OBSERVE_ONLY=1`, require the hardware lease, and require the local safety
   limiter. Start with `LELAB_MAX_RELATIVE_TARGET=5` degrees and an action TTL of 250 ms.
3. Verify serial identities with `/dev/serial/by-id`; never guess `/dev/ttyUSB0` after a reboot.
4. Verify every camera by image preview without enabling torque.
5. Run observation-only, then a single-joint low-speed test, then multi-joint teleoperation.
6. Never combine calibration, teleoperation, recording, and inference; only one hardware lease
   may own the robot.

Datasets stay under the Jetson `shared/datasets` tree. Recording must use
`push_to_hub=false`; do not set an HF write token in the Jetson service. Training copies are sent
only when explicitly selected. The source sync exclusion list blocks datasets, calibration,
outputs, `.env` files, private keys, and `remote_server.md`.

## 3. Prerequisites and first connectivity check

Run these read-only commands from Windows PowerShell:

```powershell
Test-NetConnection 192.0.2.22 -Port 22
Test-NetConnection 192.0.2.10 -Port 22
ssh -o BatchMode=yes -o ConnectTimeout=5 -p 22 nvidia@192.0.2.22 true
ssh -o BatchMode=yes -o ConnectTimeout=5 -p 22 trainer@192.0.2.10 true
```

`Permission denied (publickey)` proves the route and SSH daemon work but the key is absent.
Timeout/refused must be fixed before deployment. Verify each SSH host fingerprint through an
independent trusted channel before adding it to `known_hosts`.

Create separate Ed25519 deployment keys if needed. It is acceptable for initial key enrollment
to prompt once interactively; never place a password in a command, script, environment file,
terminal log, or repository. Restrict the train-side key to the account and operations required.

## 4. Build frontend and distribute immutable source releases

From the Windows LeLab checkout, build the production frontend before packaging:

```powershell
Set-Location C:\Users\Administrator\Desktop\embodied_intelligence_proj\lelab\frontend
npm ci
npm run build
```

Preview both Jetson source transfers; these commands make no changes:

```powershell
Set-Location C:\Users\Administrator\Desktop\embodied_intelligence_proj\lelab
.\deploy\windows\sync-source.ps1 `
  -SourceRoot ..\lelab `
  -RemoteHost 192.0.2.22 -RemoteUser nvidia -RemotePort 22 `
  -RemoteRoot Desktop/lelab

.\deploy\windows\sync-source.ps1 `
  -SourceRoot ..\lerobot-main `
  -RemoteHost 192.0.2.22 -RemoteUser nvidia -RemotePort 22 `
  -RemoteRoot Desktop/lelab/lerobot
```

After checking file counts, paths, and exclusions, append `-Apply` to each. A SHA-256 verified
release is extracted under `releases/<timestamp>` and `current` is atomically changed to a
relative symlink. Existing releases are retained for rollback.

Repeat for the train host when training code is needed:

```powershell
.\deploy\windows\sync-source.ps1 `
  -SourceRoot ..\lelab `
  -RemoteHost 192.0.2.10 -RemoteUser trainer -RemotePort 22 `
  -RemoteRoot Desktop/lelab_server

.\deploy\windows\sync-source.ps1 `
  -SourceRoot ..\lerobot-main `
  -RemoteHost 192.0.2.10 -RemoteUser trainer -RemotePort 22 `
  -RemoteRoot Desktop/lelab_server/lerobot
```

Use `-Apply` only after the dry-run. Source releases intentionally do not synchronize or delete
remote state. The deployment must use the same reviewed LeRobot source revision on Jetson and
train; do not mix the old Git `v0.6.0` dependency with the local `0.6.2` tree.

## 5. Audit and bootstrap Jetson

SSH to Jetson, then run the read-only audit from the deployed release:

```bash
cd ~/Desktop/lelab/current
bash deploy/jetson/audit.sh
bash deploy/jetson/audit.sh --proxy-url http://192.0.2.23:7890
```

The proxy is optional and Jetson-only. Shell proxy variables may help `curl`, `uv`, and package
downloads; Docker uses daemon proxy settings, so do not assume `docker pull` uses them. Never
copy this private proxy configuration to the public train server unless an intentional reverse
tunnel has been designed and audited.

First preview, then create persistent directories and the credential-free environment template:

```bash
bash deploy/jetson/bootstrap.sh --install-packages --add-device-groups --install-service
bash deploy/jetson/bootstrap.sh --apply --install-packages --add-device-groups --install-service \
  --proxy-url http://192.0.2.23:7890
```

Log out/in after group changes. Review and lock down:

```bash
chmod 600 ~/Desktop/lelab/shared/config/jetson.env
editor ~/Desktop/lelab/shared/config/jetson.env
```

Populate `LELAB_SERIAL_DEVICES` and `LELAB_VIDEO_DEVICES` only with audited paths, for example a
stable `/dev/serial/by-id/...` and selected `/dev/videoN`. The API container receives no other
devices. The local PolicyServer receives no robot or camera devices at all.

### Jetson PyTorch container gate

`nvcr.io/nvidia/pytorch:25.04-py3-igpu` is a **candidate**, not an automatic choice. It provides
the Python 3.12/PyTorch generation required by the current code, but it may be pulled or run only
after the actual L4T/JetPack release from the audit is checked against NVIDIA's current
[Jetson PyTorch compatibility table](https://docs.nvidia.com/deeplearning/frameworks/install-pytorch-jetson-platform-release-notes/pytorch-jetson-rel.html).
JetPack 6.2.1 details are in the official
[JetPack SDK page](https://developer.nvidia.com/embedded/jetpack-sdk-621), and the candidate's
framework contents are described in the
[PyTorch 25.04 release notes](https://docs.nvidia.com/deeplearning/frameworks/pytorch-release-notes/rel-25-04.html).

After that manual review, preview and apply the explicit acknowledgement/pull:

```bash
bash deploy/jetson/bootstrap.sh --pull-base-image --container-compatible
bash deploy/jetson/bootstrap.sh --apply --pull-base-image --container-compatible
```

Build the application image from the project root. The Dockerfile-specific ignore file keeps
`shared/` (including datasets and calibration) outside the build context:

```bash
cd ~/Desktop/lelab
docker build \
  --build-arg BASE_IMAGE=nvcr.io/nvidia/pytorch:25.04-py3-igpu \
  -f current/deploy/jetson/Containerfile \
  -t lelab-jetson:local .
```

The container creates a system-site-packages venv and explicitly prevents `uv` from replacing
the NVIDIA-provided `torch`, `torchvision`, and `torchcodec` packages with generic wheels.
Confirm the final build log reports CUDA available.

Start only the web/status service first:

```bash
sudo systemctl enable --now lelab-api.service
systemctl status --no-pager lelab-api.service
journalctl -u lelab-api.service -n 100 --no-pager
curl --fail http://127.0.0.1:8000/health
```

Enable local inference only after its process can start without robot ownership:

```bash
sudo systemctl enable --now lerobot-local-policy-server.service
systemctl status --no-pager lerobot-local-policy-server.service
```

## 6. Open the Jetson UI from Windows

Preview, then open the blocking tunnel:

```powershell
.\deploy\windows\open-jetson-ui-tunnel.ps1
.\deploy\windows\open-jetson-ui-tunnel.ps1 -Apply
```

Browse `http://127.0.0.1:8000`. Closing the PowerShell process closes the tunnel. Direct LAN
binding (`0.0.0.0`) is not the default; enable it only after application token authentication,
restricted CORS, and a firewall rule limited to the management workstation are verified.

The operational dashboard should display Jetson CPU/RAM/GPU/temperature, filesystem free space,
serial/camera presence, current hardware lease, recording status, and transfer progress. Dataset
details should show `meta/info.json`, LeRobot version, fps, features, camera encoding/resolution,
Parquet schema, path templates, and the file tree. These views are read-only unless a separate
explicit management action is confirmed.

## 7. Audit and bootstrap the train server

```bash
ssh -p 22 trainer@192.0.2.10
cd ~/Desktop/lelab_server/current
bash deploy/train/audit.sh
bash deploy/train/bootstrap.sh --install-packages --install-service
bash deploy/train/bootstrap.sh --apply --install-packages --install-service
chmod 600 ~/Desktop/lelab_server/shared/config/train.env
editor ~/Desktop/lelab_server/shared/config/train.env
```

Install the reviewed Python 3.12/CUDA environment and synchronize dependencies only after the
matching LeRobot source dependency is configured:

```bash
bash deploy/train/bootstrap.sh --sync-dependencies
bash deploy/train/bootstrap.sh --apply --sync-dependencies
```

Do not start the PolicyServer until a compatible GPU PyTorch build and a model have been tested.
When ready:

```bash
sudo systemctl enable --now lerobot-policy-server.service
ss -ltn | grep ':28081'
```

The only acceptable listener is loopback. A `0.0.0.0:28081` or `[::]:28081` listener is a stop
condition.

## 8. Transfer a selected dataset for training

On Jetson, first preview:

```bash
cd ~/Desktop/lelab/current
bash deploy/data/upload-dataset-to-train.sh --repo-id local/pick_blocks
```

Then apply after verifying the source path and train destination:

```bash
bash deploy/data/upload-dataset-to-train.sh \
  --repo-id local/pick_blocks \
  --apply
```

The protocol is:

1. Validate `meta/info.json` locally.
2. Produce a sorted SHA-256 manifest and `transfer.json`.
3. `rsync` into a unique train-side `.incoming/<transfer-id>/payload` directory.
4. Verify every checksum on the train host.
5. Write `.ready`, then rename the payload into its final dataset path on the same filesystem.

An existing final dataset causes failure. `--replace` must be explicit and moves the old tree to
`.trash/<transfer-id>/...`; it does not delete it. Interrupted staging directories remain
quarantined under `.incoming` for inspection. List only verified datasets on train with:

```bash
bash ~/Desktop/lelab_server/current/deploy/train/list-ready-datasets.sh
```

Training jobs must accept only datasets with `.ready` and should record the transfer manifest SHA
in their run metadata. They write checkpoints to `shared/outputs` and publish selected models to
`shared/models`. Model return to Jetson should use the same staged/checksum/atomic-current design;
never overwrite the active model in place.

## 9. Inference selection and failover

Preferred Jetson-local endpoint:

```text
127.0.0.1:8080
```

Before remote fallback, establish the train tunnel on Jetson. Preview, then apply:

```bash
bash deploy/jetson/open-train-inference-tunnel.sh
bash deploy/jetson/open-train-inference-tunnel.sh --apply
```

The RobotClient then uses `127.0.0.1:28081`; it never connects to the public server address on
the gRPC port. Switching inference endpoints must stop the current RobotClient, drain queued
actions, acquire a fresh hardware lease, and re-enter observe-only mode. A disconnected/stale
server must result in zero new motor commands, not replayed actions.

## 10. Commissioning sequence

Run these as distinct approvals, recording results after every phase:

1. UI/status only; no serial/video devices passed to the container.
2. Camera preview only; check device identity, resolution, fps, and timestamps.
3. Serial discovery only; torque disabled and no movement command.
4. Calibration backup/restore verification.
5. One joint, low velocity, maximum 5-degree relative target; verify emergency stop.
6. Teleoperation without recording; confirm lease exclusion and stale-action drop.
7. Record one short episode locally with Hub upload disabled; inspect schema and video playback.
8. Transfer that dataset to train and verify `.ready`/checksums.
9. Train a smoke-test model; return it through staged validation.
10. Local inference in observe-only mode, then the same 5-degree constrained test.
11. Remote inference through SSH, again starting observe-only with an empty action queue.

Do not expand motion limits merely to make a failing test pass. Diagnose calibration, units,
joint ordering, timestamps, model preprocessing, and camera mapping first.

## 11. Rollback and recovery

Source releases are immutable. To roll back on either Linux host:

```bash
cd ~/Desktop/lelab
ls -1 releases
ln -s "releases/REVIEWED_RELEASE_ID" .rollback-current
mv -Tf .rollback-current current
sudo systemctl restart lelab-api.service
```

For the train root use `~/Desktop/lelab_server`. Never point `current` at an unverified partial
directory. Dataset replacement backups remain in `shared/datasets/.trash`; restoring one should
be a reviewed rename on the same filesystem, followed by checksum verification. Do not use
recursive deletion during incident recovery.

If the API or inference process fails repeatedly, stop its unit, preserve logs and the active
release id, and return the arm to a hardware-safe state before troubleshooting:

```bash
sudo systemctl stop lelab-api.service lerobot-local-policy-server.service
journalctl -u lelab-api.service -u lerobot-local-policy-server.service --since -30min --no-pager
```

## 12. Files delivered by this deployment layer

- `env/*.env.example`: credential-free, role-specific configuration templates.
- `windows/sync-source.ps1`: exclusion-aware, checksum-verified immutable release distribution.
- `jetson/audit.sh` and `train/audit.sh`: read-only platform/device/storage/exposure checks.
- `jetson/bootstrap.sh` and `train/bootstrap.sh`: explicit-apply host preparation.
- `jetson/Containerfile`: Jetson PyTorch-preserving application image.
- `systemd/*.service`: loopback-first API and inference services.
- `data/upload-dataset-to-train.sh`: staged rsync/checksum/atomic-ready data transfer.
- `data/pull-model-from-train.sh`: staged model return with SHA-256 verification and an atomic
  `models/<policy>/active` switch (dry-run by default).
- `windows/open-jetson-ui-tunnel.ps1`: Windows UI tunnel.
- `windows/link-lerobot.ps1`: creates the `lerobot` junction the local LeRobot dependency needs.
- `jetson/open-train-inference-tunnel.sh`: Jetson-to-train inference tunnel.
- `config/servers.example.toml`: credential-free deployment profile (train + Jetson hosts).
- `IMPLEMENTATION-STATUS.md`: architecture-to-code map and the remaining work.

Populated environment files, SSH keys, datasets, calibration, recordings, checkpoints, and
`remote_server.md` are runtime state and must never enter a source release or Git history.
