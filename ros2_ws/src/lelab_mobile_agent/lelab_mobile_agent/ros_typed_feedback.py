"""Typed ROS 2 publishers for measured feedback, health and command receipts.

This module is imported only when the interface package is built.  The raw v2
``Int32MultiArray`` topic remains the owner's wire format; this is a typed
projection of exactly the same measurements and never of the command cache.
"""
from __future__ import annotations

import time
from typing import Any

from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import String

from lelab_mobile_interfaces.msg import (
    CommandReceipt, DriverFailure, DriverHealth, FreshnessWindowStatus,
    MeasuredServoFrame,
)

from .ros_feedback_mapping import driver_health, measured_frame


def _reliable(depth: int = 1) -> QoSProfile:
    return QoSProfile(reliability=QoSReliabilityPolicy.RELIABLE,
                      history=QoSHistoryPolicy.KEEP_LAST, depth=depth)


def _fill_failure(target: DriverFailure, fields: dict[str, Any]) -> None:
    target.present = bool(fields["present"])
    target.servo_id = int(fields["servo_id"])
    target.command = str(fields["command"])
    target.reason = str(fields["reason"])
    target.lock_wait_ms = float(fields["lock_wait_ms"])
    target.duration_ms = float(fields["duration_ms"])
    target.recovery_phase = str(fields["recovery_phase"])
    target.last_success_seq = int(fields["last_success_seq"])
    target.scan_interval_s = float(fields["scan_interval_s"])
    target.at_mono_ns = int(fields["at_mono_ns"])
    target.data_identity = str(fields["data_identity"])


class TypedFeedbackPublisher:
    """Publish one MeasuredServoFrame per owner scan and DriverHealth at 1 Hz."""

    def __init__(self, node: Any, readback: Any, *, calibration_hash: str,
                 data_identity: str, clock_epoch: str,
                 frame_topic: str = "/lelab_ros/feedback/measured",
                 health_topic: str = "/lelab_ros/feedback/health",
                 health_period_s: float = 1.0) -> None:
        self.node = node
        self.readback = readback
        self.calibration_hash = calibration_hash
        self.data_identity = data_identity
        self.clock_epoch = clock_epoch
        self.frame_topic = frame_topic
        self.health_topic = health_topic
        self._frame_pub = node.create_publisher(MeasuredServoFrame, frame_topic, _reliable(1))
        self._health_pub = node.create_publisher(DriverHealth, health_topic, _reliable(1))
        self.counts = {"frames": 0, "health": 0, "errors": 0}
        self.last_error: str | None = None
        self.last_snapshot: dict[str, Any] | None = None
        self._last_health_snapshot: dict[str, Any] | None = None
        if readback is not None:
            readback.on_frame = self._on_readback_frame
        node.create_timer(max(0.1, float(health_period_s)), self.publish_health)

    def _on_readback_frame(self, snapshot: dict[str, Any]) -> None:
        self.last_snapshot = snapshot
        try:
            fields = measured_frame(
                snapshot, published_mono_ns=time.monotonic_ns(),
                clock_epoch_value=self.clock_epoch,
                calibration_hash=self.calibration_hash,
                data_identity=self.data_identity)
            message = MeasuredServoFrame()
            message.header.stamp = self.node.get_clock().now().to_msg()
            message.source = fields["source"]
            message.data_identity = fields["data_identity"]
            message.frame_format = fields["frame_format"]
            message.owner_instance = fields["owner_instance"]
            message.scan_sequence = fields["scan_sequence"]
            message.scan_span_us = fields["scan_span_us"]
            message.sample_start_mono_ns = fields["sample_start_mono_ns"]
            message.sample_end_mono_ns = fields["sample_end_mono_ns"]
            message.published_mono_ns = fields["published_mono_ns"]
            message.sample_age_ns = fields["sample_age_ns"]
            message.clock_domain = fields["clock_domain"]
            message.clock_epoch = fields["clock_epoch"]
            message.calibration_hash = fields["calibration_hash"]
            message.joint_names = fields["joint_names"]
            message.servo_ids = fields["servo_ids"]
            message.raw = fields["raw"]
            message.position_rad = fields["position_rad"]
            message.valid = fields["valid"]
            message.errors = fields["errors"]
            self._frame_pub.publish(message)
            self.counts["frames"] += 1
            self.last_error = None
        except Exception as error:  # noqa: BLE001 - feedback must keep flowing
            self.counts["errors"] += 1
            self.last_error = f"{type(error).__name__}:{error}"

    def publish_health(self) -> None:
        try:
            snapshot = (self.readback.read_snapshot() if self.readback is not None
                        else {})
            self._last_health_snapshot = snapshot
            fields = driver_health(snapshot, now_mono_ns=time.monotonic_ns(),
                                   clock_epoch_value=self.clock_epoch,
                                   data_identity=self.data_identity)
            message = DriverHealth()
            message.header.stamp = self.node.get_clock().now().to_msg()
            for name in ("source", "data_identity", "owner_identity_kind",
                         "owner_instance", "scan_sequence", "period_s", "quiet_s",
                         "transaction_budget_s", "scans_started", "scans_ok",
                         "scans_failed", "consecutive_failures", "write_failures",
                         "last_success_seq", "last_success_mono_ns",
                         "last_success_age_s", "last_scan_interval_s",
                         "max_scan_interval_s", "stale",
                         "last_recovery_phase", "last_recovery_ok",
                         "last_recovery_quiet_s", "last_recovery_duration_ms",
                         "last_recovery_mono_ns", "consumer_successful_scans",
                         "consumer_failed_scans", "consumer_last_error",
                         "consumer_first_failure_present",
                         "consumer_first_failure_reason",
                         "consumer_first_failure_mono_ns", "clock_domain",
                         "clock_epoch"):
                setattr(message, name, fields[name])
            _fill_failure(message.first_failure, fields["first_failure"])
            _fill_failure(message.last_failure, fields["last_failure"])
            self._health_pub.publish(message)
            self.counts["health"] += 1
        except Exception as error:  # noqa: BLE001 - a health fault is a state, not a crash
            self.counts["errors"] += 1
            self.last_error = f"{type(error).__name__}:{error}"

    def state(self) -> dict[str, Any]:
        return {"frame_topic": self.frame_topic,
                "health_topic": self.health_topic,
                "counts": dict(self.counts), "last_error": self.last_error,
                "clock_epoch": self.clock_epoch,
                "data_identity": self.data_identity}


