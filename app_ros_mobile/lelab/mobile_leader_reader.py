"""Read-only SO-101 leader arm reader for the mobile teleop session.

The reader claims the leader serial port (flock + TIOCEXCL) and only ever issues reads: no torque
enable/disable, no calibration writes, no operating-mode changes, no ID reassignment. The bus
is injected so the semantics are testable without hardware; the default factory uses the local
customized LeRobot Feetech bus.
"""
from __future__ import annotations

import fcntl
import math
import os
import termios
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

LEADER_JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]
LEADER_GRIPPER = "gripper"
DEG_TO_RAD = math.pi / 180.0


@dataclass
class LeaderConfig:
    port: str
    ids: dict[str, int] = field(default_factory=lambda: {
        "shoulder_pan": 1, "shoulder_lift": 2, "elbow_flex": 3,
        "wrist_flex": 4, "wrist_roll": 5, "gripper": 6,
    })
    read_hz: float = 30.0
    motor_model: str = "sts3215"
    read_timeout_s: float = 0.35
    num_read_retries: int = 2
    stale_after_s: float = 0.5
    unhealthy_after_failures: int = 3

    def as_dict(self) -> dict[str, Any]:
        return {
            "port": self.port, "ids": dict(self.ids), "read_hz": self.read_hz,
            "read_timeout_s": self.read_timeout_s, "num_read_retries": self.num_read_retries,
            "stale_after_s": self.stale_after_s,
            "unhealthy_after_failures": self.unhealthy_after_failures,
        }


CALIBRATION_FIELDS = ("id", "drive_mode", "homing_offset", "range_min", "range_max")


def build_bus(config: LeaderConfig, calibration: dict[str, Any] | None = None):
    """Create a Feetech bus for reading only; no writer is ever called.

    Normalisation is what makes the leader readings directly usable: arm joints come out in
    degrees and the gripper in 0-100 opening percent, matching layer A's declared units. That
    needs the leader's real LeRobot calibration, so an absent or partial one is refused here
    rather than silently read as raw ticks.
    """
    from lerobot.motors.feetech.feetech import FeetechMotorsBus  # type: ignore
    from lerobot.motors.motors_bus import Motor, MotorNormMode  # type: ignore

    if not calibration:
        raise ValueError("leader calibration missing: layer A must carry the existing "
                         "SO-101 lerobot_calibration block")
    incomplete = {name: [f for f in CALIBRATION_FIELDS if f not in data]
                  for name, data in calibration.items()
                  if isinstance(data, dict) and any(f not in data for f in CALIBRATION_FIELDS)}
    if incomplete:
        raise ValueError(f"leader calibration incomplete for {sorted(incomplete)}: "
                         "needs id/drive_mode/homing_offset/range_min/range_max")
    missing = [name for name in (*LEADER_JOINTS, LEADER_GRIPPER) if name not in calibration]
    if missing:
        raise ValueError(f"leader calibration has no entry for {missing}")

    defs = {name: Motor(id=config.ids[name], model=config.motor_model,
                        norm_mode=(MotorNormMode.RANGE_0_100 if name == LEADER_GRIPPER
                                   else MotorNormMode.DEGREES))
            for name in (*LEADER_JOINTS, LEADER_GRIPPER)}
    from lerobot.motors.motors_bus import MotorCalibration  # type: ignore
    calib = {name: MotorCalibration(**{k: calibration[name][k] for k in CALIBRATION_FIELDS})
             for name in defs}
    return FeetechMotorsBus(port=config.port, motors=defs, calibration=calib)


def calibration_id_mismatches(calibration: dict[str, Any] | None,
                              ids: dict[str, int]) -> list[str]:
    """Name every disagreement between a leader calibration and the bus ids it will drive.

    Presence of the five fields is not enough: the same complete-looking calibration applied to a
    different arm yields smooth, plausible, wrong angles. The mobile wizard reads whichever arm is
    plugged in, so the calibration chosen in layer A has to agree motor-by-motor with the ids the
    bus will actually poll.
    """
    if not calibration:
        return ["leader_calibration_missing"]
    known = (*LEADER_JOINTS, LEADER_GRIPPER)
    out: list[str] = []
    for name in known:
        entry = calibration.get(name)
        if not isinstance(entry, dict) or entry.get("id") is None:
            out.append(f"{name}:no_id")
            continue
        wanted = ids.get(name)
        if wanted is None:
            out.append(f"{name}:not_polled_by_bus")
        elif int(entry["id"]) != int(wanted):
            out.append(f"{name}:calibration_id={entry['id']}!=bus_id={wanted}")
    out.extend(f"{name}:unexpected_motor_in_calibration"
               for name in sorted(set(calibration) - set(known)))
    return out


