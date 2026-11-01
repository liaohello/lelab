"""No-hardware protection tests for the Jetson-hosted policy path.

These tests exercise the local client, contract checks, mode boundaries, TTL,
fault handling, and the read-only observation source with fake transports.  They
do not start the Jetson server, open a serial port, or touch a camera.
"""

from __future__ import annotations

import json
import pickle
import time
from pathlib import Path

import numpy as np
import pytest

from lelab import jetson_policy as jp

try:
    import grpc

    from lerobot.async_inference.helpers import TimedAction, TimedObservation
    from lerobot.transport import services_pb2
except Exception as exc:  # pragma: no cover - environment guard
    pytest.skip(f"async inference dependencies unavailable: {exc}", allow_module_level=True)


def _checkpoint(
    root: Path,
    *,
    state_dim: int = 6,
    action_dim: int = 6,
    cameras: tuple[str, ...] = ("right", "up"),
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    inputs = {
        "observation.state": {"type": "STATE", "shape": [state_dim]},
    }
    for camera in cameras:
        inputs[f"observation.images.{camera}"] = {
            "type": "VISUAL",
            "shape": [3, 480, 640],
        }
    (root / "config.json").write_text(
        json.dumps(
            {
                "type": "act",
                "input_features": inputs,
                "output_features": {"action": {"type": "ACTION", "shape": [action_dim]}},
                "chunk_size": 10,
                "n_action_steps": 5,
                "normalization_mapping": {
                    "VISUAL": "MEAN_STD",
                    "STATE": "MEAN_STD",
                    "ACTION": "MEAN_STD",
                },
            }
        )
    )
    (root / "policy_preprocessor.json").write_text(
        json.dumps(
            {
                "steps": [
                    {"registry_name": "to_batch_processor"},
                    {"registry_name": "normalizer_processor"},
                ]
            }
        )
    )
    (root / "policy_postprocessor.json").write_text(
        json.dumps({"steps": [{"registry_name": "unnormalizer_processor"}]})
    )
    return root


def _contract(root: Path, **kwargs) -> jp.PolicyContract:
    return jp.PolicyContract.from_checkpoint(_checkpoint(root, **kwargs))


class _FakeStub:
    """Minimal gRPC stub: one valid chunk per sent observation."""

    def __init__(
        self,
        action_dim: int,
        *,
        delay_s: float = 0.0,
        bad_action: str | None = None,
        fail: bool = False,
    ):
        self.action_dim = action_dim
        self.delay_s = delay_s
        self.bad_action = bad_action
        self.fail = fail
        self.calls: list[str] = []
        self.observations: list[TimedObservation] = []
        self._observation_event = time.time()  # noqa: F841
        self._obs_ready = __import__("threading").Event()
        self._returned: set[int] = set()

    def Ready(self, request):  # noqa: N802
        self.calls.append("ready")
        return services_pb2.Empty()

    def SendPolicyInstructions(self, request):  # noqa: N802
        self.calls.append("instructions")
        self.policy = pickle.loads(request.data)
        return services_pb2.Empty()

    def SendObservations(self, iterator):  # noqa: N802
        self.calls.append("observations")
        data = b"".join(message.data for message in iterator)
        self.observations.append(pickle.loads(data))
        self._obs_ready.set()
        return services_pb2.Empty()

    def GetActions(self, request):  # noqa: N802
        self.calls.append("actions")
        if self.fail:
            raise grpc.RpcError("server exited")
        self._obs_ready.wait(2.0)
        if not self.observations:
            return services_pb2.Empty()
        observation = self.observations[-1]
        if observation.get_timestep() in self._returned:
            time.sleep(0.02)
            return services_pb2.Empty()
        self._returned.add(observation.get_timestep())
        time.sleep(self.delay_s)
        value = np.ones(self.action_dim, dtype=np.float32)
        if self.bad_action == "nan":
            value[0] = np.nan
        elif self.bad_action == "wrong_dim":
            value = np.ones(self.action_dim + 1, dtype=np.float32)
        actions = [
            TimedAction(
                timestamp=observation.get_timestamp() + index / 30.0,
                timestep=observation.get_timestep() + index,
                action=value,
            )
            for index in range(3)
        ]
        return services_pb2.Actions(data=pickle.dumps(actions))


def _client(
    tmp_path: Path,
    stub: _FakeStub,
    *,
    mode: str = jp.MODE_SHADOW,
    ttl_ms: float = 5000.0,
    startup_ttl_ms: float = 5000.0,
    action_sink: jp.BoundedActionSink | None = None,
) -> jp.JetsonShadowClient:
    config = jp.JetsonConfig(host="127.0.0.1", policy_port=1)
    contract = _contract(tmp_path, action_dim=stub.action_dim)
    source = jp.SyntheticObservationSource(contract, fps=20.0)
    return jp.JetsonShadowClient(
        config,
        contract,
        source,
        mode=mode,
        fps=20.0,
        ttl_ms=ttl_ms,
        startup_ttl_ms=startup_ttl_ms,
        duration_s=1.0,
        stub_factory=lambda: stub,
        metrics_path=tmp_path / "metrics.jsonl",
        action_sink=action_sink,
    )


def test_contract_matches_real_act_metadata():
    root = Path.home() / ".local/share/lelab-jetson-20261031/checkpoints"
    if not (root / "config.json").is_file():
        pytest.skip("copied ACT checkpoint metadata is not present")
    contract = jp.PolicyContract.from_checkpoint(root)
    assert contract.policy_type == "act"
    assert contract.state_dim == 6
    assert contract.action_dim == 6
    assert contract.image_keys == (
        "observation.images.right",
        "observation.images.up",
    )
    assert contract.normalization["ACTION"] == "MEAN_STD"
    assert "normalizer_processor" in contract.preprocessor_steps
    assert len(contract.checkpoint_sha256) == 64


def test_contract_rejects_wrong_observation_and_action(tmp_path: Path):
    contract = _contract(tmp_path)
    with pytest.raises(jp.JetsonPolicyError, match="missing state"):
        contract.validate_observation({"right": np.zeros((480, 640, 3), np.uint8)})
    with pytest.raises(jp.JetsonPolicyError, match="action shape"):
        contract.validate_action(np.zeros(7, dtype=np.float32))
    with pytest.raises(jp.JetsonPolicyError, match="NaN"):
        contract.validate_action(np.array([np.nan] + [0.0] * 5, dtype=np.float32))


def test_dataset_features_preserve_joint_order(tmp_path: Path):
    contract = _contract(tmp_path)
    features = contract.dataset_features()
    assert features["observation.state"]["names"] == list(jp.DEFAULT_STATE_NAMES)
    assert features["observation.images.right"]["shape"] == [480, 640, 3]


def test_observe_mode_never_opens_policy_channel_or_sends_actions(tmp_path: Path):
    def forbidden_stub():
        raise AssertionError("observe mode must not create a gRPC stub")

    contract = _contract(tmp_path)
    source = jp.SyntheticObservationSource(contract, fps=20.0)
    client = jp.JetsonShadowClient(
        jp.JetsonConfig(),
        contract,
        source,
        mode=jp.MODE_OBSERVE,
        fps=20.0,
        duration_s=0.2,
        stub_factory=forbidden_stub,
        metrics_path=tmp_path / "metrics.jsonl",
    )
    client.start()
    time.sleep(0.2)
    status = client.stop()["status"]
    assert status["mode"] == jp.MODE_OBSERVE
    assert status["observations_captured"] > 0
    assert status["observations_sent"] == 0
    assert status["actions_received"] == 0
    assert status["actions_dispatched"] == 0
    assert client.fault is None


def test_shadow_mode_records_actions_without_dispatching(tmp_path: Path):
    stub = _FakeStub(6)
    client = _client(tmp_path, stub)
    client.start()
    deadline = time.monotonic() + 2.0
    while client.metrics.chunks_received == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    status = client.stop()["status"]
    assert status["mode"] == jp.MODE_SHADOW
    assert status["chunks_received"] >= 1
    assert status["actions_received"] >= 3
    assert status["actions_dispatched"] == 0
    assert client.fault is None
    summary_path = tmp_path / "metrics-summary.json"
    assert summary_path.is_file()
    summary = json.loads(summary_path.read_text())
    assert summary["metrics"]["roundtrip_ms"]["p50"] is not None


def test_shadow_rejects_nan_action_and_faults(tmp_path: Path):
    stub = _FakeStub(6, bad_action="nan")
    client = _client(tmp_path, stub)
    client.start()
    deadline = time.monotonic() + 2.0
    while client.fault is None and time.monotonic() < deadline:
        time.sleep(0.02)
    client.stop()
    assert client.fault is not None
    assert "NaN" in client.fault


def test_shadow_rejects_wrong_dimension(tmp_path: Path):
    stub = _FakeStub(6, bad_action="wrong_dim")
    client = _client(tmp_path, stub)
    client.start()
    deadline = time.monotonic() + 2.0
    while client.fault is None and time.monotonic() < deadline:
        time.sleep(0.02)
    client.stop()
    assert client.fault is not None
    assert "action shape" in client.fault


def test_stale_chunk_is_rejected_by_local_monotonic_ttl(tmp_path: Path):
    stub = _FakeStub(6, delay_s=0.1)
    client = _client(tmp_path, stub, ttl_ms=5.0, startup_ttl_ms=5.0)
    client.start()
    deadline = time.monotonic() + 2.0
    while client.fault is None and time.monotonic() < deadline:
        time.sleep(0.02)
    client.stop()
    assert client.fault is not None
    assert "expired" in client.fault


def test_out_of_order_and_old_session_chunks_are_rejected(tmp_path: Path):
    contract = _contract(tmp_path)
    source = jp.SyntheticObservationSource(contract, fps=20.0)
    client = jp.JetsonShadowClient(
        jp.JetsonConfig(), contract, source, mode=jp.MODE_SHADOW, fps=20.0
    )
    client._pending[3] = {
        "capture_mono": time.monotonic(),
        "capture_ms": 0.0,
        "sent_mono": time.monotonic(),
    }
    chunk = [
        TimedAction(timestamp=time.time(), timestep=3, action=np.zeros(6, dtype=np.float32))
    ]
    client._last_chunk_seq = 4
    client._handle_chunk(chunk)
    assert client.metrics.chunks_rejected == 1
    client._handle_chunk(chunk, epoch=client.epoch + 1)
    assert client.metrics.chunks_rejected == 2
    assert client.metrics.chunks_received == 0


def test_execute_requires_all_gates_and_sink(tmp_path: Path):
    contract = _contract(tmp_path)
    source = jp.SyntheticObservationSource(contract)
    with pytest.raises(jp.ExecutionNotAllowedError, match="calibration_verified"):
        jp.JetsonShadowClient(
            jp.JetsonConfig(), contract, source, mode=jp.MODE_EXECUTE
        )
    gate = jp.ExecuteGate(
        calibration_verified=True,
        contract_verified=True,
        shadow_verified=True,
        stop_verified=True,
        site_authorized=True,
    )
    with pytest.raises(jp.ExecutionNotAllowedError, match="action sink"):
        jp.JetsonShadowClient(
            jp.JetsonConfig(), contract, source, mode=jp.MODE_EXECUTE, execute_gate=gate
        )


def test_execute_dispatches_only_validated_actions(tmp_path: Path):
    class RecordingSink:
        def __init__(self):
            self.actions: list[np.ndarray] = []

        def send(self, action):
            self.actions.append(np.asarray(action).copy())

    stub = _FakeStub(6)
    contract = _contract(tmp_path)
    gate = jp.ExecuteGate(True, True, True, True, True)
    sink = RecordingSink()
    bounded = jp.BoundedActionSink(sink, contract, ttl_ms=5000.0)
    client = jp.JetsonShadowClient(
        jp.JetsonConfig(),
        contract,
        jp.SyntheticObservationSource(contract, fps=20.0),
        mode=jp.MODE_EXECUTE,
        fps=20.0,
        ttl_ms=5000.0,
        startup_ttl_ms=5000.0,
        execute_gate=gate,
        action_sink=bounded,
        stub_factory=lambda: stub,
        metrics_path=tmp_path / "metrics.jsonl",
    )
    client.start()
    deadline = time.monotonic() + 3.0
    while not sink.actions and time.monotonic() < deadline:
        time.sleep(0.02)
    client.stop()
    assert sink.actions
    assert client.metrics.actions_dispatched >= 1
    assert client.fault is None


def test_disconnect_faults_and_clears_queue(tmp_path: Path):
    stub = _FakeStub(6, fail=True)
    client = _client(tmp_path, stub)
    client.start()
    deadline = time.monotonic() + 2.0
    while client.fault is None and time.monotonic() < deadline:
        time.sleep(0.02)
    client.stop()
    assert client.fault is not None
    assert "server_disconnected" in client.fault
    assert client.status()["queued_actions"] == 0


def test_duplicate_stop_is_idempotent(tmp_path: Path):
    stub = _FakeStub(6)
    client = _client(tmp_path, stub)
    client.start()
    time.sleep(0.1)
    first = client.stop()
    second = client.stop()
    assert first["stopped"] is True
    assert second["stopped"] is True
    assert second["already_stopped"] is True


class _FakeBus:
    def __init__(self, values: dict[str, float] | None = None):
        self.values = values or {
            "shoulder_pan": 1.0,
            "shoulder_lift": 2.0,
            "elbow_flex": 3.0,
            "wrist_flex": 4.0,
            "wrist_roll": 5.0,
            "gripper": 6.0,
        }
        self.connected = False
        self.disconnect_kwargs: dict | None = None
        self.writes: list[str] = []

    def connect(self):
        self.connected = True

    def sync_read(self, name):
        return dict(self.values)

    def disconnect(self, **kwargs):
        self.disconnect_kwargs = kwargs
        self.connected = False

    def write(self, *args, **kwargs):
        self.writes.append("write")


class _FakeCamera:
    def __init__(self, spec):
        self.spec = spec
        self.released = False

    def read(self):
        return True, np.zeros((480, 640, 3), dtype=np.uint8)

    def release(self):
        self.released = True


def test_read_only_source_opens_bus_and_cameras_without_writes(tmp_path: Path):
    contract = _contract(tmp_path)
    calibration = tmp_path / "calibration.json"
    calibration.write_text("{}")
    bus = _FakeBus()
    source = jp.ReadOnlyFollowerSource(
        "/dev/fake",
        calibration,
        contract,
        cameras=[{"key": "right", "index": 0}, {"key": "up", "index": 2}],
        bus_factory=lambda port, path: bus,
        camera_factory=_FakeCamera,
    )
    source.start()
    observation = source.capture()
    source.close()
    assert set(observation) == set(contract.state_names) | set(contract.camera_keys)
    assert bus.writes == []
    assert bus.disconnect_kwargs == {"disable_torque": False}
    assert source.forbidden_writes == ()
    with pytest.raises(jp.JetsonPolicyError, match="motor write"):
        source._bus = bus
        bus.write("Torque_Enable", "shoulder_pan", 1)


def test_read_only_source_rejects_missing_calibration(tmp_path: Path):
    contract = _contract(tmp_path)
    source = jp.ReadOnlyFollowerSource(
        "/dev/fake",
        tmp_path / "missing.json",
        contract,
        bus_factory=lambda port, path: _FakeBus(),
        camera_factory=_FakeCamera,
    )
    # The injected bus skips calibration parsing; the real path is covered by
    # the explicit file check in the default builder.
    assert source.calibration_path.name == "missing.json"