class TypedControlPublisher:
    """Publish windows, receipts and stops without waiting on any subscriber."""

    def __init__(self, node: Any, *, data_identity: str, clock_epoch: str,
                 clock_domain: str,
                 window_topic: str = "/lelab_ros/control/window",
                 receipt_topic: str = "/lelab_ros/control/receipt") -> None:
        self.node = node
        self.data_identity = data_identity
        self.clock_epoch = clock_epoch
        self.clock_domain = clock_domain
        self.window_topic = window_topic
        self.receipt_topic = receipt_topic
        self._window_pub = node.create_publisher(
            FreshnessWindowStatus, window_topic, _reliable(1))
        self._receipt_pub = node.create_publisher(
            CommandReceipt, receipt_topic, _reliable(10))
        self.counts = {"windows": 0, "receipts": 0, "errors": 0}
        self.last_error: str | None = None

    def _guard(self, callback) -> None:
        try:
            callback()
        except Exception as error:  # noqa: BLE001 - telemetry must not stop the tick
            self.counts["errors"] += 1
            self.last_error = f"{type(error).__name__}:{error}"

    def publish_window(self, *, session_id: str | None, session_epoch,
                       stream_epoch, window: dict[str, Any], remaining_ms: float,
                       command_transport_ready: bool, reason: str = "") -> None:
        def send() -> None:
            message = FreshnessWindowStatus()
            message.header.stamp = self.node.get_clock().now().to_msg()
            message.session_id = str(session_id or "")
            message.session_epoch = int(session_epoch or 0)
            message.stream_epoch = str(stream_epoch)
            message.window_id = str(window.get("window_id"))
            message.duration_ms = int(window.get("duration_ms") or 0)
            message.remaining_ms_at_publish = float(remaining_ms)
            message.issued_mono_ns = int(window.get("issued_ns") or 0)
            message.deadline_mono_ns = int(window.get("deadline_ns") or 0)
            message.clock_domain = self.clock_domain
            message.clock_epoch = self.clock_epoch
            message.command_transport_ready = bool(command_transport_ready)
            message.reason = str(reason)
            self._window_pub.publish(message)
            self.counts["windows"] += 1
        self._guard(send)

    def publish_receipt(self, *, transport: str, session_id, session_epoch,
                        stream_epoch, frame_seq, sample_seq, state: str,
                        reason: str = "", detail: str = "", admitted: bool = False,
                        publisher_sent: bool = True, emitted: bool = False,
                        received_mono_ns: int | None = None,
                        deadline_mono_ns: int | None = None,
                        window_remaining_ms: float | None = None,
                        window_age_ms: float | None = None) -> None:
        def send() -> None:
            message = CommandReceipt()
            message.header.stamp = self.node.get_clock().now().to_msg()
            message.data_identity = self.data_identity
            message.transport = str(transport)
            message.session_id = str(session_id or "")
            message.session_epoch = int(session_epoch or 0)
            message.stream_epoch = str(stream_epoch)
            message.frame_seq = int(frame_seq or 0)
            message.sample_seq = int(sample_seq or 0)
            message.state = str(state)
            message.reason = str(reason)
            message.detail = str(detail)[:512]
            message.admitted = bool(admitted)
            message.publisher_sent = bool(publisher_sent)
            message.emitted = bool(emitted)
            message.controller_acknowledged = "unavailable"
            message.execution_observed = "unavailable"
            message.received_mono_ns = int(
                time.monotonic_ns() if received_mono_ns is None else received_mono_ns)
            message.deadline_mono_ns = int(deadline_mono_ns or 0)
            message.window_remaining_ms = float(window_remaining_ms or 0.0)
            message.window_age_ms = float(window_age_ms or 0.0)
            self._receipt_pub.publish(message)
            self.counts["receipts"] += 1
        self._guard(send)

    def state(self) -> dict[str, Any]:
        return {"window_topic": self.window_topic,
                "receipt_topic": self.receipt_topic,
                "counts": dict(self.counts), "last_error": self.last_error,
                "data_identity": self.data_identity}
