#!/usr/bin/env python3
"""Standalone car-side ROS 2 control process.

Why this exists as its own process: the agent keeps the HTTP surface, the camera,
the control lease and the recorder, and none of that may sit on the same Python
interpreter lock as the control loop.  Adding a node or a thread inside the agent
does not achieve the separation - a GIL is per process, and a default callback group
can still serialise a multi-threaded executor.

Division of authority:
* the agent decides whether output may happen at all and pushes that decision here
  as a short-lived mirror (``licence_ipc``);
* this process decides, per tick, whether one specific frame is admissible on its
  own clock (freshness window, TTL, licence expiry, limits, step rate, feedback age);
* the vendor board owner remains the only writer to the serial bus.

The frame path is whole-frame or nothing: one ``ServosPosition`` carrying ids
1..5 and 10 with one shared duration, or no publish at all.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import Int32MultiArray, String

from ros_robot_controller_msgs.msg import (
    BusServoState, ServoPosition, ServosPosition, SetBusServoState,
)
from ros_robot_controller_msgs.srv import GetBusServoState

from .licence_ipc import LicenceMirror
from .ros_servo_output import RosServoOutputDriver
from .ros_servo_readback import RosServoFeedbackTopicDriver, TorqueStateReadback
from .teleop_frame_v1 import (
    FrameAdmission, FrameError, FreshnessWindow, six_axis_targets,
)
from .teleop_stream import StreamServer

MS_TO_NS = 1_000_000


def percentiles(values: list[float]) -> dict[str, Any]:
    """p50/p95/p99 over the samples kept, or None when there are too few."""
    if len(values) < 3:
        return {"count": len(values), "p50_ms": None, "p95_ms": None, "p99_ms": None,
                "max_ms": None}
    ordered = sorted(values)
    def at(fraction: float) -> float:
        index = min(len(ordered) - 1, int(len(ordered) * fraction))
        return ordered[index]
    return {"count": len(ordered), "p50_ms": at(0.50), "p95_ms": at(0.95),
            "p99_ms": at(0.99), "max_ms": ordered[-1]}


class CarControlRuntime:
    """Tick-driven control: admit newest frame, re-check life, emit one six-axis write."""

    def __init__(self, node: Node, *, profile: dict[str, Any], control_hz: float,
                 window_ms: int, licence: LicenceMirror, readback: RosServoFeedbackTopicDriver,
                 driver: RosServoOutputDriver, data_identity: str, status_path: Path,
                 bundle_hash: str = "", max_ttl_ms: int = 200,
                 recording_publisher=None):
        self.node = node
        self.profile = profile
        self.control_hz = control_hz
        self.window_ms = window_ms
        self.licence = licence
        self.readback = readback
        self.driver = driver
        self.data_identity = data_identity
        self.status_path = status_path
        self.bundle_hash = bundle_hash
        self.max_ttl_ms = max_ttl_ms
        # Two clocks, named: the recorded stamp has to match what the recorder and the bag
        # use, and the control loop's own deadlines are measured on this process' monotonic
        # clock.  They are never compared to each other.
        runtime = profile.get("runtime", {}) or {}
        self.clock_domain = str(runtime.get("clock_domain") or "car_system_time")
        self.control_clock_domain = str(runtime.get("control_clock_domain")
                                       or "car_process_monotonic")
        self.recording_publisher = recording_publisher
        self.windows = FreshnessWindow()
        self.stream_epoch = self.windows.new_stream_epoch("start")
        self.admission: FrameAdmission | None = None
        self.latched_fault: str | None = None
        self.latched_at_generation: int | None = None
        # Set when a stop was lifted: motion may not resume until the client re-hellos
        # (re-align) and the agent separately enables outputs again.
        self.reengage_required = False
        self.last_clear: dict[str, object] | None = None
        self.paused = False
        self.started = time.monotonic()
        self.counters = {"ticks": 0, "frames_received": 0, "frames_emitted": 0,
                         "frames_refused": 0, "frames_expired": 0, "empty_ticks": 0,
                         "hold_frames": 0, "window_issues": 0, "licences_lost": 0,
                         "contexted_rows": 0, "uncontexted_rows": 0}
        self.last_emit_mono: float | None = None
        # Interval between emitted frames, kept so a benchmark can report publish
        # interval percentiles without re-deriving them from counters.
        self.emit_intervals_ms: collections.deque = collections.deque(maxlen=2000)
        self._licence_was_present = False
        self._driver_was_enabled = False
        self.last_refusal: dict[str, str] | None = None
        self.refusal_lag_ms: list[float] = []
        self.lock = threading.Lock()
        # The ROS timer thread and the stream thread both touch the admission and
        # the output driver; the driver is already internally locked, the admission
        # is not, so the tick and the frame handler serialise here.
        self.control_lock = threading.Lock()
        self.stream: StreamServer | None = None
        node.create_timer(1.0 / control_hz, self._tick)
        node.create_timer(0.5, self._write_status)

    # -- session wiring ----------------------------------------------------
    def open_epoch(self, *, session_id, session_epoch: str, bundle_hash: str,
                   max_ttl_ms: int) -> FrameAdmission:
        """One admission per control epoch, bound to the connection generation."""
        self.admission = FrameAdmission(
            session_id=session_id, session_epoch=session_epoch,
            calibration_bundle_hash=bundle_hash, windows=self.windows,
            channel_validator=self._validate_channel, max_ttl_ms=max_ttl_ms,
            data_identity=self.data_identity, stream_epoch=self.stream_epoch)
        return self.admission

    def _validate_channel(self, channel: str, command: dict[str, Any]) -> dict[str, Any]:
        """Pure per-channel checks; the driver keeps the authority on legality.

        Nothing here may mutate a filter, a limiter or a sequence - that is what
        makes a refused frame demonstrably free of side effects.
        """
        permitted, reason = self.licence.may_emit(channel)
        if not permitted:
            raise ValueError(f"licence_{reason}")
        target = command.get("target")
        if not isinstance(target, dict):
            raise ValueError("target_not_an_object")
        values = ([(name, value) for name, value in
                   zip(self.profile["robot"]["arm_joints"], target["positions_rad"])]
                  if channel == "arm" else [(target["joint_name"], target["position_rad"])])
        for name, value in values:
            self.driver.check_only(name, value)
        return {"accepted": True}

    # -- the loop ----------------------------------------------------------
    def _tick(self) -> None:
        with self.control_lock:
            self._tick_locked()

    def _tick_locked(self) -> None:
        self.counters["ticks"] += 1
        window = self.windows.issue(duration_ms=self.window_ms)
        self.counters["window_issues"] += 1
        if self.stream is not None:
            self.stream.push_window({"window_id": window["window_id"],
                                     "stream_epoch": window["stream_epoch"],
                                     "duration_ms": window["duration_ms"],
                                     "window_remaining_ms": round(self.windows.remaining_ms(), 1)})
        # A stop is lifted only by an explicit clearance from the agent.  A renew, a
        # reconnect, a re-published licence or any higher generation does not count:
        # those are the paths by which motion could resume with nobody having decided
        # it was safe.  Clearing also leaves the outputs disabled, because the
        # clearance message carries no permission, and the next emission still needs a
        # fresh hello plus the agent's own enable step.
        clear = self.licence.take_clear_estop()
        if clear is not None and self.latched_fault:
            if self.latched_at_generation is None \
                    or clear["control_generation"] > self.latched_at_generation:
                self.latched_fault = None
                self.latched_at_generation = None
                self.reengage_required = True
                if self.admission is not None:
                    self.admission.clear("estop_cleared")
                # Rotating only the window issuer would leave the admission bound to the
                # generation that existed before the stop, so a frame from that generation
                # could still be accepted and only be caught later. The clearance retires
                # the served generation itself; motion still needs a new hello plus an
                # explicit enable, which is what reengage_required holds open.
                self.stream_epoch = self.windows.new_stream_epoch("estop_cleared")
                if self.admission is not None:
                    self.admission.bind_stream_epoch(self.stream_epoch)
                self.counters["fault_clears"] = self.counters.get("fault_clears", 0) + 1
                self.last_clear = {"reason": clear["reason"],
                                   "control_generation": clear["control_generation"]}
        if self.latched_fault or self.paused or self.reengage_required:
            # A lifted stop leaves this process in the no-motion state it started in: the
            # queue is cleared, the old reference is gone, and nothing emits until the
            # client re-hellos (the re-align) and the agent's licence still says it may.
            self.counters["empty_ticks"] += 1
            return
        licence = self.licence.current()
        if licence is None:
            self._stop_following("licence_lost")
            self._driver_was_enabled = False
            return
        self._licence_was_present = True
        # Enabling is decided here, at the emitting boundary, from the current
        # mirror - not once at session start.  The driver still refuses to enable
        # without trusted feedback of its own.
        if licence["outputs_enabled"] and not self.driver.outputs_enabled:
            if self._driver_was_enabled:
                self._require_explicit_reengage(
                    "driver_stopped:" + str(getattr(self.driver, "last_error", None) or "unknown"))
                return
            try:
                self.driver.enable()
                self._driver_was_enabled = True
            except RuntimeError as error:
                self.last_refusal = {"frame_seq": "none", "reason": f"enable_{error}"}
                return
        elif not licence["outputs_enabled"] and self.driver.outputs_enabled:
            self._stop_following("licence_output_permission_withdrawn")
        if not licence["outputs_enabled"]:
            self._driver_was_enabled = False
        admission = self.admission
        if admission is None:
            return
        pending = admission.take()
        if pending is None:
            self.counters["empty_ticks"] += 1
            return
        # The frame was admitted a moment ago against the licence of that instant. Between
        # admission and emission the session, epoch, bundle or generation can change - an
        # estop, a rebind, another owner taking over - and a hello-time check alone would
        # still emit the stale target. Nothing leaves this process without matching the
        # permission that is live right now.
        still_valid, why = self._emission_identity_ok(pending["frame"])
        if not still_valid:
            self.counters["frames_dropped_before_emit"] = \
                self.counters.get("frames_dropped_before_emit", 0) + 1
            self.last_refusal = {"frame_seq": str(pending["frame"].get("frame_seq")),
                                 "reason": why}
            self._stop_following(why)
            return
        targets = self._targets(pending["decoded"])
        try:
            self.driver.submit_frame(targets)
        except (RuntimeError, ValueError) as error:
            self.counters["frames_refused"] += 1
            self.last_refusal = {"frame_seq": str(pending["frame"]["frame_seq"]),
                                 "reason": str(error)}
            self._require_explicit_reengage(str(error), pending["frame"]["frame_seq"])
            return
        now_mono = time.monotonic()
        if self.last_emit_mono is not None:
            self.emit_intervals_ms.append(round((now_mono - self.last_emit_mono) * 1000, 2))
        self.counters["frames_emitted"] += 1
        self.last_emit_mono = now_mono
        self._record_frame(pending)

    def _emission_identity_ok(self, frame: dict[str, Any]) -> tuple[bool, str]:
        """Compare one admitted frame against the licence and generation live right now."""
        licence = self.licence.current()
        if licence is None:
            return False, "licence_lost_before_emit"
        for key in ("session_id", "session_epoch", "calibration_bundle_hash"):
            if str(licence.get(key)) != str(frame.get(key)):
                return False, f"licence_identity_changed_before_emit:{key}"
        epoch = getattr(self, "stream_epoch", None)
        if epoch is not None and str(frame.get("stream_epoch")) != str(epoch):
            return False, "stream_epoch_retired_before_emit"
        if not licence["outputs_enabled"]:
            return False, "licence_output_permission_withdrawn"
        return True, "ok"

    def _record_frame(self, accepted: dict[str, Any]) -> None:
        """One row per emitted frame, on the car clock, with its own identity.

        action is the post-admission target and state is the readback: the command
        cache topic is never substituted for a measurement, and offline rows stay
        tagged simulated so nothing here can be mistaken for hardware evidence.

        Two clocks travel with every row because they answer different questions.
        `recorded_ns` is this process' monotonic clock, which is what the TTL and the
        freshness window are measured against; `bag_stamp_ns` is the node clock, the same
        domain `ros2 bag record` stamps messages with, so a row can be placed next to a
        camera frame or a phase event without pretending the two clocks are one.  Neither
        is ever compared to the other.
        """
        if self.recording_publisher is None:
            return
        frame = accepted["frame"]
        arm = frame["commands"]["arm"]["target"]
        gripper = frame["commands"]["gripper"]["target"]
        snapshot = self.readback.read_snapshot()
        context = self.licence.context() or {}
        if context:
            self.counters["contexted_rows"] += 1
        else:
            # No context is not an error: it means the agent has no open recording session,
            # and the row says so instead of inventing an episode to sit in.
            self.counters["uncontexted_rows"] += 1
        row = {
            "kind": "teleop_frame",
            "data_identity": self.data_identity,
            "clock_domain": self.clock_domain,
            "stamp_ns": int(self.node.get_clock().now().nanoseconds),
            "control_clock_domain": self.control_clock_domain,
            "control_mono_ns": time.monotonic_ns(),
            "session_id": frame["session_id"], "session_epoch": frame["session_epoch"],
            "stream_epoch": frame["stream_epoch"], "frame_seq": frame["frame_seq"],
            "sample_seq": frame["sample_seq"],
            "channel_seq": {name: detail["seq"] for name, detail in accepted["decoded"].items()},
            "calibration_bundle_hash": frame["calibration_bundle_hash"],
            # The recorder's identity, mirrored by the agent: rows with no episode here
            # belong to no episode, and no export stage may guess one for them.
            "recording_session_id": context.get("session_id"),
            "recording_session_epoch": context.get("session_epoch"),
            "episode_id": context.get("episode_id"),
            "phase": context.get("phase"),
            "clock_epoch": context.get("clock_epoch"),
            "context_seq": context.get("context_seq"),
            "recording_context": "agent" if context else "missing",
            "freshness_window_id": accepted["window_id"],
            "window_remaining_ms": round(accepted["window_remaining_ms"], 1),
            "window_age_ms": round(accepted["window_age_ms"], 1),
            "ttl_ms": {name: detail["ttl_ms"] for name, detail in accepted["decoded"].items()},
            "clutch": {name: detail["command"].get("clutch")
                       for name, detail in accepted["decoded"].items()},
            "leader_rad": arm.get("leader_rad"),
            "pre_clip_rad": arm.get("pre_clip_rad"),
            "action_rad": {**dict(zip(arm["joint_names"], arm["positions_rad"])),
                           gripper["joint_name"]: gripper["position_rad"]},
            "state_rad": snapshot.get("positions_rad"),
            "state_raw": snapshot.get("servo_pulses"),
            "state_source": "servo_position_readback" if snapshot.get("available") else None,
            "feedback_age_s": snapshot.get("oldest_age_s"),
            "feedback_scan_span_s": snapshot.get("scan_span_s"),
            "confirmation": "driver_sent",
            "controller_acknowledged": "unavailable",
            "execution_observed": "unavailable",
            "hardware_effect": "unverified",
        }
        message = String()
        message.data = json.dumps(row, sort_keys=True, default=str)
        self.recording_publisher.publish(message)

    def _targets(self, decoded: dict[str, Any]) -> list[tuple[str, float]]:
        return [(entry["joint_name"], entry["position_rad"])
                for entry in six_axis_targets(decoded)]

    def _stop_following(self, reason: str) -> None:
        if self.admission is not None:
            self.admission.clear(reason)
        if not self._licence_was_present:
            return          # already reported this outage; count each transition once
        self._licence_was_present = False
        self.reengage_required = True
        self.counters["licences_lost"] += 1
        if self.driver.outputs_enabled:
            self.driver.stop(reason)
            self.counters["hold_frames"] += 1
        self._driver_was_enabled = False
        self.paused = self.paused or reason == "paused_by_request"

    def _require_explicit_reengage(self, reason: str, frame_seq: Any = None) -> None:
        """Park on an emitter failure; a still-live permission cannot restart motion."""
        self.reengage_required = True
        if self.admission is not None:
            self.admission.clear(reason)
        if self.driver.outputs_enabled:
            self.driver.stop(reason)
            self.counters["hold_frames"] += 1
        self._driver_was_enabled = False
        self.last_refusal = {"frame_seq": str(frame_seq), "reason": reason}
        if self.stream is not None:
            self.stream.push({"type": "receipt", "state": "refused",
                              "reason": "emitter_stopped", "detail": reason,
                              "frame_seq": frame_seq})
        self._write_status()

    def _on_feedback_owner_changed(self, previous: int | None, current: int,
                                   generation: int) -> None:
        """The feedback publisher is a new software instance: revoke immediately.

        Runs on the ROS executor thread (the readback subscription callback), the
        same thread as the control tick, so taking the control lock here cannot
        race a tick. Old targets and the admission queue are dropped, torque
        verification is invalidated, and motion waits for an explicit re-engage.
        """
        with self.control_lock:
            self.driver.invalidate_torque_verification()
            self.counters["feedback_owner_changes"] = \
                self.counters.get("feedback_owner_changes", 0) + 1
            self._require_explicit_reengage(
                f"feedback_owner_changed:{previous}->{current}:gen{generation}")

    # -- surface for the stream server ------------------------------------
    def on_hello(self, session_id, session_epoch, lease_token: str):
        # The tick thread owns the admission, the window ring and the pause flags; a hello
        # rewrites all three, so it takes the same lock instead of racing with a tick that
        # is mid-frame.
        with self.control_lock:
            return self._on_hello_locked(session_id, session_epoch, lease_token)

    def _on_hello_locked(self, session_id, session_epoch, lease_token: str):
        digest = self.licence.token_digest()
        if digest is None:
            return False, "no_licence_from_agent"
        from .teleop_stream import token_matches

        if not token_matches(lease_token, digest):
            return False, "lease_token_does_not_match_the_agent_mirror"
        if not self.licence.matches(session_id=session_id, session_epoch=session_epoch,
                                    bundle_hash=self.bundle_hash):
            return False, "epoch_or_bundle_not_licensed"
        if self.latched_fault:
            return False, f"fault_latched:{self.latched_fault}"
        current = self.licence.current()
        if self.reengage_required and (current is None or current.get("outputs_enabled") is not False):
            return False, "reengage_requires_output_revocation"
        # A new connection is a new generation: retire what the previous one buffered.
        # A hello after a lifted stop is the re-align the operator asked for.
        self.reengage_required = False
        self.stream_epoch = self.windows.new_stream_epoch("hello")
        self.open_epoch(session_id=session_id, session_epoch=session_epoch,
                        bundle_hash=self.bundle_hash, max_ttl_ms=self.max_ttl_ms)
        self.paused = False
        # The very first window has to be in the welcome: rotation above cleared it,
        # and a client that starts sending before the next tick would have no
        # generation to name.
        self.windows.issue(duration_ms=self.window_ms)
        # Written immediately: the agent's bridge reads this file to decide whether a
        # legacy action route may still emit, so a stale snapshot would be a window in
        # which two writers are live at once.
        self._write_status()
        return True, {"frame_protocol": 1}

    def on_frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        self.counters["frames_received"] += 1
        with self.control_lock:
            return self._on_frame_locked(frame)

    def _on_frame_locked(self, frame: dict[str, Any]) -> dict[str, Any]:
        if self.latched_fault:
            # After an emergency stop nothing is admitted either, so a sequence number
            # cannot be consumed by a frame that will never be emitted.
            return {"type": "receipt", "state": "refused",
                    "reason": f"fault_latched:{self.latched_fault}",
                    "frame_seq": frame.get("frame_seq")}
        if self.reengage_required or self.paused:
            return {"type": "receipt", "state": "refused", "reason": "reengage_required",
                    "frame_seq": frame.get("frame_seq")}
        admission = self.admission
        if admission is None:
            return {"type": "receipt", "state": "refused", "reason": "not_engaged",
                    "frame_seq": frame.get("frame_seq")}
        licence = self.licence.current()
        if licence is None:
            # Nothing may be admitted without a live licence behind it; answering
            # "admitted" and then dropping the frame at emission would tell the sender a
            # lie about the state of the arm.
            self.counters["frames_refused"] += 1
            self.last_refusal = {"frame_seq": str(frame.get("frame_seq")),
                                 "reason": "no_live_licence"}
            return {"type": "receipt", "state": "refused", "reason": "no_live_licence",
                    "detail": str(self.licence.revoked_by() if hasattr(self.licence, "revoked_by")
                                  else "licence missing"),
                    "frame_seq": frame.get("frame_seq")}
        expiry_ns = licence["deadline_ns"]
        try:
            accepted = admission.accept(frame, licence_expiry_ns=expiry_ns)
        except FrameError as error:
            self.last_refusal = {"frame_seq": str(frame.get("frame_seq")), "reason": error.reason}
            live = self.windows.describe() or {}
            self.refusal_lag_ms.append(round(
                (time.monotonic_ns() - live.get("issued_ns", time.monotonic_ns()))
                / MS_TO_NS, 1))
            self.counters["frames_refused"] += 1
            return {"type": "receipt", "state": "refused", "reason": error.reason,
                    "detail": error.detail, "frame_seq": frame.get("frame_seq")}
        return {"type": "receipt", "state": "admitted",
                "frame_seq": accepted["frame_seq"], "sample_seq": accepted["sample_seq"],
                "window_remaining_ms": round(accepted["window_remaining_ms"], 1),
                # Age of the named window on this clock when the frame arrived:
                # the sender tick plus the trip, which is what the window has to cover.
                "window_age_at_arrival_ms": round(accepted["window_age_ms"], 1),
                "deadline_in_ms": round(
                    (accepted["deadline_ns"] - time.monotonic_ns()) / MS_TO_NS, 1),
                "confirmation": "gateway_accepted",
                "controller_acknowledged": "unavailable",
                "execution_observed": "unavailable",
                "data_identity": self.data_identity}

    def on_preempt(self, action, reason):
        reason_text = str(reason or "requested")
        with self.control_lock:
            return self._preempt_locked(action, reason_text)

    def _preempt_locked(self, action, reason_text: str) -> dict[str, Any]:
        if action == "estop":
            self.latched_fault = "estop"
            # Recovery is the agent's to grant, not ours: it must clear the stop and
            # publish a newer licence generation before this process will emit again.
            licence = self.licence.current()
            self.latched_at_generation = None if licence is None else licence["control_generation"]
            if self.admission is not None:
                self.admission.clear("estop")
            receipt = self.driver.stop("estop")
            self._driver_was_enabled = False
            # Written only after the stop has actually been carried out.  A status file
            # that said "latched" while the driver was still enabled would let a reader
            # see a half-applied stop, and the acceptance harness reads exactly this file.
            self._write_status()
        elif action in ("pause_all", "pause"):
            self.paused = True
            if self.admission is not None:
                self.admission.clear("paused")
            receipt = self.driver.stop("paused_by_request")
            self._driver_was_enabled = False
        else:
            return {"type": "receipt", "state": "refused", "reason": "unknown_preempt",
                    "action": str(action)}
        self.counters["hold_frames"] += 1
        return {"type": "receipt", "state": "stopped", "action": str(action),
                "reason": reason_text, "latched": self.latched_fault,
                "hold_published": bool(receipt.get("hold_published")),
                "driver_sent": receipt.get("driver_sent"),
                "hardware_effect": "unverified"}

    def clear_fault(self) -> None:
        self.latched_fault = None

    # -- reporting ---------------------------------------------------------
    def state(self) -> dict[str, Any]:
        admission = self.admission.state() if self.admission else {}
        snapshot = self.readback.read_snapshot()
        return {
            "data_identity": self.data_identity,
            "configured_bundle_hash": self.bundle_hash,
            # Nothing produced by this offline runtime is training eligible.
            "training_eligible": False,
            "uptime_s": round(time.monotonic() - self.started, 2),
            "control_hz": self.control_hz,
            "window_ms": self.window_ms,
            "stream_epoch": self.stream_epoch,
            "paused": self.paused,
            "latched_fault": self.latched_fault,
            "reengage_required": self.reengage_required,
            "last_clear": self.last_clear,
            "outputs_enabled": self.driver.outputs_enabled,
            "trial_motion": getattr(self.driver, "trial_motion", None),
            "trial_first_frame_pending": getattr(
                self.driver, "trial_first_frame_pending", None),
            "homing_motion": getattr(self.driver, "homing_motion", None),
            "homing_first_frame_pending": getattr(
                self.driver, "homing_first_frame_pending", None),
            "homing_active_joint": getattr(self.driver, "homing_active_joint", None),
            "homing_last_target_raw": getattr(
                self.driver, "homing_last_target_raw", None),
            "publisher_subscriptions": getattr(
                self.driver, "last_subscription_count", None),
            "homing_torque_enabled": getattr(
                self.driver, "homing_torque_enabled", None),
            "homing_torque_state": getattr(
                self.driver, "homing_torque_state", None),
            "homing_torque_results": getattr(
                self.driver, "homing_torque_results", None),
            "homing_torque_error": getattr(
                self.driver, "homing_torque_error", None),
            "torque_publish_count": getattr(
                self.driver, "torque_publish_count", None),
            "torque_servo_ids": getattr(self.driver, "torque_servo_ids", None),
            "publish_count": self.driver.publish_count,
            "step_clamp_count": getattr(self.driver, "step_clamp_count", 0),
            "last_step_clamp": getattr(self.driver, "last_step_clamp", None),
            "position_pid": getattr(self.driver, "position_pid", None),
            "last_pid": getattr(self.driver, "last_pid", None),
            "emit_intervals": percentiles(list(self.emit_intervals_ms)),
            "stop_count": self.driver.stop_count,
            "counters": dict(self.counters),
            "effective_emit_hz": (round(self.counters["frames_emitted"]
                                        / max(0.001, time.monotonic() - self.started), 2)),
            "admission": admission,
            "last_refusal": self.last_refusal,
            "licence": self.licence.state(),
            # The store already measures age on this clock; re-deriving it here from
            # a sample field that is not a timestamp produced a nonsense number.
            "feedback": {
                "available": snapshot.get("available"),
                "reason": snapshot.get("reason"),
                "last_error": snapshot.get("last_error"),
                "scan_span_s": snapshot.get("scan_span_s"),
                "scan_period_s": snapshot.get("scan_period_s"),
                "sample_seq": snapshot.get("scan_sequence"),
                "oldest_age_s": snapshot.get("oldest_age_s"),
                "stale_after_s": snapshot.get("stale_after_s"),
                "ages_ms": {name: round(float(age) * 1000, 1)
                            for name, age in (snapshot.get("age_s") or {}).items()},
                "raw": snapshot.get("servo_pulses"),
                "position_rad": snapshot.get("positions_rad"),
                "ingests": snapshot.get("ingests"),
                "rejects": snapshot.get("rejects"),
                "publisher_instance": snapshot.get("publisher_instance"),
                "publisher_generation": snapshot.get("publisher_generation"),
                "publisher_identity_kind": snapshot.get("publisher_identity_kind"),
            },
        }

    def _write_status(self) -> None:
        document = self.state()
        if self.stream is not None:
            document["stream"] = self.stream.state()
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
                             encoding="utf-8")
        os.replace(temporary, self.status_path)


def build(profile: dict[str, Any], args: argparse.Namespace) -> tuple[Node, CarControlRuntime,
                                                                      LicenceMirror, StreamServer]:
    node = Node("lelab_car_control")
    feedback = profile["follower_feedback"]
    qos = QoSProfile(reliability=QoSReliabilityPolicy.RELIABLE,
                     history=QoSHistoryPolicy.KEEP_LAST, depth=1)
    readback = RosServoFeedbackTopicDriver(
        node, Int32MultiArray,
        topic=args.readback_topic,
        calibration_hash=args.bundle_hash or None,
        stale_after_s=float(feedback["stale_after_s"]),
        max_scan_span_s=float(feedback["max_scan_span_s"]),
        scan_period_s=float(feedback["scan_period_s"]))
    publisher = node.create_publisher(ServosPosition, args.output_topic, qos)
    state_publisher = node.create_publisher(
        SetBusServoState, args.state_topic, 10)
    torque_readback = TorqueStateReadback(node, GetBusServoState, service=args.state_service)
    recording = node.create_publisher(String, args.recording_topic, 10) if args.recording_topic else None
    limits = {name: tuple(values) for name, values in
              profile["limits"]["joint_positions_rad"].items()}
    teleop = profile["teleop"]
    driver = RosServoOutputDriver(
        node, publisher,
        servos_position_type=ServosPosition,
        servo_position_type=ServoPosition,
        readback=readback, limits_rad=limits,
        max_step_rad_s=float(teleop["max_step_rad_s"]),
        max_silence_s=float(teleop["output_watchdog_s"]),
        duration_s=float(teleop["output_duration_s"]),
        command_topic=args.output_topic,
        trial_motion=teleop.get("trial_motion"),
        homing_motion=teleop.get("homing_motion"),
        servo_state_publisher=state_publisher,
        bus_servo_state_type=BusServoState,
        set_bus_servo_state_type=SetBusServoState,
        torque_readback=torque_readback,
        position_pid=teleop.get("position_pid"))
    licence = LicenceMirror(Path(args.licence_socket))
    licence.start()
    runtime = CarControlRuntime(
        node, profile=profile, control_hz=args.control_hz, window_ms=args.window_ms,
        licence=licence, readback=readback, driver=driver,
        data_identity=args.data_identity, status_path=Path(args.status_path),
        bundle_hash=args.bundle_hash, max_ttl_ms=args.max_ttl_ms,
        recording_publisher=recording)
    readback.on_owner_changed = runtime._on_feedback_owner_changed
    server = StreamServer({
        "on_hello": runtime.on_hello,
        "on_frame": runtime.on_frame,
        "on_preempt": runtime.on_preempt,
        "window_provider": lambda: (runtime.windows.describe() or {}),
        "stream_epoch_provider": lambda: runtime.stream_epoch,
    }, host=args.stream_host, port=args.stream_port, idle_timeout_s=0.2)
    runtime.stream = server
    return node, runtime, licence, server


def validate_hardware_trial(profile: dict[str, Any], args: argparse.Namespace) -> None:
    """Fail closed if this real single-axis candidate is launched with wrong wiring."""
    profile_name = profile.get("profile")
    if profile_name == "hardware_hybrid_follow_v1":
        runtime = profile.get("runtime") or {}
        teleop = profile.get("teleop") or {}
        safety = profile.get("safety") or {}
        if (runtime.get("mode") != "hardware_hybrid_follow"
                or runtime.get("output_transport") != "frame_stream"
                or runtime.get("actuator_outputs") is not True
                or runtime.get("data_source") != "hardware"
                or teleop.get("chassis_locked") is not True
                or teleop.get("trial_motion") is not None
                or teleop.get("homing_motion") is not None
                or float(teleop.get("max_step_rad_s", 0)) > 0.15
                or not 0 < float(teleop.get("max_step_rad_s", 0))
                or args.data_identity != "hardware"
                or args.stream_host not in ("127.0.0.1", "localhost")
                or args.output_topic != "/ros_robot_controller/bus_servo/set_position"
                or args.readback_topic != "/lelab/ros_robot_controller/servo_readback"
                or args.state_topic != "/ros_robot_controller/bus_servo/set_state"
                or args.state_service != "/ros_robot_controller/bus_servo/get_state"
                or not args.bundle_hash
                or args.bundle_hash != safety.get("calibration_bundle_hash")):
            raise ValueError("hardware_follow_configuration_mismatch")
        return
    if profile_name == "hardware_hybrid_homing_v1":
        runtime = profile.get("runtime") or {}
        teleop = profile.get("teleop") or {}
        safety = profile.get("safety") or {}
        homing = teleop.get("homing_motion") or {}
        if (runtime.get("mode") != "hardware_hybrid_homing"
                or runtime.get("output_transport") != "frame_stream"
                or runtime.get("actuator_outputs") is not True
                or runtime.get("data_source") != "hardware"
                or teleop.get("chassis_locked") is not True
                or teleop.get("trial_motion") is not None
                or homing.get("axis_order") != [
                    "joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"]
                or homing.get("targets_raw") != {
                    "joint1": 531, "joint2": 492, "joint3": 525,
                    "joint4": 502, "joint5": 510, "r_joint": 457}
                or homing.get("tolerance_raw") != 5
                or homing.get("locked_tolerance_raw") != 5
                or homing.get("first_frame_tolerance_raw") != 20
                or homing.get("max_step_raw") != 20
                or homing.get("enable_torque") is not True
                or float(teleop.get("max_step_rad_s", 0)) > 0.15
                or not 0 < float(teleop.get("max_step_rad_s", 0))
                or args.data_identity != "hardware"
                or args.stream_host not in ("127.0.0.1", "localhost")
                or args.output_topic != "/ros_robot_controller/bus_servo/set_position"
                or args.readback_topic != "/lelab/ros_robot_controller/servo_readback"
                or args.state_topic != "/ros_robot_controller/bus_servo/set_state"
                or args.state_service != "/ros_robot_controller/bus_servo/get_state"
                or not args.bundle_hash
                or args.bundle_hash != safety.get("calibration_bundle_hash")):
            raise ValueError("hardware_homing_configuration_mismatch")
        return
    expected_axis = {"hardware_hybrid_arm_trial_v1": "joint1",
                     "hardware_hybrid_arm2_trial_v1": "joint2",
                     "hardware_hybrid_arm3_trial_v1": "joint3",
                     "hardware_hybrid_arm4_trial_v1": "joint4",
                     "hardware_hybrid_arm5_trial_v1": "joint5"}.get(profile.get("profile"))
    if expected_axis is None:
        return
    runtime = profile.get("runtime") or {}
    teleop = profile.get("teleop") or {}
    safety = profile.get("safety") or {}
    trial = teleop.get("trial_motion") or {}
    if (runtime.get("mode") != "hardware_hybrid_arm_trial"
            or runtime.get("output_transport") != "frame_stream"
            or runtime.get("actuator_outputs") is not True
            or runtime.get("data_source") != "hardware"
            or teleop.get("chassis_locked") is not True
            or trial.get("allowed_joints") != [expected_axis]
            or trial.get("max_displacement_rad") != 0.08
            or trial.get("first_frame_tolerance_rad") != 0.02
            or trial.get("locked_tolerance_rad") != 0.02
            or float(teleop.get("max_step_rad_s", 0)) > 0.15
            or not 0 < float(teleop.get("max_step_rad_s", 0))
            or args.data_identity != "hardware"
            or args.stream_host not in ("127.0.0.1", "localhost")
            or args.output_topic != "/ros_robot_controller/bus_servo/set_position"
            or args.readback_topic != "/lelab/ros_robot_controller/servo_readback"
            or not args.bundle_hash or args.bundle_hash != safety.get("calibration_bundle_hash")):
        raise ValueError("hardware_trial_configuration_mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--stream-host", default="127.0.0.1")
    parser.add_argument("--stream-port", type=int, default=18121)
    parser.add_argument("--licence-socket", default="/out/state/licence.sock")
    parser.add_argument("--status-path", default="/out/state/car_control_status.json")
    parser.add_argument("--output-topic", default="/lelab_offline/ros_robot_controller/bus_servo/set_position")
    parser.add_argument("--readback-topic", default="/lelab_offline/lelab/ros_robot_controller/servo_readback")
    parser.add_argument("--state-topic", default="/lelab_offline/ros_robot_controller/bus_servo/set_state")
    parser.add_argument("--state-service", default="/lelab_offline/ros_robot_controller/bus_servo/get_state")
    parser.add_argument("--control-hz", type=float, default=10.0)
    parser.add_argument("--window-ms", type=int, default=150)
    parser.add_argument("--max-ttl-ms", type=int, default=200)
    parser.add_argument("--recording-topic", default="/lelab_offline/recording/teleop")
    parser.add_argument("--bundle-hash", default="")
    parser.add_argument("--data-identity", default="simulated_offline")
    args = parser.parse_args()
    if "serial" in sys.modules:
        print("refusing to start: a serial module is loaded in this process", file=sys.stderr)
        return 2
    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    try:
        validate_hardware_trial(profile, args)
    except (TypeError, ValueError) as error:
        print(f"refusing to start: {error}", file=sys.stderr)
        return 2
    ceiling = float(profile["teleop"].get("control_hz_max", 20.0))
    if not 0 < args.control_hz <= ceiling:
        print(f"control-hz {args.control_hz} exceeds the profile ceiling {ceiling}", file=sys.stderr)
        return 2
    if args.max_ttl_ms > 200:
        print("the command TTL ceiling stays at the car gate's 200 ms", file=sys.stderr)
        return 2
    rclpy.init()
    node, runtime, licence, server = build(profile, args)
    server.start()
    print(json.dumps({"started": True, "stream_port": server.port,
                      "control_hz": args.control_hz, "data_identity": args.data_identity,
                      "output_topic": args.output_topic,
                      "readback_topic": args.readback_topic}), flush=True)
    signal.signal(signal.SIGTERM, lambda *_: rclpy.shutdown())
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, RuntimeError):
        pass
    finally:
        server.stop()
        licence.stop()
        node.destroy_node()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
