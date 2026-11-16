"""An invalid measured pose must be explained before requesting frame outputs."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lelab import hybrid_stream_hardware as module
from lelab.hybrid_hardware import HardwareCompositionError


def status(raw=500):
    names = ("joint1", "joint2", "joint3", "joint4", "joint5", "r_joint")
    return {"control": {
        "follower_feedback": {"joint_samples": {
            name: {"raw_position": raw if name == "joint3" else 500}
            for name in names
        }},
        "feedback_raw_limits": {
            "version": "limits-v1",
            "sha256": "a" * 64,
            "binding_status": "device_bound",
            "device_identity": {"host_serial": "host-1"},
            "ranges": {
                name: [-50 if name == "joint3" else 0, 1000]
                for name in names
            },
        },
    }}


def driver():
    obj = object.__new__(module.FrameStreamDriver)
    obj.agent_url = "http://no-hardware.invalid"
    obj.timeout_s = 0.1
    return obj


@pytest.mark.parametrize("raw", [-51, 1001, True, None, float("nan"), float("inf")])
def test_invalid_measured_position_is_named_before_enable(monkeypatch, raw):
    monkeypatch.setattr(module, "_http_json", lambda *a, **k: status(raw))
    with pytest.raises(HardwareCompositionError, match="frame_emitter_feedback_out_of_range:joint3"):
        driver()._preflight_emitter_feedback()


@pytest.mark.parametrize("raw", [-50, 0, 500, 1000])
def test_preflight_preserves_the_existing_emitter_domain(monkeypatch, raw):
    monkeypatch.setattr(module, "_http_json", lambda *a, **k: status(raw))
    driver()._preflight_emitter_feedback()


def test_configured_negative_measurement_limit_is_read_from_the_car(monkeypatch):
    doc = status(-50)
    monkeypatch.setattr(module, "_http_json", lambda *a, **k: doc)
    driver()._preflight_emitter_feedback()
    doc["control"]["follower_feedback"]["joint_samples"]["joint3"]["raw_position"] = -51
    with pytest.raises(HardwareCompositionError, match="allowed=-50..1000"):
        driver()._preflight_emitter_feedback()


@pytest.mark.parametrize("transport", ["tcp", "dds"])
def test_bad_pose_never_requests_output_permission(monkeypatch, transport):
    calls = []
    lease = SimpleNamespace(token="test-only", stop=lambda reason: calls.append("rollback"))
    observer = SimpleNamespace()
    if transport == "tcp":
        obj = module.FrameStreamDriver(lease, observer, "http://no-hardware.invalid", host="127.0.0.1", port=1)
    else:
        obj = module.DdsFrameDriver(lease, observer, "http://no-hardware.invalid",
                                    dds_socket="unused", key_file="unused", sender_id="test")
        monkeypatch.setattr(module, "load_auth_key", lambda _: b"k" * 32)
        monkeypatch.setattr(module.DdsFrameLink, "build", lambda **_: SimpleNamespace(
            client=SimpleNamespace(close=lambda: calls.append("close"))))
    def http(_url, endpoint, **kwargs):
        calls.append(endpoint)
        assert endpoint == "/status", "Output request must not be reached"
        return status(-51)
    monkeypatch.setattr(module, "_http_json", http)
    with pytest.raises(HardwareCompositionError, match="joint3:raw=-51"):
        obj.enable({"session_id": "test", "session_epoch": 1,
                    "calibration_bundle_hash": "a" * 64})
    assert "/hybrid/enable_output" not in calls


def test_torque_failure_is_reported_even_when_outputs_never_enabled(monkeypatch):
    doc = {"licence": {"bridge": {"frame_process": {
        "outputs_enabled": False, "homing_torque_state": "failed",
        "homing_torque_error": "ID3_timeout"}}}}
    monkeypatch.setattr(module, "_http_json", lambda *a, **k: doc)
    with pytest.raises(HardwareCompositionError, match="homing_torque_unverified:ID3_timeout"):
        driver()._await_emitter_ready()


def test_old_refusal_is_only_diagnostic_on_timeout(monkeypatch):
    obj = driver()
    obj.EMITTER_ENABLE_TIMEOUT_S = 0.003
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    doc = {"licence": {"bridge": {"frame_process": {
        "outputs_enabled": False, "homing_torque_state": "idle",
        "last_refusal": {"reason": "enable_trial_feedback_invalid:joint3"}}}}}
    monkeypatch.setattr(module, "_http_json", lambda *a, **k: doc)
    with pytest.raises(HardwareCompositionError,
                       match="frame_emitter_enable_timeout:idle;car_last_refusal=enable_trial_feedback_invalid:joint3"):
        obj._await_emitter_ready()


def test_historical_refusal_does_not_block_a_successful_new_enable(monkeypatch):
    doc = {"licence": {"bridge": {"frame_process": {
        "outputs_enabled": True, "homing_motion": None,
        "last_refusal": {"reason": "enable_trial_feedback_invalid:joint3"}}}}}
    monkeypatch.setattr(module, "_http_json", lambda *a, **k: doc)
    driver()._await_emitter_ready()
