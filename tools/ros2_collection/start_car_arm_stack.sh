#!/usr/bin/env bash
# Minimal car-side startup entry for real follower feedback (arm + gripper servos).
#
# It starts ONLY these two vendor nodes inside the existing ROS container:
#   ros_robot_controller  -> owns the control-board UART (/dev/rrc), serves GetBusServoState
#   servo_controller      -> node name controller_manager, publishes /controller_manager/joint_states
#                            and hosts /arm_controller + /gripper_controller FollowJointTrajectory
#
# It deliberately does NOT use bringup.launch.py / controller.launch.py: those also start
# init_pose (moves the arm), odom_publisher, robot_localization/ekf_node and behavior apps.
#
# IMPORTANT: starting ros_robot_controller is NOT "connect only". Its constructor performs
# writes to the control board (see WRITE_SIDEEFFECTS), so --apply requires the explicit flag
# --i-authorize-board-writes. No credential is ever read, printed or stored.
#
# Usage:
#   PI=pi@<verified-current-address> tools/ros2_collection/start_car_arm_stack.sh --check
#   PI=pi@<verified-current-address> tools/ros2_collection/start_car_arm_stack.sh --apply --i-authorize-board-writes
#   PI=pi@<verified-current-address> tools/ros2_collection/start_car_arm_stack.sh --status
#   PI=pi@<verified-current-address> tools/ros2_collection/start_car_arm_stack.sh --stop
#
# Process management is restricted to the session ids this script recorded. It never uses
# pkill/killall. Set LELAB_STACK_DRYRUN=1 to print remote commands without executing them.
set -euo pipefail

# The car uses DHCP. Never contact a remembered address: set PI only after the current device
# identity and SSH host key are verified. --render remains local and does not require PI.
PI=${PI:-}
CONTAINER=${CONTAINER:-MentorPi}
DOCKER_EXEC_USER=${DOCKER_EXEC_USER:--u ubuntu}
REMOTE_DIR=${REMOTE_DIR:-/home/ubuntu/lelab_car}
STATE_DIR=${STATE_DIR:-$REMOTE_DIR/car_arm_stack}
BOARD_DEVICE=${BOARD_DEVICE:-/dev/rrc}
FEEDBACK_TOPIC=${FEEDBACK_TOPIC:-/controller_manager/joint_states}
JOINTS=${JOINTS:-joint1 joint2 joint3 joint4 joint5 r_joint}
EXPECTED_RATE_HZ=${EXPECTED_RATE_HZ:-50}
SERVICE_TIMEOUT_S=${SERVICE_TIMEOUT_S:-5.0}
MEASURE_SECONDS=${MEASURE_SECONDS:-4.0}
START_CAMERA=${START_CAMERA:-0}
ROBOT_CONTROLLER_PYTHONPATH=${ROBOT_CONTROLLER_PYTHONPATH:-}
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes)
# The vendor container only loads ROS from an interactive login shell, so every
# non-interactive exec must source it explicitly. Missing files are skipped.
ROS_SOURCE=${ROS_SOURCE:-'/home/ubuntu/ros2_ws/.typerc /opt/ros/humble/setup.bash /home/ubuntu/ros2_ws/install/setup.bash /home/ubuntu/third_party_ros2/third_party_ws/install/setup.bash'}
DRYRUN=${LELAB_STACK_DRYRUN:-0}
# Candidate locations of the servo parameter file (installed share dir first, vendor source
# second). The controller_manager node cannot build joint configs without it.
PARAMS_CANDIDATES=('$PREFIX/share/servo_controller/config/servo_controller.yaml'
                  '/home/ubuntu/ros2_ws/src/driver/servo_controller/config/servo_controller.yaml')

MODE=${1:---check}
shift || true
AUTHORIZE=0
for arg in "$@"; do
  case "$arg" in
    --i-authorize-board-writes) AUTHORIZE=1 ;;
    *) printf '[car-arm-stack] unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

