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
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from rclpy.signals import SignalHandlerOptions
from std_msgs.msg import Int32MultiArray, String

from ros_robot_controller_msgs.msg import (
    BusServoState, ServoPosition, ServosPosition, SetBusServoState,
)
from ros_robot_controller_msgs.srv import GetBusServoState

from .licence_ipc import LicenceMirror
from . import local_gamepad_base as gamepad_base
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


class BoundedRecordingPublisher:
    """Record rows through a bounded queue so a slow subscriber cannot stall a tick.

    The ROS publisher call itself is usually cheap, but a blocked DDS writer or a
    broken recorder must never turn the control tick into a recording wait.  A
    full queue drops the oldest row and counts it; the recorder's own required
    topic gate still decides whether the episode is usable.
    """

    def __init__(self, publisher: Any, *, maxlen: int = 256) -> None:
        self.publisher = publisher
        self.queue: queue.Queue = queue.Queue(maxsize=max(1, int(maxlen)))
        self.stop_event = threading.Event()
        self.dropped = 0
        self.published = 0
        self.last_error: str | None = None
        self.thread = threading.Thread(target=self._run, name="teleop-recording-publisher",
                                       daemon=True)
        self.thread.start()

    def publish(self, message: Any) -> None:
        try:
            self.queue.put_nowait(message)
            return
        except queue.Full:
            pass
        try:
            self.queue.get_nowait()
            self.dropped += 1
        except queue.Empty:
            pass
        try:
            self.queue.put_nowait(message)
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                message = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.publisher.publish(message)
                self.published += 1
            except Exception as error:  # noqa: BLE001 - recording is not the control path
                self.last_error = f"{type(error).__name__}:{error}"

    def stop(self, timeout_s: float = 1.0) -> None:
        """Stop promptly; rows still queued are counted rather than flushed forever."""
        self.stop_event.set()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=max(0.05, timeout_s))
        while True:
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except queue.Empty:
                break

    def state(self) -> dict[str, Any]:
        return {"queued": self.queue.qsize(), "published": self.published,
                "dropped": self.dropped, "last_error": self.last_error,
                "stopped": not self.thread.is_alive()}


