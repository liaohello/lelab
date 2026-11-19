#!/usr/bin/env bash
# FW-BUDGET-01: exercise `run_with_deadline.sh` with fake commands.
#
# Four cases, no GPU involved:
#   1. a command that finishes on its own        -> exit 0, deadline not fired
#   2. a command that outlives the deadline      -> SIGTERM, exit 143, flagged
#   3. a command that ignores SIGTERM            -> SIGKILL after 30 s, exit 137
#   4. checkpoints present when the deadline hits-> last_checkpoint still reported
#
# Run: bash test_deadline_runner.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="$HERE/run_with_deadline.sh"
WORK=$(mktemp -d)
PASS=0
FAIL=0

check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  PASS $name ($actual)"; PASS=$((PASS+1))
  else
    echo "  FAIL $name: expected $expected, got $actual"; FAIL=$((FAIL+1))
  fi
}

echo "case 1: normal completion"
bash "$RUN" --deadline-sec 60 --outdir "$WORK/c1" --log "$WORK/c1.log" -- bash -c 'echo done; exit 0'
code=$(/home/liaozhengyan/Desktop/lelab-lerobot070/.venv/bin/python -c "import json;print(json.load(open('$WORK/c1/timeline.json'))['exit_code'])")
fired=$(/home/liaozhengyan/Desktop/lelab-lerobot070/.venv/bin/python -c "import json;print(json.load(open('$WORK/c1/timeline.json'))['deadline_fired'])")
check "exit code" 0 "$code"
check "deadline fired" 0 "$fired"

echo "case 2: deadline fires"
bash "$RUN" --deadline-sec 2 --outdir "$WORK/c2" --log "$WORK/c2.log" -- sleep 30
code=$(/home/liaozhengyan/Desktop/lelab-lerobot070/.venv/bin/python -c "import json;print(json.load(open('$WORK/c2/timeline.json'))['exit_code'])")
fired=$(/home/liaozhengyan/Desktop/lelab-lerobot070/.venv/bin/python -c "import json;print(json.load(open('$WORK/c2/timeline.json'))['deadline_fired'])")
check "terminated" 143 "$code"
check "deadline fired" 1 "$fired"

echo "case 3: command ignores SIGTERM (expect SIGKILL after 30s)"
bash "$RUN" --deadline-sec 2 --outdir "$WORK/c3" --log "$WORK/c3.log" -- bash -c 'trap "" TERM; sleep 120'
code=$(/home/liaozhengyan/Desktop/lelab-lerobot070/.venv/bin/python -c "import json;print(json.load(open('$WORK/c3/timeline.json'))['exit_code'])")
check "killed" 137 "$code"

echo "case 4: checkpoint preserved when the deadline hits"
mkdir -p "$WORK/c4/checkpoints/000010" "$WORK/c4/checkpoints/last"
bash "$RUN" --deadline-sec 2 --outdir "$WORK/c4" --log "$WORK/c4.log" -- sleep 30
last=$(/home/liaozhengyan/Desktop/lelab-lerobot070/.venv/bin/python -c "import json;print(json.load(open('$WORK/c4/timeline.json'))['last_checkpoint'])")
case "$last" in
  */checkpoints/last/) echo "  PASS last checkpoint ($last)"; PASS=$((PASS+1)) ;;
  *) echo "  FAIL last checkpoint: $last"; FAIL=$((FAIL+1)) ;;
esac

echo "case 5: vram wait is recorded as disabled without --gpu-wait"
gpu=$(/home/liaozhengyan/Desktop/lelab-lerobot070/.venv/bin/python -c "import json;print(json.load(open('$WORK/c1/timeline.json'))['gpu_wait_enabled'])")
check "gpu_wait_enabled" 0 "$gpu"

echo
echo "passed=$PASS failed=$FAIL  (artifacts in $WORK)"
[ "$FAIL" -eq 0 ]