def list_leader_calibrations(base: str | None = None) -> dict[str, Any]:
    """Read-only inventory of the calibrations the existing SO-101 wizard already produced.

    Layer A has to reuse those numbers rather than have the operator retype six motors by five
    fields from a terminal, so the page is given the real files on this host. Nothing here opens a
    port and nothing is ever written back into LeRobot's calibration tree.
    """
    import hashlib
    import json

    if base is None:
        from .utils.config import CALIBRATION_BASE_PATH_TELEOP
        base = CALIBRATION_BASE_PATH_TELEOP
    root = Path(base)
    entries: list[dict[str, Any]] = []
    if root.is_dir():
        for rig_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for path in sorted(rig_dir.glob("*.json")):
                raw = path.read_bytes()
                entry: dict[str, Any] = {
                    "robot_type": rig_dir.name, "name": path.stem, "path": str(path),
                    "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()[:16],
                }
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    entries.append({**entry, "usable": False, "reason": f"unparsable:{type(exc).__name__}"})
                    continue
                if not isinstance(data, dict):
                    entries.append({**entry, "usable": False, "reason": "not_a_mapping"})
                    continue
                incomplete = {name: [f for f in CALIBRATION_FIELDS if f not in body]
                              for name, body in data.items()
                              if isinstance(body, dict) and any(f not in body for f in CALIBRATION_FIELDS)}
                absent = [name for name in (*LEADER_JOINTS, LEADER_GRIPPER) if name not in data]
                usable = not absent and not incomplete
                entry.update({
                    "motors": {name: body.get("id") for name, body in data.items()
                               if isinstance(body, dict)},
                    "missing_motors": absent, "incomplete_motors": incomplete, "usable": usable,
                })
                if usable:
                    # Handing the block back is what lets layer A reuse the real numbers; the page
                    # fills them instead of the operator retyping six motors by five fields.
                    entry["calibration"] = data
                entries.append(entry)
    return {"root": str(root), "exists": root.is_dir(), "entries": entries,
            "note": "read from this host only; the mobile wizard never writes into this tree"}


def list_leader_ports(base: str = "/dev/serial/by-id") -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    root = Path(base)
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        name = entry.name
        if "USB_Single_Serial" not in name:
            continue
        serial = name.split("_")[-1].split("-")[0]
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        out.append({"by_id": str(entry), "device": os.path.basename(target), "serial": serial})
    return out


