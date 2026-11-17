"""Production-bundle acceptance harness (R2 → closure).

Serves the built frontend/dist and stubs the backend routes the SPA touches,
logging every request. Training/inference submissions are validated against the
REAL LeLab request schemas and then accepted with a fake job — no runner, no
device access, no subprocess. Requests and responses go to ui_requests.jsonl
next to this file so the browser-visible behaviour can be reconciled with the
wire contract.

Notes
-----
* SPA routes are real paths (`/training`, `/training/:jobId`), so a hard
  navigation must receive index.html. Browsers mark document navigations with
  `Sec-Fetch-Mode: navigate`; XHR/fetch calls do not. That header — not a route
  allow-list — decides the fallback, so a missing API route still returns the
  JSON stub instead of silently rendering the app shell.
* `/__ui_log` exposes the captured request log to the Playwright assertions
  without shelling out. It is harness-only and never part of the app.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Request


def _find_worktree(start: Path) -> Path:
    """Locate the worktree by its release tree, independent of nesting depth."""
    for parent in [start, *start.parents]:
        if (parent / "releases/sanitized-20261024-01/app").is_dir():
            return parent
    raise SystemExit("cannot locate worktree containing releases/sanitized-20261024-01/app")


WORKTREE = _find_worktree(Path(__file__).resolve().parent)
APP = WORKTREE / "releases/sanitized-20261024-01/app"
DIST = APP / "frontend/dist"
LOG = Path(__file__).resolve().parent / "ui_requests.jsonl"

sys.path.insert(0, str(APP))

from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from lelab.server import StartTrainingBody  # noqa: E402

app = FastAPI()
LOG.write_text("", encoding="utf-8")

FAKE_JOB_ID = "fake-rmr-001"
FAKE_ROBOT = {
    "name": "so101-red",
    "leader_port": "/dev/lelab-fake-leader",
    "follower_port": "/dev/lelab-fake-follower",
    "leader_config": "so101_leader",
    "follower_config": "so101_follower",
    "cameras": [{"name": "front", "index": 0, "width": 640, "height": 480, "fps": 30}],
    "is_clean": True,
    "configuration": {"complete": True, "issues": [], "warnings": [], "checks": {}},
}


def _log(route: str, body=None, status: int = 200, note: str = "") -> None:
    entry = {"ts": time.time(), "route": route, "status": status, "note": note}
    if body is not None:
        entry["body"] = body
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _fake_job() -> dict:
    return {
        "id": FAKE_JOB_ID,
        "name": "fastwam-so101-6dim",
        "state": "done",
        "config": {
            "dataset_repo_id": "so101/pick_cube_local",
            "policy_type": "fastwam",
            "policy_action_dim": 6,
            "policy_proprio_dim": 6,
            "steps": 1000,
            "batch_size": 1,
            "wandb_enable": False,
            "target": {"runner": "local"},
        },
        "output_dir": "/tmp/rmr-fake-out",
        "started_at": 1794800000.0,
        "ended_at": 1794803600.0,
        "exit_code": 0,
        "error_message": None,
        "metrics": {
            "current_step": 1000,
            "total_steps": 1000,
            "loss": 0.42,
            "lr": 1e-4,
            "epoch": 1.0,
            "grad_norm": 0.9,
            "samples_per_sec": 12.0,
            "eta_seconds": 0,
        },
        "runner": "local",
        "hf_job_id": None,
        "hf_flavor": None,
        "hf_repo_id": None,
        "hf_job_url": None,
        "wandb_run_url": None,
        "checkpoint_count": 1,
        "attempts": [],
        "pause_capable": False,
        "pause_status": None,
        "pause_error": None,
        "paused_at": None,
        "pause_checkpoint": None,
        "pause_step": None,
        "resume_error": None,
    }


@app.get("/")
def index():
    return FileResponse(DIST / "index.html")


@app.get("/assets/{name}")
def asset(name: str):
    return FileResponse(DIST / "assets" / name)


@app.get("/favicon.ico")
def favicon():
    return FileResponse(DIST / "favicon.ico")


@app.get("/system/status")
def system_status():
    _log("/system/status")
    return {"ok": True, "fake": True, "gpus": [], "disk": {}, "devices": []}


@app.get("/hf-auth-status")
def hf_auth():
    _log("/hf-auth-status")
    return {"logged_in": False, "fake": True}


@app.get("/inference-status")
def inference_status():
    _log("/inference-status")
    return {"active": False, "fake": True, "phase": "idle"}


@app.get("/inference/remote/server-status")
def remote_server_status():
    _log("/inference/remote/server-status")
    return {"active": False, "fake": True}


@app.get("/jobs")
def jobs_list():
    """The SPA expects {"jobs": [...]}, not a bare array."""
    _log("/jobs")
    return {"jobs": [_fake_job()]}


@app.get("/jobs/hub")
def jobs_hub():
    _log("/jobs/hub")
    return {"authenticated": False, "jobs": [], "models": []}


@app.get("/jobs/orphans")
def jobs_orphans():
    _log("/jobs/orphans")
    return {"jobs": []}


@app.get("/jobs/runners/hardware")
def runner_hardware():
    _log("/jobs/runners/hardware")
    return {"authenticated": False, "username": None, "flavors": []}


@app.get("/jobs/{job_id}")
def job_detail(job_id: str):
    _log(f"/jobs/{job_id}")
    job = _fake_job()
    job["id"] = job_id
    return job


@app.get("/jobs/{job_id}/logs")
def job_logs(job_id: str):
    _log(f"/jobs/{job_id}/logs")
    return {"logs": [{"ts": 1794800000.0, "level": "info", "message": "fake job finished"}]}


@app.get("/jobs/{job_id}/monitor")
def job_monitor(job_id: str):
    _log(f"/jobs/{job_id}/monitor")
    return {
        "job_id": job_id,
        "run_id": "fake-run-001",
        "state": "done",
        "attempts": [],
        "current_attempt_id": None,
        "capabilities": {
            "supported": False,
            "reason": "fake backend",
            "save_steps": None,
            "resume": False,
        },
        "requested": {},
        "resolved_config": None,
        "latest": {},
        "validation": {
            "configured": False,
            "eval_steps": 0,
            "eval_episodes": None,
            "latest": None,
        },
        "pause": {
            "status": None,
            "checkpoint": None,
            "step": None,
            "paused_at": None,
            "error": None,
            "resume_error": None,
        },
        "metric_event_count": 0,
    }


@app.get("/jobs/{job_id}/checkpoints")
def job_checkpoints(job_id: str):
    _log(f"/jobs/{job_id}/checkpoints")
    return {"checkpoints": [{"step": 1000, "source": "local", "ref": "/tmp/rmr-fake-out/checkpoints/1000"}]}


@app.get("/jobs/{job_id}/checkpoints/{step}/policy-config")
def checkpoint_policy_config(job_id: str, step: int):
    _log(f"/jobs/{job_id}/checkpoints/{step}/policy-config")
    return {
        "policy_type": "fastwam",
        "image_features": {},
        "requires_task": True,
    }


@app.get("/system/training-extra")
def training_extra():
    _log("/system/training-extra")
    return {"available": True, "install_hint": ""}


@app.get("/system/policy-extra/{policy_type}")
def policy_extra(policy_type: str):
    _log(f"/system/policy-extra/{policy_type}")
    return {
        "needs_extra": False,
        "available": True,
        "package": "",
        "install_target": "",
        "install_hint": "",
    }


@app.get("/system/train-server")
def train_server():
    _log("/system/train-server")
    return {
        "configured": True,
        "endpoint": "g128:8000",
        "host": "g128",
        "user": "lfy",
        "gpus": [],
        "reachable": True,
    }


@app.get("/datasets")
def datasets():
    _log("/datasets")
    return [
        {
            "repo_id": "so101/pick_cube_local",
            "source": "local",
            "last_modified": "2026-11-15T10:00:00Z",
            "private": False,
            "sealed": True,
            "seal_state": "sealed",
            "finalized": True,
            "total_episodes": 20,
            "total_frames": 3000,
        }
    ]


@app.get("/robots")
def robots():
    """`useRobots` reads data.robots, not a bare array."""
    _log("/robots")
    return {"robots": [FAKE_ROBOT]}


@app.get("/available-cameras")
def available_cameras():
    _log("/available-cameras")
    return {"cameras": [{"index": 0, "name": "fake-usb-cam", "available": True}]}


@app.get("/hardware/lease")
def hardware_lease():
    _log("/hardware/lease")
    return {
        "state": "idle", "task": None, "owner": None, "token": None,
        "expires_in": None, "ttl_seconds": 30,
        "last_release": {"task": "fake", "owner": "fake", "reason": "fake", "at": None},
    }


@app.get("/teleoperation/test/readiness")
def teleop_readiness():
    _log("/teleoperation/test/readiness")
    return {"ready": False, "reasons": ["fake backend"]}


@app.get("/system/update-check")
def update_check():
    _log("/system/update-check")
    return {"update_available": False}


@app.get("/training/status")
def training_status():
    _log("/training/status")
    return {"active": False, "fake": True, "jobs": []}


@app.get("/training-extra/status")
def training_extra_status():
    _log("/training-extra/status")
    return {"active": False, "fake": True}


@app.get("/__ui_log")
def ui_log(limit: int = 200):
    lines = [line for line in LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {"entries": [json.loads(line) for line in lines[-limit:]]}


@app.post("/jobs/training")
def jobs_training(payload: dict):
    """Validate with the REAL StartTrainingBody path; never spawn a runner."""
    try:
        body = StartTrainingBody.from_legacy(payload)
    except ValidationError as exc:
        _log("/jobs/training", payload, status=422, note="validation_failed")
        return JSONResponse(status_code=422, content={"detail": json.loads(exc.json())})
    normalized = json.loads(body.config.model_dump_json())
    target = json.loads(body.target.model_dump_json()) if body.target else None
    # Mirror the real gate: job start runs build_training_command, whose
    # ValueError becomes HTTP 400 with the actionable message.
    from lelab.train import build_training_command
    try:
        build_training_command(body.config, "/tmp/rmr-fake-out")
    except ValueError as exc:
        _log("/jobs/training", {"config": normalized, "target": target},
             status=400, note=f"start_refused: {exc}")
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    _log("/jobs/training", {"config": normalized, "target": target},
         status=201, note="accepted_fake_runner")
    return JSONResponse(status_code=201, content={
        "id": FAKE_JOB_ID, "job_id": FAKE_JOB_ID, "status": "queued", "fake": True,
        "name": "fastwam-so101-6dim",
        "config": normalized, "target": target,
    })


@app.post("/start-inference")
def start_inference(payload: dict):
    """Validate the wire contract with the REAL InferenceRequest schema.

    Session/lease orchestration (SSH, server lifecycle) stays out of scope here;
    it is covered by app tests against handle_start_remote_inference.
    """
    from lelab.rollout import InferenceRequest
    try:
        parsed = InferenceRequest(**payload)
    except ValidationError as exc:
        _log("/start-inference", payload, status=422, note="validation_failed")
        return JSONResponse(status_code=422, content={"detail": json.loads(exc.json())})
    normalized = json.loads(parsed.model_dump_json())
    _log("/start-inference", normalized, status=200, note="accepted_fake")
    return {"ok": True, "fake": True, "request": normalized}


@app.post("/{missing_path:path}")
def catch_all_post(missing_path: str, payload: dict = None):
    _log(f"POST/{missing_path}", payload, status=200, note="catch_all_stub")
    return {"ok": True, "fake": True, "stub": missing_path}


@app.get("/{missing_path:path}")
def catch_all_get(missing_path: str, request: Request):
    if "." in Path(missing_path).name:  # static asset fallback
        target = DIST / missing_path
        if target.is_file():
            return FileResponse(target)
    if request.headers.get("sec-fetch-mode") == "navigate":
        # Real SPA route (react-router path) hit by a document navigation.
        _log(f"GET/{missing_path}", status=200, note="spa_index")
        return FileResponse(DIST / "index.html")
    _log(f"GET/{missing_path}", status=200, note="catch_all_stub")
    return {"ok": True, "fake": True, "stub": missing_path}
