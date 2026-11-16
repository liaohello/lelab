#!/usr/bin/env python3
"""Upper-computer Humble bridge: typed DDS feedback plus thin flow services.

Three separations are deliberate:

* the socket thread only copies the latest typed snapshot.  ROS callbacks never
  wait on a client, and a client never blocks the executor;
* the services/action call the existing FastAPI routes with a short timeout.
  They do not copy the session state machine, do not acquire a second lease and
  never publish an actuator topic;
* the raw control token is never part of a DDS message.  R2 command messages
  carry an HMAC ticket over a pre-shared key instead.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import String

from lelab_mobile_interfaces.action import HomeArm
from lelab_mobile_interfaces.msg import (
    CommandReceipt, DriverHealth, FreshnessWindowStatus, MeasuredServoFrame,
    Preempt, TeleopFrame, TransportHello,
)
from lelab_mobile_interfaces.srv import ClearEstop, SessionControl

from .dds_frame_codec import FrameCodecError, _identity_from_text, frame_to_message
from .teleop_frame_v1 import (
    auth_key_id, load_auth_key, verify_frame_auth_ticket,
    verify_hello_auth_ticket, verify_preempt_auth_ticket,
)

SCHEMA = "lelab.upper_dds_bridge.v1"
MAX_REQUEST_BYTES = 262144
MAX_CLIENTS = 8
SOCKET_SEND_TIMEOUT_S = 1.0
MAX_COMMAND_HZ = 100.0


def _qos(depth: int = 1) -> QoSProfile:
    return QoSProfile(reliability=QoSReliabilityPolicy.BEST_EFFORT,
                      history=QoSHistoryPolicy.KEEP_LAST, depth=depth)


def _reliable(depth: int = 1) -> QoSProfile:
    return QoSProfile(reliability=QoSReliabilityPolicy.RELIABLE,
                      history=QoSHistoryPolicy.KEEP_LAST, depth=depth)


def _stamp(message: Any) -> dict[str, int]:
    stamp = message.header.stamp
    return {"sec": int(stamp.sec), "nanosec": int(stamp.nanosec)}


def _failure(message: Any) -> dict[str, Any]:
    return {
        "present": bool(message.present), "servo_id": int(message.servo_id),
        "command": message.command, "reason": message.reason,
        "lock_wait_ms": float(message.lock_wait_ms),
        "duration_ms": float(message.duration_ms),
        "recovery_phase": message.recovery_phase,
        "last_success_seq": int(message.last_success_seq),
        "scan_interval_s": float(message.scan_interval_s),
        "at_mono_ns": int(message.at_mono_ns),
        "data_identity": message.data_identity,
    }


def _measured(message: MeasuredServoFrame) -> dict[str, Any]:
    return {
        "header": _stamp(message), "source": message.source,
        "data_identity": message.data_identity,
        "frame_format": int(message.frame_format),
        "owner_instance": int(message.owner_instance),
        "scan_sequence": int(message.scan_sequence),
        "scan_span_us": int(message.scan_span_us),
        "sample_start_mono_ns": int(message.sample_start_mono_ns),
        "sample_end_mono_ns": int(message.sample_end_mono_ns),
        "published_mono_ns": int(message.published_mono_ns),
        "sample_age_ns": int(message.sample_age_ns),
        "clock_domain": message.clock_domain, "clock_epoch": message.clock_epoch,
        "calibration_hash": message.calibration_hash,
        "joint_names": list(message.joint_names),
        "servo_ids": [int(value) for value in message.servo_ids],
        "raw": [int(value) for value in message.raw],
        "position_rad": [float(value) for value in message.position_rad],
        "valid": [bool(value) for value in message.valid],
        "errors": list(message.errors),
    }


def _health(message: DriverHealth) -> dict[str, Any]:
    return {
        "header": _stamp(message), "source": message.source,
        "data_identity": message.data_identity,
        "owner_identity_kind": message.owner_identity_kind,
        "owner_instance": int(message.owner_instance),
        "scan_sequence": int(message.scan_sequence),
        "period_s": float(message.period_s), "quiet_s": float(message.quiet_s),
        "transaction_budget_s": float(message.transaction_budget_s),
        "scans_started": int(message.scans_started),
        "scans_ok": int(message.scans_ok),
        "scans_failed": int(message.scans_failed),
        "consecutive_failures": int(message.consecutive_failures),
        "write_failures": int(message.write_failures),
        "last_success_seq": int(message.last_success_seq),
        "last_success_mono_ns": int(message.last_success_mono_ns),
        "last_success_age_s": float(message.last_success_age_s),
        "last_scan_interval_s": float(message.last_scan_interval_s),
        "max_scan_interval_s": float(message.max_scan_interval_s),
        "stale": bool(message.stale),
        "first_failure": _failure(message.first_failure),
        "last_failure": _failure(message.last_failure),
        "last_recovery_phase": message.last_recovery_phase,
        "last_recovery_ok": bool(message.last_recovery_ok),
        "last_recovery_quiet_s": float(message.last_recovery_quiet_s),
        "last_recovery_duration_ms": float(message.last_recovery_duration_ms),
        "last_recovery_mono_ns": int(message.last_recovery_mono_ns),
        "consumer_successful_scans": int(message.consumer_successful_scans),
        "consumer_failed_scans": int(message.consumer_failed_scans),
        "consumer_last_error": message.consumer_last_error,
        "consumer_first_failure_present": bool(message.consumer_first_failure_present),
        "consumer_first_failure_reason": message.consumer_first_failure_reason,
        "consumer_first_failure_mono_ns": int(message.consumer_first_failure_mono_ns),
        "clock_domain": message.clock_domain, "clock_epoch": message.clock_epoch,
    }


def _window(message: FreshnessWindowStatus) -> dict[str, Any]:
    return {
        "header": _stamp(message), "session_id": message.session_id,
        "session_epoch": int(message.session_epoch),
        "stream_epoch": _identity_from_text(message.stream_epoch),
        "window_id": _identity_from_text(message.window_id),
        "duration_ms": int(message.duration_ms),
        "remaining_ms_at_publish": float(message.remaining_ms_at_publish),
        "issued_mono_ns": int(message.issued_mono_ns),
        "deadline_mono_ns": int(message.deadline_mono_ns),
        "clock_domain": message.clock_domain, "clock_epoch": message.clock_epoch,
        "command_transport_ready": bool(message.command_transport_ready),
        "reason": message.reason,
    }


def _receipt(message: CommandReceipt) -> dict[str, Any]:
    return {
        "header": _stamp(message), "data_identity": message.data_identity,
        "transport": message.transport, "session_id": message.session_id,
        "session_epoch": int(message.session_epoch),
        "stream_epoch": message.stream_epoch,
        "frame_seq": int(message.frame_seq), "sample_seq": int(message.sample_seq),
        "state": message.state, "reason": message.reason,
        "detail": message.detail, "admitted": bool(message.admitted),
        "publisher_sent": bool(message.publisher_sent),
        "emitted": bool(message.emitted),
        "controller_acknowledged": message.controller_acknowledged,
        "execution_observed": message.execution_observed,
        "received_mono_ns": int(message.received_mono_ns),
        "deadline_mono_ns": int(message.deadline_mono_ns),
        "window_remaining_ms": float(message.window_remaining_ms),
        "window_age_ms": float(message.window_age_ms),
    }


class UpperDdsBridge(Node):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("lelab_upper_dds_bridge")
        self.args = args
        self.lock = threading.Lock()
        self.started_mono_ns = time.monotonic_ns()
        self.clock_epoch = "upper_monotonic:" + str(os.getpid())
        self.feedback: dict[str, Any] | None = None
        self.feedback_receipt_mono_ns: int | None = None
        self.health: dict[str, Any] | None = None
        self.health_receipt_mono_ns: int | None = None
        self.window: dict[str, Any] | None = None
        self.window_receipt_mono_ns: int | None = None
        self.receipt: dict[str, Any] | None = None
        self.receipt_receipt_mono_ns: int | None = None
        self.receipts: deque[dict[str, Any]] = deque(maxlen=32)
        self.counts = {"feedback": 0, "health": 0, "windows": 0, "receipts": 0,
                       "invalid": 0, "clients": 0, "bytes": 0, "hellos": 0,
                       "commands": 0, "commands_rejected": 0, "preempts": 0}
        self.last_error: str | None = None
        self.last_command_mono_ns = 0
        self.command_key: bytes | None = None
        self.command_key_id: str | None = None
        if args.command_key_file:
            try:
                self.command_key = load_auth_key(args.command_key_file)
            except (OSError, ValueError) as error:
                raise SystemExit(f"refusing the command key: {error}")
            self.command_key_id = auth_key_id(self.command_key)
        self._hello_pub = self.create_publisher(
            TransportHello, args.hello_topic, _reliable())
        self._frame_pub = self.create_publisher(
            TeleopFrame, args.command_topic, _qos())
        self._preempt_pub = self.create_publisher(
            Preempt, args.preempt_topic, _reliable())
        self.create_subscription(MeasuredServoFrame, args.feedback_topic,
                                 self._on_feedback, _qos())
        self.create_subscription(DriverHealth, args.health_topic,
                                 self._on_health, _reliable())
        self.create_subscription(FreshnessWindowStatus, args.window_topic,
                                 self._on_window, _reliable())
        self.create_subscription(CommandReceipt, args.receipt_topic,
                                 self._on_receipt, _reliable(10))
        self._ipc_server: socket.socket | None = None
        self._socket_thread: threading.Thread | None = None
        self._ipc_clients: list[socket.socket] = []
        self._home_server = None
        if not args.no_services:
            group = ReentrantCallbackGroup()
            self.create_service(SessionControl, "~/session_control", self._session_control,
                                callback_group=group)
            self.create_service(ClearEstop, "~/clear_estop", self._clear_estop,
                                callback_group=group)
            self._home_server = ActionServer(
                self, HomeArm, "~/home_arm", execute_callback=self._execute_home,
                goal_callback=lambda _goal: GoalResponse.ACCEPT,
                cancel_callback=lambda _goal: CancelResponse.ACCEPT,
                callback_group=group)

    # -- subscriptions -----------------------------------------------------
    def _on_feedback(self, message: MeasuredServoFrame) -> None:
        try:
            document = _measured(message)
            if document["owner_instance"] < 1 or document["scan_sequence"] < 0:
                raise ValueError("feedback_identity_invalid")
            if len(document["joint_names"]) != 6 or len(document["raw"]) != 6:
                raise ValueError("feedback_shape_invalid")
        except (TypeError, ValueError) as error:
            with self.lock:
                self.counts["invalid"] += 1
                self.last_error = str(error)
            return
        with self.lock:
            self.feedback = document
            self.feedback_receipt_mono_ns = time.monotonic_ns()
            self.counts["feedback"] += 1

    def _on_health(self, message: DriverHealth) -> None:
        try:
            document = _health(message)
        except (TypeError, ValueError) as error:
            with self.lock:
                self.counts["invalid"] += 1
                self.last_error = str(error)
            return
        with self.lock:
            self.health = document
            self.health_receipt_mono_ns = time.monotonic_ns()
            self.counts["health"] += 1

    def _on_window(self, message: FreshnessWindowStatus) -> None:
        try:
            document = _window(message)
        except (TypeError, ValueError) as error:
            with self.lock:
                self.counts["invalid"] += 1
                self.last_error = str(error)
            return
        with self.lock:
            self.window = document
            self.window_receipt_mono_ns = time.monotonic_ns()
            self.counts["windows"] += 1

    def _on_receipt(self, message: CommandReceipt) -> None:
        try:
            document = _receipt(message)
        except (TypeError, ValueError) as error:
            with self.lock:
                self.counts["invalid"] += 1
                self.last_error = str(error)
            return
        with self.lock:
            received = time.monotonic_ns()
            document = dict(document)
            document["receipt_mono_ns"] = received
            self.receipt = document
            self.receipt_receipt_mono_ns = received
            self.receipts.append(document)
            self.counts["receipts"] += 1

    # -- snapshot ----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic_ns()
        with self.lock:
            feedback = None if self.feedback is None else dict(self.feedback)
            health = None if self.health is None else dict(self.health)
            window = None if self.window is None else dict(self.window)
            receipt = None if self.receipt is None else dict(self.receipt)
            receipt_list = [dict(item) for item in self.receipts]
            feedback_receipt = self.feedback_receipt_mono_ns
            health_receipt = self.health_receipt_mono_ns
            window_receipt = self.window_receipt_mono_ns
            receipt_receipt = self.receipt_receipt_mono_ns
            counts = dict(self.counts)
            last_error = self.last_error
        if feedback is not None:
            feedback["receipt_mono_ns"] = feedback_receipt
            feedback["receipt_age_ns"] = max(0, now - feedback_receipt)
        if health is not None:
            health["receipt_mono_ns"] = health_receipt
            health["receipt_age_ns"] = max(0, now - health_receipt)
        if window is not None:
            window["receipt_mono_ns"] = window_receipt
            window["receipt_age_ns"] = max(0, now - window_receipt)
            window["remaining_ms_conservative"] = max(
                0.0, float(window["remaining_ms_at_publish"])
                - window["receipt_age_ns"] / 1_000_000.0)
        if receipt is not None:
            receipt["receipt_mono_ns"] = receipt_receipt
            receipt["receipt_age_ns"] = max(0, now - receipt_receipt)
        for item in receipt_list:
            stamp = item.get("receipt_mono_ns") or now
            item["receipt_age_ns"] = max(0, now - stamp)
        return {
            "schema": SCHEMA,
            "bridge": {
                "pid": os.getpid(), "started_mono_ns": self.started_mono_ns,
                "clock_domain": "upper_process_monotonic",
                "clock_epoch": self.clock_epoch,
                "data_identity": "simulated_offline" if self.args.simulated
                                 else "hardware",
                "feedback_topic": self.args.feedback_topic,
                "health_topic": self.args.health_topic,
                "window_topic": self.args.window_topic,
                "receipt_topic": self.args.receipt_topic,
                "command_transport": "disabled_no_auth" if not self.args.command_key_file
                                     else "configured",
                "command_enabled": self.command_key is not None,
                "auth_key_id": self.command_key_id,
            },
            "feedback": feedback, "health": health,
            "window": window, "receipt": receipt, "receipts": receipt_list,
            "counts": counts, "last_error": last_error,
            "snapshot_mono_ns": now,
        }

    # -- local IPC ---------------------------------------------------------
    def start_socket(self) -> None:
        path = Path(self.args.socket)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if not stat.S_ISSOCK(path.stat().st_mode):
                raise SystemExit(f"refusing to replace a non-socket path: {path}")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(path))
            except OSError:
                path.unlink()
            else:
                raise SystemExit(f"another bridge is listening on {path}")
            finally:
                probe.close()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o660)
        if self.args.socket_uid >= 0:
            os.chown(path, int(self.args.socket_uid),
                     int(self.args.socket_gid) if self.args.socket_gid >= 0 else -1)
        server.listen(MAX_CLIENTS)
        server.settimeout(0.5)
        self._ipc_server = server
        self._socket_thread = threading.Thread(
            target=self._accept_loop, name="upper-bridge-ipc", daemon=True)
        self._socket_thread.start()
        self.get_logger().info(f"feedback snapshot socket: {path}")

    def stop_socket(self) -> None:
        server, self._ipc_server = self._ipc_server, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        with self.lock:
            clients = list(self._ipc_clients)
            self._ipc_clients.clear()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        path = Path(self.args.socket)
        try:
            if path.exists() and stat.S_ISSOCK(path.stat().st_mode):
                path.unlink()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        assert self._ipc_server is not None
        while self._ipc_server is not None:
            try:
                client, _ = self._ipc_server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self.lock:
                if len(self._ipc_clients) >= MAX_CLIENTS:
                    client.close()
                    continue
                self._ipc_clients.append(client)
                self.counts["clients"] += 1
            client.settimeout(1.0)
            threading.Thread(target=self._client_loop, args=(client,),
                             name="upper-bridge-client", daemon=True).start()

    def _client_loop(self, client: socket.socket) -> None:
        buffer = b""
        try:
            while True:
                try:
                    chunk = client.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > MAX_REQUEST_BYTES:
                    self._send(client, {"ok": False, "error": "request_too_large"})
                    return
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        request = json.loads(line)
                        operation = request.get("op", "snapshot") if isinstance(request, dict) else None
                    except (ValueError, TypeError):
                        self._send(client, {"ok": False, "error": "invalid_json"})
                        continue
                    if operation == "snapshot":
                        payload = self.snapshot()
                        payload["ok"] = True
                        self._send(client, payload)
                    elif isinstance(operation, str) and operation.startswith("dds_"):
                        self._send(client, self._handle_command(request))
                    elif operation == "ping":
                        self._send(client, {"ok": True, "pong_mono_ns": time.monotonic_ns()})
                    elif operation == "quit":
                        return
                    else:
                        self._send(client, {"ok": False, "error": "unknown_operation"})
        finally:
            with self.lock:
                if client in self._ipc_clients:
                    self._ipc_clients.remove(client)
            try:
                client.close()
            except OSError:
                pass

    def _send(self, client: socket.socket, payload: dict[str, Any]) -> None:
        raw = (json.dumps(payload, sort_keys=True) + "\n").encode()
        client.settimeout(SOCKET_SEND_TIMEOUT_S)
        try:
            client.sendall(raw)
        except OSError:
            with self.lock:
                if client in self._ipc_clients:
                    self._ipc_clients.remove(client)
            try:
                client.close()
            except OSError:
                pass
            return
        with self.lock:
            self.counts["bytes"] += len(raw)

    # -- authenticated command publication ---------------------------------
    def _reject_command(self, reason: str) -> dict[str, Any]:
        with self.lock:
            self.counts["commands_rejected"] += 1
            self.last_error = reason
        return {"ok": False, "error": reason}

    def _command_key_ready(self, request: dict[str, Any]) -> str | None:
        if self.command_key is None:
            return "dds_command_disabled:no_auth_key"
        if request.get("auth_key_id") != self.command_key_id:
            return "dds_command_auth_key_unknown"
        return None

    def _publish_hello(self, request: dict[str, Any]) -> dict[str, Any]:
        refusal = self._command_key_ready(request)
        if refusal:
            return self._reject_command(refusal)
        try:
            transport = str(request.get("transport") or "dds_v1")
            session_id = str(request["session_id"])
            session_epoch = int(request["session_epoch"])
            bundle = str(request["calibration_bundle_hash"])
            sender_id = str(request["sender_id"])
            transport_epoch = str(request["transport_epoch"])
            ticket = str(request.get("auth_ticket") or "")
        except (KeyError, TypeError, ValueError):
            return self._reject_command("dds_hello_invalid_fields")
        if transport != "dds_v1":
            return self._reject_command("dds_hello_transport_unsupported")
        if not verify_hello_auth_ticket(
                self.command_key, ticket, transport=transport,
                session_id=session_id, session_epoch=session_epoch,
                calibration_bundle_hash=bundle, sender_id=sender_id,
                transport_epoch=transport_epoch):
            return self._reject_command("dds_hello_auth_failed")
        message = TransportHello()
        message.transport = transport
        message.session_id = session_id
        message.session_epoch = session_epoch
        message.calibration_bundle_hash = bundle
        message.sender_id = sender_id
        message.auth_key_id = str(request.get("auth_key_id") or "")
        message.auth_ticket = ticket
        message.transport_epoch = transport_epoch
        self._hello_pub.publish(message)
        with self.lock:
            self.counts["hellos"] += 1
            self.last_error = None
        return {"ok": True, "published": "hello"}

    def _publish_frame(self, request: dict[str, Any], now_ns: int) -> dict[str, Any]:
        refusal = self._command_key_ready(request)
        if refusal:
            return self._reject_command(refusal)
        if now_ns - self.last_command_mono_ns < int(1e9 / MAX_COMMAND_HZ):
            return self._reject_command("dds_command_rate_limited")
        frame = request.get("frame")
        sender_id = str(request.get("sender_id") or "")
        transport_epoch = str(request.get("transport_epoch") or "")
        ticket = str(request.get("auth_ticket") or "")
        try:
            if not verify_frame_auth_ticket(
                    self.command_key, ticket, frame, sender_id=sender_id,
                    transport_epoch=transport_epoch):
                return self._reject_command("dds_frame_auth_failed")
            message = TeleopFrame()
            frame_to_message(message, frame, sender_id=sender_id,
                             transport_epoch=transport_epoch,
                             auth_key_id=str(request.get("auth_key_id") or ""),
                             auth_ticket=ticket)
        except (FrameCodecError, KeyError, TypeError, ValueError) as error:
            return self._reject_command(f"dds_frame_invalid:{type(error).__name__}:{error}")
        self._frame_pub.publish(message)
        self.last_command_mono_ns = now_ns
        with self.lock:
            self.counts["commands"] += 1
            self.last_error = None
        return {"ok": True, "published": "frame", "frame_seq": int(frame["frame_seq"])}

    def _publish_preempt(self, request: dict[str, Any]) -> dict[str, Any]:
        refusal = self._command_key_ready(request)
        if refusal:
            return self._reject_command(refusal)
        try:
            action = str(request["action"])
            reason = str(request.get("reason") or "")
            session_id = str(request["session_id"])
            session_epoch = int(request["session_epoch"])
            stream_epoch = str(request["stream_epoch"])
            sender_id = str(request["sender_id"])
            ticket = str(request.get("auth_ticket") or "")
        except (KeyError, TypeError, ValueError):
            return self._reject_command("dds_preempt_invalid_fields")
        if action not in ("estop", "pause_all"):
            return self._reject_command("dds_preempt_action_unsupported")
        if not verify_preempt_auth_ticket(
                self.command_key, ticket, action=action, reason=reason,
                session_id=session_id, session_epoch=session_epoch,
                stream_epoch=stream_epoch, sender_id=sender_id):
            return self._reject_command("dds_preempt_auth_failed")
        message = Preempt()
        message.action = action
        message.reason = reason
        message.session_id = session_id
        message.session_epoch = session_epoch
        message.stream_epoch = stream_epoch
        message.sender_id = sender_id
        message.auth_key_id = str(request.get("auth_key_id") or "")
        message.auth_ticket = ticket
        self._preempt_pub.publish(message)
        with self.lock:
            self.counts["preempts"] += 1
            self.last_error = None
        return {"ok": True, "published": "preempt"}

    def _handle_command(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = request.get("op")
        if operation == "dds_hello":
            return self._publish_hello(request)
        if operation == "dds_frame":
            return self._publish_frame(request, time.monotonic_ns())
        if operation == "dds_preempt":
            return self._publish_preempt(request)
        return self._reject_command("unknown_command_operation")

    # -- thin flow services ------------------------------------------------
    def _http(self, path: str, body: dict[str, Any] | None = None,
              method: str = "GET") -> tuple[int, dict[str, Any]]:
        url = self.args.app_url.rstrip("/") + path
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.args.http_timeout_s) as response:
                payload = response.read(1_048_576)
                return response.status, json.loads(payload or b"{}")
        except urllib.error.HTTPError as error:
            payload = error.read(4096)
            try:
                document = json.loads(payload or b"{}")
            except ValueError:
                document = {"detail": payload.decode(errors="replace")}
            return error.code, document
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            return 0, {"detail": f"{type(error).__name__}:{error}"}

    def _session_control(self, request, response):
        action = str(request.action or "status")
        body: dict[str, Any] = {}
        if request.request_json:
            try:
                parsed = json.loads(request.request_json)
                body = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                response.success = False
                response.detail = "request_json_invalid"
                return response
        if request.session_id:
            body.setdefault("session_id", request.session_id)
        if request.session_epoch:
            body.setdefault("session_epoch", int(request.session_epoch))
        if request.owner:
            body.setdefault("owner", request.owner)
        if request.reason:
            body.setdefault("reason", request.reason)
        if request.ttl_s and "ttl_s" not in body:
            body["ttl_s"] = float(request.ttl_s)
        if action == "status":
            status, document = self._http("/ros-mobile/hybrid/status")
        elif action == "acquire":
            body["action"] = "acquire"
            status, document = self._http("/ros-mobile/hybrid/session", body, "POST")
        elif action == "renew":
            status, document = self._http("/ros-mobile/hybrid/heartbeat", body, "POST")
        elif action == "release":
            body["action"] = "release"
            status, document = self._http("/ros-mobile/hybrid/session", body, "POST")
        elif action == "clear_estop":
            status, document = self._http("/ros-mobile/hybrid/clear-estop", {}, "POST")
        else:
            response.success = False
            response.detail = f"unknown_action:{action}"
            return response
        response.success = 200 <= status < 300
        response.state = str(document.get("state") or "")
        response.detail = str(document.get("detail") or "")[:512]
        response.response_json = json.dumps(document, sort_keys=True)[:65536]
        return response

    def _clear_estop(self, request, response):
        status, document = self._http("/ros-mobile/hybrid/clear-estop", {}, "POST")
        state = str(document.get("state") or "")
        error = str(document.get("last_error") or "")
        cleared = (200 <= status < 300 and state != "estop"
                   and not error.startswith("estop_clear_"))
        response.success = bool(cleared)
        response.confirmed_state = ("cleared" if cleared
                                    else "pending_or_failed" if status else "pending_or_failed")
        response.detail = (f"http={status};state={state};last_error={error}"[:512])
        response.motion_granted = False
        response.response_json = json.dumps(document, sort_keys=True)[:65536]
        return response

    def _execute_home(self, goal_handle):
        goal = goal_handle.request
        body = {"action": "start", "session_id": goal.session_id,
                "session_epoch": int(goal.session_epoch),
                "request_id": goal.request_id}
        status, document = self._http("/ros-mobile/hybrid/home", body, "POST")
        result = HomeArm.Result()
        result.result_json = json.dumps(document, sort_keys=True)[:65536]
        if not 200 <= status < 300:
            result.success = False
            result.state = "fault"
            result.detail = f"start_failed:http={status}:{document.get('detail','')}"[:512]
            goal_handle.abort()
            return result
        deadline = time.monotonic() + self.args.home_timeout_s
        while time.monotonic() < deadline:
            if goal_handle.is_cancel_requested:
                self._http("/ros-mobile/hybrid/home",
                           {**body, "action": "cancel"}, "POST")
                goal_handle.canceled()
                result.success = False
                result.state = "cancelled"
                result.detail = "cancel_requested"
                return result
            status, state_document = self._http("/ros-mobile/hybrid/home")
            state = str(state_document.get("state") or "unknown")
            feedback = HomeArm.Feedback()
            progress = state_document.get("progress_pct")
            feedback.progress = (float(progress) / 100.0
                                 if isinstance(progress, (int, float))
                                 and not isinstance(progress, bool) else 0.0)
            feedback.phase = state
            feedback.detail = str(state_document.get("current_joint")
                                  or state_document.get("last_error") or "")[:256]
            goal_handle.publish_feedback(feedback)
            if state == "complete":
                goal_handle.succeed()
                result.success = True
                result.state = state
                result.detail = "homing_complete"
                result.result_json = json.dumps(state_document, sort_keys=True)[:65536]
                return result
            if state in ("fault", "cancelled"):
                goal_handle.abort()
                result.success = False
                result.state = state
                result.detail = str(state_document.get("last_error") or state)[:512]
                return result
            time.sleep(0.2)
        self._http("/ros-mobile/hybrid/home", {**body, "action": "cancel"}, "POST")
        goal_handle.abort()
        result.success = False
        result.state = "timeout"
        result.detail = f"homing_timeout_after_{self.args.home_timeout_s}s"
        return result

    def destroy_bridge(self) -> None:
        self.stop_socket()
        if self._home_server is not None:
            self._home_server.destroy()
        self.destroy_node()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feedback-topic", default="/lelab_ros/feedback/measured")
    parser.add_argument("--health-topic", default="/lelab_ros/feedback/health")
    parser.add_argument("--window-topic", default="/lelab_ros/control/window")
    parser.add_argument("--receipt-topic", default="/lelab_ros/control/receipt")
    parser.add_argument("--hello-topic", default="/lelab_ros/command/hello")
    parser.add_argument("--command-topic", default="/lelab_ros/command/frame")
    parser.add_argument("--preempt-topic", default="/lelab_ros/command/preempt")
    parser.add_argument("--socket", default="/work/out/dds_r1/bridge.sock")
    parser.add_argument("--socket-uid", type=int, default=-1,
                        help="optional owner for the local socket (host app uid)")
    parser.add_argument("--socket-gid", type=int, default=-1)
    parser.add_argument("--app-url", default="http://127.0.0.1:18287")
    parser.add_argument("--http-timeout-s", type=float, default=2.0)
    parser.add_argument("--home-timeout-s", type=float, default=120.0)
    parser.add_argument("--command-key-file", default="")
    parser.add_argument("--simulated", action="store_true")
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--no-services", action="store_true")
    args = parser.parse_args()
    if args.home_timeout_s <= 0 or args.http_timeout_s <= 0:
        raise SystemExit("timeouts must be positive")
    rclpy.init()
    bridge = UpperDdsBridge(args)
    bridge.start_socket()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(bridge)
    started = time.monotonic()
    try:
        while rclpy.ok():
            executor.spin_once(timeout_sec=0.2)
            if args.duration and time.monotonic() - started >= args.duration:
                break
    except KeyboardInterrupt:
        pass
    finally:
        bridge.destroy_bridge()
        executor.shutdown()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
