"""Bounded bus-servo response logic for injection into the existing serial owner.

No port is opened here. send must be a bounded, serialized write supplied by the owner.
The legacy wire protocol has no transaction identifier; a fault latches the channel.
"""
from __future__ import annotations

import math
import queue
import struct
import threading
import time
from typing import Callable


class ProtocolFault(RuntimeError):
    pass


class ServoTransactions:
    def __init__(self, send: Callable[[int, int], None], *, timeout_s: float,
                 clock: Callable[[], float] = time.monotonic):
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= 1:
            raise ValueError("timeout_s must be in (0, 1]")
        self.send, self.timeout_s, self.clock = send, timeout_s, clock
        self._serial = threading.Lock()
        self._state = threading.Lock()
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=1)
        self._inflight = False
        self.poisoned = False

    def receive(self, data: bytes) -> None:
        """Called only by the owner's CRC-checked BUS_SERVO parser."""
        with self._state:
            if not self._inflight or self.poisoned:
                self.poisoned = True
                return
            try:
                self._queue.put_nowait(bytes(data))
            except queue.Full:
                self.poisoned = True

    def read(self, servo_id: int, subcommand: int, fmt: str, *,
             cancel: threading.Event | None = None) -> tuple:
        if (isinstance(servo_id, bool) or not isinstance(servo_id, int) or not 0 <= servo_id <= 253
                or isinstance(subcommand, bool) or not isinstance(subcommand, int) or not 0 <= subcommand <= 255):
            raise ValueError("invalid id or subcommand")
        if fmt not in ("<BBbh", "<BBbB", "<BBbb", "<BBbH", "<BBb2H"):
            raise ValueError("unsupported reply format")
        # A caller cannot wait indefinitely for another transaction.
        if not self._serial.acquire(timeout=self.timeout_s):
            raise ProtocolFault("transaction_busy")
        try:
            with self._state:
                if self.poisoned or not self._queue.empty():
                    self.poisoned = True
                    raise ProtocolFault("channel_latched")
                self._inflight = True
            deadline = self.clock() + self.timeout_s
            try:
                if cancel is not None and cancel.is_set():
                    raise ProtocolFault("cancelled")
                self.send(subcommand, servo_id)
                while True:
                    if cancel is not None and cancel.is_set():
                        raise ProtocolFault("cancelled")
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise ProtocolFault("timeout")
                    try:
                        frame = self._queue.get(timeout=min(remaining, 0.01))
                        break
                    except queue.Empty:
                        continue
                if len(frame) != struct.calcsize(fmt):
                    raise ProtocolFault("wrong_length")
                rid, command, error, *values = struct.unpack(fmt, frame)
                if rid != servo_id or command != subcommand:
                    raise ProtocolFault("wrong_id_or_subcommand")
                if error != 0:
                    raise ProtocolFault("controller_error")
                with self._state:
                    if self.poisoned or not self._queue.empty():
                        raise ProtocolFault("duplicate_frame")
                    self._inflight = False
                return tuple(values)
            except BaseException:
                with self._state:
                    self.poisoned = True
                    self._inflight = False
                raise
        finally:
            self._serial.release()
