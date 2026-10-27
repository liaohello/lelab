# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Single-owner lease for the SO-101 serial bus, cameras and motors.

Calibration, teleoperation, recording and inference all drive the same
follower over one serial link. The modules already refuse to start while a
sibling task is active, but that check is advisory: a crashed worker, a
closed browser tab or a second client can still leave the bus claimed with no
way to tell who owns it.

`HardwareLeaseManager` makes ownership explicit and enforceable:

* at most one task holds the lease, every other acquire fails with the current
  holder in the error payload;
* holders must renew (heartbeat) within their TTL, so a closed page or a dead
  worker cannot keep control indefinitely;
* an expiry triggers the registered stop hook and a *safe* stop of the running
  task (default), because "no heartbeat" must mean "no motion";
* the emergency stop pre-empts any lease regardless of holder or TTL;
* remote observers cannot acquire: only ``role="operator"`` is accepted.

The manager is deliberately transport-agnostic and clock-injectable so the
API layer and the tests can drive it without real time passing.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)

HardwareTask = Literal["calibration", "teleoperation", "recording", "inference"]

HARDWARE_TASKS: tuple[str, ...] = ("calibration", "teleoperation", "recording", "inference")
EMERGENCY_STOP_TASK = "emergency-stop"

_DISABLED_VALUES = {"0", "false", "no", "off", "disabled"}

DEFAULT_LEASE_TTL_SECONDS = 60.0
MIN_LEASE_TTL_SECONDS = 5.0
MAX_LEASE_TTL_SECONDS = 3600.0
#: A task is allowed this long to flip its module-level `*_active` flag after
#: the lease was granted before the activity probe is trusted.
ACTIVITY_GRACE_SECONDS = 10.0

