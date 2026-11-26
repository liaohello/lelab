#!/bin/bash
# Bring up the LeLab chassis-trial chain on the LanderPi, host side (no sudo).
#
#   /home/pi/lelab_chain_up.sh                 # start whatever is missing
#   /home/pi/lelab_chain_up.sh --restart       # stop control/agent, start again
#   /home/pi/lelab_chain_up.sh --stop          # zero the wheels, then stop control+agent
#   BUNDLE=<hash> /home/pi/lelab_chain_up.sh   # override the announced arm bundle
#
# The vendor ros_robot_controller stays the only /dev/rrc owner: this script never
# opens the serial port, it only publishes the set_motor topic through the control
# process.  The arm has no driver in this profile and stays locked at zero output.
set -u

CONTAINER=${CONTAINER:-MentorPi}
CODE=${CODE:-/home/ubuntu/shared/lelab_car/base_trial_20261126/code}
PROFILE=${PROFILE:-ros2_ws/src/lelab_mobile_agent/config/hardware-hybrid-base-trial-v1.json}
BUNDLE=${BUNDLE:-aaca64554fc43b0897ed39acafcb3d8766026f74019b0e7995b6d3969f73f021}
TRIAL_DIR=/tmp/lelab_trial
TRACE_DIR=/tmp/base_trial
AGENT_PORT=${AGENT_PORT:-18110}
UPPER_HOST=${UPPER_HOST:-192.168.31.54}
UPPER_PORT=${UPPER_PORT:-2222}
UPPER_USER=${UPPER_USER:-liaozhengyan}
UPPER_PATH=${UPPER_PATH:-/home/liaozhengyan/Desktop/lelab/work/ros2_collection/upper-recordings}
UPPER_MOUNT=${UPPER_MOUNT:-/tmp/lelab-upper-storage}
UPPER_KEY=${UPPER_KEY:-/root/.ssh/id_lelab_upper}
HELPER_HOST=/home/pi/lelab_base/lelab_lease.py
HELPER=/tmp/lelab_lease.py

die() { echo "FAILED: $*" >&2; exit 1; }
in_container() { docker exec "$CONTAINER" bash -lc "$1"; }
running() { docker exec "$CONTAINER" bash -lc "pgrep -f '$1' >/dev/null 2>&1"; }

