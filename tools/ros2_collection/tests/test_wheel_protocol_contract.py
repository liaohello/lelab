"""Cross-check T1's receiver codec against T2's compiled-C golden vectors."""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from lelab_mobile_agent import wheel_protocol as wheel

ROOT = Path(__file__).resolve().parents[3]
VECTORS = ROOT / "tools/ros2_collection/vendor_patches/firmware/golden_vectors.json"


@pytest.fixture(scope="module")
def vectors():
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def raw(vectors, key):
    return bytes.fromhex(vectors[key])


def test_every_t2_golden_vector_crosses_the_t1_receiver_boundary(vectors):
    boot = bytes(range(1, 17))
    capabilities = wheel.decode_capabilities(raw(vectors, "capabilities"))
    assert capabilities["boot_id"] == boot.hex()
    assert capabilities["capabilities"] == 7
    assert capabilities["supported"] is True
    assert wheel.decode_capability_query(raw(vectors, "capability_query")) == {
        "function": wheel.T2_FUNCTION,
        "protocol_version": 1,
    }

    first = wheel.decode_snapshot(raw(vectors, "snapshot"), expected_boot=boot)
    second = wheel.decode_snapshot(
        raw(vectors, "count_wrap"),
        expected_boot=boot,
        previous_tick=first["board_tick"],
        previous_seq=first["seq"],
    )
    assert first["counts"] == [59999, -1, 0, 123]
    assert second["counts"] == [60001, -2, 0, 124]
    assert second["counts"][0] - first["counts"][0] == 2
    assert first["source_kind"] == "wheel_encoder_readback"
    assert first["usable_for_state"] is True

    for key, reason in (
        ("new_boot", "boot_changed"),
        ("bad_crc", "bad_crc"),
        ("wrong_function", "wrong_function"),
        ("wrong_length", "length_mismatch"),
        ("legacy_no_response", "short_frame"),
    ):
        with pytest.raises(wheel.WheelProtocolError) as error:
            wheel.decode_snapshot(raw(vectors, key), expected_boot=boot)
        assert error.value.reason == reason


def test_t1_command_encoder_matches_all_backward_compatible_vectors(vectors):
    boot = bytes(range(1, 17))
    zero = {wheel_id: 0.0 for wheel_id in range(4)}
    drive = {0: 0.0, 1: 0.25, 2: 0.5, 3: 0.75}
    cases = {
        "enable": wheel.encode_command(
            action=wheel.ACTION_ENABLE,
            boot_id=boot,
            epoch=7,
            seq=1,
            ttl_ms=100,
            target_rps=zero,
        ),
        "update": wheel.encode_command(
            action=wheel.ACTION_DRIVE,
            boot_id=boot,
            epoch=7,
            seq=2,
            ttl_ms=100,
            target_rps=drive,
        ),
        "stop": wheel.encode_command(
            action=wheel.ACTION_CANCEL,
            boot_id=boot,
            epoch=7,
            seq=3,
            ttl_ms=0,
        ),
        "release": wheel.encode_command(
            action=wheel.ACTION_RELEASE,
            boot_id=boot,
            epoch=8,
            seq=1,
            ttl_ms=0,
        ),
    }
    for key, encoded in cases.items():
        assert encoded == raw(vectors, key)
        decoded = wheel.decode_command(encoded, expected_boot=boot)
        assert decoded["frame"] == "valid"

    update = wheel.decode_command(raw(vectors, "update"), expected_boot=boot)
    assert update["action_name"] == "DRIVE"
    assert update["ttl_ms"] == 100
    assert update["target_rps"] == drive


def test_snapshot_faults_are_reported_but_never_repaired():
    boot = bytes(range(1, 17))
    payload = bytearray(
        struct.pack("<B16sIII", wheel.SUB_SNAPSHOT, boot, 10, 1, 0x00010004)
    )
    for wheel_id in range(4):
        payload += struct.pack("<qff", wheel_id, 0.0, 0.0)
    decoded = wheel.decode_snapshot(wheel.encode_frame(wheel.T2_FUNCTION, bytes(payload)))
    assert decoded["valid"] is True
    assert decoded["usable_for_state"] is False
    assert "encoder_invalid" in decoded["fault_names"]
    assert "latched_timeout" in decoded["fault_names"]


def test_a_command_from_another_boot_is_refused(vectors):
    boot = bytes(range(1, 17))
    with pytest.raises(wheel.WheelProtocolError, match="boot_changed"):
        wheel.decode_command(raw(vectors, "enable"), expected_boot=bytes(range(2, 18)))
