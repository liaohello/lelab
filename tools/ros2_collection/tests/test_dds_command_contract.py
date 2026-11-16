"""DDS command identity, codec, selector and car-side transport arbitration."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "ros2_ws/src/lelab_mobile_agent"), str(ROOT / "app_ros_mobile")]

from lelab_mobile_agent.teleop_frame_v1 import (
    FrameError, TransportSelector, frame_auth_ticket, frame_v1,
    hello_auth_ticket, load_auth_key, preempt_auth_ticket,
    verify_frame_auth_ticket, verify_hello_auth_ticket,
    verify_preempt_auth_ticket,
)
from lelab_mobile_agent.teleop_stream import token_digest
from lelab.hybrid_channel import command_v1

try:
    from lelab_mobile_agent.car_control_process import CarControlRuntime
    from lelab_mobile_agent.licence_ipc import LicenceMirror, licence_record
    HAS_RUNTIME = True
except Exception as error:  # noqa: BLE001
    HAS_RUNTIME = False
    IMPORT_ERROR = f"{type(error).__name__}: {error}"

from lelab_mobile_agent.dds_frame_codec import (  # noqa: E402
    FrameCodecError, frame_to_message, teleop_message_to_frame, validate_frame,
)

BUNDLE = "d" * 64
TOKEN = "lease-token-not-carried-on-dds"
KEY = b"k" * 48
JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"]


def sample_frame(*, frame_seq=1, stream_epoch=7, window_id=7, session_id="sess-dds",
                 session_epoch=88, ttl_ms=150, positions=(0.1, 0.2, 0.3, 0.4, 0.5)):
    common = dict(session_id=session_id, session_epoch=session_epoch,
                  source="so101_leader", sample_seq=frame_seq, ttl_ms=ttl_ms,
                  calibration_bundle_hash=BUNDLE, clutch=True)
    arm = command_v1(channel="arm", seq=frame_seq,
                     target={"joint_names": JOINTS, "positions_rad": list(positions),
                             "leader_rad": [value + 0.01 for value in positions]},
                     **common)
    gripper = command_v1(channel="gripper", seq=frame_seq,
                         target={"joint_name": "r_joint", "position_rad": 0.02},
                         **common)
    return frame_v1(session_id=session_id, session_epoch=session_epoch,
                    calibration_bundle_hash=BUNDLE, frame_seq=frame_seq,
                    stream_epoch=stream_epoch, sample_seq=frame_seq,
                    freshness_window_id=window_id, commands={"arm": arm, "gripper": gripper})


def test_a_frame_ticket_covers_the_whole_frame_and_binds_the_sender():
    frame = sample_frame()
    ticket = frame_auth_ticket(KEY, frame, sender_id="upper-1",
                               transport_epoch="dds-epoch-1")
    assert verify_frame_auth_ticket(KEY, ticket, frame, sender_id="upper-1",
                                    transport_epoch="dds-epoch-1")
    assert not verify_frame_auth_ticket(KEY, ticket, frame, sender_id="upper-2",
                                        transport_epoch="dds-epoch-1")
    tampered = json.loads(json.dumps(frame))
    tampered["commands"]["arm"]["target"]["positions_rad"][0] = 1.5
    assert not verify_frame_auth_ticket(KEY, ticket, tampered, sender_id="upper-1",
                                        transport_epoch="dds-epoch-1")
    assert not verify_frame_auth_ticket(b"x" * 48, ticket, frame, sender_id="upper-1",
                                        transport_epoch="dds-epoch-1")


def test_hello_and_preempt_tickets_are_separate():
    hello = hello_auth_ticket(KEY, transport="dds_v1", session_id="s", session_epoch=1,
                              calibration_bundle_hash=BUNDLE, sender_id="upper-1",
                              transport_epoch="e1")
    assert verify_hello_auth_ticket(KEY, hello, transport="dds_v1", session_id="s",
                                    session_epoch=1, calibration_bundle_hash=BUNDLE,
                                    sender_id="upper-1", transport_epoch="e1")
    assert not verify_hello_auth_ticket(KEY, hello, transport="dds_v1", session_id="s",
                                        session_epoch=2, calibration_bundle_hash=BUNDLE,
                                        sender_id="upper-1", transport_epoch="e1")
    preempt = preempt_auth_ticket(KEY, action="estop", reason="operator",
                                  session_id="s", session_epoch=1,
                                  stream_epoch="7", sender_id="upper-1")
    assert verify_preempt_auth_ticket(KEY, preempt, action="estop", reason="operator",
                                      session_id="s", session_epoch=1,
                                      stream_epoch="7", sender_id="upper-1")
    assert not verify_preempt_auth_ticket(KEY, preempt, action="pause_all",
                                          reason="operator", session_id="s",
                                          session_epoch=1, stream_epoch="7",
                                          sender_id="upper-1")


def test_a_key_file_must_not_be_group_or_world_readable(tmp_path):
    path = tmp_path / "key"
    path.write_bytes(KEY)
    path.chmod(0o600)
    assert load_auth_key(path) == KEY
    path.chmod(0o644)
    with pytest.raises(FrameError) as caught:
        load_auth_key(path)
    assert caught.value.reason == "auth_key_permissions"
    path.chmod(0o600)
    path.write_bytes(b"short")
    with pytest.raises(FrameError) as caught:
        load_auth_key(path)
    assert caught.value.reason == "auth_key_too_short"


def test_the_codec_round_trips_the_exact_admission_frame():
    frame = sample_frame()
    message = types.SimpleNamespace()
    frame_to_message(message, frame, sender_id="upper-1", transport_epoch="e1",
                     auth_key_id="k1", auth_ticket="ticket")
    decoded = teleop_message_to_frame(message)
    assert decoded == frame
    assert message.arm_ttl_ms == 150
    assert message.gripper_joint == "r_joint"


@pytest.mark.parametrize("mutation,reason", [
    (lambda f: f["commands"]["arm"]["target"].update(joint_names=["joint2", "joint1"]),
     "joint order"),
    (lambda f: f["commands"]["arm"].update(ttl_ms=201), "ttl"),
    (lambda f: f.update(extra=True), "fields wrong"),
    (lambda f: f["commands"]["arm"]["target"].update(positions_rad=[0.0] * 4),
     "positions_rad"),
])
def test_the_codec_rejects_a_malformed_frame(mutation, reason):
    frame = sample_frame()
    mutation(frame)
    with pytest.raises(FrameCodecError, match=reason):
        validate_frame(frame)


def test_the_selector_accepts_one_framed_transport_and_refuses_a_switch():
    selector = TransportSelector()
    chosen = selector.negotiate(session_id="s", session_epoch=1, transport="dds_v1")
    assert chosen["transport"] == "dds_v1"
    assert chosen["legacy_apply_enabled"] is False
    assert selector.allows_legacy_apply(session_id="s", session_epoch=1) is False
    with pytest.raises(FrameError) as caught:
        selector.negotiate(session_id="s", session_epoch=1, transport="frame_stream")
    assert caught.value.reason == "transport_switch_locked"
    selector.release(session_id="s", session_epoch=1, reason="stop")
    assert selector.allows_legacy_apply(session_id="s", session_epoch=1) is True


# --------------------------------------------------------------------------
# car-side runtime arbitration
# --------------------------------------------------------------------------
if HAS_RUNTIME:
    class FakeNode:
        def create_timer(self, *_args, **_kwargs):
            return None

        def get_clock(self):
            class Clock:
                def now(self):
                    class Stamp:
                        nanoseconds = int(time.time() * 1e9)
                    return Stamp()
            return Clock()

    class FakeReadback:
        def read_snapshot(self):
            return {"available": True,
                    "positions_rad": {name: 0.0 for name in JOINTS + ["r_joint"]},
                    "servo_pulses": [500] * 6, "oldest_age_s": 0.05,
                    "scan_span_s": 0.02, "last_error": None}

    class FakeDriver:
        def __init__(self):
            self.outputs_enabled = False
            self.publish_count = 0
            self.stop_count = 0
            self.last_error = None

        def enable(self):
            self.outputs_enabled = True

        def invalidate_torque_verification(self):
            pass

        def check_only(self, name, value):
            if value != value:
                raise RuntimeError(f"nonfinite_{name}_position")
            return float(value)

        def submit_frame(self, targets):
            if not self.outputs_enabled:
                raise RuntimeError("outputs_disabled")
            self.publish_count += 1

        def stop(self, reason):
            self.outputs_enabled = False
            self.stop_count += 1
            return {"stopped": True, "reason": reason, "hold_published": True}

    def profile(*, transports, outputs="dds_v1"):
        return {
            "robot": {"arm_joints": list(JOINTS), "gripper_joint": "r_joint"},
            "limits": {"joint_positions_rad": {name: [-3.0, 3.0]
                                               for name in JOINTS + ["r_joint"]}},
            "teleop": {"max_step_rad_s": 3.0, "output_watchdog_s": 1.0,
                       "output_duration_s": 0.15},
            "follower_feedback": {"stale_after_s": 0.5, "max_scan_span_s": 0.5,
                                  "scan_period_s": 0.2},
            "runtime": {"output_transport": outputs, "allowed_transports": transports},
        }

    class RuntimeFixture:
        def __init__(self, *, transports, outputs="dds_v1"):
            self.tmp = tempfile.TemporaryDirectory()
            base = pathlib.Path(self.tmp.name)
            self.mirror = LicenceMirror(base / "licence.sock")
            self.driver = FakeDriver()
            self.runtime = CarControlRuntime(
                FakeNode(), profile=profile(transports=transports, outputs=outputs),
                control_hz=10.0, window_ms=250, licence=self.mirror,
                readback=FakeReadback(), driver=self.driver,
                data_identity="simulated_offline", status_path=base / "status.json",
                bundle_hash=BUNDLE, max_ttl_ms=200)

        def licence(self, *, generation=1, outputs=True, session_id="sess-dds",
                    session_epoch=88):
            self.mirror.apply(licence_record(
                control_generation=generation, session_id=session_id,
                session_epoch=session_epoch, calibration_bundle_hash=BUNDLE,
                permits={"arm": True, "gripper": True}, outputs_enabled=outputs,
                token_sha256=token_digest(TOKEN), ttl_ms=300))

        def close(self):
            self.tmp.cleanup()

    def test_dds_hello_opens_the_only_dds_epoch_and_frames_are_admitted():
        fixture = RuntimeFixture(transports=["dds_v1"])
        try:
            fixture.licence()
            ok, welcome = fixture.runtime.on_dds_hello(
                session_id="sess-dds", session_epoch=88, bundle_hash=BUNDLE,
                sender_id="upper-1", transport_epoch="e1")
            assert ok is True, welcome
            assert fixture.runtime.active_transport == "dds_v1"
            frame = sample_frame(stream_epoch=welcome["stream_epoch"],
                                 window_id=fixture.runtime.windows.current_id)
            receipt = fixture.runtime.on_frame(frame, transport="dds_v1")
            assert receipt["state"] == "admitted", receipt
            fixture.runtime._tick_locked()
            assert fixture.driver.publish_count == 1
        finally:
            fixture.close()

    def test_the_same_session_cannot_also_open_frame_stream():
        fixture = RuntimeFixture(transports=["dds_v1"])
        try:
            fixture.licence()
            assert fixture.runtime.on_dds_hello(
                session_id="sess-dds", session_epoch=88, bundle_hash=BUNDLE,
                sender_id="upper-1", transport_epoch="e1")[0] is True
            ok, reason = fixture.runtime.on_hello("sess-dds", 88, TOKEN)
            assert ok is False and "transport" in reason
        finally:
            fixture.close()

    def test_a_frame_from_the_wrong_transport_is_refused():
        fixture = RuntimeFixture(transports=["dds_v1"])
        try:
            fixture.licence()
            ok, welcome = fixture.runtime.on_dds_hello(
                session_id="sess-dds", session_epoch=88, bundle_hash=BUNDLE,
                sender_id="upper-1", transport_epoch="e1")
            assert ok
            frame = sample_frame(stream_epoch=welcome["stream_epoch"],
                                 window_id=fixture.runtime.windows.current_id)
            receipt = fixture.runtime.on_frame(frame, transport="frame_stream_v1")
            assert receipt["state"] == "refused"
            assert receipt["reason"] == "transport_mismatch"
            assert fixture.driver.publish_count == 0
        finally:
            fixture.close()

    def test_a_dds_profile_refuses_frame_stream_hello():
        fixture = RuntimeFixture(transports=["dds_v1"])
        try:
            fixture.licence()
            ok, reason = fixture.runtime.on_hello("sess-dds", 88, TOKEN)
            assert ok is False and reason.startswith("transport_not_enabled_in_profile")
        finally:
            fixture.close()

    def test_a_frame_stream_profile_refuses_dds_hello():
        fixture = RuntimeFixture(transports=["frame_stream_v1"], outputs="frame_stream")
        try:
            fixture.licence()
            ok, reason = fixture.runtime.on_dds_hello(
                session_id="sess-dds", session_epoch=88, bundle_hash=BUNDLE,
                sender_id="upper-1", transport_epoch="e1")
            assert ok is False and reason.startswith("transport_not_enabled_in_profile")
        finally:
            fixture.close()

    def test_switching_transport_after_a_session_requires_output_revocation():
        fixture = RuntimeFixture(transports=["dds_v1", "frame_stream_v1"])
        try:
            fixture.licence(outputs=True)
            assert fixture.runtime.on_dds_hello(
                session_id="sess-dds", session_epoch=88, bundle_hash=BUNDLE,
                sender_id="upper-1", transport_epoch="e1")[0] is True
            # The agent's new session exists, but the old DDS output is still enabled.
            fixture.licence(outputs=True, session_id="sess-new", session_epoch=99,
                            generation=2)
            ok, reason = fixture.runtime.on_hello("sess-new", 99, TOKEN)
            assert ok is False
            assert reason == "transport_switch_requires_output_revocation"
            # A released output permission allows the explicit new session.
            fixture.licence(outputs=False, session_id="sess-new", session_epoch=99,
                            generation=3)
            ok, reason = fixture.runtime.on_hello("sess-new", 99, TOKEN)
            assert ok is True, reason
            assert fixture.runtime.active_transport == "frame_stream_v1"
        finally:
            fixture.close()

    def test_a_repeated_dds_hello_is_idempotent_and_keeps_the_epoch():
        fixture = RuntimeFixture(transports=["dds_v1"])
        try:
            fixture.licence()
            first = fixture.runtime.on_dds_hello(
                session_id="sess-dds", session_epoch=88, bundle_hash=BUNDLE,
                sender_id="upper-1", transport_epoch="e1")
            epoch = fixture.runtime.stream_epoch
            second = fixture.runtime.on_dds_hello(
                session_id="sess-dds", session_epoch=88, bundle_hash=BUNDLE,
                sender_id="upper-1", transport_epoch="e1")
            assert first[0] is True and second[0] is True
            assert second[1]["idempotent"] is True
            assert fixture.runtime.stream_epoch == epoch
        finally:
            fixture.close()
