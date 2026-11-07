"""teleop_frame_v1 contract tests. SIMULATED ONLY: no ROS graph, no serial port,
no hardware, no actuator.  These prove the whole-frame admission rules (refusal
semantics, freshness windows, latest-wins queue, transport exclusivity) and that
both runtimes read the same shared contract file.  They are NOT evidence of
Humble/Python 3.10 runtime behaviour or of any physical stopping.
"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "app_ros_mobile"), str(ROOT / "ros2_ws" / "src" / "lelab_mobile_agent")]

from lelab.hybrid_channel import command_v1, validate_target  # noqa: E402
from lelab.teleop_frame_v1 import (  # noqa: E402
    FRAME_FIELDS, FrameAdmission, FrameError, FreshnessWindow, TransportSelector,
    six_axis_targets,
)

ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"]
SESSION = "s-1"
EPOCH = "e-1"
BUNDLE = "b" * 64


class Clock:
    def __init__(self):
        self.now = 1_000_000_000

    def __call__(self) -> int:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += int(ms * 1_000_000)


def arm_command(seq=1, sample_seq=1, ttl_ms=200, positions=None, clutch=True):
    return command_v1(session_id=SESSION, session_epoch=EPOCH, channel="arm",
                      source="so101_leader", seq=seq, sample_seq=sample_seq, ttl_ms=ttl_ms,
                      calibration_bundle_hash=BUNDLE,
                      target={"joint_names": ARM_JOINTS,
                              "positions_rad": positions if positions is not None else [0.1] * 5},
                      clutch=clutch)


def gripper_command(seq=1, sample_seq=1, ttl_ms=200, position_rad=0.02, clutch=True):
    return command_v1(session_id=SESSION, session_epoch=EPOCH, channel="gripper",
                      source="so101_leader", seq=seq, sample_seq=sample_seq, ttl_ms=ttl_ms,
                      calibration_bundle_hash=BUNDLE,
                      target={"joint_name": "r_joint", "position_rad": position_rad},
                      clutch=clutch)


class Fixture(unittest.TestCase):
    """One window issuer plus one admission, wired to a permissive validator."""

    def setUp(self):
        self.clock = Clock()
        self.windows = FreshnessWindow(clock_ns=self.clock)
        self.stream_epoch = self.windows.new_stream_epoch("connect")
        self.denied: set[str] = set()
        self.admission = FrameAdmission(
            session_id=SESSION, session_epoch=EPOCH, calibration_bundle_hash=BUNDLE,
            windows=self.windows, channel_validator=self.validator,
            max_ttl_ms=200, clock_ns=self.clock, stream_epoch=self.stream_epoch)

    def validator(self, channel, command):
        """What each side really does: the frame gate delegates target legality to the
        existing per-channel validator, which raises rather than repairing."""
        if channel in self.denied:
            return {"accepted": False, "reason": "channel_not_permitted", "detail": channel}
        validate_target(channel, command["target"], command.get("clutch"))
        return {"accepted": True}

    def make(self, frame_seq=1, sample_seq=1, arm=None, gripper=None, window_id=None,
             stream_epoch=None):
        return {
            "frame_protocol_version": 1,
            "frame_seq": frame_seq,
            "stream_epoch": self.stream_epoch if stream_epoch is None else stream_epoch,
            "session_id": SESSION,
            "session_epoch": EPOCH,
            "calibration_bundle_hash": BUNDLE,
            "sample_seq": sample_seq,
            "freshness_window_id": (self.windows.current_id if window_id is None else window_id),
            "commands": {"arm": arm or arm_command(), "gripper": gripper or gripper_command()},
        }

    def open_window(self, duration_ms=120):
        self.windows.issue(duration_ms=duration_ms)


class ShapeAndIdentity(Fixture):
    def test_envelope_whitelist_rejects_extra_and_missing(self):
        self.open_window()
        frame = self.make()
        frame["sent_at_ns"] = self.clock.now
        with self.assertRaises(FrameError) as ctx:
            self.admission.accept(frame)
        self.assertEqual(ctx.exception.reason, "invalid_frame")
        self.assertEqual(self.admission.state()["next_frame_seq"], 0)

    def test_missing_gripper_refuses_whole_frame(self):
        self.open_window()
        frame = self.make()
        frame["commands"].pop("gripper")
        with self.assertRaises(FrameError) as ctx:
            self.admission.accept(frame)
        self.assertEqual(ctx.exception.reason, "incomplete_frame")

    def test_base_channel_is_not_a_frame_member(self):
        self.open_window()
        frame = self.make()
        frame["commands"]["base"] = {"vx_m_s": 0.1}
        with self.assertRaises(FrameError):
            self.admission.accept(frame)

    def test_bundle_hash_mismatch_refused(self):
        self.open_window()
        frame = self.make()
        frame["calibration_bundle_hash"] = "f" * 64
        with self.assertRaises(FrameError) as ctx:
            self.admission.accept(frame)
        self.assertEqual(ctx.exception.reason, "identity_mismatch")

    def test_command_identity_split_refused_at_construction(self):
        from lelab.teleop_frame_v1 import frame_v1

        arm = arm_command()
        arm["session_epoch"] = "other-epoch"
        with self.assertRaises(FrameError) as ctx:
            frame_v1(session_id=SESSION, session_epoch=EPOCH, calibration_bundle_hash=BUNDLE,
                     frame_seq=1, stream_epoch=self.stream_epoch, sample_seq=1,
                     freshness_window_id=self.windows.current_id,
                     commands={"arm": arm, "gripper": gripper_command()})
        self.assertEqual(ctx.exception.reason, "frame_identity_split")

    def test_non_finite_target_refused(self):
        self.open_window()
        bad = arm_command()
        bad["target"]["positions_rad"] = [float("nan")] + [0.1] * 4
        with self.assertRaises(FrameError) as ctx:
            self.admission.accept(self.make(arm=bad))
        self.assertEqual(ctx.exception.reason, "channel_refused")
        self.assertEqual(self.admission.state()["channel_seq"], {},
                         "a bad arm value must not advance the gripper either")

    def test_released_clutch_in_one_channel_refuses_the_frame(self):
        self.open_window()
        released = arm_command()
        released["clutch"] = False
        released["clutch_label"] = "released"
        with self.assertRaises(FrameError) as ctx:
            self.admission.accept(self.make(arm=released))
        self.assertEqual(ctx.exception.reason, "channel_refused")
        self.assertNotIn("gripper", self.admission.state()["channel_seq"])


class SequenceRules(Fixture):
    def test_accepted_advances_frame_and_channel_sequences(self):
        self.open_window()
        receipt = self.admission.accept(self.make(frame_seq=1, arm=arm_command(seq=1),
                                                  gripper=gripper_command(seq=1)))
        self.assertTrue(receipt["accepted"])
        state = self.admission.state()
        self.assertEqual(state["last_frame_seq"], 1)
        self.assertEqual(state["channel_seq"], {"arm": 1, "gripper": 1})

    def test_replayed_frame_seq_consumes_nothing(self):
        self.open_window()
        self.admission.accept(self.make(frame_seq=5, arm=arm_command(seq=2),
                                        gripper=gripper_command(seq=2)))
        before = self.admission.state()
        with self.assertRaises(FrameError) as ctx:
            self.admission.accept(self.make(frame_seq=5, arm=arm_command(seq=3),
                                            gripper=gripper_command(seq=3)))
        self.assertEqual(ctx.exception.reason, "frame_seq_replayed")
        after = self.admission.state()
        self.assertEqual(after["channel_seq"], before["channel_seq"])
        self.assertEqual(after["last_frame_seq"], 5)

    def test_replayed_channel_seq_refused_without_advancing_the_other(self):
        self.open_window()
        self.admission.accept(self.make(frame_seq=1, arm=arm_command(seq=4),
                                        gripper=gripper_command(seq=4)))
        with self.assertRaises(FrameError) as ctx:
            self.admission.accept(self.make(frame_seq=2, arm=arm_command(seq=4),
                                            gripper=gripper_command(seq=9)))
        self.assertEqual(ctx.exception.reason, "channel_seq_replayed")
        state = self.admission.state()
        self.assertEqual(state["channel_seq"]["gripper"], 4, "the gripper must not advance either")
        self.assertEqual(state["refusals"].get("channel_seq_replayed"), 1)

    def test_ttl_above_ceiling_refuses_whole_frame(self):
        self.open_window()
        with self.assertRaises(FrameError) as ctx:
            self.admission.accept(self.make(arm=arm_command(seq=1, ttl_ms=400)))
        self.assertEqual(ctx.exception.reason, "ttl_too_long")
        self.assertEqual(self.admission.state()["channel_seq"], {})

    def test_denied_channel_blocks_the_permitted_one(self):
        self.open_window()
        self.denied.add("gripper")
        with self.assertRaises(FrameError) as ctx:
            self.admission.accept(self.make(arm=arm_command(seq=7), gripper=gripper_command(seq=7)))
        self.assertEqual(ctx.exception.reason, "channel_not_permitted")
        self.assertNotIn("arm", self.admission.state()["channel_seq"])


class FreshnessRules(Fixture):
    def test_unknown_window_id_refused(self):
        self.open_window()
        with self.assertRaises(FrameError) as ctx:
            self.admission.accept(self.make(window_id="never-issued"))
        self.assertEqual(ctx.exception.reason, "stale_window")

    def test_expired_window_refused(self):
        self.open_window(duration_ms=50)
        self.clock.advance_ms(60)
        with self.assertRaises(FrameError) as ctx:
            self.admission.accept(self.make())
        self.assertEqual(ctx.exception.reason, "window_expired")

    def test_no_window_issued_refused(self):
        with self.assertRaises(FrameError) as ctx:
            self.admission.accept(self.make(window_id=1))
        self.assertEqual(ctx.exception.reason, "no_window")

    def test_stream_epoch_rotation_refuses_the_old_generation(self):
        self.open_window()
        self.admission.accept(self.make(frame_seq=1))
        self.stream_epoch = self.windows.new_stream_epoch("reconnect")
        self.open_window()
        old = self.make(frame_seq=2, stream_epoch=self.stream_epoch - 1)
        with self.assertRaises(FrameError) as ctx:
            self.admission.accept(old)
        self.assertEqual(ctx.exception.reason, "stream_epoch_mismatch")

    def test_deadline_is_the_earliest_of_window_ttl_and_licence(self):
        self.open_window(duration_ms=100)
        licence_expiry = self.clock.now + 30 * 1_000_000      # 30 ms: the binding one
        receipt = self.admission.accept(self.make(), licence_expiry_ns=licence_expiry)
        self.assertEqual(receipt["deadline_ns"], licence_expiry)

    def test_window_is_the_binding_one_when_licence_is_long(self):
        self.open_window(duration_ms=40)
        far = self.clock.now + 10 * 60 * 1_000_000
        receipt = self.admission.accept(self.make(), licence_expiry_ns=far)
        self.assertEqual(receipt["deadline_ns"], self.windows.describe()["deadline_ns"])

    def test_queue_is_latest_wins_and_holds_only_the_newest(self):
        self.open_window(duration_ms=1000)
        for seq in (1, 2, 3):
            self.admission.accept(self.make(frame_seq=seq, arm=arm_command(seq=seq),
                                            gripper=gripper_command(seq=seq)))
        state = self.admission.state()
        self.assertEqual(state["queue_depth"], 1, "the bounded queue never keeps a backlog")
        self.assertEqual(self.admission.take()["frame"]["frame_seq"], 3)

    def test_queued_frame_expires_rather_than_replaying(self):
        self.open_window(duration_ms=60)
        self.admission.accept(self.make())
        self.clock.advance_ms(80)
        self.assertIsNone(self.admission.pending())
        self.assertEqual(self.admission.state()["counters"]["expired_in_queue"], 1)

    def test_clear_drops_the_pending_frame(self):
        self.open_window(duration_ms=200)
        self.admission.accept(self.make())
        self.admission.clear("estop")
        self.assertIsNone(self.admission.pending())
        self.assertEqual(self.admission.clear_reason, "estop")

    def test_a_reconnect_does_not_revive_a_buffered_frame(self):
        self.open_window(duration_ms=1000)
        self.admission.accept(self.make(frame_seq=3))
        new_epoch = self.windows.new_stream_epoch("reconnect")
        self.open_window()
        with self.assertRaises(FrameError) as ctx:
            self.admission.accept(self.make(frame_seq=4, stream_epoch=new_epoch))
        self.assertEqual(ctx.exception.reason, "stream_epoch_retired")
        self.assertIsNotNone(self.admission.pending(), "nothing is dropped before a rebind")

        # An explicit re-align to the new generation drops what was buffered and
        # lets sequences restart; the old packet can never be emitted again.
        self.admission.bind_stream_epoch(new_epoch)
        self.assertIsNone(self.admission.pending())
        self.assertEqual(self.admission.state()["last_frame_seq"], None)
        self.admission.accept(self.make(frame_seq=1, arm=arm_command(seq=1),
                                        gripper=gripper_command(seq=1),
                                        stream_epoch=new_epoch))
        self.assertEqual(self.admission.take()["frame"]["frame_seq"], 1)

    def test_a_new_generation_refuses_frames_from_the_retired_one(self):
        self.open_window(duration_ms=500)
        old_epoch = self.stream_epoch
        self.admission.accept(self.make(frame_seq=1))
        new_epoch = self.windows.new_stream_epoch("reconnect")
        self.open_window()
        self.admission.bind_stream_epoch(new_epoch)
        with self.assertRaises(FrameError) as ctx:
            self.admission.accept(self.make(frame_seq=2, stream_epoch=old_epoch))
        self.assertEqual(ctx.exception.reason, "stream_epoch_retired")


class ValidatorErrorKinds(unittest.TestCase):
    """Both sides refuse with their own exception types; neither may escape a frame."""

    def _admission(self, raise_with):
        clock = Clock()
        windows = FreshnessWindow(clock_ns=clock)
        epoch = windows.new_stream_epoch("connect")
        windows.issue(duration_ms=200)

        def validator(channel, command):
            if channel == "gripper":
                raise raise_with("nonfinite_r_joint_position")
            return {"accepted": True}

        admission = FrameAdmission(session_id=SESSION, session_epoch=EPOCH,
                                   calibration_bundle_hash=BUNDLE, windows=windows,
                                   channel_validator=validator, clock_ns=clock,
                                   stream_epoch=epoch)
        frame = {
            "frame_protocol_version": 1, "frame_seq": 1, "stream_epoch": epoch,
            "session_id": SESSION, "session_epoch": EPOCH,
            "calibration_bundle_hash": BUNDLE, "sample_seq": 1,
            "freshness_window_id": windows.current_id,
            "commands": {"arm": arm_command(), "gripper": gripper_command()},
        }
        return admission, frame

    def test_runtime_error_becomes_a_whole_frame_refusal(self):
        admission, frame = self._admission(RuntimeError)
        with self.assertRaises(FrameError) as ctx:
            admission.accept(frame)
        self.assertEqual(ctx.exception.reason, "channel_refused")
        self.assertIn("RuntimeError", ctx.exception.detail)
        state = admission.state()
        self.assertEqual(state["channel_seq"], {}, "no channel sequence was consumed")
        self.assertEqual(state["next_frame_seq"], 0)

    def test_key_error_becomes_a_whole_frame_refusal(self):
        admission, frame = self._admission(KeyError)
        with self.assertRaises(FrameError) as ctx:
            admission.accept(frame)
        self.assertEqual(ctx.exception.reason, "channel_refused")


class ReceiptFields(unittest.TestCase):
    def test_accept_carries_the_window_age_and_the_remaining_life(self):
        from lelab.teleop_frame_v1 import FrameAdmission, FreshnessWindow

        clock = Clock()
        windows = FreshnessWindow(clock_ns=clock)
        windows.new_stream_epoch("connect")
        windows.issue(duration_ms=200)
        clock.advance_ms(50)
        admission = FrameAdmission(
            session_id=SESSION, session_epoch=EPOCH, calibration_bundle_hash=BUNDLE,
            windows=windows, channel_validator=lambda channel, command: {"accepted": True},
            clock_ns=clock, stream_epoch=windows.stream_epoch)
        frame = {
            "frame_protocol_version": 1, "frame_seq": 1,
            "stream_epoch": windows.stream_epoch, "session_id": SESSION,
            "session_epoch": EPOCH, "calibration_bundle_hash": BUNDLE,
            "sample_seq": 1, "freshness_window_id": windows.current_id,
            "commands": {"arm": arm_command(), "gripper": gripper_command()},
        }
        receipt = admission.accept(frame)
        self.assertAlmostEqual(receipt["window_age_ms"], 50.0, places=1)
        self.assertAlmostEqual(receipt["window_remaining_ms"], 150.0, places=1)


class TransportRules(unittest.TestCase):
    def setUp(self):
        self.selector = TransportSelector()

    def test_frame_transport_disables_legacy_apply_for_that_epoch(self):
        self.assertTrue(self.selector.allows_legacy_apply(session_id=SESSION, session_epoch=EPOCH))
        self.selector.negotiate(session_id=SESSION, session_epoch=EPOCH, transport="frame_stream")
        self.assertFalse(self.selector.allows_legacy_apply(session_id=SESSION, session_epoch=EPOCH))

    def test_other_epochs_keep_their_own_choice(self):
        self.selector.negotiate(session_id=SESSION, session_epoch=EPOCH, transport="frame_http")
        self.assertTrue(self.selector.allows_legacy_apply(session_id=SESSION, session_epoch="e-2"))

    def test_switching_transport_inside_one_epoch_is_refused(self):
        self.selector.negotiate(session_id=SESSION, session_epoch=EPOCH, transport="frame_http")
        with self.assertRaises(FrameError) as ctx:
            self.selector.negotiate(session_id=SESSION, session_epoch=EPOCH, transport="frame_stream")
        self.assertEqual(ctx.exception.reason, "transport_switch_locked")

    def test_unsupported_transport_is_refused_not_degraded(self):
        with self.assertRaises(FrameError) as ctx:
            self.selector.negotiate(session_id=SESSION, session_epoch=EPOCH, transport="udp")
        self.assertEqual(ctx.exception.reason, "transport_unsupported")

    def test_legacy_transport_cannot_be_negotiated_as_framed(self):
        with self.assertRaises(FrameError) as ctx:
            self.selector.negotiate(session_id=SESSION, session_epoch=EPOCH, transport="legacy_apply")
        self.assertEqual(ctx.exception.reason, "transport_not_framed")

    def test_release_reopens_legacy(self):
        self.selector.negotiate(session_id=SESSION, session_epoch=EPOCH, transport="frame_stream")
        self.selector.release(session_id=SESSION, session_epoch=EPOCH, reason="epoch_end")
        self.assertTrue(self.selector.allows_legacy_apply(session_id=SESSION, session_epoch=EPOCH))


class BoardOrder(unittest.TestCase):
    def decoded(self):
        return {"arm": {"command": arm_command()}, "gripper": {"command": gripper_command()}}

    def test_six_axis_targets_are_ids_1_to_5_then_10(self):
        targets = six_axis_targets(self.decoded())
        self.assertEqual([t["servo_id"] for t in targets], [1, 2, 3, 4, 5, 10])
        self.assertEqual([t["joint_name"] for t in targets], ARM_JOINTS + ["r_joint"])

    def test_reordered_arm_is_refused(self):
        payload = self.decoded()
        payload["arm"]["command"]["target"]["joint_names"] = list(reversed(ARM_JOINTS))
        with self.assertRaises(FrameError) as ctx:
            six_axis_targets(payload)
        self.assertEqual(ctx.exception.reason, "joint_order")

    def test_targets_stay_radians(self):
        for entry in six_axis_targets(self.decoded()):
            self.assertIsInstance(entry["position_rad"], float)
            self.assertNotIn("position_raw", entry)


class SharedSourceOfTruth(unittest.TestCase):
    def test_frame_fields_are_exactly_the_documented_whitelist(self):
        self.assertEqual(set(FRAME_FIELDS), {
            "frame_protocol_version", "frame_seq", "stream_epoch", "session_id",
            "session_epoch", "calibration_bundle_hash", "sample_seq",
            "freshness_window_id", "commands"})

    def test_both_runtimes_load_the_same_contract_bytes(self):
        host = ROOT / "app_ros_mobile/lelab/teleop_frame_v1.py"
        car = ROOT / "ros2_ws/src/lelab_mobile_agent/lelab_mobile_agent/teleop_frame_v1.py"
        host_digest = hashlib.sha256(host.read_bytes()).hexdigest()
        car_digest = hashlib.sha256(Path(car).resolve().read_bytes()).hexdigest()
        self.assertEqual(host_digest, car_digest,
                         "the car-side contract must be the same file, not a copy")

    def test_car_package_imports_the_shared_module(self):
        from lelab_mobile_agent import teleop_frame_v1 as car_module
        from lelab import teleop_frame_v1 as host_module

        self.assertEqual(sorted(car_module.FRAME_FIELDS), sorted(host_module.FRAME_FIELDS))
        self.assertEqual(car_module.FRAME_PROTOCOL_VERSION, host_module.FRAME_PROTOCOL_VERSION)


if __name__ == "__main__":
    unittest.main()
