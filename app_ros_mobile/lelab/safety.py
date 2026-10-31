"""Local SO-101 motion-safety settings.

The limits live on the machine that owns the serial bus. Remote inference may
produce a target, but it cannot relax these guards: LeLab injects them into the
follower configuration used by teleoperation, recording, and rollout.

Environment values accept either a positive scalar or a JSON object keyed by
SO-101 motor name (without the ``.pos`` suffix). Use ``off``, ``none``,
``disabled``, or ``false`` to explicitly disable one guard.
"""

from __future__ import annotations

import json
import os

type SafetyLimit = float | dict[str, float] | None

MAX_RELATIVE_TARGET_ENV = "LELAB_MAX_RELATIVE_TARGET"
MAX_POSITION_DEVIATION_ENV = "LELAB_MAX_POSITION_DEVIATION"
CALIBRATION_RANGE_MARGIN_ENV = "LELAB_CALIBRATION_RANGE_MARGIN"
DEFAULT_MAX_RELATIVE_TARGET = 3.0
DEFAULT_MAX_POSITION_DEVIATION = 12.0
# 0 keeps every command inside the range measured at calibration time.
# Positive values shrink that range; negative values expand it (use a small
# negative value when the calibration was measured narrower than the real
# motion range, e.g. the red-bowl demos overshoot by up to 3.2 units).
DEFAULT_CALIBRATION_RANGE_MARGIN = 0.0

_DISABLED_VALUES = {"off", "none", "null", "disabled", "disable", "false"}


def _validate_limit(value: object, env_name: str) -> float | dict[str, float]:
    if isinstance(value, bool):
        raise ValueError(f"{env_name} must be a positive number, a motor map, or 'off'")
    if isinstance(value, (int, float)):
        scalar = float(value)
        if scalar <= 0:
            raise ValueError(f"{env_name} must be greater than zero; use 'off' to disable it")
        return scalar
    if isinstance(value, dict):
        if not value:
            raise ValueError(f"{env_name} motor map cannot be empty")
        parsed: dict[str, float] = {}
        for key, raw_limit in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"{env_name} motor-map keys must be non-empty strings")
            if isinstance(raw_limit, bool) or not isinstance(raw_limit, (int, float)):
                raise ValueError(f"{env_name}[{key!r}] must be a positive number")
            limit = float(raw_limit)
            if limit <= 0:
                raise ValueError(f"{env_name}[{key!r}] must be greater than zero")
            parsed[key] = limit
        return parsed
    raise ValueError(f"{env_name} must be a positive number, a JSON motor map, or 'off'")


def _read_limit(env_name: str, default: float) -> SafetyLimit:
    raw = os.getenv(env_name)
    if raw is None:
        return default
    text = raw.strip()
    if text.lower() in _DISABLED_VALUES:
        return None
    if not text:
        raise ValueError(f"{env_name} is empty; use 'off' to disable it")
    try:
        value = json.loads(text) if text.startswith("{") else float(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"{env_name} must be a positive number, a JSON motor map, or 'off'"
        ) from exc
    return _validate_limit(value, env_name)


def _validate_margin(value: object, env_name: str) -> float | dict[str, float]:
    """Like ``_validate_limit`` but margins may be zero or negative."""

    if isinstance(value, bool):
        raise ValueError(f"{env_name} must be a number, a motor map, or 'off'")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        if not value:
            raise ValueError(f"{env_name} motor map cannot be empty")
        parsed: dict[str, float] = {}
        for key, raw_limit in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"{env_name} motor-map keys must be non-empty strings")
            if isinstance(raw_limit, bool) or not isinstance(raw_limit, (int, float)):
                raise ValueError(f"{env_name}[{key!r}] must be a number")
            parsed[key] = float(raw_limit)
        return parsed
    raise ValueError(f"{env_name} must be a number, a JSON motor map, or 'off'")


def _read_margin(env_name: str, default: float) -> SafetyLimit:
    raw = os.getenv(env_name)
    if raw is None:
        return default
    text = raw.strip()
    if text.lower() in _DISABLED_VALUES:
        return None
    if not text:
        raise ValueError(f"{env_name} is empty; use 'off' to disable it")
    try:
        value = json.loads(text) if text.startswith("{") else float(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{env_name} must be a number, a JSON motor map, or 'off'") from exc
    return _validate_margin(value, env_name)


def follower_safety_kwargs() -> dict[str, SafetyLimit]:
    """Return fresh local follower limits so environment changes take effect."""

    return {
        "max_relative_target": _read_limit(MAX_RELATIVE_TARGET_ENV, DEFAULT_MAX_RELATIVE_TARGET),
        "max_position_deviation": _read_limit(
            MAX_POSITION_DEVIATION_ENV, DEFAULT_MAX_POSITION_DEVIATION
        ),
        "calibration_range_margin": _read_margin(
            CALIBRATION_RANGE_MARGIN_ENV, DEFAULT_CALIBRATION_RANGE_MARGIN
        ),
    }


def _format_cli_limit(value: float | dict[str, float]) -> str:
    if isinstance(value, dict):
        body = ", ".join(f"{key}: {limit}" for key, limit in value.items())
        return "{" + body + "}"
    return str(value)


def follower_safety_cli_args() -> list[str]:
    """Render enabled guards as draccus-compatible robot CLI arguments."""

    args: list[str] = []
    for name, limit in follower_safety_kwargs().items():
        if limit is not None:
            args.append(f"--robot.{name}={_format_cli_limit(limit)}")
    return args
