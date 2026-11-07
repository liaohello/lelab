# T2 firmware candidate artifacts

Status 2026-11-04 (Wave A): protocol spec, golden vectors, standalone C candidate, board-integration patch set, native host harness and contract tests are complete and verified offline. **No MCU binary was produced and nothing was flashed**: this host has no ARM cross-toolchain (see "Build status"). `PROTOCOL.md` and `golden_vectors.json` are the T1-consumable wire contract; they do not describe a deployed capability.

## Files

- `PROTOCOL.md` — wire contract v1: framing `AA 55 func len payload crc8` (function `0x0C`, Dallas/Maxim CRC-8 poly 0x8c, `crc8("123456789")=a1`), capability query/response, ENABLE/UPDATE/STOP command model with boot_id+epoch+seq gating and TTL expiry/latch, 98-byte periodic wheel snapshots. Original firmware does not implement these messages; legacy boards answer nothing on `0x0C`.
- `golden_vectors.json` — 13 byte-exact vectors (enable, update, stop, release, capability_query, capabilities, snapshot, count_wrap, new_boot, bad_crc, wrong_function, wrong_length, legacy_no_response) shared by this C logic, the host harness and T1 decoder fixtures.
- `t2_protocol.h` / `t2_protocol.c` — standalone candidate state machine and serializers (`-std=c99 -Wall -Wextra -Werror -pedantic` clean). Not included or called by the original project.
- `t2_host_harness.c` (+ `t2_crc_reference.h`) — native host harness that executes the real compiled C logic: 76 checks covering all rejection paths (bad CRC, wrong function/length, illegal motor id/count, NaN/Inf, TTL bounds, stale seq/epoch, wrong boot, replay, expiry/latch, snapshot torn-read detection, counter wrap across timer rollover).
- `t2_host_build.sh` — reproducible native build + report archive into `docs/evidence/parallel-20261104/T2/`.
- `baseline-source.sha256` / `baseline.sha256` — exact reference-tree source hashes (1159 files full list / 13-file relevant subset). HEAD alone does not represent the heavily dirty reference tree.
- `0001-encoder-timer-fixes.patch` — recorded `Hiwonder/Portings/motor_porting.c`: 60001-count timer modulus and TIM3 start for motor 4. Does not fix pending-wrap races or validate the physical encoder path.
- `0002-standalone-candidate.patch` — adds `Hiwonder/System/t2_protocol.{c,h}` without wiring them into the project.
- `0003-t2-board-integration.patch` — wires the candidate into the recorded baseline: packet callback registration in `packet_handle.c`, 10 ms ISR-driven sampling snapshot + expiry call in `stm32f4xx_it.c` TIM7 handler (no serial I/O in the interrupt), main-loop call sites in `app.c`, plus new glue files `Hiwonder/System/t2_board.{c,h}` (snapshot publish, SysTick time, boot id). Hash-pinned: each hunk applies only against the exact recorded baseline; wrong baseline fails the dry-run.
- `MANIFEST.json` — patch set manifest (verified by `verify_patches.py`).
- `t2_verify.py` — **superseded** by `t2_host_build.sh` + `work/ros2_collection/firmware-development/verify_patches.py`; it depends on an llvmlite JIT environment that is not installed on this host. Kept for reference only.

## Verifying patches (never patch the reference tree)

Copy the exact baseline elsewhere first. Apply `0001` → `0002` → `0003` with `patch --dry-run -p1` then `patch -p1`; roll back in reverse order with `patch -R --dry-run -p1` then `patch -R -E -p1` (`-E` removes created empty files). `work/ros2_collection/firmware-development/verify_patches.py` automates apply/revert/wrong-baseline-rejection in isolated temp dirs (47 checks).

## Offline checks

Native build + harness (uses the project-isolated conda gcc; falls back to `cc`/`gcc`; installs nothing):

```
bash tools/ros2_collection/vendor_patches/firmware/t2_host_build.sh
```

Protocol contract tests (pytest, project ros_jazzy env):

```
work/ros2_collection/envs/ros_jazzy/bin/python -m pytest tools/ros2_collection/tests/test_t2_firmware_protocol.py -v
```

Latest results (2026-11-04): harness **76/76 PASS**, golden vectors **13/13 byte-identical** between compiled C and `golden_vectors.json`, pytest **10 passed + 25 subtests**, patch verify **47/47**. Evidence: `docs/evidence/parallel-20261104/T2/`.

## Build status / gap

Keil project targets STM32F407VETx with ARMCC 5.06u6 + STM32F4xx_DFP 2.16.0. No `arm-none-eabi-*`, `armcc` or Keil MDK is installed on this host, so **no hex/bin/axf was produced by this work**; any existing hex in the reference tree predates it. MCU-side behavior (timing, ISR safety on target, IWDG interaction, actual parking/reset behavior) is unverified and requires an integrated cross-build plus hardware acceptance. Installing a toolchain is out of scope for this wave.
