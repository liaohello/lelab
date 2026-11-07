"""Gamepad intent gate. No device access and no actuator output."""
from __future__ import annotations

import math
from typing import Any


class GamepadIntent:
    def __init__(self, deadzone: float, timeout_ns: int):
        if (isinstance(deadzone, bool) or not isinstance(deadzone, (int, float)) or
            not math.isfinite(deadzone) or not 0 <= deadzone < 1 or
            type(timeout_ns) is not int or timeout_ns <= 0):
            raise ValueError("invalid gamepad calibration")
        self.deadzone, self.timeout_ns = deadzone, timeout_ns
        self.reset()

    def reset(self, *, preserve_sequence: bool = False):
        previous_seq = getattr(self, "last_seq", -1) if preserve_sequence else -1
        previous_ns = getattr(self, "last_ns", None) if preserve_sequence else None
        self.neutral_seen = False
        self.previous_deadman = False
        self.armed = False
        self.last_seq = previous_seq
        self.last_ns = previous_ns
        self.intent = None

    def ingest(self, sample: dict[str, Any], now_ns: int) -> dict[str, Any]:
        try:
            seq, stamp = sample["seq"], sample["received_mono_ns"]
            axes, deadman = sample["axes"], sample["deadman"]
            if (isinstance(seq, bool) or not isinstance(seq, int) or seq <= self.last_seq or
                isinstance(stamp, bool) or not isinstance(stamp, int) or stamp > now_ns or
                now_ns - stamp >= self.timeout_ns or
                (self.last_ns is not None and stamp <= self.last_ns) or not isinstance(deadman, bool) or
                not isinstance(axes, list) or len(axes) != 3 or
                any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or abs(x) > 1 for x in axes)):
                raise ValueError("invalid_or_stale_gamepad_sample")
        except (KeyError, TypeError, ValueError) as exc:
            self.reset(preserve_sequence=True)
            raise ValueError("invalid_or_stale_gamepad_sample") from exc
        if self.last_ns is not None and stamp - self.last_ns >= self.timeout_ns:
            self.reset(preserve_sequence=True)
        neutral = all(abs(x) <= self.deadzone for x in axes)
        if neutral and not deadman:
            self.neutral_seen = True
        if not deadman:
            self.armed = False
        elif self.neutral_seen and not self.previous_deadman and neutral:
            self.armed = True
        self.previous_deadman, self.last_seq, self.last_ns = deadman, seq, stamp
        self.intent = [0.0 if abs(x) <= self.deadzone else float(x) for x in axes] if self.armed else None
        return self.snapshot(now_ns)

    def snapshot(self, now_ns: int) -> dict[str, Any]:
        if self.last_ns is not None and (now_ns < self.last_ns or now_ns - self.last_ns >= self.timeout_ns):
            self.reset(preserve_sequence=True)
        return {"armed": self.armed, "neutral_seen": self.neutral_seen, "intent": self.intent,
                "last_seq": self.last_seq, "last_received_mono_ns": self.last_ns}
