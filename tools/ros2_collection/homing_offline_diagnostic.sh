#!/usr/bin/env bash
# OFFline continuous homing diagnostic: isolated Humble rig + production upper facade.
#
# Starts a dedicated Docker stack (own container name, domain, ports and work dir) so
# it never touches another offline/real rig, then runs the host-side diagnostic that
# drives read-only preflight -> session -> bounded heartbeat -> only_joint homing ->
# cancel -> release. Everything is simulated: no serial device, no real board, every
# artefact tagged simulated_offline.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$ROOT/work/ros2_collection/homing_offline/local_humble"
CAL_ROOT="$ROOT/work/ros2_collection/homing_offline/calibration"
EVID="$ROOT/docs/evidence/landerpi-offline-integration-20261106"
PY="$ROOT/app_ros_mobile/.venv/bin/python"
DOCKER="$ROOT/tools/ros2_collection/local_ros2/bin/docker"
DIAG="$ROOT/tools/ros2_collection/homing_offline_diagnostic.py"
PROFILE="/work/lelab/ros2_ws/src/lelab_mobile_agent/config/hardware-hybrid-homing-v1-offline.json"
CONTAINER="lelab-homing-offline"
DOMAIN_ID="${LROS_DIAG_DOMAIN_ID:-94}"
STREAM_PORT="${LROS_DIAG_STREAM_PORT:-18123}"
AGENT_PORT="${LROS_DIAG_AGENT_PORT:-18131}"
BOARD_RAW="${LROS_DIAG_BOARD_RAW:-joint1=527,joint2=632,joint3=76,joint4=118,joint5=907,r_joint=457}"

# docker group membership is not inherited by every shell; re-exec through sg once.
if ! docker info >/dev/null 2>&1; then
  cmd="$(printf '%q ' "$0" "$@")"
  exec sg docker -c "$cmd"
fi

mkdir -p "$CAL_ROOT" "$EVID"
# Fresh fixture versions make the bundle deterministic for this run: print-bundle
# creates version 1, the diagnostic reads it back without saving again.
rm -rf "$CAL_ROOT"
mkdir -p "$CAL_ROOT"
BUNDLE="$("$PY" "$DIAG" --print-bundle --calibration-root "$CAL_ROOT" \
  | "$PY" -c 'import json,sys; d=json.load(sys.stdin); assert d["ready"], d["reasons"]; print(d["bundle_hash"])')" || {
    echo "refusing: calibration fixture is not a ready three-layer bundle" >&2; exit 2; }

started=0
cleanup() {
  if [ "$started" = "1" ]; then
    LROS_WORK_DIR="$WORK" LROS_CONTAINER="$CONTAINER" "$DOCKER" down >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "[diagnostic] starting isolated rig: container=$CONTAINER domain=$DOMAIN_ID" \
     "stream=$STREAM_PORT agent=$AGENT_PORT bundle=${BUNDLE:0:12}..."
started=1
LROS_WORK_DIR="$WORK" \
LROS_CONTAINER="$CONTAINER" \
LROS_DOCKER_DOMAIN_ID="$DOMAIN_ID" \
LROS_DOCKER_STREAM_PORT="$STREAM_PORT" \
LROS_DOCKER_AGENT_PORT="$AGENT_PORT" \
LROS_DOCKER_PROFILE="$PROFILE" \
LROS_DOCKER_BUNDLE="$BUNDLE" \
LROS_DOCKER_BOARD_RAW="$BOARD_RAW" \
  "$DOCKER" up || { echo "[diagnostic] rig failed to start" >&2; exit 3; }

# Wait for the agent HTTP surface (bounded).
deadline=$(( $(date +%s) + 60 ))
until curl -fsS -m 2 "http://127.0.0.1:$AGENT_PORT/health" >/dev/null 2>&1; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "[diagnostic] agent did not become healthy" >&2
    exit 3
  fi
  sleep 0.5
done

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT="$EVID/homing-offline-diagnostic-$STAMP.json"
"$PY" "$DIAG" --agent-url "http://127.0.0.1:$AGENT_PORT" --frame-port "$STREAM_PORT" \
  --calibration-root "$CAL_ROOT" --bundle "$BUNDLE" --report "$REPORT" \
  --only-joint joint1
code=$?
echo "[diagnostic] report=$REPORT exit=$code"
exit "$code"