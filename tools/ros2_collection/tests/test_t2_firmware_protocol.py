"""T2 offline wire-contract tests (independently named for session T2).

These are a Python *model/fixture* of the T2 wire contract, NOT execution of
MCU C and NOT hardware evidence. The compiled-C counterpart is
test_t2_firmware_host_c.py; patch reproducibility is test_t2_firmware_patch.py.
"""
import json
import math
import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "vendor_patches" / "firmware"
FUNC = 12
ORIGINAL_KEYS = ["enable", "update", "stop", "capabilities", "snapshot",
                 "count_wrap", "new_boot", "bad_crc", "wrong_function",
                 "legacy_no_response"]


def crc8(data):
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ (0x8C if c & 1 else 0)
    return c


def frame(payload, function=FUNC):
    body = bytes((function, len(payload))) + payload
    return b"\xaa\x55" + body + bytes((crc8(body),))


def parse_frame(raw):
    if len(raw) < 5 or raw[:2] != b"\xaa\x55":
        raise ValueError("header")
    if len(raw) != raw[3] + 5:
        raise ValueError("length")
    if crc8(raw[2:-1]) != raw[-1]:
        raise ValueError("crc")
    return raw[2], raw[4:-1]


def decode(raw, expected_boot=None, last=None):
    """Receiver-side validity gate for a wheel snapshot."""
    func, p = parse_frame(raw)
    if func != FUNC:
        raise ValueError("function")
    if len(p) != 93 or p[0] != 0x82:
        raise ValueError("not snapshot")
    boot, tick, seq, faults = struct.unpack_from("<16sIII", p, 1)
    if expected_boot is not None and boot != expected_boot:
        raise ValueError("boot changed")
    wheels = [struct.unpack_from("<qff", p, 29 + 16 * i) for i in range(4)]
    for count, measured, target in wheels:
        if not math.isfinite(measured) or not math.isfinite(target):
            raise ValueError("non-finite rps")
    if faults & 0x0000000C:
        raise ValueError("encoder invalid / snapshot stale")
    if last is not None:
        prev_tick, prev_seq = last
        if (seq - prev_seq) & 0xFFFFFFFF > 0x7FFFFFFF:
            raise ValueError("sample_seq went backwards")
        if (tick - prev_tick) & 0xFFFFFFFF > 0x7FFFFFFF:
            raise ValueError("board tick went backwards")
    return boot, tick, seq, faults, wheels


def command(action, boot, epoch, seq, ttl, speeds=()):
    p = struct.pack("<BB16sQIHB", 0x02, action, boot, epoch, seq, ttl,
                    4 if action in (1, 2) else 0)
    for i, value in enumerate(speeds):
        p += struct.pack("<Bf", i, value)
    return frame(p)


def fixture_vectors():
    """Byte-for-byte reproduction of the original Wave A golden vectors."""
    boot = bytes(range(1, 17))
    build = b"T2-DEMO-BUILD-01"
    caps = frame(struct.pack("<BB16s16sI", 0x81, 1, boot, build, 7))

    def snap(b, tick, seq, counts):
        p = struct.pack("<B16sIII", 0x82, b, tick, seq, 0)
        for i, count in enumerate(counts):
            p += struct.pack("<qff", count, (i - 1) * 0.5, i * 0.25)
        return frame(p)

    first = snap(boot, 1000, 1, [59999, -1, 0, 123])
    over = snap(boot, 1010, 2, [60001, -2, 0, 124])
    new = snap(bytes(range(17, 33)), 10, 1, [0, 0, 0, 0])
    bad = bytearray(first)
    bad[-1] ^= 1
    return {"capabilities": caps.hex(), "snapshot": first.hex(),
            "count_wrap": over.hex(), "new_boot": new.hex(),
            "bad_crc": bytes(bad).hex(),
            "wrong_function": frame(first[4:-1], 3).hex(),
            "legacy_no_response": "",
            "enable": command(1, boot, 7, 1, 100, [0, 0, 0, 0]).hex(),
            "update": command(2, boot, 7, 2, 100, [0, 0.25, 0.5, 0.75]).hex(),
            "stop": command(3, boot, 7, 3, 0).hex()}


