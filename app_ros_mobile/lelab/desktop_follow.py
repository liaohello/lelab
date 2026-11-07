"""Relative-increment follow for the desktop leader/follower pair.

Sending the leader pose straight to the follower is what makes a session jump the moment
it starts: the two arms are almost never in the same pose. An alignment captures where the
leader is and where the *measured* follower is, and every later target is the follower
reference plus the leader's change since then.

Values are in the bus' own space (``<motor>.pos``, degrees with ``use_degrees=True``)
because that is what ``get_action()`` returns and what ``send_action()`` takes.
"""
from __future__ import annotations

from typing import Any

# wrist_roll is the one continuous joint: a leader moved past its stop reads back on the
# other side, and a raw subtraction there would command a full-spin sweep.
WRAPPED_JOINTS = frozenset({"wrist_roll"})


def motor_of(key: str) -> str:
    return key.split(".", 1)[0]


def wrap_delta(delta: float) -> float:
    """Fold a relative angle into (-180, 180] degrees."""
    folded = (delta + 180.0) % 360.0 - 180.0
    return 180.0 if folded == -180.0 else folded


def capture(leader: dict[str, float], follower_measured: dict[str, float]) -> tuple[dict[str, Any] | None, list[str]]:
    """Return (alignment, missing). Every leader joint needs a measured follower value.

    A missing joint is refused rather than defaulted: taking the follower reference from the
    last target or from zero would silently re-introduce the jump this whole module exists to
    avoid.
    """
    missing = sorted(key for key, value in leader.items() if follower_measured.get(key) is None)
    if missing:
        return None, missing
    return {"leader": dict(leader), "follower": dict(follower_measured), "wrap": sorted(WRAPPED_JOINTS)}, []


def follow_target(leader: dict[str, float], alignment: dict[str, Any]) -> dict[str, float]:
    reference_leader = alignment["leader"]
    reference_follower = alignment["follower"]
    target: dict[str, float] = {}
    for key, value in leader.items():
        if key not in reference_leader or key not in reference_follower:
            continue
        delta = value - reference_leader[key]
        if motor_of(key) in WRAPPED_JOINTS:
            delta = wrap_delta(delta)
        target[key] = reference_follower[key] + delta
    return target