WRITE_SIDEEFFECTS='ros_robot_controller.__init__: board.enable_reception(); load_servo_offsets()
  from YAML (may reach servo EEPROM); send_machine_type(); LED write when
  /home/ubuntu/shared/.networkmode.txt exists; board.set_motor_speed([[1,0],[2,0],[3,0],[4,0]])
  chassis zero-speed. controller_manager.__init__: ServoManager.connect() (read) and
  client.wait_for_service() with NO timeout, hence this script starts and finite-waits for
  ros_robot_controller first.'

log() { printf '[car-arm-stack] %s\n' "$*"; }
require_pi() {
  if [ -z "$PI" ]; then
    log "REFUSED: set PI to the currently verified car address and verify its SSH host key first" >&2
    return 2
  fi
}
remote() {
  require_pi || return $?
  if [ "$DRYRUN" = 1 ]; then printf '[dryrun] ssh ... %s\n' "$1"; return 0; fi
  ssh "${SSH_OPTS[@]}" "$PI" "$@"
}
# `set +u` is required because the ROS setup scripts reference unset variables, and the vendor
# install overlay must be sourced before ros2 commands work in a non-interactive shell.
source_prefix() {
  local prefix='set +u; ' file
  for file in $ROS_SOURCE; do prefix+="[ -f '$file' ] && source '$file'; "; done
    prefix+='unset CYCLONEDDS_URI; export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}"; export ROS_LOCALHOST_ONLY=1; '
  printf '%s' "$prefix"
}
ROS_PREFIX=$(source_prefix)
dexec() {
  if [ "$DRYRUN" = 1 ]; then printf '[dryrun] docker exec %s %s\n' "${DOCKER_EXEC_USER:-}" "$1"; return 0; fi
  remote "docker exec ${DOCKER_EXEC_USER:-} '$CONTAINER' bash -lc $(printf '%q' "$ROS_PREFIX$1")"
}

banner() {
  log "mode=$MODE target=$PI container=$CONTAINER state_dir=$STATE_DIR dryrun=$DRYRUN"
  log "writes performed by --apply: $WRITE_SIDEEFFECTS"
}

# Read-only audit over /proc: who holds the control-board UART, and which vendor nodes are
# already running (co-control risk). Deliberately avoids pkill/pgrep so it can never match
# its own command line or another task's process.
BUS_AUDIT_PY='
import glob, os, sys
PROC = os.environ.get("PROC_ROOT", "/proc")
def cmdline(pid):
    try:
        with open("%s/%s/cmdline" % (PROC, pid), "rb") as fh:
            # Decode before joining: /proc/PID/cmdline is NUL-separated bytes, and
            # " ".join() over a list of bytes objects raises TypeError — which would
            # abort the whole audit at the very first process and leave the UART
            # ownership question unanswered.
            return fh.read().decode("utf-8", "replace").replace("\0", " ").strip()
    except OSError:
        return ""
def ancestry(pid):
    # The audit is piped in as `bash -lc ... python3 - <<PY`, so its own parent chain
    # carries every needle in the heredoc text. Matching that reported the wrapper as a
    # running vendor node (measured on the car 2026-11-03); walking PPid and skipping the
    # whole chain keeps the audit from being its own evidence.
    seen = set()
    current = str(pid)
    while current not in seen:
        seen.add(current)
        try:
            with open("%s/%s/status" % (PROC, current)) as fh:
                current = [line.split()[1] for line in fh if line.startswith("PPid:")][0]
        except (OSError, IndexError):
            break
    return seen
def pid_of(path):
    # Take the number straight after PROC instead of a fixed "/proc/PID/…" index, so the
    # audit still works when PROC_ROOT points somewhere else (the tests build a fake tree).
    return path[len(PROC) + 1:].split("/")[0]
