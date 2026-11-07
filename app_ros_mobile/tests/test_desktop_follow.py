"""The relative-increment mapper behind the desktop clutch: what it may assume, and what it must refuse.

The desktop loop used to send the leader pose directly, so starting a session could sweep the
follower across the whole workspace. These tests pin the alternative: a target is the follower's
own measured reference plus how far the leader has moved since, and no reference is invented when
a joint cannot be measured.
"""
from __future__ import annotations

import pytest

from lelab.desktop_follow import WRAPPED_JOINTS, capture, follow_target, wrap_delta

KEYS = [f"{name}.pos" for name in
        ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")]


def _reading(value: float) -> dict[str, float]:
    return {key: value for key in KEYS}


class TestCapture:
    def test_a_measured_pair_becomes_the_reference(self):
        alignment, missing = capture(_reading(30.0), _reading(4.0))
        assert missing == []
        assert alignment["leader"]["shoulder_pan.pos"] == 30.0
        assert alignment["follower"]["shoulder_pan.pos"] == 4.0

    def test_a_joint_the_follower_cannot_report_refuses_the_whole_alignment(self):
        """Half a reference would send the arm to a pose that mixes a measured joint with a guess."""
        follower = _reading(4.0)
        del follower["elbow_flex.pos"]
        alignment, missing = capture(_reading(30.0), follower)
        assert alignment is None and missing == ["elbow_flex.pos"]

    def test_a_null_reading_counts_as_missing_rather_than_zero(self):
        follower = _reading(4.0)
        follower["gripper.pos"] = None
        alignment, missing = capture(_reading(30.0), follower)
        assert alignment is None and missing == ["gripper.pos"]


class TestFollowTarget:
    def test_a_still_leader_holds_the_follower_where_it_is(self):
        alignment, _ = capture(_reading(30.0), _reading(4.0))
        assert follow_target(_reading(30.0), alignment) == _reading(4.0)

    def test_the_target_moves_by_the_leaders_change_not_to_the_leaders_pose(self):
        alignment, _ = capture(_reading(30.0), _reading(4.0))
        moved = _reading(35.0)
        moved["gripper.pos"] = 12.0
        target = follow_target(moved, alignment)
        assert target["shoulder_pan.pos"] == pytest.approx(9.0)
        assert target["gripper.pos"] == pytest.approx(12.0 - 30.0 + 4.0)

    def test_a_continuous_joint_is_wrapped_so_it_never_commands_a_full_sweep(self):
        leader_ref = _reading(179.0)
        alignment, _ = capture(leader_ref, _reading(0.0))
        target = follow_target({**_reading(0.0), "wrist_roll.pos": 0.0}, alignment)
        # 179 -> 0 is -179 degrees of intent, not +181 of raw subtraction.
        assert target["wrist_roll.pos"] == pytest.approx(-179.0)
        assert "wrist_roll" in WRAPPED_JOINTS

    def test_a_non_wrapped_joint_is_left_alone(self):
        assert wrap_delta(200.0) == pytest.approx(-160.0)
        assert follow_target({"shoulder_lift.pos": 200.0},
                             {"leader": {"shoulder_lift.pos": 0.0},
                              "follower": {"shoulder_lift.pos": 0.0}})["shoulder_lift.pos"] == 200.0

    def test_a_joint_without_a_reference_is_dropped_not_invented(self):
        alignment = {"leader": {"shoulder_pan.pos": 0.0}, "follower": {"shoulder_pan.pos": 1.0}}
        target = follow_target({"shoulder_pan.pos": 5.0, "gripper.pos": 9.0}, alignment)
        assert target == {"shoulder_pan.pos": 6.0}
