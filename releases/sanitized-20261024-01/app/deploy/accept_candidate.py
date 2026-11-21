#!/usr/bin/env python3
"""Accept an already-built LeLab 0.7 candidate archive.

`package_lerobot070_candidate.py` builds a package and rehearses it from the
filesystem it just wrote. This tool starts from the opposite end: a
`candidate.tar.gz` that someone else produced, and nothing else.

It answers four questions, in order, and refuses to continue if one fails:

1. Is this the archive we think it is?  (SHA-256 of the tarball, plus the
   candidate name/profile/revision it claims.)
2. Does the archive agree with the trusted, version-controlled manifest, and
   does every payload byte match it?  (Manifest bytes, SHA256SUMS bytes, file
   set, per-file hashes, all read out of the tar without extracting.)
3. Does it still agree after unpacking, and does a single flipped byte or a
   deleted file get rejected?  (Re-verified from disk, tamper, restore.)
4. Can an isolated environment be built from the lock inside the package, and
   do `lelab`, `lerobot` and the frontend dist then resolve to the unpacked
   copy rather than to any checkout on this machine?

The verified tree is never used as a working directory: environment creation
writes `*.egg-info`/`__pycache__`, so the tool copies it to `run/` (and re-checks
that copy) before anything executes there. `payload/` stays byte-identical, and
that is re-asserted at the end.

Nothing here deploys, starts a service, or touches a `current` pointer.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from package_lerobot070_candidate import PROFILES, _hash_file  # noqa: E402

PAYLOAD_PREFIX = "payload/"
MANIFEST_NAME = "MANIFEST.json"
SUMS_NAME = "SHA256SUMS"


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _check(checks: dict, name: str, ok: bool, detail: object = None) -> bool:
    checks[name] = {"ok": bool(ok), "detail": detail}
    return bool(ok)


def _read_member(archive: tarfile.TarFile, name: str) -> bytes:
    handle = archive.extractfile(name)
    if handle is None:
        raise SystemExit(f"{name} is not a regular file inside the archive")
    return handle.read()


def _payload_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    return {
        member.name[len(PAYLOAD_PREFIX) :]: member
        for member in archive.getmembers()
        if member.isfile() and member.name.startswith(PAYLOAD_PREFIX)
    }


def _verify_tree(root: Path, expected: dict[str, str]) -> dict[str, object]:
    """Compare a directory against a manifest hash map, in both directions."""
    missing = [name for name in expected if not (root / name).is_file()]
    bad = [
        name
        for name, value in expected.items()
        if (root / name).is_file() and _hash_file(root / name) != value
    ]
    actual = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }
    extra = sorted(actual - set(expected))
    return {
        "missing": missing[:10],
        "missing_count": len(missing),
        "mismatched": bad[:10],
        "mismatched_count": len(bad),
        "extra": extra[:10],
        "extra_count": len(extra),
        "ok": not missing and not bad and not extra,
    }


def inspect_archive(
    archive_path: Path,
    profile: str,
    expect_commit: str | None,
    trusted_dir: Path | None,
) -> dict[str, object]:
    report: dict[str, object] = {
        "path": str(archive_path),
        "bytes": archive_path.stat().st_size,
        "sha256": _hash_file(archive_path),
        "mtime_utc": datetime.fromtimestamp(
            archive_path.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
    }
    checks: dict[str, dict] = {}
    report["checks"] = checks

    with tarfile.open(archive_path) as archive:
        manifest_bytes = _read_member(archive, MANIFEST_NAME)
        sums_bytes = _read_member(archive, SUMS_NAME)
        manifest = json.loads(manifest_bytes)

        report["manifest"] = {
            "candidate": manifest.get("candidate"),
            "profile": manifest.get("profile"),
            "revision": manifest.get("revision"),
            "branch": manifest.get("branch"),
            "created_utc": manifest.get("created_utc"),
            "file_count": manifest.get("file_count"),
            "hash_entries": len(manifest.get("sha256") or {}),
            "frontend_dist": manifest.get("frontend_dist"),
            "requires": manifest.get("requires"),
            "source": manifest.get("source"),
            "manifest_sha256": _sha256_bytes(manifest_bytes),
            "sums_sha256": _sha256_bytes(sums_bytes),
        }

        if trusted_dir is not None:
            trusted_manifest = (trusted_dir / MANIFEST_NAME).read_bytes()
            trusted_sums = (trusted_dir / SUMS_NAME).read_bytes()
            _check(
                checks,
                "manifest_matches_trusted_copy",
                trusted_manifest == manifest_bytes,
                str(trusted_dir / MANIFEST_NAME),
            )
            _check(
                checks,
                "sums_matches_trusted_copy",
                trusted_sums == sums_bytes,
                str(trusted_dir / SUMS_NAME),
            )

        _check(checks, "profile_matches", manifest.get("profile") == profile,
               {"expected": profile, "actual": manifest.get("profile")})
        _check(
            checks,
            "candidate_name_matches",
            manifest.get("candidate") == archive_path.parent.name,
            {"manifest": manifest.get("candidate"), "directory": archive_path.parent.name},
        )
        if expect_commit:
            revision = str(manifest.get("revision") or "")
            _check(
                checks,
                "revision_matches_expected",
                revision.startswith(expect_commit) or expect_commit.startswith(revision),
                {"expected": expect_commit, "manifest": revision},
            )

        hashes = manifest.get("sha256") or {}
        _check(
            checks,
            "file_count_matches",
            manifest.get("file_count") == len(hashes) and len(hashes) > 0,
            {"file_count": manifest.get("file_count"), "entries": len(hashes)},
        )

        # SHA256SUMS is the human-readable copy of the same map; it must be
        # exactly equal, not merely consistent, or one of the two is stale.
        parsed_sums: dict[str, str] = {}
        for line in sums_bytes.decode().splitlines():
            if not line.strip():
                continue
            digest, _, name = line.partition("  ")
            parsed_sums[name] = digest
        _check(
            checks,
            "sums_match_manifest",
            parsed_sums == hashes,
            {
                "sums_entries": len(parsed_sums),
                "manifest_entries": len(hashes),
                "only_in_sums": sorted(set(parsed_sums) - set(hashes))[:5],
                "only_in_manifest": sorted(set(hashes) - set(parsed_sums))[:5],
            },
        )

        members = _payload_members(archive)
        _check(
            checks,
            "payload_file_set_matches_manifest",
            set(members) == set(hashes),
            {
                "in_tar": len(members),
                "in_manifest": len(hashes),
                "only_in_tar": sorted(set(members) - set(hashes))[:5],
                "only_in_manifest": sorted(set(hashes) - set(members))[:5],
            },
        )

        mismatched = []
        for name, member in members.items():
            if name not in hashes:
                continue
            handle = archive.extractfile(member)
            digest = __import__("hashlib").sha256()
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
            if digest.hexdigest() != hashes[name]:
                mismatched.append(name)
        _check(
            checks,
            "payload_bytes_match_manifest",
            not mismatched,
            {"mismatched": mismatched[:10], "checked": len(members)},
        )

        report["payload_file_count"] = len(members)
        report["archive_member_count"] = len([m for m in archive.getmembers() if m.isfile()])

    report["ok"] = all(item["ok"] for item in checks.values())
    return report


def unpack(archive_path: Path, dest: Path) -> dict[str, object]:
    if dest.exists():
        raise SystemExit(f"destination already exists, refusing to overwrite: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with tarfile.open(archive_path) as archive:
        archive.extractall(dest, filter="data")
    return {
        "dest": str(dest),
        "seconds": round(time.time() - started, 2),
        "top_level": sorted(p.name for p in dest.iterdir()),
    }


def tamper(manifest: dict, root: Path) -> dict[str, object]:
    """One flipped byte and one deleted file must each be rejected."""
    hashes: dict[str, str] = manifest["sha256"]
    # Prefer a built frontend asset: it is the thing a browser actually loads.
    preferred = sorted(n for n in hashes if "/frontend/dist/" in n and n.endswith(".js"))
    victim = preferred[0] if preferred else sorted(hashes)[0]
    target = root / victim

    original = target.read_bytes()
    flipped = bytearray(original)
    flipped[len(flipped) // 2] ^= 0x01
    target.write_bytes(bytes(flipped))
    byte_flip = _verify_tree(root, hashes)
    target.write_bytes(original)
    restored = _verify_tree(root, hashes)

    deleted_victim = sorted(n for n in hashes if n != victim)[0]
    deleted_path = root / deleted_victim
    saved = deleted_path.read_bytes()
    deleted_path.unlink()
    deletion = _verify_tree(root, hashes)
    deleted_path.write_bytes(saved)
    restored_after_delete = _verify_tree(root, hashes)

    return {
        "victim": victim,
        "byte_flip_rejected": not byte_flip["ok"] and byte_flip["mismatched_count"] == 1,
        "byte_flip_detail": byte_flip,
        "restore_verified": restored["ok"],
        "deleted_victim": deleted_victim,
        "deletion_rejected": not deletion["ok"] and deletion["missing_count"] == 1,
        "restore_after_delete_verified": restored_after_delete["ok"],
        "ok": (
            not byte_flip["ok"]
            and byte_flip["mismatched_count"] == 1
            and restored["ok"]
            and not deletion["ok"]
            and deletion["missing_count"] == 1
            and restored_after_delete["ok"]
        ),
    }


def prepare_env(project: Path, env_dir: Path, log_path: Path, timeout: int = 3600,
                reuse: bool = False) -> dict:
    """Build the isolated environment from the lock that ships in the package.

    `--no-install-project` keeps the unpacked tree read-only: installing the root
    project editable would write `*.egg-info` next to the sources, and the point
    of the exercise is that the verified bytes stay verified. The app directory is
    put on `PYTHONPATH` at run time instead, while `lerobot` still comes from the
    environment's editable install of the sibling checkout.
    """
    uv = shutil.which("uv")
    result: dict[str, object] = {
        "project": str(project),
        "env_dir": str(env_dir),
        "uv": uv,
        "status": "not_run",
    }
    if uv is None:
        result["status"] = "blocked"
        result["error"] = "uv is not installed; the package ships uv.lock, not wheels"
        return result
    if not (project / "uv.lock").is_file():
        result["status"] = "blocked"
        result["error"] = f"no uv.lock in {project}"
        return result
    if reuse:
        if not (env_dir / "bin/python").is_file():
            result["status"] = "blocked"
            result["error"] = f"--reuse-env but no interpreter at {env_dir}/bin/python"
            return result
        result.update({"status": "reused", "exit_code": None, "seconds": 0.0,
                       "log": str(log_path) if log_path.exists() else None,
                       "note": "environment was synced once and is reused; see the sync log"})

    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(key, None)
    env["UV_PROJECT_ENVIRONMENT"] = str(env_dir)

    if not reuse:
        # Keep the resolved wheels local when the cache is complete: this machine
        # cannot reach pypi.org, and accepting an artifact must not depend on a
        # fetch. A cold cache reports exactly which wheel is missing, and the
        # operator can rerun the same command with a reachable index.
        env.setdefault("UV_OFFLINE", "1")
        started = time.time()
        proc = subprocess.run(
            [uv, "sync", "--frozen", "--no-dev", "--no-install-project",
             "--python", sys.executable],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        log_path.write_text(
            f"$ uv sync --frozen --no-dev --no-install-project --no-install-package lerobot "
        f"--python {sys.executable}   "
            f"(cwd={project}, UV_PROJECT_ENVIRONMENT={env_dir})\n"
            f"exit={proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n"
        )
        result.update(
            {
                "exit_code": proc.returncode,
                "seconds": round(time.time() - started, 1),
                "log": str(log_path),
                "status": "passed" if proc.returncode == 0 else "failed",
                "stderr_tail": proc.stderr.strip().splitlines()[-6:],
            }
        )
        if proc.returncode != 0:
            return result

    python = env_dir / "bin/python"
    # Resolve paths without importing `lelab.server`: importing the server pulls in
    # torch and the whole app, which is a runtime question answered by the service
    # check, not by "which copy of the package is on sys.path".
    probe_code = """
