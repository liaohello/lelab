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
"""Non-blocking observation delivery for the async inference robot client.

Uploading an observation (two camera frames over the SSH tunnel to the policy
server) takes ~200ms on the Jetson. Running that upload inline stalled the
30Hz control loop down to ~16Hz and made the arm freeze-and-jump. This helper
moves the upload to a background thread and keeps only the newest pending
observation, so a slow link can drop stale frames but never block the loop.

Safety note: the ``must_go`` flag of a dropped observation is carried over to
the newer one that replaces it. Otherwise the client could silently lose the
"queue is empty, request a chunk now" signal and starve.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


class TimedObservationLike(Protocol):
    """The pieces of ``TimedObservation`` this sender needs."""

    must_go: bool

    def get_timestep(self) -> int: ...


@dataclass
class ObservationSenderStats:
    """Counters since the sender was created."""

    submitted: int = 0
    sent: int = 0
    dropped: int = 0
    failed: int = 0
    last_send_ms: float | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted,
            "sent": self.sent,
            "dropped": self.dropped,
            "failed": self.failed,
            "last_send_ms": None if self.last_send_ms is None else round(self.last_send_ms, 1),
        }


class LatestObservationSender:
    """Hand observations to a background thread, keeping only the newest."""

    def __init__(
        self,
        send: Callable[[Any], bool],
        *,
        logger: logging.Logger | None = None,
        poll_s: float = 0.1,
        thread_name: str = "observation-sender",
    ) -> None:
        self._send = send
        self._logger = logger or logging.getLogger(__name__)
        self._poll_s = poll_s
        self._thread_name = thread_name

        self._lock = threading.Lock()
        self._pending: Any | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = ObservationSenderStats()

    # -- producer side -----------------------------------------------------

    def submit(self, observation: Any) -> None:
        """Queue an observation for upload without blocking the caller."""
        with self._lock:
            previous = self._pending
            if previous is not None:
                self._stats.dropped += 1
                # Never lose a must-go request because a newer frame replaced it.
                if getattr(previous, "must_go", False):
                    observation.must_go = True
            self._pending = observation
            self._stats.submitted += 1
        self._wake.set()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    # -- consumer side -----------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name=self._thread_name, daemon=True)
        self._thread.start()

    def run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._poll_s)
            if self._stop.is_set():
                break
            # Clear before reading: a submit racing with this block stays
            # visible either way (event set again or pending already stored).
            self._wake.clear()
            with self._lock:
                observation = self._pending
                self._pending = None
            if observation is None:
                continue

            started = time.perf_counter()
            try:
                success = self._send(observation)
            except Exception as exc:  # noqa: BLE001 - the thread must survive
                self._stats.failed += 1
                self._logger.error(f"Observation sender failed: {exc}")
                continue
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._stats.last_send_ms = elapsed_ms
            if success:
                self._stats.sent += 1
            else:
                self._stats.failed += 1
                if not self._stop.is_set():
                    self._logger.error(
                        f"Observation #{observation.get_timestep()} was not delivered "
                        "to the policy server"
                    )

    # -- introspection -----------------------------------------------------

    @property
    def stats(self) -> ObservationSenderStats:
        with self._lock:
            return ObservationSenderStats(**vars(self._stats))

    @property
    def pending(self) -> bool:
        with self._lock:
            return self._pending is not None