device = sys.argv[1]
real = os.path.realpath(device) if os.path.exists(device) else None
holders = []
if real:
    for fd in glob.glob("%s/[0-9]*/fd/*" % PROC):
        try:
            if os.path.realpath(fd) == real:
                holders.append([pid_of(fd), cmdline(pid_of(fd))[:120]])
        except OSError:
            pass
needles = ("ros_robot_controller", "controller_manager", "joystick", "gamepad", "sbus",
           "init_pose", "arm_init", "odom_publisher", "ekf_node")
skip = ancestry(os.environ.get("AUDIT_SELF_PID") or os.getpid())
nodes = []
for path in glob.glob("%s/[0-9]*/cmdline" % PROC):
    pid = pid_of(path)
    if pid in skip:
        continue
    args = cmdline(pid)
    hits = [n for n in needles if n in args and ("ros2" in args or "python" in args)]
    if hits:
        nodes.append({"pid": pid, "matched": hits, "cmd": args[:160]})
print("self_ancestry_excluded=%s" % sorted(skip))
print("uart_device=%s present=%s holders=%s" % (device, os.path.exists(device), holders or "none"))
print("vendor_nodes_running=%s" % (nodes or "none"))
print("co_control_risk=%s" % any(n["matched"][0] not in ("ros_robot_controller", "controller_manager")
                                 for n in nodes))
'

