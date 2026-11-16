"""Car-side authenticated DDS command receiver.

This is a transport adapter, not a second control path: every accepted frame is
reconstructed into the same ``teleop_frame_v1`` dict and passed to
``CarControlRuntime.on_frame`` so the existing FrameAdmission, freshness window,
licence mirror and control tick remain the only gate before a servo write.

If no pre-shared key is provisioned, the subscriptions exist but every hello and
frame is refused with ``dds_command_disabled``.  Nothing silently degrades to an
unauthenticated command path.
"""
from __future__ import annotations

from typing import Any

from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy

from lelab_mobile_interfaces.msg import Preempt, TeleopFrame, TransportHello

from .dds_frame_codec import FrameCodecError, teleop_message_to_frame
from .teleop_frame_v1 import (
    FrameError, auth_key_id, load_auth_key, verify_frame_auth_ticket,
    verify_hello_auth_ticket, verify_preempt_auth_ticket,
)


def _best_effort() -> QoSProfile:
    return QoSProfile(reliability=QoSReliabilityPolicy.BEST_EFFORT,
                      history=QoSHistoryPolicy.KEEP_LAST, depth=1)


def _reliable() -> QoSProfile:
    return QoSProfile(reliability=QoSReliabilityPolicy.RELIABLE,
                      history=QoSHistoryPolicy.KEEP_LAST, depth=1)


class DdsCommandReceiver:
    def __init__(self, node: Node, runtime: Any, *, key_file: str | None = None,
                 hello_topic: str = "/lelab_ros/command/hello",
                 command_topic: str = "/lelab_ros/command/frame",
                 preempt_topic: str = "/lelab_ros/command/preempt") -> None:
        self.node = node
        self.runtime = runtime
        self.hello_topic = hello_topic
        self.command_topic = command_topic
        self.preempt_topic = preempt_topic
        self.key: bytes | None = None
        self.key_id: str | None = None
        self.load_error: str | None = None
        if key_file:
            try:
                self.key = load_auth_key(key_file)
                self.key_id = auth_key_id(self.key)
            except (OSError, FrameError) as error:
                # A configured but unusable key is a hard failure for command
                # reception; it must not look like an unconfigured, disabled path.
                self.load_error = f"{type(error).__name__}:{error}"
        self.counts = {"hellos_ok": 0, "hellos_rejected": 0,
                       "frames_received": 0, "frames_rejected": 0,
                       "preempts_ok": 0, "preempts_rejected": 0}
        self.last_error: str | None = self.load_error
        node.create_subscription(TransportHello, hello_topic, self._on_hello,
                                 _reliable())
        node.create_subscription(TeleopFrame, command_topic, self._on_frame,
                                 _best_effort())
        node.create_subscription(Preempt, preempt_topic, self._on_preempt,
                                 _reliable())

    def state(self) -> dict[str, Any]:
        return {
            "enabled": self.key is not None,
            "key_id": self.key_id,
            "load_error": self.load_error,
            "topics": {"hello": self.hello_topic, "frame": self.command_topic,
                       "preempt": self.preempt_topic},
            "counts": dict(self.counts),
            "last_error": self.last_error,
            "authentication": ("hmac_sha256_pre_shared_key"
                               if self.key is not None else "disabled_no_key"),
        }

    def _reject(self, counter: str, reason: str) -> None:
        self.counts[counter] += 1
        self.last_error = reason
        self.node.get_logger().warning(f"DDS command rejected: {reason}")

    def _on_hello(self, message: TransportHello) -> None:
        if self.key is None:
            self._reject("hellos_rejected", "dds_command_disabled:no_auth_key")
            return
        if message.transport != "dds_v1":
            self._reject("hellos_rejected", "dds_hello_transport_unsupported")
            return
        if message.auth_key_id != self.key_id:
            self._reject("hellos_rejected", "dds_hello_auth_key_unknown")
            return
        if not verify_hello_auth_ticket(
                self.key, message.auth_ticket, transport=message.transport,
                session_id=message.session_id,
                session_epoch=int(message.session_epoch),
                calibration_bundle_hash=message.calibration_bundle_hash,
                sender_id=message.sender_id,
                transport_epoch=message.transport_epoch):
            self._reject("hellos_rejected", "dds_hello_auth_failed")
            return
        ok, detail = self.runtime.on_dds_hello(
            session_id=message.session_id,
            session_epoch=int(message.session_epoch),
            bundle_hash=message.calibration_bundle_hash,
            sender_id=message.sender_id,
            transport_epoch=message.transport_epoch)
        if ok:
            self.counts["hellos_ok"] += 1
            self.last_error = None
        else:
            self._reject("hellos_rejected", f"dds_hello_refused:{detail}")

    def _on_frame(self, message: TeleopFrame) -> None:
        if self.key is None:
            self._reject("frames_rejected", "dds_command_disabled:no_auth_key")
            return
        self.counts["frames_received"] += 1
        try:
            frame = teleop_message_to_frame(message)
        except (FrameCodecError, ValueError, TypeError, KeyError) as error:
            self._reject("frames_rejected",
                         f"dds_frame_invalid:{type(error).__name__}:{error}")
            return
        if message.auth_key_id != self.key_id:
            self._reject("frames_rejected", "dds_frame_auth_key_unknown")
            return
        if not verify_frame_auth_ticket(
                self.key, message.auth_ticket, frame,
                sender_id=message.sender_id,
                transport_epoch=message.transport_epoch):
            self._reject("frames_rejected", "dds_frame_auth_failed")
            return
        if (self.runtime.dds_peer != message.sender_id
                or self.runtime.dds_transport_epoch != message.transport_epoch):
            self._reject("frames_rejected", "dds_frame_peer_or_epoch_mismatch")
            return
        result = self.runtime.on_frame(frame, transport="dds_v1")
        if result.get("state") != "admitted":
            self.counts["frames_rejected"] += 1
            self.last_error = f"admission:{result.get('reason')}"
            return
        self.last_error = None

    def _on_preempt(self, message: Preempt) -> None:
        if self.key is None:
            self._reject("preempts_rejected", "dds_command_disabled:no_auth_key")
            return
        action = str(message.action)
        if action not in ("estop", "pause_all"):
            self._reject("preempts_rejected", "dds_preempt_action_unsupported")
            return
        if not verify_preempt_auth_ticket(
                self.key, message.auth_ticket, action=action,
                reason=message.reason, session_id=message.session_id,
                session_epoch=int(message.session_epoch),
                stream_epoch=message.stream_epoch,
                sender_id=message.sender_id):
            self._reject("preempts_rejected", "dds_preempt_auth_failed")
            return
        if action == "pause_all":
            admission = getattr(self.runtime, "admission", None)
            if (admission is None
                    or str(admission.session_id) != str(message.session_id)
                    or str(admission.session_epoch) != str(message.session_epoch)
                    or str(self.runtime.stream_epoch) != str(message.stream_epoch)):
                self._reject("preempts_rejected", "dds_preempt_session_mismatch")
                return
        self.runtime.on_preempt(action, str(message.reason or "dds"))
        self.counts["preempts_ok"] += 1
        self.last_error = None
