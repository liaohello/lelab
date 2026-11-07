"""Offline tests for the measured topic and the ROS servo output adapter."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ros2_ws" / "src" / "lelab_mobile_agent"))

from lelab_mobile_agent.follower_feedback import RADIANS_PER_ENCODER_TICK
from lelab_mobile_agent.ros_servo_output import RosServoOutputDriver, rad_to_raw
from lelab_mobile_agent.ros_servo_readback import RosServoFeedbackTopicDriver


class Position:
    def __init__(self):
        self.id = 0
        self.position = 0


class Positions:
    def __init__(self):
        self.duration = 0.0
        self.position = []


class Publisher:
    def __init__(self):
        self.messages = []
        self.publish_count = 0

    def publish(self, message):
        self.messages.append(message)
        self.publish_count += 1


class MatchedPublisher(Publisher):
    def __init__(self, subscriptions=0):
        super().__init__()
        self.subscriptions = subscriptions

    def get_subscription_count(self):
        return self.subscriptions


class ServoState:
    def __init__(self):
        self.present_id = []
        self.enable_torque = []


class SetServoState:
    def __init__(self):
        self.duration = 0.0
        self.state = []


class StatePublisher(MatchedPublisher):
    def __init__(self):
        super().__init__(subscriptions=1)


class Timer:
    pass


class Node:
    def __init__(self):
        self.callback = None

    def create_timer(self, _period, _callback):
        return Timer()

    def create_subscription(self, _kind, _topic, callback, _depth):
        self.callback = callback
        return object()


class Readback:
    def __init__(self, raw):
        self.raw = dict(raw)
        self.last_success_mono = time.monotonic()

    def read_snapshot(self):
        return {
            "available": True,
            "last_error": None,
            "last_success_mono": time.monotonic(),
            "joint_samples": {
                name: {"raw_position": value} for name, value in self.raw.items()
            },
        }


class FakeTorqueReadback:
    """Retires each per-ID read on the next poll; fail_ids stay unloaded."""

    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)
        self.started_ids = []
        self._id = None
        self.in_flight = False

    def start(self, servo_id):
        self._id = servo_id
        self.in_flight = True
        self.started_ids.append(servo_id)

    def poll(self):
        if not self.in_flight:
            return None
        servo_id, self._id = self._id, None
        self.in_flight = False
        loaded = servo_id not in self.fail_ids
        return servo_id, {"verified": loaded, "servo_id": servo_id,
                          "enable_torque": 1 if loaded else 0, "position": 500,
                          "reason": None if loaded else "torque_not_loaded"}

    def cancel(self):
        self.in_flight = False
        self._id = None


def enable_homing(driver, attempts=300):
    for _ in range(attempts):
        try:
            driver.enable()
            return
        except RuntimeError as exc:
            if "pending" in str(exc):
                continue
            raise
    raise AssertionError("homing enable did not complete within the step budget")


class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now


RAW = {"joint1": 500, "joint2": 510, "joint3": 490,
       "joint4": 520, "joint5": 480, "r_joint": 492}
LIMITS = {name: (-2.09, 2.09) for name in RAW}


def test_raw_conversion_and_gripper_direction():
    assert rad_to_raw("joint1", 0.0) == 500
    assert rad_to_raw("joint1", RADIANS_PER_ENCODER_TICK) == 501
    assert rad_to_raw("r_joint", RADIANS_PER_ENCODER_TICK) == 499
    assert rad_to_raw("r_joint", -RADIANS_PER_ENCODER_TICK) == 501


def test_watchdog_arms_at_the_first_frame_not_at_enable():
    driver = RosServoOutputDriver(
        Node(), Publisher(), servos_position_type=Positions, servo_position_type=Position,
        readback=Readback(RAW), limits_rad=LIMITS, max_step_rad_s=0.5,
        max_silence_s=0.6, clock=Clock())
    driver.enable()
    driver.clock.now += 2.0
    driver._watchdog()
    assert driver.outputs_enabled is True, "the enable handshake is not a silent stream"
    driver.submit({
        "channel": "arm",
        "target": {"joint_names": [f"joint{i}" for i in range(1, 6)],
                   "positions_rad": [0.0, 0.02, -0.02, 0.03, -0.03]},
    })
    driver.clock.now += 2.0
    driver._watchdog()
    assert driver.outputs_enabled is False


def test_output_driver_publishes_integer_uint16_and_hold_frame():
    node, publisher, clock = Node(), Publisher(), Clock()
    readback = Readback(RAW)
    driver = RosServoOutputDriver(
        node, publisher,
        servos_position_type=Positions,
        servo_position_type=Position,
        readback=readback,
        limits_rad=LIMITS,
        max_step_rad_s=0.5,
        max_silence_s=0.6,
        duration_s=0.15,
        clock=clock,
    )
    driver.enable()
    arm_rad = [0.0, 0.02, -0.02, 0.03, -0.03]
    result = driver.submit({
        "channel": "arm",
        "target": {
            "joint_names": [f"joint{i}" for i in range(1, 6)],
            "positions_rad": arm_rad,
        },
    })
    assert result["driver_sent"] is True
    message = publisher.messages[-1]
    assert [item.id for item in message.position] == [1, 2, 3, 4, 5]
    assert all(type(item.position) is int for item in message.position)

    clock.now += 2.0
    driver._watchdog()
    assert driver.capabilities()["outputs_enabled"] is False
    hold = publisher.messages[-1]
    assert [item.id for item in hold.position] == [1, 2, 3, 4, 5, 10]
    assert [item.position for item in hold.position] == [
        RAW[f"joint{i}"] for i in range(1, 6)
    ] + [RAW["r_joint"]]


def test_position_pid_corrects_the_emitted_position_from_measured_feedback():
    publisher = Publisher()
    clock = Clock()
    driver = RosServoOutputDriver(
        Node(), publisher,
        servos_position_type=Positions,
        servo_position_type=Position,
        readback=Readback(RAW),
        limits_rad=LIMITS,
        max_step_rad_s=0.5,
        max_silence_s=0.6,
        clock=clock,
        position_pid={
            "enabled": True, "kp": 0.3, "ki": 0.0, "kd": 0.0,
            "deadband_rad": 0.004, "integral_limit_rad": 0.03,
            "max_correction_rad": 0.04,
            "derivative_filter_alpha": 1.0,
        },
    )
    driver.enable()
    result = driver.submit_frame([
        ("joint1", 0.02), ("joint2", 0.06), ("joint3", -0.02),
        ("joint4", 0.10), ("joint5", -0.06), ("r_joint", 0.05),
    ])
    assert result["driver_sent"] is True
    assert set(result["position_pid"]) == {
        "joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"}
    assert result["position_pid"]["joint1"]["error_rad"] == pytest.approx(0.02)
    assert result["position_pid"]["joint1"]["correction_rad"] > 0
    assert driver.last_pid["joint1"]["command_rad"] > 0.02


def test_output_driver_rejects_unsafe_step_and_wrong_channel():
    driver = RosServoOutputDriver(
        Node(), Publisher(),
        servos_position_type=Positions,
        servo_position_type=Position,
        readback=Readback(RAW),
        limits_rad=LIMITS,
        max_step_rad_s=0.5,
        max_silence_s=0.6,
        duration_s=0.15,
        clock=Clock(),
    )
    driver.enable()
    driver.submit({
        "channel": "arm",
        "target": {
            "joint_names": [f"joint{i}" for i in range(1, 6)],
            "positions_rad": [0.0] * 5,
        },
    })
    with pytest.raises(RuntimeError, match="target_step_exceeded"):
        driver.submit({
            "channel": "arm",
            "target": {
                "joint_names": [f"joint{i}" for i in range(1, 6)],
                "positions_rad": [1.0] * 5,
            },
        })
    with pytest.raises(RuntimeError, match="arm_output_only"):
        driver.submit({"channel": "base", "target": {}})


def test_output_driver_does_not_count_a_publish_without_an_actuator_subscriber():
    publisher = MatchedPublisher(subscriptions=0)
    driver = RosServoOutputDriver(
        Node(), publisher, servos_position_type=Positions, servo_position_type=Position,
        readback=Readback(RAW), limits_rad=LIMITS, max_step_rad_s=0.15, clock=Clock())
    with pytest.raises(RuntimeError, match="no_actuator_subscriber"):
        driver.enable()
    assert driver.publish_count == 0
    publisher.subscriptions = 1
    driver.enable()
    result = driver.submit({
        "channel": "gripper",
        "target": {"joint_name": "r_joint",
                   "position_rad": (500 - RAW["r_joint"]) * RADIANS_PER_ENCODER_TICK},
    })
    assert result["driver_sent"] is True
    assert driver.last_subscription_count == 1


def test_trial_first_frame_locked_axes_and_displacement_are_enforced():
    publisher = Publisher()
    readback = Readback(RAW)
    driver = RosServoOutputDriver(
        Node(), publisher, servos_position_type=Positions, servo_position_type=Position,
        readback=readback, limits_rad=LIMITS, clock=Clock(),
        trial_motion={"allowed_joints": ["joint1"], "first_frame_tolerance_rad": 0.02,
                      "locked_tolerance_rad": 0.02, "max_displacement_rad": 0.08},
    )
    driver.enable()
    origin = [(name, (500 - raw if name == "r_joint" else raw - 500)
               * RADIANS_PER_ENCODER_TICK) for name, raw in RAW.items()]
    distant = [(n, v + 0.04 if n == "joint1" else v) for n, v in origin]
    with pytest.raises(RuntimeError, match="trial_first_frame_discontinuous:joint1"):
        driver.submit_frame(distant)
    assert not publisher.messages
    driver.submit_frame(origin)
    assert [item.id for item in publisher.messages[-1].position] == [1]
    wrong_axis = [(n, v + 0.03 if n == "joint2" else v) for n, v in origin]
    with pytest.raises(RuntimeError, match="trial_joint_locked:joint2"):
        driver.submit_frame(wrong_axis)
    driver.max_step_rad_s = 10
    driver.clock.now += 1.0
    outside = [(n, v + 0.09 if n == "joint1" else v) for n, v in origin]
    with pytest.raises(RuntimeError, match="trial_displacement_exceeded:joint1"):
        driver.submit_frame(outside)
    assert len(publisher.messages) == 1
    driver.stop("test")
    assert [item.id for item in publisher.messages[-1].position] == [1]
    # A later engagement must take a new measured reference, not replay the old one.
    readback.raw["joint1"] += 10
    driver.enable()
    count = len(publisher.messages)
    with pytest.raises(RuntimeError, match="trial_first_frame_discontinuous:joint1"):
        driver.submit_frame(origin)
    assert len(publisher.messages) == count


@pytest.mark.parametrize("axis", [2, 3, 4, 5])
def test_single_joint_trial_only_publishes_selected_servo_and_checks_limits(axis):
    publisher = Publisher()
    readback = Readback(RAW)
    clock = Clock()
    driver = RosServoOutputDriver(
        Node(), publisher, servos_position_type=Positions, servo_position_type=Position,
        readback=readback, limits_rad=LIMITS, max_step_rad_s=0.15, clock=clock,
        trial_motion={"allowed_joints": [f"joint{axis}"], "first_frame_tolerance_rad": 0.02,
                      "locked_tolerance_rad": 0.02, "max_displacement_rad": 0.08})
    driver.enable()
    origin = [(name, (500 - raw if name == "r_joint" else raw - 500)
               * RADIANS_PER_ENCODER_TICK) for name, raw in RAW.items()]
    driver.submit_frame(origin)
    clock.now += 0.2
    selected = f"joint{axis}"
    moved = [(name, value + (0.02 if name == selected else 0.0)) for name, value in origin]
    driver.submit_frame(moved)
    assert all([item.id for item in msg.position] == [axis] for msg in publisher.messages)
    with pytest.raises(RuntimeError, match="trial_joint_locked:joint1"):
        driver.submit_frame([(n, v + 0.03 if n == "joint1" else v) for n, v in origin])
    clock.now += 1
    with pytest.raises(RuntimeError, match=f"trial_displacement_exceeded:{selected}"):
        driver.submit_frame([(n, v + 0.09 if n == selected else v) for n, v in origin])
    driver.stop("test")
    assert [item.id for item in publisher.messages[-1].position] == [axis]


def test_trial_first_frame_refusal_is_single_shot_and_reseeds_rate_limiter():
    publisher = Publisher()
    readback = Readback(RAW)
    clock = Clock()
    driver = RosServoOutputDriver(
        Node(), publisher, servos_position_type=Positions, servo_position_type=Position,
        readback=readback, limits_rad=LIMITS, max_step_rad_s=0.15, clock=clock,
        trial_motion={"allowed_joints": ["joint1"], "first_frame_tolerance_rad": 0.02,
                      "locked_tolerance_rad": 0.02, "max_displacement_rad": 0.08},
    )
    driver.enable()
    origin = [(name, (500 - raw if name == "r_joint" else raw - 500)
               * RADIANS_PER_ENCODER_TICK) for name, raw in RAW.items()]
    target = [(name, value + (0.04 if name == "joint1" else 0.0))
              for name, value in origin]

    with pytest.raises(RuntimeError, match="trial_first_frame_discontinuous:joint1"):
        driver.submit_frame(target)
    assert not publisher.messages
    assert driver.trial_first_frame_pending is False
    assert driver.last_target_rad["joint1"] == pytest.approx(origin[0][1])

    clock.now += 0.2
    driver.submit_frame(target)
    assert publisher.publish_count == 1
    assert [item.id for item in publisher.messages[-1].position] == [1]
    assert driver.trial_first_frame_pending is False


def test_trial_cannot_start_without_complete_calibrated_measured_feedback():
    with pytest.raises(ValueError, match="all six joints"):
        RosServoOutputDriver(Node(), Publisher(), servos_position_type=Positions,
            servo_position_type=Position, readback=Readback(RAW), limits_rad={"joint1": (-1, 1)},
            trial_motion={"allowed_joints": ["joint1"], "first_frame_tolerance_rad": 0.02,
                          "locked_tolerance_rad": 0.02, "max_displacement_rad": 0.08})
    raw = dict(RAW)
    del raw["joint5"]
    driver = RosServoOutputDriver(Node(), Publisher(), servos_position_type=Positions,
        servo_position_type=Position, readback=Readback(raw), limits_rad=LIMITS,
        trial_motion={"allowed_joints": ["joint1"], "first_frame_tolerance_rad": 0.02,
                      "locked_tolerance_rad": 0.02, "max_displacement_rad": 0.08})
    with pytest.raises(RuntimeError, match="trial_feedback_invalid:joint5"):
        driver.enable()


def test_homing_mode_emits_only_the_one_axis_that_moves_toward_its_raw_target():
    raw = {"joint1": 510, "joint2": 505, "joint3": 495,
           "joint4": 498, "joint5": 503, "r_joint": 360}
    publisher = Publisher()
    state_publisher = StatePublisher()
    clock = Clock()
    driver = RosServoOutputDriver(
        Node(), publisher, servos_position_type=Positions, servo_position_type=Position,
        readback=Readback(raw), limits_rad=LIMITS, max_step_rad_s=0.15, clock=clock,
        servo_state_publisher=state_publisher,
        bus_servo_state_type=ServoState,
        set_bus_servo_state_type=SetServoState,
        torque_readback=FakeTorqueReadback(),
        homing_motion={
            "axis_order": ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"],
            "targets_raw": {"joint1": 500, "joint2": 500, "joint3": 500,
                            "joint4": 500, "joint5": 500, "r_joint": 350},
            "tolerance_raw": 2, "locked_tolerance_raw": 2,
            "first_frame_tolerance_raw": 20, "max_step_raw": 20,
            "enable_torque": True,
        })
    enable_homing(driver)
    assert driver.homing_torque_enabled is True
    assert driver.homing_torque_state == "verified"
    assert driver.torque_servo_ids == [1, 2, 3, 4, 5, 10]
    assert all(row["state"] == "verified" for row in driver.homing_torque_results.values())
    assert [message.state[0].present_id for message in state_publisher.messages[:6]] == [
        [1, 1], [1, 2], [1, 3], [1, 4], [1, 5], [1, 10]]

    def rads(values):
        return [(name, (value - 500) * RADIANS_PER_ENCODER_TICK
                 * (-1 if name == "r_joint" else 1)) for name, value in values.items()]

    targets = dict(raw)
    targets["joint1"] = 503
    result = driver.submit_frame(rads(targets))
    assert result["single_axis_homing"] is True
    assert result["homing_active_joint"] == "joint1"
    assert result["homing_target_raw"] == 503
    assert [item.id for item in publisher.messages[-1].position] == [1]

    driver.stop("test_home_stop")
    assert [item.id for item in publisher.messages[-1].position] == [1]
    assert publisher.messages[-1].position[0].position == raw["joint1"]


def test_homing_mode_refuses_multiple_axes_bad_direction_and_oversized_step():
    raw = {"joint1": 520, "joint2": 520, "joint3": 480,
           "joint4": 480, "joint5": 520, "r_joint": 370}
    publisher = Publisher()
    driver = RosServoOutputDriver(
        Node(), publisher, servos_position_type=Positions, servo_position_type=Position,
        readback=Readback(raw), limits_rad=LIMITS, max_step_rad_s=0.15, clock=Clock(),
        servo_state_publisher=StatePublisher(),
        bus_servo_state_type=ServoState,
        set_bus_servo_state_type=SetServoState,
        torque_readback=FakeTorqueReadback(),
        homing_motion={
            "axis_order": ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"],
            "targets_raw": {"joint1": 500, "joint2": 500, "joint3": 500,
                            "joint4": 500, "joint5": 500, "r_joint": 350},
            "tolerance_raw": 2, "locked_tolerance_raw": 2,
            "first_frame_tolerance_raw": 20, "max_step_raw": 20,
            "enable_torque": True,
        })
    enable_homing(driver)

    def rads(values):
        return [(name, (value - 500) * RADIANS_PER_ENCODER_TICK
                 * (-1 if name == "r_joint" else 1)) for name, value in values.items()]

    two = dict(raw)
    two.update(joint1=517, joint2=517)
    with pytest.raises(RuntimeError, match="homing_multiple_axes:joint1,joint2"):
        driver.submit_frame(rads(two))

    wrong = dict(raw)
    wrong["joint1"] = 523
    with pytest.raises(RuntimeError, match="homing_direction_or_overshoot:joint1"):
        driver.submit_frame(rads(wrong))

    too_far = dict(raw)
    too_far["joint1"] = 490
    with pytest.raises(RuntimeError, match="homing_step_exceeded:joint1"):
        driver.submit_frame(rads(too_far))
    assert not publisher.messages


def test_homing_mode_moves_gripper_as_id10_toward_raw_350():
    raw = {"joint1": 500, "joint2": 500, "joint3": 500,
           "joint4": 500, "joint5": 500, "r_joint": 360}
    publisher = Publisher()
    driver = RosServoOutputDriver(
        Node(), publisher, servos_position_type=Positions, servo_position_type=Position,
        readback=Readback(raw), limits_rad=LIMITS, max_step_rad_s=0.15, clock=Clock(),
        servo_state_publisher=StatePublisher(),
        bus_servo_state_type=ServoState,
        set_bus_servo_state_type=SetServoState,
        torque_readback=FakeTorqueReadback(),
        homing_motion={
            "axis_order": ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"],
            "targets_raw": {"joint1": 500, "joint2": 500, "joint3": 500,
                            "joint4": 500, "joint5": 500, "r_joint": 350},
            "tolerance_raw": 2, "locked_tolerance_raw": 2,
            "first_frame_tolerance_raw": 20, "max_step_raw": 20,
            "enable_torque": True,
        })
    enable_homing(driver)
    target = dict(raw)
    target["r_joint"] = 353
    rads = [(name, (value - 500) * RADIANS_PER_ENCODER_TICK
             * (-1 if name == "r_joint" else 1)) for name, value in target.items()]
    result = driver.submit_frame(rads)
    assert result["homing_active_joint"] == "r_joint"
    assert result["homing_target_raw"] == 353
    assert [item.id for item in publisher.messages[-1].position] == [10]
    assert publisher.messages[-1].position[0].position == 353


class FrameMessage:
    def __init__(self, values):
        self.data = list(values)


def _v2(owner, seq, raws=(533, 815, -49, 118, 880, 492), span_us=18_000):
    return [2, owner, seq, span_us,
            1, raws[0], 2, raws[1], 3, raws[2], 4, raws[3], 5, raws[4], 10, raws[5]]


def test_measured_topic_driver_tracks_signed_raw_and_rejects_replay():
    node = Node()
    driver = RosServoFeedbackTopicDriver(
        node, FrameMessage, calibration_hash="bundle-1")
    driver.set_calibration_hash("bundle-1")
    values = _v2(777, 7)
    node.callback(FrameMessage(values))
    snapshot = driver.read_snapshot()
    assert snapshot["available"] is True
    assert snapshot["joint_samples"]["joint3"]["raw_position"] == -49
    assert snapshot["scan_span_s"] == 0.018
    assert snapshot["servo_pulses"]["joint3"] == -49
    assert snapshot["publisher_instance"] == 777
    assert snapshot["publisher_generation"] == 0
    assert snapshot["publisher_identity_kind"] == "software_owner_instance"

    node.callback(FrameMessage(values))
    assert driver.read_snapshot()["available"] is False
    assert driver.read_snapshot()["last_error"] == "readback_sequence_invalid"


def test_measured_topic_driver_revokes_on_owner_change_without_sequence_catchup():
    node = Node()
    driver = RosServoFeedbackTopicDriver(
        node, FrameMessage, calibration_hash="bundle-1")
    changes = []
    driver.on_owner_changed = lambda old, new, gen: changes.append((old, new, gen))
    node.callback(FrameMessage(_v2(777, 40)))
    assert driver.read_snapshot()["available"] is True
    # A new owner instance resets its sequence to 0; the consumer must accept it at
    # once (no waiting for the sequence to climb past 40) and retire the old cache.
    node.callback(FrameMessage(_v2(888, 0, raws=(527, 632, 76, 118, 907, 457))))
    snapshot = driver.read_snapshot()
    assert snapshot["available"] is True
    assert snapshot["scan_sequence"] == 0
    assert snapshot["servo_pulses"]["joint1"] == 527
    assert snapshot["publisher_instance"] == 888
    assert snapshot["publisher_generation"] == 1
    assert changes == [(777, 888, 1)]
    # A late frame from the retired owner is refused, not merged into the new space.
    node.callback(FrameMessage(_v2(777, 41)))
    assert driver.read_snapshot()["available"] is False
    assert driver.read_snapshot()["last_error"] == "readback_retired_owner"


def test_measured_topic_driver_rejects_legacy_v1_and_unknown_format():
    node = Node()
    driver = RosServoFeedbackTopicDriver(
        node, FrameMessage, calibration_hash="bundle-1")
    legacy = [7, 18_000, 1, 533, 2, 815, 3, -49, 4, 118, 5, 880, 10, 492]
    node.callback(FrameMessage(legacy))
    assert driver.read_snapshot()["available"] is False
    assert driver.read_snapshot()["last_error"] == "readback_format_unsupported:v1"
    node.callback(FrameMessage([3, 777, 0, 18_000] + legacy[2:]))
    assert driver.read_snapshot()["last_error"] == "readback_payload_invalid"


def test_measured_topic_driver_rejects_same_owner_low_sequence_without_stale_gap():
    node = Node()
    driver = RosServoFeedbackTopicDriver(
        node, FrameMessage, calibration_hash="bundle-1")
    node.callback(FrameMessage(_v2(777, 40)))
    # Same owner, lower sequence, even after a stale gap, is a replay not a restart.
    driver.last_success_mono = time.monotonic() - 10.0
    node.callback(FrameMessage(_v2(777, 1)))
    assert driver.read_snapshot()["available"] is False
    assert driver.read_snapshot()["last_error"] == "readback_sequence_invalid"
    assert driver.publisher_generation == 0


def _homing_driver(publisher, state_publisher, readback, torque_readback, clock=None):
    return RosServoOutputDriver(
        Node(), publisher, servos_position_type=Positions, servo_position_type=Position,
        readback=readback, limits_rad=LIMITS, max_step_rad_s=0.15, clock=clock or Clock(),
        servo_state_publisher=state_publisher,
        bus_servo_state_type=ServoState,
        set_bus_servo_state_type=SetServoState,
        torque_readback=torque_readback,
        homing_motion={
            "axis_order": ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"],
            "targets_raw": {"joint1": 500, "joint2": 500, "joint3": 500,
                            "joint4": 500, "joint5": 500, "r_joint": 350},
            "tolerance_raw": 2, "locked_tolerance_raw": 2,
            "first_frame_tolerance_raw": 20, "max_step_raw": 20,
            "enable_torque": True,
        })


def test_homing_torque_partial_failure_blocks_output_and_never_unloads():
    publisher = Publisher()
    state_publisher = StatePublisher()
    torque = FakeTorqueReadback(fail_ids={2})
    driver = _homing_driver(publisher, state_publisher, Readback(RAW), torque)
    with pytest.raises(RuntimeError, match="homing_torque_unverified:2:torque_not_loaded"):
        enable_homing(driver)
    assert driver.outputs_enabled is False
    assert driver.homing_torque_enabled is False
    assert driver.homing_torque_state == "failed"
    assert driver.homing_torque_results[2]["state"] == "failed"
    assert driver.homing_torque_results[1]["state"] == "verified"
    assert not publisher.messages  # no position output admitted
    # Every published state write is a load ([1,1]); no axis is ever unloaded.
    assert all(list(msg.state[0].enable_torque) == [1, 1] for msg in state_publisher.messages)
    # A latched failure does not re-publish torque on subsequent enable attempts.
    writes = len(state_publisher.messages)
    with pytest.raises(RuntimeError, match="homing_torque_unverified"):
        driver.enable()
    assert len(state_publisher.messages) == writes


def test_homing_torque_requires_a_readback_adapter():
    driver = RosServoOutputDriver(
        Node(), Publisher(), servos_position_type=Positions, servo_position_type=Position,
        readback=Readback(RAW), limits_rad=LIMITS, clock=Clock(),
        servo_state_publisher=StatePublisher(),
        bus_servo_state_type=ServoState, set_bus_servo_state_type=SetServoState,
        homing_motion={
            "axis_order": ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"],
            "targets_raw": {"joint1": 500, "joint2": 500, "joint3": 500,
                            "joint4": 500, "joint5": 500, "r_joint": 350},
            "tolerance_raw": 2, "locked_tolerance_raw": 2,
            "first_frame_tolerance_raw": 20, "max_step_raw": 20,
            "enable_torque": True,
        })
    with pytest.raises(RuntimeError, match="homing_torque_readback_unavailable"):
        driver.enable()
    assert driver.outputs_enabled is False


def test_homing_torque_verification_is_invalidated_on_owner_change():
    publisher = Publisher()
    state_publisher = StatePublisher()
    driver = _homing_driver(publisher, state_publisher, Readback(RAW), FakeTorqueReadback())
    enable_homing(driver)
    assert driver.homing_torque_state == "verified"
    driver.invalidate_torque_verification()
    assert driver.homing_torque_state == "idle"
    assert driver.homing_torque_enabled is False
    assert driver.homing_torque_results == {}
    # A fresh enable loads and verifies again rather than trusting the old proof.
    enable_homing(driver)
    assert driver.homing_torque_state == "verified"
    assert driver.torque_publish_count == 12
