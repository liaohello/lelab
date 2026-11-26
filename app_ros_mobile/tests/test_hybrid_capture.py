"""Orchestration contracts for the operator capture flow (offline, fake device edge).

The facade here is a state-machine stand-in; the recording controller and its
in-memory car client are the production classes. Real lease/TTL/feedback gates
are exercised by the full-stack isolated run, not by these tests.
"""
import json
import uuid

import pytest

from lelab import ros_mobile
from lelab.hybrid_capture import CaptureOrchestrator
from lelab.hybrid_recording import HybridRecordingController, InMemoryRecordingCarClient


class FakeFacade:
    def __init__(self):
        self.state = "disconnected"
        self.session_id = None
        self.session_epoch = None
        self.arm_state = "idle"
        self.outputs = False
        self.estop = False
        self.video = {"available": True, "stale": False, "age_s": 0.05}
        self.feedback = {"valid": True, "trusted": True, "trusted_reasons": []}
        self.offline_agent_recording = False
        self.phase = None
        self._next_epoch = 100
        self.prepare_calls = 0
        self.acquire_calls = 0
        self.frame_process = {}

    def status(self):
        return {"state": self.state, "session_id": self.session_id,
                "session_epoch": self.session_epoch,
                "outputs_enabled": self.outputs, "estop_latched": self.estop,
                "channels": {"arm": {"state": self.arm_state}},
                "video": dict(self.video), "feedback": dict(self.feedback),
                "last_error": None, "calibration_bundle_hash": "bundle-test",
                "profile": "test-profile", "simulated": False, "frame_process": self.frame_process}

    def acquire_session(self, request):
        self.acquire_calls += 1
        if self.session_id:
            raise RuntimeError("session_already_active")
        if self.estop:
            raise RuntimeError("estop_latched")
        self.session_id = f"sess-{uuid.uuid4().hex[:8]}"
        self.session_epoch = self._next_epoch
        self._next_epoch += 1
        self.state = "ready"
        self.arm_state = "idle"
        return self.status()

    def _check(self, identity):
        if isinstance(identity, dict) and identity.get("session_id") is not None:
            if (identity.get("session_id") != self.session_id
                    or identity.get("session_epoch") != self.session_epoch):
                raise RuntimeError("session_epoch_mismatch")
        if self.state != "ready":
            raise RuntimeError("session_not_ready")

    def preview(self, identity=None):
        self._check(identity)
        self.arm_state = "preview"
        return self.status()

    def set_arm_state(self, event):
        action = event.get("action") if isinstance(event, dict) else event
        if action == "pause_all":
            self.outputs = False
            self.arm_state = "idle"
            return self.status()
        if action == "estop":
            self.estop = True
            self.state = "estop"
            self.outputs = False
            self.arm_state = "idle"
            return self.status()
        self._check(event)
        if action == "align" and self.arm_state == "preview":
            self.arm_state = "aligned"
        elif action == "arm" and self.arm_state == "aligned":
            self.arm_state = "armed"
        elif action == "engage" and self.arm_state == "armed":
            self.arm_state = "following"
            self.outputs = True
        else:
            raise RuntimeError("invalid_transition")
        return self.status()

    def release_session(self, reason, session_id=None, session_epoch=None):
        self.session_id = None
        self.session_epoch = None
        self.state = "disconnected"
        self.outputs = False
        self.arm_state = "idle"
        return self.status()

    def prepare_recording_phase(self):
        self.prepare_calls += 1
        self.set_arm_state("pause_all")

    def apply_recording_phase(self, phase, episode_id):
        self.phase = phase

    def confirm_recording_phase(self, **kwargs):
        return None

    def emergency_stop(self, reason="estop"):
        return self.set_arm_state("estop")


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(ros_mobile, "UPPER_EXPORTS", tmp_path / "exports")
    facade = FakeFacade()
    car = InMemoryRecordingCarClient()
    recording = HybridRecordingController(facade, car, background=False)
    facade.recording = recording
    sync_calls = []
    export_calls = []

    def sync_fn(session_id):
        sync_calls.append(session_id)
        return {"session_id": session_id, "status": "raw_ready",
                "path": str(tmp_path / "sessions" / session_id),
                "manifest_sha256": "abc"}

    def export_spawn(session_id, include_failed, simulated_offline):
        export_calls.append((session_id, include_failed, simulated_offline))
        return {"job_id": f"job-{len(export_calls)}"}

    def export_status(job_id):
        return {"status": "completed",
                "report": {"repo_id": "lelab/test-dataset",
                           "dataset_root": str(tmp_path / "ds"),
                           "total_episodes": 2, "total_frames": 40}}

    orch = CaptureOrchestrator(
        facade, recording, collection_root=tmp_path,
        sync_fn=sync_fn, export_spawn=export_spawn, export_status=export_status,
        dataset_meta=None, background=False)
    return {"facade": facade, "car": car, "recording": recording, "orch": orch,
            "tmp": tmp_path, "sync_calls": sync_calls, "export_calls": export_calls}


