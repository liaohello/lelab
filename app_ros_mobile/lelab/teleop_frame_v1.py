"""teleop_frame_v1: one complete arm+gripper target frame per input snapshot.

Pure Python 3.10, ROS-free, serial-port-free and network-free.  This is the single
source of truth for the frame wire contract: the upper computer package and the car
side both import the *same file* (the car-side copy is a symlink, guarded by a test
that compares SHA256), so neither side can drift from the other.

What a frame is
---------------
The envelope carries identity that is shared by both channels plus one sequence for
the frame itself, and its ``commands`` member holds two strict ``command_v1`` objects
(arm and gripper).  Nothing is invented here: channel permission, target units,
sequence rules and TTL ceilings stay where they are today, in the host router and
the car-side gate.  This module only guarantees that a frame is admitted or refused
as a whole.

Two rules the rest of the design depends on
-------------------------------------------
* validate-then-commit.  ``FrameAdmission.accept`` performs every check against
  unmodified state and only then advances sequences.  A refused frame consumes no
  ``frame_seq``, no channel ``seq`` and no filter or rate-limiter state.
* a frame's freshness is expressed by a window issued by the consuming side.  Host
  and car each read their own ``CLOCK_MONOTONIC``, so those values are never
  subtracted across the boundary: the window id names a deadline on the consumer's
  own clock, and the receiving deadline is
  ``min(window_deadline, received + ttl, licence_expiry)``.
"""
from __future__ import annotations

import copy
import math
import time
from typing import Any, Callable

FRAME_PROTOCOL_VERSION = 1
FRAME_SCHEMA_VERSION = 1

# Exact wire envelope.  A received frame that carries any other field - including a
# sender-side timestamp, which would be meaningless on this clock - is malformed.
FRAME_FIELDS = frozenset({
    "frame_protocol_version",
    "frame_seq",
    "stream_epoch",
    "session_id",
    "session_epoch",
    "calibration_bundle_hash",
    "sample_seq",
    "freshness_window_id",
    "commands",
})
COMMAND_FIELDS = frozenset({"arm", "gripper"})
# A frame never carries a base (chassis) target: this phase is arm + gripper only.
FRAME_CHANNELS = ("arm", "gripper")

TRANSPORTS = ("legacy_apply", "frame_http", "frame_stream")

MS_TO_NS = 1_000_000


class FrameError(ValueError):
    """Refusal of a whole frame.  ``reason`` is stable and wire-safe."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def _int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise FrameError("invalid_frame", f"{name} must be an integer >= {minimum}")
    return value


def _identity(value: Any, name: str):
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise FrameError("invalid_frame", f"{name} must be a non-empty string or integer")


def monotonic_ns() -> int:
    """This process' own clock.  Never compared against another host's value."""
    return time.monotonic_ns()


# --------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------
def frame_v1(*, session_id, session_epoch: str, calibration_bundle_hash: str,
             frame_seq: int, stream_epoch, sample_seq: int, freshness_window_id,
             commands: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a teleop_frame_v1 from two already-built ``command_v1`` objects."""
    if not isinstance(commands, dict):
        raise FrameError("invalid_commands", "commands must be an object")
    if set(commands) != set(COMMAND_FIELDS):
        raise FrameError("invalid_commands",
                         f"commands must hold exactly {sorted(COMMAND_FIELDS)}, got {sorted(commands)}")
    for channel, command in commands.items():
        if not isinstance(command, dict):
            raise FrameError("invalid_commands", f"{channel} command must be an object")
    shared = ("session_id", "session_epoch", "calibration_bundle_hash", "sample_seq")
    for field in shared:
        expected = {"session_id": session_id, "session_epoch": session_epoch,
                    "calibration_bundle_hash": calibration_bundle_hash,
                    "sample_seq": sample_seq}[field]
        for channel, command in commands.items():
            if command.get(field) != expected:
                raise FrameError("frame_identity_split",
                                 f"{channel}.{field} disagrees with the frame envelope")
    return {
        "frame_protocol_version": FRAME_PROTOCOL_VERSION,
        "frame_seq": _int(frame_seq, "frame_seq"),
        "stream_epoch": _identity(stream_epoch, "stream_epoch"),
        "session_id": _identity(session_id, "session_id"),
        "session_epoch": _identity(session_epoch, "session_epoch"),
        "calibration_bundle_hash": _identity(calibration_bundle_hash, "calibration_bundle_hash"),
        "sample_seq": _int(sample_seq, "sample_seq"),
        "freshness_window_id": _identity(freshness_window_id, "freshness_window_id"),
        "commands": copy.deepcopy(dict(commands)),
    }


