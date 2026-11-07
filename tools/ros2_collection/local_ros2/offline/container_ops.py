#!/usr/bin/env python3
"""Container-side operations with identity verification, so the launcher never has to.

The shell wrapper used to embed these as nested quoted strings, which is both
unmaintainable and unsafe: a signal was sent to whatever pid happened to be in a pid
file.  Everything here checks the live process against the command line recorded when
it started, and refuses on a mismatch.  Nothing in this file kills by name or pattern.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

MS = 0.001


def read_pid_file(path: pathlib.Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def proc_cmdline(pid: int) -> str:
    try:
        return pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return ""


def proc_start_ticks(pid: int) -> str:
    """Field 22 of /proc/<pid>/stat: process start time, so a recycled pid is detected."""
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
        return stat.rpartition(") ")[2].split()[19]
    except (OSError, IndexError):
        return ""


def identity_matches(pid_file: pathlib.Path, expect: str) -> tuple[int | None, str]:
    """(pid, reason). pid is None when the recorded process is not ours to signal."""
    recorded = {}
    meta = pid_file.with_suffix(".identity.json")
    if meta.exists():
        recorded = json.loads(meta.read_text())
    pid = read_pid_file(pid_file)
    if pid is None:
        return None, "no_pid_recorded"
    live = proc_cmdline(pid)
    if not live:
        return None, "process_gone"
    if expect not in live:
        return None, f"cmdline_mismatch: {live[:120]}"
    if recorded.get("start_ticks") and recorded["start_ticks"] != proc_start_ticks(pid):
        return None, "pid_recycled"
    return pid, "ok"


def resolve_pid_file(candidates: list[pathlib.Path]) -> pathlib.Path:
    """The first pid file that exists. Names changed once during development, and a
    transition must not need a blind kill to work."""
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


LEGACY_NAMES = {"agent": ("agent",),
                "licence": ("licence_agent", "licence"),
                "board": ("sim_board_owner", "board"),
                "camera": ("sim_camera", "camera"),
                "control": ("car_control", "control")}


def stop_recorded(pid_file: pathlib.Path, expect: str, timeout_s: float,
                  state: pathlib.Path | None = None) -> dict:
    # The pid file moved directories once during development, so the lookup tries the
    # current location and the earlier ones. Every candidate is still checked against
    # /proc/<pid>/cmdline and the recorded start time before a signal goes out: this is
    # a filename fallback, never a blind kill.
    candidates = [pid_file]
    for name in LEGACY_NAMES.get(pid_file.stem, (pid_file.stem,)):
        for directory in [pid_file.parent, pid_file.parent.parent, state,
                          pid_file.parent / "state"]:
            if directory is not None:
                candidates.append(pathlib.Path(directory) / f"{name}.pid")
    pid_file = resolve_pid_file(candidates)
    pid, reason = identity_matches(pid_file, expect)
    if pid is None and reason != "process_gone":
        return {"stopped": False, "reason": reason}
    if pid is None:
        return {"stopped": True, "reason": "already_gone"}
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not proc_cmdline(pid):
            pid_file.unlink(missing_ok=True)
            pid_file.with_suffix(".identity.json").unlink(missing_ok=True)
            return {"stopped": True, "pid": pid, "reason": "terminated"}
        time.sleep(0.1)
    # A fixture that ignores SIGTERM would leave every caller reading this as "the
    # source stopped". Only escalate when the same verified process is still there:
    # identity_matches re-checks the command line and the recorded start time, so a
    # recycled pid is never killed.
    verified, reason = identity_matches(pid_file, expect)
    if verified != pid or reason != "ok":
        return {"stopped": False, "pid": pid, "reason": f"term_timeout_identity_{reason}"}
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + max(timeout_s, 5.0)
    while time.monotonic() < deadline:
        if not proc_cmdline(pid):
            pid_file.unlink(missing_ok=True)
            pid_file.with_suffix(".identity.json").unlink(missing_ok=True)
            return {"stopped": True, "pid": pid, "reason": "killed_after_term_timeout"}
        time.sleep(0.1)
    return {"stopped": False, "pid": pid, "reason": "kill_timeout"}


def start_detached(argv: list[str], logfile: pathlib.Path, pid_file: pathlib.Path,
                  module: str = "") -> dict:
    if module:
        argv = ["-m", module, *argv]
    """Start one owned process and record both its pid and its start identity."""
    logfile.parent.mkdir(parents=True, exist_ok=True)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    handle = logfile.open("a", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, *argv], stdout=handle, stderr=subprocess.STDOUT,
                            start_new_session=True, env=os.environ.copy())
    pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
    pid_file.with_suffix(".identity.json").write_text(json.dumps({
        "pid": proc.pid, "argv": argv, "start_ticks": proc_start_ticks(proc.pid),
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2) + "\n", encoding="utf-8")
    return {"pid": proc.pid, "log": str(logfile)}


def live_cmdlines(needle: str) -> list[dict]:
    """Scan /proc for processes whose command line contains `needle`.

    This is the check behind "the simulated issuer is not running": a pid file can be
    stale or written by another tool, but a live command line cannot lie about itself.
    """
    found = []
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        line = proc_cmdline(int(entry.name))
        if needle and needle in line:
            found.append({"pid": int(entry.name), "cmdline": line[:200]})
    return sorted(found, key=lambda item: item["pid"])


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("stop-board", "stop-control", "stop-licence", "stop-agent", "stop-camera"):
        p = sub.add_parser(name)
        p.add_argument("--state-dir", required=True)
        p.add_argument("--timeout-s", type=float, default=10.0)

    p = sub.add_parser("start-licence")
    p.add_argument("--state-dir", required=True)
    p.add_argument("--script", required=True)
    p.add_argument("--bundle", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--outputs", required=True, choices=["true", "false"])
    p.add_argument("--permits", default="arm,gripper")
    p.add_argument("--session-id", default="offline-session")
    p.add_argument("--session-epoch", default="1")

    for name in ("start-board", "start-control", "start-agent", "start-camera"):
        p = sub.add_parser(name)
        p.add_argument("--state-dir", required=True)
        # a module for the production-shaped package entry, a script for the fixtures
        p.add_argument("--script", default="")
        p.add_argument("--module", default="")
        p.add_argument("--arg", action="append", default=[])

    p = sub.add_parser("status")
    p.add_argument("--state-dir", required=True)

    p = sub.add_parser("assert-single-issuer")
    p.add_argument("--needle", default="licence_agent.py",
                   help="command-line fragment that marks the simulated issuer")
    p.add_argument("--json-only", action="store_true")

    args = parser.parse_args()
    if args.command == "assert-single-issuer":
        found = live_cmdlines(args.needle)
        self_pid = os.getpid()
        # The check itself is a python process whose argv names the needle; excluding it
        # keeps the assertion about the container, not about this invocation.
        found = [item for item in found if item["pid"] != self_pid]
        print(json.dumps({"needle": args.needle, "live": found, "ok": not found},
                         indent=2, sort_keys=True))
        return 0 if not found else 1

    state = pathlib.Path(args.state_dir)
    log_dir = state.parent / "logs"
    expect = {"stop-board": "sim_board_owner.py", "stop-control": "car_control_process",
              "stop-licence": "licence_agent.py",
              "stop-agent": "lelab_mobile_agent.agent",
              "stop-camera": "sim_camera.py"}.get(args.command)
    if expect:
        result = stop_recorded(state.parent / f"{args.command.replace('stop-', '')}.pid",
                               expect, args.timeout_s, state=state)
        print(json.dumps(result))
        return 0 if result["stopped"] else 1

    if args.command == "status":
        out = {}
        for kind in ("board", "control", "licence", "agent", "camera"):
            pid_file = state.parent / f"{kind}.pid"
            pid = read_pid_file(pid_file)
            out[kind] = {"pid": pid, "alive": bool(pid and proc_cmdline(pid)),
                         "cmdline": proc_cmdline(pid)[:160] if pid else ""}
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0

    if args.command == "start-licence":
        argv = [args.script, "--socket", str(state / "licence.sock"),
                "--session-id", args.session_id, "--session-epoch", args.session_epoch,
                "--bundle", args.bundle, "--token", args.token, "--permits", args.permits,
                "--outputs", args.outputs,
                "--status-path", str(state / "licence_agent_status.json"),
                "--generation-file", str(state / "licence_generation.txt")]
        print(json.dumps(start_detached(argv, log_dir / "licence_agent.log",
                                        state.parent / "licence.pid")))
        return 0

    if args.command == "start-board" and not args.script:
        parser.error("start-board needs --script")
    if args.command in ("start-control", "start-agent") and not args.module:
        parser.error(f"{args.command} needs --module")
    if args.command == "start-camera" and not args.script:
        parser.error("start-camera needs --script")

    kind = {"start-board": "board", "start-control": "control",
            "start-agent": "agent", "start-camera": "camera"}[args.command]
    launch = [args.script] if args.command in ("start-board", "start-camera") else []
    print(json.dumps(start_detached(
        launch + list(args.arg),
        log_dir / f"{kind}.log",
        state.parent / f"{kind}.pid",
        module=args.module)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