wait_for() {  # wait_for <seconds> <shell test>
    local limit=$1 test=$2 i=0
    while [ "$i" -lt "$limit" ]; do
        if in_container "$test"; then return 0; fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

stop_chain() {
    # Ask the agent for a zero first: killing the control process alone leaves the
    # STM32 holding the last target.
    local token=""
    [ -f "$TRIAL_DIR/token.txt" ] && token=$(docker exec "$CONTAINER" cat "$TRIAL_DIR/token.txt" 2>/dev/null)
    if [ -n "$token" ]; then
        docker exec -i "$CONTAINER" python3 - "$AGENT_PORT" "$token" <<'PY' >/dev/null 2>&1 || true
import json, sys, urllib.request
port, token = sys.argv[1], sys.argv[2]
req = urllib.request.Request(f"http://127.0.0.1:{port}/control/estop", data=b"{}",
                             headers={"content-type": "application/json",
                                      "X-LeLab-Control-Token": token})
urllib.request.urlopen(req, timeout=3).read()
PY
        sleep 1
    fi
    # Bracket patterns: a plain "pkill -f car_control_process" also matches this
    # very bash -lc command line and the shell kills itself before the real target.
    docker exec "$CONTAINER" bash -lc "pkill -f '[r]enew_trial.py'; pkill -f '[l]elab_lease.py renew'; pkill -f '[l]elab_mobile_agent.agent'; pkill -f '[l]elab_mobile_agent.car_control_process'; pkill -f '[l]og_motor.py'" 2>/dev/null
    sleep 2
    local left
    left=$(docker exec "$CONTAINER" bash -lc "pgrep -fc '[l]elab_mobile_agent.(agent|car_control_process)'" 2>/dev/null)
    echo "stopped control+agent (remaining=$left, board owner left running)"
}

# Recordings are written straight onto the upper computer's disk (recorder.py
# storage_location=upper_computer).  sshfs is mounted inside the container because
# that is where ros2 bag runs; the mount must carry fsname=lelab-upper-recordings or
# the recorder refuses to treat it as trusted storage.
ensure_upper_mount() {
    if docker exec "$CONTAINER" grep -q " $UPPER_MOUNT .* fuse.sshfs lelab-upper-recordings" /proc/self/mountinfo 2>/dev/null; then
        echo "upper mount: present at $UPPER_MOUNT"
        return
    fi
    echo "upper mount: mounting $UPPER_USER@$UPPER_HOST:$UPPER_PATH"
    docker exec "$CONTAINER" bash -lc "
      mkdir -p $UPPER_MOUNT
      timeout 15 umount $UPPER_MOUNT 2>/dev/null
      sshfs $UPPER_USER@$UPPER_HOST:$UPPER_PATH $UPPER_MOUNT -p $UPPER_PORT \
        -o IdentityFile=$UPPER_KEY -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/root/.ssh/known_hosts \
        -o fsname=lelab-upper-recordings -o reconnect \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o allow_other" || true
    docker exec "$CONTAINER" grep -q " $UPPER_MOUNT .* fuse.sshfs lelab-upper-recordings" /proc/self/mountinfo \
        && echo "upper mount: up" || echo "upper mount: FAILED (recording will refuse to start)"
}

start_owner() {
    if running "[l]ib/ros_robot_controller"; then
        echo "owner: already running"
        return
    fi
    echo "owner: starting"
    docker exec -d "$CONTAINER" bash -lc "
      source /opt/ros/humble/setup.bash
      source /home/ubuntu/ros2_ws/install/setup.bash
      export ROS_DOMAIN_ID=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp MACHINE_TYPE=LanderPi_Mecanum
      exec ros2 run ros_robot_controller ros_robot_controller > /tmp/vendor_owner.log 2>&1"
    wait_for 20 "pgrep -f 'lib/ros_robot_controller' >/dev/null" || die "owner did not appear"
    echo "owner: up"
}

start_joy() {
    # `ros-humble-joy` is no longer installed in this image (a noble-sourced apt run
    # removed it), so /joy comes from the package's own js reader instead.
    if running "[j]oy_source"; then
        echo "joy_source: already running"
        return
    fi
    echo "joy_source: starting"
    docker exec -d "$CONTAINER" bash -lc "
      source /opt/ros/humble/setup.bash
      source /home/ubuntu/ros2_ws/install/setup.bash
      export ROS_DOMAIN_ID=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
      export PYTHONPATH=$CODE/ros2_ws/src/lelab_mobile_agent:\$PYTHONPATH
      exec python3 -m lelab_mobile_agent.joy_source --port ${JOYSTICK_PORT:-platform-xhci-hcd.1-usb-0:2:1.0-joystick} --rate 20 > /tmp/joy_node.log 2>&1"
    wait_for 10 "grep -q 'js device generation' /tmp/joy_node.log 2>/dev/null" || true
}

start_control() {
    if running "[l]elab_mobile_agent.car_control_process"; then
        echo "control: already running"
        return
    fi
    docker exec "$CONTAINER" bash -lc "mkdir -p $TRIAL_DIR"
    echo "control: starting"
    docker exec -d "$CONTAINER" bash -lc "
      source /opt/ros/humble/setup.bash
      source /home/ubuntu/ros2_ws/install/setup.bash
      export ROS_DOMAIN_ID=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
      export PYTHONPATH=$CODE/ros2_ws/src/lelab_mobile_agent:\$PYTHONPATH
      cd $CODE
      exec python3 -m lelab_mobile_agent.car_control_process \
        --profile $PROFILE --data-identity hardware \
        --output-topic /ros_robot_controller/bus_servo/set_position \
        --readback-topic /lelab/ros_robot_controller/servo_readback \
        --health-topic /lelab/ros_robot_controller/driver_health \
        --state-topic /ros_robot_controller/bus_servo/set_state \
        --state-service /ros_robot_controller/bus_servo/get_state \
        --licence-socket $TRIAL_DIR/licence.sock \
        --status-path $TRIAL_DIR/car_control_status.json \
        --stream-port 18221 --control-hz 20 --bundle-hash $BUNDLE \
        --no-typed-feedback --no-dds-command > $TRIAL_DIR/control.log 2>&1"
    wait_for 15 "test -S $TRIAL_DIR/licence.sock" || die "licence socket never appeared (see $TRIAL_DIR/control.log)"
    echo "control: up"
}

start_agent() {
    if running "[l]elab_mobile_agent.agent"; then
        echo "agent: already running"
        return
    fi
    echo "agent: starting"
    docker exec -d "$CONTAINER" bash -lc "
      source /opt/ros/humble/setup.bash
      source /home/ubuntu/ros2_ws/install/setup.bash
      export ROS_DOMAIN_ID=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
      export PYTHONPATH=$CODE/ros2_ws/src/lelab_mobile_agent:\$PYTHONPATH
      cd $CODE
      exec python3 -m lelab_mobile_agent.agent --ros-args \
        -p profile_path:=$CODE/$PROFILE -p host:=127.0.0.1 -p port:=$AGENT_PORT \
        -p licence_socket:=$TRIAL_DIR/licence.sock \
        -p control_status_path:=$TRIAL_DIR/car_control_status.json > $TRIAL_DIR/agent.log 2>&1"
    wait_for 15 "python3 -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:$AGENT_PORT/status',timeout=1)\"" \
        || die "agent did not answer on $AGENT_PORT (see $TRIAL_DIR/agent.log)"
    echo "agent: up on $AGENT_PORT"
}

take_lease() {
    docker exec "$CONTAINER" bash -lc "rm -f $TRIAL_DIR/stop_renew; pkill -f '[r]enew_trial.py'; pkill -f '[l]elab_lease.py renew'" 2>/dev/null
    echo "lease: acquire + begin_epoch(base)"
    if ! docker exec "$CONTAINER" python3 "$HELPER" acquire --channels base --bundle "$BUNDLE" \
            --token-file "$TRIAL_DIR/token.txt"; then
        die "acquire failed (if control_held: another client still owns the lease, '/home/pi/lelab_chain_up.sh --restart' or release it from that client)"
    fi
    docker exec -d "$CONTAINER" python3 "$HELPER" renew --seconds 3600 --interval 10 \
        > /dev/null 2>&1
    echo "lease: renew loop every 10 s for 1 h"
}

summary() {
    docker exec "$CONTAINER" python3 "$HELPER" status --status-path "$TRIAL_DIR/car_control_status.json"
    echo -n "joy: "
    docker exec "$CONTAINER" bash -lc "
      source /opt/ros/humble/setup.bash; source /home/ubuntu/ros2_ws/install/setup.bash
      export ROS_DOMAIN_ID=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
      timeout 4 ros2 topic hz /joy 2>&1 | grep -m1 average" || echo "/joy silent"
    echo -n "set_motor: "
    docker exec "$CONTAINER" bash -lc "
      source /opt/ros/humble/setup.bash; source /home/ubuntu/ros2_ws/install/setup.bash
      export ROS_DOMAIN_ID=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
      timeout 4 ros2 topic hz /ros_robot_controller/set_motor 2>&1 | grep -m1 average" || echo "idle"
}

case "${1:-}" in
    --stop) stop_chain; exit 0 ;;
    --restart) stop_chain ;;
    "") : ;;
    *) echo "usage: $0 [--restart|--stop]" >&2; exit 2 ;;
esac

# The container's /tmp survives a container restart but not a container rebuild,
# so keep the source of truth on the host and re-seed it when it is gone.
if [ -f "$HELPER_HOST" ]; then
    docker exec "$CONTAINER" test -f "$HELPER" || docker cp "$HELPER_HOST" "$CONTAINER:$HELPER" >/dev/null
fi
docker exec "$CONTAINER" test -f "$HELPER" || die "missing $HELPER inside $CONTAINER (host copy: $HELPER_HOST)"

start_owner
ensure_upper_mount
start_joy
start_control
start_agent
take_lease
echo "--- summary ---"
summary
