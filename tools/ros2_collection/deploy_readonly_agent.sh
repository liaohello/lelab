#!/usr/bin/env bash
# Deploy the lightweight read-only agent into the existing Pi ROS container.
# No system packages, Python environments, drivers or containers are installed.
#
# Usage:
#   PI=user@car-host tools/ros2_collection/deploy_readonly_agent.sh --check
#   PI=user@car-host tools/ros2_collection/deploy_readonly_agent.sh --apply
#   PI=user@car-host tools/ros2_collection/deploy_readonly_agent.sh --stop
#   PI=user@car-host tools/ros2_collection/deploy_readonly_agent.sh --rollback
#   PI=user@car-host tools/ros2_collection/deploy_readonly_agent.sh --render
#
# The car profile is chosen by name and defaults to the already accepted read-only one; running the
# teleop profile is an explicit act (PROFILE_NAME=hardware-teleop-v1 ... --apply) and never widens
# the read-only profile. --render prints what would ship without touching the Pi.
#
# Authentication is delegated to the user's existing SSH configuration.
# This script never reads, prints or stores a password or token.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# The car uses DHCP. Set PI only after verifying the current device identity and
# SSH host key; never commit the discovered address or hardware identifiers.
PI=${PI:-}
CONTAINER=${CONTAINER:-MentorPi}
REMOTE_DIR=${REMOTE_DIR:-/home/ubuntu/lelab_car}
REMOTE_UID=${REMOTE_UID:-1000}
REMOTE_GID=${REMOTE_GID:-1000}
DOCKER_EXEC_USER=${DOCKER_EXEC_USER:--u ubuntu}
MODE=${1:---check}
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)
AGENT_SRC="$REPO_ROOT/ros2_ws/src/lelab_mobile_agent"
CONFIG_DIR="$REPO_ROOT/configs/ros2_collection"
PROFILE_NAME=${PROFILE_NAME:-hardware-readonly-v1}
if [[ ! "$PROFILE_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "REFUSED: PROFILE_NAME must be a bare profile file name without a path (got '$PROFILE_NAME')" >&2
  exit 2
fi
PROFILE="$CONFIG_DIR/$PROFILE_NAME.json"
if [ ! -f "$PROFILE" ]; then
  echo "REFUSED: no such car profile: $PROFILE" >&2
  exit 2
fi
# The car container only loads ROS from the vendor's interactive shell, so every
# non-interactive exec has to source it explicitly. Missing files are skipped.
ROS_SOURCE=${ROS_SOURCE:-'/opt/ros/humble/setup.bash /home/ubuntu/ros2_ws/install/setup.bash'}
source_prefix() {
  local prefix='for f in'
  local file
  for file in $ROS_SOURCE; do prefix+=" $file"; done
  prefix+='; do [ -f "$f" ] && source "$f"; done; '
  printf '%s' "$prefix"
}
ROS_PREFIX=$(source_prefix)
TMPDIR_LOCAL=$(mktemp -d -t lelab_readonly_deploy.XXXXXX)
trap 'rm -rf "$TMPDIR_LOCAL"' EXIT

log() { printf '[readonly-deploy] %s\n' "$*"; }
require_target() {
  if [ -z "$PI" ]; then
    log "REFUSED: set PI=user@car-host after verifying the current address and SSH host key" >&2
    return 2
  fi
}
remote() { require_target || return $?; ssh "${SSH_OPTS[@]}" "$PI" "$@"; }
dexec() { remote "docker exec ${DOCKER_EXEC_USER:-} '$CONTAINER' bash -lc $(printf '%q' "$ROS_PREFIX$1")"; }

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
  dexec "ros2 topic list 2>/dev/null | grep -E '^/ascamera/camera_publisher/rgb0/image$|^/ascamera/camera_publisher/rgb0/camera_info$'" || {
    log "warning: configured camera topics are not currently visible; start the sensor driver first.";
  }
}

package_payload() {
  cp -a "$AGENT_SRC/lelab_mobile_agent" "$TMPDIR_LOCAL/"
  mkdir -p "$TMPDIR_LOCAL/config"
  cp "$PROFILE" "$TMPDIR_LOCAL/config/"
  cp "$REPO_ROOT/tools/ros2_collection/probe_real_sensors.py" "$TMPDIR_LOCAL/"
  cat > "$TMPDIR_LOCAL/run_readonly.sh" <<SH
#!/usr/bin/env bash
set -euo pipefail
# ROS setup scripts reference unset AMENT variables, so -u must be off while sourcing.
set +u
for f in $ROS_SOURCE; do [ -f "\$f" ] && source "\$f"; done
set -u
unset CYCLONEDDS_URI
export PYTHONPATH=__REMOTE_DIR__\${PYTHONPATH:+:\$PYTHONPATH}
export ROS_DOMAIN_ID=\${ROS_DOMAIN_ID:-0}
export ROS_LOCALHOST_ONLY=1
export RMW_IMPLEMENTATION=\${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}
# This script execs into python, so \$\$ is the exact agent pid.
echo \$\$ > __REMOTE_DIR__/readonly_agent.pid
exec python3 -m lelab_mobile_agent.agent --ros-args \
  -p profile_path:=__REMOTE_DIR__/config/__PROFILE_NAME__.json \
  -p car_root:=__REMOTE_DIR__ \
  -p host:=127.0.0.1 -p port:=\${LELAB_READONLY_PORT:-8110} -p use_sim_time:=false
SH
  sed -i "s|__REMOTE_DIR__|$REMOTE_DIR|g; s|__PROFILE_NAME__|$PROFILE_NAME|g" "$TMPDIR_LOCAL/run_readonly.sh"
  chmod +x "$TMPDIR_LOCAL/run_readonly.sh"
  tar -C "$TMPDIR_LOCAL" -czf "$TMPDIR_LOCAL/payload.tar.gz" lelab_mobile_agent config probe_real_sensors.py run_readonly.sh
}

apply() {
  log "deploying car profile: $PROFILE_NAME"
  preflight
  package_payload
  scp -q "${SSH_OPTS[@]}" "$TMPDIR_LOCAL/payload.tar.gz" "$PI:/tmp/lelab_readonly_payload.tar.gz"
  remote "docker cp /tmp/lelab_readonly_payload.tar.gz '$CONTAINER:/tmp/lelab_readonly_payload.tar.gz'"
  dexec "mkdir -p '$REMOTE_DIR' && tar -xzf /tmp/lelab_readonly_payload.tar.gz -C '$REMOTE_DIR' && chown -R '$REMOTE_UID:$REMOTE_GID' '$REMOTE_DIR/lelab_mobile_agent' '$REMOTE_DIR/config' && chown '$REMOTE_UID:$REMOTE_GID' '$REMOTE_DIR/probe_real_sensors.py' '$REMOTE_DIR/run_readonly.sh'"
  remote "docker exec '$CONTAINER' rm -f /tmp/lelab_readonly_payload.tar.gz"
  dexec "{ cd '$REMOTE_DIR' && nohup ./run_readonly.sh > readonly_agent.log 2>&1 & }"
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
  --render)
    package_payload
    log "profile to ship: $PROFILE_NAME ($PROFILE)"
    sed -n '1,80p' "$TMPDIR_LOCAL/run_readonly.sh"
    ;;
  --check) log "car profile: $PROFILE_NAME"; preflight ;;
  --apply) apply ;;
  --stop) stop ;;
  --rollback) rollback ;;
  *) echo "usage: $0 --check|--apply|--stop|--rollback|--render" >&2; exit 2 ;;
esac