# --------------------------------------------------------------------------
# freshness windows
# --------------------------------------------------------------------------
class FreshnessWindow:
    """A short admission window issued by the side that will emit the frame.

    The window id names a deadline on the issuer's own monotonic clock.  A sender
    may stamp a sample with the window it currently holds; a reconnect invalidates
    the window, so a buffered packet can never become valid again by itself.
    """

    def __init__(self, *, clock_ns: Callable[[], int] = monotonic_ns, history: int = 4):
        self._clock = clock_ns
        self._epoch = 0
        self._current: dict[str, Any] | None = None
        self._issued_count = 0
        # Windows are validated against their own deadline, so the ones still in
        # force overlap: a sender legitimately names the window it last received,
        # which is a tick behind the newest one.  The history is bounded, and a
        # generation change empties it, so an old buffer cannot be revived.
        self._history_limit = max(1, int(history))
        self._issued: dict[Any, dict[str, Any]] = {}

    @property
    def stream_epoch(self) -> int:
        return self._epoch

    @property
    def current_id(self):
        return None if self._current is None else self._current["window_id"]

    def new_stream_epoch(self, reason: str) -> int:
        """Rotate the generation: called on connect, disconnect, align and stop."""
        self._epoch += 1
        self._current = None
        self._issued = {}
        self._rotation_reason = reason
        return self._epoch

    def issue(self, *, duration_ms: int, window_seq: int | None = None) -> dict[str, Any]:
        duration_ms = _int(duration_ms, "duration_ms", minimum=1)
        if duration_ms > 1000:
            raise FrameError("window_too_long", "a freshness window must stay short")
        self._issued_count += 1
        window_id = window_seq if window_seq is not None else self._issued_count
        _identity(window_id, "window_id")
        now = self._clock()
        self._current = {
            "window_id": window_id,
            "stream_epoch": self._epoch,
            "issued_ns": now,
            "deadline_ns": now + duration_ms * MS_TO_NS,
            "duration_ms": duration_ms,
        }
        self._issued[window_id] = self._current
        while len(self._issued) > self._history_limit:
            oldest = next(iter(self._issued))
            if oldest == window_id:
                break
            del self._issued[oldest]
        return copy.deepcopy(self._current)

    def describe(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._current)

    def remaining_ms(self) -> float:
        if self._current is None:
            return 0.0
        return max(0.0, (self._current["deadline_ns"] - self._clock()) / MS_TO_NS)

    def resolve(self, window_id, stream_epoch) -> dict[str, Any]:
        """Pure check of a received window reference against the live window."""
        if self._current is None:
            raise FrameError("no_window", "the control side has not issued a window yet")
        if stream_epoch != self._epoch:
            raise FrameError("stream_epoch_mismatch",
                             f"frame carries stream epoch {stream_epoch}, live epoch is {self._epoch}")
        record = self._issued.get(window_id)
        if record is None:
            raise FrameError("stale_window",
                             f"window {window_id} is not among the {len(self._issued)} still in force "
                             f"(newest is {self._current['window_id']})")
        if record.get("stream_epoch") != self._epoch:
            raise FrameError("stale_window", f"window {window_id} belongs to a retired generation")
        if self._clock() >= record["deadline_ns"]:
            raise FrameError("window_expired", f"window {window_id} closed "
                             f"{(self._clock() - record['deadline_ns']) / MS_TO_NS:.1f} ms ago")
        return copy.deepcopy(record)


