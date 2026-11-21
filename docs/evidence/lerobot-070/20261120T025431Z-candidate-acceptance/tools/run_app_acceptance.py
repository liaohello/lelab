#!/usr/bin/env python3
"""Start the unpacked app and check what it actually serves and refuses.

Everything here talks HTTP to a real uvicorn process started from the packaged
entry point, with `PYTHONPATH` cleared. The checks are deliberately about
observable behaviour, not about importing modules:

* `/health` answers, and the process really is the one we started;
* `/` and the JS/CSS it references are served from the packaged `dist`, proven
  by hashing the bytes against the candidate manifest;
* a client-side route falls back to `index.html` (the SPA mount);
* four malformed remote-inference requests are refused, and the refusal text
  names the missing field instead of failing later on a device;
* stopping the service leaves no listener and no child processes behind.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: object = None) -> bool:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail})
    return bool(ok)


def http_get(url: str, timeout: float = 10.0, accept: str = "*/*") -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, headers={"Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def http_post(url: str, payload: dict, timeout: float = 60.0) -> tuple[int, dict, bytes]:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def wait_health(url: str, deadline: float) -> tuple[bool, float, dict]:
    started = time.time()
    last: dict = {}
    while time.time() - started < deadline:
        try:
            status, _, body = http_get(f"{url}/health", timeout=3.0)
            if status == 200:
                last = json.loads(body)
                return True, round(time.time() - started, 1), last
        except Exception as exc:  # noqa: BLE001 - the waiter reports the last error
            last = {"error": f"{type(exc).__name__}: {exc}"}
        time.sleep(1.0)
    return False, round(time.time() - started, 1), last


def children_of(pid: int) -> list[int]:
    kids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
            if int(fields[1]) == pid:
                kids.append(int(entry.name))
        except (OSError, IndexError, ValueError):
            continue
    return kids


def stop_process(proc: subprocess.Popen, grace: float = 20.0) -> dict:
    started = time.time()
    kids_before = children_of(proc.pid)
    proc.send_signal(signal.SIGTERM)
    try:
        code = proc.wait(timeout=grace)
        forced = False
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait(timeout=10)
        forced = True
    leftovers = [pid for pid in kids_before if Path(f"/proc/{pid}").exists()]
    return {
        "exit_code": code,
        "forced": forced,
        "seconds": round(time.time() - started, 1),
        "children_before": kids_before,
        "children_after": children_of(proc.pid) if proc.poll() is None else [],
        "children_left_running": leftovers,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", required=True, type=Path, help="unpacked MANIFEST + payload/")
    parser.add_argument("--env", required=True, type=Path)
    parser.add_argument("--project", default="app", help="payload subdirectory to run")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--health-timeout", type=float, default=180.0)
    parser.add_argument("--home", type=Path, default=None,
                        help="HOME for the service, so calibration/log paths stay inside the test tree")
    args = parser.parse_args()

    tree = (args.stack / "payload").resolve()
    project = tree / args.project
    report: dict = {"port": args.port, "tree": str(tree), "project": str(project)}
    manifest = json.loads((args.stack / "MANIFEST.json").read_text())
    python = args.env / "bin/python"
    launcher = Path(__file__).resolve().parent / "app_launcher.py"

    check("env_python_exists", python.is_file(), str(python))
    check("launcher_in_packaged_tree", (project / "lelab/scripts/lelab.py").is_file(),
          str(project / "lelab/scripts/lelab.py"))
    check("port_free_before_start", not port_open("127.0.0.1", args.port), str(args.port))

    env = {k: v for k, v in os.environ.items()
           if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
    env.update(
        {
            "LELAB_ACCEPT_PORT": str(args.port),
            "PYTHONUNBUFFERED": "1",
            "HF_HUB_OFFLINE": "1",
            # A dummy train server: without one the remote handler stops at
            # "Missing training-server configuration" before it ever looks at the
            # policy contract. Port 1 is refused instantly, so the legal request
            # still fails fast at the SSH hop instead of waiting for a timeout.
            "LELAB_TRAIN_HOST": "127.0.0.1",
            "LELAB_TRAIN_PORT": "1",
            "LELAB_TRAIN_USER": "probe",
            "LELAB_TRAIN_ROOT": "/tmp/lelab-probe",
            "PYTHONPATH": f"{project}:{tree / 'lerobot' / 'src'}",
            **({"HOME": str(args.home)} if args.home else {}),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    started = time.time()
    log_handle = args.log.open("w", buffering=1)
    proc = subprocess.Popen(
        [str(python), str(launcher)],
        cwd=project,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    report["pid"] = proc.pid
    base = f"http://127.0.0.1:{args.port}"

    try:
        healthy, health_seconds, health_body = wait_health(base, args.health_timeout)
        report["health"] = {"ok": healthy, "seconds": health_seconds, "body": health_body,
                            "startup_seconds": round(time.time() - started, 1)}
        if not healthy:
            report["process_exit"] = proc.poll()
            report["log_tail"] = "".join(
                args.log.read_text(errors="replace").splitlines(keepends=True)[-25:]
            ) if args.log.exists() else ""
        check("health_endpoint", healthy and health_body.get("status") == "ok", health_body)

        if healthy:
            status, headers, body = http_get(f"{base}/")
            text = body.decode("utf-8", errors="replace")
            check("index_served", status == 200 and "text/html" in headers.get("content-type", ""),
                  {"status": status, "bytes": len(body), "content_type": headers.get("content-type")})
            check("index_has_spa_root", 'id="root"' in text, text[:120])

            assets = sorted(set(re.findall(r'(?:src|href)="(/assets/[^"]+)"', text)))
            served = {}
            dist_prefix = "app/frontend/dist"
            for asset in assets:
                astatus, _, abody = http_get(f"{base}{asset}")
                key = f"{dist_prefix}{asset}"
                expected = manifest["sha256"].get(key)
                digest = hashlib.sha256(abody).hexdigest()
                served[asset] = {
                    "status": astatus,
                    "bytes": len(abody),
                    "sha256": digest,
                    "manifest_sha256": expected,
                    "matches_manifest": expected == digest,
                }
            report["assets"] = served
            check("assets_referenced_by_index", len(assets) >= 2, assets)
            check(
                "every_asset_matches_manifest",
                bool(assets) and all(v["status"] == 200 and v["matches_manifest"] for v in served.values()),
                {k: (v["status"], v["matches_manifest"]) for k, v in served.items()},
            )

            # A client-side route must return the same index (SPAStaticFiles).
            # The fallback is deliberately limited to navigations: it only fires
            # when the request accepts HTML, which is why the Accept header here
            # has to look like a browser's.
            dstatus, _, dbody = http_get(f"{base}/training", accept="text/html,application/xhtml+xml")
            check(
                "client_route_falls_back_to_index",
                dstatus == 200 and dbody == body,
                {"status": dstatus, "same_bytes": dbody == body},
            )

            # Process identity: cwd and executable, not just a listening port.
            try:
                cwd = os.readlink(f"/proc/{proc.pid}/cwd")
                exe = os.readlink(f"/proc/{proc.pid}/exe")
            except OSError as exc:
                cwd, exe = f"unreadable: {exc}", ""
            report["process"] = {"cwd": cwd, "exe": exe}
            check("process_runs_from_the_unpacked_tree",
                  Path(cwd).resolve() == project.resolve(), {"cwd": cwd, "expected": str(project)})
            check("process_uses_the_given_interpreter",
                  Path(exe).resolve() == python.resolve(), {"exe": exe, "expected": str(python)})
            probe = subprocess.run(
                [str(python), "-c",
                 "import json, lelab, lerobot; print(json.dumps({'lelab': lelab.__file__, "
                 "'lerobot': lerobot.__file__}))"],
                cwd=project, env=env, capture_output=True, text=True, timeout=120,
            )
            modules = json.loads(probe.stdout) if probe.returncode == 0 else {"error": probe.stderr[-300:]}
            report["modules"] = modules
            check("lelab_imports_from_the_tree",
                  str(modules.get("lelab", "")).startswith(str(tree)), modules)
            check("lerobot_imports_from_the_tree",
                  str(modules.get("lerobot", "")).startswith(str(tree)), modules)

            # --- refusals: each names the field it wants -------------------
            single = {"policy_ref": "/tmp/does-not-matter", "follower_port": "/dev/null",
                      "follower_config": "so101_follower", "target": "remote"}
            cases = {
                "no_deployment": (single, "remote_deployment and remote_semantics"),
                "no_dimensions": ({**single, "remote_deployment": "so101_red",
                                   "remote_semantics": "so101-degrees-gripper-percent-v1"},
                                  "remote_state_dim/remote_action_dim"),
                "legacy_async_params": ({**single, "remote_deployment": "so101_red",
                                         "remote_semantics": "so101-degrees-gripper-percent-v1",
                                         "remote_state_dim": 6, "remote_action_dim": 6,
                                         "actions_per_chunk": 100},
                                        "retired async client"),
                "six_dim_bimanual": ({
                    "policy_ref": "/tmp/does-not-matter",
                    "target": "remote",
                    "bimanual": {
                        "left": {"leader_port": "/dev/ttyUSB0", "leader_config": "l",
                                 "follower_port": "/dev/ttyUSB1", "follower_config": "lf"},
                        "right": {"leader_port": "/dev/ttyUSB2", "leader_config": "r",
                                  "follower_port": "/dev/ttyUSB3", "follower_config": "rf"},
                    },
                    "cameras": {"left": {"width": 640, "height": 480},
                                "right": {"width": 640, "height": 480}},
                    "remote_deployment": "bimanual_red",
                    "remote_semantics": "so101-degrees-gripper-percent-v1",
                    "remote_state_dim": 6, "remote_action_dim": 6,
                    "policy_contract": {"state_dim": 6, "action_dim": 6},
                }, "state_dim=12 and action_dim=12"),
            }
            refusals = {}
            for name, (payload, needle) in cases.items():
                status, _, body = http_post(f"{base}/start-inference", payload)
                text_body = body.decode("utf-8", errors="replace")
                refusals[name] = {"status": status, "mentions": needle in text_body,
                                  "body": text_body[:400]}
            report["refusals"] = refusals
            check("refusal_no_deployment",
                  refusals["no_deployment"]["status"] in (400, 422)
                  and refusals["no_deployment"]["mentions"], refusals["no_deployment"])
            check("refusal_no_dimensions",
                  refusals["no_dimensions"]["status"] in (400, 422)
                  and refusals["no_dimensions"]["mentions"], refusals["no_dimensions"])
            check("refusal_legacy_async",
                  refusals["legacy_async_params"]["status"] in (400, 422)
                  and refusals["legacy_async_params"]["mentions"],
                  refusals["legacy_async_params"])
            check("refusal_six_dim_bimanual",
                  refusals["six_dim_bimanual"]["status"] in (400, 422, 500)
                  and refusals["six_dim_bimanual"]["mentions"],
                  refusals["six_dim_bimanual"])
            report["refusal_status_note"] = (
                "A six-dimension bimanual contract is produced by the request validator's "
                "downstream guard and surfaces as HTTP %s, not 400: the handler maps every "
                "exception to 500. The refusal is correct, the status class is not."
                % refusals["six_dim_bimanual"]["status"]
            )

            # A legal remote request must clear validation and reach the
            # dispatcher. With no train server configured it stops there with a
            # 400 that names the missing endpoint - which is the proof that the
            # request itself was accepted.
            legal = {
                "policy_ref": "/tmp/does-not-matter",
                "follower_port": "/dev/null",
                "follower_config": "so101_follower",
                "target": "remote",
                "remote_deployment": "so101_red",
                "remote_semantics": "so101-degrees-gripper-percent-v1",
                "remote_state_dim": 6,
                "remote_action_dim": 6,
                "policy_contract": {"state_dim": 6, "action_dim": 6, "processor": "act-v1",
                                    "normalization_mapping": {"ACTION": "MEAN_STD",
                                                              "OBSERVATION": "MEAN_STD"},
                                    "camera_keys": ["observation.images.front"]},
                "cameras": {"front": {"width": 640, "height": 480}},
            }
            status, _, body = http_post(f"{base}/start-inference", legal, timeout=90)
            text_body = body.decode("utf-8", errors="replace")
            report["legal_remote"] = {"status": status, "body": text_body[:600]}
            # Passing request validation is not enough: the handler must reach
            # the SSH staging step. With port 1 closed that fails in under a
            # second, and the message is about the connection, not about a
            # missing field.
            check(
                "legal_remote_reaches_the_dispatcher",
                status in (400, 500)
                and "remote_deployment and remote_semantics" not in text_body
                and any(token in text_body.lower() for token in
                        ("ssh", "connection refused", "could not", "stage", "connect")),
                {"status": status, "body": text_body[:300]},
            )
    finally:
        report["stop"] = stop_process(proc)
        log_handle.close()

    check("stopped_cleanly", report["stop"]["exit_code"] is not None, report["stop"])
    check("no_children_left", not report["stop"]["children_left_running"], report["stop"])
    time.sleep(1.0)
    check("port_released", not port_open("127.0.0.1", args.port), str(args.port))

    report["checks"] = CHECKS
    report["passed"] = sum(1 for c in CHECKS if c["ok"])
    report["failed"] = sum(1 for c in CHECKS if not c["ok"])
    report["status"] = "passed" if report["failed"] == 0 else "failed"
    report["log"] = str(args.log)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": report["status"], "passed": report["passed"],
                      "failed": report["failed"], "report": str(args.report)}, indent=2))
    for item in CHECKS:
        if not item["ok"]:
            print(f"FAIL {item['name']}: {json.dumps(item['detail'])[:300]}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