class TestT2Vectors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = json.loads((ROOT / "golden_vectors.json").read_text())
        cls.boot = bytes(range(1, 17))

    def test_original_vectors_unchanged(self):
        """The 10 keys established at Wave A start are byte-identical."""
        expected = fixture_vectors()
        for key in ORIGINAL_KEYS:
            with self.subTest(key=key):
                self.assertEqual(self.v[key], expected[key])

    def test_crc_check_value(self):
        self.assertEqual(crc8(b"123456789"), 0xA1)

    def test_new_vectors_and_meta(self):
        self.assertEqual(self.v["meta"]["protocol_version"], 1)
        self.assertEqual(self.v["meta"]["function_code"], FUNC)
        self.assertEqual(self.v["meta"]["source_kind"],
                         "compiled_C_host_harness_golden_bytes")
        for key in ("capability_query", "release", "wrong_length"):
            self.assertIn(key, self.v)
        func, p = parse_frame(bytes.fromhex(self.v["capability_query"]))
        self.assertEqual(func, FUNC)
        self.assertEqual(p, b"\x01\x01")
        self.assertEqual(len(bytes.fromhex(self.v["wrong_length"])), 97)

    def test_counts_cross_timer_wrap_without_resetting_total(self):
        a = decode(bytes.fromhex(self.v["snapshot"]), self.boot)
        b = decode(bytes.fromhex(self.v["count_wrap"]), self.boot,
                   last=(a[1], a[2]))
        self.assertEqual([a[4][0][0], b[4][0][0]], [59999, 60001])
        self.assertEqual(b[4][0][0] - a[4][0][0], 2)  # ARR=60000 => 60001 counts/rollover
        self.assertEqual(b[4][1][0], -2)

    def test_bad_frames_and_boot_change_are_rejected(self):
        for key in ("bad_crc", "wrong_function", "wrong_length",
                    "legacy_no_response", "new_boot"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                decode(bytes.fromhex(self.v[key]), self.boot)
        good = bytes.fromhex(self.v["snapshot"])
        with self.assertRaises(ValueError):
            decode(good[:-1], self.boot)

    def test_capabilities_is_not_a_snapshot_and_legacy_is_unknown(self):
        with self.assertRaises(ValueError):
            decode(bytes.fromhex(self.v["capabilities"]), self.boot)
        self.assertEqual(self.v["legacy_no_response"], "")
        self.assertEqual(self.v["expectations"]["legacy_no_response"]["action"],
                         "capability_status=unsupported_or_unknown")

    def test_snapshot_fields_and_finite_rps(self):
        _, tick, seq, faults, wheels = decode(
            bytes.fromhex(self.v["snapshot"]), self.boot)
        self.assertEqual((tick, seq, faults), (1000, 1, 0))
        self.assertEqual([w[0] for w in wheels], [59999, -1, 0, 123])
        self.assertEqual([round(w[1], 3) for w in wheels], [-0.5, 0.0, 0.5, 1.0])
        self.assertEqual([round(w[2], 3) for w in wheels], [0.0, 0.25, 0.5, 0.75])

    def test_command_model_fields(self):
        boot = self.boot
        for key, action, seq, ttl in (("enable", 1, 1, 100),
                                      ("update", 2, 2, 100),
                                      ("stop", 3, 3, 0),
                                      ("release", 4, 1, 0)):
            with self.subTest(key=key):
                raw = bytes.fromhex(self.v[key])
                func, p = parse_frame(raw)
                self.assertEqual(func, FUNC)
                sub, act, b, epoch, s, t, count = struct.unpack_from(
                    "<BB16sQIHB", p, 0)
                self.assertEqual((sub, act, b, s, t), (0x02, action, boot, seq, ttl))
                self.assertGreater(epoch, 0)
                self.assertEqual(count, 4 if action in (1, 2) else 0)
                self.assertEqual(len(p), 53 if action in (1, 2) else 33)
        # ENABLE must carry zero speeds; UPDATE carries the four wheel targets.
        _, p = parse_frame(bytes.fromhex(self.v["enable"]))
        self.assertTrue(all(struct.unpack_from("<f", p, 34 + 5 * i)[0] == 0.0
                            for i in range(4)))
        _, p = parse_frame(bytes.fromhex(self.v["update"]))
        self.assertEqual([round(struct.unpack_from("<f", p, 34 + 5 * i)[0], 3)
                          for i in range(4)], [0.0, 0.25, 0.5, 0.75])

    def test_command_model_rejects_illegal_values(self):
        """Python model of board-side rejection rules; C proof is in host_c test."""
        boot = self.boot
        cases = {
            "nan": command(2, boot, 7, 3, 100, [float("nan"), 0, 0, 0]),
            "inf": command(2, boot, 7, 3, 100, [float("inf"), 0, 0, 0]),
            "over_range": command(2, boot, 7, 3, 100, [7.0, 0, 0, 0]),
            "zero_ttl": command(2, boot, 7, 3, 0, [0, 0, 0, 0]),
            "oversize_ttl": command(2, boot, 7, 3, 501, [0, 0, 0, 0]),
            "enable_nonzero": command(1, boot, 8, 1, 100, [0.1, 0, 0, 0]),
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                _, p = parse_frame(raw)
                sub, act, b, epoch, seq, ttl, count = struct.unpack_from(
                    "<BB16sQIHB", p, 0)
                values = [struct.unpack_from("<f", p, 34 + 5 * i)[0]
                          for i in range(count)]
                finite = all(math.isfinite(v) for v in values)
                in_range = all(-6.0 <= v <= 6.0 for v in values)
                enable_zero = act != 1 or all(v == 0.0 for v in values)
                self.assertFalse(finite and in_range and enable_zero and
                                 1 <= ttl <= 500 and count == 4,
                                 "model should reject %s" % name)
        # wrong boot / old epoch / stale seq are receiver-side session checks
        _, p = parse_frame(command(2, bytes(range(17, 33)), 7, 3, 100, [0] * 4))
        self.assertNotEqual(p[2:18], boot)
        _, p = parse_frame(command(2, boot, 6, 4, 100, [0] * 4))
        self.assertEqual(struct.unpack_from("<Q", p, 18)[0], 6)

    def test_expectations_declare_not_proof_of(self):
        not_proof = self.v["meta"]["not_proof_of"]
        for item in ("board flashed", "real encoder readback",
                     "ARM firmware build"):
            self.assertIn(item, not_proof)


if __name__ == "__main__":
    unittest.main()