class CarControlRuntime:
    """Tick-driven control: admit newest frame, re-check life, emit one six-axis write."""

    def __init__(self, node: Node, *, profile: dict[str, Any], control_hz: float,
                 window_ms: int, licence: LicenceMirror, readback: RosServoFeedbackTopicDriver,
                 driver: RosServoOutputDriver, data_identity: str, status_path: Path,
                 bundle_hash: str = "", max_ttl_ms: int = 200,
                 recording_publisher=None, typed_control=None,
                 typed_feedback=None):
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
        self.launch_bundle_hash = bundle_hash
        self.bundle_hash_source = "launch_argument"
        self.last_bundle_adoption: dict[str, Any] | None = None
        self.max_ttl_ms = max_ttl_ms
        # Two clocks, named: the recorded stamp has to match what the recorder and the bag
        # use, and the control loop's own deadlines are measured on this process' monotonic
        # clock.  They are never compared to each other.
        runtime = profile.get("runtime", {}) or {}
        self.clock_domain = str(runtime.get("clock_domain") or "car_system_time")
        self.control_clock_domain = str(runtime.get("control_clock_domain")
                                       or "car_process_monotonic")
        self.recording_publisher = recording_publisher
        self.typed_control = typed_control
        self.typed_feedback = typed_feedback
        self.transport = str(runtime.get("output_transport") or "frame_stream")
        self.transport_label = ("dds_v1" if self.transport in ("dds", "dds_v1")
                                else "frame_stream_v1")
        configured = runtime.get("allowed_transports")
        if configured is None:
            self.allowed_transports = [self.transport_label]
        else:
            self.allowed_transports = [self._normalize_transport(value)
                                       for value in configured]
        self.active_transport: str | None = None
        self.dds_peer: str | None = None
        self.dds_transport_epoch: str | None = None
        self.dds_receiver = None
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
        self.last_emit_stream_epoch: int | None = None
        self.local_base = None
        self.gamepad_preview = None
        self.gamepad_input = None
        self.gamepad_generation = None
        self.base_motor_topic = None
        self.base_trace = None
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
    @staticmethod
    def _normalize_transport(value: Any) -> str:
        return {"frame_stream": "frame_stream_v1",
                "frame_stream_v1": "frame_stream_v1",
                "dds": "dds_v1", "dds_v1": "dds_v1",
                "legacy": "legacy_http",
                "legacy_http": "legacy_http"}.get(str(value), str(value))

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
            try:
                self._tick_locked()
                self._tick_base()
            except Exception:
                self._stop_base("control_tick_failed")
                raise

    def _on_local_joy(self, message):
        stamp = message.header.stamp
        with self.control_lock:
            self.local_base.ingest(message.axes, message.buttons, now_ns=time.monotonic_ns(),
                                   source_ns=stamp.sec * 1_000_000_000 + stamp.nanosec,
                                   ros_now_ns=self.node.get_clock().now().nanoseconds)

    def _stop_base(self, reason):
        base = getattr(self, "local_base", None)
        if base is not None:
            try:
                base.stop(reason, time.monotonic_ns())
            except Exception as exc:
                # The isolated base publisher must not block the arm stop.
                base.reason = "base_zero_publish_failed"
                self.node.get_logger().error(f"{base.reason}: {type(exc).__name__}")

    def _tick_base(self):
        base = getattr(self, "local_base", None)
        preview = getattr(self, "gamepad_preview", None)
        if base is None and preview is None:
            return
        reader = getattr(self, "gamepad_input", None)
        raw_gamepad = None if reader is None else reader.snapshot()
        now = time.monotonic_ns()
        lease = self.licence.current() or {}
        context = self.licence.context() or {}
        if (not lease or str(context.get("session_epoch")) != str(lease.get("session_epoch"))
                or context.get("calibration_bundle_hash") != lease.get("calibration_bundle_hash")):
            context = {}
        admission = self.admission
        scope = None
        reason = "base_licence_missing"
        if base is not None and base.trial:
            # Chassis-only trial.  There are no arm frames and no episode to bind to,
            # so the licence itself is the authority: a base permit inside a bound
            # session/bundle.  The arm has no driver here and stays locked; the base
            # keeps its own owner/HID/neutral gates inside LocalGamepadBase.
            if ((lease.get("permits") or {}).get("base") is True
                    and lease.get("estop_latched") is not True
                    and lease.get("session_id") and lease.get("session_epoch") is not None
                    and lease.get("calibration_bundle_hash") == self.bundle_hash):
                reason = "base_trial_waiting_unlock"
                if not (self.paused or self.latched_fault or self.reengage_required):
                    scope = (lease["session_id"], lease["session_epoch"], self.bundle_hash,
                             self.stream_epoch, "base_trial")
            else:
                reason = "base_trial_licence_incomplete"
        elif (base is not None and admission is not None and lease.get("outputs_enabled") is True
                and (lease.get("permits") or {}).get("base") is True
                and lease.get("session_id") == admission.session_id
                and str(lease.get("session_epoch")) == str(admission.session_epoch)
                and lease.get("calibration_bundle_hash") == self.bundle_hash):
            reason = "base_requires_live_arm_follow"
            if (not (self.paused or self.latched_fault or self.reengage_required)
                    and self.driver.outputs_enabled and self.last_emit_mono is not None
                    and self.last_emit_stream_epoch == self.stream_epoch
                    and 0 <= now / 1e9 - self.last_emit_mono < base.timeout_ns / 1e9
                    and self.readback.read_snapshot().get("available") is True):
                reason = "base_locked_outside_drive"
                if (context.get("phase") == "drive" and context.get("episode_id")
                        and context.get("clock_epoch") and context.get("session_id")):
                    scope = (admission.session_id, admission.session_epoch, self.bundle_hash,
                             self.stream_epoch, context.get("session_id"), context["episode_id"],
                             context["clock_epoch"])
        if base is not None:
            odom_episode = tuple(context.get(key) for key in ("session_id", "episode_id", "clock_epoch"))
            if all(odom_episode):
                base.set_odom_episode(odom_episode)
            if raw_gamepad is not None:
                input_scope, input_reason = self._joint_base_input(base, raw_gamepad, now, scope, reason)
                if scope is not None and input_scope is None:
                    base.stop(input_reason, now)
                scope, reason = input_scope, input_reason
            base.tick(now, scope, reason)
        row = {**(base or preview).status(now), "kind": "base_gamepad_diagnostic", "source": "gamepad",
               "stamp_ns": self.node.get_clock().now().nanoseconds, "control_mono_ns": now,
               "clock_domain": self.clock_domain, "session_id": lease.get("session_id"),
               "session_epoch": lease.get("session_epoch"), "recording_session_id": context.get("session_id"),
               "calibration_bundle_hash": lease.get("calibration_bundle_hash"),
               **{key: context.get(key) for key in ("episode_id", "phase", "clock_epoch", "context_seq")}}
        if raw_gamepad is not None:
            row["raw_gamepad"] = raw_gamepad
        if self.base_trace is not None:
            self.base_trace.publish(String(data=json.dumps(row, allow_nan=False)))

    def _joint_base_input(self, base, sample, now, scope, reason):
        generation = sample["generation"]
        changed = self.gamepad_generation is not None and generation != self.gamepad_generation
        self.gamepad_generation = generation
        if scope is None:
            return scope, reason
        if changed or not sample["connected"]:
            return None, "base_receiver_disconnected_or_changed"
        axes, buttons = sample["axes_raw"], sample["buttons_raw"]
        report_ns = sample.get("usb_report_mono_ns")
        if (report_ns is None or not 0 <= now - report_ns < base.timeout_ns
                or any(axis.index >= len(axes) for axis in base.axes)
                or any(value is None for value in (*axes, *buttons))):
            return None, "base_waiting_for_usb_input"
        owners = self.node.get_subscriptions_info_by_topic(self.base_motor_topic)
        if (self.node.count_publishers(self.base_motor_topic) != 1
                or sum(owner.node_name == "ros_robot_controller" for owner in owners) != 1):
            return None, "base_motor_owner_or_publisher_conflict"
        # HID receive time describes USB transport. RF state remains unknown.
        if base.scope == scope and (base.source_stamp_ns is None or report_ns > base.source_stamp_ns):
            base.ingest([max(-1.0, value / 32767) for value in axes], buttons,
                        now_ns=now, source_ns=report_ns, ros_now_ns=now,
                        source_clock="car_monotonic_usb_receive")
        return scope, reason

    def _adopt_licensed_bundle_locked(self) -> bool:
        """Adopt a new authenticated mapping bundle while outputs are closed.

        The upper host can save a new mapping version without restarting either
        process. The car still needs the same bundle identity for measured
        feedback and for every command frame. Adoption is allowed only from the
        agent-authenticated licence after output permission is explicitly off,
        and it always parks the old target state for an explicit re-engage.
        """
        licence = self.licence.current()
        if licence is None:
            return True
        candidate = licence.get("calibration_bundle_hash")
        if (not isinstance(candidate, str) or not candidate
                or candidate == "unbound" or candidate == self.bundle_hash):
            return True
        if licence.get("outputs_enabled") is not False:
            self._require_explicit_reengage(
                "licence_bundle_changed_with_outputs_enabled")
            return False
        setter = getattr(self.readback, "set_calibration_hash", None)
        if not callable(setter):
            self._require_explicit_reengage(
                "readback_bundle_adoption_unavailable")
            return False
        previous = self.bundle_hash
        if self.admission is not None:
            self.admission.clear("licence_bundle_changed")
        if self.driver.outputs_enabled:
            self.driver.stop("licence_bundle_changed")
            self.counters["hold_frames"] += 1
        self._driver_was_enabled = False
        self.active_transport = None
        self.dds_peer = None
        self.dds_transport_epoch = None
        self.admission = None
        self.stream_epoch = self.windows.new_stream_epoch("bundle_changed")
        self.bundle_hash = candidate
        self.bundle_hash_source = "authenticated_licence"
        setter(candidate)
        if self.typed_feedback is not None:
            self.typed_feedback.calibration_hash = candidate
        self.reengage_required = True
        self.counters["bundle_adoptions"] = (
            self.counters.get("bundle_adoptions", 0) + 1)
        self.last_bundle_adoption = {
            "previous": previous,
            "current": candidate,
            "source": self.bundle_hash_source,
            "action_required": "explicit_hello_and_reengage",
        }
        self._write_status()
        return True

    def _tick_locked(self) -> None:
        self.counters["ticks"] += 1
        if not self._adopt_licensed_bundle_locked():
            self.counters["empty_ticks"] += 1
            return
        window = self.windows.issue(duration_ms=self.window_ms)
        self.counters["window_issues"] += 1
        if self.stream is not None:
            self.stream.push_window({"window_id": window["window_id"],
                                     "stream_epoch": window["stream_epoch"],
                                     "duration_ms": window["duration_ms"],
                                     "window_remaining_ms": round(self.windows.remaining_ms(), 1)})
        typed_control = getattr(self, "typed_control", None)
        if typed_control is not None:
            admission = self.admission
            ready = (admission is not None and not self.latched_fault
                     and not self.reengage_required and not self.paused)
            typed_control.publish_window(
                session_id=None if admission is None else admission.session_id,
                session_epoch=None if admission is None else admission.session_epoch,
                stream_epoch=self.stream_epoch, window=window,
                remaining_ms=self.windows.remaining_ms(),
                command_transport_ready=ready,
                reason="ready" if ready else "gate_closed")
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
        still_valid, why = self._emission_identity_ok(
            pending["frame"], pending.get("transport"))
        if not still_valid:
            self.counters["frames_dropped_before_emit"] = \
                self.counters.get("frames_dropped_before_emit", 0) + 1
            self.last_refusal = {"frame_seq": str(pending["frame"].get("frame_seq")),
                                 "reason": why}
            self._stop_following(why)
            return
        targets = self._targets(pending["decoded"])
        try:
            dispatch = self.driver.submit_frame(targets)
        except (RuntimeError, ValueError) as error:
            self.counters["frames_refused"] += 1
            self.last_refusal = {"frame_seq": str(pending["frame"]["frame_seq"]),
                                 "reason": str(error)}
            if str(error) == "trusted_feedback_unavailable":
                # The admitted target is dropped; it is never replayed. Keep the
                # output watchdog armed and wait for the next fresh frame. If the
                # feedback outage persists, _watchdog() stops the driver and the next
                # tick still requires an explicit re-engage.
                self.counters["frames_deferred_feedback"] = (
                    self.counters.get("frames_deferred_feedback", 0) + 1)
                if admission is not None:
                    admission.clear("transient_feedback_requires_fresh_frame")
                self._write_status()
                return
            self._require_explicit_reengage(str(error), pending["frame"]["frame_seq"])
            self._publish_receipt(pending["frame"], state="refused", reason="emitter_stopped",
                                  detail=str(error), admitted=True, accepted=pending)
            return
        now_mono = time.monotonic()
        if self.last_emit_mono is not None:
            self.emit_intervals_ms.append(round((now_mono - self.last_emit_mono) * 1000, 2))
        self.counters["frames_emitted"] += 1
        self.last_emit_mono = now_mono
        self.last_emit_stream_epoch = self.stream_epoch
        self.last_refusal = None
        self._record_frame(pending, dispatch)
        self._publish_receipt(pending["frame"], state="emitted", admitted=True,
                              emitted=True, accepted=pending)

    def _emission_identity_ok(self, frame: dict[str, Any],
                              transport: str | None = None) -> tuple[bool, str]:
        """Compare one admitted frame against the licence and generation live right now."""
        if (transport is not None and self.active_transport is not None
                and transport != self.active_transport):
            return False, "transport_changed_before_emit"
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

    def _record_frame(self, accepted: dict[str, Any], dispatch: dict[str, Any] | None = None) -> None:
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
            "feedback_limits_version": (getattr(self.driver, "feedback_limits", {}) or {}).get("version"),
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
            "feedback_owner_instance": snapshot.get("publisher_instance"),
            "feedback_scan_sequence": snapshot.get("scan_sequence"),
            "feedback_consumer": snapshot.get("consumer"),
            "feedback_owner_health": {
                key: (snapshot.get("owner_health") or {}).get(key)
                for key in ("owner_instance", "scans_ok", "scans_failed",
                            "consecutive_failures", "last_success_seq",
                            "max_scan_interval_s", "first_failure", "last_failure",
                            "last_recovery", "write_failures")
            } if snapshot.get("owner_health") else None,
            "confirmation": "driver_sent",
            "controller_acknowledged": "unavailable",
            "execution_observed": "unavailable",
            "hardware_effect": "unverified",
        }
        if getattr(self.driver, "hold_outside_command_range", False) and dispatch is not None:
            row["published_positions_raw"] = dispatch["published_positions_raw"]
            row["uncommanded_joints"] = dispatch["uncommanded_joints"]
        message = String()
        message.data = json.dumps(row, sort_keys=True, default=str)
        self.recording_publisher.publish(message)

    def _targets(self, decoded: dict[str, Any]) -> list[tuple[str, float]]:
        return [(entry["joint_name"], entry["position_rad"])
                for entry in six_axis_targets(decoded)]

    def _stop_following(self, reason: str) -> None:
        self._stop_base(reason)
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
        self._publish_receipt(None, state="stopped", reason=reason)

    def _require_explicit_reengage(self, reason: str, frame_seq: Any = None) -> None:
        """Park on an emitter failure; a still-live permission cannot restart motion."""
        self._stop_base(reason)
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
        if "frame_stream_v1" not in self.allowed_transports:
            return False, "transport_not_enabled_in_profile:frame_stream_v1"
        if self.active_transport == "dds_v1":
            current = self.licence.current() or {}
            admission = self.admission
            same = (admission is not None
                    and str(current.get("session_id")) == str(admission.session_id)
                    and str(current.get("session_epoch")) == str(admission.session_epoch))
            if same:
                return False, "transport_already_active:dds_v1"
            if current.get("outputs_enabled") is not False:
                return False, "transport_switch_requires_output_revocation"
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
        self.active_transport = "frame_stream_v1"
        self.dds_peer = None
        self.dds_transport_epoch = None
        # The very first window has to be in the welcome: rotation above cleared it,
        # and a client that starts sending before the next tick would have no
        # generation to name.
        self.windows.issue(duration_ms=self.window_ms)
        # Written immediately: the agent's bridge reads this file to decide whether a
        # legacy action route may still emit, so a stale snapshot would be a window in
        # which two writers are live at once.
        self._write_status()
        return True, {"frame_protocol": 1}

    def _publish_receipt(self, frame: Any, *, state: str, reason: str = "",
                         detail: str = "", admitted: bool = False,
                         emitted: bool = False, accepted: dict[str, Any] | None = None,
                         transport: str | None = None) -> None:
        typed_control = getattr(self, "typed_control", None)
        if typed_control is None:
            return
        payload = frame if isinstance(frame, dict) else {}
        typed_control.publish_receipt(
            transport=(transport or self.active_transport or self.transport_label),
            session_id=payload.get("session_id"),
            session_epoch=payload.get("session_epoch"),
            stream_epoch=payload.get("stream_epoch", self.stream_epoch),
            frame_seq=payload.get("frame_seq"),
            sample_seq=payload.get("sample_seq"),
            state=state, reason=reason, detail=detail, admitted=admitted,
            publisher_sent=True, emitted=emitted,
            received_mono_ns=(None if accepted is None else accepted.get("received_ns")),
            deadline_mono_ns=(None if accepted is None else accepted.get("deadline_ns")),
            window_remaining_ms=(None if accepted is None
                                 else accepted.get("window_remaining_ms")),
            window_age_ms=(None if accepted is None else accepted.get("window_age_ms")))

    def on_dds_hello(self, *, session_id, session_epoch, bundle_hash: str,
                     sender_id: str, transport_epoch: str):
        """Open a DDS epoch after the receiver authenticated the hello ticket.

        The raw lease token is not presented: the agent's licence mirror is the
        authority and the HMAC ticket proves the bridge identity. The one-session
        one-transport rule is enforced here, not only in the upper selector.
        """
        with self.control_lock:
            return self._on_dds_hello_locked(
                session_id=session_id, session_epoch=session_epoch,
                bundle_hash=bundle_hash, sender_id=sender_id,
                transport_epoch=transport_epoch)

    def _on_dds_hello_locked(self, *, session_id, session_epoch, bundle_hash: str,
                             sender_id: str, transport_epoch: str):
        if "dds_v1" not in self.allowed_transports:
            return False, "transport_not_enabled_in_profile:dds_v1"
        current = self.licence.current()
        if current is None:
            return False, "no_licence_from_agent"
        if bundle_hash != self.bundle_hash or not self.licence.matches(
                session_id=session_id, session_epoch=session_epoch,
                bundle_hash=self.bundle_hash):
            return False, "epoch_or_bundle_not_licensed"
        if self.latched_fault:
            return False, f"fault_latched:{self.latched_fault}"
        admission = self.admission
        if (self.active_transport == "dds_v1" and admission is not None
                and str(admission.session_id) == str(session_id)
                and str(admission.session_epoch) == str(session_epoch)
                and self.dds_peer == sender_id
                and self.dds_transport_epoch == transport_epoch):
            # A repeated hello from the same authenticated peer is idempotent:
            # rotating the epoch here would discard a live sequence for no reason.
            self.windows.issue(duration_ms=self.window_ms)
            self._write_status()
            return True, {"frame_protocol": 1, "stream_epoch": self.stream_epoch,
                          "idempotent": True}
        if self.active_transport == "frame_stream_v1" and admission is not None:
            same = (str(admission.session_id) == str(session_id)
                    and str(admission.session_epoch) == str(session_epoch))
            if same:
                return False, "transport_already_active:frame_stream_v1"
            if current.get("outputs_enabled") is not False:
                return False, "transport_switch_requires_output_revocation"
        if self.reengage_required and current.get("outputs_enabled") is not False:
            return False, "reengage_requires_output_revocation"
        self.reengage_required = False
        self.stream_epoch = self.windows.new_stream_epoch("dds_hello")
        self.open_epoch(session_id=session_id, session_epoch=session_epoch,
                        bundle_hash=self.bundle_hash, max_ttl_ms=self.max_ttl_ms)
        self.paused = False
        self.active_transport = "dds_v1"
        self.dds_peer = str(sender_id)
        self.dds_transport_epoch = str(transport_epoch)
        self.windows.issue(duration_ms=self.window_ms)
        self._write_status()
        return True, {"frame_protocol": 1, "stream_epoch": self.stream_epoch}

    def on_frame(self, frame: dict[str, Any],
                 transport: str = "frame_stream_v1") -> dict[str, Any]:
        self.counters["frames_received"] += 1
        with self.control_lock:
            return self._on_frame_locked(frame, transport)

    def _on_frame_locked(self, frame: dict[str, Any],
                         transport: str = "frame_stream_v1") -> dict[str, Any]:
        if self.active_transport is not None and transport != self.active_transport:
            self.counters["frames_refused"] += 1
            self.last_refusal = {"frame_seq": str(frame.get("frame_seq")),
                                 "reason": "transport_mismatch"}
            self._publish_receipt(frame, state="refused", reason="transport_mismatch",
                                  detail=f"active={self.active_transport}",
                                  transport=transport)
            return {"type": "receipt", "state": "refused",
                    "reason": "transport_mismatch",
                    "frame_seq": frame.get("frame_seq")}
        if self.latched_fault:
            # After an emergency stop nothing is admitted either, so a sequence number
            # cannot be consumed by a frame that will never be emitted.
            self._publish_receipt(
                frame, state="refused", reason=f"fault_latched:{self.latched_fault}")
            return {"type": "receipt", "state": "refused",
                    "reason": f"fault_latched:{self.latched_fault}",
                    "frame_seq": frame.get("frame_seq")}
        if self.reengage_required or self.paused:
            self._publish_receipt(
                frame, state="refused", reason="reengage_required")
            return {"type": "receipt", "state": "refused", "reason": "reengage_required",
                    "frame_seq": frame.get("frame_seq")}
        admission = self.admission
        if admission is None:
            self._publish_receipt(frame, state="refused", reason="not_engaged")
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
            self._publish_receipt(frame, state="refused", reason="no_live_licence",
                                  detail="licence missing")
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
            self._publish_receipt(frame, state="refused", reason=error.reason,
                                  detail=error.detail)
            return {"type": "receipt", "state": "refused", "reason": error.reason,
                    "detail": error.detail, "frame_seq": frame.get("frame_seq")}
        self._publish_receipt(frame, state="admitted", admitted=True, accepted=accepted,
                              transport=transport)
        accepted["transport"] = transport
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
        if action in ("estop", "pause", "pause_all"):
            self._stop_base(reason_text)
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
        self._publish_receipt(
            None, state="stopped", reason=reason_text,
            detail=f"action={action};latched={self.latched_fault}")
        return {"type": "receipt", "state": "stopped", "action": str(action),
                "reason": reason_text, "latched": self.latched_fault,
                "hold_published": bool(receipt.get("hold_published")),
                "driver_sent": receipt.get("driver_sent"),
                "hardware_effect": "unverified"}

    def clear_fault(self) -> None:
        self.latched_fault = None

    def stop(self) -> None:
        with self.control_lock:
            self._stop_base("control_shutdown")
        if self.gamepad_input is not None:
            self.gamepad_input.close()
        if self.gamepad_preview is not None:
            self.gamepad_preview.close()
        if self.base_trace is not None:
            self.base_trace.stop(timeout_s=1.0)
        publisher = self.recording_publisher
        if publisher is not None and hasattr(publisher, "stop"):
            publisher.stop(timeout_s=1.0)

    # -- reporting ---------------------------------------------------------
    def state(self) -> dict[str, Any]:
        admission = self.admission.state() if self.admission else {}
        snapshot = self.readback.read_snapshot()
        base = self.local_base or self.gamepad_preview
        return {
            "data_identity": self.data_identity,
            "base_gamepad": None if base is None else base.status(time.monotonic_ns()),
            "configured_bundle_hash": self.bundle_hash,
            "launch_bundle_hash": self.launch_bundle_hash,
            "bundle_hash_source": self.bundle_hash_source,
            "last_bundle_adoption": self.last_bundle_adoption,
            # Nothing produced by this offline runtime is training eligible.
            "training_eligible": False,
            "uptime_s": round(time.monotonic() - self.started, 2),
            "control_hz": self.control_hz,
            "window_ms": self.window_ms,
            "stream_epoch": self.stream_epoch,
            "paused": self.paused,
            "latched_fault": self.latched_fault,
            "reengage_required": self.reengage_required,
            "uncommanded_joints": getattr(self.driver, "uncommanded_joints", []),
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
            "feedback_raw_limits": getattr(self.driver, "feedback_limits", None),
            "emit_intervals": percentiles(list(self.emit_intervals_ms)),
            "stop_count": self.driver.stop_count,
            "counters": dict(self.counters),
            "effective_emit_hz": (round(self.counters["frames_emitted"]
                                        / max(0.001, time.monotonic() - self.started), 2)),
            "admission": admission,
            "last_refusal": self.last_refusal,
            "transport": self.transport_label,
            "allowed_transports": list(self.allowed_transports),
            "active_transport": self.active_transport,
            "dds_peer": self.dds_peer,
            "dds_transport_epoch": self.dds_transport_epoch,
            "dds_command": (None if self.dds_receiver is None
                            else self.dds_receiver.state()),
            "recording_publisher": (None if self.recording_publisher is None
                                    else (self.recording_publisher.state()
                                          if hasattr(self.recording_publisher, "state")
                                          else {"kind": "ros_publisher"})),
            "typed_feedback": (None if self.typed_feedback is None
                               else self.typed_feedback.state()),
            "typed_control": (None if self.typed_control is None
                              else self.typed_control.state()),
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
                # Owner acquisition health and this consumer's receipt counters are
                # deliberately separate: consumer failed_scans=0 does not mean the
                # board owner never failed a scan.
                "owner_health": snapshot.get("owner_health"),
                "owner_health_age_s": snapshot.get("owner_health_age_s"),
                "owner_health_rejects": snapshot.get("owner_health_rejects"),
                "owner_health_last_error": snapshot.get("owner_health_last_error"),
                "consumer": snapshot.get("consumer"),
                "first_failure": snapshot.get("first_failure"),
                "frame_callback_errors": snapshot.get("frame_callback_errors"),
            },
        }

    def _write_status(self) -> None:
        document = self.state()
        # Why the base is (not) permitted is usually a licence question; keep the
        # mirror's own verdict in the status file instead of guessing from the car.
        document["licence"] = self.licence.state()
        if self.stream is not None:
            document["stream"] = self.stream.state()
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
                             encoding="utf-8")
        os.replace(temporary, self.status_path)