def call(orch, action, **payload):
    return orch.handle(action, {"request_id": uuid.uuid4().hex, **payload})


def ok(result):
    assert result["state"] == "completed", result.get("error")
    return result


def open_task(rig, name="two-episode-demo"):
    ok(call(rig["orch"], "acquire", owner="tester"))
    return ok(call(rig["orch"], "open_dataset", name=name,
                   task="pick and place the cube", fps=14, target_episodes=2))


def record_one(rig, result="unknown", decision="save"):
    started = ok(call(rig["orch"], "start_episode"))
    assert rig["orch"].status()["recording_active"] is True
    ok(call(rig["orch"], "mark_grasp"))
    ended = ok(call(rig["orch"], "end_episode", result=result))
    assert rig["orch"].status()["recording_active"] is False
    assert rig["orch"].status()["operator_stage"] == "review"
    attempt_id = ended["result"]["attempt_id"]
    ok(call(rig["orch"], "review", attempt_id=attempt_id, decision=decision))
    ok(call(rig["orch"], "next_episode"))
    return attempt_id


def test_two_episode_full_flow(rig):
    orch = rig["orch"]
    open_task(rig)
    status = orch.status()
    assert status["operator_stage"] == "dataset_idle"
    assert status["recording_active"] is False
    first = record_one(rig)
    second = record_one(rig, result="success")
    assert first != second
    task = orch.status()["task"]
    assert task["saved_count"] == 2
    closed = ok(call(orch, "close_dataset"))
    assert closed["result"]["closed"] is True
    final = orch.status()
    assert final["operator_stage"] == "done"
    assert final["task"]["pipeline"]["stage"] == "exported"
    assert len(rig["sync_calls"]) == 1
    assert len(rig["export_calls"]) == 1
    review = json.loads((ros_mobile.UPPER_EXPORTS / task["car_recording_id"]
                         / "capture_review.json").read_text())
    assert review["dispositions"] == {first: "saved", second: "saved"}
    assert review["clock_epoch"]
    assert review["session_epoch"] == task["session_epoch"]


def test_gamepad_drive_requires_observe_and_closes_raw_only(rig):
    orch = rig["orch"]
    rig["facade"].frame_process = {
        "base_gamepad": {"available": True, "data_identity": "simulated_offline"},
    }
    open_task(rig)
    assert orch.status()["task"]["base_capture_raw_only"] is True
    ok(call(orch, "start_episode"))
    assert orch.status()["attempt"]["phase"] == "drive"
    assert call(orch, "mark_grasp")["state"] == "failed"
    ok(call(orch, "mark_observe"))
    assert orch.status()["attempt"]["phase"] == "observe"
    ok(call(orch, "mark_grasp"))
    ended = ok(call(orch, "end_episode", result="unknown"))
    ok(call(orch, "review", attempt_id=ended["result"]["attempt_id"], decision="save"))
    ok(call(orch, "close_dataset"))
    assert orch.status()["task"]["pipeline"]["stage"] == "raw_synced"
    assert len(rig["sync_calls"]) == 1
    assert rig["export_calls"] == []


