"""Pull LIBERO development files and results from the training host over SSH.

Dry-run is the default. Apply preserves replaced files in a timestamped backup,
verifies SHA-256, and never changes files on the server.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import time


REMOTE_INVENTORY = r'''
from pathlib import Path
import hashlib, json
root=Path(REMOTE_ROOT).expanduser().resolve()
if not root.is_dir():
    raise SystemExit('Remote LIBERO directory does not exist')
paths=[]
for directory in ['scripts','results']:
    base=root/directory
    if not base.is_dir(): continue
    for path in base.rglob('*'):
        if not path.is_file() or path.is_symlink() or any(part in ['__pycache__','.venv','node_modules'] for part in path.parts):
            continue
        if root not in path.resolve().parents:
            raise SystemExit('File escapes LIBERO directory')
        paths.append(path)
for name in ['deployment.json','libero.env','.lelab-libero.json']:
    if (root/name).is_file(): paths.append(root/name)
paths.extend(p for p in root.glob('DEPLOYMENT*.md') if p.is_file())
files=[]
for path in sorted(set(paths)):
    content=path.read_bytes()
    files.append({'path':str(path.relative_to(root)), 'size':len(content),
                  'sha256':hashlib.sha256(content).hexdigest(),
                  'lf_sha256':hashlib.sha256(content.replace(b'\r\n',b'\n')).hexdigest(),
                  'mtime':path.stat().st_mtime})
print(json.dumps({'remote_root':str(root),'files':files}))
'''


def sha256(path: Path, *, normalize: bool = False) -> str:
    if normalize:
        return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} or ":" in part or "\\" in part for part in path.parts):
        raise ValueError(f"Unsafe remote path: {value}")
    return Path(*path.parts)


def ssh_run(command: list[str], script: str, *, output: Path | None = None) -> bytes:
    for attempt in range(3):
        if output:
            with output.open("wb") as stream:
                result = subprocess.run(command, input=script.encode("utf-8"), stdout=stream,
                                        stderr=subprocess.PIPE, timeout=600, check=False)
        else:
            result = subprocess.run(command, input=script.encode("utf-8"), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=180, check=False)
        # Only retry failures before authentication/remote execution.
        transient = result.returncode == 255 and result.stderr.strip().startswith(b"Connection closed by ")
        if transient and attempt < 2:
            time.sleep(3)
            continue
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
        return result.stdout if output is None else b""
    raise RuntimeError("SSH connection failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("LELAB_TRAIN_HOST"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LELAB_TRAIN_PORT", "22")))
    parser.add_argument("--user", default=os.environ.get("LELAB_TRAIN_USER"))
    parser.add_argument("--remote-root", default=os.environ.get("LELAB_LIBERO_ROOT"))
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--max-mb", type=int, default=512)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not all((args.host, args.user, args.remote_root)):
        parser.error("Set --host, --user and --remote-root, or LELAB_TRAIN_HOST/LELAB_TRAIN_USER/LELAB_LIBERO_ROOT")
    repo = args.repo_root.resolve()
    runtime = repo / "runs" / "libero" / args.host
    if not runtime.resolve().is_relative_to(repo):
        parser.error("Runtime path must remain inside the repository")
    command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=10", "-p", str(args.port), f"{args.user}@{args.host}", "python3", "-"]
    inventory = json.loads(ssh_run(command, "REMOTE_ROOT=" + repr(args.remote_root) + "\n" + REMOTE_INVENTORY))
    entries = inventory["files"]
    total_bytes = sum(entry["size"] for entry in entries)
    if total_bytes > args.max_mb * 1024 * 1024:
        parser.error(f"Selected files exceed {args.max_mb} MiB; increase --max-mb if intended")
    destinations = {}
    for entry in entries:
        relative = safe_relative(entry["path"])
        if relative.parts[0] == "scripts" and len(relative.parts) > 1:
            destination = repo / "deploy" / "train" / "libero" / Path(*relative.parts[1:])
        elif len(relative.parts) == 1 and relative.name.startswith("DEPLOYMENT") and relative.suffix == ".md":
            destination = repo / "deploy" / "train" / "libero" / relative.name
        else:
            destination = runtime / relative
        if not destination.resolve().is_relative_to(repo):
            raise ValueError(f"Destination escapes repository: {destination}")
        destinations[entry["path"]] = destination
    changes = []
    for entry in entries:
        destination = destinations[entry["path"]]
        same = destination.is_file() and sha256(destination, normalize=True) == entry["lf_sha256"]
        if not same:
            changes.append({"remote": entry["path"], "local": str(destination.relative_to(repo)),
                            "action": "update_with_backup" if destination.exists() else "create"})
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "files": len(entries),
                      "bytes": total_bytes, "changes": changes}, indent=2, ensure_ascii=False))
    if not args.apply:
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    staging = runtime / ".sync-staging" / stamp
    staging.mkdir(parents=True, exist_ok=False)
    archive = staging / "download.tar.gz"
    archive_script = (
        "from pathlib import Path\nimport tarfile,sys\n"
        "root=Path(" + repr(inventory["remote_root"]) + ").resolve()\n"
        "files=" + repr([entry["path"] for entry in entries]) + "\n"
        "with tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz') as archive:\n"
        "    for relative in files:\n"
        "        path=root/relative\n"
        "        if path.is_symlink() or root not in path.resolve().parents:\n"
        "            raise RuntimeError('File escapes remote root')\n"
        "        archive.add(path,arcname=relative,recursive=False)\n"
    )
    ssh_run(command, archive_script, output=archive)
    expected = {entry["path"]: entry for entry in entries}
    received = set()
    with tarfile.open(archive, "r:gz") as packed:
        for member in packed:
            if not member.isfile() or member.name not in expected or member.name in received:
                raise RuntimeError(f"Unexpected archive member: {member.name}")
            staged = staging / "files" / safe_relative(member.name)
            staged.parent.mkdir(parents=True, exist_ok=True)
            with packed.extractfile(member) as source, staged.open("wb") as target:
                shutil.copyfileobj(source, target)
            entry = expected[member.name]
            if staged.stat().st_size != entry["size"] or sha256(staged) != entry["sha256"]:
                raise RuntimeError(f"Remote file changed during download: {member.name}; no files applied")
            received.add(member.name)
    if received != set(expected):
        raise RuntimeError("Archive is incomplete; no files applied")

    updated = []
    backups = []
    for entry in entries:
        destination = destinations[entry["path"]]
        if destination.is_file() and sha256(destination, normalize=True) == entry["lf_sha256"]:
            continue
        if destination.exists():
            backup = runtime / ".sync-backups" / stamp / destination.relative_to(repo)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup)
            backups.append(str(backup.relative_to(repo)))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staging / "files" / safe_relative(entry["path"]), destination)
        updated.append(str(destination.relative_to(repo)))
    # Verify every destination, including files retained due only to CRLF/LF differences.
    for entry in entries:
        if sha256(destinations[entry["path"]], normalize=True) != entry["lf_sha256"]:
            raise RuntimeError(f"Final verification failed: {entry['path']}")
    report = {"synced_utc": datetime.now(timezone.utc).isoformat(),
              "server": f"{args.user}@{args.host}:{args.port}", **inventory,
              "destinations": {name: str(path.relative_to(repo)) for name, path in destinations.items()},
              "updated": updated, "backups": backups, "verified_files": len(entries)}
    (runtime / "sync-manifest.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Verified {len(entries)} files. Updated {len(updated)}; backups {len(backups)}.")
    print(f"Local results and configurations: {runtime}")


if __name__ == "__main__":
    main()
