# LeLab module deep-dive: remote_sync, sealing, composites, model_store

Read completely: `lelab/remote_sync.py` (648 L), `lelab/sealing.py` (448 L), `lelab/composites.py` (876 L),
`lelab/model_store.py` (302 L). Supporting readers used only for shared helpers:
`lelab/runners/ssh_ops.py`, `lelab/utils/servers.py`. No speculation about server.py beyond quoted lines.

Legend: **B** = blocking (returns when the work FINISHED), **NB** = non-blocking (returns when the work STARTED).

---

## 1. `lelab/remote_sync.py` — Jetson → train-server dataset transfer

### Accessor and public surface
- `dataset_sync_manager = DatasetSyncManager()` — `remote_sync.py:643`; accessor
  `def get_dataset_sync_manager() -> DatasetSyncManager:` — `remote_sync.py:646-648`.
  Construction is inert: "construction performs no environment, filesystem, network, or subprocess work." (`:641-642`)
- Methods called by server.py (`server.py:946, 1011, 1209, 1223, 1239, 1247, 2054`):
  - `status() -> dict[str, Any]` — `:375-378` (deepcopy under RLock)
  - `history(repo_id: str | None = None) -> dict[str, Any] | list[dict[str, Any]]` — `:380-385`
  - `start(repo_id: str, *, dry_run: bool = False) -> dict[str, Any]` — `:387`
  - `cancel() -> dict[str, Any]` — `:443`
- Constructor takes keyword-only `dataset_root=None, config: TrainServerConfig | None = None` (`:357-362`);
  when `config` is None, `start` builds it from the environment: `config = self._config if self._config is not None else TrainServerConfig.from_env()` (`:405`).

### Constants / exception
- `ACTIVE_SYNC_PHASES = frozenset({"queued", "checking", "preparing", "transferring", "finalizing", "cancelling"})` — `:52-54`; alias `_ACTIVE_PHASES = ACTIVE_SYNC_PHASES` — `:55`. Docstring: "Phases in which a transfer owns the singleton and the dataset must not be modified (records its `repo_id`) or started again." (`:50-51`)
- `class DatasetSyncError(RuntimeError)` — `:62-63` (docstring: "A human-readable dataset transfer failure.").
  Private control-flow signal `class _SyncCancelledError(Exception)` — `:66-67`.

### `TrainServerConfig` (`@dataclass(frozen=True, slots=True)`, `:70-78`)
Fields and types: `host: str`, `port: int`, `user: str`, `root: str`, `ssh_key: Path | None = None`.
`from_env(cls, env: Mapping[str, str] | None = None)` — `:80-115`; values come from
`resolved_env(os.environ if env is None else env)` (`:84`), i.e. profile defaults first, env wins
(`utils/servers.py:105-118`; profile file precedence: `LELAB_SERVERS_CONFIG` → `~/.config/lelab/servers.toml` → `deploy/config/servers.toml`, `utils/servers.py:68-77`).

| env var | field | notes |
|---|---|---|
| `LELAB_TRAIN_HOST` | `host` | `:85`; required; regex `^[A-Za-z0-9][A-Za-z0-9._:-]*$` and must not start with `-` (`:102-103`) |
| `LELAB_TRAIN_USER` | `user` | `:86`; required; regex `^[A-Za-z_][A-Za-z0-9_.-]*$` (`:104-105`) |
| `LELAB_TRAIN_ROOT` | `root` | `:87`; required; normalized by `_normalize_remote_root` (`:113`, `:214-231`) |
| `LELAB_TRAIN_PORT` | `port` | `:88`; default `"22"`; must be int in 1..65535 (`:106-111`) |
| `LELAB_TRAIN_SSH_KEY` | `ssh_key` | `:89`; `Path(raw_key).expanduser().resolve(strict=False)` else `None` (`:114`) |

Profile section is `[train_server]` with keys `host/port/user/root/ssh_key` (`utils/servers.py:46-54`).
`@property endpoint` — `:117-122` → `f"{user}@{host}"`, IPv6 host bracketed.

### `ssh_base_command(config)` — `:255-268` (B, pure)
```python
command = ["ssh", "-p", str(config.port), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]   # :257-265
if config.ssh_key is not None:
    command.extend(["-i", str(config.ssh_key)])                                              # :266-267
```
Returns `list[str]`; private wrapper `_ssh_base` — `:251-252`. Used by `ssh_ops` too (`ssh_ops.py:46, 119, 190`).

### Blocking behaviour
- `start` is **NB**: validation + plan are inline, then
  `self._thread = threading.Thread(target=self._run_sync, args=(config, plan), name=f"dataset-sync-{sync_id[:8]}", daemon=True)` — `:434-439`; `self._thread.start()` — `:440`; `return copy.deepcopy(state)` — `:441`.
  Docstring: "Validate and enqueue one transfer without waiting for network I/O." (`:388`)
