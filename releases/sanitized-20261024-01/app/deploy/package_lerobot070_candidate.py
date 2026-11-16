#!/usr/bin/env python3
"""Build and rehearse a review-only LeLab 0.7 candidate package.

This tool never deploys, never changes `current`, and never writes outside the
chosen output directory. It packages the tracked app and sibling LeRobot source
plus the built frontend, excluding environments, model/data runs and credentials
according to deploy/source-excludes.txt. A local unpack/verify/tamper/rollback
rehearsal is performed so the release gate has a repeatable failure path.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

EXTRA_EXCLUDES = (
    ".git",
    ".venv",
    "node_modules",
    "frontend/node_modules",
    "frontend/dist-old",
    "docs/evidence",
    "logs",
    "runs",
    "*.log",
    "*.pem",
    "*.key",
    "servers.toml",
    "migration.env",
)


def _excludes(worktree: Path) -> list[str]:
    source = worktree / "releases/sanitized-20261024-01/app/deploy/source-excludes.txt"
    patterns = list(EXTRA_EXCLUDES)
    if source.exists():
        patterns += [
            line.strip()
            for line in source.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    return patterns


def _ignored(relative: str, patterns: Iterable[str]) -> bool:
    parts = Path(relative).parts
    if not parts:
        return False
    candidates = [relative, parts[-1], *parts]
    return any(fnmatch.fnmatch(text, pattern) for pattern in patterns for text in candidates)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_hashes(root: Path, patterns: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if _ignored(relative, patterns):
            continue
        result[relative] = _hash_file(path)
    return result


def _revision(worktree: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True
        ).strip()
    except Exception:  # noqa: BLE001 - a source snapshot without git is still packageable
        return "unknown"


def build(worktree: Path, output: Path) -> dict[str, object]:
    app = worktree / "releases/sanitized-20261024-01/app"
    library = worktree / "releases/sanitized-20261024-01/lerobot"
    dist = app / "frontend/dist"
    for path in (app, library, dist):
        if not path.is_dir():
            raise SystemExit(f"missing required source directory: {path}")
    index = dist / "index.html"
    js_files = sorted((dist / "assets").glob("*.js"))
    has_form = any("remoteDeployment" in path.read_text(errors="ignore") for path in js_files)
    if not index.is_file() or not has_form:
        raise SystemExit("dist does not contain the rebuilt remote-deployment form")

    patterns = _excludes(worktree)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    package_root = output / f"lelab-lerobot070-{run_id}"
    package_root.mkdir(parents=True, exist_ok=False)
    payload = package_root / "payload"
    payload.mkdir()

    included: dict[str, dict[str, str]] = {}
    for name, source in (("app", app), ("lerobot", library)):
        target = payload / name
        shutil.copytree(
            source,
            target,
            ignore=lambda _dir, names: {n for n in names if _ignored(n, patterns)},
            symlinks=False,
        )
        included[name] = _tree_hashes(target, patterns)

    hashes = {
        f"{component}/{path}": value
        for component, values in included.items()
        for path, value in values.items()
    }
    manifest = {
        "candidate": package_root.name,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "revision": _revision(worktree),
        "branch": "codex/lerobot-070-integration",
        "source": {
            "app": "releases/sanitized-20261024-01/app",
            "lerobot": "releases/sanitized-20261024-01/lerobot",
            "frontend_dist": "releases/sanitized-20261024-01/app/frontend/dist",
        },
        "excludes": patterns,
        "file_count": len(hashes),
        "sha256": hashes,
        "notes": "review candidate only; not published, current unchanged",
    }
    (package_root / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (package_root / "SHA256SUMS").write_text(
        "".join(f"{value}  {name}\n" for name, value in sorted(hashes.items()))
    )
    with tarfile.open(package_root / "candidate.tar.gz", "w:gz") as archive:
        archive.add(payload, arcname="payload")
        archive.add(package_root / "MANIFEST.json", arcname="MANIFEST.json")
        archive.add(package_root / "SHA256SUMS", arcname="SHA256SUMS")

    rehearsal = rehearse(package_root)
    (package_root / "REHEARSAL.json").write_text(json.dumps(rehearsal, indent=2, sort_keys=True) + "\n")
    return {"path": str(package_root), "file_count": len(hashes), "rehearsal": rehearsal}


def rehearse(package_root: Path) -> dict[str, object]:
    """Unpack, verify, tamper, reject, restore: no live target is touched."""
    result: dict[str, object] = {"unpack": "not_run", "verify": "not_run", "tamper_rejected": False, "restore_verified": False}
    with tempfile.TemporaryDirectory(prefix="lelab070-stack-") as temp:
        stack = Path(temp)
        with tarfile.open(package_root / "candidate.tar.gz") as archive:
            archive.extractall(stack, filter="data")
        result["unpack"] = "passed"
        manifest = json.loads((stack / "MANIFEST.json").read_text())
        payload = stack / "payload"
        expected = manifest["sha256"]

        def verify() -> bool:
            return all(
                (payload / name).is_file() and _hash_file(payload / name) == value
                for name, value in expected.items()
            )

        result["verify"] = "passed" if verify() else "failed"
        victim = next(name for name in expected if name.startswith("app/frontend/dist/"))
        target = payload / victim
        original = target.read_bytes()
        target.write_bytes(original + b"\n")
        result["tamper_rejected"] = not verify()
        target.write_bytes(original)
        result["restore_verified"] = verify()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    summary = build(args.worktree.resolve(), args.output.resolve())
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    sys.exit(main())
