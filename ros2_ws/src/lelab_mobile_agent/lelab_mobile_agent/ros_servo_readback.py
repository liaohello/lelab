"""Read-only six-servo feedback through the existing control-board ROS owner.

This module never opens a serial port and never publishes an actuator command.  The
existing ``ros_robot_controller`` process remains the only ``/dev/rrc`` owner; this
driver periodically asks its bounded read service for joint identity and position.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .follower_feedback import FeedbackStore, JointSample, pulse_to_rad

JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint")
SERVO_IDS = {"joint1": 1, "joint2": 2, "joint3": 3,
             "joint4": 4, "joint5": 5, "r_joint": 10}
SERVICE = "/ros_robot_controller/bus_servo/get_state"
RAW_MIN = -1000
RAW_MAX = 1000
FEEDBACK_TOPIC = "/lelab/ros_robot_controller/servo_readback"
# v2 measured-frame envelope: [format=2, owner_instance, seq, scan_us, id,pos ×6].
# owner_instance is a software identifier the publishing owner generates once at
# startup; it is NOT an MCU boot ID and carries no hardware provenance. Sequence
# numbers are only comparable inside one owner instance.
READBACK_FORMAT_V2 = 2
READBACK_V2_LEN = 16
OWNER_INSTANCE_MIN = 1
OWNER_INSTANCE_MAX = 2147483647


class ReadbackProtocolError(RuntimeError):
    pass


def decode_position(raw: int) -> int:
    """Decode the vendor int16 protocol value from its ROS uint16 transport."""
    value = int(raw)
    return value - 65536 if value > 32767 else value


def parse_servo_states(states: Any) -> dict[str, int]:
    """Validate six ordered id+position responses without repairing malformed data."""
    try:
        states = list(states)
    except (TypeError, ValueError):
        raise ReadbackProtocolError("servo responses are not iterable") from None
    if len(states) != len(JOINTS):
        raise ReadbackProtocolError("expected exactly six servo responses")
    positions: dict[str, int] = {}
    for name, state in zip(JOINTS, states, strict=True):
        present = list(getattr(state, "present_id", []) or [])
        position = list(getattr(state, "position", []) or [])
        expected_id = SERVO_IDS[name]
        if present != [expected_id]:
            raise ReadbackProtocolError(
                f"{name}: expected present_id=[{expected_id}], got {present}")
        if len(position) != 1 or isinstance(position[0], bool):
            raise ReadbackProtocolError(
                f"{name}: expected one integer position, got {position}")
        raw = decode_position(position[0])
        if not RAW_MIN <= raw <= RAW_MAX:
            raise ReadbackProtocolError(
                f"{name}: position {raw} outside raw range {RAW_MIN}..{RAW_MAX}")
        positions[name] = raw
    return positions


class RosServoReadbackDriver:
    """T1 driver surface backed by a read-only ROS service client."""

    def __init__(
        self,
        node: Any,
        service_type: Any,
        *,
        calibration_hash: str | None,
        stale_after_s: float = 0.5,
        max_scan_span_s: float = 0.5,
        scan_period_s: float = 0.2,
        timeout_s: float = 1.0,
        converter: Callable[[int, str], float] | None = None,
    ) -> None:
        self.node = node
        self.service_type = service_type
        self.calibration_hash = calibration_hash
        self.stale_after_s = stale_after_s
        self.max_scan_span_s = max_scan_span_s
        self.scan_period_s = scan_period_s
        self.timeout_s = timeout_s
        self.converter = converter or self._default_converter
        self.store = FeedbackStore(
            list(JOINTS),
            stale_after_s=stale_after_s,
            calibration_hash=calibration_hash,
            servo_ids=dict(SERVO_IDS),
            max_scan_span_s=max_scan_span_s,
        )
        self.client = node.create_client(service_type, SERVICE)
        self.lock = threading.RLock()
        self.future = None
        self.scan_started = 0.0
        self.next_scan = 0.0
        self.sample_seq = 0
        self.scan_span_s: float | None = None
        self.last_error: str | None = "no_readback_yet"
        self.last_success_mono: float | None = None
        self.successful_scans = 0
        self.failed_scans = 0
        self.paused = False
        node.create_timer(min(0.05, scan_period_s), self._poll)

    @staticmethod
    def _default_converter(raw: int, _name: str) -> float:
        return pulse_to_rad(float(raw), initial_position_raw=500, flipped=False)

    def set_calibration_hash(self, calibration_hash: str | None) -> None:
        with self.lock:
            self.calibration_hash = calibration_hash
            self.store = FeedbackStore(
                list(JOINTS),
                stale_after_s=self.stale_after_s,
                calibration_hash=calibration_hash,
                servo_ids=dict(SERVO_IDS),
                max_scan_span_s=self.max_scan_span_s,
            )
            self.last_error = "calibration_identity_changed"

    def capabilities(self) -> dict[str, Any]:
        with self.lock:
            return {
                "driver": "ros_servo_readback",
                "source_kind": "servo_position_readback",
                "joint_topic": SERVICE,
                "base_topic": None,
                "servo_ids": dict(SERVO_IDS),
                "scan_is_sequential": True,
                "scan_period_s": self.scan_period_s,
                "max_scan_span_s": self.max_scan_span_s,
                "timeout_s": self.timeout_s,
                "calibration_bound": bool(
                    self.calibration_hash and self.calibration_hash != "unbound"),
                "outputs_enabled": False,
                "driver_sent": "unavailable",
                "controller_acknowledged": "unavailable",
                "execution_observed": "unavailable",
            }

    def _request(self):
        request = self.service_type.Request()
        command_type = type(self.service_type.Request().cmd)
        # The generated sequence needs one concrete element before append works in
        # some Humble builds.  Construct through the request's own command type.
        del command_type
        for name in JOINTS:
            item = self._empty_command()
            item.id = SERVO_IDS[name]
            item.get_id = 1
            item.get_position = 1
            request.cmd.append(item)
        return request

    def _empty_command(self):
        request = self.service_type.Request()
        if request.cmd:
            return request.cmd[0]
        # A generated ROS sequence is untyped while empty.  Importing the concrete
        # message lazily keeps this module importable on hosts without the car msgs.
        from ros_robot_controller_msgs.msg import GetBusServoCmd

        return GetBusServoCmd()

    def _fail(self, reason: str) -> None:
        with self.lock:
            self.last_error = reason
            self.failed_scans += 1
            self.store.clear(reason)

    def _poll(self) -> None:
        if self.paused:
            return
        now = time.monotonic()
        with self.lock:
            future = self.future
            if future is not None and not future.done():
                if now - self.scan_started > self.timeout_s:
                    future.cancel()
                    self.future = None
                    self._fail("readback_timeout")
                    self.next_scan = now + self.scan_period_s
                return
            if future is not None:
                self.future = None
                try:
                    response = future.result()
                except Exception as exc:  # noqa: BLE001
                    self._fail(f"service_failed:{type(exc).__name__}")
                else:
                    if response is None or getattr(response, "success", False) is not True:
                        self._fail("controller_reported_read_failure")
                    else:
                        finished = time.monotonic()
                        span = max(0.0, finished - self.scan_started)
                        if span > self.max_scan_span_s:
                            self._fail(f"scan_span_exceeded:{span:.3f}s")
                        else:
                            try:
                                positions = parse_servo_states(response.state)
                            except ReadbackProtocolError as exc:
                                self._fail(str(exc))
                            else:
                                self.sample_seq += 1
                                recv_ns = time.time_ns()
                                for name, raw in positions.items():
                                    error = None if self.calibration_hash else "calibration_not_configured"
                                    self.store.ingest_sample(JointSample(
                                        name=name,
                                        position_rad=self.converter(raw, name),
                                        wall=finished,
                                        source="servo_position_readback",
                                        servo_id=SERVO_IDS[name],
                                        raw_position=float(raw),
                                        sample_seq=self.sample_seq,
                                        sample_time_ns=recv_ns,
                                        clock_domain="host_monotonic",
                                        valid=error is None,
                                        error=error,
                                        calibration_hash=self.calibration_hash,
                                    ))
                                self.scan_span_s = span
                                self.last_success_mono = finished
                                self.last_error = None
                                self.successful_scans += 1
                self.next_scan = now + self.scan_period_s
            if self.future is not None or now < self.next_scan:
                return
            if not self.client.service_is_ready():
                self._fail("service_unavailable")
                self.next_scan = now + self.scan_period_s
                return
            self.scan_started = time.monotonic()
            self.future = self.client.call_async(self._request())

    def read_snapshot(self) -> dict[str, Any]:
        with self.lock:
            snapshot = self.store.snapshot()
            snapshot["scan_span_s"] = self.scan_span_s
            snapshot["scan_period_s"] = self.scan_period_s
            snapshot["scan_sequence"] = self.sample_seq
            snapshot["successful_scans"] = self.successful_scans
            snapshot["failed_scans"] = self.failed_scans
            snapshot["last_error"] = self.last_error
            snapshot["last_success_mono"] = self.last_success_mono
            snapshot["outputs_enabled"] = False
            return snapshot

    def pause_and_drain(self, timeout_s: float = 1.5) -> None:
        """Stop periodic scans and wait for an in-flight response to retire."""
        with self.lock:
            self.paused = True
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self.lock:
                future = self.future
                if future is None or future.done():
                    return
            time.sleep(0.01)
        with self.lock:
            if self.future is not None:
                self.future.cancel()
                self.future = None

    def resume(self) -> None:
        with self.lock:
            self.paused = False
            self.next_scan = time.monotonic()

    def submit(self, command: dict[str, Any]) -> dict[str, Any]:
        del command
        raise RuntimeError("readback_only_driver_has_no_output")

    def stop(self, reason: str) -> dict[str, Any]:
        return {
            "stopped": True,
            "reason": str(reason),
            "hardware_effect": "not_applicable_readback_only",
            "driver_sent": "unavailable",
        }


class TorqueStateReadback:
    """Bounded per-ID torque-state reads through the unique board owner's service.

    This opens no serial port: it reuses the same ``get_state`` service the vendor
    ``ros_robot_controller`` owner already serves. One read is in flight at a time;
    ``start``/``poll`` are non-blocking so a ROS executor, HTTP loop or control tick
    is never held waiting for a reply. A timeout retires the read as a failure rather
    than blocking or silently retrying the same query as trusted.
    """

    def __init__(self, node: Any, service_type: Any, *, timeout_s: float = 1.0,
                 service: str = SERVICE) -> None:
        if not 0.1 <= timeout_s <= 5.0:
            raise ValueError("invalid torque readback timeout")
        self.node = node
        self.service_type = service_type
        self.timeout_s = float(timeout_s)
        self.client = node.create_client(service_type, service)
        self._future = None
        self._servo_id: int | None = None
        self._deadline = 0.0

    @property
    def in_flight(self) -> bool:
        return self._future is not None

    def _command(self, servo_id: int):
        request = self.service_type.Request()
        if request.cmd:
            command = request.cmd[0]
        else:
            from ros_robot_controller_msgs.msg import GetBusServoCmd

            command = GetBusServoCmd()
        command.id = servo_id
        command.get_id = 1
        command.get_position = 1
        command.get_torque_state = 1
        request.cmd.append(command)
        return request

    def start(self, servo_id: int) -> None:
        if self._future is not None:
            raise RuntimeError("torque_readback_already_in_flight")
        self._servo_id = servo_id
        self._deadline = time.monotonic() + self.timeout_s
        self._future = self.client.call_async(self._command(servo_id))

    def cancel(self) -> None:
        if self._future is not None:
            self._future.cancel()
        self._future = None
        self._servo_id = None

    def poll(self) -> tuple[int, dict[str, Any]] | None:
        """Retire the in-flight read as (servo_id, result), or return None."""
        if self._future is None:
            return None
        servo_id = self._servo_id
        if not self._future.done():
            if time.monotonic() <= self._deadline:
                return None
            self._future.cancel()
            self._future = None
            self._servo_id = None
            return servo_id, {"verified": False, "servo_id": servo_id,
                              "reason": "readback_timeout"}
        future, self._future = self._future, None
        self._servo_id = None
        try:
            response = future.result()
        except Exception as exc:  # noqa: BLE001 - a failed read is a failure, not a raise
            return servo_id, {"verified": False, "servo_id": servo_id,
                              "reason": f"service_failed:{type(exc).__name__}"}
        return servo_id, self.parse(servo_id, response)

    @staticmethod
    def parse(servo_id: int, response: Any) -> dict[str, Any]:
        if response is None or getattr(response, "success", False) is not True:
            return {"verified": False, "servo_id": servo_id, "reason": "read_failed"}
        states = list(getattr(response, "state", []) or [])
        if len(states) != 1:
            return {"verified": False, "servo_id": servo_id, "reason": "read_failed"}
        state = states[0]
        present = list(getattr(state, "present_id", []) or [])
        torques = list(getattr(state, "enable_torque", []) or [])
        positions = list(getattr(state, "position", []) or [])
        if present != [servo_id] or len(torques) != 1 or len(positions) != 1:
            return {"verified": False, "servo_id": servo_id,
                    "reason": "readback_identity_mismatch",
                    "present_id": present, "enable_torque": torques}
        loaded = torques[0] == 1
        return {"verified": loaded, "servo_id": servo_id,
                "enable_torque": int(torques[0]), "position": int(positions[0]),
                "reason": None if loaded else "torque_not_loaded"}


class RosServoFeedbackTopicDriver:
    """Continuously subscribe to signed raw positions published by the UART owner."""

    def __init__(
        self,
        node: Any,
        message_type: Any,
        *,
        calibration_hash: str | None,
        topic: str = FEEDBACK_TOPIC,
        stale_after_s: float = 0.5,
        max_scan_span_s: float = 0.5,
        scan_period_s: float = 0.2,
        converter: Callable[[int, str], float] | None = None,
    ) -> None:
        self.node = node
        self.message_type = message_type
        self.topic = topic
        self.calibration_hash = calibration_hash
        self.stale_after_s = stale_after_s
        self.max_scan_span_s = max_scan_span_s
        self.scan_period_s = scan_period_s
        self.converter = converter or self._default_converter
        self.store = FeedbackStore(
            list(JOINTS),
            stale_after_s=stale_after_s,
            calibration_hash=calibration_hash,
            servo_ids=dict(SERVO_IDS),
            max_scan_span_s=max_scan_span_s,
        )
        self.lock = threading.RLock()
        self.last_seq = -1
        self.sample_seq = 0
        self.publisher_instance: int | None = None
        self.publisher_generation = 0
        self._retired_owners: list[int] = []
        self.on_owner_changed: Callable[[int | None, int, int], None] | None = None
        self.scan_span_s: float | None = None
        self.last_error: str | None = "no_readback_yet"
        self.last_success_mono: float | None = None
        self.successful_scans = 0
        self.failed_scans = 0
        self.paused = False
        self.subscription = node.create_subscription(
            message_type, topic, self._on_message, 10)

    @staticmethod
    def _default_converter(raw: int, _name: str) -> float:
        return pulse_to_rad(float(raw), initial_position_raw=500, flipped=False)

    def set_calibration_hash(self, calibration_hash: str | None) -> None:
        with self.lock:
            self.calibration_hash = calibration_hash
            self.store = FeedbackStore(
                list(JOINTS),
                stale_after_s=self.stale_after_s,
                calibration_hash=calibration_hash,
                servo_ids=dict(SERVO_IDS),
                max_scan_span_s=self.max_scan_span_s,
            )
            self.last_seq = -1
            self.sample_seq = 0
            self.last_error = "calibration_identity_changed"

    def capabilities(self) -> dict[str, Any]:
        with self.lock:
            return {
                "driver": "ros_servo_readback_topic",
                "source_kind": "servo_position_readback",
                "joint_topic": self.topic,
                "scan_is_sequential": True,
                "scan_period_s": self.scan_period_s,
                "max_scan_span_s": self.max_scan_span_s,
                "calibration_bound": bool(
                    self.calibration_hash and self.calibration_hash != "unbound"),
                "outputs_enabled": False,
                "driver_sent": "unavailable",
                "controller_acknowledged": "unavailable",
                "execution_observed": "unavailable",
            }

    def _fail(self, reason: str) -> None:
        with self.lock:
            self.last_error = reason
            self.failed_scans += 1
            self.store.clear(reason)

    def _on_message(self, message: Any) -> None:
        owner_change: tuple[int | None, int, int] | None = None
        with self.lock:
            # Traffic bookkeeping for the node that owns this driver: the recorder's
            # required-topic gate reads the node's own receive table, and a driver that
            # consumes a topic without stamping it makes a live stream look missing.
            received = getattr(self.node, "last_rx", None)
            if isinstance(received, dict):
                received[self.topic] = time.monotonic()
            if self.paused:
                return
            try:
                data = list(getattr(message, "data", []))
            except (TypeError, ValueError):
                self._fail("readback_payload_not_iterable")
                return
            if any(type(value) is not int for value in data):
                self._fail("readback_payload_invalid")
                return
            if len(data) == 14:
                # The legacy envelope has no owner instance, so a sequence reset after
                # a publisher restart is indistinguishable from a replay. Refuse it
                # rather than guess; the matched v2 owner ships with this consumer.
                self._fail("readback_format_unsupported:v1")
                return
            if len(data) != READBACK_V2_LEN or data[0] != READBACK_FORMAT_V2:
                self._fail("readback_payload_invalid")
                return
            owner, sequence, scan_span_us = data[1], data[2], data[3]
            if not OWNER_INSTANCE_MIN <= owner <= OWNER_INSTANCE_MAX or sequence < 0:
                self._fail("readback_owner_or_sequence_invalid")
                return
            now = time.monotonic()
            changed = False
            if self.publisher_instance is not None and owner != self.publisher_instance:
                if owner in self._retired_owners:
                    # A late frame from an instance we already retired is stale data,
                    # not a fresh publisher; a genuine restart draws a new instance.
                    self._fail("readback_retired_owner")
                    return
                changed = True
            if not changed and sequence <= self.last_seq:
                self._fail("readback_sequence_invalid")
                return
            if scan_span_us < 0:
                self._fail("readback_sequence_invalid")
                return
            if scan_span_us > int(self.max_scan_span_s * 1_000_000):
                self._fail("scan_span_exceeded")
                return
            pairs = [(data[index], data[index + 1])
                     for index in range(4, READBACK_V2_LEN, 2)]
            if [servo_id for servo_id, _ in pairs] != list(SERVO_IDS.values()):
                self._fail("servo_id_mismatch")
                return
            if any(not RAW_MIN <= raw <= RAW_MAX for _, raw in pairs):
                self._fail("raw_out_of_range")
                return
            finished = now
            sample_time_ns = time.time_ns()
            error = None if self.calibration_hash else "calibration_not_configured"
            previous_owner = self.publisher_instance
            if changed:
                # A new software owner instance retires the old cache and sequence
                # space at once; we do not wait for the new sequence to catch up.
                self.store.clear("readback_publisher_changed")
                self.last_seq = -1
                self.publisher_generation += 1
                if previous_owner is not None:
                    self._retired_owners.append(previous_owner)
                    del self._retired_owners[:-8]
            self.publisher_instance = owner
            for (servo_id, raw), name in zip(
                    pairs, JOINTS, strict=True):
                self.store.ingest_sample(JointSample(
                    name=name,
                    position_rad=self.converter(raw, name),
                    wall=finished,
                    source="servo_position_readback",
                    servo_id=servo_id,
                    raw_position=float(raw),
                    sample_seq=sequence,
                    sample_time_ns=sample_time_ns,
                    clock_domain="host_monotonic",
                    valid=error is None,
                    error=error,
                    calibration_hash=self.calibration_hash,
                ))
            self.last_seq = sequence
            self.sample_seq = sequence
            self.scan_span_s = scan_span_us / 1_000_000.0
            self.last_success_mono = finished
            self.last_error = None
            self.successful_scans += 1
            if changed:
                owner_change = (previous_owner, owner, self.publisher_generation)
        if owner_change is not None and self.on_owner_changed is not None:
            # Fired outside the readback lock: the control runtime takes its own
            # control lock and stops the output driver here. Observation resumes
            # with the new owner's samples; motion does not resume on its own.
            self.on_owner_changed(*owner_change)

    def read_snapshot(self) -> dict[str, Any]:
        with self.lock:
            snapshot = self.store.snapshot()
            snapshot["scan_span_s"] = self.scan_span_s
            snapshot["scan_period_s"] = self.scan_period_s
            snapshot["scan_sequence"] = self.sample_seq
            snapshot["successful_scans"] = self.successful_scans
            snapshot["failed_scans"] = self.failed_scans
            snapshot["last_error"] = self.last_error
            snapshot["topic"] = self.topic
            snapshot["publisher_instance"] = self.publisher_instance
            snapshot["publisher_generation"] = self.publisher_generation
            snapshot["publisher_identity_kind"] = "software_owner_instance"
            snapshot["last_success_mono"] = self.last_success_mono
            snapshot["servo_pulses"] = {
                name: int(sample["raw_position"])
                for name, sample in (snapshot.get("joint_samples") or {}).items()
                if isinstance(sample, dict)
                and isinstance(sample.get("raw_position"), (int, float))
                and not isinstance(sample.get("raw_position"), bool)
            }
            snapshot["outputs_enabled"] = False
            return snapshot

    def pause_and_drain(self, timeout_s: float = 1.5) -> None:
        del timeout_s
        with self.lock:
            self.paused = True

    def resume(self) -> None:
        with self.lock:
            self.paused = False

    def submit(self, command: dict[str, Any]) -> dict[str, Any]:
        del command
        raise RuntimeError("readback_only_driver_has_no_output")

    def stop(self, reason: str) -> dict[str, Any]:
        return {
            "stopped": True,
            "reason": str(reason),
            "hardware_effect": "not_applicable_readback_only",
            "driver_sent": "unavailable",
        }