def test_discard_and_rerecord_keep_old_attempts(rig):
    orch = rig["orch"]
    open_task(rig)
    started = ok(call(orch, "start_episode"))["result"]["attempt_id"]
    ok(call(orch, "mark_grasp"))
    ok(call(orch, "end_episode"))
    ok(call(orch, "review", attempt_id=started, decision="discard"))
    assert orch.status()["task"]["saved_count"] == 0
    ok(call(orch, "next_episode"))
    rerecorded = ok(call(orch, "start_episode"))["result"]["attempt_id"]
    assert rerecorded != started
    ok(call(orch, "mark_grasp"))
    ok(call(orch, "end_episode"))
    ok(call(orch, "review", attempt_id=rerecorded, decision="rerecord"))
    ok(call(orch, "next_episode"))
    third = ok(call(orch, "start_episode"))["result"]["attempt_id"]
    ok(call(orch, "mark_grasp"))
    ok(call(orch, "end_episode"))
    ok(call(orch, "review", attempt_id=third, decision="save"))
    ok(call(orch, "next_episode"))
    task = orch.status()["task"]
    assert task["saved_count"] == 1
    states = {a["attempt_id"]: a["state"] for a in task["attempts"]}
    assert states == {started: "discarded", rerecorded: "rerecorded", third: "saved"}
    ok(call(orch, "close_dataset"))
    review = json.loads((ros_mobile.UPPER_EXPORTS / task["car_recording_id"]
                         / "capture_review.json").read_text())
    assert review["dispositions"][started] == "discarded"
    assert review["dispositions"][third] == "saved"
    journal = (rig["tmp"] / "upper" / "capture" / task["task_id"]
               / "journal.jsonl").read_text()
    assert "attempt_discard" in journal and "attempt_rerecord" in journal


def test_duplicate_request_id_never_duplicates_episode_or_save(rig):
    orch = rig["orch"]
    open_task(rig)
    request_id = uuid.uuid4().hex
    first = orch.handle("start_episode", {"request_id": request_id})
    assert first["state"] == "completed"
    replay = orch.handle("start_episode", {"request_id": request_id})
    assert replay["replayed"] is True
    assert replay["result"]["attempt_id"] == first["result"]["attempt_id"]
    assert len(orch.status()["task"]["attempts"]) == 1
    ok(call(orch, "mark_grasp"))
    ended = ok(call(orch, "end_episode"))["result"]["attempt_id"]
    save_id = uuid.uuid4().hex
    ok(orch.handle("review", {"request_id": save_id, "attempt_id": ended,
                              "decision": "save"}))
    again = orch.handle("review", {"request_id": save_id, "attempt_id": ended,
                                   "decision": "save"})
    assert again["replayed"] is True
    assert orch.status()["task"]["saved_count"] == 1
    # A different request id for the same decided attempt is also a no-op.
    ok(call(orch, "review", attempt_id=ended, decision="save"))
    assert orch.status()["task"]["saved_count"] == 1


def test_session_generation_change_interrupts_and_never_inherits_rec(rig):
    orch = rig["orch"]
    facade = rig["facade"]
    open_task(rig)
    ok(call(orch, "start_episode"))
    assert orch.status()["recording_active"] is True
    old_epoch = facade.session_epoch
    facade.release_session("lease_lost")
    facade.acquire_session({"owner": "tester"})
    assert facade.session_epoch != old_epoch
    status = orch.status()
    assert status["recording_active"] is False
    assert status["operator_stage"] == "interrupted"
    assert status["attempt"]["state"] == "interrupted"
    assert status["attempt"]["interrupt_reason"] == "control_session_changed"
    # The new session cannot start an episode on the old task.
    refused = call(orch, "start_episode")
    assert refused["state"] == "failed"
    assert refused["error"]["reason"] in ("control_session_changed", "dataset_not_open")
    recovered = ok(call(orch, "recover"))
    assert recovered["result"]["recovered"] is True
    car_paths = [path for path, _ in rig["car"].calls]
    assert "/recording/end_episode" in car_paths
    assert "/recording/stop_session" in car_paths
    assert orch.status()["operator_stage"] in ("pipeline", "done")
    # A fresh dataset opens under the new control session.
    ok(call(orch, "open_dataset", name="after-recovery", task="retry the task"))
    assert orch.status()["operator_stage"] == "dataset_idle"


