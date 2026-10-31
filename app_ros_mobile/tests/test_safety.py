"""Pure tests for local SO-101 safety configuration (no hardware access)."""

from __future__ import annotations

import pytest

from lelab.safety import (
    CALIBRATION_RANGE_MARGIN_ENV,
    DEFAULT_CALIBRATION_RANGE_MARGIN,
    DEFAULT_MAX_POSITION_DEVIATION,
    DEFAULT_MAX_RELATIVE_TARGET,
    MAX_POSITION_DEVIATION_ENV,
    MAX_RELATIVE_TARGET_ENV,
    follower_safety_cli_args,
    follower_safety_kwargs,
)


@pytest.fixture(autouse=True)
def _clear_safety_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MAX_RELATIVE_TARGET_ENV, raising=False)
    monkeypatch.delenv(MAX_POSITION_DEVIATION_ENV, raising=False)
    monkeypatch.delenv(CALIBRATION_RANGE_MARGIN_ENV, raising=False)


def test_conservative_defaults_are_enabled() -> None:
    assert follower_safety_kwargs() == {
        "max_relative_target": DEFAULT_MAX_RELATIVE_TARGET,
        "max_position_deviation": DEFAULT_MAX_POSITION_DEVIATION,
        "calibration_range_margin": DEFAULT_CALIBRATION_RANGE_MARGIN,
    }


def test_environment_can_override_or_disable_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MAX_RELATIVE_TARGET_ENV, "1.5")
    monkeypatch.setenv(MAX_POSITION_DEVIATION_ENV, "off")

    assert follower_safety_kwargs() == {
        "max_relative_target": 1.5,
        "max_position_deviation": None,
        "calibration_range_margin": DEFAULT_CALIBRATION_RANGE_MARGIN,
    }
    assert follower_safety_cli_args() == [
        "--robot.max_relative_target=1.5",
        f"--robot.calibration_range_margin={DEFAULT_CALIBRATION_RANGE_MARGIN}",
    ]


def test_calibration_margin_accepts_negative_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CALIBRATION_RANGE_MARGIN_ENV, "-4")

    assert follower_safety_kwargs()["calibration_range_margin"] == -4.0
    assert follower_safety_cli_args()[-1] == "--robot.calibration_range_margin=-4.0"


def test_calibration_margin_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CALIBRATION_RANGE_MARGIN_ENV, "off")

    assert follower_safety_kwargs()["calibration_range_margin"] is None
    assert all("calibration_range_margin" not in arg for arg in follower_safety_cli_args())


def test_calibration_margin_motor_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CALIBRATION_RANGE_MARGIN_ENV, '{"shoulder_lift": -4, "gripper": 0}')

    limits = follower_safety_kwargs()

    assert limits["calibration_range_margin"] == {"shoulder_lift": -4.0, "gripper": 0.0}
    assert follower_safety_cli_args()[-1] == (
        "--robot.calibration_range_margin={shoulder_lift: -4.0, gripper: 0.0}"
    )


def test_json_motor_map_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MAX_RELATIVE_TARGET_ENV, '{"shoulder_pan": 2, "gripper": 5}')

    limits = follower_safety_kwargs()
    assert limits["max_relative_target"] == {"shoulder_pan": 2.0, "gripper": 5.0}
    assert follower_safety_cli_args()[0] == (
        "--robot.max_relative_target={shoulder_pan: 2.0, gripper: 5.0}"
    )


@pytest.mark.parametrize("value", ["", "0", "-1", "garbage", "{}", '{"gripper": 0}'])
def test_invalid_environment_value_fails_closed(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(MAX_RELATIVE_TARGET_ENV, value)

    with pytest.raises(ValueError, match=MAX_RELATIVE_TARGET_ENV):
        follower_safety_kwargs()
