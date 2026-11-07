#!/usr/bin/env python3
"""OFFLINE ONLY: reproduce the three frame-mode safety gaps against the target semantics.

Every assertion here is the behaviour the plan asks for, not the behaviour the code has
today, so this script is expected to fail before the fixes and to pass after them. It
records the raw car-side answers on both sides of each action so a reviewer can see the
state the upper host was told, the state the agent holds, and the state the control
process reports for itself.

Nothing here touches hardware: the leader, board and camera are the frozen simulated
fixtures, and "stopped" means the software stopped admitting and emitting, never that a
loaded arm physically halted.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import time
import traceback

from facade_smoke import (AGENT, STATE, STREAM_PORT, SyntheticUpper, WORK, car_status,
                          calibration_assets_fixture, FrameCarControlLease,
                          HybridCalibrationStore, StreamHardwareObserver,
                          FrameStreamHardwareFacade, FrameStreamDriver,
                          configure_hybrid_facade)
from lelab.hybrid_hardware import _http_json
from lelab.hybrid_recording import HttpCarClient, HybridRecordingController

CAR_DOC = STATE / "car_control_status.json"
ARM_JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5")
LEADER_NAMES = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex",
              "wrist_roll")
LATCH_WAIT_S = 5.0


def read_doc(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


class Repro:
    def __init__(self) -> None:
        self.report: dict = {"agent": AGENT, "data_identity": "simulated_offline",
                             "training_eligible": False,
                             "meaning_of_stopped": ("software admission and emission stopped; "
                                                    "not a physical stop"),
                             "cases": [], "checks": []}

    def check(self, case: str, name: str, ok, detail=None) -> bool:
        self.report["checks"].append({"case": case, "name": name, "ok": bool(ok),
                                      "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} [{case}] {name}: "
              f"{json.dumps(detail, ensure_ascii=False, default=str)[:240]}", flush=True)
        return bool(ok)

    def car_view(self) -> dict:
        """Both car-side witnesses side by side: what the agent answers, what the emitter does."""
        status = car_status()
        control = status.get("control") or {}
        licence = status.get("licence") or {}
        bridge = licence.get("bridge") or {}
        frame = bridge.get("frame_process") or {}
        document = read_doc(CAR_DOC)
        return {
            "agent_control_active": control.get("active"),
            "agent_client_id": control.get("client_id"),
            "agent_estop_latched": control.get("estop_latched"),
            "agent_lease_expires_in_s": control.get("expires_in_s"),
            "agent_frame_outputs_enabled": licence.get("frame_outputs_enabled"),
            "agent_frame_stream_connected": licence.get("frame_stream_connected"),
            "bridge_frame_process_age_s": frame.get("age_s"),
            "bridge_queue_depth": frame.get("queue_depth"),
            "bridge_process_outputs": frame.get("outputs_enabled"),
            "process_latched_fault": document.get("latched_fault"),
            "process_reengage_required": document.get("reengage_required"),
            "process_outputs_enabled": document.get("outputs_enabled"),
            "process_emitted": (document.get("counters") or {}).get("frames_emitted"),
            "process_refused": (document.get("counters") or {}).get("frames_refused"),
            "process_queue_depth": (document.get("admission") or {}).get("queue_depth"),
            "process_stream_epoch": (document.get("admission") or {}).get("session_epoch"),
        }

    def build(self) -> None:
        status = car_status()
        if (status.get("control") or {}).get("active"):
            raise RuntimeError("offline_agent_already_has_a_lease")
        store = HybridCalibrationStore(WORK / "calibration")
        assets = calibration_assets_fixture(measured_at_utc="2026-11-04T00:00:00Z")
        ids = {kind: assets[kind]["identity"] for kind in ("leader", "follower", "mapping")}
        self.upper = SyntheticUpper(agent_url=AGENT, store=store, leader_identity=ids["leader"])
        self.car_lease = FrameCarControlLease(
            AGENT, expected_profile="hardware_hybrid_manual_v1_offline6d")
        self.observer = StreamHardwareObserver(
            upper=self.upper, agent_url=AGENT, store=store, leader_identity=ids["leader"],
            follower_identity=ids["follower"], mapping_identity=ids["mapping"],
            simulated_source=True)
        self.car_lease.observer = self.observer
        facade = FrameStreamHardwareFacade(upper=self.upper, car=self.car_lease,
                                           observer=self.observer, calibration_store=store)
        self.driver = FrameStreamDriver(self.car_lease, self.observer, AGENT,
                                        host="127.0.0.1", port=STREAM_PORT)
        facade.bind_driver(self.driver)
        facade.simulated_context = {"simulated": True, "data_identity": "simulated_offline",
                                    "training_eligible": False}
        self.facade = configure_hybrid_facade(facade=facade, replace=True)
        client = HttpCarClient(lambda path, body: _http_json(
            AGENT, path, method="POST", body=body, token=self.car_lease.token, timeout=10.0))
        self.recording = HybridRecordingController(self.facade, client, offline_6d=True)
        self.facade.bind_recording_controller(self.recording, replace=True)
        self.trace: list[dict] = []

    def note(self, event: str, **extra) -> None:
        entry = {"at": round(time.monotonic() - self.t0, 3), "event": event,
                 "facade_state": self.facade.state,
                 "arm_state": self.facade.arm_state,
                 "estop_latched": getattr(self.facade, "estop_latched", None),
                 "estop_requested": getattr(self.facade, "_estop_requested", None),
                 "last_error": self.facade.last_error}
        entry.update(extra)
        self.trace.append(entry)
        print(f"  trace {json.dumps(entry, ensure_ascii=False, default=str)[:260]}", flush=True)

    def engage(self) -> None:
        self.facade.preview(self.identity)
        for action in ("align", "arm", "engage"):
            self.facade.set_arm_state({"action": action, **self.identity})

    def open(self, engage: bool = True) -> None:
        if getattr(self, "identity", None) is None:
            self.identity = None
        if self.identity and self.facade.session_id:
            try:
                self.facade.release_session("safety_repro_recover", **self.identity)
            except Exception as exc:  # noqa: BLE001 - the acquire below is the finding
                self.report.setdefault("release_notes", {})[time.strftime("%H:%M:%S")] = (
                    f"{type(exc).__name__}:{str(exc)[:140]}")
        acquired = self.facade.acquire_session({"owner": "safety-repro"})
        self.identity = {"session_id": acquired["session_id"],
                         "session_epoch": acquired["session_epoch"]}
        if engage:
            self.engage()
        else:
            # The recovery half of the path only: preview, align, arm. Engaging is the
            # operator's separate decision, so a test that stops here proves that
            # clearing a stop did not hand out motion by itself.
            self.facade.preview(self.identity)
            for action in ("align", "arm"):
                self.facade.set_arm_state({"action": action, **self.identity})

    # -- case A: an explicit emergency stop has to latch both car layers ----------
    def case_estop(self) -> None:
        case = "estop_car_latch"
        self.report["cases"].append({"name": case,
                                     "intent": ("an explicit stop must latch the agent's "
                                                "authority and the control process's emitter, "
                                                "without waiting for the recorder")})
        self.t0 = time.monotonic()
        self.trace = []
        self.open()
        before = self.car_view()
        self.note("engaged", car=before)
        self.check(case, "following_before_the_stop",
                   self.facade.arm_state == "following" and before["agent_control_active"] is True,
                   {"arm": self.facade.arm_state, "active": before["agent_control_active"]})
        stopped_locally = time.monotonic() - self.t0
        self.recording.estop("safety_repro_explicit")
        local_stop = time.monotonic() - self.t0
        self.note("estop_requested", local_stop_s=round(local_stop, 3))
        deadline = time.monotonic() + LATCH_WAIT_S
        view = self.car_view()
        while time.monotonic() < deadline and not (
                view["agent_estop_latched"] is True
                and view["process_latched_fault"] not in (None, False)):
            time.sleep(0.1)
            view = self.car_view()
        agent_at = round(time.monotonic() - self.t0, 3)
        self.note("car_view_after_estop_wait", car=view, waited_to_s=agent_at)
        self.report["cases"][-1]["before"] = before
        self.report["cases"][-1]["after"] = view
        self.report["cases"][-1]["timings_s"] = {
            "local_stopped": round(stopped_locally, 3), "estop_returned": round(local_stop, 3),
            "car_confirmed_or_timed_out": agent_at}
        self.check(case, "agent_latches_the_estop", view["agent_estop_latched"] is True,
                   {"agent_estop_latched": view["agent_estop_latched"],
                    "agent_frame_outputs_enabled": view["agent_frame_outputs_enabled"],
                    "waited_to_s": agent_at})
        self.check(case, "control_process_latches_the_estop",
                   view["process_latched_fault"] not in (None, False),
                   {"process_latched_fault": view["process_latched_fault"],
                    "process_outputs_enabled": view["process_outputs_enabled"]})
        self.check(case, "car_stops_emitting",
                   (view["process_queue_depth"] or 0) == 0
                   and view["process_outputs_enabled"] is not True, view)
        # and the stop must not be a lease loss dressed up as one
        self.check(case, "stop_reported_as_stop_not_lease_loss",
                   "lease_lost" not in str(self.facade.last_error),
                   {"last_error": self.facade.last_error})
        self.trace = []

    # -- case B: lease expiry must clean the frame-mode permission ------------------
    def case_lease_expiry(self) -> None:
        case = "lease_expiry_cleanup"
        self.report["cases"].append({"name": case,
                                     "intent": ("an expired control lease revokes the frame "
                                                "permission and the old identity on its own, "
                                                "with no legacy publisher present and no status "
                                                "call doing the cleanup")})
        self.t0 = time.monotonic()
        self.trace = []
        self.open()
        before = self.car_view()
        self.note("engaged", car=before)
        # Stop renewing and stop reading: nobody may do the cleanup as a side effect.
        deadline = time.monotonic() + 45.0
        view = before
        while time.monotonic() < deadline:
            time.sleep(2.0)
            view = self.car_view()
            if view["agent_control_active"] is not True:
                break
        self.note("lease_expired", car=view, waited_to_s=round(time.monotonic() - self.t0, 2))
        self.report["cases"][-1]["before"] = before
        self.report["cases"][-1]["after"] = view
        self.check(case, "car_lease_expired", view["agent_control_active"] is not True,
                   {"expires_in_s": view["agent_lease_expires_in_s"], "active":
                    view["agent_control_active"]})
        expired_at = time.monotonic()
        dropped = None
        while time.monotonic() - expired_at < 3.0:
            view = self.car_view()
            if view["agent_client_id"] in (None, ""):
                dropped = round(time.monotonic() - expired_at, 3)
                break
            time.sleep(0.1)
        self.report["cases"][-1]["identity_dropped_after_s"] = dropped
        self.check(case, "frame_permission_dropped_with_the_lease",
                   view["agent_frame_outputs_enabled"] is not True,
                   {"agent_frame_outputs_enabled": view["agent_frame_outputs_enabled"],
                    "process_outputs_enabled": view["process_outputs_enabled"],
                    "detail": "the agent mirror still advertises outputs the emitter will not do"})
        self.check(case, "old_identity_dropped", view["agent_client_id"] in (None, ""),
                   {"agent_client_id": view["agent_client_id"]})
        self.check(case, "no_queued_targets_survive",
                   (view["process_queue_depth"] or 0) == 0, view)
        self.trace = []

    # -- case C: clearing a stop must not be trapped by its own request flag ---------
    def case_clear_recovery(self) -> None:
        case = "clear_then_new_session"
        self.report["cases"].append({"name": case,
                                     "intent": ("clearing grants recovery only: the session "
                                                "must leave estop, the old session be released, "
                                                "and a new session reach preview without motion")})
        self.t0 = time.monotonic()
        self.trace = []
        # This case deliberately starts wherever the previous one left the facade: a stop
        # that was never cleared is exactly the state an operator is sitting in.
        start_state = self.facade.state
        self.note("case_start", state=start_state,
                  preexisting_estop=start_state == "estop")
        if start_state != "estop":
            self.recording.estop("safety_repro_clear_case")
            self.note("after_estop")
        stuck = self.facade.state
        try:
            self.facade.set_arm_state({"action": "clear_estop", **self.identity})
            clear_error = None
        except Exception as exc:  # noqa: BLE001 - the refusal is the finding
            clear_error = f"{type(exc).__name__}:{str(exc)[:160]}"
        after_clear = self.facade.state
        self.note("after_clear", raised=clear_error)
        self.report["cases"][-1]["state_at_case_start"] = start_state
        self.report["cases"][-1]["state_after_estop"] = stuck
        self.report["cases"][-1]["state_after_clear"] = after_clear
        self.report["cases"][-1]["clear_raised"] = clear_error
        self.check(case, "clear_leaves_the_estop_state", after_clear != "estop",
                   {"state_after_estop": stuck, "state_after_clear": after_clear,
                    "raised": clear_error,
                    "estop_requested": getattr(self.facade, "_estop_requested", None),
                    "estop_latched": getattr(self.facade, "estop_latched", None),
                    "detail": "_refresh re-forces estop from _estop_requested, which the "
                              "clear branch never resets"})
        right_after_clear = self.car_view()
        self.report["cases"][-1]["car_right_after_clear"] = right_after_clear
        self.check(case, "clearing_grants_no_motion",
                   self.facade.arm_state != "following"
                   and right_after_clear["process_outputs_enabled"] is not True
                   and right_after_clear["agent_frame_outputs_enabled"] is not True,
                   {"arm": self.facade.arm_state, "state": self.facade.state,
                    "agent_frame_outputs_enabled":
                        right_after_clear["agent_frame_outputs_enabled"],
                    "process_outputs_enabled": right_after_clear["process_outputs_enabled"],
                    "detail": "clearing may allow recovery, never movement"})
        released = None
        if self.facade.session_id:
            try:
                self.facade.release_session("safety_repro_release", **self.identity)
            except Exception as exc:  # noqa: BLE001
                released = f"{type(exc).__name__}:{str(exc)[:160]}"
        self.note("after_release", raised=released)
        reacquired = None
        try:
            self.open()
        except Exception as exc:  # noqa: BLE001 - the dead end is the finding
            reacquired = f"{type(exc).__name__}:{str(exc)[:200]}"
        self.note("after_reacquire", raised=reacquired)
        view = self.car_view()
        self.report["cases"][-1]["release_raised"] = released
        self.report["cases"][-1]["reacquire_raised"] = reacquired
        self.report["cases"][-1]["car_after_reacquire"] = view
        self.check(case, "a_new_session_can_be_taken_after_a_clear", reacquired is None,
                   {"reacquire_raised": reacquired, "release_raised": released,
                    "state": self.facade.state,
                    "agent_estop_latched": view["agent_estop_latched"],
                    "detail": "the upper facade refuses acquire on its own estop flag while "
                              "the agent never latched one, so neither side can be cleared"})
        enabled_at = None
        waited = time.monotonic()
        while time.monotonic() - waited < 5.0:
            if self.car_view()["process_outputs_enabled"] is True:
                enabled_at = round(time.monotonic() - waited, 3)
                break
            time.sleep(0.2)
        view = self.car_view()
        # Recovery must end somewhere usable: the helper's new session reaches following
        # only through preview/align/arm/engage, which is the path the page has to walk.
        self.check(case, "new_session_reaches_following_only_by_expense_of_engage",
                   self.facade.arm_state == "following"
                   and view["process_outputs_enabled"] is True,
                   {"arm": self.facade.arm_state, "emitter_enabled_after_s": enabled_at,
                    "process_outputs_enabled": view["process_outputs_enabled"],
                    "state": self.facade.state})
        self.trace = []


    # -- case D: a retired generation on a still-open socket must not reach the arm ------
    def case_stale_generation(self) -> None:
        case = "stale_generation_online"
        self.report["cases"].append({"name": case,
                                     "intent": ("while the first control socket stays open, "
                                                "rotate the car's stream generation and prove "
                                                "a frame from the old generation is refused "
                                                "without touching sequences, the queue or the "
                                                "published count")})
        self.t0 = time.monotonic()
        self.trace = []
        self.open()
        link = self.driver.link
        if link is None:
            raise RuntimeError("no_live_link_to_test")
        # The follow worker closes this socket the moment the stop lands, and the whole
        # point is to keep the first connection open across the rotation: park the host's
        # own loop and send the frames below by hand.
        self.facade._stop_follow_worker()
        sent: list[dict] = []
        original_send = link.client.send

        def recording_send(message):
            if isinstance(message, dict) and message.get("type") == "frame":
                sent.append(dict(message))
            return original_send(message)

        link.client.send = recording_send          # type: ignore[method-assign]
        for _ in range(3):
            self.send_one_frame(link)
            time.sleep(0.2)
        retired = link.stream_epoch
        self.report["cases"][-1]["retired_stream_epoch"] = str(retired)
        self.report["cases"][-1]["frames_captured"] = len(sent)
        self.check(case, "the_link_was_actually_sending", len(sent) >= 1,
                   {"captured": len(sent), "stream_epoch": str(retired)})
        before = self.car_counters_full()
        # 1. the car latches from the frame channel itself, on the open socket
        link.client.send({"type": "preempt", "action": "estop", "reason": "safety_repro_generation"})
        receipt = None
        waited = time.monotonic()
        while time.monotonic() - waited < 3.0:
            answer = self.car_view()
            if answer["process_latched_fault"] not in (None, False):
                receipt = answer
                break
            time.sleep(0.1)
        self.check(case, "car_latched_from_the_frame_channel", receipt is not None,
                   {"latched_fault": (receipt or {}).get("process_latched_fault"),
                    "waited_s": round(time.monotonic() - waited, 3)})
        # 2. clearing rotates the generation; the same socket stays open
        cleared = self._car_http("/control/clear_estop", {})
        self.check(case, "agent_confirmed_the_clear", cleared.get("estop") is False, cleared)
        rotated_at = time.monotonic()
        live_epoch = None
        while time.monotonic() - rotated_at < 4.0:
            document = read_doc(CAR_DOC)
            live_epoch = (document.get("stream_epoch")
                        or (document.get("admission") or {}).get("stream_epoch"))
            if live_epoch is not None and str(live_epoch) != str(retired):
                break
            time.sleep(0.1)
        after_clear = self.car_counters_full()
        self.report["cases"][-1]["generation_after_clear"] = {
            "retired": str(retired), "live": str(live_epoch),
            "counters_after_clear": after_clear}
        self.check(case, "clearing_rotated_the_generation",
                   live_epoch is not None and str(live_epoch) != str(retired),
                   {"retired": str(retired), "live": str(live_epoch)})
        self.check(case, "clearing_left_the_process_disarmed",
                   after_clear.get("reengage_required") is True
                   and after_clear.get("outputs_enabled") is not True, after_clear)
        # 3. replay a real frame of the retired generation on the still-open socket
        stale = dict(sent[-1])
        stale["frame_seq"] = int(stale.get("frame_seq", 0)) + 10_000
        probe_before = self.car_counters_full()
        # The frames sent before the stop each have a receipt still sitting in the socket.
        # Reading "the next reply" would report one of those as the verdict on the stale
        # frame, so replies are matched by frame_seq and the others are recorded.
        seen: list[dict] = []
        waited = time.monotonic()
        answer = None
        while time.monotonic() - waited < 4.0:
            try:
                link.client.send(stale)
            except Exception as exc:  # noqa: BLE001 - a dead socket is itself a refusal
                answer = {"state": "socket_closed", "reason": f"{type(exc).__name__}"}
                break
            while len(seen) < 24:
                try:
                    reply = link.client.recv(0.4)
                except Exception as exc:  # noqa: BLE001
                    reply = {"type": "recv_error", "reason": str(exc)[:80]}
                if not isinstance(reply, dict):
                    break
                if reply.get("type") != "receipt":
                    break
                seen.append(reply)
                if reply.get("frame_seq") == stale["frame_seq"]:
                    answer = reply
                    break
            if answer is not None:
                break
            time.sleep(0.2)
        self.report["cases"][-1]["receipts_seen"] = seen[:8]
        probe_after = self.car_counters_full()
        self.report["cases"][-1]["stale_frame_answer"] = answer
        self.report["cases"][-1]["stale_frame_sent"] = {
            "frame_seq": stale.get("frame_seq"), "stream_epoch": str(stale.get("stream_epoch"))}
        self.report["cases"][-1]["counters_across_the_stale_frame"] = {
            "before": probe_before, "after": probe_after}
        refused_reason = str((answer or {}).get("reason") or "")
        self.check(case, "the_old_generation_frame_is_refused",
                   (answer or {}).get("state") == "refused"
                   and (stale.get("stream_epoch") is None
                        or refused_reason.startswith("stream_epoch")
                        or refused_reason in ("no_live_licence", "not_engaged")
                        or "epoch" in refused_reason),
                   {"state": (answer or {}).get("state"), "reason": refused_reason,
                    "stale_frame_seq": stale.get("frame_seq"),
                    "stale_carries_epoch": str(stale.get("stream_epoch")),
                    "answer": str(answer)[:220], "other_receipts": len(seen)})
        self.check(case, "refusal_consumed_nothing_new",
                   probe_after["emitted"] == probe_before["emitted"]
                   and (probe_after["queue_depth"] or 0) == 0
                   and probe_after["accepted"] == probe_before["accepted"],
                   {"emitted": (probe_before["emitted"], probe_after["emitted"]),
                    "accepted": (probe_before["accepted"], probe_after["accepted"]),
                    "queue_depth": probe_after["queue_depth"],
                    "refused": (probe_before["refused"], probe_after["refused"])})
        # 4. a fresh generation on a new session works again
        link.client.send = original_send            # type: ignore[assignment]
        self.driver.abort()
        self.open()
        moved = self.moving_frames(1.5)
        self.report["cases"][-1]["new_generation"] = {
            "stream_epoch": str(self.driver.link.stream_epoch) if self.driver.link else None,
            **moved}
        self.check(case, "a_new_generation_works_again",
                   moved["emitted_delta"] > 0
                   and str((self.driver.link.stream_epoch if self.driver.link else None))
                   != str(retired),
                   {"retired": str(retired),
                    "live": str(self.driver.link.stream_epoch) if self.driver.link else None,
                    **moved})
        self.trace = []

    def send_one_frame(self, link) -> dict:
        """Submit one whole frame from the live observation, exactly as the worker would."""
        facts = self.observer.snapshot()
        preview = self.observer.map_sample(facts)
        targets = {item.get("joint"): item for item in preview.get("targets") or []}
        gripper = preview.get("gripper") or {}
        leader = facts.get("leader") or {}
        positions = leader.get("positions_rad") or {}
        return link.send_snapshot(
            arm_positions=[float(targets[name]["target_rad"]) for name in ARM_JOINTS],
            gripper_rad=float(gripper["target_rad"]),
            sample_seq=int(leader.get("reads") or 1),
            leader_rad=[float(positions.get(name) or 0.0) for name in LEADER_NAMES],
            pre_clip_rad=[float(targets[name]["pre_clip_rad"]) for name in ARM_JOINTS],
            clutch=True)

    def car_counters_full(self) -> dict:
        document = read_doc(CAR_DOC)
        counters = document.get("counters") or {}
        admission = document.get("admission") or {}
        return {"emitted": counters.get("frames_emitted"),
                "accepted": (admission.get("counters") or {}).get("accepted"),
                "refused": (admission.get("counters") or {}).get("refused"),
                "queue_depth": admission.get("queue_depth"),
                "outputs_enabled": document.get("outputs_enabled"),
                "reengage_required": document.get("reengage_required"),
                "latched_fault": document.get("latched_fault")}

    def _car_http(self, path: str, body: dict) -> dict:
        return self._raw_http(path, body)

    def _raw_http(self, path: str, body: dict) -> dict:
        return _http_json(AGENT, path, method="POST", body=body,
                          token=self.car_lease.token, timeout=6.0)

    def moving_frames(self, seconds: float) -> dict:
        before = self.car_counters_full()["emitted"] or 0
        time.sleep(seconds)
        after = self.car_counters_full()["emitted"] or 0
        return {"emitted_delta": after - before}


    # -- case E: clearing with a dead or released token --------------------------
    def case_token_branches(self) -> None:
        case = "token_branches"
        self.report["cases"].append({"name": case, "intent": (
            "an emergency stop must be liftable through the page's own clear action with a "
            "live token, with an expired lease, and after the session was released - each "
            "time granting recovery only, never motion - and never by an unrelated client")})
        branches: dict = {}
        for label, kill, authorised_by in (
                ("live_token", None, "control_token"),
                ("expired_lease", "expire", "prior_owner_recovery"),
                ("released_session", "release", "prior_owner_recovery")):
            self.t0 = time.monotonic()
            self.trace = []
            self.open()
            self.recording.estop(f"safety_repro_{label}")
            latched = self.car_view()
            self.check(case, f"{label}_latched_on_both_car_layers",
                       latched["agent_estop_latched"] is True
                       and latched["process_latched_fault"] == "estop", latched)
            if kill == "expire":
                deadline = time.monotonic() + 40.0
                while time.monotonic() < deadline:
                    view = self.car_view()
                    if view["agent_control_active"] is not True and view["agent_client_id"] in (
                            None, ""):
                        break
                    time.sleep(1.0)
            elif kill == "release":
                self.facade.release_session("safety_repro_release_token", **self.identity)
                self.identity = None
            after_kill = self.car_view()
            # The operator's action, not the private helper: the page only ever posts the
            # arm-state event, and the session's own latch flag has to come down with it.
            try:
                self.facade.set_arm_state({"action": "clear_estop",
                                           **(self.identity or {})})
                cleared = dict(self.facade.status().get("car_clear") or {})
            except Exception as exc:  # noqa: BLE001 - the refusal is the finding
                cleared = {"car_clear": "raised",
                           "car_clear_error": f"{type(exc).__name__}:{str(exc)[:160]}"}
            view = self.car_view()
            branches[label] = {
                "token_state": {"live_token": "held", "expired_lease": "expired",
                                "released_session": "released"}[label],
                "lease_live_before_clear": after_kill["agent_control_active"],
                "lease_expires_in_s_before_clear": after_kill["agent_lease_expires_in_s"],
                "clear": cleared,
                "agent_latched_after_clear": view["agent_estop_latched"],
                "process_latched_after_clear": view["process_latched_fault"],
                "outputs_enabled_after_clear": view["agent_frame_outputs_enabled"],
                "session_state_after_clear": self.facade.state,
                "arm_state_after_clear": self.facade.arm_state}
            self.check(case, f"{label}_token_branch_clears_both_layers",
                       cleared.get("car_clear") == "confirmed"
                       and cleared.get("authorised_by") == authorised_by
                       and view["agent_estop_latched"] is not True
                       and view["process_latched_fault"] is None, branches[label])
            self.check(case, f"{label}_branch_granted_no_motion",
                       view["agent_frame_outputs_enabled"] is not True
                       and view["process_outputs_enabled"] is not True
                       and self.facade.arm_state != "following",
                       {"frame_outputs_enabled": view["agent_frame_outputs_enabled"],
                        "process_outputs_enabled": view["process_outputs_enabled"],
                        "arm_state": self.facade.arm_state})
            # Recovery is then the ordinary path: a new session, preview, align, arm -
            # and still nothing may reach the arm until the operator engages.
            armed = None
            try:
                self.open(engage=False)
            except Exception as exc:  # noqa: BLE001
                armed = f"{type(exc).__name__}:{str(exc)[:160]}"
            pre = self.car_view()
            branches[label]["recovered_to_awaiting_engage"] = {
                "error": armed, "frame_outputs_enabled": pre["agent_frame_outputs_enabled"],
                "process_outputs_enabled": pre["process_outputs_enabled"],
                "arm_state": self.facade.arm_state}
            self.check(case, f"{label}_branch_recovers_to_awaiting_engage_without_output",
                       armed is None and self.facade.arm_state == "armed"
                       and pre["agent_frame_outputs_enabled"] is not True
                       and pre["process_outputs_enabled"] is not True,
                       branches[label]["recovered_to_awaiting_engage"])
            self.engage()
            following = self.car_view()
            branches[label]["engaged_after_recovery"] = {
                "arm_state": self.facade.arm_state,
                "frame_outputs_enabled": following["agent_frame_outputs_enabled"]}
            self.check(case, f"{label}_branch_then_engages_explicitly",
                       self.facade.arm_state == "following"
                       and following["agent_frame_outputs_enabled"] is True,
                       branches[label]["engaged_after_recovery"])
        # and the negative control: a stranger must not be able to lift it
        self.recording.estop("safety_repro_stranger")
        self.car_lease.token = None
        stranger = self.facade._car_answer("/control/clear_estop",
                                          {"client_id": "not-the-owner"}, None)
        self.report["cases"][-1]["branches"] = branches
        self.report["cases"][-1]["stranger_attempt"] = stranger
        still = self.car_view()
        self.check(case, "a_stranger_cannot_lift_the_stop",
                   still["agent_estop_latched"] is True,
                   {"answer": str(stranger)[:200], "still_latched": still["agent_estop_latched"],
                    "process_latched": still["process_latched_fault"]})
        # and the refusal must not be a dead end: the owner that is really in control can
        # still lift the stop with the same page action, and still gets recovery only.
        try:
            self.facade.set_arm_state({"action": "clear_estop"})
            lifted = dict(self.facade.status().get("car_clear") or {})
        except Exception as exc:  # noqa: BLE001 - the refusal is the finding
            lifted = {"car_clear": "raised",
                      "car_clear_error": f"{type(exc).__name__}:{str(exc)[:160]}"}
        owner = self.car_view()
        self.report["cases"][-1]["owner_clear_after_refusal"] = {"clear": lifted, "car": owner}
        self.check(case, "the_owner_can_still_lift_it_after_the_refusal",
                   lifted.get("car_clear") == "confirmed"
                   and owner["agent_estop_latched"] is not True
                   and owner["process_latched_fault"] is None
                   and owner["agent_frame_outputs_enabled"] is not True,
                   self.report["cases"][-1]["owner_clear_after_refusal"])
        self.trace = []

    def release(self) -> None:
        try:
            if self.facade.session_id:
                self.facade.release_session("safety_repro_finished", **{
                    "session_id": self.facade.session_id,
                    "session_epoch": self.facade.session_epoch})
        except Exception as exc:  # noqa: BLE001
            self.report["release_error"] = f"{type(exc).__name__}:{str(exc)[:160]}"
        try:
            _http_json(AGENT, "/control/clear_estop", method="POST", body={}, timeout=6.0)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            self.report["clear_estop_note"] = f"{type(exc).__name__}:{str(exc)[:160]}"
        final = self.car_view()
        self.report["car_after_repro"] = final


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=("estop_car_latch", "lease_expiry_cleanup",
                                           "clear_then_new_session",
                                           "stale_generation_online",
                                           "token_branches"), action="append")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    repro = Repro()
    repro.build()
    methods = {"estop_car_latch": "case_estop", "lease_expiry_cleanup": "case_lease_expiry",
               "clear_then_new_session": "case_clear_recovery",
               "stale_generation_online": "case_stale_generation",
               "token_branches": "case_token_branches"}
    # lease expiry runs first: an uncleared stop blocks every later acquire,
    # and that dead end is itself the third finding.
    for case in (args.only or ("lease_expiry_cleanup", "estop_car_latch",
                               "clear_then_new_session", "stale_generation_online")):
        try:
            getattr(repro, methods[case])()
        except Exception as exc:  # noqa: BLE001 - the report is the deliverable
            repro.report.setdefault("case_errors", {})[case] = {
                "type": type(exc).__name__, "detail": str(exc)[:300],
                "traceback": traceback.format_exc()[-1500:]}
            print(f"ERROR [{case}] {type(exc).__name__}: {str(exc)[:200]}", flush=True)
    repro.release()
    repro.report["failed"] = [c["name"] for c in repro.report["checks"] if not c["ok"]]
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(repro.report, indent=2, default=str) + "\n")
    print(f"report: {out}", flush=True)
    return 1 if repro.report["failed"] or repro.report.get("case_errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
