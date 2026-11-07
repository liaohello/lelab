"""Upper-computer side of the car frame link.

Two problems this solves, both measured before on the live path:

* the control worker used to make three blocking HTTP calls every 100 ms cycle - a
  status GET and one apply POST per channel - so its rate was the network's, not the
  loop's.  Here a cycle is one frame, sent without waiting for a reply;
* status is read from a background cache that carries its own age.  A worker never
  asks the car "are you still mine" inline, and a stale cache is treated as a fault
  rather than as permission to keep going.

The car-side licence authority is unchanged: the token presented on the stream is the
car control-lease token the session already holds, and the car validates it against
the mirror its own agent publishes.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .hybrid_channel import HybridProtocolError, command_v1
from .teleop_frame_v1 import FRAME_PROTOCOL_VERSION, FrameError, frame_v1
from .teleop_stream import StreamClient, StreamError

ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"]
# The frame is built on the worker thread and handed to an asynchronous writer.
# Leave enough headroom for that handoff under Python scheduling jitter: a window
# with less life than this is skipped before it consumes a frame sequence.
DEFAULT_WINDOW_MARGIN_MS = 120.0


class FrameLinkError(RuntimeError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class CachedCarStatus:
    """Background status cache.  Freshness is a property of the cache, not a hope.

    ``probe`` performs one remote read (the existing /status route).  Consumers ask
    ``fresh()`` and get either a snapshot with its age or None - never a network call
    inside the control loop.
    """

    def __init__(self, probe: Callable[[], dict[str, Any]], *, period_s: float = 0.5,
                 max_age_s: float = 1.0, clock: Callable[[], float] = time.monotonic):
        self.probe = probe
        self.period_s = max(0.05, float(period_s))
        self.max_age_s = max(0.1, float(max_age_s))
        self.clock = clock
        self.snapshot: dict[str, Any] | None = None
        self.taken_at: float | None = None
        self.last_error: str | None = None
        self.counts = {"polls": 0, "ok": 0, "failed": 0}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="car-status-cache", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.period_s):
            self.poll()

    def poll(self) -> bool:
        self.counts["polls"] += 1
        try:
            snapshot = self.probe()
        except Exception as error:      # noqa: BLE001 - a failed probe is a state, not a crash
            self.counts["failed"] += 1
            self.last_error = f"{type(error).__name__}: {error}"
            return False
        if not isinstance(snapshot, dict):
            self.counts["failed"] += 1
            self.last_error = "probe_returned_non_object"
            return False
        with self._lock:
            self.snapshot = snapshot
            self.taken_at = self.clock()
        self.counts["ok"] += 1
        self.last_error = None
        return True

    def age_s(self) -> float | None:
        with self._lock:
            if self.taken_at is None:
                return None
            return self.clock() - self.taken_at

    def fresh(self) -> dict[str, Any] | None:
        age = self.age_s()
        if age is None or age > self.max_age_s:
            return None
        with self._lock:
            return dict(self.snapshot or {})


class FrameLink:
    """One snapshot in, one frame out; replies are collected, never waited on."""

    def __init__(self, *, host: str, port: int, session_id, session_epoch, bundle_hash: str,
                 lease_token: str, ttl_ms: int = 150,
                 window_margin_ms: float = DEFAULT_WINDOW_MARGIN_MS,
                 status_cache: CachedCarStatus | None = None,
                 clock_ns: Callable[[], int] = time.monotonic_ns):
        self.session_id = session_id
        self.session_epoch = session_epoch
        self.bundle_hash = bundle_hash
        self.ttl_ms = int(ttl_ms)
        if not 1 <= self.ttl_ms <= 200:
            raise FrameLinkError("ttl_out_of_range", "the car gate caps a command at 200 ms")
        self.window_margin_ms = float(window_margin_ms)
        self.status_cache = status_cache
        self._clock_ns = clock_ns
        self.client = StreamClient(host=host, port=port, timeout_s=1.0)
        self._seq = {"arm": 0, "gripper": 0}
        self._frame_seq = 0
        self.counts = {"submitted": 0, "skipped_no_window": 0, "admitted": 0,
                       "refused": 0, "receipts": 0, "superseded": 0, "errors": 0}
        self.refusals: dict[str, int] = {}
        self.last_error: str | None = None
        self.stream_epoch: int | None = None
        self.engaged = False

    # -- session -----------------------------------------------------------
    def engage(self) -> dict[str, Any]:
        """Connect and authenticate with the lease the session already holds.

        Recovery is explicit by construction: a reconnect re-hellos, and the car
        retires everything the previous generation had buffered.
        """
        try:
            welcome = self.client.connect(lease_token=self._lease_token(),
                                          session_id=self.session_id,
                                          session_epoch=self.session_epoch)
        except StreamError as error:
            self.last_error = f"{error.reason}: {error.detail}"
            raise FrameLinkError(error.reason, error.detail) from error
        self.stream_epoch = welcome.get("stream_epoch")
        self.client.start_writer()
        if self.status_cache is not None:
            self.status_cache.start()
        self.engaged = True
        self._lease = None
        return welcome

    def _lease_token(self) -> str:
        token = getattr(self, "_lease", None)
        if not token:
            raise FrameLinkError("no_lease", "acquire the car control lease first")
        return token

    def attach_lease(self, token: str) -> None:
        """Hand over the existing car lease token; this creates no second lease."""
        self._lease = token

    def disengage(self, reason: str) -> None:
        self.engaged = False
        try:
            self.client.send({"type": "preempt", "action": "pause_all", "reason": reason})
        except StreamError:
            self.counts["errors"] += 1
        self.client.close()
        if self.status_cache is not None:
            self.status_cache.stop()

    def stop(self, reason: str) -> None:
        """Emergency stop: a preemption ahead of anything still queued."""
        try:
            self.client.send({"type": "preempt", "action": "estop", "reason": reason})
        except StreamError as error:
            self.counts["errors"] += 1
            self.last_error = f"estop_send_failed:{error.reason}"
        self.engaged = False

    # -- the per-cycle path ------------------------------------------------
    def send_snapshot(self, *, arm_positions: list[float], gripper_rad: float,
                      sample_seq: int, leader_rad: list[float] | None = None,
                      pre_clip_rad: list[float] | None = None,
                      clutch: bool | str = True) -> dict[str, Any]:
        """Build and submit one whole frame for this input snapshot.

        Returns a link-level outcome.  It never blocks on the car's reply, and a
        refusal here is only about the sender's own view: the car's admission decides
        whether the frame was usable.
        """
        self.collect()
        if not self.engaged:
            raise FrameLinkError("not_engaged", "call engage() after an explicit align")
        window = self.client.current_window
        remaining = float((window or {}).get("window_remaining_ms") or 0.0)
        if window is None or remaining < self.window_margin_ms:
            # A frame naming a nearly-closed window is refused by the car, so the
            # sender pauses instead of burning sequence numbers on certain refusals.
            self.counts["skipped_no_window"] += 1
            return {"sent": False, "reason": "no_fresh_window", "remaining_ms": remaining}
        # Candidate numbers first: a snapshot that fails to build must consume none of
        # them, which is the same rule the car applies to a refused frame.
        next_seq = {"arm": self._seq["arm"] + 1, "gripper": self._seq["gripper"] + 1}
        candidate_frame = self._frame_seq + 1
        commands = {}
        for channel, target in (
            ("arm", {"joint_names": ARM_JOINTS, "positions_rad": list(arm_positions)}),
            ("gripper", {"joint_name": "r_joint", "position_rad": float(gripper_rad)}),
        ):
            if leader_rad is not None and channel == "arm":
                target["leader_rad"] = list(leader_rad)
            if pre_clip_rad is not None and channel == "arm":
                target["pre_clip_rad"] = list(pre_clip_rad)
            try:
                commands[channel] = command_v1(
                    session_id=self.session_id, session_epoch=self.session_epoch,
                    channel=channel, source="so101_leader", seq=next_seq[channel],
                    sample_seq=sample_seq, ttl_ms=self.ttl_ms,
                    calibration_bundle_hash=self.bundle_hash, target=target, clutch=clutch)
            except HybridProtocolError as error:
                raise FrameLinkError(f"command_{error.reason}", str(error)) from error
        try:
            frame = frame_v1(session_id=self.session_id, session_epoch=self.session_epoch,
                             calibration_bundle_hash=self.bundle_hash,
                             frame_seq=candidate_frame, stream_epoch=self.stream_epoch,
                             sample_seq=sample_seq, freshness_window_id=window["window_id"],
                             commands=commands)
        except FrameError as error:
            raise FrameLinkError(error.reason, error.detail) from error
        self._seq = next_seq
        self._frame_seq = candidate_frame
        self.client.submit_frame(frame)
        self.counts["submitted"] += 1
        return {"sent": True, "frame_seq": self._frame_seq, "sample_seq": sample_seq,
                "window_remaining_ms": remaining,
                "status_age_s": None if self.status_cache is None else self.status_cache.age_s()}

    def collect(self) -> list[dict[str, Any]]:
        """Pick up whatever replies have arrived.  Never blocks."""
        receipts = []
        try:
            self.client.pump(0.0)
        except StreamError as error:
            self.counts["errors"] += 1
            self.last_error = f"pump_{error.reason}"
            return receipts
        for receipt in self.client.drain_receipts():
            self.counts["receipts"] += 1
            receipts.append(receipt)
            if receipt.get("state") == "admitted":
                self.counts["admitted"] += 1
            else:
                self.counts["refused"] += 1
                reason = str(receipt.get("reason"))
                self.refusals[reason] = self.refusals.get(reason, 0) + 1
        return receipts

    def state(self) -> dict[str, Any]:
        return {"engaged": self.engaged, "stream_epoch": self.stream_epoch,
                "frame_seq": self._frame_seq, "channel_seq": dict(self._seq),
                "counts": dict(self.counts), "refusals": dict(self.refusals),
                "last_error": self.last_error,
                "sender": self.client.stats(),
                "status": (None if self.status_cache is None else
                           {"age_s": self.status_cache.age_s(),
                            "fresh": self.status_cache.fresh() is not None,
                            "counts": dict(self.status_cache.counts),
                            "last_error": self.status_cache.last_error}),
                "frame_protocol_version": FRAME_PROTOCOL_VERSION,
                "transport": "frame_stream"}
