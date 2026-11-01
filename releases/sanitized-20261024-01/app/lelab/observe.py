"""Read-only single-arm observation for the LeLab web UI.

The arm bus is opened directly through ``ReadOnlyFollowerSource``; the
SO follower ``connect()/configure()`` path is never used.  The loop broadcasts
the same ``joint_update`` WebSocket message as recording/teleoperation and
serves the latest camera JPEGs, but it has no action sink and always reports
``actions_sent=0``.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from pydantic import BaseModel, Field, model_validator

from .jetson_policy import (
    JetsonPolicyError,
    PolicyContract,
    ReadOnlyFollowerSource,
)
from .rig import LEFT_JOIN_NAMES, RIGHT_JOIN_NAMES

logger = logging.getLogger(__name__)

DEFAULT_CONTRACT_DIR = str(
    Path.home() / ".local/share/lelab-jetson-20261031/checkpoints"
)
DEFAULT_CALIBRATION = str(
    Path.home() / ".local/share/lelab-jetson-20261031/calibration/my_robot.json"
)
DEFAULT_PORT = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B91045575-if00"


class ObserveRequest(BaseModel):
    port: str = DEFAULT_PORT
    calibration: str = DEFAULT_CALIBRATION
    contract_dir: str = DEFAULT_CONTRACT_DIR
    cameras: dict[str, dict[str, Any]] = Field(
        default_factory=lambda: {
            "right": {"index": 0, "width": 640, "height": 480, "fps": 30},
            "up": {"index": 2, "width": 640, "height": 480, "fps": 30},
        }
    )
    fps: float = 5.0


class BimanualObserveRequest(BaseModel):
    left_port: str
    left_calibration: str | None = None
    right_port: str
    right_calibration: str | None = None
    cameras: dict[str, dict[str, Any]] = Field(default_factory=dict)
    fps: float = 5.0

    @model_validator(mode="after")
    def _distinct_ports(self) -> BimanualObserveRequest:
        left = Path(self.left_port).expanduser()
        right = Path(self.right_port).expanduser()
        if not str(left).strip() or not str(right).strip():
            raise ValueError("both follower ports are required")
        if left == right:
            raise ValueError("left and right followers must use different serial ports")
        return self


_lock = threading.RLock()
_session: dict[str, Any] | None = None


def observe_active() -> bool:
    with _lock:
        return bool(_session and _session.get("active"))


def _camera_specs(cameras: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for key, config in cameras.items():
        raw_index = config.get("index", config.get("index_or_path"))
        if raw_index is None:
            raise JetsonPolicyError(f"camera {key!r} has no index/index_or_path")
        specs.append(
            {
                "key": key,
                "index": int(raw_index),
                "width": int(config.get("width", 640)),
                "height": int(config.get("height", 480)),
                "fps": int(config.get("fps", 30)),
            }
        )
    return specs


def _session_status() -> dict[str, Any]:
    with _lock:
        if _session is None:
            return {
                "active": False,
                "phase": "idle",
                "fault": None,
                "frames": 0,
                "state": {},
                "camera_ages_ms": {},
                "torque": {},
                "actions_sent": 0,
                "mode": "observe",
            }
        status: dict[str, Any] = {
            "active": bool(_session.get("active")),
            "phase": _session.get("phase"),
            "fault": _session.get("fault"),
            "frames": _session.get("frames", 0),
            "state": dict(_session.get("state") or {}),
            "camera_ages_ms": dict(_session.get("camera_ages_ms") or {}),
            "torque": dict(_session.get("torque") or {}),
            "actions_sent": 0,
            "mode": _session.get("mode") or "observe",
            "started_mono": _session.get("started_mono"),
        }
        if status["mode"] == "bimanual":
            status["arms"] = dict(_session.get("arms") or {})
            status["bindings"] = dict(_session.get("arms_summary") or {})
        else:
            status["port"] = _session.get("port")
            status["calibration"] = _session.get("calibration")
        return status


def handle_start_observe(request: ObserveRequest, manager: Any) -> dict[str, Any]:
    global _session
    from . import (
        record as _record,
        remote_inference as _remote,
        rollout as _rollout,
        teleoperate as _teleoperate,
    )

    with _lock:
        if _session is not None and _session.get("active"):
            return {
                "success": False,
                "status_code": 409,
                "message": "Read-only observation is already active",
            }
        for name, active in (
            ("recording", _record.recording_active),
            ("teleoperation", _teleoperate.teleoperation_active),
            ("inference", _rollout.inference_active or _remote.remote_inference_active()),
        ):
            if active:
                return {
                    "success": False,
                    "status_code": 409,
                    "message": f"{name} is active; stop it before observation",
                }

    try:
        contract = PolicyContract.from_checkpoint(request.contract_dir)
        source = ReadOnlyFollowerSource(
            request.port,
            request.calibration,
            contract,
            cameras=_camera_specs(request.cameras),
        )
        source.start()
    except Exception as exc:  # noqa: BLE001
        logger.exception("read-only observation failed to open")
        return {
            "success": False,
            "status_code": 503,
            "message": f"Could not open read-only observation: {exc}",
        }

    stop_event = threading.Event()
    with _lock:
        _session = {
            "active": True,
            "phase": "observing",
            "mode": "observe",
            "fault": None,
            "frames": 0,
            "state": {},
            "camera_ages_ms": {},
            "frames_jpeg": {},
            "torque": source.torque_state(),
            "port": request.port,
            "calibration": request.calibration,
            "started_mono": time.monotonic(),
            "stop": stop_event,
            "source": source,
        }

    def worker() -> None:
        period = 1.0 / max(float(request.fps), 0.1)
        try:
            while not stop_event.is_set():
                started = time.monotonic()
                try:
                    observation = source.capture()
                    contract.validate_observation(observation)
                    state = {
                        name: float(observation[name]) for name in contract.state_names
                    }
                    ages = source.camera_ages_ms()
                    frames_jpeg: dict[str, bytes] = {}
                    for key in contract.camera_keys:
                        ok, buffer = cv2.imencode(
                            ".jpg",
                            np.asarray(observation[key]),
                            [cv2.IMWRITE_JPEG_QUALITY, 80],
                        )
                        if ok:
                            frames_jpeg[key] = buffer.tobytes()
                    with _lock:
                        if _session is None:
                            return
                        _session["frames"] += 1
                        _session["state"] = state
                        _session["camera_ages_ms"] = ages
                        _session["frames_jpeg"] = frames_jpeg
                    if manager is not None:
                        manager.broadcast_joint_data_sync(
                            {
                                "type": "joint_update",
                                "mode": "single",
                                "source": "observe_only",
                                "joints": state,
                                "actions_sent": 0,
                                "timestamp": time.time(),
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("read-only observation loop failed")
                    with _lock:
                        if _session is not None:
                            _session["fault"] = f"{type(exc).__name__}: {exc}"
                            _session["phase"] = "fault"
                            _session["active"] = False
                    stop_event.set()
                    return
                time.sleep(max(0.0, period - (time.monotonic() - started)))
        finally:
            with _lock:
                if _session is not None and _session.get("source") is source:
                    _session["active"] = False
                    if _session.get("phase") != "fault":
                        _session["phase"] = "stopped"
            source.close()

    thread = threading.Thread(
        target=worker, name="read-only-observer", daemon=True
    )
    with _lock:
        if _session is not None:
            _session["thread"] = thread
    thread.start()
    logger.info(
        "read-only observation started port=%s calibration=%s cameras=%s",
        request.port,
        request.calibration,
        list(request.cameras),
    )
    return {"success": True, "status": _session_status()}


def handle_stop_observe() -> dict[str, Any]:
    global _session
    with _lock:
        session = _session
        if session is None or not session.get("active"):
            if session is not None:
                _session = None
            return {"success": True, "already_stopped": True, "status": _session_status()}
        session["active"] = False
        session["phase"] = "stopping"
        stop_event = session.get("stop")
        thread = session.get("thread")
    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=5.0)
    with _lock:
        if _session is not None:
            _session["active"] = False
            if _session.get("phase") != "fault":
                _session["phase"] = "stopped"
    status = _session_status()
    return {"success": True, "already_stopped": False, "status": status}


def handle_observe_status() -> dict[str, Any]:
    return _session_status()


def handle_bimanual_observe_status() -> dict[str, Any]:
    """Return whichever read-only observe session is active (single or bimanual)."""
    return _session_status()


def camera_frame(key: str) -> bytes | None:
    with _lock:
        if _session is None:
            return None
        return (_session.get("frames_jpeg") or {}).get(key)


def reset_observe_state() -> None:
    """Test helper: stop and forget any observer without touching hardware."""
    global _session
    with _lock:
        session = _session
        _session = None
    if session is not None:
        event = session.get("stop")
        if event is not None:
            event.set()


class _ReadOnlyFollowerArm:
    """One SO-101 follower bus with every motor-writing method guarded."""

    DEFAULT_MOTOR_IDS = {
        "shoulder_pan": 1,
        "shoulder_lift": 2,
        "elbow_flex": 3,
        "wrist_flex": 4,
        "wrist_roll": 5,
        "gripper": 6,
    }

    def __init__(
        self,
        port: str,
        calibration_path: str | None,
        *,
        bus_factory: Any | None = None,
    ):
        self.port = port
        self.calibration_path = (
            Path(calibration_path).expanduser() if calibration_path else None
        )
        self._bus_factory = bus_factory
        self._bus: Any = None
        self._writes: list[str] = []
        self._lock = threading.Lock()

    @property
    def calibrated(self) -> bool:
        return self.calibration_path is not None

    def _build_bus(self) -> Any:
        if self._bus_factory is not None:
            bus = self._bus_factory(self.port, self.calibration_path)
        else:
            from lerobot.motors import Motor, MotorCalibration, MotorNormMode
            from lerobot.motors.feetech import FeetechMotorsBus

            raw: dict[str, Any] = {}
            if self.calibration_path is not None:
                raw = json.loads(self.calibration_path.read_text(encoding="utf-8"))
                missing = [
                    name for name in self.DEFAULT_MOTOR_IDS if name not in raw
                ]
                if missing:
                    raise JetsonPolicyError(
                        f"calibration {self.calibration_path} is missing {missing}"
                    )

            motors = {
                name: Motor(
                    int((raw.get(name) or {}).get("id", motor_id)),
                    "sts3215",
                    MotorNormMode.RANGE_0_100
                    if name == "gripper"
                    else MotorNormMode.DEGREES,
                )
                for name, motor_id in self.DEFAULT_MOTOR_IDS.items()
            }
            calibration = (
                {
                    name: MotorCalibration(
                        id=int(entry["id"]),
                        drive_mode=int(entry.get("drive_mode", 0)),
                        homing_offset=int(entry["homing_offset"]),
                        range_min=int(entry["range_min"]),
                        range_max=int(entry["range_max"]),
                    )
                    for name, entry in raw.items()
                }
                if raw
                else {}
            )
            bus = FeetechMotorsBus(
                port=self.port, motors=motors, calibration=calibration
            )

        for name in ReadOnlyFollowerSource.FORBIDDEN:
            if hasattr(bus, name):
                setattr(bus, name, self._forbidden(name))
        return bus

    def _forbidden(self, name: str):
        def guard(*args: Any, **kwargs: Any) -> Any:
            self._writes.append(name)
            raise JetsonPolicyError(
                f"read-only follower attempted motor write: {name}"
            )

        return guard

    def start(self) -> None:
        if self._bus is not None and getattr(self._bus, "is_connected", False):
            return
        self._bus = self._build_bus()
        self._bus.connect()

    def read(self) -> dict[str, float]:
        if self._bus is None:
            raise JetsonPolicyError("read-only follower arm is not started")
        with self._lock:
            # Feetech can only convert encoder steps into engineering units with
            # a registered calibration.  An uncalibrated read-only arm therefore
            # reports raw 12-bit positions and remains labelled as such.
            values = self._bus.sync_read(
                "Present_Position",
                normalize=self.calibrated,
            )
        missing = [name for name in self.DEFAULT_MOTOR_IDS if name not in values]
        if missing:
            raise JetsonPolicyError(f"follower bus did not return {missing}")
        return {name: float(values[name]) for name in self.DEFAULT_MOTOR_IDS}

    def torque_state(self) -> dict[str, int]:
        if self._bus is None:
            return {}
        with self._lock:
            return {
                motor: int(self._bus.read("Torque_Enable", motor, normalize=False))
                for motor in self._bus.motors
            }

    def close(self) -> None:
        if self._bus is None:
            return
        try:
            self._bus.disconnect(disable_torque=False)
        except Exception:  # noqa: BLE001
            logger.exception("read-only follower arm failed to close port=%s", self.port)
        finally:
            self._bus = None

    def summary(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "calibration": str(self.calibration_path) if self.calibration_path else None,
            "calibrated": self.calibrated,
            "unit": "degrees" if self.calibrated else "raw_steps",
        }


class ReadOnlyBimanualFollowerSource:
    """Read two follower buses and shared cameras without writing motors."""

    def __init__(
        self,
        left_port: str,
        right_port: str,
        *,
        left_calibration: str | None = None,
        right_calibration: str | None = None,
        cameras: list[dict[str, Any]] | None = None,
        bus_factory: Any | None = None,
        camera_factory: Any | None = None,
    ):
        if not left_port.strip() or not right_port.strip():
            raise JetsonPolicyError("both follower ports are required")
        self.arms = {
            "left": _ReadOnlyFollowerArm(
                left_port, left_calibration, bus_factory=bus_factory
            ),
            "right": _ReadOnlyFollowerArm(
                right_port, right_calibration, bus_factory=bus_factory
            ),
        }
        self.cameras = cameras or []
        self._camera_factory = camera_factory
        self._captures: dict[str, Any] = {}
        self._capture_mono: dict[str, float] = {}

    def start(self) -> None:
        started: list[str] = []
        try:
            for side in ("left", "right"):
                self.arms[side].start()
                started.append(side)
            for spec in self.cameras:
                key = str(spec["key"])
                self._captures[key] = self._build_camera(spec)
                for _ in range(5):
                    self._captures[key].read()
        except Exception:
            for side in reversed(started):
                self.arms[side].close()
            for capture in self._captures.values():
                try:
                    capture.release()
                except Exception:  # noqa: BLE001
                    logger.debug("failed to release camera during bimanual rollback")
            self._captures.clear()
            raise

    def _build_camera(self, spec: dict[str, Any]) -> Any:
        if self._camera_factory is not None:
            return self._camera_factory(spec)
        import cv2

        capture = cv2.VideoCapture(int(spec["index"]))
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(spec.get("width", 640)))
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(spec.get("height", 480)))
        capture.set(cv2.CAP_PROP_FPS, int(spec.get("fps", 30)))
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not capture.isOpened():
            raise JetsonPolicyError(
                f"camera {spec.get('key')} index {spec.get('index')} did not open"
            )
        return capture

    def capture(self) -> dict[str, Any]:
        observation: dict[str, Any] = {}
        for side in ("left", "right"):
            for motor, value in self.arms[side].read().items():
                observation[f"{side}_{motor}.pos"] = value
        now = time.monotonic()
        for key, capture in self._captures.items():
            ok, frame = capture.read()
            if not ok or frame is None:
                raise JetsonPolicyError(f"camera {key!r} produced no frame")
            observation[key] = frame
            self._capture_mono[key] = now
        return observation

    def camera_ages_ms(self) -> dict[str, float]:
        now = time.monotonic()
        return {
            key: (now - stamp) * 1000.0 for key, stamp in self._capture_mono.items()
        }

    def torque_state(self) -> dict[str, int]:
        return {
            f"{side}_{motor}": value
            for side, arm in self.arms.items()
            for motor, value in arm.torque_state().items()
        }

    def arms_summary(self) -> dict[str, dict[str, Any]]:
        return {side: arm.summary() for side, arm in self.arms.items()}

    @property
    def forbidden_writes(self) -> tuple[str, ...]:
        return tuple(
            f"{side}:{name}"
            for side, arm in self.arms.items()
            for name in arm._writes
        )

    def close(self) -> None:
        for capture in self._captures.values():
            try:
                capture.release()
            except Exception:  # noqa: BLE001
                logger.debug("failed to release bimanual observation camera")
        self._captures.clear()
        for side in ("right", "left"):
            self.arms[side].close()


def _split_bimanual_state(state: dict[str, float]) -> dict[str, dict[str, float]]:
    return {
        side: {
            name: value
            for name, value in state.items()
            if name.startswith(f"{side}_")
        }
        for side in ("left", "right")
    }


def handle_start_bimanual_observe(
    request: BimanualObserveRequest, manager: Any
) -> dict[str, Any]:
    global _session
    from . import (
        record as _record,
        remote_inference as _remote,
        rollout as _rollout,
        teleoperate as _teleoperate,
    )

    with _lock:
        if _session is not None and _session.get("active"):
            return {
                "success": False,
                "status_code": 409,
                "message": "Read-only observation is already active",
            }
        for name, active in (
            ("recording", _record.recording_active),
            ("teleoperation", _teleoperate.teleoperation_active),
            ("inference", _rollout.inference_active or _remote.remote_inference_active()),
        ):
            if active:
                return {
                    "success": False,
                    "status_code": 409,
                    "message": f"{name} is active; stop it before observation",
                }

    try:
        source = ReadOnlyBimanualFollowerSource(
            request.left_port,
            request.right_port,
            left_calibration=request.left_calibration,
            right_calibration=request.right_calibration,
            cameras=_camera_specs(request.cameras),
        )
        source.start()
    except Exception as exc:  # noqa: BLE001
        logger.exception("bimanual read-only observation failed to open")
        return {
            "success": False,
            "status_code": 503,
            "message": f"Could not open bimanual observation: {exc}",
        }

    stop_event = threading.Event()
    with _lock:
        _session = {
            "active": True,
            "phase": "observing",
            "mode": "bimanual",
            "fault": None,
            "frames": 0,
            "state": {},
            "arms": {},
            "camera_ages_ms": {},
            "frames_jpeg": {},
            "torque": source.torque_state(),
            "arms_summary": source.arms_summary(),
            "source": source,
            "started_mono": time.monotonic(),
            "stop": stop_event,
        }

    def worker() -> None:
        period = 1.0 / max(float(request.fps), 0.1)
        try:
            while not stop_event.is_set():
                started = time.monotonic()
                try:
                    observation = source.capture()
                    state: dict[str, float] = {}
                    for key in (*LEFT_JOIN_NAMES, *RIGHT_JOIN_NAMES):
                        if key not in observation:
                            raise JetsonPolicyError(f"missing {key}")
                        value = float(observation[key])
                        if not math.isfinite(value):
                            raise JetsonPolicyError(f"{key} is not finite")
                        state[key] = value
                    camera_names = [
                        name
                        for name in observation
                        if not name.endswith(".pos")
                    ]
                    frames_jpeg: dict[str, bytes] = {}
                    for key in camera_names:
                        ok, buffer = cv2.imencode(
                            ".jpg",
                            np.asarray(observation[key]),
                            [cv2.IMWRITE_JPEG_QUALITY, 80],
                        )
                        if ok:
                            frames_jpeg[key] = buffer.tobytes()
                    with _lock:
                        if _session is None:
                            return
                        _session["frames"] += 1
                        _session["state"] = state
                        _session["arms"] = _split_bimanual_state(state)
                        _session["camera_ages_ms"] = source.camera_ages_ms()
                        _session["frames_jpeg"] = frames_jpeg
                    if manager is not None:
                        manager.broadcast_joint_data_sync(
                            {
                                "type": "joint_update",
                                "mode": "bimanual",
                                "source": "observe_only",
                                "joints": state,
                                "arms": _split_bimanual_state(state),
                                "actions_sent": 0,
                                "timestamp": time.time(),
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("bimanual read-only observation loop failed")
                    with _lock:
                        if _session is not None:
                            _session["fault"] = f"{type(exc).__name__}: {exc}"
                            _session["phase"] = "fault"
                            _session["active"] = False
                    stop_event.set()
                    return
                time.sleep(max(0.0, period - (time.monotonic() - started)))
        finally:
            with _lock:
                if _session is not None and _session.get("source") is source:
                    _session["active"] = False
                    if _session.get("phase") != "fault":
                        _session["phase"] = "stopped"
            source.close()

    thread = threading.Thread(
        target=worker, name="read-only-bimanual-observer", daemon=True
    )
    with _lock:
        if _session is not None:
            _session["thread"] = thread
    thread.start()
    logger.info(
        "bimanual read-only observation started left=%s right=%s cameras=%s",
        request.left_port,
        request.right_port,
        list(request.cameras),
    )
    return {"success": True, "status": _session_status()}

