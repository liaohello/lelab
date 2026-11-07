#!/usr/bin/env python3
"""Offline T2 evidence, no device access. Requires existing cc1 and llvmlite."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
SOURCE = Path(os.environ.get("T2_FIRMWARE_SOURCE", "/home/liaozhengyan/Desktop/resource/RosRobotControllerLite_ros_250811"))


def run(args, **kwargs):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    print("$", " ".join(map(str, args)), flush=True)
    print(result.stdout, end="", flush=True)
    print("exit:", result.returncode, flush=True)
    if result.returncode:
        raise SystemExit(result.returncode)
    return result.stdout


def main():
    print("T2 offline verification; hardware was not accessed", flush=True)
    r = subprocess.run(["sha256sum", "-c", str(HERE / "baseline-source.sha256")], cwd=SOURCE,
                       text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    lines = r.stdout.splitlines()
    failed = [line for line in lines if not line.endswith(": OK")]
    print("baseline source manifest: checked", len(lines), "entries; exit:", r.returncode, flush=True)
    if r.returncode:
        print("\n".join(failed), flush=True)
        raise SystemExit(r.returncode)
    with tempfile.TemporaryDirectory(prefix="t2-patch-check-") as tmp:
        target = Path(tmp)
        (target / "Hiwonder/Portings").mkdir(parents=True)
        (target / "Hiwonder/System").mkdir(parents=True)
        rel = Path("Hiwonder/Portings/motor_porting.c")
        original = (SOURCE / rel).read_bytes()
        (target / rel).write_bytes(original)
        for patch in ("0001-encoder-timer-fixes.patch", "0002-standalone-candidate.patch"):
            patch_bytes = (HERE / patch).read_bytes()
            for flags in (["--dry-run"], []):
                r = subprocess.run(["patch", "-p1", *flags, "-d", str(target)], input=patch_bytes,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                print(patch, "apply", "dry-run" if flags else "actual", "exit:", r.returncode, flush=True)
                if r.returncode:
                    print(r.stdout.decode(errors="replace"), flush=True)
                    raise SystemExit(r.returncode)
        for name in ("t2_protocol.c", "t2_protocol.h"):
            assert (target / "Hiwonder/System" / name).read_bytes() == (HERE / name).read_bytes()
        for patch in ("0002-standalone-candidate.patch", "0001-encoder-timer-fixes.patch"):
            for flags in (["--dry-run"], []):
                r = subprocess.run(["patch", "-R", "-p1", *flags, "-d", str(target)], input=(HERE / patch).read_bytes(),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                print(patch, "reverse", "dry-run" if flags else "actual", "exit:", r.returncode, flush=True)
                if r.returncode:
                    print(r.stdout.decode(errors="replace"), flush=True)
                    raise SystemExit(r.returncode)
        assert (target / rel).read_bytes() == original
        assert not (target / "Hiwonder/System/t2_protocol.c").exists()
        assert not (target / "Hiwonder/System/t2_protocol.h").exists()
        print("reversed patch equals original baseline bytes", flush=True)
    run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tools/ros2_collection/tests"),
         "-p", "test_t2_firmware_protocol.py", "-v"], cwd=ROOT)
    run([sys.executable, str(HERE / "t2_host_jit.py")], cwd=ROOT)


if __name__ == "__main__":
    main()
