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
