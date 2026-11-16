"""Operator-facing capture orchestration over the existing hybrid control stack.

This is a thin layer above the production facade and ``HybridRecordingController``.
It owns no lease, no motion path and no recorder of its own: every step calls the
same primitives the debug pages use, so TTL, exclusivity, trusted feedback and
emergency stop keep their existing enforcement.

Identity model (four different things, never conflated):

* ``task_id`` -- one stable collection task (dataset), survives control sessions;
* ``session_id`` / ``session_epoch`` -- the control lease generation (facade);
* ``attempt_id`` -- one episode attempt, used verbatim as the car ``episode_id``;
* ``episode_index`` -- assigned only by the LeRobot export, reported afterwards.

The server is the recording authority.  ``recording_active`` is true only when an
attempt is confirmed recording *and* its control identity still matches the live
facade session; a stale episode id can never render REC after a session change.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .hybrid_recording import RecordingError

CAPTURE_ACTIONS = (
    "acquire", "open_dataset", "start_episode", "mark_grasp", "end_episode",
    "review", "next_episode", "resume_follow", "close_dataset", "recover",
    "retry_pipeline",
)

OPEN_ATTEMPT_STATES = ("starting", "recording", "paused")
DECIDED_STATES = ("saved", "discarded", "rerecorded")


class CaptureError(RuntimeError):
    def __init__(self, reason: str, detail: str = "", *, status_code: int = 409):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason
        self.status_code = status_code

    def as_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "detail": self.detail,
                "status_code": self.status_code}


def _reason_of(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, (CaptureError, RecordingError)):
        return exc.as_dict()
    if isinstance(exc, RuntimeError):
        return {"reason": str(exc) or type(exc).__name__,
                "detail": str(exc), "status_code": 409}
    detail = getattr(exc, "detail", None)
    status = getattr(exc, "status", None)
    if isinstance(detail, str) and isinstance(status, int):
        return {"reason": detail, "detail": detail, "status_code": status}
    return {"reason": type(exc).__name__, "detail": str(exc), "status_code": 500}


class CaptureOrchestrator:
    def __init__(self, facade: Any, recording: Any, *,
                 collection_root: str | Path,
                 clock_ns: Callable[[], int] = time.monotonic_ns,
                 wall_ns: Callable[[], int] = time.time_ns,
                 sync_fn: Callable[[str], dict] | None = None,
                 export_spawn: Callable[..., dict] | None = None,
                 export_status: Callable[[str], dict] | None = None,
                 dataset_meta: Callable[[str], dict] | None = None,
                 background: bool = True):
        self.facade = facade
        self.recording = recording
        self.clock_ns = clock_ns
        self.wall_ns = wall_ns
        self.sync_fn = sync_fn
        self.export_spawn = export_spawn
        self.export_status = export_status
        self.dataset_meta = dataset_meta
        self.background = background
        self.lock = threading.RLock()
        self._action_lock = threading.Lock()
        self.tasks_dir = Path(collection_root) / "upper" / "capture"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, dict[str, Any]] = {}
        self._active_task_id: str | None = None
        self._requests: dict[str, dict[str, Any]] = {}
        self._pipeline_threads: dict[str, threading.Thread] = {}
        self._load_tasks()

    # -- persistence ---------------------------------------------------------
    def _task_path(self, task_id: str) -> Path:
        return self.tasks_dir / task_id / "task.json"

    def _save_task(self, task: dict[str, Any]) -> None:
        directory = self._task_path(task["task_id"]).parent
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".task-{uuid.uuid4().hex[:8]}.tmp"
        tmp.write_text(json.dumps(task, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._task_path(task["task_id"]))

    def _journal(self, task: dict[str, Any], kind: str, **payload: Any) -> None:
        entry = {"kind": kind, "wall_ns": self.wall_ns(),
                 "task_id": task["task_id"], **payload}
        directory = self._task_path(task["task_id"]).parent
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / "journal.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _load_tasks(self) -> None:
        if not self.tasks_dir.is_dir():
            return
        for path in sorted(self.tasks_dir.iterdir()):
            task_file = path / "task.json"
            if not task_file.is_file():
                continue
            try:
                task = json.loads(task_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - a corrupt task stays on disk, never loaded as truth
                continue
            if not isinstance(task, dict) or not task.get("task_id"):
                continue
            if task.get("stage") == "pipeline":
                task["stage"] = "failed"
                task.setdefault("pipeline", {})["stage"] = "failed"
                task["pipeline"]["error"] = "service_restarted_during_pipeline"
            self._tasks[task["task_id"]] = task
            if task.get("stage") in ("open", "interrupted") and self._active_task_id is None:
                self._active_task_id = task["task_id"]

    # -- small helpers -------------------------------------------------------
    def _active_task(self) -> dict[str, Any] | None:
        if self._active_task_id is None:
            return None
        return self._tasks.get(self._active_task_id)

    def _open_attempt(self, task: dict[str, Any] | None) -> dict[str, Any] | None:
        if not task:
            return None
        for attempt in reversed(task.get("attempts", [])):
            if attempt.get("state") in OPEN_ATTEMPT_STATES:
                return attempt
        return None

    def _current_attempt(self, task: dict[str, Any] | None) -> dict[str, Any] | None:
        """The attempt the operator is looking at: open, in review, or interrupted."""
        if not task:
            return None
        attempt = self._open_attempt(task)
        if attempt is not None:
            return attempt
        for candidate in reversed(task.get("attempts", [])):
            if candidate.get("state") in ("review", "interrupted"):
                return candidate
        return None

    def _identity(self, status: dict[str, Any]) -> tuple[str | None, int | None]:
        return status.get("session_id"), status.get("session_epoch")

    def _facade_status(self) -> dict[str, Any]:
        status = self.facade.status()
        if not isinstance(status, dict):
            raise CaptureError("facade_status_invalid", status_code=503)
        return status

    def _control_ready(self, status: dict[str, Any]) -> None:
        if status.get("state") != "ready" or not status.get("session_id"):
            raise CaptureError("control_not_ready",
                               f"state={status.get('state')!r}: "
                               f"{status.get('last_error') or 'acquire control first'}")

    def _check_camera(self, status: dict[str, Any]) -> None:
        video = status.get("video") if isinstance(status.get("video"), dict) else {}
        age = video.get("age_s")
        if (video.get("available") is not True or video.get("stale") is True
                or not isinstance(age, (int, float)) or isinstance(age, bool) or age > 1.0):
            raise CaptureError(
                "camera_frame_stale",
                f"camera available={video.get('available')} stale={video.get('stale')} "
                f"age_s={age!r}; recording needs a fresh wrist frame")

    def _check_feedback(self, status: dict[str, Any]) -> None:
        feedback = status.get("feedback") if isinstance(status.get("feedback"), dict) else {}
        if feedback.get("valid") is not True or feedback.get("trusted") is not True:
            raise CaptureError("feedback_untrusted",
                               ",".join(feedback.get("trusted_reasons") or [])
                               or "six-axis measured feedback is not trusted")

    # -- reconciliation: the server decides what is really recording ---------
    def _reconcile(self, status: dict[str, Any]) -> None:
        """Called under self.lock with a fresh facade status (read outside the lock)."""
        task = self._active_task()
        if task is None or task.get("stage") not in ("open", "interrupted"):
            return
        attempt = self._open_attempt(task)
        rec = self.recording.status() if self.recording is not None else {}
        current_id, current_epoch = self._identity(status)
        if attempt is not None:
            bound = (attempt.get("session_id"), attempt.get("session_epoch"))
            identity_lost = (
                current_id is None
                or bound != (current_id, current_epoch)
            )
            if identity_lost:
                attempt["state"] = "interrupted"
                attempt["interrupt_reason"] = (
                    "service_restarted" if current_id is None and status.get("state") == "disconnected"
                    else "control_session_changed")
                attempt["ended_upper_mono_ns"] = self.clock_ns()
                attempt["car_episode_open"] = rec.get("episode_id") == attempt["episode_id"]
                task["stage"] = "interrupted"
                self._journal(task, "attempt_interrupted", attempt_id=attempt["attempt_id"],
                              reason=attempt["interrupt_reason"])
                self._save_task(task)
                return
            if rec.get("status") == "invalid":
                attempt["state"] = "interrupted"
                attempt["interrupt_reason"] = rec.get("last_error") or "recording_invalid"
                attempt["ended_upper_mono_ns"] = self.clock_ns()
                attempt["car_episode_open"] = rec.get("episode_id") == attempt["episode_id"]
                task["stage"] = "interrupted"
                self._journal(task, "attempt_interrupted", attempt_id=attempt["attempt_id"],
                              reason=attempt["interrupt_reason"])
                self._save_task(task)
                return
            outputs = status.get("outputs_enabled") is True
            following = (status.get("channels", {}).get("arm", {}).get("state")
                         == "following")
            if attempt["state"] == "recording" and not (outputs and following):
                attempt["state"] = "paused"
                attempt["pause_reason"] = status.get("last_error") or "outputs_paused"
                self._journal(task, "attempt_paused", attempt_id=attempt["attempt_id"],
                              reason=attempt["pause_reason"])
                self._save_task(task)
            elif attempt["state"] == "paused" and outputs and following:
                attempt["state"] = "recording"
                attempt["pause_reason"] = None
                self._save_task(task)
        elif rec.get("status") == "invalid" and task.get("stage") == "open":
            # Estop/fault with no open attempt still invalidates the car session;
            # the task needs explicit recovery instead of silently continuing.
            task["stage"] = "interrupted"
            task["interrupt_reason"] = rec.get("last_error") or "recording_invalid"
            self._journal(task, "task_interrupted", reason=task["interrupt_reason"])
            self._save_task(task)

    # -- read model ----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        try:
            control = self._facade_status()
            control_error = None
        except Exception as exc:  # noqa: BLE001 - offline must stay pollable
            control = {"state": "unreachable", "session_id": None, "session_epoch": None,
                       "outputs_enabled": False}
            control_error = _reason_of(exc)
        with self.lock:
            self._reconcile(control)
            task = self._active_task()
            attempt = self._open_attempt(task)
            current = self._current_attempt(task)
            rec = {}
            try:
                rec = self.recording.status() if self.recording is not None else {}
            except Exception:  # noqa: BLE001
                rec = {"status": "unavailable"}
            stage = self._operator_stage(control, task, current, rec)
            recording_active = bool(
                attempt is not None and attempt["state"] == "recording"
                and stage == "recording"
                and rec.get("status") == "active"
                and (rec.get("session_id"), rec.get("session_epoch"))
                == (attempt.get("session_id"), attempt.get("session_epoch"))
                and control.get("outputs_enabled") is True)
            elapsed_s = None
            if current is not None and current.get("started_upper_mono_ns") is not None:
                end_mono = current.get("ended_upper_mono_ns")
                if current["state"] in ("recording", "paused") and end_mono is None:
                    elapsed_s = max(0.0, (self.clock_ns() - current["started_upper_mono_ns"]) / 1e9)
                elif end_mono is not None:
                    elapsed_s = max(0.0, (end_mono - current["started_upper_mono_ns"]) / 1e9)
            return {
                "source": "hybrid_capture_orchestrator",
                "operator_stage": stage,
                "recording_active": recording_active,
                "elapsed_s": round(elapsed_s, 2) if elapsed_s is not None else None,
                "control": control,
                "control_error": control_error,
                "recording": rec,
                "task": self._task_view(task),
                "attempt": copy.deepcopy(current) if current else None,
                "blocking": self._blocking(control, task, attempt),
                "server_wall_ns": self.wall_ns(),
            }

    def _operator_stage(self, control: dict[str, Any], task: dict[str, Any] | None,
                        attempt: dict[str, Any] | None, rec: dict[str, Any]) -> str:
        if control.get("state") == "unreachable":
            return "offline"
        if control.get("estop_latched") is True or control.get("state") == "estop":
            return "estop"
        if task is not None and task.get("stage") in ("closed", "done", "failed"):
            # Sync/export progress does not depend on holding control.
            if task.get("stage") == "done":
                return "done"
            if task.get("stage") == "failed":
                return "pipeline_failed"
            pipeline = (task.get("pipeline") or {}).get("stage")
            if pipeline == "exported":
                return "done"
            if pipeline == "failed":
                return "pipeline_failed"
            return "pipeline"
        if not control.get("session_id") or control.get("state") not in ("ready", "fault"):
            return "no_control"
        if control.get("state") == "fault":
            return "fault"
        if task is not None and task.get("stage") == "interrupted":
            return "interrupted"
        if attempt is not None and attempt["state"] == "interrupted":
            return "interrupted"
        if task is None:
            return "control_ready"
        if attempt is not None:
            if attempt["state"] == "starting":
                return "starting"
            if attempt["state"] == "recording":
                return "recording"
            if attempt["state"] == "paused":
                return "paused"
            if attempt["state"] == "review":
                return "review"
        if task.get("awaiting_reset"):
            return "reset"
        return "dataset_idle"

    def _task_view(self, task: dict[str, Any] | None) -> dict[str, Any] | None:
        if task is None:
            return None
        view = {key: task.get(key) for key in (
            "task_id", "name", "description", "fps", "target_episodes", "mode",
            "append_to", "stage", "created_wall", "session_id", "session_epoch",
            "calibration_bundle_hash", "car_recording_id", "car_session_path",
            "clock_domain", "clock_epoch", "profile", "simulated", "offline_6d",
            "saved_count", "awaiting_reset", "interrupt_reason", "pipeline",
            "feedback_limits_version")}
        view["attempt_count"] = len(task.get("attempts", []))
        view["attempts"] = [{key: attempt.get(key) for key in (
            "attempt_id", "episode_id", "state", "phase", "result",
            "started_time_ns", "ended_time_ns", "interrupt_reason",
            "export_eligible", "eligibility_reasons", "created_wall")}
            for attempt in task.get("attempts", [])]
        view["review_pending"] = any(a.get("state") == "review"
                                     for a in task.get("attempts", []))
        return view

    def _blocking(self, control: dict[str, Any], task: dict[str, Any] | None,
                  attempt: dict[str, Any] | None) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        if control.get("state") == "unreachable":
            out.append({"code": "car_unreachable", "message": "车端状态不可确认"})
            return out
        if control.get("estop_latched") is True or control.get("state") == "estop":
            out.append({"code": "estop_latched", "message": "急停已锁存，清除后才能继续"})
        if control.get("state") == "fault":
            out.append({"code": "control_fault",
                        "message": str(control.get("last_error") or "控制会话故障")})
        if not control.get("session_id"):
            out.append({"code": "no_control", "message": "尚未获取控制权"})
            return out
        if control.get("state") != "ready":
            out.append({"code": "control_not_ready",
                        "message": str(control.get("last_error") or "会话未就绪")})
            return out
        video = control.get("video") if isinstance(control.get("video"), dict) else {}
        age = video.get("age_s")
        if (video.get("available") is not True or video.get("stale") is True
                or not isinstance(age, (int, float)) or isinstance(age, bool) or age > 1.0):
            out.append({"code": "camera_frame_stale", "message": "相机画面过期，REC 被阻止"})
        feedback = control.get("feedback") if isinstance(control.get("feedback"), dict) else {}
        if feedback.get("valid") is not True or feedback.get("trusted") is not True:
            out.append({"code": "feedback_untrusted", "message": "六路实测反馈不满足安全门限"})
        if task is not None and task.get("stage") == "interrupted":
            out.append({"code": "task_interrupted",
                        "message": "旧采集已中断，先收尾旧数据再开始新数据集"})
        if attempt is not None and attempt["state"] == "paused":
            out.append({"code": "attempt_paused",
                        "message": str(attempt.get("pause_reason") or "输出已暂停")})
        return out

    # -- request handling ----------------------------------------------------
    def handle(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action not in CAPTURE_ACTIONS:
            raise CaptureError("unknown_capture_action", str(action), status_code=400)
        payload = dict(payload or {})
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise CaptureError("request_id_required",
                               "every capture mutation needs a stable request_id",
                               status_code=400)
        with self.lock:
            existing = self._requests.get(request_id)
            if existing is not None:
                if existing["state"] == "processing":
                    return {"request_id": request_id, "state": "processing",
                            "action": existing["action"], "capture": self.status()}
                return {**existing["response"], "replayed": True}
            self._requests[request_id] = {"state": "processing", "action": action,
                                          "started_wall": time.time()}
        response: dict[str, Any]
        try:
            with self._action_lock:
                result = self._dispatch(action, payload)
            response = {"request_id": request_id, "state": "completed",
                        "action": action, "result": result, "capture": self.status()}
        except Exception as exc:  # noqa: BLE001 - one primary reason reaches the operator
            reason = _reason_of(exc)
            response = {"request_id": request_id, "state": "failed", "action": action,
                        "error": reason, "capture": self.status()}
        with self.lock:
            self._requests[request_id] = {"state": response["state"], "action": action,
                                          "response": response}
            task = self._active_task()
            if task is not None:
                self._journal(task, f"request_{response['state']}", action=action,
                              request_id=request_id,
                              error=reason if response["state"] == "failed" else None)
        return response

    def _dispatch(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_act_{action}")
        return handler(payload)

    # -- actions -------------------------------------------------------------
    def _act_acquire(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = self._facade_status()
        if status.get("session_id") and status.get("state") == "ready":
            return {"acquired": False, "reason": "session_already_active",
                    "session_id": status["session_id"],
                    "session_epoch": status.get("session_epoch")}
        if status.get("session_id") and status.get("state") in ("fault", "estop"):
            raise CaptureError("recover_before_acquire",
                               f"state={status['state']}: "
                               f"{status.get('last_error') or ''}; recover or clear first")
        owner = str(payload.get("owner") or "capture-operator")
        acquired = self.facade.acquire_session({"owner": owner, **{
            key: payload[key] for key in ("profile", "transport") if key in payload}})
        identity = {"session_id": acquired.get("session_id"),
                    "session_epoch": acquired.get("session_epoch")}
        try:
            self.facade.preview(dict(identity))
            self.facade.set_arm_state({"action": "align", **identity})
        except Exception:
            # No half-connected control: release what was acquired, keep the cause.
            try:
                self.facade.release_session("capture_acquire_prepare_failed",
                                            session_id=identity["session_id"],
                                            session_epoch=identity["session_epoch"])
            except Exception:  # noqa: BLE001
                pass
            raise
        return {"acquired": True, **identity}

    def _act_open_dataset(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = self._facade_status()
        self._control_ready(status)
        with self.lock:
            self._reconcile(status)
            existing = self._active_task()
            # A task still holding a live car recording session (open) or needing
            # recovery (interrupted) must be closed first. A "closed/done/failed"
            # task has already finalized its raw bag, so a new dataset can begin
            # even while its background sync/export pipeline is still running.
            if existing is not None and existing.get("stage") in ("open", "interrupted"):
                raise CaptureError("task_already_open",
                                   f"task {existing['task_id']} is {existing['stage']}; "
                                   "close or recover it first")
        name = str(payload.get("name") or "").strip()
        description = str(payload.get("task") or payload.get("description") or "").strip()
        if not name:
            raise CaptureError("dataset_name_required", status_code=400)
        if not description:
            raise CaptureError("task_description_required", status_code=400)
        mode = str(payload.get("mode") or "new")
        if mode not in ("new", "append"):
            raise CaptureError("unknown_dataset_mode", mode, status_code=400)
        fps = payload.get("fps")
        if fps is None:
            fps = 14.0
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not 1 <= fps <= 120:
            raise CaptureError("invalid_fps", repr(fps), status_code=400)
        target_episodes = payload.get("target_episodes")
        if target_episodes is not None and (
                isinstance(target_episodes, bool) or not isinstance(target_episodes, int)
                or not 1 <= target_episodes <= 10000):
            raise CaptureError("invalid_target_episodes", repr(target_episodes), status_code=400)
        append_to = None
        if mode == "append":
            repo_id = str(payload.get("repo_id") or "").strip()
            if not repo_id or "/" not in repo_id or ".." in repo_id:
                raise CaptureError("append_repo_id_required", status_code=400)
            expected_identity = ("simulated_offline"
                                 if getattr(self.facade, "offline_agent_recording", False)
                                 else "hardware")
            append_to = self._check_append_compatibility(repo_id, expected_identity)
        identity = {"session_id": status["session_id"],
                    "session_epoch": status.get("session_epoch")}
        out = self.recording.dispatch("start_session", {
            "task": description, **identity})
        task_id = f"cap_{uuid.uuid4().hex[:12]}"
        limits = ((status.get("frame_process") or {}).get("feedback_raw_limits") or {})
        task = {
            "task_id": task_id, "name": name, "description": description,
            "fps": float(fps), "target_episodes": target_episodes,
            "mode": mode, "append_to": append_to,
            "stage": "open", "created_wall": time.time(),
            "session_id": identity["session_id"],
            "session_epoch": identity["session_epoch"],
            "calibration_bundle_hash": out.get("calibration_bundle_hash")
            or status.get("calibration_bundle_hash"),
            "car_recording_id": out.get("car_recording_id"),
            "car_session_path": out.get("car_session_path"),
            "clock_domain": out.get("clock_domain"),
            "clock_epoch": out.get("clock_epoch"),
            "profile": status.get("profile"),
            "simulated": status.get("simulated") is True,
            "offline_6d": bool(getattr(self.facade, "offline_agent_recording", False)),
            "feedback_limits_version": limits.get("version"),
            "saved_count": 0, "awaiting_reset": False,
            "attempts": [], "pipeline": {"stage": "idle"},
        }
        with self.lock:
            self._tasks[task_id] = task
            self._active_task_id = task_id
            self._journal(task, "dataset_opened", name=name, mode=mode,
                          car_recording_id=task["car_recording_id"])
            self._save_task(task)
        return {"task_id": task_id, "car_recording_id": task["car_recording_id"]}

    def _check_append_compatibility(self, repo_id: str,
                                    expected_identity: str) -> dict[str, Any]:
        """Structural pre-check for append.

        The authoritative comparison (exact generated names, units, image key and
        fps) runs at export against the real dataset copy; here we only reject an
        obviously-incompatible target early so the operator does not open a session
        that is doomed. Joint ORDER is checked on the stripped feature names so the
        suffixed ``.target``/``.position`` layout and either wrist camera name are
        both accepted.
        """
        if self.dataset_meta is None:
            raise CaptureError("append_unavailable", "dataset metadata reader not wired",
                               status_code=503)
        try:
            meta = self.dataset_meta(repo_id)
        except Exception as exc:  # noqa: BLE001
            raise CaptureError("append_dataset_unreadable",
                               f"{repo_id}: {type(exc).__name__}: {exc}") from exc
        expected_joints = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]

        def strip(name: str) -> str:
            return name.rsplit(".", 1)[0] if "." in name else name

        reasons: list[str] = []
        if meta.get("robot_type") != "lelab_mobile_arm_gripper_phase1":
            reasons.append(f"robot_type={meta.get('robot_type')!r}")
        existing_fps = meta.get("fps")
        if (isinstance(existing_fps, bool) or not isinstance(existing_fps, (int, float))
                or existing_fps < 1 or float(existing_fps) != int(existing_fps)):
            reasons.append(f"fps={existing_fps!r} not a whole-number rate")
        features = meta.get("features") or {}
        for key in ("observation.state", "action"):
            feature = features.get(key) or {}
            if feature.get("dtype") != "float32" or list(feature.get("shape") or []) != [6]:
                reasons.append(f"{key}.shape/dtype={feature.get('shape')}/{feature.get('dtype')}")
            names = [strip(str(name)) for name in (feature.get("names") or [])]
            if names != expected_joints:
                reasons.append(f"{key}.joint_order={names}")
            units = ((feature.get("info") or {}).get("units")) or []
            if units and [str(u).split("/")[0] for u in units] != ["rad"] * 6:
                reasons.append(f"{key}.units={units}")
        video_keys = [key for key in features if key.startswith("observation.images.")]
        if len(video_keys) != 1:
            reasons.append(f"cameras={video_keys} (expected exactly one wrist stream)")
        if meta.get("data_identity") not in (None, expected_identity):
            reasons.append(f"data_identity={meta.get('data_identity')!r} != {expected_identity!r}")
        if reasons:
            raise CaptureError("append_incompatible", "; ".join(reasons))
        return {"repo_id": repo_id, "checked_wall": time.time(),
                "robot_type": meta.get("robot_type"), "fps": existing_fps,
                "total_episodes": meta.get("total_episodes"),
                "camera": video_keys[0] if video_keys else None,
                "compatibility": ("robot_type/joint_order/units/one-camera/data_identity "
                                  "verified; exact names/fps/units rechecked at export")}

    def _require_task(self, status: dict[str, Any]) -> dict[str, Any]:
        self._control_ready(status)
        with self.lock:
            self._reconcile(status)
            task = self._active_task()
        if task is None or task.get("stage") != "open":
            raise CaptureError("dataset_not_open",
                               f"task stage={task.get('stage') if task else None}")
        if (task.get("session_id"), task.get("session_epoch")) != self._identity(status):
            raise CaptureError("control_session_changed",
                               "dataset belongs to an older control session; recover it first")
        return task

    def _engage_follow(self, identity: dict[str, Any]) -> None:
        """The existing gated wizard, executed by the backend as one explicit intent."""
        self.facade.preview(dict(identity))
        self.facade.set_arm_state({"action": "align", **identity})
        self.facade.set_arm_state({"action": "arm", **identity})
        self.facade.set_arm_state({"action": "engage", **identity})

    def _act_start_episode(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = self._facade_status()
        task = self._require_task(status)
        self._check_camera(status)
        self._check_feedback(status)
        with self.lock:
            if self._open_attempt(task) is not None:
                raise CaptureError("episode_already_open")
            if task.get("awaiting_reset"):
                raise CaptureError("reset_not_acknowledged",
                                   "acknowledge the reset step before the next episode")
        attempt_id = f"att_{uuid.uuid4().hex[:12]}"
        attempt = {
            "attempt_id": attempt_id, "episode_id": attempt_id,
            "session_id": task["session_id"], "session_epoch": task["session_epoch"],
            "state": "starting", "phase": None, "result": "unknown",
            "started_time_ns": None, "ended_time_ns": None,
            "started_upper_mono_ns": None, "ended_upper_mono_ns": None,
            "created_wall": time.time(), "request_id": payload.get("request_id"),
            "export_eligible": False, "eligibility_reasons": ["attempt_open"],
        }
        with self.lock:
            task["attempts"].append(attempt)
            self._journal(task, "attempt_starting", attempt_id=attempt_id)
            self._save_task(task)
        identity = {"session_id": task["session_id"], "session_epoch": task["session_epoch"]}
        try:
            out = self.recording.dispatch("start_episode", {
                "episode_id": attempt_id, "task": task["description"], **identity})
            started_ns = out.get("episode_started_time_ns")
            self.recording.dispatch("set_phase", {"phase": "observe", **identity})
        except Exception as exc:
            with self.lock:
                attempt["state"] = "failed"
                attempt["failure_reason"] = _reason_of(exc)["reason"]
                attempt["ended_upper_mono_ns"] = self.clock_ns()
                attempt["eligibility_reasons"] = [f"start_failed:{attempt['failure_reason']}"]
                self._journal(task, "attempt_failed", attempt_id=attempt_id,
                              reason=attempt["failure_reason"])
                self._save_task(task)
            self._safe_pause()
            raise
        try:
            self._engage_follow(identity)
        except Exception as exc:
            with self.lock:
                attempt["state"] = "paused"
                attempt["phase"] = "observe"
                attempt["started_time_ns"] = started_ns
                attempt["started_upper_mono_ns"] = self.clock_ns()
                attempt["pause_reason"] = _reason_of(exc)["reason"]
                attempt["eligibility_reasons"] = ["paused_before_follow"]
                self._journal(task, "attempt_paused", attempt_id=attempt_id,
                              reason=attempt["pause_reason"])
                self._save_task(task)
            raise CaptureError("episode_paused_before_follow",
                               f"episode opened but follow did not engage: "
                               f"{attempt['pause_reason']}; resume explicitly") from exc
        with self.lock:
            attempt["state"] = "recording"
            attempt["phase"] = "observe"
            attempt["started_time_ns"] = started_ns
            attempt["started_upper_mono_ns"] = self.clock_ns()
            attempt["eligibility_reasons"] = []
            self._journal(task, "attempt_recording", attempt_id=attempt_id,
                          car_time_ns=started_ns)
            self._save_task(task)
        return {"attempt_id": attempt_id, "episode_id": attempt_id,
                "started_time_ns": started_ns}

    def _safe_pause(self) -> None:
        try:
            self.facade.set_arm_state("pause_all")
        except Exception:  # noqa: BLE001 - the stop attempt is best-effort; status shows the truth
            pass

    _ALREADY_CLOSED_TOKENS = (
        "no_episode", "no_active_episode", "no_session", "recording_session_not_started",
        "session_mismatch", "belongs to another recording session",
        "episode_mismatch", "clock_epoch_mismatch",
    )

    def _car_close_step(self, car, path: str, body: dict) -> None:
        """Close one recording object on the car during recovery, tolerating 'already
        gone' answers. Whichever transport raised (RecordingError, BridgeError or a
        controller RuntimeError), a step that reports the object already closed or
        belonging to another session is not a recovery failure: the pipeline's sync
        re-fetches the car's real manifest and only a raw_ready session exports."""
        try:
            car.post(path, body)
        except Exception as exc:  # noqa: BLE001 - classify below, never swallow silently
            text = " ".join(filter(None, [
                str(getattr(exc, "reason", "") or ""),
                str(getattr(exc, "detail", "") or ""),
                str(exc)])).lower()
            if any(token in text for token in (
                    t.lower() for t in self._ALREADY_CLOSED_TOKENS)):
                return
            raise

    def _act_mark_grasp(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = self._facade_status()
        task = self._require_task(status)
        with self.lock:
            attempt = self._open_attempt(task)
            if attempt is None or attempt["state"] not in ("recording", "paused"):
                raise CaptureError("episode_not_open")
            if attempt.get("phase") == "grasp":
                raise CaptureError("phase_already_active", "grasp")
            was_following = attempt["state"] == "recording"
        identity = {"session_id": task["session_id"], "session_epoch": task["session_epoch"]}
        self.recording.dispatch("set_phase", {"phase": "grasp", **identity})
        with self.lock:
            attempt["phase"] = "grasp"
            self._journal(task, "phase_marked", attempt_id=attempt["attempt_id"],
                          phase="grasp")
            self._save_task(task)
        if not was_following:
            # Marking a phase while paused never sneaks motion back on.
            return {"attempt_id": attempt["attempt_id"], "phase": "grasp",
                    "outputs_enabled": False}
        try:
            self._engage_follow(identity)
        except Exception as exc:
            with self.lock:
                attempt["state"] = "paused"
                attempt["pause_reason"] = _reason_of(exc)["reason"]
                self._journal(task, "attempt_paused", attempt_id=attempt["attempt_id"],
                              reason=attempt["pause_reason"])
                self._save_task(task)
            raise CaptureError("grasp_paused",
                               f"grasp boundary recorded; follow paused: "
                               f"{attempt['pause_reason']}; resume explicitly") from exc
        with self.lock:
            attempt["state"] = "recording"
            self._save_task(task)
        return {"attempt_id": attempt["attempt_id"], "phase": "grasp",
                "outputs_enabled": True}

    def _act_end_episode(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = self._facade_status()
        task = self._require_task(status)
        with self.lock:
            attempt = self._open_attempt(task)
            if attempt is None:
                raise CaptureError("no_open_episode")
        result = str(payload.get("result") or "unknown")
        if result not in ("success", "failure", "unknown"):
            raise CaptureError("invalid_manual_result", result, status_code=400)
        identity = {"session_id": task["session_id"], "session_epoch": task["session_epoch"]}
        # One intent: the controller pauses output before closing the car window.
        out = self.recording.dispatch("end_episode", {
            "success": None if result == "unknown" else result == "success",
            "note": str(payload.get("note") or ""), **identity})
        ended_ns = None
        episodes = out.get("episodes") or []
        if episodes:
            ended_ns = episodes[-1].get("ended_time_ns")
        with self.lock:
            attempt["state"] = "review"
            attempt["result"] = result
            attempt["ended_time_ns"] = ended_ns
            attempt["ended_upper_mono_ns"] = self.clock_ns()
            attempt["note"] = str(payload.get("note") or "")
            attempt["eligibility_reasons"] = ["awaiting_review"]
            self._journal(task, "attempt_review", attempt_id=attempt["attempt_id"],
                          result=result, car_time_ns=ended_ns)
            self._save_task(task)
        return {"attempt_id": attempt["attempt_id"], "state": "review",
                "ended_time_ns": ended_ns}

    def _act_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = self._facade_status()
        task = self._require_task(status)
        attempt_id = str(payload.get("attempt_id") or "")
        decision = str(payload.get("decision") or "")
        if decision not in ("save", "discard", "rerecord"):
            raise CaptureError("unknown_review_decision", decision, status_code=400)
        with self.lock:
            attempt = next((a for a in task["attempts"]
                            if a["attempt_id"] == attempt_id), None)
            if attempt is None:
                raise CaptureError("attempt_not_found", attempt_id, status_code=404)
            if attempt["state"] == decision_past(decision):
                return {"attempt_id": attempt_id, "decision": decision,
                        "saved_count": task["saved_count"], "already": True}
            if attempt["state"] != "review":
                raise CaptureError("attempt_not_in_review",
                                   f"{attempt_id} is {attempt['state']}")
            attempt["state"] = decision_past(decision)
            attempt["decided_wall"] = time.time()
            if decision == "save":
                task["saved_count"] = int(task.get("saved_count", 0)) + 1
                attempt["export_eligible"] = True
                attempt["eligibility_reasons"] = []
            else:
                attempt["export_eligible"] = False
                attempt["eligibility_reasons"] = [f"operator_{decision}"]
            task["awaiting_reset"] = True
            self._journal(task, f"attempt_{decision}", attempt_id=attempt_id,
                          saved_count=task["saved_count"])
            self._save_task(task)
        return {"attempt_id": attempt_id, "decision": decision,
                "saved_count": task["saved_count"]}

    def _act_next_episode(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = self._facade_status()
        task = self._require_task(status)
        with self.lock:
            if self._open_attempt(task) is not None:
                raise CaptureError("episode_still_open")
            if any(a["state"] == "review" for a in task["attempts"]):
                raise CaptureError("review_pending", "decide the pending episode first")
            task["awaiting_reset"] = False
            self._journal(task, "reset_acknowledged")
            self._save_task(task)
        return {"ready_for_next": True, "saved_count": task["saved_count"]}

    def _act_resume_follow(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = self._facade_status()
        self._control_ready(status)
        self._check_feedback(status)
        with self.lock:
            self._reconcile(status)
            task = self._active_task()
            attempt = self._open_attempt(task)
        identity = {"session_id": status["session_id"],
                    "session_epoch": status.get("session_epoch")}
        self._engage_follow(identity)
        with self.lock:
            if attempt is not None and task is not None:
                attempt["state"] = "recording"
                attempt["pause_reason"] = None
                self._journal(task, "attempt_resumed", attempt_id=attempt["attempt_id"])
                self._save_task(task)
        return {"outputs_enabled": True,
                "attempt_id": attempt["attempt_id"] if attempt else None}

    def _act_close_dataset(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = self._facade_status()
        self._control_ready(status)
        with self.lock:
            self._reconcile(status)
            task = self._active_task()
        if task is None or task.get("stage") != "open":
            raise CaptureError("dataset_not_open")
        if (task.get("session_id"), task.get("session_epoch")) != self._identity(status):
            raise CaptureError("control_session_changed", "recover the old task first")
        self._safe_pause()
        with self.lock:
            attempt = self._open_attempt(task)
        if attempt is not None:
            identity = {"session_id": task["session_id"],
                        "session_epoch": task["session_epoch"]}
            self.recording.dispatch("end_episode", {
                "success": None, "reason": "dataset_closed", **identity})
            with self.lock:
                attempt["state"] = "review"
                attempt["result"] = "unknown"
                attempt["ended_upper_mono_ns"] = self.clock_ns()
                attempt["eligibility_reasons"] = ["awaiting_review"]
                self._journal(task, "attempt_review", attempt_id=attempt["attempt_id"],
                              result="unknown", reason="dataset_closed")
                self._save_task(task)
        with self.lock:
            pending = [a["attempt_id"] for a in task["attempts"] if a["state"] == "review"]
        if pending:
            raise CaptureError("review_pending",
                               f"episodes awaiting save/discard/rerecord: {', '.join(pending)}; "
                               "outputs are stopped, decide them before closing")
        identity = {"session_id": task["session_id"], "session_epoch": task["session_epoch"]}
        try:
            self.recording.dispatch("stop_session",
                                    {"reason": "operator_closed_dataset", **identity})
        except RecordingError as exc:
            # A stop whose HTTP response was lost still closed the raw bag on the car;
            # "no session to stop" is not a reason to wedge the task. The pipeline's
            # sync re-verifies the real manifest/READY, so nothing is claimed blindly.
            if "no session" not in f"{exc.reason} {exc.detail}".lower():
                raise
            with self.lock:
                self._journal(task, "stop_session_already_closed", error=str(exc))
        with self.lock:
            task["stage"] = "closed"
            task["pipeline"] = {"stage": "stopping", "updated_wall": time.time()}
            self._journal(task, "dataset_closed",
                          car_recording_id=task.get("car_recording_id"))
            self._save_task(task)
        try:
            self.facade.release_session("dataset_closed",
                                        session_id=identity["session_id"],
                                        session_epoch=identity["session_epoch"])
        except Exception as exc:  # noqa: BLE001 - raw is closed; release failure is reported
            with self.lock:
                task["release_warning"] = f"{type(exc).__name__}: {exc}"
                self._save_task(task)
        self._start_pipeline(task["task_id"])
        return {"task_id": task["task_id"], "closed": True,
                "car_recording_id": task.get("car_recording_id")}

    def _act_recover(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Close the old car session after control was lost, then let a new task start."""
        status = self._facade_status()
        self._control_ready(status)
        with self.lock:
            self._reconcile(status)
            task = self._active_task()
        if task is None or task.get("stage") not in ("interrupted", "open"):
            raise CaptureError("nothing_to_recover")
        if (task.get("session_id"), task.get("session_epoch")) == self._identity(status):
            raise CaptureError("recover_requires_new_session",
                               "control session still matches the old task; "
                               "release and re-acquire after a fault, or close the dataset normally")
        car = getattr(self.recording, "car", None)
        if car is None:
            raise CaptureError("car_client_unavailable", status_code=503)
        # The recording is addressed by the CAR's own session id: releasing the old
        # control lease clears the upper control session on the car, but the raw
        # recording (car_recording_id + its epoch) is exactly what must be closed.
        old_identity = {"session_id": task.get("car_recording_id"),
                        "session_epoch": task["session_epoch"],
                        "calibration_bundle_hash": task.get("calibration_bundle_hash")}
        attempt = next((a for a in reversed(task.get("attempts", []))
                        if a.get("state") == "interrupted" and a.get("car_episode_open")), None)
        if attempt is not None:
            self._car_close_step(
                car, "/recording/end_episode",
                {"episode_id": attempt["episode_id"], "discarded": True,
                 "reason": f"control_lost:{attempt.get('interrupt_reason') or 'unknown'}",
                 **old_identity})
            with self.lock:
                attempt["car_episode_open"] = False
                attempt["ended_upper_mono_ns"] = self.clock_ns()
                attempt["export_eligible"] = False
                attempt["eligibility_reasons"] = ["control_lost"]
                self._save_task(task)
        self._car_close_step(
            car, "/recording/stop_session",
            {"reason": "recovered_after_control_loss", **old_identity})
        with self.lock:
            task["stage"] = "closed"
            task["recovered_wall"] = time.time()
            task["pipeline"] = {"stage": "stopping", "updated_wall": time.time()}
            self._journal(task, "task_recovered",
                          new_session_id=status.get("session_id"))
            self._save_task(task)
        self._start_pipeline(task["task_id"])
        return {"task_id": task["task_id"], "recovered": True}

    def _act_retry_pipeline(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            task = self._active_task()
            if task is None or task.get("stage") not in ("closed", "failed"):
                raise CaptureError("pipeline_not_retryable")
            pipeline = task.get("pipeline") or {}
            if pipeline.get("stage") in ("syncing", "exporting", "stopping"):
                raise CaptureError("pipeline_running")
            task["stage"] = "closed"
            task["pipeline"] = {"stage": "stopping", "updated_wall": time.time()}
            self._save_task(task)
        self._start_pipeline(task["task_id"])
        return {"task_id": task["task_id"], "retried": True}

    # -- background pipeline: stop never waits for sync/export ----------------
    def _start_pipeline(self, task_id: str) -> None:
        if not self.background:
            self._run_pipeline(task_id)
            return
        thread = threading.Thread(target=self._run_pipeline, args=(task_id,),
                                  name=f"capture-pipeline-{task_id}", daemon=True)
        self._pipeline_threads[task_id] = thread
        thread.start()

    def _run_pipeline(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        session_id = task.get("car_recording_id")
        try:
            if not session_id:
                raise CaptureError("car_recording_id_missing")
            if self.sync_fn is None or self.export_spawn is None or self.export_status is None:
                raise CaptureError("pipeline_not_wired", status_code=503)
            with self.lock:
                task["pipeline"] = {"stage": "syncing", "updated_wall": time.time()}
                self._save_task(task)
            sync = self.sync_fn(session_id)
            with self.lock:
                task["pipeline"] = {"stage": "synced", "sync_path": sync.get("path"),
                                    "manifest_sha256": sync.get("manifest_sha256"),
                                    "updated_wall": time.time()}
                self._journal(task, "sync_completed", session_id=session_id)
                self._save_task(task)
                self._write_review_file(task)
            job = self.export_spawn(session_id, False, bool(task.get("offline_6d")))
            job_id = job.get("job_id")
            with self.lock:
                task["pipeline"] = {**task["pipeline"], "stage": "exporting",
                                    "job_id": job_id, "updated_wall": time.time()}
                self._save_task(task)
            deadline = time.monotonic() + 3600
            while time.monotonic() < deadline:
                state = self.export_status(job_id)
                if state.get("status") != "running":
                    break
                time.sleep(1.0)
            else:
                raise CaptureError("export_timeout", f"job {job_id} still running after 1 h")
            if state.get("status") == "completed":
                report = state.get("report") or {}
                with self.lock:
                    task["stage"] = "done"
                    task["pipeline"] = {**task["pipeline"], "stage": "exported",
                                        "repo_id": report.get("repo_id"),
                                        "dataset_root": report.get("dataset_root"),
                                        "total_episodes": report.get("total_episodes"),
                                        "total_frames": report.get("total_frames"),
                                        "updated_wall": time.time()}
                    self._journal(task, "export_completed", repo_id=report.get("repo_id"))
                    self._save_task(task)
            else:
                raise CaptureError("export_failed",
                                   str(state.get("error") or state.get("status") or "unknown"))
        except Exception as exc:  # noqa: BLE001 - the failure is the pipeline result
            reason = _reason_of(exc)
            with self.lock:
                task["stage"] = "failed"
                task["pipeline"] = {**(task.get("pipeline") or {}), "stage": "failed",
                                    "error": f"{reason['reason']}: {reason['detail']}",
                                    "updated_wall": time.time()}
                self._journal(task, "pipeline_failed", error=task["pipeline"]["error"])
                self._save_task(task)

    def _write_review_file(self, task: dict[str, Any]) -> None:
        """Upper-side review decisions consumed by the export gate (raw stays untouched)."""
        if self.export_spawn is None:
            return
        from . import ros_mobile

        directory = Path(ros_mobile.UPPER_EXPORTS) / task["car_recording_id"]
        directory.mkdir(parents=True, exist_ok=True)
        review = {
            "schema_version": 1,
            "task_id": task["task_id"],
            "name": task["name"],
            "description": task["description"],
            "session_id": task["session_id"],
            "session_epoch": task["session_epoch"],
            "calibration_bundle_hash": task.get("calibration_bundle_hash"),
            "clock_domain": task.get("clock_domain"),
            "clock_epoch": task.get("clock_epoch"),
            "feedback_limits_version": task.get("feedback_limits_version"),
            "simulated_offline": bool(task.get("offline_6d")),
            "append_to": (task.get("append_to") or {}).get("repo_id"),
            "dispositions": {a["episode_id"]: a["state"] for a in task.get("attempts", [])
                             if a.get("episode_id")},
            "results": {a["episode_id"]: a.get("result", "unknown")
                        for a in task.get("attempts", []) if a.get("episode_id")},
            "written_wall": time.time(),
        }
        tmp = directory / f".capture-review-{uuid.uuid4().hex[:8]}.tmp"
        tmp.write_text(json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, directory / "capture_review.json")


def decision_past(decision: str) -> str:
    return {"save": "saved", "discard": "discarded", "rerecord": "rerecorded"}[decision]