# --------------------------------------------------------------------------
# transport selection
# --------------------------------------------------------------------------
class TransportSelector:
    """One output transport per session epoch, negotiated explicitly.

    The legacy per-channel apply route stays reachable only while no frame
    transport has been activated, which is what keeps a negotiating failure from
    turning into a silent double-send.
    """

    def __init__(self):
        self._active: dict[str, Any] | None = None

    def negotiate(self, *, session_id, session_epoch, transport: str,
                  supported: tuple[str, ...] = TRANSPORTS) -> dict[str, Any]:
        if transport not in supported:
            raise FrameError("transport_unsupported", f"{transport!r} is not offered")
        if transport not in ("frame_http", "frame_stream"):
            raise FrameError("transport_not_framed",
                             "only the framed transports may be negotiated for a control epoch")
        candidate = {"session_id": session_id, "session_epoch": session_epoch,
                     "transport": transport, "legacy_apply_enabled": False}
        if self._active is not None and self._active["session_epoch"] == session_epoch:
            if self._active["transport"] != transport:
                raise FrameError("transport_switch_locked",
                                 f"epoch {session_epoch} already uses {self._active['transport']}")
            return copy.deepcopy(self._active)
        self._active = candidate
        return copy.deepcopy(candidate)

    def active(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._active) if self._active else None

    def allows_legacy_apply(self, *, session_id, session_epoch) -> bool:
        if self._active is None:
            return True
        return not (self._active["session_id"] == session_id
                    and self._active["session_epoch"] == session_epoch)

    def release(self, *, session_id, session_epoch, reason: str) -> None:
        if self._active and self._active["session_id"] == session_id \
                and self._active["session_epoch"] == session_epoch:
            self._active = None
        self.last_release_reason = reason