def build(profile: dict[str, Any], args: argparse.Namespace) -> tuple[Node, CarControlRuntime,
                                                                      LicenceMirror, StreamServer]:
    trial_settings = gamepad_base.hardware_trial_settings(profile)
    joint_settings = gamepad_base.hardware_joint_settings(profile)
    base_settings = (None if trial_settings is not None or joint_settings is not None
                     else gamepad_base.offline_settings(profile))
    preview_settings = gamepad_base.preview_settings(profile)
    if base_settings is not None and (args.data_identity != "simulated_offline"
            or args.output_topic != "/lelab_offline/ros_robot_controller/bus_servo/set_position"):
        raise ValueError("local_gamepad_base_requires_simulated_output")
    if (trial_settings is not None or joint_settings is not None) and args.data_identity != "hardware":
        raise ValueError("local_gamepad_requires_hardware_identity")
    if preview_settings is not None and args.data_identity != "hardware":
        raise ValueError("local_gamepad_preview_requires_hardware_identity")
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
        scan_period_s=float(feedback["scan_period_s"]),
        health_topic=args.health_topic,
        health_message_type=String)
    publisher = node.create_publisher(ServosPosition, args.output_topic, qos)
    state_publisher = node.create_publisher(
        SetBusServoState, args.state_topic, 10)
    torque_readback = TorqueStateReadback(node, GetBusServoState, service=args.state_service)
    recording = node.create_publisher(String, args.recording_topic, 10) if args.recording_topic else None
    if recording is not None:
        recording = BoundedRecordingPublisher(recording)
    typed_feedback = typed_control = None
    if args.typed_feedback:
        try:
            from .ros_feedback_mapping import clock_epoch as make_clock_epoch
            from .ros_typed_feedback import (
                TypedControlPublisher, TypedFeedbackPublisher,
            )
        except ImportError as error:
            raise RuntimeError(
                "typed feedback was requested but lelab_mobile_interfaces is not "
                f"available: {error}") from error
        boot_path = Path("/proc/sys/kernel/random/boot_id")
        boot_id = boot_path.read_text(encoding="utf-8").strip() if boot_path.exists() else "unknown"
        epoch = make_clock_epoch(boot_id, time.monotonic_ns())
        runtime_config = profile.get("runtime") or {}
        typed_control = TypedControlPublisher(
            node, data_identity=args.data_identity, clock_epoch=epoch,
            clock_domain=str(runtime_config.get("clock_domain") or "car_system_time"),
            window_topic=args.typed_window_topic,
            receipt_topic=args.typed_receipt_topic)
        typed_feedback = TypedFeedbackPublisher(
            node, readback, calibration_hash=args.bundle_hash,
            data_identity=args.data_identity, clock_epoch=epoch,
            frame_topic=args.typed_feedback_topic,
            health_topic=args.typed_health_topic)
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
        position_pid=teleop.get("position_pid"),
        feedback_limits_path=Path(args.status_path).with_name("feedback-raw-limits.json"),
        feedback_limits_profile=profile["profile"],
        hold_outside_command_range=joint_settings is not None)
    licence = LicenceMirror(Path(args.licence_socket))
    licence.start()
    runtime = CarControlRuntime(
        node, profile=profile, control_hz=args.control_hz, window_ms=args.window_ms,
        licence=licence, readback=readback, driver=driver,
        data_identity=args.data_identity, status_path=Path(args.status_path),
        bundle_hash=args.bundle_hash, max_ttl_ms=args.max_ttl_ms,
        recording_publisher=recording, typed_control=typed_control,
        typed_feedback=typed_feedback)
    readback.on_owner_changed = runtime._on_feedback_owner_changed
    if base_settings is not None or trial_settings is not None or joint_settings is not None:
        from ros_robot_controller_msgs.msg import MotorState, MotorsState
        from sensor_msgs.msg import Joy

        if trial_settings is not None or joint_settings is not None:
            # The vendor ros_robot_controller stays the only /dev/rrc owner; this
            # process only publishes its set_motor topic. joy_node feeds /joy.
            motor_output = node.create_publisher(MotorsState, gamepad_base.HARDWARE_TRIAL_TOPIC, qos)
            joy_qos = QoSProfile(reliability=QoSReliabilityPolicy.BEST_EFFORT,
                                 history=QoSHistoryPolicy.KEEP_LAST, depth=1)

            def publish_base(target):
                motor_output.publish(gamepad_base.motor_message(target, MotorState, MotorsState))

            mode, joy_topic = "hardware_trial", gamepad_base.HARDWARE_JOY_TOPIC
            trace_topic, sub_qos = gamepad_base.PREVIEW_TOPIC, joy_qos
            if joint_settings is not None:
                mode = "hardware_joint"
                trace_topic = gamepad_base.JOINT_TRACE_TOPIC
                runtime.base_motor_topic = motor_output.topic_name
        else:
            from geometry_msgs.msg import Twist
            output = node.create_publisher(Twist, gamepad_base.TWIST_TOPIC, qos)
            motor_output = node.create_publisher(MotorsState, gamepad_base.OFFLINE_MOTOR_TOPIC, qos)

            def publish_base(target):
                motor_output.publish(gamepad_base.motor_message(target, MotorState, MotorsState))
                message = Twist()
                message.linear.x, message.linear.y, message.angular.z = target
                try:
                    output.publish(message)
                except Exception as exc:
                    node.get_logger().error(f"base Twist diagnostic failed: {type(exc).__name__}")

            mode, joy_topic = "offline", gamepad_base.JOY_TOPIC
            trace_topic, sub_qos = gamepad_base.TRACE_TOPIC, qos

        settings = ({key: value for key, value in joint_settings.items() if key != "usb_port"}
                    if joint_settings is not None else trial_settings or base_settings)
        runtime.local_base = gamepad_base.LocalGamepadBase(settings, publish_base, mode=mode)
        runtime.base_trace = BoundedRecordingPublisher(node.create_publisher(String, trace_topic, 10))
        if joint_settings is None:
            node.create_subscription(Joy, joy_topic, runtime._on_local_joy, sub_qos)
        else:
            runtime.gamepad_input = gamepad_base.LinuxGamepadPreview(
                joint_settings["usb_port"], monitor_usb=True)
    if preview_settings is not None:
        runtime.gamepad_preview = gamepad_base.LinuxGamepadPreview(preview_settings["usb_port"])
        runtime.base_trace = BoundedRecordingPublisher(
            node.create_publisher(String, gamepad_base.PREVIEW_TOPIC, 10))
    if args.dds_command and typed_control is not None:
        from .dds_command_receiver import DdsCommandReceiver
        runtime.dds_receiver = DdsCommandReceiver(
            node, runtime, key_file=args.dds_command_key_file or None,
            hello_topic=args.dds_hello_topic,
            command_topic=args.dds_frame_topic,
            preempt_topic=args.dds_preempt_topic)
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
    if profile_name == "hardware_hybrid_manual_v1_offline6d_dds":
        runtime = profile.get("runtime") or {}
        if (runtime.get("mode") != "offline_humble_simulated_board"
                or runtime.get("simulated") is not True
                or runtime.get("output_transport") != "dds_v1"
                or runtime.get("allowed_transports") != ["dds_v1"]
                or runtime.get("data_source") != "simulated"
                or args.data_identity != "simulated_offline"
                or args.stream_host not in ("127.0.0.1", "localhost")):
            raise ValueError("offline_dds_profile_configuration_mismatch")
        return
    if profile_name in ("hardware_hybrid_follow_v1_dds", gamepad_base.HARDWARE_JOINT_PROFILE):
        runtime = profile.get("runtime") or {}
        teleop = profile.get("teleop") or {}
        safety = profile.get("safety") or {}
        if (runtime.get("mode") != "hardware_hybrid_follow"
                or runtime.get("output_transport") != "dds_v1"
                or runtime.get("allowed_transports") != ["dds_v1"]
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
                or (profile_name == gamepad_base.HARDWARE_JOINT_PROFILE
                    and args.recording_topic != "/lelab/recording/teleop")
                or not args.bundle_hash
                or args.bundle_hash != safety.get("calibration_bundle_hash")
                or not args.dds_command_key_file
                or not Path(args.dds_command_key_file).is_file()):
            raise ValueError("hardware_follow_dds_configuration_mismatch")
        return
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
    if profile_name == "hardware_hybrid_base_trial_v1":
        runtime = profile.get("runtime") or {}
        teleop = profile.get("teleop") or {}
        if (runtime.get("mode") != "hardware_base_trial"
                or runtime.get("output_transport") != "dds_v1"
                or runtime.get("allowed_transports") != ["dds_v1"]
                or runtime.get("actuator_outputs") is not True
                or runtime.get("data_source") != "hardware"
                or teleop.get("chassis_locked") is not True
                or teleop.get("trial_motion") is not None
                or args.data_identity != "hardware"
                or args.output_topic != "/ros_robot_controller/bus_servo/set_position"
                or args.stream_host not in ("127.0.0.1", "localhost")
                # The arm stays locked in this profile, so the chassis trial does not
                # depend on which arm calibration bundle the upper announces; it only
                # has to be a real, non-empty hash that the licence will echo back.
                or not args.bundle_hash):
            raise ValueError("hardware_base_trial_configuration_mismatch")
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
    parser.add_argument("--health-topic", default="/lelab_offline/lelab/ros_robot_controller/driver_health")
    parser.add_argument("--state-topic", default="/lelab_offline/ros_robot_controller/bus_servo/set_state")
    parser.add_argument("--state-service", default="/lelab_offline/ros_robot_controller/bus_servo/get_state")
    parser.add_argument("--control-hz", type=float, default=10.0)
    parser.add_argument("--window-ms", type=int, default=150)
    parser.add_argument("--max-ttl-ms", type=int, default=200)
    parser.add_argument("--recording-topic", default="/lelab_offline/recording/teleop")
    parser.add_argument("--typed-feedback-topic", default="/lelab_ros/feedback/measured")
    parser.add_argument("--typed-health-topic", default="/lelab_ros/feedback/health")
    parser.add_argument("--typed-window-topic", default="/lelab_ros/control/window")
    parser.add_argument("--typed-receipt-topic", default="/lelab_ros/control/receipt")
    parser.add_argument("--no-typed-feedback", dest="typed_feedback", action="store_false")
    parser.add_argument("--dds-command-key-file", default="")
    parser.add_argument("--dds-hello-topic", default="/lelab_ros/command/hello")
    parser.add_argument("--dds-frame-topic", default="/lelab_ros/command/frame")
    parser.add_argument("--dds-preempt-topic", default="/lelab_ros/command/preempt")
    parser.add_argument("--no-dds-command", dest="dds_command", action="store_false")
    parser.set_defaults(typed_feedback=True)
    parser.set_defaults(dds_command=True)
    parser.add_argument("--bundle-hash", default="")
    parser.add_argument("--data-identity", default="simulated_offline")
    args = parser.parse_args()
    if "serial" in sys.modules:
        print("refusing to start: a serial module is loaded in this process", file=sys.stderr)
        return 2
    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    try:
        validate_hardware_trial(profile, args)
        if (gamepad_base.hardware_trial_settings(profile) is None
                and gamepad_base.hardware_joint_settings(profile) is None):
            gamepad_base.offline_settings(profile)
        gamepad_base.preview_settings(profile)
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
    shutdown_requested = threading.Event()
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: shutdown_requested.set())
    node, runtime, licence, server = build(profile, args)
    server.start()
    print(json.dumps({"started": True, "stream_port": server.port,
                      "control_hz": args.control_hz, "data_identity": args.data_identity,
                      "output_topic": args.output_topic,
                      "readback_topic": args.readback_topic}), flush=True)
    try:
        while rclpy.ok() and not shutdown_requested.is_set():
            rclpy.spin_once(node, timeout_sec=0.1)
    except (KeyboardInterrupt, RuntimeError):
        pass
    finally:
        stop_runtime = getattr(runtime, "stop", None)
        if callable(stop_runtime):
            stop_runtime()
        server.stop()
        licence.stop()
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
