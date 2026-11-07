#!/usr/bin/env python3
"""OFFLINE ONLY: inject faults inside one production facade session and read the result.

Everything below runs against the isolated Docker agent with the simulated leader,
board and camera. The facade, the frame link, the recording controller and the agent
are the production objects; nothing here is a stand-in for the transport.

For every fault the same four things are asked, in this order:

1. does the upper host stop submitting frames;
2. does the car stop admitting and stop publishing;
3. does the pending queue drain instead of banking up;
4. does recovery refuse to resume following by itself.

The board counter is the simulated servo accepting a target; a frozen or dead board is
not a physical stop, and nothing here proves the real arm holds its pose or that the
emergency stop reaches the hardware. The freshness window (0.5 s) and the frame TTL
(200 ms) are read as they are configured, never widened for a case to pass.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import struct
import subprocess
import threading
import time
import traceback

from facade_smoke import (AGENT, STATE, STREAM_PORT, SyntheticReader, SyntheticUpper,
                          WORK, car_status, calibration_assets_fixture,
                          FrameCarControlLease, HybridCalibrationStore, StreamHardwareObserver,
                          FrameStreamHardwareFacade, FrameStreamDriver,
                          configure_hybrid_facade)
from lelab.hybrid_frame_link import FrameLink
from lelab.hybrid_hardware import HardwareCompositionError, _http_json
from lelab.hybrid_recording import HttpCarClient, HybridRecordingController

CONTAINER = os.environ.get("LROS_CONTAINER", "lelab-humble-facade-integration")
CAR_DOC = STATE / "car_control_status.json"
BOARD_DOC = STATE / "sim_board_owner.json"
PID_DIR = STATE.parent
FRESHNESS_S = 0.5
QUIET_S = 2.0
CASES = ("leader_stall", "feedback_stale", "car_status_stale", "licence_revoked",
         "socket_reset", "backpressure_half_open", "control_process_exit",
         "estop_repeat", "estop_during_bag_close", "retired_generation_frame")


def read_doc(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def container_signal(action: str, name: str) -> bool:
    """Signal one process of this task's own container, by the pid it recorded."""
    pid_file = PID_DIR / f"{name}.pid"
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        raise RuntimeError(f"no_pid_recorded_for:{name}")
    return subprocess.run(["sg", "docker", "-c",
                           f"docker exec {CONTAINER} kill -{action} {pid}"],
                          capture_output=True, text=True, timeout=60).returncode == 0


def container_ops(command: str, extra: str = "") -> dict:
    # The same initialisation bin/docker uses. The inner command is single-quoted on
    # purpose: with double quotes the outer shell expands $PYTHONPATH away and the
    # restarted process dies on "No module named 'rclpy'".
    init = ("source /opt/ros/humble/setup.bash; "
            "[ -f /work/out/local_humble/docker/ws/install/setup.bash ] && "
            "source /work/out/local_humble/docker/ws/install/setup.bash; "
            "export PYTHONPATH=/work/lelab/ros2_ws/src/lelab_mobile_agent:$PYTHONPATH "
            "CAR_PKG=/work/lelab/ros2_ws/src/lelab_mobile_agent; ")
    out = subprocess.run(
        ["sg", "docker", "-c",
         f"docker exec {CONTAINER} bash -lc '"
         f"{init}python3 /work/lelab/tools/ros2_collection/local_ros2/offline/"
         f"container_ops.py {command} --state-dir /work/out/local_humble/docker/state "
         f"{extra}'"],
        capture_output=True, text=True, timeout=180)
    line = [item for item in (out.stdout or "").splitlines() if item.startswith("{")]
    return json.loads(line[-1]) if line else {"raw": (out.stdout or out.stderr)[:300]}


def control_ticks() -> int:
    return int((read_doc(CAR_DOC).get("counters") or {}).get("ticks") or 0)