Watcher = Callable[[str], None]
StopHook = Callable[[str, str], None]
ActivityProbe = Callable[[str], bool]


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def lease_ttl_from_env(env: dict[str, str] | None = None) -> float:
    """Read ``LELAB_HARDWARE_LEASE_TTL_SECONDS`` with clamping."""
    values = os.environ if env is None else env
    raw = values.get("LELAB_HARDWARE_LEASE_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_LEASE_TTL_SECONDS
    try:
        ttl = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid LELAB_HARDWARE_LEASE_TTL_SECONDS=%r", raw)
        return DEFAULT_LEASE_TTL_SECONDS
    return max(MIN_LEASE_TTL_SECONDS, min(MAX_LEASE_TTL_SECONDS, ttl))


def lease_required_from_env(env: dict[str, str] | None = None) -> bool:
    """Whether start endpoints must acquire a lease (default: yes)."""
    values = os.environ if env is None else env
    raw = values.get("LELAB_HARDWARE_LEASE_REQUIRED")
    if raw is None:
        return True
    return raw.strip().lower() not in _DISABLED_VALUES


class HardwareLeaseError(RuntimeError):
    """Raised when a lease cannot be granted or renewed.

    ``status_code`` lets the API layer map the failure to the right HTTP
    status without re-deriving the reason: 409 for a busy/expired lease, 403
    for an observer trying to take control.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 409,
        holder: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.holder = holder


@dataclass(frozen=True, slots=True)
class HardwareLease:
    """One granted lease. ``token`` is the only secret that may renew/release."""

    task: str
    owner: str
    token: str
    acquired_at: float
    expires_at: float
    ttl_seconds: float
    note: str | None = None

    @property
    def expires_in(self) -> float:
        return max(0.0, self.expires_at - time.time())

    def to_dict(self, *, now: float | None = None, include_token: bool = False) -> dict[str, Any]:
        current = time.time() if now is None else now
        payload = {
            "task": self.task,
            "owner": self.owner,
            "acquired_at": _iso(self.acquired_at),
            "expires_at": _iso(self.expires_at),
            "expires_in": round(max(0.0, self.expires_at - current), 3),
            "ttl_seconds": self.ttl_seconds,
            "note": self.note,
        }
        if include_token:
            payload["token"] = self.token
        return payload


@dataclass(slots=True)
class _ReleaseRecord:
    task: str
    owner: str
    reason: str
    released_at: float
    held_seconds: float


@dataclass(slots=True)
class HardwareLeaseManager:
    """Thread-safe, at-most-one-holder hardware lease.

    Parameters are injectable for tests: ``clock`` returns epoch seconds,
    ``stop_hook`` performs the task-specific safe stop, ``activity_probe``
    reports whether the owning module still runs.
    """

    clock: Callable[[], float] = time.time
    default_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS
    stop_on_expiry: bool = True
    activity_probe: ActivityProbe | None = None
    stop_hook: StopHook | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _lease: HardwareLease | None = field(default=None, init=False, repr=False)
    _last_release: _ReleaseRecord | None = field(default=None, init=False, repr=False)
    _stopped_tasks: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _watchdog: threading.Thread | None = field(default=None, init=False, repr=False)
    _watchdog_stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    # ------------------------------------------------------------------ setup
    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> HardwareLeaseManager:
        values = os.environ if env is None else env
        raw_stop = values.get("LELAB_LEASE_STOP_ON_EXPIRY", "1").strip().lower()
        return cls(
            default_ttl_seconds=lease_ttl_from_env(values),
            stop_on_expiry=raw_stop not in _DISABLED_VALUES,
        )

    def set_activity_probe(self, probe: ActivityProbe | None) -> None:
        with self._lock:
            self.activity_probe = probe

    def set_stop_hook(self, hook: StopHook | None) -> None:
        with self._lock:
            self.stop_hook = hook

    # --------------------------------------------------------------- acquire
    def acquire(
        self,
        task: str,
        *,
        owner: str,
        note: str | None = None,
        ttl_seconds: float | None = None,
        role: str = "operator",
        force: bool = False,
    ) -> HardwareLease:
        """Grant the lease to ``task`` or raise :class:`HardwareLeaseError`."""
        self._validate_task(task)
        if role != "operator":
            raise HardwareLeaseError(
                "Observer sessions are read-only and cannot take hardware control.",
                status_code=403,
            )
        normalized_owner = (owner or "").strip() or "unknown-client"
        ttl = self._normalize_ttl(ttl_seconds)

        with self._lock:
            now = self.clock()
            self._expire_locked(now, reason="expired")
            current = self._lease
            if current is not None and not force:
                raise HardwareLeaseError(
                    f"{current.task} is holding the hardware lease "
                    f"(owner: {current.owner}, {max(0.0, current.expires_at - now):.0f}s left). "
                    "Stop it first or send an emergency stop.",
                    holder=current.to_dict(now=now),
                )
            if current is not None:
                logger.warning(
                    "Pre-empting %s lease held by %s for %s (%s)",
                    current.task,
                    current.owner,
                    task,
                    normalized_owner,
                )
                self._release_locked(current, reason="preempted")

            lease = HardwareLease(
                task=task,
                owner=normalized_owner,
                token=secrets.token_urlsafe(24),
                acquired_at=now,
                expires_at=now + ttl,
                ttl_seconds=ttl,
                note=note,
            )
            self._lease = lease
            logger.info("Hardware lease granted to %s (%s) for %.0fs", task, normalized_owner, ttl)
            return lease

    def renew(
        self,
        token: str | None = None,
        *,
        task: str | None = None,
        owner: str | None = None,
        ttl_seconds: float | None = None,
    ) -> HardwareLease:
        """Extend a held lease, proving ownership by token or by task+owner.

        The task+owner form lets a browser heartbeat with the client id it
        already sends on every request, without the API having to hand the
        secret token to every reader of `/hardware/lease`.
        """
        with self._lock:
            now = self.clock()
            self._expire_locked(now, reason="expired")
            current = self._lease
            if current is None:
                raise HardwareLeaseError(
                    "The hardware lease is no longer held by this session.",
                )
            token_match = bool(token) and secrets.compare_digest(current.token, token or "")
            owner_match = (
                task is not None
                and owner is not None
                and current.task == task
                and secrets.compare_digest(current.owner, owner)
            )
            if not (token_match or owner_match):
                raise HardwareLeaseError(
                    "The hardware lease is no longer held by this session.",
                    holder=current.to_dict(now=now),
                )
            ttl = self._normalize_ttl(ttl_seconds if ttl_seconds is not None else current.ttl_seconds)
            renewed = HardwareLease(
                task=current.task,
                owner=current.owner,
                token=current.token,
                acquired_at=current.acquired_at,
                expires_at=now + ttl,
                ttl_seconds=ttl,
                note=current.note,
            )
            self._lease = renewed
            return renewed

    def release(
        self,
        *,
        token: str | None = None,
        task: str | None = None,
        owner: str | None = None,
        reason: str = "released",
        force: bool = False,
    ) -> bool:
        """Release the lease. Returns True when a lease was actually released.

        Without ``force`` the caller must prove ownership: either the token,
        or the task name (used by the stop endpoints, which only run when that
        task was the one that had started). ``force=True`` is reserved for the
        emergency-stop and operator-takeover paths.
        """
        with self._lock:
            current = self._lease
            if current is None:
                return False
            if not force:
                matched = (token and secrets.compare_digest(current.token, token)) or (
                    task is not None and current.task == task and (owner is None or current.owner == owner)
                )
                if not matched:
                    return False
            self._release_locked(current, reason=reason)
            return True

    def preempt(self, *, reason: str = "emergency-stop") -> HardwareLease | None:
        """Unconditionally drop the current lease (highest priority)."""
        with self._lock:
            current = self._lease
            if current is None:
                return None
            self._release_locked(current, reason=reason)
            return current

    # ---------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        """JSON-serializable snapshot for the API/frontend."""
        with self._lock:
            now = self.clock()
            self._expire_locked(now, reason="expired")
            lease = self._lease
            last = self._last_release
            return {
                "state": "held" if lease is not None else "idle",
                "task": lease.task if lease else None,
                "owner": lease.owner if lease else None,
                "acquired_at": _iso(lease.acquired_at) if lease else None,
                "expires_at": _iso(lease.expires_at) if lease else None,
                "expires_in": round(max(0.0, lease.expires_at - now), 3) if lease else None,
                "ttl_seconds": lease.ttl_seconds if lease else self.default_ttl_seconds,
                "note": lease.note if lease else None,
                "stop_on_expiry": self.stop_on_expiry,
                "last_release": (
                    {
                        "task": last.task,
                        "owner": last.owner,
                        "reason": last.reason,
                        "released_at": _iso(last.released_at),
                        "held_seconds": round(last.held_seconds, 3),
                    }
                    if last
                    else None
                ),
                "auto_stopped": list(self._stopped_tasks[-5:]),
            }

    def tick(self) -> list[dict[str, Any]]:
        """Enforce TTL and activity probes. Returns the actions taken.

        Called by the server watchdog (and by tests) roughly once per second.
        """
        actions: list[dict[str, Any]] = []
        with self._lock:
            now = self.clock()
            expired = self._expire_locked(now, reason="expired")
            if expired is not None:
                actions.append({"action": "expired", "task": expired.task, "owner": expired.owner})

            lease = self._lease
            probe = self.activity_probe
            if (
                lease is not None
                and probe is not None
                and now - lease.acquired_at > ACTIVITY_GRACE_SECONDS
            ):
                try:
                    active = bool(probe(lease.task))
                except Exception:  # a broken probe must never strand the lease
                    logger.exception("Hardware-lease activity probe failed for %s", lease.task)
                    active = True
                if not active:
                    self._release_locked(lease, reason="task-finished")
                    actions.append({"action": "task-finished", "task": lease.task, "owner": lease.owner})
        return actions

    # ------------------------------------------------------------- internals
    @staticmethod
    def _validate_task(task: str) -> None:
        if task not in HARDWARE_TASKS:
            raise HardwareLeaseError(
                f"Unknown hardware task {task!r}; expected one of {', '.join(HARDWARE_TASKS)}.",
                status_code=400,
            )

    def _normalize_ttl(self, ttl_seconds: float | None) -> float:
        if ttl_seconds is None:
            return self.default_ttl_seconds
        try:
            ttl = float(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise HardwareLeaseError("ttl_seconds must be a number", status_code=400) from exc
        if ttl <= 0:
            raise HardwareLeaseError("ttl_seconds must be positive", status_code=400)
        return max(MIN_LEASE_TTL_SECONDS, min(MAX_LEASE_TTL_SECONDS, ttl))

    def _release_locked(self, lease: HardwareLease, *, reason: str) -> None:
        now = self.clock()
        self._lease = None
        self._last_release = _ReleaseRecord(
            task=lease.task,
            owner=lease.owner,
            reason=reason,
            released_at=now,
            held_seconds=max(0.0, now - lease.acquired_at),
        )
        logger.info("Hardware lease released by %s (%s) after %s", lease.task, lease.owner, reason)

    def _expire_locked(self, now: float, *, reason: str) -> _ReleaseRecord | None:
        lease = self._lease
        if lease is None or now < lease.expires_at:
            return None
        self._release_locked(lease, reason=reason)
        if self.stop_on_expiry:
            self._stop_task_locked(lease, reason=reason)
        return self._last_release

    def _stop_task_locked(self, lease: HardwareLease, *, reason: str) -> None:
        hook = self.stop_hook
        record = {"task": lease.task, "owner": lease.owner, "reason": reason, "at": _iso(self.clock())}
        self._stopped_tasks.append(record)
        if hook is None:
            logger.warning(
                "Hardware lease for %s expired but no stop hook is registered; "
                "the task may still be running.",
                lease.task,
            )
            return
        try:
            hook(lease.task, reason)
        except Exception:
            logger.exception("Stop hook failed for %s after lease %s", lease.task, reason)

    def stop_task(self, task: str, *, reason: str) -> bool:
        """Run the stop hook for ``task`` outside the expiry path.

        Used by the emergency-stop endpoint, which must stop every task even
        when only one of them can hold the lease.
        """
        self._validate_task(task)
        hook = self.stop_hook
        if hook is None:
            return False
        try:
            hook(task, reason)
        except Exception:
            logger.exception("Stop hook failed for %s (%s)", task, reason)
            return False
        return True

    # -------------------------------------------------------------- watchdog
    def start_watchdog(self, *, interval: float = 1.0) -> None:
        """Repeatedly call :meth:`tick` so expiries are enforced without traffic."""
        with self._lock:
            if self._watchdog is not None and self._watchdog.is_alive():
                return
            self._watchdog_stop.clear()
            thread = threading.Thread(
                target=self._watchdog_loop,
                args=(max(0.1, float(interval)),),
                name="hardware-lease-watchdog",
                daemon=True,
            )
            self._watchdog = thread
        thread.start()

    def stop_watchdog(self, *, timeout: float = 2.0) -> None:
        self._watchdog_stop.set()
        thread = self._watchdog
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            self._watchdog = None

    def _watchdog_loop(self, interval: float) -> None:
        while not self._watchdog_stop.wait(interval):
            try:
                self.tick()
            except Exception:  # never let the watchdog die
                logger.exception("Hardware-lease watchdog tick failed")


_manager = HardwareLeaseManager.from_env()


def get_hardware_lease_manager() -> HardwareLeaseManager:
    """Process-wide lease manager shared by the API layer and the modules."""
    return _manager


def reset_hardware_lease_manager() -> HardwareLeaseManager:
    """Replace the singleton (tests only)."""
    global _manager
    _manager.stop_watchdog()
    _manager = HardwareLeaseManager.from_env()
    return _manager
