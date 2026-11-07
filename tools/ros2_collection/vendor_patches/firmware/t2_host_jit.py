#!/usr/bin/env python3
"""Run the actual C sources via locally available GCC cc1 + llvmlite MCJIT.

Linux/x86-64 development fallback only. No board, flash or ARM build involved.
Use PYTHONPATH to select an existing llvmlite installation.
"""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import tempfile

from llvmlite import binding as llvm

HERE = Path(__file__).resolve().parent
CC1 = os.environ.get("T2_CC1", "/usr/libexec/gcc/x86_64-linux-gnu/13/cc1")
HEADERS = {
    "stddef.h": "typedef __SIZE_TYPE__ size_t;\n#define NULL ((void *)0)\n",
    "stdint.h": "typedef __UINT8_TYPE__ uint8_t; typedef __UINT16_TYPE__ uint16_t; typedef __UINT32_TYPE__ uint32_t; typedef __UINT64_TYPE__ uint64_t; typedef __INT32_TYPE__ int32_t; typedef __INT64_TYPE__ int64_t;\n",
    "string.h": "#include <stddef.h>\nvoid *memset(void *, int, size_t); void *memcpy(void *, const void *, size_t); int memcmp(const void *, const void *, size_t);\n",
    "stdio.h": "int puts(const char *);\n",
    "assert.h": "#define assert(x) ((x) ? (void)0 : __builtin_trap())\n",
}


class State(ctypes.Structure):
    _fields_ = [("boot", ctypes.c_uint8 * 16), ("highest_epoch", ctypes.c_uint64),
                ("seq", ctypes.c_uint32), ("deadline_ms", ctypes.c_uint32),
                ("faults", ctypes.c_uint32), ("enabled", ctypes.c_uint8),
                ("boot_valid", ctypes.c_uint8), ("target", ctypes.c_float * 4)]


class Sample(ctypes.Structure):
    _fields_ = [("count", ctypes.c_int64 * 4), ("measured_rps", ctypes.c_float * 4),
                ("target_rps", ctypes.c_float * 4), ("tick_ms", ctypes.c_uint32),
                ("sample_seq", ctypes.c_uint32), ("faults", ctypes.c_uint32)]


def main():
    llvm.initialize_native_target()
    llvm.initialize_native_asmprinter()
    llvm.initialize_native_asmparser()
    modules = []
    with tempfile.TemporaryDirectory(prefix="t2-host-") as tmp:
        tmp = Path(tmp)
        for name, content in HEADERS.items():
            (tmp / name).write_text(content)
        for stem in ("t2_protocol", "t2_host_harness"):
            assembly = tmp / (stem + ".s")
            subprocess.run([CC1, "-quiet", "-std=c99", "-Wall", "-Wextra", "-Werror", "-fPIC", "-O0",
                            "-I", str(tmp), "-I", str(HERE), "-o", str(assembly), str(HERE / (stem + ".c"))], check=True)
            data = assembly.read_bytes()
            escaped = "".join(chr(b) if 32 <= b < 127 and b not in (34, 92) else "\\%02X" % b for b in data)
            modules.append(llvm.parse_assembly('module asm "' + escaped + '"'))
        machine = llvm.Target.from_default_triple().create_target_machine(reloc="pic")
        libc = ctypes.CDLL(None)
        for name in ("memcpy", "memcmp", "memset", "puts"):
            llvm.add_symbol(name, ctypes.cast(getattr(libc, name), ctypes.c_void_p).value)
        engine = llvm.create_mcjit_compiler(modules[0], machine)
        engine.add_module(modules[1])
        engine.finalize_object()
        address = engine.get_function_address("main")
        if not address:
            raise RuntimeError("host harness entry point not linked")
        if ctypes.CFUNCTYPE(ctypes.c_int)(address)() != 0:
            raise RuntimeError("C harness failed")
        print("C host harness returned 0", flush=True)

        # Compare actual compiled C serializers to independent Python golden bytes.
        init = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(State), ctypes.POINTER(ctypes.c_uint8))(engine.get_function_address("t2_init"))
        caps = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.POINTER(State), ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t)(engine.get_function_address("t2_capabilities"))
        snapshot = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.POINTER(State), ctypes.POINTER(Sample), ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t)(engine.get_function_address("t2_snapshot"))
        boot = (ctypes.c_uint8 * 16)(*range(1, 17))
        build = (ctypes.c_uint8 * 16).from_buffer_copy(b"T2-DEMO-BUILD-01")
        out = (ctypes.c_uint8 * 110)()
        state = State()
        assert init(ctypes.byref(state), boot) == 1
        vectors = json.loads((HERE / "golden_vectors.json").read_text())
        length = caps(ctypes.byref(state), build, out, len(out))
        assert bytes(out[:length]).hex() == vectors["capabilities"]
        state.faults = 0
        sample = Sample((ctypes.c_int64 * 4)(59999, -1, 0, 123),
                        (ctypes.c_float * 4)(-.5, 0, .5, 1),
                        (ctypes.c_float * 4)(0, .25, .5, .75), 1000, 1, 0)
        length = snapshot(ctypes.byref(state), ctypes.byref(sample), out, len(out))
        assert bytes(out[:length]).hex() == vectors["snapshot"]
        sample.count[0] = 60001
        sample.count[1] = -2
        sample.count[3] = 124
        sample.tick_ms = 1010
        sample.sample_seq = 2
        length = snapshot(ctypes.byref(state), ctypes.byref(sample), out, len(out))
        assert bytes(out[:length]).hex() == vectors["count_wrap"]
        command = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(State), ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_uint32)(engine.get_function_address("t2_command"))
        for key, now in (("enable", 0), ("update", 1), ("stop", 2)):
            raw = bytes.fromhex(vectors[key])
            wire = (ctypes.c_uint8 * len(raw)).from_buffer_copy(raw)
            assert command(ctypes.byref(state), wire, len(raw), now) == 1
        assert not state.enabled and all(value == 0 for value in state.target)
        print("Compiled C capability, snapshots, and command sequence match golden vectors", flush=True)


if __name__ == "__main__":
    main()
