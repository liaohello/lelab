"""Receiver-side codec for the proposed T2 wheel protocol.

This module is transport-free.  The unique board UART owner must provide a
bounded, CRC-checked frame to :func:`decode_snapshot`; importing or using this
codec never opens a serial port and never proves that a board supports the
protocol.  The vectors in ``tools/ros2_collection/vendor_patches/firmware`` are
the byte-level contract shared with the candidate C implementation.
"""
from __future__ import annotations

import math
import struct
from typing import Any

T2_FUNCTION = 0x0C
T2_PROTOCOL_VERSION = 1

SUB_CAP_QUERY = 0x01
SUB_COMMAND = 0x02
SUB_CAP_REPORT = 0x81
SUB_SNAPSHOT = 0x82

ACTION_ENABLE = 1
ACTION_DRIVE = 2
ACTION_CANCEL = 3
ACTION_RELEASE = 4

WHEEL_COUNT = 4
SNAPSHOT_PAYLOAD_LEN = 93
CAPABILITY_PAYLOAD_LEN = 38
COMMAND_HEADER_LEN = 33
COMMAND_DRIVE_LEN = 53
COMMAND_STOP_LEN = 33
MAX_ABS_RPS = 6.0

CURRENT_FAULTS = {
    0x00000001: "timeout",
    0x00000002: "stopped",
    0x00000004: "encoder_invalid",
    0x00000008: "snapshot_stale",
    0x00000010: "rejected",
}
LATCHED_FAULTS = {
    0x00010000: "latched_timeout",
    0x00020000: "latched_reject",
    0x00040000: "latched_estop",
}


