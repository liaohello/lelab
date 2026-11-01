"""Device cleanup and non-interactive connection helpers.

Serial ports are a trust boundary: if a normal disconnect fails while disabling
torque, the port/camera handle can stay open.  Bimanual wrappers add another
failure mode: ``BimanualMixin.connect()`` connects the left arm and then the
right arm without rollback, and ``is_connected`` is false when only one arm
opened.  Cleanup therefore walks the physical children instead of trusting the
wrapper flag.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any

_ARMS = ("left_arm", "right_arm")


def physical_devices(device: Any) -> list[Any]:
    """Return leaf devices, expanding a bimanual wrapper exactly once."""

    if device is None:
        return []
    if any(getattr(device, arm, None) is not None for arm in _ARMS):
        leaves: list[Any] = []
        for arm in _ARMS:
            leaves.extend(physical_devices(getattr(device, arm, None)))
        return leaves
    return [device]


def unique_cameras(devices: list[Any]) -> list[Any]:
    """Return each camera object once.

    The bimanual wrapper exposes top-level cameras through the left arm as well
    as through ``robot.cameras``; connecting the same object twice would open
    one physical device twice.
    """

    seen: set[int] = set()
    cameras: list[Any] = []
    for device in devices:
        for camera in (getattr(device, "cameras", {}) or {}).values():
            if id(camera) in seen:
                continue
            seen.add(id(camera))
            cameras.append(camera)
    return cameras


def connect_recording_devices(robot: Any, teleop: Any) -> None:
    """Bring up physical arms in the historical non-interactive order.

    Order per arm: bus connect (all arms) -> write calibration (torque is still
    disabled) -> connect each unique camera -> configure motors.  This mirrors
    the proven single-arm path and deliberately avoids ``Robot.connect()``,
    which may prompt on stdin and enables torque before the calibration
    registers are written.

    On any failure every bus/camera opened so far is force-released so a
    half-connected bimanual rig cannot lock the next attempt out of its ports.
    """

    follower_leaves = physical_devices(robot)
    leader_leaves = physical_devices(teleop)
    leaves = follower_leaves + leader_leaves

    for device in leaves:
        calibration = getattr(device, "calibration", None)
        if not calibration:
            name = getattr(device, "id", None) or getattr(device, "name", "device")
            raise ValueError(
                f"Missing local calibration for {name}; complete per-arm calibration first"
            )

    connected: list[Any] = []
    opened_cameras: list[Any] = []
    try:
        for device in leaves:
            device.bus.connect()
            connected.append(device)

        for device in leaves:
            device.bus.write_calibration(device.calibration)
            # Rebuild the calibrated-range guard from the just-written
            # calibration; SOFollower normally does this inside connect().
            if hasattr(device, "_build_calibration_limits"):
                with suppress(Exception):
                    device._position_origin = None
                    device._calibration_limits = device._build_calibration_limits()

        for camera in unique_cameras(leaves):
            camera.connect()
            opened_cameras.append(camera)

        for device in leaves:
            device.configure()
    except Exception:
        for camera in reversed(opened_cameras):
            with suppress(Exception):
                camera.disconnect()
        for device in reversed(connected):
            _force_close_device_resources(device, logging.getLogger(__name__))
        raise


def safe_disconnect_device(device: Any, logger: logging.Logger, context: str = "cleanup") -> None:
    """Disconnect a device (or both bimanual children) and force-release resources."""

    if device is None:
        return

    children = [getattr(device, arm, None) for arm in _ARMS]
    if any(child is not None for child in children):
        # A partially connected wrapper reports is_connected=False; always clean
        # each child so one failed bus cannot strand the other.
        for child in children:
            safe_disconnect_device(child, logger, context=context)
        return

    try:
        device.disconnect()
        return
    except Exception as exc:
        logger.warning("Error disconnecting device during %s: %s", context, exc)

    _force_close_device_resources(device, logger)


def _force_close_device_resources(device: Any, logger: logging.Logger) -> None:
    """Best-effort release for serial/camera resources after disconnect fails."""

    bus = getattr(device, "bus", None)
    port_handler = getattr(bus, "port_handler", None)
    if port_handler is not None:
        with suppress(Exception):
            port_handler.clearPort()
        with suppress(Exception):
            port_handler.is_using = False
        try:
            port_handler.closePort()
            logger.info("Force-closed serial port after disconnect failure")
        except Exception as exc:
            logger.warning("Failed to force-close serial port after disconnect failure: %s", exc)

    cameras = getattr(device, "cameras", None)
    if isinstance(cameras, dict):
        for cam in cameras.values():
            try:
                cam.disconnect()
            except Exception as exc:
                logger.warning("Failed to disconnect camera after device cleanup failure: %s", exc)
