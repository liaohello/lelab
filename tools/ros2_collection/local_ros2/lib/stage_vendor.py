#!/usr/bin/env python3
"""Stage the vendor message package as a read-only build input, with provenance.

The reference tree is never written to.  Each staged file records where it came
from and its SHA256, so a later arm64 build on the Pi can be compared against the
same source content instead of trusting a directory name.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.is_dir():
        raise SystemExit(f"vendor source is not a directory: {source}")
    files = sorted(p for p in source.rglob("*") if p.is_file())
    if not files:
        raise SystemExit(f"vendor source is empty: {source}")

    dest = args.dest
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    entries = []
    for path in files:
        relative = path.relative_to(source)
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        shutil.copystat(path, target)
        entries.append({"path": str(relative), "sha256": sha256(target),
                        "bytes": target.stat().st_size})

    manifest = {
        "package": source.name,
        "source_absolute_path": str(source),
        "staged_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "file_count": len(entries),
        "files": entries,
        "combined_sha256": hashlib.sha256(
            "".join(entry["sha256"] for entry in entries).encode()).hexdigest(),
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"staged {len(entries)} files from {source} -> {dest} combined={manifest['combined_sha256'][:16]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