class WheelProtocolError(ValueError):
    """A stable, machine-readable refusal from the receiver-side codec."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def crc8(data: bytes | bytearray | memoryview) -> int:
    """Dallas/Maxim CRC-8 used by the board packet framing."""
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0x8C if value & 1 else 0)
    return value


def parse_frame(raw: bytes | bytearray | memoryview) -> tuple[int, bytes]:
    """Validate a complete board frame and return ``(function, payload)``."""
    data = bytes(raw)
    if len(data) < 5:
        raise WheelProtocolError("short_frame")
    if data[:2] != b"\xAA\x55":
        raise WheelProtocolError("bad_header")
    payload_len = data[3]
    if len(data) != payload_len + 5:
        raise WheelProtocolError("length_mismatch")
    if crc8(data[2:-1]) != data[-1]:
        raise WheelProtocolError("bad_crc")
    return data[2], data[4:-1]


def encode_frame(function: int, payload: bytes) -> bytes:
    if type(function) is not int or not 0 <= function <= 0xFF:
        raise WheelProtocolError("invalid_function")
    if len(payload) > 0xFF:
        raise WheelProtocolError("payload_too_long")
    body = bytes((function, len(payload))) + bytes(payload)
    return b"\xAA\x55" + body + bytes((crc8(body),))


def decode_capability_query(raw: bytes) -> dict[str, Any]:
    function, payload = parse_frame(raw)
    if function != T2_FUNCTION:
        raise WheelProtocolError("wrong_function")
    if payload != bytes((SUB_CAP_QUERY, T2_PROTOCOL_VERSION)):
        raise WheelProtocolError("not_capability_query")
    return {"function": function, "protocol_version": T2_PROTOCOL_VERSION}


def decode_capabilities(raw: bytes) -> dict[str, Any]:
    function, payload = parse_frame(raw)
    if function != T2_FUNCTION:
        raise WheelProtocolError("wrong_function")
    if len(payload) != CAPABILITY_PAYLOAD_LEN:
        raise WheelProtocolError("wrong_capability_length")
    subcommand, version, boot_id, build_id, caps = struct.unpack(
        "<BB16s16sI", payload
    )
    if subcommand != SUB_CAP_REPORT:
        raise WheelProtocolError("not_capability_report")
    if version != T2_PROTOCOL_VERSION:
        raise WheelProtocolError("unsupported_protocol_version")
    required = 0b111
    if caps & required != required:
        raise WheelProtocolError("required_capabilities_missing")
    if caps & ~required:
        raise WheelProtocolError("unknown_capability_bits")
    return {
        "frame": "valid",
        "decoder": "capability_report",
        "protocol_version": version,
        "boot_id": boot_id.hex(),
        "build_id_hex": build_id.hex(),
        "capabilities": caps,
        "wheel_snapshot": bool(caps & 1),
        "command_expiry": bool(caps & 2),
        "session_gate": bool(caps & 4),
        "supported": True,
        "source_kind": "firmware_capability_report",
    }


def decode_snapshot(
    raw: bytes,
    *,
    expected_boot: bytes | str | None = None,
    previous_tick: int | None = None,
    previous_seq: int | None = None,
    received_mono_ns: int | None = None,
    clock_domain: str = "car_monotonic",
) -> dict[str, Any]:
    """Decode one coherent four-wheel snapshot.

    Structural validity is not proof of a fresh or physically plausible wheel
    reading.  The returned ``usable_for_state`` also requires no current fault
    bits.  Calibration, scan freshness, and body-velocity derivation remain the
    caller's responsibility.
    """
    function, payload = parse_frame(raw)
    if function != T2_FUNCTION:
        raise WheelProtocolError("wrong_function")
    if len(payload) != SNAPSHOT_PAYLOAD_LEN:
        raise WheelProtocolError("wrong_snapshot_length")
    subcommand, boot_id, tick_ms, sample_seq, faults = struct.unpack_from(
        "<B16sIII", payload, 0
    )
    if subcommand != SUB_SNAPSHOT:
        raise WheelProtocolError("not_snapshot")

    expected = _normalise_boot(expected_boot)
    if expected is not None and boot_id != expected:
        raise WheelProtocolError("boot_changed")
    if previous_seq is not None:
        _require_advancing_u32(sample_seq, previous_seq, "sample_seq_went_backwards")
    if previous_tick is not None:
        _require_non_regressing_u32(tick_ms, previous_tick, "board_tick_went_backwards")

    wheels = []
    for wheel_id in range(WHEEL_COUNT):
        count, measured_rps, target_rps = struct.unpack_from(
            "<qff", payload, 29 + 16 * wheel_id
        )
        if not math.isfinite(measured_rps) or not math.isfinite(target_rps):
            raise WheelProtocolError("non_finite_rps")
        wheels.append(
            {
                "wheel_id": wheel_id,
                "ros_motor_id": wheel_id + 1,
                "count": count,
                "measured_rps": measured_rps,
                "target_rps": target_rps,
            }
        )

    fault_names = _fault_names(faults)
    current = faults & 0xFFFF
    return {
        "frame": "valid",
        "decoder": "snapshot",
        "source_kind": "wheel_encoder_readback",
        "boot_id": boot_id.hex(),
        "board_tick": tick_ms,
        "seq": sample_seq,
        "faults": faults,
        "fault_names": fault_names,
        "wheel_ids": list(range(WHEEL_COUNT)),
        "wheels": wheels,
        "measured_rps": [wheel["measured_rps"] for wheel in wheels],
        "target_rps": [wheel["target_rps"] for wheel in wheels],
        "counts": [wheel["count"] for wheel in wheels],
        "valid": True,
        "usable_for_state": current == 0,
        "received_mono_ns": _optional_int(received_mono_ns),
        "clock_domain": str(clock_domain),
        "note": "wheel speed feedback, not a measured body-velocity state",
    }


def decode_command(
    raw: bytes,
    *,
    expected_boot: bytes | str | None = None,
) -> dict[str, Any]:
    """Decode a T2 command for cross-module and audit tests."""
    function, payload = parse_frame(raw)
    if function != T2_FUNCTION:
        raise WheelProtocolError("wrong_function")
    if len(payload) not in (COMMAND_DRIVE_LEN, COMMAND_STOP_LEN):
        raise WheelProtocolError("wrong_command_length")
    subcommand, action, boot_id, epoch, seq, ttl_ms, count = struct.unpack_from(
        "<BB16sQIHB", payload, 0
    )
    if subcommand != SUB_COMMAND:
        raise WheelProtocolError("not_command")
    if action not in (
        ACTION_ENABLE,
        ACTION_DRIVE,
        ACTION_CANCEL,
        ACTION_RELEASE,
    ):
        raise WheelProtocolError("unknown_action")
    if epoch == 0 or seq == 0:
        raise WheelProtocolError("zero_epoch_or_seq")
    expected_boot_bytes = _normalise_boot(expected_boot)
    if expected_boot_bytes is not None and boot_id != expected_boot_bytes:
        raise WheelProtocolError("boot_changed")
    expected_count = WHEEL_COUNT if action in (ACTION_ENABLE, ACTION_DRIVE) else 0
    if count != expected_count:
        raise WheelProtocolError("wrong_wheel_count")
    if action in (ACTION_CANCEL, ACTION_RELEASE):
        if ttl_ms != 0 or len(payload) != COMMAND_STOP_LEN:
            raise WheelProtocolError("invalid_stop_ttl_or_length")
        speeds = {}
    else:
        if not 1 <= ttl_ms <= 500 or len(payload) != COMMAND_DRIVE_LEN:
            raise WheelProtocolError("invalid_ttl_or_length")
        speeds = {}
        seen: set[int] = set()
        for index in range(WHEEL_COUNT):
            motor_id, value = struct.unpack_from("<Bf", payload, 33 + 5 * index)
            if motor_id >= WHEEL_COUNT or motor_id in seen:
                raise WheelProtocolError("invalid_or_duplicate_motor_id")
            if not math.isfinite(value) or not -MAX_ABS_RPS <= value <= MAX_ABS_RPS:
                raise WheelProtocolError("invalid_rps")
            if action == ACTION_ENABLE and value != 0.0:
                raise WheelProtocolError("enable_requires_zero_targets")
            seen.add(motor_id)
            speeds[motor_id] = value
    return {
        "frame": "valid",
        "decoder": "command",
        "action": action,
        "action_name": {
            ACTION_ENABLE: "ENABLE",
            ACTION_DRIVE: "DRIVE",
            ACTION_CANCEL: "CANCEL",
            ACTION_RELEASE: "RELEASE",
        }[action],
        "boot_id": boot_id.hex(),
        "epoch": epoch,
        "seq": seq,
        "ttl_ms": ttl_ms,
        "wheel_count": count,
        "target_rps": speeds,
    }


def encode_command(
    *,
    action: int,
    boot_id: bytes,
    epoch: int,
    seq: int,
    ttl_ms: int,
    target_rps: dict[int, float] | None = None,
) -> bytes:
    """Build a test/owner-level command frame; this function performs no I/O."""
    boot = _normalise_boot(boot_id)
    if boot is None or len(boot) != 16:
        raise WheelProtocolError("invalid_boot_id")
    if type(epoch) is not int or not 0 < epoch <= 0xFFFFFFFFFFFFFFFF:
        raise WheelProtocolError("invalid_epoch")
    if type(seq) is not int or not 0 < seq <= 0xFFFFFFFF:
        raise WheelProtocolError("invalid_seq")
    if type(ttl_ms) is not int or not 0 <= ttl_ms <= 0xFFFF:
        raise WheelProtocolError("invalid_ttl")
    if action in (ACTION_ENABLE, ACTION_DRIVE):
        speeds = target_rps or {}
        if set(speeds) != set(range(WHEEL_COUNT)):
            raise WheelProtocolError("all_four_wheel_targets_required")
        payload = bytearray(
            struct.pack(
                "<BB16sQIHB",
                SUB_COMMAND,
                action,
                boot,
                epoch,
                seq,
                ttl_ms,
                WHEEL_COUNT,
            )
        )
        for motor_id in range(WHEEL_COUNT):
            payload += struct.pack("<Bf", motor_id, float(speeds[motor_id]))
    elif action in (ACTION_CANCEL, ACTION_RELEASE):
        payload = bytearray(
            struct.pack(
                "<BB16sQIHB",
                SUB_COMMAND,
                action,
                boot,
                epoch,
                seq,
                ttl_ms,
                0,
            )
        )
    else:
        raise WheelProtocolError("unknown_action")
    frame = encode_frame(T2_FUNCTION, bytes(payload))
    decode_command(frame, expected_boot=boot)
    return frame


def _normalise_boot(value: bytes | str | None) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = bytes.fromhex(value)
        except ValueError as exc:
            raise WheelProtocolError("invalid_boot_id") from exc
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise WheelProtocolError("invalid_boot_id")
    raw = bytes(value)
    if len(raw) != 16:
        raise WheelProtocolError("invalid_boot_id")
    return raw


def _fault_names(faults: int) -> list[str]:
    names = [name for bit, name in CURRENT_FAULTS.items() if faults & bit]
    names.extend(name for bit, name in LATCHED_FAULTS.items() if faults & bit)
    return names


def _require_advancing_u32(current: int, previous: int, reason: str) -> None:
    if _u32_delta(current, previous) <= 0:
        raise WheelProtocolError(reason)


def _require_non_regressing_u32(current: int, previous: int, reason: str) -> None:
    if _u32_delta(current, previous) < 0:
        raise WheelProtocolError(reason)


def _u32_delta(current: int, previous: int) -> int:
    delta = (int(current) - int(previous)) & 0xFFFFFFFF
    return delta if delta < 0x80000000 else delta - 0x100000000


def _optional_int(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WheelProtocolError("invalid_receive_timestamp")
    return value
