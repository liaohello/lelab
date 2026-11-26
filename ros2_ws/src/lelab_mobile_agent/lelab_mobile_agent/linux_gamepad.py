"""Read-only Linux JS reader for the LanderPi receiver. No ROS or motor I/O."""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import stat
import struct
import time
from collections import Counter
from pathlib import Path

RECEIVER = "usb-2563_USB_WirelessGamepad-joystick"
USB_ID = (0x0003, 0x2563, 0x0575)
JS_EVENT = struct.Struct("=IhBB")
FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC


class LinuxJoystick:
    """Pin receiver USB identity and port. A reconnect starts a new generation."""

    def __init__(self, port: str):
        if (not port.startswith(("platform-", "pci-")) or not port.endswith("-joystick")
                or "/" in port or port.endswith("-event-joystick")):
            raise ValueError("expected a /dev/input/by-path joystick basename")
        self.path = Path("/dev/input/by-path") / port
        self.identity_path = Path("/dev/input/by-id") / RECEIVER
        self.fd: int | None = None
        self.generation = 0
        self.hid_parent: Path | None = None
        self.axes: list[int | None] = []
        self.buttons: list[int | None] = []
        self.last_change_mono_ns: int | None = None
        self.last_error: str | None = None

    def _verify(self, fd: int):
        opened = os.fstat(fd)
        if not stat.S_ISCHR(opened.st_mode):
            raise OSError(errno.ENODEV, "not a character device")
        for path in (self.path, self.identity_path):
            current = path.stat()
            if ((current.st_dev, current.st_ino, current.st_rdev)
                    != (opened.st_dev, opened.st_ino, opened.st_rdev)):
                raise OSError(errno.ENODEV, "receiver path changed or receiver identity is ambiguous")
        device = Path(f"/sys/dev/char/{os.major(opened.st_rdev)}:{os.minor(opened.st_rdev)}/device")
        identity = tuple(int((device / "id" / name).read_text().strip(), 16)
                         for name in ("bustype", "vendor", "product"))
        if identity != USB_ID:
            raise OSError(errno.ENODEV, "unexpected receiver USB identity")
        return device.resolve(strict=True).parents[1]

    def _open(self):
        fd = os.open(self.path, FLAGS)
        try:
            hid_parent = self._verify(fd)
            axes = fcntl.ioctl(fd, 0x80016A11, bytes(1))[0]
            buttons = fcntl.ioctl(fd, 0x80016A12, bytes(1))[0]
            if not (3 <= axes <= 64 and buttons <= 64):
                raise OSError(errno.EINVAL, "unexpected joystick dimensions")
        except (OSError, ValueError):
            os.close(fd)
            raise
        self.fd = fd
        self.hid_parent = hid_parent
        self.generation += 1
        self.axes = [None] * axes
        self.buttons = [None] * buttons
        self.last_change_mono_ns = None
        self.last_error = None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.hid_parent = None
        self.axes = []
        self.buttons = []
        self.last_change_mono_ns = None

    def poll(self, now_ns: int):
        """Read a bounded batch. INIT events recover positions, never RF liveness."""
        events = []
        try:
            if self.fd is None:
                self._open()
            else:
                self._verify(self.fd)
            try:
                data = os.read(self.fd, 128 * JS_EVENT.size)
            except BlockingIOError:
                return events
            if not data or len(data) % JS_EVENT.size:
                raise OSError(errno.EIO, "invalid joystick event stream")
            for stamp, value, kind, index in JS_EVENT.iter_unpack(data):
                initial = bool(kind & 0x80)
                kind &= 0x7f
                if kind == 2:
                    name, state = "axis", self.axes
                elif kind == 1 and value in (0, 1):
                    name, state = "button", self.buttons
                else:
                    raise OSError(errno.EIO, "invalid joystick event type")
                if index >= len(state):
                    raise OSError(errno.EIO, "invalid joystick index")
                state[index] = value
                if not initial:
                    self.last_change_mono_ns = now_ns
                events.append({"target": name, "index": index, "value": value,
                               "initial": initial, "kernel_ms": stamp})
        except (OSError, ValueError) as exc:
            self.close()
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        return events

    def status(self):
        return {
            "connected": self.fd is not None, "generation": self.generation,
            "receiver": "2563:0575", "port": self.path.name,
            "axes_raw": list(self.axes), "buttons_raw": list(self.buttons),
            "last_js_change_mono_ns": self.last_change_mono_ns,
            "rf_liveness": "unverified", "rf_sample_mono_ns": None,
            "error": self.last_error,
        }


def open_hid(parent: Path):
    matches = [node for node in Path("/sys/class/hidraw").glob("hidraw*")
               if (node / "device").resolve() == parent]
    if len(matches) != 1:
        raise OSError(errno.ENODEV, "expected one hidraw device on the selected receiver interface")
    fd = os.open(Path("/dev") / matches[0].name, FLAGS)
    opened = os.fstat(fd)
    device = Path(f"/sys/dev/char/{os.major(opened.st_rdev)}:{os.minor(opened.st_rdev)}/device")
    if device.resolve() != parent:
        os.close(fd)
        raise OSError(errno.ENODEV, "hidraw identity changed")
    return fd


def main():
    parser = argparse.ArgumentParser(description="Read-only gamepad probe, JSONL on stdout")
    parser.add_argument("--port", required=True, help="verified /dev/input/by-path joystick basename")
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--usb-reports", action="store_true", help="also read the matching hidraw interface")
    args = parser.parse_args()
    reader = LinuxJoystick(args.port)
    start = time.monotonic_ns()
    until = start + int(args.seconds * 1e9)
    next_usb = start + 1_000_000_000
    reports = Counter()
    hid_fd = None
    hid_generation = None
    previous = None

    def log(kind, now, **fields):
        print(json.dumps({"kind": kind, "elapsed_s": round((now - start) / 1e9, 6),
                          "mono_ns": now, **fields}), flush=True)

    try:
        while (now := time.monotonic_ns()) < until:
            events = reader.poll(now)
            status = reader.status()
            identity = (status["connected"], status["generation"], status["error"])
            if identity != previous:
                log("device", now, **status)
                previous = identity
            for event in events:
                log("input", now, generation=reader.generation, **event)
            if hid_fd is not None and (reader.hid_parent is None or hid_generation != reader.generation):
                log("hid_closed", now, generation=hid_generation, reports=sum(reports.values()),
                    states=dict(reports))
                os.close(hid_fd)
                hid_fd = None
                reports.clear()
            if args.usb_reports and reader.hid_parent is not None and hid_generation != reader.generation:
                hid_generation = reader.generation
                try:
                    hid_fd = open_hid(reader.hid_parent)
                except OSError as exc:
                    log("hid_error", now, error=str(exc))
            if hid_fd is not None:
                try:
                    for _ in range(32):
                        data = os.read(hid_fd, 512)
                        if not data:
                            raise OSError(errno.EIO, "hidraw EOF")
                        reports[data.hex()] += 1
                except BlockingIOError:
                    pass
                except OSError as exc:
                    log("hid_error", now, error=str(exc))
                    os.close(hid_fd)
                    hid_fd = None
            if args.usb_reports and now >= next_usb:
                log("hid_interval", now, generation=hid_generation, reports=sum(reports.values()),
                    states=dict(reports), hid_open=hid_fd is not None, rf_liveness="unverified")
                reports.clear()
                next_usb += 1_000_000_000
            time.sleep(0.015)
    finally:
        if hid_fd is not None:
            os.close(hid_fd)
        reader.close()


if __name__ == "__main__":
    main()
