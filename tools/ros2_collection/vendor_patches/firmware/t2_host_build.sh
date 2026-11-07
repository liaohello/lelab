#!/usr/bin/env bash
# Reproducible NATIVE host build of the real T2 C logic (NOT an MCU build).
# Uses an already-installed project-isolated compiler; installs nothing.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"
BUILD="$ROOT/work/ros2_collection/firmware-development/build"
REPORT_DIR="${T2_EVIDENCE_DIR:-$ROOT/docs/evidence/parallel-20261104/T2}"

CC="${T2_CC:-$ROOT/work/ros2_collection/envs/ros_jazzy/bin/x86_64-conda-linux-gnu-gcc}"
if [[ ! -x "$CC" ]]; then
  CC="$(command -v cc || command -v gcc || true)"
fi
if [[ -z "$CC" || ! -x "$CC" ]]; then
  echo "T2 BUILD GAP: no host C compiler available (looked for conda gcc / cc / gcc)" >&2
  exit 3
fi

mkdir -p "$BUILD" "$REPORT_DIR"
echo "compiler: $("$CC" --version | head -1)"
echo "sources : $HERE/t2_protocol.c $HERE/t2_host_harness.c"
"$CC" -std=c99 -Wall -Wextra -Werror -pedantic -O1 -I"$HERE" \
  "$HERE/t2_protocol.c" "$HERE/t2_host_harness.c" -o "$BUILD/t2_host_harness"
echo "host binary: $BUILD/t2_host_harness"
"$BUILD/t2_host_harness" > "$BUILD/t2_host_report.json"
cp "$BUILD/t2_host_report.json" "$REPORT_DIR/t2_host_report.json"
echo "report: $REPORT_DIR/t2_host_report.json"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("host harness:", d["result"], d["total"], "checks,", d["failed"], "failed")' \
  "$REPORT_DIR/t2_host_report.json"
