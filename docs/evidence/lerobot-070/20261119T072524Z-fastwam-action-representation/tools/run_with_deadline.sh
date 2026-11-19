#!/usr/bin/env bash
# Run one GPU task under a hard wall-clock deadline and record what happened.
#
# The rule this enforces (PLAN §"执行预算"): a run is over when the process is
# gone and its VRAM is released, not when the log says "End of training" — that
# line appears minutes before the allocator lets go, and starting the next task
# in that window is what caused a self-inflicted double-allocation OOM last
# round.
#
# usage: run_with_deadline.sh --deadline-sec N --outdir DIR --log FILE \
#            [--gpu-wait] -- command...
#
# Writes DIR/timeline.json with: start/end epoch, exit code, whether the deadline
# fired, how long VRAM took to free, and the last checkpoint directory present.
# `--gpu-wait` polls nvidia-smi for our own PIDs; without it (e.g. on a CPU box)
# the VRAM step is skipped and recorded as such.
set -uo pipefail

DEADLINE_SEC=0
OUTDIR=""
CKPT_DIR=""
LOG=""
GPU_WAIT=0
FREE_GB=0
PREGATE_SEC=900

while [ $# -gt 0 ]; do
  case "$1" in
    --deadline-sec) DEADLINE_SEC="$2"; shift 2 ;;
    --outdir) OUTDIR="$2"; shift 2 ;;
    --ckpt-dir) CKPT_DIR="$2"; shift 2 ;;
    --log) LOG="$2"; shift 2 ;;
    --gpu-wait) GPU_WAIT=1; shift ;;
    --free-gb) FREE_GB="$2"; shift 2 ;;
    --pregate-sec) PREGATE_SEC="$2"; shift 2 ;;
    --) shift; break ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

[ -n "$OUTDIR" ] && [ -n "$LOG" ] && [ $# -gt 0 ] || {
  echo "usage: $0 --deadline-sec N --outdir DIR --log FILE [--gpu-wait] -- cmd..." >&2
  exit 64
}

mkdir -p "$OUTDIR"

gpu_used_mib() {
  nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d " "
}
gpu_total_mib() {
  nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d " "
}

PREWAIT=0
PREGATE_FAILED=0
if [ "$FREE_GB" -gt 0 ] && command -v nvidia-smi >/dev/null 2>&1; then
  TOTAL=$(gpu_total_mib)
  NEED=$(( FREE_GB * 1024 ))
  PRE_START=$(date +%s)
  while :; do
    USED=$(gpu_used_mib)
    [ -n "$USED" ] || break
    if [ $(( TOTAL - USED )) -ge "$NEED" ]; then break; fi
    if [ $(( $(date +%s) - PRE_START )) -ge "$PREGATE_SEC" ]; then
      echo "still only $(( TOTAL - USED )) MiB free after ${PREGATE_SEC}s; not starting" | tee -a "$OUTDIR/deadline.log"
      PREGATE_FAILED=1
      break
    fi
    echo "waiting for GPU: $(( TOTAL - USED )) MiB free, need $NEED" >> "$OUTDIR/deadline.log"
    sleep 15
  done
  PREWAIT=$(( $(date +%s) - PRE_START ))
fi

START=$(date +%s)
DEADLINE=$(( START + DEADLINE_SEC ))

{
  echo "start_epoch=$START"
  echo "deadline_epoch=$DEADLINE"
  echo "deadline_sec=$DEADLINE_SEC"
  echo "gpu_wait=$GPU_WAIT"
  echo "cmd=$*"
} > "$OUTDIR/run-meta.txt"

if [ "$PREGATE_FAILED" -eq 1 ]; then
  # Another tenant still owns the card. Refuse to start rather than OOM: a doomed
  # run costs the same wall clock and leaves nothing behind.
  cat > "$OUTDIR/timeline.json" <<JSON
{
  "start_epoch": $START,
  "end_epoch": $START,
  "wall_seconds": 0,
  "deadline_sec": $DEADLINE_SEC,
  "deadline_fired": 0,
  "exit_code": 75,
  "pid": 0,
  "leftover_children": "",
  "vram_free_after_seconds": "",
  "device_used_mib_after": "$(gpu_used_mib)",
  "pregate_wait_seconds": $PREWAIT,
  "free_gb_target": $FREE_GB,
  "ckpt_dir": "${CKPT_DIR:-$OUTDIR}",
  "last_checkpoint": "",
  "log": "$LOG",
  "refused": "insufficient_free_gpu_memory"
}
JSON
  echo "refused: only $(( $(gpu_total_mib) - $(gpu_used_mib) )) MiB free, wanted ${FREE_GB} GiB"
  exit 75