def test_service_restart_recovers_from_disk_without_inherited_rec(rig):
    orch = rig["orch"]
    open_task(rig)
    ok(call(orch, "start_episode"))
    facade2 = FakeFacade()
    recording2 = HybridRecordingController(facade2, InMemoryRecordingCarClient(),
                                           background=False)
    reborn = CaptureOrchestrator(facade2, recording2, collection_root=rig["tmp"],
                                 background=False)
    status = reborn.status()
    assert status["recording_active"] is False
    assert status["operator_stage"] == "no_control"
    ok(call(reborn, "acquire", owner="tester"))
    status = reborn.status()
    assert status["operator_stage"] == "interrupted"
    assert status["attempt"]["interrupt_reason"] == "service_restarted"
    assert status["recording_active"] is False


def test_estop_without_recording_does_not_wedge_next_dataset(rig):
    orch = rig["orch"]
    facade = rig["facade"]
    ok(call(orch, "acquire", owner="tester"))
    rig["recording"].estop("operator")
    assert rig["recording"].status()["status"] == "invalid"
    facade.estop = False
    facade.state = "ready"
    opened = ok(call(orch, "open_dataset", name="after-estop", task="still works"))
    assert opened["result"]["task_id"]
    assert rig["recording"].status()["status"] == "active"
    assert rig["recording"].status()["archived_runs"]


def test_pause_mid_episode_shows_not_recording_and_resume_is_explicit(rig):
    orch = rig["orch"]
    facade = rig["facade"]
    open_task(rig)
    ok(call(orch, "start_episode"))
    facade.set_arm_state("pause_all")
    status = orch.status()
    assert status["recording_active"] is False
    assert status["operator_stage"] == "paused"
    # Marking grasp while paused must not sneak motion back on.
    ok(call(orch, "mark_grasp"))
    assert facade.outputs is False
    assert orch.status()["operator_stage"] == "paused"
    ok(call(orch, "resume_follow"))
    assert orch.status()["recording_active"] is True
    assert orch.status()["attempt"]["phase"] == "grasp"


def test_camera_gate_blocks_rec_before_attempt_exists(rig):
    orch = rig["orch"]
    open_task(rig)
    rig["facade"].video = {"available": True, "stale": True, "age_s": 3.0}
    refused = call(orch, "start_episode")
    assert refused["state"] == "failed"
    assert refused["error"]["reason"] == "camera_frame_stale"
    assert orch.status()["task"]["attempts"] == []
    assert orch.status()["recording_active"] is False


def test_follower_observation_limit_blocks_rec_before_attempt_exists(rig):
    orch = rig["orch"]
    open_task(rig)

    def validate():
        raise RuntimeError("frame_emitter_feedback_out_of_range:joint3:raw=-63.0:allowed=-50..1000")

    rig["facade"].validate_follow_readback = validate
    refused = call(orch, "start_episode")
    assert refused["state"] == "failed"
    assert refused["error"]["reason"].startswith("frame_emitter_feedback_out_of_range:joint3")
    assert orch.status()["task"]["attempts"] == []
    assert orch.status()["recording_active"] is False


def test_close_with_pending_review_stops_first_and_never_silent_discard(rig):
    orch = rig["orch"]
    open_task(rig)
    ok(call(orch, "start_episode"))
    ok(call(orch, "mark_grasp"))
    refused = call(orch, "close_dataset")
    assert refused["state"] == "failed"
    assert refused["error"]["reason"] == "review_pending"
    assert rig["facade"].outputs is False
    status = orch.status()
    assert status["operator_stage"] == "review"
    assert status["recording_active"] is False
    attempt_id = status["attempt"]["attempt_id"]
    ok(call(orch, "review", attempt_id=attempt_id, decision="save"))
    ok(call(orch, "close_dataset"))
    assert orch.status()["operator_stage"] == "done"
    assert orch.status()["task"]["saved_count"] == 1


