"""DDS facade wiring without a car, a bridge or a serial port."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "app_ros_mobile")]

from lelab.hybrid_stream_hardware import (  # noqa: E402
    DdsFrameDriver, FrameStreamDriver, build_stream_hardware_facade,
)


def test_the_dds_environment_selects_the_dds_driver_and_lease(monkeypatch):
    monkeypatch.setenv("LELAB_HYBRID_TRANSPORT", "dds_v1")
    monkeypatch.setenv("LELAB_DDS_BRIDGE_SOCKET", "/tmp/lelab-test-bridge.sock")
    monkeypatch.setenv("LELAB_DDS_COMMAND_KEY_FILE", "/tmp/lelab-test-command.key")
    facade = build_stream_hardware_facade("http://127.0.0.1:1")
    assert isinstance(facade.driver, DdsFrameDriver)
    assert facade.driver.capabilities()["transport"] == "dds_v1"
    assert facade.driver.capabilities()["driver"] == "dds_v1"
    assert facade.car.expected_transport == "dds_v1"
    assert facade.driver.dds_socket == "/tmp/lelab-test-bridge.sock"
    assert facade.driver.key_file == "/tmp/lelab-test-command.key"
    status = facade.status()
    assert status["transport"] == "dds_v1"
    assert status["feedback_source"] == "http"


def test_the_frame_stream_environment_still_selects_frame_stream(monkeypatch):
    monkeypatch.setenv("LELAB_HYBRID_TRANSPORT", "frame_stream_v1")
    facade = build_stream_hardware_facade("http://127.0.0.1:1")
    assert isinstance(facade.driver, FrameStreamDriver)
    assert facade.driver.capabilities()["transport"] == "frame_stream_v1"
    assert facade.car.expected_transport == "frame_stream"


def test_the_observer_feedback_mode_is_explicit(monkeypatch):
    monkeypatch.setenv("LELAB_HYBRID_TRANSPORT", "dds_v1")
    monkeypatch.setenv("LELAB_HYBRID_FEEDBACK_SOURCE", "dds")
    monkeypatch.setenv("LELAB_DDS_FEEDBACK_SOCKET", "/tmp/lelab-test-feedback.sock")
    facade = build_stream_hardware_facade("http://127.0.0.1:1")
    assert facade.observer.feedback_source == "dds"
    assert facade.observer.dds_client is not None
    state = facade.observer.cache_state()
    assert state["feedback_source"] == "dds"
    assert state["dds"]["socket"] == "/tmp/lelab-test-feedback.sock"