fi

"$@" > "$LOG" 2>&1 &
PID=$!
echo "pid=$PID" >> "$OUTDIR/run-meta.txt"

KILLED=0
while kill -0 "$PID" 2>/dev/null; do
  NOW=$(date +%s)
  if [ "$DEADLINE_SEC" -gt 0 ] && [ "$NOW" -ge "$DEADLINE" ]; then
    echo "deadline reached at $NOW; terminating $PID" | tee -a "$OUTDIR/deadline.log"
    KILLED=1
    kill -TERM "$PID" 2>/dev/null
    for _ in $(seq 1 30); do
      kill -0 "$PID" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$PID" 2>/dev/null; then
      echo "pid $PID ignored SIGTERM after 30s; sending SIGKILL" | tee -a "$OUTDIR/deadline.log"
      kill -KILL "$PID" 2>/dev/null
    fi
    break
  fi
  sleep 2
done

wait "$PID"
EXIT_CODE=$?
END=$(date +%s)

# Our children may still be alive (e.g. dataloader workers); only report, never
# kill anything we did not start.
LEFTOVER=$(pgrep -P "$PID" 2>/dev/null | tr '\n' ' ' || true)

# Wait for the card to actually come back. "Our pid left the compute list" is not
# enough: the allocator releases a 12 GB model a few seconds later, and a second
# process started in that window dies with OOM. Poll device-level usage instead.
VRAM_FREE_AFTER=""
DEVICE_USED_AFTER=""
if [ "$GPU_WAIT" -eq 1 ] && command -v nvidia-smi >/dev/null 2>&1; then
  GPU_START=$(date +%s)
  TOTAL=$(gpu_total_mib)
  for _ in $(seq 1 150); do
    USED=$(gpu_used_mib)
    if [ -n "$USED" ]; then
      DEVICE_USED_AFTER="$USED"
      if [ "$FREE_GB" -gt 0 ]; then
        [ $(( TOTAL - USED )) -ge $(( FREE_GB * 1024 )) ] && break
      else
        # Without an explicit target, wait until our pid is gone and usage has
        # been stable for two consecutive samples.
        if ! nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -qw "$PID"; then
          sleep 2
          USED2=$(gpu_used_mib)
          [ "$USED" = "$USED2" ] && break
        fi
      fi
    fi
    sleep 2
  done
  VRAM_FREE_AFTER=$(( $(date +%s) - GPU_START ))
fi

# `lerobot_train` refuses to start if its output directory already exists, so the
# timeline cannot live inside it. Scan a separate directory for checkpoints.
CKPT_SCAN="${CKPT_DIR:-$OUTDIR}"
LAST_CKPT=""
if [ -d "$CKPT_SCAN/checkpoints" ]; then
  LAST_CKPT=$(ls -1d "$CKPT_SCAN"/checkpoints/*/ 2>/dev/null | sort | tail -1)
fi

cat > "$OUTDIR/timeline.json" <<JSON
{
  "start_epoch": $START,
  "end_epoch": $END,
  "wall_seconds": $(( END - START )),
  "deadline_sec": $DEADLINE_SEC,
  "deadline_fired": $KILLED,
  "exit_code": $EXIT_CODE,
  "pid": $PID,
  "leftover_children": "$LEFTOVER",
  "vram_free_after_seconds": "${VRAM_FREE_AFTER}",
  "device_used_mib_after": "${DEVICE_USED_AFTER}",
  "pregate_wait_seconds": $PREWAIT,
  "free_gb_target": $FREE_GB,
  "gpu_wait_enabled": $GPU_WAIT,
  "ckpt_dir": "$CKPT_SCAN",
  "last_checkpoint": "$LAST_CKPT",
  "log": "$LOG"
}
JSON

echo "exit_code=$EXIT_CODE wall=$(( END - START ))s deadline_fired=$KILLED vram_free_after=${VRAM_FREE_AFTER:-n/a}s"
exit "$EXIT_CODE"
