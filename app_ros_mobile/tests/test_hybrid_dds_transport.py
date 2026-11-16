from __future__ import annotations

from lelab.hybrid_dds_transport import DdsStreamClient


class _Socket:
    def close(self):
        return None


class _WindowSequenceClient(DdsStreamClient):
    def __init__(self, snapshots):
        super().__init__(
            socket_path="/unused",
            sender_id="upper-test",
            transport_epoch="dds-test",
            auth_key=b"k" * 32,
        )
        self.snapshots = list(snapshots)
        self.snapshot_calls = 0
        self.started = False
        self.bind_bundle("b" * 64)

    def _open(self):
        return _Socket()

    def _line_request(self, sock, payload, buffer=b""):
        del sock, payload
        return {"ok": True, "published": "hello"}, buffer

    def _status_snapshot(self):
        self.snapshot_calls += 1
        return self.snapshots.pop(0)

    def _start_threads(self):
        self.started = True


def _snapshot(stream_epoch: int, *, ready: bool, remaining_ms: float):
    return {
        "schema": "lelab.upper_dds_bridge.v1",
        "ok": True,
        "window": {
            "session_id": "session-1",
            "session_epoch": 9,
            "stream_epoch": stream_epoch,
            "window_id": stream_epoch,
            "duration_ms": 400,
            "remaining_ms_conservative": remaining_ms,
            "command_transport_ready": ready,
        },
        "receipts": [],
        "bridge": {"command_enabled": True},
    }


def test_connect_ignores_a_closed_previous_stream_epoch():
    client = _WindowSequenceClient([
        _snapshot(3, ready=False, remaining_ms=0.0),
        _snapshot(4, ready=True, remaining_ms=250.0),
    ])

    welcome = client.connect(
        lease_token="", session_id="session-1", session_epoch=9
    )

    assert welcome["stream_epoch"] == 4
    assert client.stream_epoch == 4
    assert client.current_window["stream_epoch"] == 4
    assert client.started is True
    assert client.snapshot_calls == 2
    client.close()
