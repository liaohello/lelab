#!/usr/bin/env bash
# Bring up the LanderPi chassis bench: vendor board owner + trial UI.
# Runs from the host as the docker-capable user; nothing here opens /dev/rrc
# directly, the vendor ros_robot_controller node stays the only owner.
set -uo pipefail

CONTAINER=${CONTAINER:-MentorPi}
PORT=${PORT:-8790}
BASE=${BASE:-/home/pi/lelab_base}

say() { printf '%s\n' "$*"; }

docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || {
  say "container $CONTAINER is not running"; exit 1; }

# Redeploy the two files if the container lost them (container recreate, not a reboot).
docker exec "$CONTAINER" test -f /tmp/base_trial_server.py || \
  docker cp "$BASE/base_trial_server.py" "$CONTAINER:/tmp/base_trial_server.py"
docker exec "$CONTAINER" test -f /tmp/lelab_probe/lelab_mobile_agent/linux_gamepad.py || {
  docker exec "$CONTAINER" mkdir -p /tmp/lelab_probe
  docker cp "$BASE/lelab_probe" "$CONTAINER:/tmp/lelab_probe"
}

docker exec "$CONTAINER" bash -lc '
  set +u
  if [ -f /tmp/vendor_owner.pid ] && kill -0 "$(cat /tmp/vendor_owner.pid)" 2>/dev/null; then
    echo "board owner already running pid $(cat /tmp/vendor_owner.pid)"
  else
    source /opt/ros/humble/setup.bash
    source /home/ubuntu/ros2_ws/install/setup.bash
    export ROS_DOMAIN_ID=0 MACHINE_TYPE=LanderPi_Mecanum RMW_IMPLEMENTATION=rmw_fastrtps_cpp
    nohup ros2 run ros_robot_controller ros_robot_controller > /tmp/vendor_owner.log 2>&1 &
    echo $! > /tmp/vendor_owner.pid
    echo "board owner started pid $!"
  fi
'

sleep 4

docker exec "$CONTAINER" bash -lc '
  set +u
  if [ -f /tmp/base_trial/server.pid ] && kill -0 "$(cat /tmp/base_trial/server.pid)" 2>/dev/null; then
    echo "trial ui already running pid $(cat /tmp/base_trial/server.pid)"
  else
    source /opt/ros/humble/setup.bash
    source /home/ubuntu/ros2_ws/install/setup.bash
    export ROS_DOMAIN_ID=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
    export PYTHONPATH=/tmp/lelab_probe:$PYTHONPATH
    mkdir -p /tmp/base_trial
    nohup python3 /tmp/base_trial_server.py --http-port '"$PORT"' --publish \
      --trace-dir /tmp/base_trial > /tmp/base_trial_server.log 2>&1 &
    echo $! > /tmp/base_trial/server.pid
    echo "trial ui started pid $!"
  fi
'

sleep 3

STATE=$(curl -s --noproxy '*' -m 5 "http://127.0.0.1:${PORT}/api/state" || true)
if [ -z "$STATE" ]; then
  say "UI not answering on port ${PORT}; see"
  say "  docker exec $CONTAINER tail -20 /tmp/base_trial_server.log"
  exit 1
fi
say "board_owner=$(printf '%s' "$STATE" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["out"]["board_owner"])')"
say "scene: http://$(hostname -I | awk '{print $1}'):${PORT}/"
say "center: $(printf '%s' "$STATE" | python3 -c 'import json,sys;d=json.load(sys.stdin);c=d["cfg"]["center"];print("vx",c["vx"],"vy",c["vy"],"wz",c["wz"])')"
