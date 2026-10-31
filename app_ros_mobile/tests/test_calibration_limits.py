"""Tests for the calibrated-range joint guard in the LeRobot fork.

The module is loaded by file path: `lerobot.robots.__init__` pulls in the whole
robot stack, while this helper is pure stdlib.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_FORK_MODULE = (
    Path(__file__).resolve().parents[2]
    / "lerobot"
    / "src"
    / "lerobot"
    / "robots"
    / "calibration_limits.py"
)


def _load_module():
    if not _FORK_MODULE.exists():  # pragma: no cover - layout guard
        pytest.skip(f"LeRobot fork not found at {_FORK_MODULE}")
    spec = importlib.util.spec_from_file_location("lelab_test_calibration_limits", _FORK_MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ensure_within_calibrated_range = _load_module().ensure_within_calibrated_range

_LIMITS = {
    "elbow_flex": (-97.7, 97.7),
    "shoulder_lift": (-102.5, 102.5),
    "gripper": (0.0, 100.0),
}


def test_inside_limits_is_untouched() -> None:
    goal = {"elbow_flex": 40.0, "shoulder_lift": -50.0, "gripper": 12.0}

    assert ensure_within_calibrated_range(goal, _LIMITS, 0.0) == goal


def test_outside_limits_is_clamped() -> None:
    goal = {"elbow_flex": 120.0, "shoulder_lift": -140.0, "gripper": -3.0}

    clamped = ensure_within_calibrated_range(goal, _LIMITS, 0.0)

    assert clamped == {"elbow_flex": 97.7, "shoulder_lift": -102.5, "gripper": 0.0}


def test_positive_margin_shrinks_and_negative_margin_expands() -> None:
    goal = {"shoulder_lift": -104.0}

    assert ensure_within_calibrated_range(goal, _LIMITS, 3.0) == {"shoulder_lift": -99.5}
    # Negative margin covers a calibration measured slightly narrower than the
    # real motion range (red-bowl demos overshoot by up to 3.2 units).
    assert ensure_within_calibrated_range(goal, _LIMITS, -4.0) == {"shoulder_lift": -104.0}


def test_motor_map_margin_is_per_joint() -> None:
    goal = {"shoulder_lift": -104.0, "gripper": 1.0}

    clamped = ensure_within_calibrated_range(
        goal, _LIMITS, {"shoulder_lift": -4.0, "gripper": 0.0}
    )

    assert clamped == {"shoulder_lift": -104.0, "gripper": 1.0}


def test_margin_wider_than_range_pins_the_midpoint() -> None:
    limits = {"elbow_flex": (0.0, 4.0)}

    assert ensure_within_calibrated_range({"elbow_flex": 10.0}, limits, 5.0) == {
        "elbow_flex": 2.0
    }