- `dry_run=True` is **B** but does almost nothing: "performs path/configuration validation and returns every subprocess argument plus the two fixed remote scripts, but launches no process and performs no connection check." (`:390-392`); returns inside the lock at `:430-431`.
- `status` / `history` / `cancel` are **B-trivial** (memory + deepcopy). `cancel` does not wait for the worker:
  `self._cancel_event.set()` / `self._status["phase"] = "cancelling"` / `process.terminate()` then `return copy.deepcopy(self._status)` — `:448-454`.
- The thread does the whole sequence to completion: connect check `:583`, prepare `:591`, rsync `:597`, finalize `:603`, then `phase="completed"` `:611-616`.

### Return shapes and phase literals
`_initial_status()` — `:333-346`, exact keys: `phase="idle"`, `id=None`, `repo_id=None`, `progress=0.0`,
`transferred=0`, `total=None`, `rate_bytes_per_second=None`, `eta_seconds=None`, `error=None`,
`started=None`, `finished=None`.

`start` state dict — `:409-419`: `phase` (`"dry_run"` if dry_run else `"queued"`), `id` (uuid4 hex, `:406`),
`repo_id`, `progress=0.0`, `transferred=0`, `total=None`, `error=None`, `started` (ISO-8601 UTC, `:329-330`),
`finished` (`started` for dry run else `None`). Dry run adds `state["commands"] = plan.public_commands()` and
`state["paths"] = plan.public_paths()` — `:420-422`.
- `public_commands()` keys — `:139-148`: `check`, `prepare`, `prepare_stdin`, `rsync`, `finalize`, `finalize_stdin`.
- `public_paths()` keys — `:150-156`: `source`, `staging`, `target`, `previous`.

Phase literals written by the worker: `"checking"` `:579`, `"preparing"` `:590`, `"transferring"` `:596`,
`"finalizing"` `:602`, `"completed"` `:612`, `"cancelled"` `:618`/`:621`, `"failed"` `:624`, plus
`"cancelling"` from `cancel` `:449`. `progress` is clamped via `max(...)` `:556`, reaches `100.0` on success `:613`.

`history(repo_id)` returns `{}` for an unknown repo — `:384` (`copy.deepcopy(self._history.get(repo_id, {}))`),
else the record dict; `history(None)` returns `list[dict]` of all records `:385`. Record written in the worker's
`finally` — `:628-638`: `repo_id`, `id`, `phase`, `progress`, `transferred`, `total`, `error`, `finished`,
`remote_path` (= `plan.target`). "repo_id -> last finished transfer (kept in memory for the dashboard; the
authoritative record is the remote `.ready` marker)" — `:371-373`.

### Failure modes
Synchronous raises (all `DatasetSyncError`), callers see them from `start`/`from_env`/`ssh_base_command` path:
- config: `:101` missing host/user/root, `:103` bad host, `:105` bad user, `:109` non-integer port, `:111` port range.
- root normalization: `:216` invalid chars, `:226` not absolute/`~/`, `:229` not a specific normalized dir.
- `start`: `:396` and `:428` `"A dataset transfer is already running"`; `:401` wraps `ValueError` from
  `resolve_dataset_path`; `:403` `f"Local LeRobot dataset not found: {repo_id}"`.
- `_spawn`: `:496` `raise DatasetSyncError(f"Could not start {command[0]}: {exc}") from exc`.

Background failures never propagate to the caller — they become status:
- `:617-618` `except _SyncCancelledError: self._set_status(phase="cancelled", error=None, finished=_timestamp())`
- `:619-624` `except Exception as exc:` → if `self._cancel_event.is_set()` → `cancelled`, else
  `message = str(exc) if isinstance(exc, DatasetSyncError) else f"Unexpected sync error: {exc}"`; `phase="failed"`.
- Reason strings raised inside the worker: `:586-588` connect failure, `:594` prepare failure, `:600` rsync exit
  code, `:606` "Remote atomic activation failed".
- `_preflight` `:566-575` raises `DatasetSyncError` for non-Linux host (`:568`), missing `ssh`/`rsync`
  (`:571-573`), missing key file (`:575`) — so a Windows host surfaces as `phase="failed"`, not an exception.

---

## 2. `lelab/sealing.py` — dataset sealing (封存)

### Accessor and public surface
- `seal_manager = SealManager()` — `:443`; `def get_seal_manager() -> SealManager:` — `:446-448` ("Return the process-wide sealer used by the API routes.").
- Manager methods called by server.py (`server.py:1288, 1300, 1307`): `start(repo_id, dataset_root=None)` `:317`,
  `status(repo_id)` `:292`, `cancel(repo_id)` `:374`; plus `active()` `:312` and `forget(repo_id)` `:383`.
