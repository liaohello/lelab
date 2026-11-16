"""Consumer-side owner/consumer separation for the servo readback driver."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "ros2_ws/src/lelab_mobile_agent")]

from lelab_mobile_agent import ros_servo_readback as readback  # noqa: E402


class FakeMessageType:
    def __init__(self, data=None):
        self.data = data


class FakeNode:
    def __init__(self):
        self.subscriptions = []
        self.last_rx = {}

    def create_subscription(self, message_type, topic, callback, depth):
        self.subscriptions.append((message_type, topic, callback, depth))
        return object()


def driver(node=None):
    node = node or FakeNode()
    instance = readback.RosServoFeedbackTopicDriver(
        node, FakeMessageType, calibration_hash="bundle-1",
        stale_after_s=1.0, max_scan_span_s=0.5, scan_period_s=0.2,
        health_topic=readback.HEALTH_TOPIC, health_message_type=FakeMessageType)
    return node, instance


def v2(owner=11, seq=1, span_us=20_000, values=(500, 510, 520, 530, 540, 550)):
    payload = [2, owner, seq, span_us]
    for servo_id, value in zip((1, 2, 3, 4, 5, 10), values, strict=True):
        payload.extend([servo_id, value])
    return FakeMessageType(payload)


def health(owner=11, scans_ok=3, scans_failed=1, last_seq=2):
    return FakeMessageType(json.dumps({
        "schema": readback.HEALTH_SCHEMA,
        "owner_instance": owner,
        "scans_ok": scans_ok,
        "scans_failed": scans_failed,
        "consecutive_failures": 1 if scans_failed else 0,
        "recovery_attempts": 2,
        "write_failures": 0,
        "last_success_seq": last_seq,
        "last_success_age_s": 0.2,
        "max_scan_interval_s": 0.31,
        "first_failure": {"servo_id": 5, "reason": "read_timeout"},
        "last_failure": {"servo_id": 5, "reason": "read_timeout"},
    }))


def test_owner_health_and_consumer_counters_are_reported_separately():
    node, instance = driver()
    assert len(node.subscriptions) == 2
    feedback_callback = node.subscriptions[0][2]
    health_callback = node.subscriptions[1][2]
    frames = []
    instance.on_frame = frames.append
    health_callback(health())
    feedback_callback(v2(seq=1))
    snapshot = instance.read_snapshot()
    assert snapshot["owner_health"]["owner_instance"] == 11
    assert snapshot["owner_health"]["scans_failed"] == 1
    assert snapshot["owner_health"]["first_failure"]["servo_id"] == 5
    assert snapshot["owner_health_age_s"] is not None
    assert snapshot["consumer"]["successful_scans"] == 1
    assert snapshot["consumer"]["failed_scans"] == 0
    assert snapshot["first_failure"] is None
    assert len(frames) == 1
    assert frames[0]["publisher_instance"] == 11


def test_a_consumer_receipt_failure_is_named_and_cleared_by_the_next_good_frame():
    node, instance = driver()
    feedback_callback = node.subscriptions[0][2]
    feedback_callback(FakeMessageType([2, 11, 1, 20_000, 1, 500]))  # truncated
    snapshot = instance.read_snapshot()
    assert snapshot["consumer"]["failed_scans"] == 1
    assert snapshot["first_failure"]["reason"] == "readback_payload_invalid"
    feedback_callback(v2(seq=2))
    snapshot = instance.read_snapshot()
    assert snapshot["first_failure"] is None
    assert snapshot["consumer"]["successful_scans"] == 1


def test_malformed_owner_health_is_rejected_without_replacing_the_last_good_one():
    node, instance = driver()
    health_callback = node.subscriptions[1][2]
    health_callback(health())
    health_callback(FakeMessageType("{}"))
    health_callback(FakeMessageType(json.dumps({
        "schema": readback.HEALTH_SCHEMA, "owner_instance": True,
        "scans_ok": 1, "scans_failed": 0})))
    snapshot = instance.read_snapshot()
    assert snapshot["owner_health"]["owner_instance"] == 11
    assert snapshot["owner_health_rejects"] == 2
    assert snapshot["owner_health_last_error"]


def test_an_old_owner_frame_is_rejected_and_its_reason_is_kept():
    node, instance = driver()
    feedback_callback = node.subscriptions[0][2]
    feedback_callback(v2(owner=11, seq=5))
    feedback_callback(v2(owner=12, seq=1))
    feedback_callback(v2(owner=11, seq=6))   # a retired owner's late frame
    snapshot = instance.read_snapshot()
    assert snapshot["consumer"]["failed_scans"] == 1
    assert snapshot["first_failure"]["reason"] == "readback_retired_owner"
    assert snapshot["publisher_instance"] == 12
    assert snapshot["publisher_generation"] == 1