def test_append_compatibility_is_checked(rig):
    orch = rig["orch"]
    open_task(rig)
    ok(call(orch, "start_episode"))
    ok(call(orch, "mark_grasp"))
    ended = ok(call(orch, "end_episode"))["result"]["attempt_id"]
    ok(call(orch, "review", attempt_id=ended, decision="save"))
    ok(call(orch, "next_episode"))
    ok(call(orch, "close_dataset"))

    good_meta = {"robot_type": "lelab_mobile_arm_gripper_phase1", "fps": 14,
                 "total_episodes": 1, "data_identity": "hardware",
                 "features": {
                     "observation.state": {"dtype": "float32", "shape": [6],
                                           "names": [f"{j}.position" for j in ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]],
                                           "info": {"units": ["rad"] * 6}},
                     "action": {"dtype": "float32", "shape": [6],
                                "names": [f"{j}.target" for j in ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]],
                                "info": {"units": ["rad"] * 6}},
                     "observation.images.wrist_real": {"dtype": "video", "shape": [400, 640, 3]}}}
    orch.dataset_meta = lambda repo_id: dict(good_meta)
    ok(call(orch, "acquire", owner="tester"))
    opened = ok(call(orch, "open_dataset", name="append-run", task="same task",
                     mode="append", repo_id="lelab/test-dataset"))
    assert opened["result"]["task_id"]
    bad_meta = dict(good_meta)
    bad_meta["features"] = {**good_meta["features"]}
    # reversed joint order on the action side is an incompatibility the pre-check
    # must catch before a doomed session is opened
    bad_meta["features"]["action"] = {
        **good_meta["features"]["action"],
        "names": [f"{j}.target" for j in ["joint2", "joint1", "joint3", "joint4", "joint5", "r_joint"]]}
    orch2_facade = FakeFacade()
    orch2 = CaptureOrchestrator(orch2_facade, HybridRecordingController(
        orch2_facade, InMemoryRecordingCarClient(), background=False),
        collection_root=rig["tmp"] / "other", dataset_meta=lambda _: bad_meta,
        background=False)
    ok(call(orch2, "acquire", owner="tester"))
    refused = call(orch2, "open_dataset", name="bad-append", task="nope",
                   mode="append", repo_id="lelab/test-dataset")
    assert refused["state"] == "failed"
    assert refused["error"]["reason"] == "append_incompatible"


def test_failed_start_keeps_evidence_and_single_reason(rig):
    orch = rig["orch"]
    open_task(rig)
    rig["facade"].feedback = {"valid": False, "trusted": False,
                              "trusted_reasons": ["feedback_stale"]}
    refused = call(orch, "start_episode")
    assert refused["state"] == "failed"
    assert refused["error"]["reason"] == "feedback_untrusted"
    assert orch.status()["task"]["attempts"] == []
    assert rig["facade"].outputs is False


def test_close_dataset_tolerates_already_closed_car_session(rig):
    """A stop whose response was lost closed the bag; retry must not wedge the task."""
    orch = rig["orch"]
    open_task(rig)
    from lelab.hybrid_recording import RecordingError

    def stop_then_report_no_session(action, payload):
        if action == "stop_session":
            raise RecordingError("car_rejected", "recording/stop_session: no session to stop",
                                 status_code=502)
        return rig["recording"].dispatch(action, payload)

    real_dispatch = rig["recording"].dispatch
    rig["recording"].dispatch = stop_then_report_no_session
    try:
        # Force the controller to be ACTIVE so close reaches the stop_session branch.
        rig["recording"].status_value = "active"
        closed = call(orch, "close_dataset")
    finally:
        rig["recording"].dispatch = real_dispatch
    assert closed["state"] == "completed", closed.get("error")
    assert closed["result"]["closed"] is True
    assert orch.status()["operator_stage"] == "done"
