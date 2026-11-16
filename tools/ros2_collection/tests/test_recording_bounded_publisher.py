"""The recording publisher queue must never make a control tick wait on a slow consumer."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "ros2_ws/src/lelab_mobile_agent"), str(ROOT / "app_ros_mobile")]

try:
    from lelab_mobile_agent.car_control_process import BoundedRecordingPublisher
    HAS_RUNTIME = True
except Exception as error:  # noqa: BLE001 - app venv has no rclpy; Humble runs this
    HAS_RUNTIME = False
    IMPORT_ERROR = f"{type(error).__name__}: {error}"

pytestmark = pytest.mark.skipif(not HAS_RUNTIME, reason=globals().get("IMPORT_ERROR", ""))


class SlowPublisher:
    def __init__(self, delay_s: float = 0.02):
        self.delay_s = delay_s
        self.messages = []

    def publish(self, message):
        time.sleep(self.delay_s)
        self.messages.append(message)


class FastPublisher:
    def __init__(self):
        self.messages = []

    def publish(self, message):
        self.messages.append(message)


def test_a_slow_recorder_does_not_block_the_caller_and_drops_are_counted():
    publisher = SlowPublisher(0.02)
    queue = BoundedRecordingPublisher(publisher, maxlen=8)
    try:
        started = time.monotonic()
        for index in range(200):
            queue.publish(f"row-{index}")
        elapsed = time.monotonic() - started
        assert elapsed < 0.2, f"publish blocked for {elapsed:.3f}s"
        assert queue.state()["dropped"] > 0
        assert queue.state()["queued"] <= 8
    finally:
        queue.stop(timeout_s=0.2)


def test_a_normal_recorder_receives_rows_in_order():
    publisher = FastPublisher()
    queue = BoundedRecordingPublisher(publisher, maxlen=64)
    for index in range(20):
        queue.publish(f"row-{index}")
    deadline = time.monotonic() + 1.0
    while len(publisher.messages) < 20 and time.monotonic() < deadline:
        time.sleep(0.01)
    queue.stop(timeout_s=1.0)
    assert publisher.messages == [f"row-{index}" for index in range(20)]
    assert queue.state()["dropped"] == 0


def test_stop_is_bounded_even_with_a_slow_recorder():
    publisher = SlowPublisher(0.1)
    queue = BoundedRecordingPublisher(publisher, maxlen=4)
    for index in range(10):
        queue.publish(f"row-{index}")
    started = time.monotonic()
    queue.stop(timeout_s=0.2)
    elapsed = time.monotonic() - started
    assert elapsed < 0.6, f"stop took {elapsed:.3f}s"
    assert queue.state()["stopped"] is True
