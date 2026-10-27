# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Absolute joint limits taken from the calibration file.

``max_position_deviation`` guards a small window around the pose captured on
the first action, which does not fit tasks that travel far from that pose (see
LeLab ``refer/debug_log.md`` case 4). The per-joint range measured during
calibration is task-independent and is the same range the bus uses to convert
between encoder ticks and commands, so clamping there is a real mechanical
bound that never fights the demonstration trajectory.

It matters most in ``MotorNormMode.DEGREES`` (the SO-101 default): the bus
clamps the ``RANGE_*`` modes itself, but degree commands are passed through
untouched.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pprint import pformat

logger = logging.getLogger(__name__)


def ensure_within_calibrated_range(
    goal_pos: Mapping[str, float],
    limits: Mapping[str, tuple[float, float]],
    margin: float | Mapping[str, float] | None = 0.0,
) -> dict[str, float]:
    """Clamp ``{motor: goal}`` commands into the calibrated joint range.

    Args:
        goal_pos: commanded positions, in the same units ``send_action`` uses.
        limits: per-motor ``(low, high)`` pairs in those same units.
        margin: shrinks both ends when positive; a negative value expands the
            range (useful when the calibration was measured slightly narrower
            than the real motion range). Either one value or a per-motor map.
    """

    clamped: dict[str, float] = {}
    warnings: dict[str, dict[str, float]] = {}
    for motor, goal in goal_pos.items():
        low, high = limits[motor]
        pad = (
            float(margin.get(motor, 0.0))
            if isinstance(margin, Mapping)
            else float(margin or 0.0)
        )
        low += pad
        high -= pad
        if low > high:
            # Range narrower than the requested margin: pin it to the midpoint.
            low = high = (low + high) / 2
        safe_goal = min(high, max(low, goal))
        clamped[motor] = safe_goal
        if abs(safe_goal - goal) > 1e-4:
            warnings[motor] = {
                "original goal_pos": goal,
                "safe goal_pos": safe_goal,
                "limits": (low, high),
            }

    if warnings:
        logger.warning(
            "Goal position exceeded the calibrated joint range and was clamped.\n"
            f"{pformat(warnings, indent=4)}"
        )

    return clamped
