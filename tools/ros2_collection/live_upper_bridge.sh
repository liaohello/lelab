#!/usr/bin/env bash
# Live upper-computer DDS bridge launcher (real-hardware path, NOT bin/dds_r1).
#
# Runs INSIDE the lelab-humble-dev container (host network). Sources ROS Humble
# and the amd64 lelab_mobile_interfaces overlay, then runs the upper bridge with
# its flow services ENABLED. The bridge:
#   * copies typed car feedback/health/window/receipt to a unix socket the upper
#     FastAPI app reads (no actuator topic is ever published here);
#   * exposes ~/session_control, ~/clear_estop and the ~/home_arm action, which
#     are thin forwards to the upper app HTTP routes (--app-url);
#   * only emits authenticated dds_* command messages via local socket ops.
#
# Usage (inside the container):  live_upper_bridge.sh start|stop|status
#   OUT=/work/out/dds_r1  APP_URL=http://127.0.0.1:18288  KEY=$OUT/command.key
set -o pipefail
set +u

OUT="${OUT:-/work/out/dds_r1}"
AGENT_SRC="${AGENT_SRC:-/work/lelab/ros2_ws/src/lelab_mobile_agent}"
APP_URL="${APP_URL:-http://127.0.0.1:18288}"
SOCKET="$OUT/live_bridge.sock"
PIDFILE="$OUT/upper_bridge.pid"
LOG="$OUT/upper_bridge.log"
KEY="${KEY:-$OUT/command.key}"
CYCLONE="${CYCLONE:-$OUT/cyclonedds-upper.xml}"

source /opt/ros/humble/setup.bash
source "$OUT/ws/install/setup.bash"
export PYTHONPATH="$AGENT_SRC${PYTHONPATH:+:$PYTHONPATH}"
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=0
export ROS_LOCALHOST_ONLY=0
export CYCLONEDDS_URI="file://$CYCLONE"

cmd="${1:-status}"
case "$cmd" in
  start)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "already running pid=$(cat "$PIDFILE")"; exit 0
    fi
    setsid nohup python3 -m lelab_mobile_agent.upper_dds_bridge \
      --socket "$SOCKET" --socket-uid 1000 --socket-gid 1000 \
      --app-url "$APP_URL" --command-key-file "$KEY" \
      >"$LOG" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    sleep 1.5
    if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "started pid=$(cat "$PIDFILE") app_url=$APP_URL"
    else
      echo "failed to start; log:"; tail -n 20 "$LOG"; exit 1
    fi
    ;;
  stop)
    if [ -f "$PIDFILE" ]; then
      p=$(cat "$PIDFILE")
      if kill -0 "$p" 2>/dev/null; then kill -TERM "$p"; echo "sent TERM to $p"; fi
      rm -f "$PIDFILE"
    else
      echo "no pidfile"
    fi
    ;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "running pid=$(cat "$PIDFILE")"
    else
      echo "stopped"
    fi
    ;;
  *) echo "usage: $0 start|stop|status"; exit 2 ;;
esac