"""Production-bundle acceptance harness (R2).

Serves the built frontend/dist and stubs the backend routes the SPA touches,
logging every request. Training/inference submissions are validated against the
REAL LeLab request schemas and then accepted with a fake job — no runner, no
device access, no subprocess. Requests and responses go to ui_requests.jsonl
next to this file so the browser-visible behaviour can be reconciled with the
wire contract.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[4]
APP = WORKTREE / "releases/sanitized-20261024-01/app"
DIST = APP / "frontend/dist"
LOG = Path(__file__).resolve().parent / "ui_requests.jsonl"

sys.path.insert(0, str(APP))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from lelab.server import StartTrainingBody  # noqa: E402
from lelab.train import TrainingRequest  # noqa: E402

app = FastAPI()
LOG.write_text("", encoding="utf-8")


def _log(route: str, body=None, status: int = 200, note: str = "") -> None:
    entry = {"ts": time.time(), "route": route, "status": status, "note": note}
    if body is not None:
        entry["body"] = body
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


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


@app.get("/training/status")
def training_status():
    _log("/training/status")
    return {"active": False, "fake": True, "jobs": []}


@app.get("/training-extra/status")
def training_extra_status():
    _log("/training-extra/status")
    return {"active": False, "fake": True}


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
        "id": "fake-rmr-001", "job_id": "fake-rmr-001", "status": "queued", "fake": True,
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
def catch_all_get(missing_path: str):
    if "." in Path(missing_path).name:  # static asset fallback
        target = DIST / missing_path
        if target.is_file():
            return FileResponse(target)
    _log(f"GET/{missing_path}", status=200, note="catch_all_stub")
    return {"ok": True, "fake": True, "stub": missing_path}