# --------------------------------------------------------------------------
# whole-frame admission
# --------------------------------------------------------------------------
class FrameAdmission:
    """Validate-then-commit admission of complete frames for one control epoch.

    ``channel_validator`` is the host router or the car-side gate: it receives a
    single ``command_v1`` and must raise (or return a refusal) without touching
    state when that command would not be admissible.  ``sequences`` is owned here so
    a refused frame demonstrably consumes nothing.
    """

    def __init__(self, *, session_id, session_epoch, calibration_bundle_hash: str,
                 windows: FreshnessWindow, channel_validator: Callable[[str, dict], Any],
                 max_ttl_ms: int = 200, clock_ns: Callable[[], int] = monotonic_ns,
                 data_identity: str = "simulated_offline", stream_epoch=None):
        self.session_id = session_id
        self.session_epoch = session_epoch
        self.calibration_bundle_hash = calibration_bundle_hash
        self.windows = windows
        self.max_ttl_ms = _int(max_ttl_ms, "max_ttl_ms", minimum=1)
        self._clock = clock_ns
        self._validate_channel = channel_validator
        self.data_identity = data_identity
        self.stream_epoch = stream_epoch
        self._next_frame_seq = 0
        self._last_frame_seq: int | None = None
        self._channel_seq: dict[str, int] = {}
        self._pending: dict[str, Any] | None = None   # latest-wins, bounded to one
        self.counters = {"received": 0, "accepted": 0, "refused": 0, "expired_in_queue": 0}
        self.refusals: dict[str, int] = {}

    def bind_stream_epoch(self, epoch) -> None:
        """Attach to a connection generation, dropping everything before it.

        A reconnect must re-align and re-engage explicitly, so this clears the
        buffered frame and the sequence expectations: sequences are keyed by
        generation on both sides, and a packet that sat in a socket buffer cannot
        become valid again by waiting for a new connection.
        """
        epoch = _identity(epoch, "stream_epoch")
        if epoch == self.stream_epoch:
            return
        self.stream_epoch = epoch
        self._pending = None
        self._next_frame_seq = 0
        self._last_frame_seq = None
        self._channel_seq = {}

    # -- pure structural checks ------------------------------------------
    def _check_shape(self, frame: Any) -> dict[str, Any]:
        if not isinstance(frame, dict):
            raise FrameError("invalid_frame", "frame must be an object")
        missing = FRAME_FIELDS - set(frame)
        extra = set(frame) - FRAME_FIELDS
        if missing or extra:
            raise FrameError("invalid_frame",
                             f"frame fields wrong, missing={sorted(missing)} unexpected={sorted(extra)}")
        if frame["frame_protocol_version"] != FRAME_PROTOCOL_VERSION:
            raise FrameError("frame_version",
                             f"expected protocol {FRAME_PROTOCOL_VERSION}, got {frame['frame_protocol_version']}")
        for name in ("session_id", "session_epoch", "calibration_bundle_hash"):
            value = _identity(frame[name], name)
            if value != getattr(self, name):
                raise FrameError("identity_mismatch", f"{name} does not belong to this epoch")
        commands = frame["commands"]
        if not isinstance(commands, dict) or set(commands) != set(COMMAND_FIELDS):
            raise FrameError("incomplete_frame",
                             f"both {' and '.join(COMMAND_FIELDS)} are required in every frame")
        frame_seq = _int(frame["frame_seq"], "frame_seq")
        _int(frame["sample_seq"], "sample_seq")
        _identity(frame["freshness_window_id"], "freshness_window_id")
        _identity(frame["stream_epoch"], "stream_epoch")
        if self.stream_epoch is not None and frame["stream_epoch"] != self.stream_epoch:
            raise FrameError("stream_epoch_retired",
                             f"this admission serves generation {self.stream_epoch}, the frame carries {frame['stream_epoch']}")
        if frame_seq < self._next_frame_seq:
            raise FrameError("frame_seq_replayed",
                             f"{frame_seq} is below the next expected {self._next_frame_seq}")
        return frame

    def _check_channels(self, frame: dict[str, Any]) -> dict[str, Any]:
        resolved = self.windows.resolve(frame["freshness_window_id"], frame["stream_epoch"])
        decoded: dict[str, Any] = {}
        for channel in FRAME_CHANNELS:
            command = frame["commands"][channel]
            if not isinstance(command, dict):
                raise FrameError("invalid_command", f"{channel} command must be an object")
            ttl_ms = _int(command.get("ttl_ms"), "ttl_ms", minimum=1)
            if ttl_ms > self.max_ttl_ms:
                raise FrameError("ttl_too_long", f"{channel} ttl_ms {ttl_ms} exceeds {self.max_ttl_ms}")
            seq = _int(command.get("seq"), "seq")
            expected = self._channel_seq.get(channel)
            if expected is not None and seq <= expected:
                raise FrameError("channel_seq_replayed",
                                 f"{channel} seq {seq} does not follow {expected}")
            try:
                # The host router and the car-side gate both refuse a bad command by
                # raising a ValueError subclass; that becomes a whole-frame refusal
                # with a wire-safe reason instead of leaking a side-specific type.
                outcome = self._validate_channel(channel, command)
            except FrameError:
                raise
            except (ValueError, RuntimeError, KeyError, TypeError) as error:
                # The host router raises a ValueError subclass and the car-side driver
                # a RuntimeError; a frame arriving off the wire can also be shaped
                # wrongly enough to raise KeyError.  All of them are a whole-frame
                # refusal with the class named, never a raw exception at the caller.
                raise FrameError("channel_refused",
                                 f"{channel}: {type(error).__name__}: {error}") from error
            if isinstance(outcome, dict) and outcome.get("accepted") is not True:
                raise FrameError(outcome.get("reason", "channel_refused"),
                                 f"{channel}: {outcome.get('detail', '')}")
            decoded[channel] = {
                "command": copy.deepcopy(command),
                "seq": seq,
                "ttl_ms": ttl_ms,
                "outcome": outcome,
            }
        return {"frame": frame, "resolved_window": resolved, "decoded": decoded}

    def _deadline_ns(self, candidate: dict[str, Any], received_ns: int, licence_expiry_ns: int) -> int:
        """Local deadline from the earliest of window, TTL and licence expiry."""
        ttl_ns = min(c["ttl_ms"] for c in candidate["decoded"].values()) * MS_TO_NS
        candidates = [candidate["resolved_window"]["deadline_ns"], received_ns + ttl_ns]
        if licence_expiry_ns is not None:
            candidates.append(licence_expiry_ns)
        return min(candidates)

    # -- public -----------------------------------------------------------
    def validate(self, frame: Any, *, licence_expiry_ns: int | None = None) -> dict[str, Any]:
        """Every check, no state change.  Raises FrameError on the first refusal."""
        checked = self._check_shape(frame)
        candidate = self._check_channels(checked)
        received_ns = self._clock()
        deadline_ns = self._deadline_ns(candidate, received_ns, licence_expiry_ns)
        if deadline_ns <= received_ns:
            raise FrameError("expired_on_arrival",
                             "the frame has no usable lifetime left on this clock")
        return {
            "valid": True,
            "frame": candidate["frame"],
            "decoded": candidate["decoded"],
            "received_ns": received_ns,
            "deadline_ns": deadline_ns,
            "window_id": candidate["resolved_window"]["window_id"],
            "window_remaining_ms": (
                candidate["resolved_window"]["deadline_ns"] - received_ns) / MS_TO_NS,
            # How old the named window already was on this clock when the frame
            # arrived.  Without it a refusal looks like a protocol failure when it
            # is really the sender tick plus the trip exceeding the window.
            "window_age_ms": (
                received_ns - candidate["resolved_window"]["issued_ns"]) / MS_TO_NS,
        }

    def accept(self, frame: Any, *, licence_expiry_ns: int | None = None) -> dict[str, Any]:
        """Validate, then commit: only now are sequences consumed and the queue set."""
        self.counters["received"] += 1
        try:
            accepted = self.validate(frame, licence_expiry_ns=licence_expiry_ns)
        except FrameError as error:
            self.counters["refused"] += 1
            self.refusals[error.reason] = self.refusals.get(error.reason, 0) + 1
            raise
        frame_obj = accepted["frame"]
        self._last_frame_seq = frame_obj["frame_seq"]
        self._next_frame_seq = frame_obj["frame_seq"] + 1
        for channel, detail in accepted["decoded"].items():
            self._channel_seq[channel] = detail["seq"]
        # latest-wins: at most one frame waits, and it is always the newest.
        self._pending = accepted
        self.counters["accepted"] += 1
        return {
            "accepted": True,
            "frame_seq": frame_obj["frame_seq"],
            "sample_seq": frame_obj["sample_seq"],
            "channel_seq": dict(accepted["decoded"]),
            "received_ns": accepted["received_ns"],
            "deadline_ns": accepted["deadline_ns"],
            "window_id": accepted["window_id"],
            "window_remaining_ms": accepted["window_remaining_ms"],
            "window_age_ms": accepted["window_age_ms"],
            "data_identity": self.data_identity,
        }

    def pending(self) -> dict[str, Any] | None:
        """The newest committed frame that is still inside its deadline, else None."""
        pending = self._pending
        if pending is None:
            return None
        if self._clock() >= pending["deadline_ns"]:
            self._pending = None
            self.counters["expired_in_queue"] += 1
            return None
        return pending

    def take(self) -> dict[str, Any] | None:
        pending = self.pending()
        self._pending = None
        return pending

    def clear(self, reason: str) -> None:
        """Disconnect, epoch change, stop or fault: nothing survives this."""
        self._pending = None
        self.clear_reason = reason

    def state(self) -> dict[str, Any]:
        pending = self._pending
        return {
            "session_epoch": self.session_epoch,
            "counters": dict(self.counters),
            "refusals": dict(self.refusals),
            "next_frame_seq": self._next_frame_seq,
            "last_frame_seq": self._last_frame_seq,
            "channel_seq": dict(self._channel_seq),
            "queue_depth": 0 if pending is None else 1,
            "queue_age_ms": 0.0 if pending is None else (self._clock() - pending["received_ns"]) / MS_TO_NS,
            "data_identity": self.data_identity,
        }


ARM_SERVO_IDS = {"joint1": 1, "joint2": 2, "joint3": 3, "joint4": 4, "joint5": 5}
GRIPPER_SERVO_ID = 10


def six_axis_targets(decoded: dict[str, Any]) -> list[dict[str, Any]]:
    """The six ordered (servo id, joint, radians) entries one frame resolves to.

    Radians stay radians: the radian -> board-tick conversion belongs to the output
    driver, which owns the calibration-backed mapping.  What this function fixes is
    the ordering that makes one frame one atomic six-axis write - joint1..joint5 then
    r_joint on id 10 - and the refusal of anything else.
    """
    arm = decoded["arm"]["command"]["target"]
    gripper = decoded["gripper"]["command"]["target"]
    if arm.get("joint_names") != list(ARM_SERVO_IDS):
        raise FrameError("joint_order", "arm joint order is not joint1..joint5")
    targets = [
        {"servo_id": ARM_SERVO_IDS[name], "joint_name": name, "position_rad": position}
        for name, position in zip(arm["joint_names"], arm["positions_rad"])
    ]
    targets.append({"servo_id": GRIPPER_SERVO_ID, "joint_name": gripper["joint_name"],
                    "position_rad": gripper["position_rad"]})
    return targets
