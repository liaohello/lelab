"""Tests for the single-owner hardware lease manager."""

from __future__ import annotations

import pytest

from lelab.hardware_lease import (
    ACTIVITY_GRACE_SECONDS,
    HardwareLeaseError,
    HardwareLeaseManager,
)


class FakeClock:
    """Deterministic epoch clock; ``advance`` moves it forward."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def manager(clock: FakeClock) -> HardwareLeaseManager:
    return HardwareLeaseManager(clock=clock, default_ttl_seconds=60.0)


def test_acquire_grants_exclusive_lease(manager: HardwareLeaseManager) -> None:
    lease = manager.acquire("recording", owner="operator-1")

    assert lease.task == "recording"
    assert lease.token
    status = manager.status()
    assert status["state"] == "held"
    assert status["task"] == "recording"
    assert status["owner"] == "operator-1"
    assert status["expires_in"] == pytest.approx(60.0)


def test_second_acquire_reports_current_holder(manager: HardwareLeaseManager) -> None:
    manager.acquire("teleoperation", owner="windows")

    with pytest.raises(HardwareLeaseError) as excinfo:
        manager.acquire("recording", owner="jetson-ui")

    assert excinfo.value.status_code == 409
    assert excinfo.value.holder is not None
    assert excinfo.value.holder["task"] == "teleoperation"
    assert excinfo.value.holder["owner"] == "windows"


def test_same_task_cannot_stack(manager: HardwareLeaseManager) -> None:
    manager.acquire("recording", owner="operator-1")
    with pytest.raises(HardwareLeaseError):
        manager.acquire("recording", owner="operator-1")


def test_observers_cannot_take_control(manager: HardwareLeaseManager) -> None:
    with pytest.raises(HardwareLeaseError) as excinfo:
        manager.acquire("inference", owner="phone", role="observer")

    assert excinfo.value.status_code == 403
    assert manager.status()["state"] == "idle"


def test_unknown_task_is_rejected(manager: HardwareLeaseManager) -> None:
    with pytest.raises(HardwareLeaseError) as excinfo:
        manager.acquire("mowing-the-lawn", owner="operator-1")
    assert excinfo.value.status_code == 400


def test_release_requires_the_token_or_task(manager: HardwareLeaseManager) -> None:
    lease = manager.acquire("calibration", owner="operator-1")

    assert manager.release(token="not-the-token") is False
    assert manager.status()["state"] == "held"
    assert manager.release(task="recording") is False
    assert manager.release(token=lease.token) is True
    assert manager.status()["state"] == "idle"
    assert manager.status()["last_release"]["reason"] == "released"


def test_stop_endpoint_release_by_task(manager: HardwareLeaseManager) -> None:
    manager.acquire("teleoperation", owner="operator-1")
    assert manager.release(task="teleoperation") is True
    assert manager.status()["state"] == "idle"


def test_renew_extends_only_with_the_token(manager: HardwareLeaseManager, clock: FakeClock) -> None:
    lease = manager.acquire("recording", owner="operator-1", ttl_seconds=20)
    clock.advance(15)

    renewed = manager.renew(lease.token, ttl_seconds=30)
    assert renewed.expires_at == clock.now + 30
    assert manager.status()["expires_in"] == pytest.approx(30.0)

    with pytest.raises(HardwareLeaseError):
        manager.renew("bogus-token")


def test_renew_accepts_task_and_owner_instead_of_the_token(
    manager: HardwareLeaseManager,
) -> None:
    manager.acquire("recording", owner="windows-chrome")

    lease = manager.renew(None, task="recording", owner="windows-chrome", ttl_seconds=15)

    assert lease.ttl_seconds == 15.0
    with pytest.raises(HardwareLeaseError):
        manager.renew(None, task="recording", owner="someone-else")


def test_status_never_exposes_the_token(manager: HardwareLeaseManager) -> None:
    lease = manager.acquire("recording", owner="operator-1")

    assert "token" not in manager.status()
    assert "token" not in lease.to_dict()
    assert lease.to_dict(include_token=True)["token"] == lease.token


def test_expiry_releases_and_stops_the_task(clock: FakeClock) -> None:
    stopped: list[tuple[str, str]] = []
    manager = HardwareLeaseManager(
        clock=clock,
        default_ttl_seconds=10.0,
        stop_on_expiry=True,
        stop_hook=lambda task, reason: stopped.append((task, reason)),
    )
    manager.acquire("recording", owner="browser-tab")

    clock.advance(10.5)
    actions = manager.tick()

    assert actions == [{"action": "expired", "task": "recording", "owner": "browser-tab"}]
    assert stopped == [("recording", "expired")]
    status = manager.status()
    assert status["state"] == "idle"
    assert status["last_release"]["reason"] == "expired"
    assert status["auto_stopped"][-1]["task"] == "recording"


def test_expiry_without_stop_on_expiry_still_releases(clock: FakeClock) -> None:
    stopped: list[tuple[str, str]] = []
    manager = HardwareLeaseManager(
        clock=clock,
        default_ttl_seconds=10.0,
        stop_on_expiry=False,
        stop_hook=lambda task, reason: stopped.append((task, reason)),
    )
    manager.acquire("inference", owner="browser-tab")

    clock.advance(11)
    assert manager.status()["state"] == "idle"
    assert stopped == []


def test_finished_task_releases_after_the_activity_grace(
    manager: HardwareLeaseManager, clock: FakeClock
) -> None:
    active = {"value": True}
    manager.set_activity_probe(lambda task: active["value"])
    manager.acquire("teleoperation", owner="operator-1")

    clock.advance(ACTIVITY_GRACE_SECONDS - 1)
    assert manager.tick() == []
    assert manager.status()["state"] == "held"

    active["value"] = False
    clock.advance(2)
    actions = manager.tick()

    assert actions == [{"action": "task-finished", "task": "teleoperation", "owner": "operator-1"}]
    assert manager.status()["state"] == "idle"


def test_emergency_preempt_drops_any_lease(manager: HardwareLeaseManager) -> None:
    lease = manager.acquire("recording", owner="operator-1")

    preempted = manager.preempt(reason="emergency-stop")

    assert preempted is not None and preempted.token == lease.token
    assert manager.status()["state"] == "idle"
    assert manager.status()["last_release"]["reason"] == "emergency-stop"
    # The old token must not work after the pre-emption.
    with pytest.raises(HardwareLeaseError):
        manager.renew(lease.token)


def test_forced_acquire_preempts_the_previous_holder(manager: HardwareLeaseManager) -> None:
    manager.acquire("teleoperation", owner="windows")
    lease = manager.acquire("recording", owner="jetson", force=True)
    assert manager.status()["owner"] == "jetson"
    assert manager.status()["task"] == "recording"
    assert lease.owner == "jetson"


def test_stop_task_runs_the_hook_for_any_task(manager: HardwareLeaseManager) -> None:
    stopped: list[tuple[str, str]] = []
    manager.set_stop_hook(lambda task, reason: stopped.append((task, reason)))

    assert manager.stop_task("inference", reason="emergency-stop") is True
    assert stopped == [("inference", "emergency-stop")]

    with pytest.raises(HardwareLeaseError):
        manager.stop_task("disco", reason="emergency-stop")


def test_broken_stop_hook_does_not_strand_the_lease(clock: FakeClock) -> None:
    def boom(task: str, reason: str) -> None:
        raise RuntimeError("hook exploded")

    manager = HardwareLeaseManager(clock=clock, default_ttl_seconds=5.0, stop_hook=boom)
    manager.acquire("recording", owner="operator-1")
    clock.advance(6)

    assert manager.status()["state"] == "idle"


def test_ttl_is_clamped_to_safe_bounds(manager: HardwareLeaseManager) -> None:
    lease = manager.acquire("inference", owner="operator-1", ttl_seconds=0.1)
    assert lease.ttl_seconds == 5.0
    with pytest.raises(HardwareLeaseError):
        manager.acquire("recording", owner="operator-1", ttl_seconds=-5)