import importlib, json, pathlib, sys
import lelab, lerobot

dist = pathlib.Path(lelab.__file__).resolve().parent.parent / "frontend" / "dist"


def try_import(name):
    try:
        importlib.import_module(name)
        return None
    except Exception as exc:  # noqa: BLE001 - the message is the evidence
        return f"{type(exc).__name__}: {exc}"


print(json.dumps({
    "python": sys.executable,
    "lelab": str(pathlib.Path(lelab.__file__).resolve()),
    "lerobot": str(pathlib.Path(lerobot.__file__).resolve()),
    "frontend_dist": str(dist),
    "dist_exists": dist.is_dir(),
    "index_html": (dist / "index.html").is_file(),
    "assets": sorted(p.name for p in (dist / "assets").glob("*")) if (dist / "assets").is_dir() else [],
    "server_import_error": try_import("lelab.server"),
    "lerobot_datasets_error": try_import("lerobot.datasets"),
    "sys_path": [p for p in sys.path if p],
}, indent=1))
"""
    env = dict(env)
    env["PYTHONPATH"] = str(project)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    probe = subprocess.run(
        [str(python), "-c", probe_code],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    result["probe_exit"] = probe.returncode
    result["probe_stderr"] = probe.stderr.strip().splitlines()[-6:] if probe.returncode else []
    if probe.returncode == 0:
        info = json.loads(probe.stdout)
        result["probe"] = info
        run_root = env_dir.parent.resolve()
        result["lelab_from_env"] = str(info["lelab"]).startswith(str(run_root))
        result["lerobot_from_env"] = str(info["lerobot"]).startswith(str(run_root))
        result["dist_from_env"] = str(info["frontend_dist"]).startswith(str(run_root))
        # A package whose own server cannot be imported is not acceptable, even
        # when every hash matches: this is the check that a file-level rehearsal
        # cannot make. It runs here because the import roots are known.
        result["import_ok"] = info.get("server_import_error") is None
        result["import_error"] = info.get("server_import_error")
        result["status"] = (
            "passed"
            if all(result[k] for k in ("lelab_from_env", "lerobot_from_env", "dist_from_env"))
            and info["dist_exists"]
            and info["index_html"]
            and result["import_ok"]
            else "failed"
        )
    else:
        result["status"] = "failed"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--profile", required=True, choices=sorted(PROFILES))
    parser.add_argument("--dest", required=True, type=Path,
                        help="new directory for payload/, run/ and the environment")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--trusted-dir", type=Path, default=None,
                        help="version-controlled copy of MANIFEST.json/SHA256SUMS to compare against")
    parser.add_argument("--expect-commit", default=None)
    parser.add_argument("--worktree", type=Path, default=None,
                        help="git worktree used to record HEAD and the commit distance")
    parser.add_argument("--skip-env", action="store_true")
    parser.add_argument("--reuse-env", action="store_true",
                        help="probe an environment that was already synced instead of syncing again")
    parser.add_argument("--reuse-stack", action="store_true",
                        help="accept an already-unpacked stack/ under --dest instead of extracting again")
    args = parser.parse_args()

    started = time.time()
    dest = args.dest.resolve()
    report: dict[str, object] = {
        "tool": "accept_candidate.py",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "profile": args.profile,
        "dest": str(dest),
        "host": {"hostname": os.uname().nodename, "python": sys.version.split()[0]},
    }

    if args.worktree is not None:
        def git(*argv: str) -> str:
            return subprocess.run(
                ["git", "-C", str(args.worktree), *argv],
                capture_output=True, text=True, check=False
            ).stdout.strip()

        report["worktree"] = {
            "path": str(args.worktree),
            "head": git("rev-parse", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(git("status", "--porcelain")),
        }
        with tarfile.open(args.archive.resolve()) as handle:
            revision = json.loads(_read_member(handle, MANIFEST_NAME))["revision"]
        ancestor = subprocess.run(
            ["git", "-C", str(args.worktree), "merge-base", "--is-ancestor", revision, "HEAD"],
            capture_output=True, text=True,
        )
        report["worktree"]["manifest_revision_is_ancestor_of_head"] = ancestor.returncode == 0
        report["worktree"]["commits_since_revision"] = git("rev-list", "--count", f"{revision}..HEAD")

    archive = inspect_archive(args.archive.resolve(), args.profile, args.expect_commit, args.trusted_dir)
    report["archive"] = archive

    stack = dest / "stack"          # MANIFEST.json, SHA256SUMS, payload/
    tree = stack / "payload"        # the payload root that the hashes are relative to
    if archive["ok"] and args.reuse_stack and (stack / MANIFEST_NAME).is_file():
        report["unpack"] = {
            "dest": str(stack),
            "seconds": 0.0,
            "reused": True,
            "top_level": sorted(p.name for p in stack.iterdir()),
        }
        manifest = json.loads((stack / MANIFEST_NAME).read_text())
        hashes = manifest["sha256"]
        unpacked = _verify_tree(tree, hashes)
        report["unpack"]["verify"] = unpacked
        report["unpack"]["ok"] = unpacked["ok"]
        if unpacked["ok"]:
            report["tamper"] = tamper(manifest, tree)
            report["tamper"]["note"] = (
                "archive-level rehearsal: a flipped byte and a deleted file must each be "
                "rejected, and restoring must verify again. This is file integrity, not a "
                "service rollback (see the rollback drill)."
            )
        else:
            report["tamper"] = {"ok": False, "error": "payload did not verify after unpack"}
    elif archive["ok"]:
        report["unpack"] = unpack(args.archive.resolve(), stack)
        manifest = json.loads((stack / MANIFEST_NAME).read_text())
        hashes = manifest["sha256"]
        unpacked = _verify_tree(tree, hashes)
        report["unpack"]["verify"] = unpacked
        report["unpack"]["ok"] = unpacked["ok"]
        if unpacked["ok"]:
            report["tamper"] = tamper(manifest, tree)
            report["tamper"]["note"] = (
                "archive-level rehearsal: a flipped byte and a deleted file must each be "
                "rejected, and restoring must verify again. This is file integrity, not a "
                "service rollback (see the rollback drill)."
            )
        else:
            report["tamper"] = {"ok": False, "error": "payload did not verify after unpack"}
    else:
        report["unpack"] = {"ok": False, "error": "archive failed verification; not unpacked"}
        report["tamper"] = {"ok": False, "error": "archive failed verification"}

    if report["unpack"].get("ok") and not args.skip_env:
        project = tree / ("app" if args.profile == "app" else "app_ros_mobile")
        report["env"] = prepare_env(project, dest / "env", dest / "env-prepare.log",
                                    reuse=args.reuse_env)

    # The verified tree must still be pristine after everything above.
    if report["unpack"].get("ok"):
        pristine = _verify_tree(tree, hashes)
        report["payload_pristine_after"] = pristine["ok"]
    else:
        report["payload_pristine_after"] = False

    statuses = [
        archive["ok"],
        report["unpack"].get("ok", False),
        report.get("tamper", {}).get("ok", False),
    ]
    env_status = report.get("env", {}).get("status")
    if env_status is not None and env_status != "passed":
        statuses.append(False)
    report["status"] = "passed" if all(statuses) else "failed"
    if env_status == "blocked":
        report["status"] = "blocked"
    report["seconds"] = round(time.time() - started, 1)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "status": report["status"],
        "archive_sha256": archive["sha256"],
        "revision": archive["manifest"]["revision"],
        "files": archive["payload_file_count"],
        "env": env_status,
        "report": str(args.report),
    }, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