class SO101LeaderReader:
    """Poll the leader bus in a background thread and expose the newest measurement only."""

    def __init__(self, config: LeaderConfig, bus: Any | None = None,
                 bus_factory: Callable[[LeaderConfig], Any] | None = None):
        self.config = config
        self._bus = bus
        self._bus_factory = bus_factory or build_bus
        self.lock = threading.Lock()
        self.connected = False
        self.port_claim = "not_opened"
        self.reads = 0
        self.failures = 0
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self.last_wall: float | None = None
        self.positions_deg: dict[str, float] = {}
        self.gripper_percent: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle
    def _claim_port(self) -> str:
        """Ask the OS for exclusive use of the serial port before any traffic is sent.

        `flock` blocks other cooperating processes and `TIOCEXCL` makes further O_EXCL opens fail;
        a process that neither flock()s nor uses O_EXCL can still open the node, which is why the
        upper-host hardware lease remains the authoritative gate.
        """
        handler = getattr(self._bus, "port_handler", None) or getattr(self._bus, "serial", None)
        for candidate in (getattr(handler, "ser", None), getattr(handler, "serobj", None),
                          handler, getattr(self._bus, "serial", None)):
            if getattr(candidate, "fileno", None) is None:
                continue
            return self._claim_fd(candidate.fileno())
        return "unverified:no_fileno"

    def _claim_fd(self, fd: int) -> str:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"leader_serial_busy: {exc}") from exc
        try:
            fcntl.ioctl(fd, termios.TIOCEXCL)
        except OSError as exc:
            self.last_error = f"tioxcl_unavailable:{type(exc).__name__}: {exc}"
            return "flock_only"
        return "flock+tioxcl"

    def open(self) -> dict[str, Any]:
        if self._bus is None:
            self._bus = self._bus_factory(self.config)
        try:
            self._bus.connect()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(f"leader_connect_failed:{self.last_error}") from exc
        self.port_claim = self._claim_port()
        if self.port_claim == "unverified:no_fileno":
            self.last_error = "leader_port_claim_unverified: the bus exposes no fd"
        self.connected = True
        return self.status()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="so101-leader-reader")
        self._thread.start()

    def close(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._bus is not None:
            try:
                # LeRobot defaults to writing torque-off on disconnect; a passive observer must
                # never do that, or closing the reader could drop a loaded arm.
                self._bus.disconnect(disable_torque=False)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"disconnect:{type(exc).__name__}: {exc}"
        with self.lock:
            self.connected = False
            self.positions_deg.clear()
            self.gripper_percent = None
            self.last_wall = None
        return {"closed": True, "reason": "user"}

    # ------------------------------------------------------------------ polling
    def _loop(self) -> None:
        period = 1.0 / max(1.0, self.config.read_hz)
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(period)

    def poll_once(self) -> dict[str, Any]:
        if not self.connected or self._bus is None:
            return self.status()
        try:
            values = self._bus.sync_read(
                "Present_Position", list(self.config.ids), num_retry=self.config.num_read_retries)
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                self.failures += 1
                self.consecutive_failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
            if self.consecutive_failures >= self.config.unhealthy_after_failures:
                self.connected = False
            return self.status()
        now = time.monotonic()
        # Only values in this poll are current; never carry the gripper over from a prior poll.
        def finite_value(name: str) -> float | None:
            try:
                value = float(values[name])
            except (KeyError, TypeError, ValueError):
                return None
            return value if math.isfinite(value) else None

        current = {name: value for name in LEADER_JOINTS
                   if (value := finite_value(name)) is not None}
        gripper = finite_value(LEADER_GRIPPER)
        with self.lock:
            self.reads += 1
            self.consecutive_failures = 0
            self.last_error = None
            self.last_wall = now
            self.positions_deg = current
            self.gripper_percent = gripper
        return self.status()

    # ------------------------------------------------------------------ reporting
    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        with self.lock:
            age = None if self.last_wall is None else round(now - self.last_wall, 4)
            stale = age is None or age > self.config.stale_after_s
            missing = [j for j in LEADER_JOINTS if j not in self.positions_deg]
            if self.gripper_percent is None:
                missing.append(LEADER_GRIPPER)
            return {
                "connected": self.connected,
                "port_claim": self.port_claim,
                "healthy": self.connected and not stale and not missing and self.consecutive_failures == 0,
                "reason": self._reason(stale, age, missing),
                "positions_deg": dict(self.positions_deg),
                "positions_rad": {k: round(v * DEG_TO_RAD, 6) for k, v in self.positions_deg.items()},
                "gripper_percent": self.gripper_percent,
                "age_s": age,
                "reads": self.reads,
                "failures": self.failures,
                "consecutive_failures": self.consecutive_failures,
                "last_error": self.last_error,
                "source": f"leader:{os.path.basename(self.config.port)}",
                "units": {"joints": "deg→rad (deg from Feetech bus with use_degrees)",
                          "gripper": "0-100 opening percent, not an angle"},
                "expected_joints": LEADER_JOINTS + [LEADER_GRIPPER],
                "missing_joints": missing,
                "config": self.config.as_dict(),
            }

    def _reason(self, stale: bool, age: float | None, missing: list[str]) -> str:
        if not self.connected:
            return "leader_unreachable"
        if not self.positions_deg:
            return "no_read_yet"
        if stale:
            return f"leader_read_stale:{age}"
        if self.consecutive_failures:
            return f"leader_read_degraded:{self.consecutive_failures}"
        if missing:
            return f"leader_read_incomplete:{','.join(missing)}"
        return "ok"

    def status(self) -> dict[str, Any]:
        return self.snapshot()