- Module functions called by server.py (`server.py:958, 1317, 1328`): `seal_status(repo_id, dataset_root=None)` `:103`,
  `unseal_dataset(...)` `:236`, `verify_seal(...)` `:252`. Also public: `seal_manifest_path` `:59`, `read_seal` `:66`,
  `is_sealed` `:82`, `ensure_mutable(repo_id, dataset_root=None, *, action="modify")` `:92`,
  `build_seal_manifest(repo_id, dataset_root=None, *, cancel=None, progress=None)` `:168`, `write_seal_manifest(path, manifest)` `:228`.
- `class SealError(RuntimeError)` — `:48-49` ("Raised when a dataset cannot be sealed, unsealed or is not mutable.").

### Manifest path / name
`SEAL_RELATIVE_PATH = Path("meta") / ".lelab-sealed.json"` — `:43`; `SEAL_SCHEMA_VERSION = 1` — `:44`;
path = `resolve_dataset_path(repo_id, dataset_root) / SEAL_RELATIVE_PATH` — `:63`, i.e.
`<dataset>/meta/.lelab-sealed.json`. Written atomically (`tmp` + `os.replace`) — `:230-233`.
Module docstring: "A sealed dataset may be viewed, trained on, synced and verified. It may not be recorded into,
re-recorded, repaired, overwritten, trashed or deleted." (`:16-18`).

### Blocking behaviour
- `SealManager.start` is **NB** for the hashing, but **B** for the pre-checks:
  it calls `dataset_summary(repo_id, dataset_root)` `:329` and `dataset_integrity(repo_id, dataset_root)` `:337`
  inline, then spawns the worker:
  `thread = threading.Thread(target=self._run, args=(repo_id, dataset_root, job, cancel), name=f"seal-{repo_id}", daemon=True)` — `:363-368`;
  `thread.start()` — `:371`; `return dict(job)` — `:372`. Docstring context: "Hashing a multi-gigabyte dataset takes a
  while, so sealing runs in a background thread (see :class:`SealManager`) and reports progress through
  `GET /datasets/local/{repo_id}/seal`." (`:22-24`)
- `status` is **B-trivial**: in-memory job copy `:293-296`, otherwise one `dataset_summary` call `:297-310` (metadata only, no hashing).
- `cancel` is **NB**: `cancel.set()` then `return {"repo_id": repo_id, "state": "cancelling"}` — `:380-381`;
  the worker notices via `if cancel is not None and cancel.is_set(): raise SealError(...)` — `:199-200`.
- `build_seal_manifest` itself is **B** (hashes every file, `:198-207`); `verify_seal` is **B** because it recomputes
  the full manifest: `current = build_seal_manifest(repo_id, dataset_root)` — `:259`.

### Return shapes and state literals
`build_seal_manifest` payload — `:209-225`, exact keys: `schema_version` (1), `state="sealed"`, `repo_id`,
`sealed_at` (ISO-8601 UTC), `root` (str), `codebase_version`, `robot_type`, `fps`, `total_episodes`,
`total_frames`, `file_count` (int), `size_bytes` (int), `manifest_sha256` (str),
`files` (list of `{"path": str, "size": int, "sha256": str}`, sorted by posix relative path `:141`),
`tool={"name": "lelab", "version": <lelab.__version__>}`. Manifest digest algorithm — `:156-165`
(path\0size\0sha256\n over every entry).

`SealManager.start` job dict — `:349-359`: `repo_id`, `state="sealing"`, `started_at` (float epoch),
`files_done=0`, `files_total` (from summary `file_count`), `bytes_done=0`, `bytes_total` (from summary `size_bytes`),
`progress=0.0`, `error=None`. Early returns:
- already sealing → same dict, no new thread — `:324-327`;
- already sealed → `{"repo_id", "state": "sealed", "sealed_at", "manifest_sha256"}` — `:331-336`.

Worker completion updates — `:426-437`: `state="sealed"`, `finished_at` (float), `progress=1.0`,
`manifest_sha256`, `sealed_at`, `file_count`, `size_bytes`.
Worker failure — `:417-424`: `state = "cancelled" if cancel.is_set() else "failed"`, `error=str(exc)`, `finished_at`.

`status(repo_id)` — `:292-310`:
- tracked job → the job dict (plain `dict(job)` copy);
- no job, dataset missing/unreadable → `{"repo_id": repo_id, "state": "unknown"}` — `:302`;
- no job, summary available → `{"repo_id", "state": "sealed"|"unsealed" (from summary["sealed"])", "sealed_at", "manifest_sha256", "file_count", "size_bytes"}` — `:303-310`.

Module-level `seal_status(repo_id)` — `:103-120` ("Compact seal state for listings summarised without touching big files"):
- corrupt manifest → `{"sealed": True, "seal_state": "corrupt", "seal_error": str(exc)}` — `:110`;
- unsealed → `{"sealed": False, "seal_state": "unsealed"}` — `:112`;
- sealed → `{"sealed": True, "seal_state": "sealed", "sealed_at", "seal_manifest_sha256", "seal_file_count", "seal_size_bytes"}` — `:113-120`.
  (These six `seal_*` keys are merged into `dataset_summary` at `dataset_storage.py:621`.)