resolve_params() {
  # Stores SERVO_PARAMS=<resolved path> in our own state dir so the wrappers and --status agree
  # on one file instead of re-guessing it at every step.
  dexec "mkdir -p '$STATE_DIR'
prefix=\$(ros2 pkg prefix servo_controller 2>/dev/null || true)
resolved=''
for c in ${PARAMS_CANDIDATES[0]//\$PREFIX/\$prefix} ${PARAMS_CANDIDATES[1]}; do
  [ -n \"\$c\" ] && [ -f \"\$c\" ] && resolved=\"\$c\" && break
done
if [ -z \"\$resolved\" ]; then rm -f '$STATE_DIR/params.env'; echo 'servo params yaml NOT FOUND'; exit 1; fi
printf 'SERVO_PARAMS=%s\n' \"\$resolved\" > '$STATE_DIR/params.env'
echo \"servo_params=\$resolved\""
}

probe_params() {
  # Read-only counterpart to resolve_params: --check must not create remote state.
  dexec "prefix=\$(ros2 pkg prefix servo_controller 2>/dev/null || true)
resolved=''
for c in ${PARAMS_CANDIDATES[0]//\$PREFIX/\$prefix} ${PARAMS_CANDIDATES[1]}; do
  [ -n \"\$c\" ] && [ -f \"\$c\" ] && resolved=\"\$c\" && break
done
if [ -z \"\$resolved\" ]; then echo 'servo params yaml NOT FOUND'; exit 1; fi
printf 'servo_params_candidate=%s\\n' \"\$resolved\""
}

check() {
  require_pi || return $?
  log "checking reachability (read-only)"
  remote "hostname; date -u +%FT%TZ; docker inspect -f '{{.State.Running}} {{.Config.Image}}' '$CONTAINER'" || {
    log "Pi or container unavailable; nothing was changed."; return 1; }
  dexec "printf 'machine_type=%s\n' \"\${MACHINE_TYPE:-UNSET}\"; python3 -V; \
printf 'ros_distro=%s ros_domain_id=%s\n' \"\${ROS_DISTRO:-unset}\" \"\${ROS_DOMAIN_ID:-unset}\""
  dexec "for p in ros_robot_controller servo_controller; do printf 'pkg_prefix %s=' \$p; \
ros2 pkg prefix \$p 2>/dev/null || printf 'MISSING\n'; done"
  probe_params
  dexec "python3 - '$BOARD_DEVICE' <<'PY'
$BUS_AUDIT_PY
PY"
  log "joints expected in feedback: $JOINTS (w_joint only exists when MACHINE_TYPE=JetRover_Acker)"
  log "check is read-only; it does not create state files or write to the board; --apply is not implied"
}

# Per-node wrapper: records its own session id BEFORE exec'ing the node, so a later --stop can
# only ever signal a process group this script created. Refuses to run if the params file is
# unresolved or MACHINE_TYPE is unset (the vendor node raises KeyError instead of failing clean).
wrapper() {
  local name=$1 exec_line=$2
  cat <<SH
#!/usr/bin/env bash
set +u
echo \$\$ > '$STATE_DIR/$name.pid'
for f in $ROS_SOURCE; do [ -f "\$f" ] && source "\$f"; done
unset CYCLONEDDS_URI
export RMW_IMPLEMENTATION="\${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}"
[ -f '$STATE_DIR/params.env' ] && source '$STATE_DIR/params.env'
  [ -n '$ROBOT_CONTROLLER_PYTHONPATH' ] && export PYTHONPATH='$ROBOT_CONTROLLER_PYTHONPATH'\${PYTHONPATH:+:\$PYTHONPATH}
[ -n "\${SERVO_PARAMS:-}" ] || { echo 'missing SERVO_PARAMS - run --check first'; exit 1; }
[ -n "\${MACHINE_TYPE:-}" ] || { echo 'MACHINE_TYPE is unset - ros_robot_controller would crash'; exit 1; }
export ROS_DOMAIN_ID=\${ROS_DOMAIN_ID:-0}
export ROS_LOCALHOST_ONLY=1
cd '$REMOTE_DIR'
exec $exec_line
SH
}

TMPDIR_LOCAL=$(mktemp -d -t lelab_car_arm_stack.XXXXXX)
trap 'rm -rf "$TMPDIR_LOCAL"' EXIT

build_wrappers() {
  wrapper controller_manager \
    'ros2 run servo_controller servo_controller --ros-args --params-file "$SERVO_PARAMS" -p base_frame:=base_footprint' \
    > "$TMPDIR_LOCAL/controller_manager.sh"
  wrapper ros_robot_controller 'python3 -m ros_robot_controller.ros_robot_controller_node' \
    > "$TMPDIR_LOCAL/ros_robot_controller.sh"
  if [ "$START_CAMERA" = 1 ]; then
    wrapper camera 'ros2 launch peripherals depth_camera.launch.py' \
      > "$TMPDIR_LOCAL/camera.sh"
  fi
  chmod +x "$TMPDIR_LOCAL"/*.sh
  log "wrappers built locally: $(basename "$TMPDIR_LOCAL")/{controller_manager,ros_robot_controller}.sh"
  if [ "$START_CAMERA" = 1 ]; then log "camera wrapper enabled"; fi
}

apply() {
  if [ "$AUTHORIZE" != 1 ]; then
    log "REFUSED: ros_robot_controller writes to the control board (enable_reception, servo"
    log "offsets from YAML, machine type, LED, chassis zero-speed)."
    log "Re-run with --i-authorize-board-writes once you accept those writes."
    return 78
  fi
  require_pi || return $?
  check
  resolve_params
  build_wrappers
  names='ros_robot_controller controller_manager'
  if [ "$START_CAMERA" = 1 ]; then names="$names camera"; fi
  for name in $names; do
    remote "cat > /tmp/lelab_stack_$name.sh" < "$TMPDIR_LOCAL/$name.sh"
    remote "docker cp /tmp/lelab_stack_$name.sh '$CONTAINER:$STATE_DIR/$name.sh' && rm -f /tmp/lelab_stack_$name.sh"
    dexec "chmod +x '$STATE_DIR/$name.sh'"
  done
  log "starting ros_robot_controller (this performs the board writes listed above)"
  dexec "nohup setsid '$STATE_DIR/ros_robot_controller.sh' > '$STATE_DIR/ros_robot_controller.log' 2>&1 < /dev/null &"
  wait_init_finish
  log "starting controller_manager (servo_controller)"
  dexec "nohup setsid '$STATE_DIR/controller_manager.sh' > '$STATE_DIR/controller_manager.log' 2>&1 < /dev/null &"
  sleep "$([ "$DRYRUN" = 1 ] && echo 0 || echo 3)"
  if [ "$START_CAMERA" = 1 ]; then
    log "starting camera only (no full bringup)"
    dexec "nohup setsid '$STATE_DIR/camera.sh' > '$STATE_DIR/camera.log' 2>&1 < /dev/null &"
    sleep "$([ "$DRYRUN" = 1 ] && echo 0 || echo 3)"
  fi
  status
}

# Finite-timeout wait for /ros_robot_controller/init_finish. controller_manager would block
# forever in wait_for_service() without it, so this gate is a hard prerequisite.
wait_init_finish() {
  dexec "python3 - <<'PY'
import time, rclpy
from rclpy.node import Node
from std_srvs.srv import Trigger
rclpy.init()
node = Node('lelab_stack_gate')
client = node.create_client(Trigger, '/ros_robot_controller/init_finish')
if not client.wait_for_service(timeout_sec=$SERVICE_TIMEOUT_S):
    node.destroy_node(); rclpy.shutdown()
    raise SystemExit('init_finish service never appeared; controller_manager would hang - not started')
deadline = time.monotonic() + $SERVICE_TIMEOUT_S
ready = False
while time.monotonic() < deadline:
    future = client.call_async(Trigger.Request())
    rclpy.spin_until_future_complete(node, future, timeout_sec=1.0)
    res = future.result() if future.done() else None
    if res is not None and res.success:
        ready = True
        break
    time.sleep(0.3)
node.destroy_node(); rclpy.shutdown()
print('init_finish_ready=%s' % ready)
if not ready:
    raise SystemExit('ros_robot_controller did not report init_finish within the timeout')
PY"
}

status() {
  dexec "for f in '$STATE_DIR'/*.pid; do [ -f \"\$f\" ] || continue; pid=\$(cat \"\$f\"); \
printf '%s pid=%s alive=' \"\$(basename \"\$f\" .pid)\" \"\$pid\"; \
[ -d \"/proc/\$pid\" ] && printf yes || printf no; \
printf ' cmd=%s\n' \"\$(tr '\0' ' ' < /proc/\$pid/cmdline 2>/dev/null | cut -c1-110)\"; done"
  dexec "export PYTHONPATH='$ROBOT_CONTROLLER_PYTHONPATH'\${PYTHONPATH:+:\$PYTHONPATH}; python3 - $FEEDBACK_TOPIC $EXPECTED_RATE_HZ <<'PY'
import json, sys, time, rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
from ros_robot_controller_msgs.msg import GetBusServoCmd
from ros_robot_controller_msgs.srv import GetBusServoState
topic, expected_hz, timeout_s = sys.argv[1], float(sys.argv[2]), 5.0
rclpy.init()
node = Node('lelab_stack_status')
seen = []
node.create_subscription(JointState, topic, lambda m: seen.append(m), 10)
start = time.monotonic()
while time.monotonic() - start < $MEASURE_SECONDS:
    rclpy.spin_once(node, timeout_sec=0.2)
client = node.create_client(GetBusServoState, '/ros_robot_controller/bus_servo/get_state')
if client.wait_for_service(timeout_sec=timeout_s):
    request = GetBusServoState.Request()
    for servo_id in (1, 2, 3, 4, 5, 10):
        cmd = GetBusServoCmd()
        cmd.id = servo_id
        cmd.get_id = 1
        cmd.get_position = 1
        request.cmd.append(cmd)
    future = client.call_async(request)
    rclpy.spin_until_future_complete(node, future, timeout_sec=timeout_s)
    res = future.result()
    if res is None:
        pulses = 'TIMEOUT'
    elif not res.success:
        pulses = 'READ_FAILED'
    else:
        pulses = [[int(st.present_id[0]), int(st.position[0])]
                  for st in res.state if len(st.present_id) == 1 and len(st.position) == 1]
else:
    pulses = 'SERVICE_MISSING'
last = seen[-1] if seen else None
rate = len(seen) / max(time.monotonic() - start, 1e-6)
stamp_age = None
if last is not None:
    stamp_age = round(time.time() - (last.header.stamp.sec + last.header.stamp.nanosec * 1e-9), 3)
print(json.dumps({
    'feedback_topic': topic,
    'samples': len(seen),
    'measured_hz': round(rate, 3),
    'expected_hz': expected_hz,
    'names': list(last.name) if last else [],
    'positions_rad': [round(float(v), 4) for v in last.position] if last else [],
    'stamp_age_s': stamp_age,
    'bus_servo_pulses': pulses,
    'feedback_is_measured': False,
    'joint_state_source': 'controller_manager_command_cache',
    'vendor_sdk_import': __import__('ros_robot_controller.ros_robot_controller_sdk').ros_robot_controller_sdk.__file__,
    'note': 'joint_states positions are vendor command cache, not measured; bus_servo_pulses are separate service readbacks when valid',
}, default=str))
node.destroy_node(); rclpy.shutdown()
if not seen:
    raise SystemExit('no real follower feedback on ' + topic)
PY"
  log "if samples=0 the stack is NOT usable for following; do not substitute targets"
}

# Stop only the process groups this script recorded (children first, then the group), then drop
# the pid files. A recorded pid whose command line is not one of our nodes is skipped, never killed.
stop() {
  dexec "for f in '$STATE_DIR'/*.pid; do [ -f \"\$f\" ] || continue; pid=\$(cat \"\$f\");
if [ ! -d \"/proc/\$pid\" ]; then printf 'pid %s already gone\\n' \"\$pid\"; rm -f \"\$f\"; continue; fi
args=\$(tr '\0' ' ' < /proc/\$pid/cmdline 2>/dev/null)
case \"\$args\" in
  *ros_robot_controller*|*servo_controller*|*controller_manager*|*depth_camera.launch*) ;;
  *) printf 'SKIP %s: pid %s is not one of our nodes (%s)\\n' \"\$(basename \"\$f\")\" \"\$pid\" \"\$args\"; continue ;;
esac
printf 'stopping pgid=%s (%s)\\n' \"\$pid\" \"\$args\"
for p in \$(cat /proc/\$pid/task/\$pid/children 2>/dev/null); do kill -TERM \"\$p\" 2>/dev/null || true; done
kill -TERM -\"\$pid\" 2>/dev/null || kill -TERM \"\$pid\" 2>/dev/null || true
done
sleep 2
for f in '$STATE_DIR'/*.pid; do [ -f \"\$f\" ] || continue; pid=\$(cat \"\$f\")
if [ -d \"/proc/\$pid\" ]; then printf 'force pgid=%s\\n' \"\$pid\"; kill -KILL -\"\$pid\" 2>/dev/null || kill -KILL \"\$pid\" 2>/dev/null || true; fi
rm -f \"\$f\"
done"
  log "stopped only the process groups this script recorded; platform services were untouched"
}

case "$MODE" in
  --check) banner; check ;;
  --apply) banner; apply ;;
  --status) banner; status ;;
  --stop) banner; stop ;;
  # --render prints the generated wrappers without touching the car, so the pid-file,
  # setsid and pre-flight guards are reviewable before anyone runs --apply.
  --render) build_wrappers; cat "$TMPDIR_LOCAL/ros_robot_controller.sh" "$TMPDIR_LOCAL/controller_manager.sh" ;;
  -h|--help) sed -n '1,32p' "${BASH_SOURCE[0]}" ;;
  *) log "unknown mode $MODE"; exit 2 ;;
esac
