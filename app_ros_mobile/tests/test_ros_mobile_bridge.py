"""Regression tests for the upper-host ROS mobile bridge."""
from __future__ import annotations

import pytest

from lelab import ros_mobile


def test_readonly_status_is_loaded_before_recording(monkeypatch):
    bridge = ros_mobile.RosMobileBridge()
    calls = []

    def fake_http(method, path, payload=None, token=None, timeout=30.0):
        calls.append((method, path))
        return {"read_only": True, "profile": "hardware_readonly_v1"}

    monkeypatch.setattr(ros_mobile, "_http", fake_http)
    assert bridge.agent_token_required() == ""
    assert bridge.read_only is True
    assert calls == [("GET", "/status")]


def test_controllable_agent_still_requires_a_token(monkeypatch):
    bridge = ros_mobile.RosMobileBridge()

    monkeypatch.setattr(
        ros_mobile,
        "_http",
        lambda *args, **kwargs: {"read_only": False},
    )
    with pytest.raises(ros_mobile.BridgeError) as exc:
        bridge.agent_token_required()
    assert exc.value.status == 409
    assert bridge.read_only is False


@pytest.mark.parametrize("failure", [TimeoutError("timed out"), OSError(104, "Connection reset")])
def test_tunnel_stall_is_a_defined_agent_failure_not_an_unhandled_crash(monkeypatch, failure):
    """A stalled SSH tunnel surfaces as a socket error, not a URLError."""
    def explode(*args, **kwargs):
        raise failure

    monkeypatch.setattr(ros_mobile.urllib.request, "urlopen", explode)
    with pytest.raises(ros_mobile.BridgeError) as exc:
        ros_mobile._http("GET", "/status", timeout=5.0)
    assert exc.value.status == 503
    assert "car agent" in exc.value.detail


def test_command_timeout_detail_does_not_claim_the_car_stopped(monkeypatch):
    monkeypatch.setattr(ros_mobile.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")))
    with pytest.raises(ros_mobile.BridgeError) as exc:
        ros_mobile._http("POST", "/command", {"v": 1}, timeout=0.5)
    assert "timeout after 0.5s" in exc.value.detail
