"""The bridge's flow services are thin wrappers over the existing FastAPI routes."""
from __future__ import annotations

import argparse
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "ros2_ws/src/lelab_mobile_agent"), str(ROOT / "app_ros_mobile")]

try:
    import rclpy
    from lelab_mobile_agent.upper_dds_bridge import UpperDdsBridge
    HAS_RUNTIME = True
except Exception as error:  # noqa: BLE001
    HAS_RUNTIME = False
    IMPORT_ERROR = f"{type(error).__name__}: {error}"

pytestmark = pytest.mark.skipif(not HAS_RUNTIME, reason=globals().get("IMPORT_ERROR", ""))


def args(socket_path: str) -> argparse.Namespace:
    return argparse.Namespace(
        feedback_topic="/lelab_ros/feedback/measured",
        health_topic="/lelab_ros/feedback/health",
        window_topic="/lelab_ros/control/window",
        receipt_topic="/lelab_ros/control/receipt",
        hello_topic="/lelab_ros/command/hello",
        command_topic="/lelab_ros/command/frame",
        preempt_topic="/lelab_ros/command/preempt",
        socket=socket_path, socket_uid=-1, socket_gid=-1,
        app_url="http://127.0.0.1:1", http_timeout_s=0.1,
        home_timeout_s=1.0, command_key_file="", simulated=True,
        duration=0.0, no_services=True)


@pytest.fixture()
def bridge():
    if not rclpy.ok():
        rclpy.init()
    with tempfile.TemporaryDirectory() as tmp:
        instance = UpperDdsBridge(args(str(Path(tmp) / "bridge.sock")))
        try:
            yield instance
        finally:
            instance.destroy_bridge()
    if rclpy.ok():
        rclpy.shutdown()


def request(**fields):
    base = {"action": "status", "session_id": "", "session_epoch": 0,
            "owner": "", "ttl_s": 0.0, "reason": "", "request_json": ""}
    base.update(fields)
    return types.SimpleNamespace(**base)


def response():
    return types.SimpleNamespace(success=False, state="", detail="", response_json="")


def test_session_control_status_and_acquire_are_passthrough(bridge):
    bridge._http = lambda path, body=None, method="GET": (
        200, {"state": "ready", "session_id": "s1"}) if path.endswith("/status") else (
        200, {"state": "ready", "session_id": "s1", "detail": ""})
    out = response()
    assert bridge._session_control(request(action="status"), out).success is True
    assert out.state == "ready"
    out = response()
    assert bridge._session_control(request(action="acquire", owner="operator"), out).success is True
    assert out.state == "ready"
    assert "s1" in out.response_json


def test_session_control_release_and_unknown_action(bridge):
    bridge._http = lambda *a, **k: (200, {"state": "disconnected"})
    out = response()
    assert bridge._session_control(request(action="release"), out).success is True
    assert out.state == "disconnected"
    out = response()
    assert bridge._session_control(request(action="teleport"), out).success is False
    assert out.detail == "unknown_action:teleport"


def test_clear_estop_never_grants_motion(bridge):
    bridge._http = lambda *a, **k: (200, {"state": "disconnected", "last_error": None})
    out = response()
    bridge._clear_estop(request(owner="operator"), out)
    assert out.success is True
    assert out.confirmed_state == "cleared"
    assert out.motion_granted is False


def test_clear_estop_reports_a_refused_or_unconfirmed_car(bridge):
    bridge._http = lambda *a, **k: (403, {"state": "estop", "detail": "wrong owner"})
    out = response()
    bridge._clear_estop(request(owner="other"), out)
    assert out.success is False
    assert out.confirmed_state == "pending_or_failed"
    assert out.motion_granted is False


def test_home_action_polls_the_existing_task_and_completes(bridge):
    calls = []

    def http(path, body=None, method="GET"):
        calls.append((path, body, method))
        if path.endswith("/home") and method == "POST":
            return 200, {"state": "starting"}
        return 200, {"state": "complete", "progress_pct": 100.0,
                     "current_joint": None, "last_error": None}

    bridge._http = http
    feedback = []
    goal_handle = types.SimpleNamespace(
        request=types.SimpleNamespace(session_id="s1", session_epoch=1,
                                      request_id="r1"),
        is_cancel_requested=False,
        publish_feedback=feedback.append,
        succeed=lambda: None, abort=lambda: None, canceled=lambda: None)
    result = bridge._execute_home(goal_handle)
    assert result.success is True
    assert result.state == "complete"
    assert calls[0][0].endswith("/home") and calls[0][2] == "POST"
    assert any(item.phase == "complete" for item in feedback)


def test_home_action_cancel_forwards_the_cancel_and_reports_it(bridge):
    calls = []

    def http(path, body=None, method="GET"):
        calls.append((path, body, method))
        if method == "POST" and (body or {}).get("action") == "cancel":
            return 200, {"state": "cancelled"}
        return 200, {"state": "moving", "progress_pct": 10.0}

    bridge._http = http
    goal_handle = types.SimpleNamespace(
        request=types.SimpleNamespace(session_id="s1", session_epoch=1,
                                      request_id="r1"),
        is_cancel_requested=True,
        publish_feedback=lambda _feedback: None,
        succeed=lambda: None, abort=lambda: None, canceled=lambda: None)
    result = bridge._execute_home(goal_handle)
    assert result.success is False
    assert result.state == "cancelled"
    assert any((body or {}).get("action") == "cancel" for _path, body, _method in calls)
