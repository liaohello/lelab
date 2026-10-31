#!/usr/bin/env bash
# Deploy the lightweight read-only agent into the existing Pi ROS container.
# No system packages, Python environments, drivers or containers are installed.
#
# Usage:
#   PI=pi@192.168.19.138 tools/ros2_collection/deploy_readonly_agent.sh --check
#   PI=pi@192.168.19.138 tools/ros2_collection/deploy_readonly_agent.sh --apply
#   PI=pi@192.168.19.138 tools/ros2_collection/deploy_readonly_agent.sh --stop
#   PI=pi@192.168.19.138 tools/ros2_collection/deploy_readonly_agent.sh --rollback
#
# Authentication is delegated to the user's existing SSH configuration.
# This script never reads, prints or stores a password or token.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PI=${PI:-pi@192.168.19.138}
CONTAINER=${CONTAINER:-MentorPi}
REMOTE_DIR=${REMOTE_DIR:-/home/ubuntu/lelab_car}
REMOTE_UID=${REMOTE_UID:-1000}
REMOTE_GID=${REMOTE_GID:-1000}
MODE=${1:---check}
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)
AGENT_SRC="$REPO_ROOT/ros2_ws/src/lelab_mobile_agent"
PROFILE="$REPO_ROOT/configs/ros2_collection/hardware-readonly-v1.json"
TMPDIR_LOCAL=$(mktemp -d -t lelab_readonly_deploy.XXXXXX)
trap 'rm -rf "$TMPDIR_LOCAL"' EXIT

log() { printf '[readonly-deploy] %s\n' "$*"; }
remote() { ssh "${SSH_OPTS[@]}" "$PI" "$@"; }
dexec() { remote "docker exec ${DOCKER_EXEC_USER:-} '$CONTAINER' bash -lc $(printf '%q' "$1")"; }

preflight() {
  remote "hostname; uname -m; date -u +%FT%TZ; df -h / | tail -1" || {
    log "Pi is not reachable via SSH; no remote changes made."; return 1;
  }
  remote "docker inspect -f '{{.State.Running}} {{.Config.Image}} {{.HostConfig.NetworkMode}}' '$CONTAINER'" || {
    log "container '$CONTAINER' is unavailable; no changes made."; return 1;
  }
  dexec "python3 - <<'PY'
mods = {}
for name in ('rclpy','numpy','PIL','cv2','rosbag2_py'):
    try:
        __import__(name); mods[name] = True
    except Exception as exc:
        mods[name] = f'{type(exc).__name__}: {exc}'
print(mods)
if not mods.get('rclpy') or not mods.get('numpy'):
    raise SystemExit('rclpy and numpy are required in the existing container')
if not (mods.get('PIL') or mods.get('cv2')):
    raise SystemExit('PIL or cv2 is required for the preview encoder')
PY"
  dexec "ros2 bag record --help 2>&1 | grep -q sqlite3" || log "warning: sqlite3 storage not visible in this container"
  dexec "ros2 topic list --no-daemon 2>/dev/null | grep -E '^/ascamera/camera_publisher/rgb0/image$|^/ascamera/camera_publisher/rgb0/camera_info$'" || {
    log "warning: configured camera topics are not currently visible; start the sensor driver first.";
  }
}

package_payload() {
  cp -a "$AGENT_SRC/lelab_mobile_agent" "$TMPDIR_LOCAL/"
  mkdir -p "$TMPDIR_LOCAL/config"
  cp "$PROFILE" "$TMPDIR_LOCAL/config/"
  cp "$REPO_ROOT/tools/ros2_collection/probe_real_sensors.py" "$TMPDIR_LOCAL/"
  cat > "$TMPDIR_LOCAL/run_readonly.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH=__REMOTE_DIR__
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-0}
exec python3 -m lelab_mobile_agent.agent --ros-args \
  -p profile_path:=__REMOTE_DIR__/config/hardware-readonly-v1.json \
  -p car_root:=__REMOTE_DIR__ \
  -p host:=127.0.0.1 -p port:=${LELAB_READONLY_PORT:-8110} -p use_sim_time:=false
SH
  sed -i "s|__REMOTE_DIR__|$REMOTE_DIR|g" "$TMPDIR_LOCAL/run_readonly.sh"
  chmod +x "$TMPDIR_LOCAL/run_readonly.sh"
  tar -C "$TMPDIR_LOCAL" -czf "$TMPDIR_LOCAL/payload.tar.gz" lelab_mobile_agent config probe_real_sensors.py run_readonly.sh
}

apply() {
  preflight
  package_payload
  scp -q "${SSH_OPTS[@]}" "$TMPDIR_LOCAL/payload.tar.gz" "$PI:/tmp/lelab_readonly_payload.tar.gz"
  remote "docker cp /tmp/lelab_readonly_payload.tar.gz '$CONTAINER:/tmp/lelab_readonly_payload.tar.gz'"
  dexec "mkdir -p '$REMOTE_DIR' && tar -xzf /tmp/lelab_readonly_payload.tar.gz -C '$REMOTE_DIR' && chown -R '$REMOTE_UID:$REMOTE_GID' '$REMOTE_DIR' && rm -f /tmp/lelab_readonly_payload.tar.gz"
  dexec "cd '$REMOTE_DIR' && nohup ./run_readonly.sh > readonly_agent.log 2>&1 & echo \$! > readonly_agent.pid"
  sleep 4
  remote "python3 - <<'PY'
import json, urllib.request
try:
    data = json.load(urllib.request.urlopen('http://127.0.0.1:8110/health', timeout=5))
    print('health:', data)
except Exception as exc:
    raise SystemExit(f'agent health failed: {exc}')
PY"
  log "started; verify with: ssh -N -L 8110:127.0.0.1:8110 $PI"
}

stop() {
  remote "docker exec '$CONTAINER' bash -lc 'if [ -f \"$REMOTE_DIR/readonly_agent.pid\" ]; then kill \$(cat \"$REMOTE_DIR/readonly_agent.pid\") 2>/dev/null || true; rm -f \"$REMOTE_DIR/readonly_agent.pid\"; fi'"
  log "stop signal sent; sessions and logs remain in $REMOTE_DIR"
}

rollback() {
  stop
  remote "docker exec '$CONTAINER' bash -lc 'rm -rf \"$REMOTE_DIR/lelab_mobile_agent\" \"$REMOTE_DIR/config\" \"$REMOTE_DIR/probe_real_sensors.py\" \"$REMOTE_DIR/run_readonly.sh\"'"
  log "removed deployed code/profile only; $REMOTE_DIR/sessions and logs were preserved"
}

case "$MODE" in
  --check) preflight ;;
  --apply) apply ;;
  --stop) stop ;;
  --rollback) rollback ;;
  *) echo "usage: $0 --check|--apply|--stop|--rollback" >&2; exit 2 ;;
esac
