#!/usr/bin/env bash
# Real LanderPi ROS 2 native stack. NOT the offline simulator launcher.
# Manages only its own recorded PIDs (owner/control/agent); never pkill.
#   LAN=0 (default): car-internal graph only, ROS_LOCALHOST_ONLY=1 (T2).
#   LAN=1: per-process CycloneDDS LAN interface + static peer (T3+), no global config change.
set -o pipefail
set +u
RID="${RID:-landerpi-ros2native-live-20261108T041158Z}"
BASE="${LELAB_CAR_ROOT:-$HOME/shared/lelab_car}"
CODE="$BASE/candidates/$RID/code"
VD="$BASE/vendor/ros_robot_controller_t2_20261108"
KD="$BASE/ros2_native_20261108"
STATE="$KD/state"; LOGS="$KD/logs"
mkdir -p "$STATE" "$LOGS"
AGENT_PORT="${AGENT_PORT:-8110}"
STREAM_PORT="${STREAM_PORT:-18121}"
BUNDLE=19a1bddc0d902cc91e5ecd0e68e758ce9f929ed8bcb72af96a17e67eeadb75ba
PROFILE="$CODE/ros2_ws/src/lelab_mobile_agent/config/hardware-hybrid-follow-v1-dds.json"

source /opt/ros/humble/setup.bash
ROS_WS="${LELAB_ROS_WS:-$HOME/ros2_ws}"
THIRD_PARTY_WS="${LELAB_THIRD_PARTY_WS:-$HOME/third_party_ros2/third_party_ws}"
source "$ROS_WS/install/setup.bash"
[ -f "$THIRD_PARTY_WS/install/setup.bash" ] && source "$THIRD_PARTY_WS/install/setup.bash"
source "$KD/install/setup.bash"
unset CYCLONEDDS_URI
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp ROS_DOMAIN_ID=0
export MACHINE_TYPE=LanderPi_Mecanum
export SERVO_PARAMS="${SERVO_PARAMS:-$ROS_WS/install/servo_controller/share/servo_controller/config/servo_controller.yaml}"
if [ "${LAN:-0}" = "1" ]; then
  export ROS_LOCALHOST_ONLY=0
  IFACE="${IFACE:-wlan0}"; PEER="${PEER:?Set PEER to the authorized DDS peer address when LAN=1}"
  cat > "$STATE/cyclonedds.xml" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<CycloneDDS xmlns="https://cdds.io/config" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="https://cdds.io/config https://raw.githubusercontent.com/eclipse-cyclonedds/cyclonedds/master/etc/cyclonedds.xsd">
  <Domain Id="0">
    <General>
      <Interfaces><NetworkInterface name="$IFACE" priority="default" multicast="default"/></Interfaces>
      <AllowMulticast>spdp</AllowMulticast>
    </General>
    <Discovery>
      <Peers><Peer address="$PEER"/></Peers>
      <ParticipantIndex>auto</ParticipantIndex>
      <MaxAutoParticipantIndex>30</MaxAutoParticipantIndex>
    </Discovery>
  </Domain>
</CycloneDDS>
XML
  export CYCLONEDDS_URI="file://$STATE/cyclonedds.xml"
else
  export ROS_LOCALHOST_ONLY=1
fi
export PYTHONPATH="$VD:$CODE/ros2_ws/src/lelab_mobile_agent:${PYTHONPATH:-}"

start_one() {
  local name="$1"; shift
  if [ -f "$STATE/$name.pid" ] && kill -0 "$(cat "$STATE/$name.pid")" 2>/dev/null; then
    echo "$name already running pid $(cat "$STATE/$name.pid")"; return 0
  fi
  nohup setsid "$@" >> "$LOGS/$name.log" 2>&1 &
  echo $! > "$STATE/$name.pid"
  sleep 1
  echo "$name pid $(cat "$STATE/$name.pid")"
}

case "${1:-status}" in
  start)
    start_one owner python3 -m ros_robot_controller.ros_robot_controller_node
    sleep 3
    start_one control python3 -m lelab_mobile_agent.car_control_process \
      --profile "$PROFILE" --stream-host 127.0.0.1 --stream-port "$STREAM_PORT" \
      --licence-socket "$STATE/licence.sock" --status-path "$STATE/car_control_status.json" \
      --control-hz 10 --window-ms 400 --max-ttl-ms 200 --bundle-hash "$BUNDLE" \
      --dds-command-key-file "$KD/command.key" --data-identity hardware \
      --output-topic /ros_robot_controller/bus_servo/set_position \
      --readback-topic /lelab/ros_robot_controller/servo_readback \
      --health-topic /lelab/ros_robot_controller/driver_health \
      --state-topic /ros_robot_controller/bus_servo/set_state \
      --state-service /ros_robot_controller/bus_servo/get_state \
      --recording-topic /lelab/recording/teleop
    sleep 2
    start_one agent python3 -m lelab_mobile_agent.agent --ros-args \
      -p profile_path:="$PROFILE" -p car_root:="$KD/car" \
      -p host:=127.0.0.1 -p port:="$AGENT_PORT" \
      -p licence_socket:="$STATE/licence.sock" \
      -p control_status_path:="$STATE/car_control_status.json"
    sleep 3
    curl -fsS -m 5 "http://127.0.0.1:$AGENT_PORT/health" && echo " agent_health_ok" || echo " agent_health_FAIL"
    ;;
  stop)
    for name in agent control owner; do
      pf="$STATE/$name.pid"
      [ -f "$pf" ] || { echo "$name: no pid"; continue; }
      pid="$(cat "$pf")"
      if [ -d "/proc/$pid" ] && tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q "$name\|ros_robot_controller\|lelab_mobile_agent"; then
        kill -TERM "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do [ -d "/proc/$pid" ] || break; sleep 0.5; done
        if [ -d "/proc/$pid" ]; then kill -KILL "$pid" 2>/dev/null || true; echo "$name: killed_after_term_timeout"; else echo "$name: terminated"; fi
      else
        echo "$name: pid $pid not our process; skipped"
      fi
      rm -f "$pf"
    done
    ;;
  status)
    for name in owner control agent; do
      pf="$STATE/$name.pid"; pid="$(cat "$pf" 2>/dev/null || true)"
      echo "$name pid=${pid:-none} alive=$([ -n "$pid" ] && [ -d "/proc/$pid" ] && echo yes || echo no)"
    done
    curl -fsS -m 5 "http://127.0.0.1:$AGENT_PORT/health" 2>/dev/null && echo || echo "agent unreachable"
    ;;
  *) echo "usage: $0 start|stop|status (LAN=0|1 IFACE= PEER= RID=)"; exit 2 ;;
esac
