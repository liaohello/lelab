"""Tests for LeLab's non-blocking observation bridge on LeRobot 0.7.

The retired fork helper under ``lerobot/async_inference`` is gone; this app now
imports its own stdlib bridge instead of loading a file path or skipping.
"""

from __future__ import annotations

import time

from lelab.utils.inference_070 import LatestObservationSender


class _Observation:
    def __init__(self, timestep: int, must_go: bool = False) -> None:
        self.timestep = timestep
        self.must_go = must_go

    def get_timestep(self) -> int:
        return self.timestep


def test_submit_never_blocks_the_caller() -> None:
    sent: list[_Observation] = []

    def slow_send(observation: _Observation) -> bool:
        time.sleep(0.2)
        sent.append(observation)
        return True

    sender = LatestObservationSender(slow_send)
    sender.start()
    try:
        started = time.perf_counter()
        for timestep in range(5):
            sender.submit(_Observation(timestep))
        elapsed = time.perf_counter() - started

        assert elapsed < 0.05, "submit() must not wait for the network"
        deadline = time.time() + 2.0
        while time.time() < deadline and sender.stats.sent < 2:
            time.sleep(0.02)
    finally:
        sender.stop()

    stats = sender.stats
    assert stats.submitted == 5
    assert stats.dropped >= 3, "intermediate frames are dropped, not queued forever"
    assert sent[-1].get_timestep() == 4, "the newest observation is delivered last"


def test_must_go_survives_being_replaced() -> None:
    sent: list[_Observation] = []
    sender = LatestObservationSender(lambda observation: sent.append(observation) or True)
    sender.start()
    try:
        sender.submit(_Observation(0, must_go=True))
        sender.submit(_Observation(0, must_go=False))
        deadline = time.time() + 2.0
        while time.time() < deadline and not sent:
            time.sleep(0.02)
    finally:
        sender.stop()

    assert sent and sent[0].must_go is True, "a dropped must-go request is carried over"


def test_send_failure_does_not_kill_the_thread() -> None:
    attempts: list[int] = []

    def flaky_send(observation: _Observation) -> bool:
        attempts.append(observation.get_timestep())
        if len(attempts) == 1:
            raise RuntimeError("tunnel reset")
        return True

    sender = LatestObservationSender(flaky_send)
    sender.start()
    try:
        sender.submit(_Observation(0))
        time.sleep(0.15)
        for _ in range(20):
            if sender.stats.failed:
                break
            time.sleep(0.02)
        sender.submit(_Observation(1))
        deadline = time.time() + 2.0
        while time.time() < deadline and sender.stats.sent < 1:
            time.sleep(0.02)
    finally:
        sender.stop()

    stats = sender.stats
    assert stats.failed == 1
    assert stats.sent == 1
    assert attempts == [0, 1]


def test_stop_ends_the_thread() -> None:
    sender = LatestObservationSender(lambda observation: True)
    sender.start()
    sender.stop()

    assert sender._thread is not None and not sender._thread.is_alive()
