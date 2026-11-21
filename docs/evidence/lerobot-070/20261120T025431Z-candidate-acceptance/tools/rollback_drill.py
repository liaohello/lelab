#!/usr/bin/env python3
"""Service-level rollback drill: pointer, real process, real health check.

This is deliberately *not* the archive rehearsal (flip a byte, verify, restore).
That one proves the bytes are intact. This one proves the deploy path can put a
previous release back in front of traffic:

    old healthy -> switch current -> candidate healthy
                -> inject a hung service -> health fails
                -> switch current back -> old healthy again

The failure is injected with SIGSTOP: the process is alive, still owns the port
and simply stops answering. That is the failure a health check is for, and it is
reversible without touching either release's files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

STEPS: list[dict] = []


def step(name: str, ok: bool, detail: object = None) -> bool:
    STEPS.append({"name": name, "ok": bool(ok), "detail": detail})
    return bool(ok)


def http(url: str, timeout: float = 3.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception:
        return 0, b""


def healthy(port: int, timeout: float = 3.0) -> tuple[bool, str]:
    status, body = http(f"http://127.0.0.1:{port}/health", timeout=timeout)
    return status == 200, body.decode("utf-8", errors="replace")[:200]


def wait_health(port: int, deadline: float) -> tuple[bool, float]:
    started = time.time()
    while time.time() - started < deadline:
        ok, _ = healthy(port)
        if ok:
            return True, round(time.time() - started, 1)
        time.sleep(1.0)
    return False, round(time.time() - started, 1)


def port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def served_asset(port: int) -> dict:
    status, body = http(f"http://127.0.0.1:{port}/", timeout=5.0)
    if status != 200:
        return {"status": status}
    text = body.decode("utf-8", errors="replace")
    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', text)
    out = {"status": status, "index_sha256": hashlib.sha256(body).hexdigest(), "assets": {}}
    for asset in sorted(set(assets)):
        astatus, abody = http(f"http://127.0.0.1:{port}{asset}", timeout=10.0)
        out["assets"][asset] = {"status": astatus, "sha256": hashlib.sha256(abody).hexdigest()}
    return out


def unpack(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as handle:
        handle.extractall(dest, filter="data")


def start(env_python: Path, launcher: Path, current: Path, project_subdir: str,
          port: int, boot: Path, log: Path, cwd: Path) -> subprocess.Popen:
    if boot.exists():
        boot.unlink()
    env = {k: v for k, v in os.environ.items()
           if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
    env.update(
        {
            "LELAB_ACCEPT_CURRENT": str(current),
            "LELAB_ACCEPT_PROJECT": project_subdir,
            "LELAB_ACCEPT_PORT": str(port),
            "LELAB_ACCEPT_BOOT": str(boot),
            "PYTHONUNBUFFERED": "1",
            "HF_HUB_OFFLINE": "1",
        }
    )
    handle = log.open("a", buffering=1)
    handle.write(f"\n=== start {time.strftime('%H:%M:%S')} current -> {os.readlink(current)} ===\n")
    proc = subprocess.Popen(
        [str(env_python), str(launcher)],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._log_handle = handle  # type: ignore[attr-defined]
    return proc


def stop(proc: subprocess.Popen, grace: float = 15.0) -> dict:
    started = time.time()
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            code = proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait(timeout=10)
    else:
        code = proc.returncode
    handle = getattr(proc, "_log_handle", None)
    if handle is not None:
        handle.close()
    return {"exit_code": code, "seconds": round(time.time() - started, 1)}


def manifest_of(release: Path) -> dict:
    return json.loads((release / "MANIFEST.json").read_text())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-archive", type=Path, default=None)
    parser.add_argument("--candidate-archive", type=Path, default=None)
    parser.add_argument("--old-dir", type=Path, default=None,
                        help="pre-unpacked old release; used instead of --old-archive")
    parser.add_argument("--candidate-dir", type=Path, default=None,
                        help="pre-unpacked candidate release; used instead of --candidate-archive")
    parser.add_argument("--env", required=True, type=Path)
    parser.add_argument("--project", default="app", help="payload subdirectory to run")
    parser.add_argument("--launcher", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8021)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--health-deadline", type=float, default=180.0)
    args = parser.parse_args()

    root = args.root.resolve()
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    env_python = args.env / "bin/python"
    current = root / "current"
    boot = root / "boot.json"
    report: dict = {
        "root": str(root),
        "port": args.port,
        "project": args.project,
        "releases": {},
    }

    pairs = (("old", args.old_archive, args.old_dir),
             ("candidate", args.candidate_archive, args.candidate_dir))
    for name, archive, directory in pairs:
        target = releases / name
        started = time.time()
        entry: dict = {}
        if directory is not None:
            shutil.copytree(directory.resolve(), target, symlinks=True)
            entry["source_dir"] = str(directory.resolve())
            entry["archive"] = str(archive.resolve()) if archive else None
            entry["archive_sha256"] = (
                hashlib.sha256(archive.read_bytes()).hexdigest() if archive else None
            )
            entry["note"] = "pre-unpacked release directory; archive hash recorded for provenance only"
        elif archive is not None:
            unpack(archive.resolve(), target)
            entry["archive"] = str(archive.resolve())
            entry["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        else:
            raise SystemExit(f"need --{name}-archive or --{name}-dir")
        manifest = manifest_of(target)
        entry.update({
            "revision": manifest["revision"],
            "candidate": manifest["candidate"],
            "files": manifest["file_count"],
            "unpack_seconds": round(time.time() - started, 1),
        })
        report["releases"][name] = entry

    if current.is_symlink() or current.exists():
        current.unlink()
    current.symlink_to(releases / "old")
    report["pointer_initial"] = os.readlink(current)

    # --- 1. old release must be healthy ---------------------------------
    proc = start(env_python, args.launcher, current, args.project, args.port, boot,
                 args.log, releases / "old")
    ok, seconds = wait_health(args.port, args.health_deadline)
    old_boot = json.loads(boot.read_text()) if boot.exists() else {}
    old_asset = served_asset(args.port) if ok else {}
    report["old_running"] = {"healthy": ok, "seconds": seconds, "boot": old_boot, "asset": old_asset}
    step("old_release_healthy", ok, {"seconds": seconds})
    old_expected = [
        f"{args.project}/frontend/dist{path}"
        for path in old_asset.get("assets", {})
    ]
    old_hashes = manifest_of(releases / "old")["sha256"]
    step(
        "old_serves_its_own_dist",
        bool(old_expected) and all(
            old_asset["assets"][path]["sha256"] == old_hashes.get(key)
            for path, key in zip(old_asset.get("assets", {}), old_expected)
        ),
        {path: old_asset["assets"][path]["sha256"][:12] for path in old_asset.get("assets", {})},
    )
    step("old_boot_matches_pointer",
         old_boot.get("current_resolved") == str((releases / "old").resolve())
         and old_boot.get("lelab_file", "").startswith(str((releases / "old").resolve())),
         old_boot)
    report["stop_old"] = stop(proc)
    step("old_stopped", report["stop_old"]["exit_code"] is not None, report["stop_old"])
    step("port_free_before_switch", not port_open("127.0.0.1", args.port))

    # --- 2. switch to the candidate -------------------------------------
    current.unlink()
    current.symlink_to(releases / "candidate")
    report["pointer_switched"] = os.readlink(current)
    proc = start(env_python, args.launcher, current, args.project, args.port, boot,
                 args.log, releases / "candidate")
    ok, seconds = wait_health(args.port, args.health_deadline)
    cand_boot = json.loads(boot.read_text()) if boot.exists() else {}
    cand_asset = served_asset(args.port) if ok else {}
    report["candidate_running"] = {"healthy": ok, "seconds": seconds, "boot": cand_boot,
                                   "asset": cand_asset}
    step("candidate_release_healthy", ok, {"seconds": seconds})
    step("candidate_boot_matches_pointer",
         cand_boot.get("current_resolved") == str((releases / "candidate").resolve())
         and cand_boot.get("lelab_file", "").startswith(str((releases / "candidate").resolve())),
         cand_boot)

    cand_hashes = manifest_of(releases / "candidate")["sha256"]
    changed = {
        path: {"candidate": cand_asset.get("assets", {}).get(path, {}).get("sha256", "")[:12],
               "old": old_asset.get("assets", {}).get(path, {}).get("sha256", "")[:12]}
        for path in cand_asset.get("assets", {})
    }
    step(
        "candidate_dist_differs_from_old",
        any(v["candidate"] != v["old"] for v in changed.values()),
        changed,
    )
    step(
        "candidate_serves_its_own_dist",
        all(
            cand_asset["assets"][path]["sha256"] == cand_hashes.get(f"{args.project}/frontend/dist{path}")
            for path in cand_asset.get("assets", {})
        ),
        {path: v["sha256"][:12] for path, v in cand_asset.get("assets", {}).items()},
    )

    # --- 3. inject a controllable health failure ------------------------
    probes = []
    proc.send_signal(signal.SIGSTOP)
    injected_at = time.time()
    for attempt in range(3):
        started = time.time()
        ok_probe, body = healthy(args.port, timeout=3.0)
        probes.append({"attempt": attempt + 1, "ok": ok_probe,
                       "seconds": round(time.time() - started, 2), "body": body})
    failure_seconds = round(time.time() - injected_at, 1)
    report["injected_failure"] = {
        "method": "SIGSTOP (process alive, port held, no reply)",
        "probes": probes,
        "seconds": failure_seconds,
        "process_alive": proc.poll() is None,
    }
    step("health_check_detects_the_hang", not any(p["ok"] for p in probes),
         {"probes": probes})
    step("failed_process_still_owns_the_port", port_open("127.0.0.1", args.port),
         "a dead process would free the port; a hung one does not")

    # --- 4. roll back ---------------------------------------------------
    proc.send_signal(signal.SIGCONT)
    report["stop_candidate"] = stop(proc)
    current.unlink()
    current.symlink_to(releases / "old")
    report["pointer_after_rollback"] = os.readlink(current)
    # The symlink may be relative or absolute; compare resolved targets, not text.
    step(
        "pointer_points_back_to_old",
        Path(os.readlink(current)).resolve() == (releases / "old").resolve(),
        {"readlink": os.readlink(current), "resolved": str(Path(os.readlink(current)).resolve())},
    )

    proc = start(env_python, args.launcher, current, args.project, args.port, boot,
                 args.log, releases / "old")
    ok, seconds = wait_health(args.port, args.health_deadline)
    back_boot = json.loads(boot.read_text()) if boot.exists() else {}
    back_asset = served_asset(args.port) if ok else {}
    report["after_rollback"] = {"healthy": ok, "seconds": seconds, "boot": back_boot,
                                "asset": back_asset}
    step("old_release_healthy_again", ok, {"seconds": seconds})
    step(
        "loaded_release_is_the_old_one",
        back_boot.get("current_resolved") == str((releases / "old").resolve())
        and Path(back_boot.get("lelab_file", "")).is_relative_to((releases / "old").resolve()),
        back_boot,
    )
    step(
        "old_bytes_served_again",
        bool(back_asset.get("assets"))
        and all(
            back_asset["assets"][path]["sha256"] == old_hashes.get(f"{args.project}/frontend/dist{path}")
            for path in back_asset.get("assets", {})
        ),
        {path: v["sha256"][:12] for path, v in back_asset.get("assets", {}).items()},
    )
    report["stop_after_rollback"] = stop(proc)
    time.sleep(1.0)
    step("port_free_at_end", not port_open("127.0.0.1", args.port))

    report["steps"] = STEPS
    report["passed"] = sum(1 for item in STEPS if item["ok"])
    report["failed"] = sum(1 for item in STEPS if not item["ok"])
    report["status"] = "passed" if report["failed"] == 0 else "failed"
    report["scope_note"] = (
        "Service-level recovery under a test current pointer. The archive-level "
        "tamper/restore rehearsal in acceptance.json is a different check: it proves "
        "the bytes can be verified, not that traffic can be moved back."
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": report["status"], "passed": report["passed"],
                      "failed": report["failed"], "report": str(args.report)}, indent=2))
    for item in STEPS:
        if not item["ok"]:
            print(f"FAIL {item['name']}: {json.dumps(item['detail'])[:300]}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