class StallableReader(SyntheticReader):
    """A leader device that stops delivering: the same frozen sample, with a growing age.

    The read counter must stop too, because the follow loop keys new submissions off it;
    a reader that keeps counting while nothing changes is a different fault than the one
    named here.
    """

    def __init__(self) -> None:
        super().__init__()
        self.stalled_at: float | None = None
        self.frozen: dict | None = None

    def snapshot(self):
        if self.stalled_at is None:
            self.frozen = super().snapshot()
            return self.frozen
        sample = dict(self.frozen)
        sample["age_s"] = time.monotonic() - self.stalled_at
        sample["stalled"] = True
        return sample


class StallableUpper(SyntheticUpper):
    def _open_reader(self):
        self.reader = StallableReader()
        return self.reader


class Matrix:
    def __init__(self) -> None:
        self.report: dict = {"agent": AGENT, "container": CONTAINER,
                             "freshness_window_s": FRESHNESS_S, "frame_ttl_policy": "unchanged",
                             "data_identity": "simulated_offline", "training_eligible": False,
                             "cases": [], "checks": []}
        self.identity: dict | None = None
        self.recording: HybridRecordingController | None = None
        self._needs_new_session = False

    # -- plumbing ----------------------------------------------------------
    def check(self, name: str, ok, detail=None) -> None:
        self.report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}: "
              f"{json.dumps(detail, ensure_ascii=False, default=str)[:260]}", flush=True)
        if not ok:
            raise RuntimeError(name)

    def car_counters(self) -> dict:
        document = read_doc(CAR_DOC)
        counters = document.get("counters") or {}
        admission = document.get("admission") or {}
        return {"emitted": counters.get("frames_emitted"),
                "received": counters.get("frames_received"),
                "refused": counters.get("frames_refused"),
                "expired": counters.get("frames_expired"),
                "expired_in_queue": (admission.get("counters") or {}).get("expired_in_queue"),
                "queue_depth": admission.get("queue_depth"),
                "board_outputs": read_doc(BOARD_DOC).get("outputs_received")}

    def link_counts(self) -> dict:
        return (((self.facade.status().get("frame_link") or {}).get("counts")) or {})

    def submitted(self) -> int:
        """Frames the facade put on the wire in this session.

        The link's own counters reset when a fault drops the link, which is exactly when
        they are read, so the facade's cumulative counter is the honest witness here.
        """
        return int(getattr(self.facade, "_follow_commands", 0))

    def quiet(self, wait_s: float = QUIET_S) -> dict:
        """Nothing may move for `wait_s`: no submit, no admission, no emission."""
        before_car, before_sent = self.car_counters(), self.submitted()
        time.sleep(wait_s)
        after_car, after_sent = self.car_counters(), self.submitted()
        delta = {key: ((after_car[key] or 0) - (before_car[key] or 0)) for key in before_car}
        delta["submitted"] = after_sent - before_sent
        return {"before": before_car, "after": after_car, "delta": delta,
                "window_s": wait_s,
                "link_counts_after": self.link_counts()}

    def expect_stopped(self, case: str, wait_s: float = QUIET_S,
                       settle_s: float = 1.5) -> dict:
        """After the in-flight frames are consumed, nothing may keep moving.

        The settle window is measured and reported rather than ignored: the car can still
        emit what was already on the socket, and the bounded hold is a real behaviour of
        the control process, not a pass/fail to paper over.
        """
        inflight = self.quiet(settle_s)
        view = self.quiet(wait_s)
        view["inflight_window"] = inflight
        delta = view["delta"]
        self.check(f"{case}_upper_stops_submitting", delta["submitted"] == 0, delta)
        self.check(f"{case}_car_stops_admitting",
                   delta["received"] == 0 and delta["emitted"] == 0,
                   {"quiet_window": delta, "frames_still_in_flight": inflight["delta"]})
        self.check(f"{case}_queue_drained", (view["after"].get("queue_depth") or 0) == 0,
                   {"queue_depth": view["after"].get("queue_depth"),
                    "expired_in_queue": delta.get("expired_in_queue")})
        self.check(f"{case}_no_board_output", delta["board_outputs"] == 0, delta)
        return view

    def engage(self) -> dict:
        for action in ("preview", "align", "arm", "engage"):
            call = (self.facade.preview if action == "preview" else self.facade.set_arm_state)
            call({"action": action, **self.identity} if action != "preview" else self.identity)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            state = ((self.facade.status().get("channels") or {}).get("arm") or {}).get("state")
            if state == "following":
                return {"arm": state}
            time.sleep(0.1)
        raise RuntimeError("engage_did_not_reach_following")

    def moving(self, wait_s: float = 1.5) -> dict:
        before_car, before_sent = self.car_counters(), self.submitted()
        time.sleep(wait_s)
        after_car, after_sent = self.car_counters(), self.submitted()
        return {"emitted_delta": (after_car["emitted"] or 0) - (before_car["emitted"] or 0),
                "submitted_delta": after_sent - before_sent,
                "arm": ((self.facade.status().get("channels") or {}).get("arm") or {}).get("state")}

    def expect_not_auto_engaging(self, case: str, wait_s: float = 3.0) -> None:
        view = self.moving(wait_s)
        self.check(f"{case}_does_not_resume_by_itself",
                   view["emitted_delta"] == 0 and view["arm"] != "following", view)

    def resume(self, name: str) -> dict:
        """Bring motion back the way the facade allows, and record which path worked."""
        try:
            self.engage()
            path = "re-engage"
        except Exception as exc:  # noqa: BLE001 - a faulted session needs a new one
            self.report.setdefault("resume_failures", {})[name] = (
                f"{type(exc).__name__}:{str(exc)[:120]}")
            self.open_session(name, verify_motion=False)
            path = "new_session"
        view = self.moving(1.5)
        self.check(f"{name}_motion_resumes", view["emitted_delta"] > 0,
                   {"recovery_path": path, **view})
        return {"recovery_path": path, **view}

    def case_header(self, name: str, inject: str) -> None:
        self.report["cases"].append({"name": name, "injection": inject,
                                     "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                  time.gmtime())})
        print(f"--- {name}: {inject}", flush=True)

    def case_footer(self) -> None:
        if not self.report["cases"]:
            return
        self.report["cases"][-1]["after"] = {
            "facade_state": self.facade.state, "last_error": self.facade.last_error,
            "car_reason": getattr(self, "car_lease", None) and self.car_lease.last_reason,
            "counters": self.car_counters()}

    # -- the session every case shares --------------------------------------
    def build(self) -> None:
        status = car_status()
        if (status.get("control") or {}).get("active"):
            raise RuntimeError("offline_agent_already_has_a_lease")
        store = HybridCalibrationStore(WORK / "calibration")
        assets = calibration_assets_fixture(measured_at_utc="2026-11-04T00:00:00Z")
        ids = {kind: assets[kind]["identity"] for kind in ("leader", "follower", "mapping")}
        self.upper = StallableUpper(agent_url=AGENT, store=store, leader_identity=ids["leader"])
        self.car_lease = FrameCarControlLease(
            AGENT, expected_profile="hardware_hybrid_manual_v1_offline6d")
        self.observer = StreamHardwareObserver(
            upper=self.upper, agent_url=AGENT, store=store, leader_identity=ids["leader"],
            follower_identity=ids["follower"], mapping_identity=ids["mapping"],
            simulated_source=True)
        self.car_lease.observer = self.observer
        self.facade = FrameStreamHardwareFacade(upper=self.upper, car=self.car_lease,
                                                observer=self.observer, calibration_store=store)
        self.driver = FrameStreamDriver(self.car_lease, self.observer, AGENT,
                                        host="127.0.0.1", port=STREAM_PORT)
        self.facade.bind_driver(self.driver)
        self.facade.simulated_context = {"simulated": True, "data_identity": "simulated_offline",
                                         "training_eligible": False}
        self.facade = configure_hybrid_facade(facade=self.facade, replace=True)
        client = HttpCarClient(lambda path, body: _http_json(
            AGENT, path, method="POST", body=body, token=self.car_lease.token, timeout=10.0))
        self.recording = HybridRecordingController(self.facade, client, offline_6d=True)
        self.facade.bind_recording_controller(self.recording, replace=True)
        self.bundle_hash = self.observer.calibration_readiness()["bundle_hash"]

    def stall_leader(self, stalled_at: float | None) -> None:
        """Freeze or resume the leader reader; it only exists once a session opened it."""
        reader = getattr(self.upper, "reader", None)
        if reader is not None:
            reader.stalled_at = stalled_at

    def clear_car_estop(self, label: str) -> None:
        """Clear a latch the previous case left on the car, the way the page's button does.

        Recorded, never assumed: if the car still refuses to enable afterwards the next
        case fails with its own evidence instead of this one's residue.
        """
        document = read_doc(CAR_DOC)
        if not document.get("latched_fault") and not (
                car_status().get("control") or {}).get("estop_latched"):
            return
        try:
            self.report.setdefault("estop_clears", {})[label] = _http_json(
                AGENT, "/control/clear_estop", method="POST", body={}, timeout=8.0)
        except Exception as exc:  # noqa: BLE001 - reported, then the case decides
            self.report.setdefault("estop_clears", {})[label] = (
                f"{type(exc).__name__}:{str(exc)[:140]}")

    def car_latched(self) -> dict:
        """The two latch fields, named as they are: the emitter's own fault and the agent's.

        In frame mode the emergency stop is executed by the control process, so its
        ``latched_fault`` is the witness; the agent-level ``estop_latched`` belongs to the
        legacy HTTP path and stays false here. Reporting both is what keeps the claim
        about the mechanism honest.
        """
        return {"control_process_latched_fault": read_doc(CAR_DOC).get("latched_fault"),
                "control_process_reengage_required":
                    read_doc(CAR_DOC).get("reengage_required"),
                "agent_estop_latched": (car_status().get("control") or {}).get("estop_latched")}

    def _release_if_held(self, label: str) -> None:
        if self.facade.session_id:
            try:
                # Use the facade's own live identity: a fault or an estop can move the
                # epoch, and releasing with a stale one leaves the session registered.
                self.facade.release_session("fault_matrix_recover", **{
                    "session_id": self.facade.session_id,
                    "session_epoch": self.facade.session_epoch})
            except Exception as exc:  # noqa: BLE001 - the next acquire is the real test
                self.report.setdefault("release_notes", {})[label] = str(exc)[:160]
        self.identity = None

    def _clear_estop_if_latched(self, label: str) -> None:
        """Press the same 清除急停 the operator can press, before asking for a session."""
        latched = bool(getattr(self.facade, "estop_latched", False)
                       or self.facade.state == "estop")
        if not latched:
            return
        try:
            self.facade.set_arm_state({"action": "clear_estop"})
            self.report.setdefault("estop_clears", {})[label] = "cleared"
        except Exception as exc:  # noqa: BLE001 - recorded, then the case decides
            self.report.setdefault("estop_clears", {})[label] = (
                f"{type(exc).__name__}:{str(exc)[:140]}")

    def open_session(self, label: str = "initial", verify_motion: bool = True) -> dict:
        """Take a session the way the page does, releasing any faulted one first.

        A stream fault runs ``_follow_fail``, which sets state=fault and stops the leases:
        recovery is a new session, not a re-engage. The matrix records that instead of
        papering over it, because it is what the operator actually has to do. An
        emergency stop latches the upper session itself, and every route but estop is
        refused while it is latched, so the clear comes before the acquire.
        """
        self.stall_leader(None)
        self.clear_car_estop(label)
        acquired = None
        for attempt in range(1, 4):
            self._release_if_held(label)
            self._clear_estop_if_latched(label)
            try:
                acquired = self.facade.acquire_session({"owner": "offline-fault-matrix"})
            except Exception as exc:  # noqa: BLE001 - only the recoverable refusals
                text = str(exc)
                if "prerequisites_missing" not in text or attempt == 3:
                    raise
                self.report.setdefault("acquire_retries", {})[label] = f"{attempt}: {text[:140]}"
                time.sleep(2.0)
                continue
            if self.facade.state == "ready":
                break
            self.report.setdefault("acquire_retries", {})[label] = (
                f"{attempt}: state={self.facade.state} after acquire")
            if attempt == 3:
                raise RuntimeError(f"session_never_became_ready:{self.facade.state}")
        self.identity = {"session_id": acquired["session_id"],
                         "session_epoch": acquired["session_epoch"]}
        self.engage()
        if not verify_motion:
            return {"state": self.facade.state}
        view = self.moving(1.5)
        self.check(f"{label}_session_follows", view["emitted_delta"] > 0, view)
        return view

    # -- cases --------------------------------------------------------------
    def case_leader_stall(self) -> None:
        self.case_header("leader_stall", "the leader reader stops delivering new samples")
        self.stall_leader(time.monotonic())
        self.expect_stopped("leader_stall")
        self.check("leader_stall_reported_as_stale",
                   (self.observer.snapshot().get("leader") or {}).get("reason")
                   == "leader_cached_sample_stale",
                   (self.observer.snapshot().get("leader") or {}).get("reason"))
        self.stall_leader(None)
        self.expect_not_auto_engaging("leader_stall")
        self.report["cases"][-1]["recovery"] = self.resume("leader_stall")

    def case_feedback_stale(self) -> None:
        self.case_header("feedback_stale", "SIGSTOP the simulated board: feedback ages out")
        container_signal("STOP", "board")
        try:
            self.expect_stopped("feedback_stale")
            self.check("feedback_stale_flagged",
                       self.facade.status()["feedback"]["valid"] is not True,
                       self.facade.status()["feedback"]["reason"])
        finally:
            container_signal("CONT", "board")
        self.expect_not_auto_engaging("feedback_stale")
        self.report["cases"][-1]["recovery"] = self.resume("feedback_stale")

    def case_car_status_stale(self) -> None:
        self.case_header("car_status_stale", "SIGSTOP the agent: /status stops answering")
        container_signal("STOP", "agent")
        try:
            time.sleep(2.0)
            self.expect_stopped("car_status_stale")
            reason = str(self.car_lease.last_reason or "")
            self.check("car_status_stale_explains_itself",
                       reason.startswith("car_status_read_failed"), reason)
            self.check("car_status_stale_is_fail_closed",
                       self.car_lease.valid(self.car_lease.token) is False,
                       {"last_reason": reason})
        finally:
            container_signal("CONT", "agent")
        time.sleep(1.0)
        self.check("agent_answers_again_after_continue",
                   (car_status().get("control") or {}).get("active") is True, {})

    def case_licence_revoked(self) -> None:
        self.case_header("licence_revoked",
                         "a second client releases the car lease out from under the facade")
        _http_json(AGENT, "/control/release", method="POST", body={},
                   token=self.car_lease.token, timeout=6.0)
        self.expect_stopped("licence_revoked")
        self.check("licence_revoked_reported",
                   self.car_lease.valid(self.car_lease.token) is False,
                   {"last_reason": self.car_lease.last_reason})
        self.expect_not_auto_engaging("licence_revoked")
        # The lease is gone: the session has to be rebuilt before anything may move.
        self.report["cases"][-1]["note"] = ("no re-engage attempted: the car lease itself "
                                            "was released, so a new session is required")
        self._needs_new_session = True

    def case_socket_reset(self) -> None:
        self.case_header("socket_reset", "RST the frame socket from the upper side (SO_LINGER 0)")
        link = self.driver.link
        if link is None:
            raise RuntimeError("no_frame_link_to_reset")
        sock = link.client._sock
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()
        time.sleep(1.0)
        self.expect_stopped("socket_reset")
        self.check("socket_reset_car_sees_no_client",
                   read_doc(CAR_DOC).get("stream", {}).get("client_connected") is not True,
                   read_doc(CAR_DOC).get("stream", {}))
        self.expect_not_auto_engaging("socket_reset")
        self.report["cases"][-1]["recovery"] = self.resume("socket_reset")

    def case_backpressure_half_open(self) -> None:
        self.case_header("backpressure_half_open",
                         "SIGSTOP the control process: the socket stays open, nobody reads")
        container_signal("STOP", "control")
        try:
            time.sleep(3.0)
            frozen = self.car_counters()
            self.check("half_open_car_published_nothing",
                       frozen["emitted"] == self.car_counters()["emitted"], frozen)
        finally:
            resumed_at = self.car_counters()["emitted"]
            container_signal("CONT", "control")
        time.sleep(1.5)
        after = self.car_counters()
        burst = (after["emitted"] or 0) - (resumed_at or 0)
        self.check("half_open_does_not_release_a_burst_of_old_frames", burst <= 20,
                   {"frames_published_after_resume": burst, "window_s": 1.5,
                    "expired_in_queue_delta": (after["expired_in_queue"] or 0)
                    - (frozen["expired_in_queue"] or 0), "ttl_ms": 200})
        self.expect_not_auto_engaging("half_open")

    def case_control_process_exit(self) -> None:
        self.case_header("control_process_exit", "stop the control process, then start it again")
        stopped = container_ops("stop-control")
        self.check("control_process_stop_confirmed", stopped.get("stopped") is True, stopped)
        time.sleep(2.0)
        self.expect_stopped("control_process_exit")
        self.check("agent_notices_the_control_process_is_gone",
                   (car_status().get("licence") or {}).get("frame_outputs_enabled") is not True,
                   (car_status().get("licence") or {}))
        started = container_ops(
            "start-control",
            "--module lelab_mobile_agent.car_control_process "
            f"--arg=--profile --arg=/work/lelab/ros2_ws/src/lelab_mobile_agent/config/"
            f"hardware-hybrid-manual-v1-offline6d.json "
            f"--arg=--stream-port --arg={STREAM_PORT} "
            "--arg=--licence-socket --arg=/work/out/local_humble/docker/state/licence.sock "
            "--arg=--status-path --arg=/work/out/local_humble/docker/state/"
            "car_control_status.json --arg=--control-hz --arg=10 --arg=--window-ms --arg=250 "
            f"--arg=--bundle-hash --arg={self.bundle_hash}")
        self.check("control_process_restarted", bool(started.get("pid")), started)
        # A start command that returns a pid is not evidence: a process that dies on
        # import looks identical for the first second. The counters also restart with
        # the process, so liveness is two reads of the new one, not a growth against the
        # old total.
        time.sleep(2.0)
        first = control_ticks()
        time.sleep(2.0)
        self.check("control_process_running_after_restart", control_ticks() > first,
                   {"ticks_first": first, "ticks_second": control_ticks(),
                    "started": started})
        self.expect_not_auto_engaging("control_process_exit")
        self.report["cases"][-1]["recovery"] = self.resume("control_process_exit")

    def estop(self) -> dict:
        """The same entry the page's emergency-stop button reaches: the route calls this."""
        return self.recording.estop("offline_fault_matrix")

    def wait_car_stream_dropped(self, limit_s: float = 5.0) -> dict:
        """The car notices a lost socket on its next read, not the instant we close it."""
        started = time.monotonic()
        seen = (car_status().get("licence") or {}).get("frame_stream_connected")
        while seen is True and time.monotonic() - started < limit_s:
            time.sleep(0.2)
            seen = (car_status().get("licence") or {}).get("frame_stream_connected")
        return {"frame_stream_connected": seen,
                "detected_after_s": round(time.monotonic() - started, 2),
                "agent_estop_latched": (car_status().get("control") or {}).get("estop_latched")}

    def case_estop_repeat(self) -> None:
        self.case_header("estop_repeat", "two emergency stops and clears in the same session")
        for round_number in (1, 2):
            self.estop()
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline and self.facade.state != "estop":
                time.sleep(0.1)
            self.check(f"estop_repeat_{round_number}_latched", self.facade.state == "estop",
                       {"state": self.facade.state, "last_error": self.facade.last_error})
            self.check(f"estop_repeat_{round_number}_session_latched",
                       self.facade.estop_latched is True,
                       {"state": self.facade.state, "latched": self.facade.estop_latched})
            dropped = self.wait_car_stream_dropped()
            self.check(f"estop_repeat_{round_number}_car_stream_dropped",
                       dropped["frame_stream_connected"] is not True,
                       {**dropped,
                        "finding": ("frame-mode emergency stop aborts the stream and drops "
                                    "the car-side connection; it does NOT set the agent's "
                                    "estop_latched flag, which the legacy HTTP path sets")})
            self.expect_stopped(f"estop_repeat_{round_number}", 1.5)
            self.facade.set_arm_state({"action": "clear_estop", **self.identity})
            time.sleep(1.0)
            self.check(f"estop_repeat_{round_number}_clear_does_not_resume",
                       self.moving(2.0)["emitted_delta"] == 0, self.moving(0.1))
        self.report["cases"][-1]["recovery"] = self.resume("estop_repeat")

    def case_estop_during_bag_close(self) -> None:
        self.case_header("estop_during_bag_close",
                         "emergency stop while a real bag close holds the recorder lock")
        self.recording.dispatch("start_session", {**self.identity,
                               "profile": "hardware_hybrid_manual_v1_offline6d",
                               "task": "fault matrix"})
        self.recording.dispatch("start_episode", {**self.identity, "episode_id": "fault_ep_1"})
        self.recording.dispatch("set_phase", {**self.identity, "phase": "observe"})
        time.sleep(1.5)
        self.recording.dispatch("end_episode", {**self.identity, "success": "unknown"})
        close: dict = {}

        def stop_session() -> None:
            try:
                close["result"] = self.recording.dispatch(
                    "stop_session", {**self.identity, "reason": "fault_matrix_close"})
            except Exception as exc:  # noqa: BLE001 - the close is the subject
                close["error"] = f"{type(exc).__name__}:{str(exc)[:200]}"

        thread = threading.Thread(target=stop_session, daemon=True)
        thread.start()
        time.sleep(0.4)
        stopped_at = time.monotonic()
        self.estop()
        thread.join(30.0)
        estop_latency = round(time.monotonic() - stopped_at, 3)
        self.check("estop_during_close_returns", not thread.is_alive(), close)
        self.check("estop_during_close_applied", self.facade.state == "estop",
                   {"state": self.facade.state, "latency_s": estop_latency,
                    "close_error": close.get("error")})
        self.check("estop_during_close_is_not_a_lease_loss",
                   "lease_lost" not in str(self.facade.last_error),
                   {"last_error": self.facade.last_error, "latency_s": estop_latency,
                    "close_duration_s": close.get("duration_s")})
        self.check("estop_during_close_stops_the_car",
                   self.car_latched()["control_process_latched_fault"] is not None
                   or (car_status().get("licence") or {}).get("frame_stream_connected") is not True,
                   {"frame_stream_connected":
                        (car_status().get("licence") or {}).get("frame_stream_connected"),
                    "session_latched": self.facade.estop_latched,
                    "control_process": self.car_latched()})
        self.facade.set_arm_state({"action": "clear_estop", **self.identity})
        # The stop dropped the frame socket, so the next case must take a fresh session.
        self._needs_new_session = True

    def case_retired_generation_frame(self) -> None:
        self.case_header("retired_generation_frame",
                         "send on a socket whose stream epoch was retired by a newer client")
        old_link = self.driver.link
        if old_link is None:
            raise RuntimeError("no_live_link_to_compare")
        sent: list[dict] = []
        original = old_link.client.submit_frame

        def recording_submit(frame):
            sent.append(dict(frame))
            return original(frame)

        old_link.client.submit_frame = recording_submit  # type: ignore[method-assign]
        time.sleep(0.5)
        retired_epoch = old_link.stream_epoch
        old_client = old_link.client
        probe = FrameLink(host="127.0.0.1", port=STREAM_PORT, session_id=self.identity["session_id"],
                          session_epoch=self.identity["session_epoch"],
                          bundle_hash=self.bundle_hash, lease_token=self.car_lease.token)
        probe.attach_lease(self.car_lease.token)
        welcome = probe.engage()
        self.check("second_client_takes_a_new_generation",
                   welcome.get("stream_epoch") != retired_epoch,
                   {"retired": retired_epoch, "new": welcome.get("stream_epoch")})
        before = self.car_counters()
        reply = None
        if sent:
            stale = dict(sent[-1])
            stale["stream_epoch"] = retired_epoch
            stale["frame_seq"] = (stale.get("frame_seq", 0) or 0) + 1000
            try:
                old_client.submit_frame(stale)
                reply = old_client.recv(1.0)
            except Exception as exc:  # noqa: BLE001 - a dead socket is itself a refusal
                reply = {"raised": f"{type(exc).__name__}:{str(exc)[:120]}"}
        after = self.car_counters()
        self.check("retired_generation_frame_is_not_published",
                   (after["emitted"] or 0) - (before["emitted"] or 0) == 0,
                   {"reply": reply, "emitted_delta": (after["emitted"] or 0)
                    - (before["emitted"] or 0)})
        self.check("retired_generation_frame_is_counted_as_refused",
                   (after["refused"] or 0) - (before["refused"] or 0) >= 1 or reply is not None,
                   {"refused_delta": (after["refused"] or 0) - (before["refused"] or 0),
                    "reply": str(reply)[:200]})
        probe.client.close()

    # -- cleanup ------------------------------------------------------------
    def release(self) -> None:
        try:
            if self.recording is not None and self.recording.status_value in ("active", "invalid"):
                self.recording.dispatch("stop_session", {**self.identity,
                                                         "reason": "fault_matrix_cleanup"})
        except Exception as exc:  # noqa: BLE001
            self.report["recording_cleanup_error"] = f"{type(exc).__name__}:{str(exc)[:200]}"
        if self.identity and self.facade.session_id:
            try:
                self.facade.release_session("fault_matrix_finished", **self.identity)
            except Exception as exc:  # noqa: BLE001
                self.report["release_error"] = f"{type(exc).__name__}:{str(exc)[:200]}"
        # Leave the car unlatched: a matrix that ends with the safety latch engaged
        # would hand the next operator a stack whose state they cannot read.
        self.clear_car_estop("release")
        final = car_status()
        self.report["car_after_matrix"] = {
            "control_active": (final.get("control") or {}).get("active"),
            "estop_latched": (final.get("control") or {}).get("estop_latched"),
            "frame_outputs_enabled": (final.get("licence") or {}).get("frame_outputs_enabled")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=CASES, action="append", default=None)
    parser.add_argument("--out", default=str(WORK / "fault-matrix.json"))
    args = parser.parse_args()
    matrix = Matrix()
    matrix.build()
    selected = args.only or list(CASES)
    # An out-of-band lease release ends the session; anything after it needs a fresh one.
    restart_needed = False
    for name in selected:
        try:
            if matrix.identity is None or restart_needed:
                matrix.open_session("initial")
                restart_needed = False
            getattr(matrix, f"case_{name}")()
            restart_needed = bool(getattr(matrix, "_needs_new_session", False))
            matrix._needs_new_session = False
        except Exception as exc:  # noqa: BLE001 - every case must report, not abort
            matrix.report.setdefault("case_errors", {})[name] = {
                "type": type(exc).__name__, "detail": str(exc)[:300],
                "traceback": traceback.format_exc()[-1500:]}
            print(f"ERROR [{name}] {type(exc).__name__}: {str(exc)[:200]}", flush=True)
            matrix.stall_leader(None)
            for process, action in (("board", "CONT"), ("control", "CONT"), ("agent", "CONT")):
                try:
                    container_signal(action, process)
                except Exception as note:  # noqa: BLE001 - the case already failed
                    matrix.report.setdefault("restore_notes", {})[process] = str(note)[:120]
            restart_needed = True
        finally:
            matrix.case_footer()
    matrix.release()
    matrix.report["failed"] = [item["name"] for item in matrix.report["checks"] if not item["ok"]]
    pathlib.Path(args.out).write_text(json.dumps(matrix.report, indent=2, default=str) + "\n")
    print(f"report: {args.out}", flush=True)
    return 1 if (matrix.report.get("case_errors") or matrix.report["failed"]
                 or matrix.report.get("release_error")
                 or matrix.report["car_after_matrix"]["control_active"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