`unseal_dataset(repo_id)` — `:236-249`: returns `{"repo_id": repo_id, "sealed": False, "state": "unsealed"}`
both when the manifest is absent (`:242`) and after a successful `path.unlink()` + `get_seal_manager().forget(repo_id)`
(`:244-249`).

`verify_seal(repo_id)` — `:252-279`, exact keys: `repo_id`, `ok` (bool = `not added and not removed and not changed`, `:270-271`),
`manifest_sha256` (recorded), `computed_sha256` (recomputed), `added` (list[str]), `removed` (list[str]),
`changed` (list[str], sha256 or size mismatch `:264-269`). **There are no `pass`/`fail` counters and no
`sealed` key in this dict**; the sealed/pass-fail accounting lives in `seal_status`'s `seal_file_count`/`seal_size_bytes`
and in the lengths of `added`/`removed`/`changed`.

State literals overall: `"sealing"`, `"sealed"`, `"unsealed"`, `"unknown"`, `"cancelling"`, `"cancelled"`,
`"failed"`; `seal_state` literals: `"sealed"`, `"unsealed"`, `"corrupt"`.

### Failure modes
`raise SealError(...)` sites:
- `:76` unreadable manifest (`read_seal`, wrapped `OSError`/`ValueError`);
- `:78` `payload.get("state") != "sealed"` → "is not a completed seal";
- `:97-100` `ensure_mutable` → "Cannot {action} {repo_id}: the dataset is sealed (封存). Unseal it first...";
- `:191` "has no files to seal";
- `:200` `raise SealError(f"Sealing {repo_id} was cancelled")` (cooperative cancel inside the hash loop);
- `:246` `raise SealError(f"Could not unseal {repo_id}: {exc}") from exc`;
- `:258` `raise SealError(f"Dataset {repo_id} is not sealed")` (`verify_seal`);
- `:344-346` `start` integrity gate: `raise SealError(f"Cannot seal {repo_id}: integrity check failed ({'; '.join(failing)})")`.
Also `raise FileNotFoundError(f"Local LeRobot dataset not found: {repo_id}")` — `:186` (`build_seal_manifest`);
this escapes through `verify_seal` (`:259`) unchanged.
`is_sealed` deliberately swallows `SealError` and returns `True` — `:84-89` ("A corrupt manifest must not silently
unlock the dataset").
Background failures are **not** raised: `except Exception as exc:  # noqa: BLE001 - surfaced through the status payload` — `:415-425`.

---

## 3. `lelab/composites.py` — composite datasets (Jetson planning + remote build)

### Accessor and public surface
- `composite_build_manager = CompositeBuildManager()` — `:871`; `def get_composite_manager() -> CompositeBuildManager:` — `:874-876`.
- Manager methods called by server.py (`server.py:1470, 1492, 1507`): `start(plan, *, runner="ssh_remote", config=None, python=None)` `:665-672`,
  `status(key)` `:649`, `cancel(key)` `:716`; plus `active()` `:656`.
- Module functions: `plan_composite(repo_ids, dataset_root=None, *, weights_mode="frames", weights=None) -> CompositePlan` `:227-233`;
  `remote_status(repo_id, *, config=None) -> dict` `:452`; `list_remote_composites(*, config=None) -> list[dict]` `:504`;
  `list_remote_composite_trash(*, config=None) -> list[dict]` `:595`;
  `trash_remote_composite(repo_id, *, config=None) -> dict` `:567`;
  `restore_remote_composite(repo_id, entry, *, config=None) -> dict` `:609`;
  `is_composite_repo_id(key) -> bool` `:77-79`; `composite_repo_id(hash)` `:73`; `composite_hash_of(repo_id)` `:82`;
  `member_fingerprint(repo_id, dataset_root=None)` `:190`; `train_server_config() -> TrainServerConfig` `:441`.
- `class CompositeError(RuntimeError)` — `:69-70` ("A human-readable composite-dataset failure.").
- Constants `:54-64`: `COMPOSITE_SCHEMA_VERSION=1`, `COMPOSITE_NAMESPACE="local"`, `COMPOSITE_PREFIX="composite_"`,
  `COMPOSITE_HASH_LENGTH=16`, `MIN_MEMBERS=2`, `MAX_MEMBERS=8`, `DEFAULT_STORAGE_FORMAT="v3.0"`,
  `_LOG_TAIL_LINES=60`, `_PROGRESS_PREFIX="LELAB_PROGRESS "`, `_REMOTE_TIMEOUT=120.0`.
- `is_composite_repo_id` body — `:78-79`: `namespace, _, name = repo_id.partition("/"); return namespace == COMPOSITE_NAMESPACE and name.startswith(COMPOSITE_PREFIX)`.

### `CompositePlan` object (`@dataclass(frozen=True, slots=True)`, `:115-127`)
Fields: `repo_ids: tuple[str, ...]`, `members: tuple[dict[str, Any], ...]`,
`blockers: tuple[dict[str, Any], ...]`, `warnings: tuple[dict[str, Any], ...]`,
`estimated_size_bytes: int`, `weights_mode: WeightsMode = "frames"`, `weights: tuple[float, ...] | None = None`,
`composite_hash: str | None = None` (`WeightsMode = Literal["frames", "equal", "custom"]` — `:66`).
Properties:
- `ok` → `return not self.blockers` — `:128-130`;
- `composite_repo_id` → `composite_repo_id(self.composite_hash) if self.composite_hash else None` — `:132-134`.
- `to_report(*, cached: bool | None = None, remote_state: str | None = None) -> dict[str, Any]` — `:136-155`, base keys exactly:
  `ok`, `repo_ids`, `members`, `blockers`, `warnings`, `estimated_size_bytes`, `composite_hash`,
  `composite_repo_id`, `weights_mode`, `weights`; plus `report["cached"] = cached` only if `cached is not None` (`:151-152`)
  and `report["remote_state"] = remote_state` only if `remote_state is not None` (`:153-154`).
- `to_plan_payload()` — `:157-183`: raises when unbuildable `raise CompositeError("the plan is not buildable: it still has blockers")` — `:160`;
  payload keys `schema_version`, `composite_hash`, `composite_repo_id`, `repo_ids`,
  `members` (list of `repo_id`, `meta_fingerprint_sha256`, `seal_manifest_sha256`, `total_episodes`, `total_frames`),
  `merge={"concatenate_videos": False, "concatenate_data": False}` (hardcoded `:176`, and in the hash `:412`),
  `estimated_size_bytes`, `weights={"mode", "values"}`, `created_by={"tool": "lelab", "version": ...}`.

Member dict keys built by `plan_composite` — `:387-404`: `repo_id`, `codebase_version`, `robot_type`, `fps`,
`total_episodes`, `total_frames`, `duration_seconds`, `size_bytes`, `storage_format`, `cameras` (list),
`sealed` (bool), `sealed_at`, `seal_manifest_sha256`, `meta_fingerprint_sha256`.
Blocker/warning dict shape — `:186-187`: `{"code": str, "message": str, "repo_ids": list[str]}` (empty list default).
Blocker codes emitted: `member-count` `:241`/`:243`, `duplicate` `:246`, `weights-mode` `:215`,
`weights-length` `:219`, `weights-value` `:222`, `not-local` `:258`, `not-finalized` `:275`, `not-sealed` `:279`,
`fingerprint` `:284`, `codebase-version` `:295`, `fps` `:303`, `robot-type` `:313`, `storage-format` `:325`,
`feature-keys` `:351`, `feature-shape` `:368`, `camera-info` `:376`. `warnings` is always `[]` in this module
(`warnings: list[dict[str, Any]] = []` `:237`, never appended; returned as `tuple(warnings)` `:428`).
`composite_hash` is set only when there are no blockers — `:408-422`
(sha256 of `schema_version` + `merge` + ordered `[{repo_id, meta_fingerprint_sha256, seal_manifest_sha256}]`, truncated to 16 hex).

### Blocking behaviour
- `plan_composite` is **B** and local: it calls `dataset_summary` (`:255`), `dataset_detail` (`:267`) and
  `member_fingerprint` (`:282`, which reads/hashes member metadata via `lelab.scripts.build_composite.meta_fingerprint*`, `:190-200`)
  for every member, in the calling thread.
- `remote_status` is **B** (SSH round-trips): `ssh_ops.read_remote_file(...)` — `:456` and `:459`
  (`ssh_ops.py:213` → `subprocess.run(..., timeout=60.0)` `ssh_ops.py:73-88`).
- `list_remote_composites` / `list_remote_composite_trash` are **B**: `ssh_ops.run_python(config, _LIST_SOURCE, config.root)` — `:507`, `:599`.
- `trash_remote_composite` / `restore_remote_composite` are **B**: `ssh_ops.run_script(..., timeout=_REMOTE_TIMEOUT)` — `:583-585`, `:615-617` (120 s).
- `get_composite_manager().start(...)` is **NB**: "One background build per composite hash, mirroring the dataset sync manager." (`:639`);
  `thread = threading.Thread(target=self._run, args=(key, plan, runner, config, python, job, cancel, build_id), name=f"composite-{key}", daemon=True)` — `:705-710`;
  `thread.start()` — `:713`; `return dict(job)` — `:714`.
- `status` / `cancel` / `active` are **NB** (memory only). `cancel` — `:716-729`: `cancel.set()`;
  `process.terminate()` under `contextlib.suppress(Exception)`; `return {"composite_hash": composite_hash, "state": "cancelling"}` `:729`.
- The worker `_run_remote` streams the builder over SSH and consumes stdout line-by-line to completion
  (`for raw_line in process.stdout:` `:856`, `return_code = process.wait()` `:860`).

### Return shapes and state literals
`remote_status` — `:452-471`:
- no `.ready` → `{"state": "missing", "repo_id": repo_id}` — `:458`;
- else `{"state": "ready", "repo_id": repo_id, "build_id": ready.strip(), "manifest": manifest}` — `:466-471`,
  where `manifest` is `json.loads(meta/lelab-composite.json)` or `None` (parse failures suppressed `:464-465`).

`list_remote_composites` — list of dicts produced by `_LIST_SOURCE` (`:486-499`), keys:
`repo_id`, `hash`, `members` (list), `built_at`, `size_bytes`, `file_count`, `estimated_size_bytes`,
`weights_mode` (default `"frames"`), `ready` (bool). Returns the parsed list, else `[]` if not a list `:516`.

`list_remote_composite_trash` — list of the stored `lelab-trash.json` payloads with `entry` defaulted to the
directory name (`:556-562`), i.e. keys `repo_id`, `entry`, `trashed_at` when written by this module (`:576-582`).

`trash_remote_composite` — `{"repo_id": repo_id, "entry": entry, "trashed_at": "YYYY-MM-DDTHH:MM:SSZ"}` — `:592`;
`entry` = `f"{stamp}-{repo_id.replace('/', '__')}"` (`:574-575`, stamp `%Y%m%dT%H%M%SZ`).

`restore_remote_composite` — `{"repo_id": repo_id, "entry": entry, "restored": True}` — `:624`.

`CompositeBuildManager.start` job dict — `:686-700`: `composite_hash` (= key), `composite_repo_id`, `repo_ids` (list),
`runner`, `build_id` (`uuid.uuid4().hex[:12]`, `:685`), `state="verifying"`, `phase="verifying"`, `progress=0.0`,
`started_at` (float), `finished_at=None`, `error=None`, `log_tail=[]`, `cached=False`.
If the same hash is already building, `start` returns the existing dict without a new thread — `:676-683`.
`status(key)` accepts a hash **or** a `local/composite_<hash>` repo id — `:650-651`; unknown →
`{"composite_hash": composite_hash, "state": "unknown"}` — `:654`.
`active()` → jobs whose state is in `{"verifying", "merging", "publishing"}` — `:656-662`.

State literals: `"verifying"` `:692`, `"merging"`/`"publishing"` `:661, 679, 722`, `"ready"` `:786`,
`"failed"` `:777`, `"cancelled"` `:777`, `"cancelling"` `:729`, `"unknown"` `:654`.
`job["phase"]` mirrors `state` and is additionally overwritten by remote progress lines
(`if isinstance(payload.get("phase"), str): job["phase"] = payload["phase"]` — `:748-749`); the builder emits
`{"phase": "verifying", "progress": 0.02}` (`scripts/build_composite.py:303`), `"merging"`/`0.1` (`:324`),
`"publishing"`/`0.95` (`:334`), `"ready"`/`1.0` (`:300`, `:340`). Log tail is capped at 60 lines — `:732-738`.
`runner` is `"ssh_remote"` (default) or `"local"` (`:766-771`; `"local"` is documented "used by tests and the
standalone CLI, never by the API" — `:798`).

### Failure modes
`raise CompositeError(...)` sites:
- `:160` unbuildable `to_plan_payload()`;
- `:445` `raise CompositeError(f"训练机未配置：{exc}") from exc` (wraps `TrainServerConfig.from_env` errors, i.e. `DatasetSyncError`);
- `:509-511` list composites ssh exit != 0; `:515` unparsable list;
- `:572` `f"{repo_id} is not a composite dataset id"`;
- `:587` remote exit 3 (`{repo_id} 在训练机上不存在或未就绪`); `:589` exit 4 (trash entry exists); `:591` other non-zero;
- `:601` trash list ssh failure; `:605` unparsable trash list;
- `:613` `"trash entry must be a plain directory name"`; `:619` restore exit 3 (entry missing); `:621` exit 4 (target exists); `:623` other non-zero;
- `:674` `raise CompositeError("组合计划存在阻塞项，无法构建")` — **synchronous, from `start`** when `not plan.ok or plan.composite_hash is None`;
- in-worker: `:771` unsupported runner, `:803` `raise CompositeError("build cancelled")` (local build callback), `:865` cancelled, `:868` `训练机构建失败（ssh exit {return_code}）`.
In-worker exceptions never reach the caller: `except Exception as exc:  # noqa: BLE001 - surfaced through the status payload` → `state`/`phase` `"cancelled"` if `cancel.is_set()` else `"failed"`, `error=str(exc)`, `finished_at` — `:772-783`.
Note: the inline SSH helpers raise plain `RuntimeError`, **not** `CompositeError`
(`ssh_ops.py:84` "SSH client not available", `:86` "SSH command timed out after ...s", `:217` "Could not read {path} on ..."),
so `remote_status`/`list_*`/`trash_*`/`restore_*` can surface `RuntimeError` alongside `CompositeError`.

---

## 4. `lelab/model_store.py` — Jetson-side model store

### Accessors (no manager class; a module singleton for pull progress)
- `_PULL_STATE = ModelPullState()` — `:224`; `def pull_state() -> dict[str, Any]: return _PULL_STATE.snapshot()` — `:227-228`.
  `class ModelPullState` — `:208-221` ("Single-slot progress record for the UI (one transfer at a time)"),
  internal dict starts as `{"phase": "idle"}` — `:213`.
- Functions: `pull_model(config, *, job_id, remote_pretrained_dir, step=None, root=None, timeout=1800.0) -> dict` `:238-246`;
  `list_local_models(root=None) -> list[dict]` `:110`; `list_trashed_models(root=None) -> list[dict]` `:129`;
  `keep_local_models(env=None) -> int` `:61`; `trash_model(job_id, *, root=None) -> str` `:153`;
  `restore_model(trash_name, *, root=None) -> str` `:172`; `purge_model(trash_name, *, root=None) -> None` `:189`;
  `enforce_retention(*, keep=None, root=None) -> list[str]` `:197`; `read_manifest(job_id, root=None) -> dict | None` `:99`;
  `sha256_file(path) -> str` `:70`; `model_root(env=None) -> Path` `:53`.
  Called by server.py at `server.py:2127, 2132, 2145, 2192, 2194, 2204, 2228, 2245, 2248, 2256, 2259, 2269, 2272`.
- `model_root()` — `:53-58`: `LELAB_MODEL_ROOT` env if set (`:55`), else `Path.home() / "Desktop" / "lelab" / "models"` (`:58`).
- `keep_local_models()` — `:61-67`: `LELAB_LOCAL_MODEL_KEEP` (`:63`), else `DEFAULT_KEEP_LOCAL_MODELS = 10` (`:49`);
  `return max(1, int(raw)) if raw else DEFAULT_KEEP_LOCAL_MODELS` (`:65`), non-integer → default (`:66-67`).
- Layout docstring — `:16-26`: `<model_root>/<job_id>/model.json`, `<model_root>/<job_id>/.ready`,
  `<model_root>/<job_id>/checkpoints/<step>/pretrained_model/...`, `<model_root>/.trash/<job_id>-<ts>/`.

### Blocking behaviour
- `pull_model` is **B, end-to-end**: it returns only after rsync + SHA-256 verification + publish + retention.
  `completed = subprocess.run(rsync, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)` — `:266`
  (default `timeout=1800.0`, `:245`); local hash `:278`; remote hash over SSH `:279` → `ssh_ops.run_script(..., timeout=600.0)` (`:86`);
  publish `shutil.move(str(incoming), str(pretrained_target))` — `:289`; then `moved = enforce_retention(keep=keep_local_models(), root=base)` — `:300`
  in the same call. It does **not** spawn a thread itself (server.py wraps it: `threading.Thread(target=worker, ...).start()` `server.py:2238`).
- `pull_state` is **NB** (lock + dict copy, `:215-217`).
- `list_local_models` / `list_trashed_models` are **B** (directory walk + `_dir_size_mb` full `rglob` — `:90-96`, `:123`, `:144`).
- `trash_model` / `restore_model` / `purge_model` / `enforce_retention` are **B** (`shutil.move` `:168`, `:184`; `shutil.rmtree` `:194`; loop `:203-204`).
- `enforce_retention` "Move everything beyond the newest `keep` models into `.trash`" — `:198`; loop `for model in models[limit:]: moved.append(trash_model(model["job_id"], root=base))` — `:203-204`.

### Return shapes and phase literals
`pull_model` — `:290-302`: manifest `{"job_id": str, "step": int, "source": config.endpoint (str),
"remote_path": str, "pulled_at": float (epoch), "size_mb": float}` written to `model.json` (`:298`),
`.ready` = `"ok\n"` (`:299`), and the returned dict = `{**manifest, "path": str(target), "moved_to_trash": list[str]}` — `:302`.
`step_value = int(step) if step is not None else 0` — `:249`; target dir `<root>/<job_id>/checkpoints/{step_value:06d}/pretrained_model` — `:287`.
Staging dir `<root>/.<job_id>.incoming` is deleted at entry — `:250-253`.

`pull_state()` — snapshots of `_state`, updated at:
- `{"phase": "idle"}` initial — `:213`;
- `:254` `_PULL_STATE.update(phase="transferring", job_id=job_id, step=step_value, started_at=time.time())`;
- `:277` `phase="verifying"`;
- `:268` `phase="failed", error=f"传输超时（{timeout:.0f}s）"`;
- `:272` `phase="failed", error=detail` (rsync output tail);
- `:281` `phase="failed", error="SHA-256 校验不一致"`;
- `:301` `phase="completed", finished_at=time.time(), moved_to_trash=moved`.
Phase literals: `"idle"`, `"transferring"`, `"verifying"`, `"failed"`, `"completed"` (server.py's guard checks
`{"transferring", "verifying"}` — `server.py:2217`). Because `update` only merges, keys from an earlier phase
persist (e.g. a stale `error` or `job_id` is not cleared on the next successful pull — `:219-221`).

`list_local_models` — `[{**manifest, "job_id": str, "path": str, "size_mb": float}, ...]`, sorted
`key=lambda item: float(item.get("pulled_at") or 0), reverse=True` — `:122-126`; entries without a readable
`model.json` are skipped (`:120-121`); `.trash` skipped (`:117`); returns `[]` when root is absent (`:114-115`).

`list_trashed_models` — `[{"job_id", "trash_name", "step", "size_mb", "trashed_at", "path"}, ...]`,
sorted by `trashed_at` desc — `:139-149`.

`trash_model` → `target.name` = `f"{job_id}-{stamp}"` (`stamp = time.strftime("%Y%m%d-%H%M%S")`) — `:159-169`;
it writes `trashed_at: time.time()` into the copied manifest (`:163-165`) and removes `.ready` (`:166-167`).
`restore_model` → `job_id` (str) — `:186`; pops `trashed_at` (`:182`) and rewrites `.ready` = `"restored\n"` (`:185`).
`purge_model` → `None` (irreversible `shutil.rmtree`) — `:189-194`.
`enforce_retention` → `list[str]` of trash entry names — `:197-205`.
`keep_local_models` → `int`; `model_root` → `Path`; `read_manifest` → `dict | None` (`:99-107`, returns `None` on missing/unreadable/non-dict).

### Failure modes
- `raise FileNotFoundError(f"local model not found: {job_id}")` — `:158` (`trash_model`);
  `raise FileNotFoundError(f"trash entry not found: {trash_name}")` — `:176` (`restore_model`) and `:193` (`purge_model`).
- `raise FileExistsError(f"a local model named {job_id} already exists")` — `:181` (`restore_model`).
- `pull_model` raises, after first recording the failure in `pull_state()`:
  `raise TimeoutError(f"pull timed out after {timeout:.0f}s") from exc` — `:269` (`except subprocess.TimeoutExpired`, `:267`);
  `raise RuntimeError(f"rsync failed: {detail}")` — `:273`;
  `raise RuntimeError("SHA-256 mismatch after transfer; not publishing")` — `:282`.
- No error dicts are returned anywhere in this module — failures are exceptions plus the `pull_state()` `"failed"` phase.
- Verification is conditional: `weights = incoming / "model.safetensors"` / `if weights.is_file():` — `:275-276`;
  a checkpoint without `model.safetensors` is published **without** any SHA-256 comparison.
  A remote hash of `""` (empty output) also skips the mismatch branch — `if remote_sha and local_sha != remote_sha:` (`:280`).

---

## Cross-module summary

| operation | blocking? | proof |
|---|---|---|
| `DatasetSyncManager.start` | NB (thread) | `remote_sync.py:434-441` |
| `DatasetSyncManager.status/history/cancel` | NB (memory) | `remote_sync.py:377-378, 382-385, 445-454` |
| `SealManager.start` | checks B, hashing NB (thread) | `sealing.py:329,337,363-372` |
| `seal_status` / `SealManager.status` | B-trivial (manifest read / metadata read) | `sealing.py:107-120, 292-310` |
| `verify_seal` / `build_seal_manifest` | B (full re-hash) | `sealing.py:259, 198-207` |
| `plan_composite` | B (local metadata walk + fingerprints) | `composites.py:255, 267, 282` |
| `remote_status` / `list_remote_composites` / `list_remote_composite_trash` | B (SSH, 60 s default) | `composites.py:456-461, 507, 599`; `ssh_ops.py:73-88` |
| `trash_remote_composite` / `restore_remote_composite` | B (SSH, 120 s) | `composites.py:583-585, 615-617` |
| `CompositeBuildManager.start` | NB (thread) | `composites.py:705-714` |
| `pull_model` | B (rsync + SHA + retention, 1800 s) | `model_store.py:266, 279, 289, 300` |
| `list_local_models` / `list_trashed_models` / `trash_model` / `restore_model` / `purge_model` | B (fs) | `model_store.py:123, 144, 168, 184, 194` |
| `pull_state` / `keep_local_models` / `model_root` | NB | `model_store.py:215-217, 61-67, 53-58` |

Singleton instances: `dataset_sync_manager` `remote_sync.py:643`, `seal_manager` `sealing.py:443`,
`composite_build_manager` `composites.py:871`, `_PULL_STATE` `model_store.py:224`.
