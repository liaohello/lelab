#!/usr/bin/env bash
# Shared configuration and helpers for the local ROS 2 Humble offline runtime.
# Sourced by every entry point in this directory. Not executable on its own.
#
# Isolation model (no container engine and no sudo on this host):
#   unshare --map-root-user --mount --fork  +  chroot into a Ubuntu 22.04
#   base rootfs. Nothing is written outside work/ros2_collection/local_humble,
#   no host package or daemon is touched, and no real device node is exposed.

set -euo pipefail

LROS_TOOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$LROS_TOOL_DIR/../../.." && pwd)"
WORK_DIR="${LROS_WORK_DIR:-${REPO_ROOT}/work/ros2_collection/local_humble}"
case "$WORK_DIR" in /*) ;; *) printf 'LROS_WORK_DIR must be absolute\n' >&2; exit 2 ;; esac
VENDOR_MSGS_SRC="${LROS_VENDOR_MSGS_SRC:-${REPO_ROOT}/../resource/lander_pi/src/driver/ros_robot_controller_msgs}"

CACHE_DIR="${WORK_DIR}/cache"
ROOTFS="${WORK_DIR}/rootfs"
WS="${WORK_DIR}/ws"
RUN_DIR="${WORK_DIR}/run"
LOG_DIR="${WORK_DIR}/logs"
DATA_DIR="${WORK_DIR}/data"
STATE_DIR="${WORK_DIR}/state"
EVIDENCE_DIR="${WORK_DIR}/evidence"
LOCK_DIR="${LROS_TOOL_DIR}/lock"

export REPO_ROOT WORK_DIR LROS_TOOL_DIR
mkdir -p "$CACHE_DIR" "$RUN_DIR" "$LOG_DIR" "$DATA_DIR" "$STATE_DIR" "$EVIDENCE_DIR" "$LOCK_DIR"

# ---- Pinned inputs ---------------------------------------------------------
ROOTFS_URL="http://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/ubuntu-base-22.04-base-amd64.tar.gz"
ROOTFS_SHA256="df6fe77cee11bd216ac532f0ee082bdc4da3c0cc1f1d9cb20f3f743196bc4b07"
ROOTFS_NAME="ubuntu-base-22.04-base-amd64.tar.gz"
ROS_APT_URL="http://packages.ros.org/ros2/ubuntu"
ROS_KEY_URL="https://raw.githubusercontent.com/ros/rosdistro/master/ros.key"

# ROS runtime knobs. The offline profile is pinned to simulated and is NOT
# read from the caller's environment, so no launcher can be flipped into a
# real-output entry point by exporting a variable.
LROS_ROS_DOMAIN_ID="${LROS_ROS_DOMAIN_ID:-92}"
LROS_OFFLINE_PROFILE=simulated

APT_SYS_PKGS=(
  ca-certificates curl wget gnupg lsb-release locales procps tar xz-utils
  python3 python3-pip python3-dev python3-setuptools python3-pkg-resources
  build-essential cmake pkg-config python3-colcon-common-extensions
  python3-pytest python3-numpy python3-pil
)
APT_ROS_PKGS=(
  ros-humble-ros-base
  ros-humble-ament-cmake
  # colcon-ros identifies ament packages through this Python module; without it a
  # workspace build silently discovers zero packages.
  ros-humble-ament-package
  ros-humble-rosidl-default-generators
  ros-humble-rosidl-default-runtime
  ros-humble-launch-ros
  ros-humble-rosbag2 ros-humble-ros2bag ros-humble-rosbag2-py
  ros-humble-rosbag2-storage-default-plugins
  ros-humble-rosbag2-storage-mcap
  ros-humble-rmw-cyclonedds-cpp
  # The agent's package.xml declares this at run time; its imports are optional, so
  # the message package is installed rather than dropped from the declaration.
  # Interfaces only - no Gazebo, no simulator.
  ros-humble-ros-gz-interfaces
)
# Deliberately absent: python3-pyserial, python3-serial, ros-humble-ros-serial-driver.
# The runtime cannot open a serial port because the module is not installed.
APT_FORBIDDEN=(python3-pyserial python3-serial python3-serial-serial-driver)

lros_log() { printf '[local_ros2] %s\n' "$*" >&2; }
lros_die() { printf '[local_ros2] ERROR: %s\n' "$*" >&2; exit 1; }

require_userns() {
  if ! unshare --map-root-user --mount /bin/true 2>/dev/null; then
    lros_die "unprivileged user namespaces unavailable (probe: unshare --map-root-user --mount /bin/true)"
  fi
}

# humble_enter <bash-command-string>
# Runs the command as root inside the Humble chroot-equivalent. Exits with the
# command's status. No PID namespace: recorded pids are host-visible and
# therefore verifiable with /proc and ss(8) from outside.
#
# Two layers are needed on this kernel. bubblewrap supplies a real /dev (this
# host's AppArmor denies writes to character devices created in a plain
# unprivileged namespace, which breaks dpkg postinsts), and a nested
# unshare --map-root-user then supplies uid 0 for apt/dpkg. Neither layer can
# be dropped: bwrap alone runs as an ordinary uid, unshare alone has no
# usable /dev/null.
humble_enter() {
  local inner="$1"
  require_userns
  command -v bwrap >/dev/null 2>&1 || lros_die "bwrap (bubblewrap) is required; it is the only usable userspace sandbox here"
  [ -d "$ROOTFS" ] || lros_die "rootfs missing: run bin/prepare first ($ROOTFS)"
  LROS_INNER="$inner" \
    bwrap \
      --bind "$ROOTFS" / \
      --dev /dev \
      --proc /proc \
      --ro-bind-try /sys /sys \
      --tmpfs /tmp \
      --bind "$WORK_DIR" /out \
      --ro-bind "$REPO_ROOT" /repo \
      --ro-bind "$VENDOR_MSGS_SRC" /vendor \
      --unshare-user-try --unshare-ipc --unshare-uts \
      --setenv LANG C.UTF-8 \
      -- /bin/bash "/repo${LROS_TOOL_DIR#"$REPO_ROOT"}/lib/enter.sh"
}

# humble_ros <bash-command-string> : same, with the ROS overlay sourced.
humble_ros() {
  local inner="$1"
  humble_enter "source /opt/ros/humble/setup.bash; [ -f /out/ws/install/setup.bash ] && source /out/ws/install/setup.bash; export LANG=C.UTF-8 ROS_DOMAIN_ID=${LROS_ROS_DOMAIN_ID} ROS_LOCALHOST_ONLY=1 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp LROS_OFFLINE_PROFILE=${LROS_OFFLINE_PROFILE} PYTHONDONTWRITEBYTECODE=1 CAR_PKG=/repo/ros2_ws/src/lelab_mobile_agent OFFLINE_DIR=/repo${LROS_TOOL_DIR#"$REPO_ROOT"}/offline; export PYTHONPATH=\${CAR_PKG}:\${PYTHONPATH:-}; ${inner}"
}

# humble_enter_detached <name> <log-relative-path> <inner-command>
# Starts a long-lived service inside the runtime.  The service records its own pid,
# which is host-visible because no PID namespace is created, so the host can verify
# /proc/<pid>/cmdline before ever signalling it.
humble_enter_detached() {
  local name="$1" logfile="$2" inner="$3"
  local pidfile="$RUN_DIR/$name.pid"
  rm -f "$pidfile"
  LROS_DETACHED_NAME="$name" setsid nohup bash -c "
    source '$LROS_TOOL_DIR/lib/common.sh'
    humble_ros 'echo \$\$ > /out/run/$name.pid; exec $inner' >> '$LOG_DIR/$logfile' 2>&1
  " >/dev/null 2>&1 &
  LAUNCHER_PID=$!
  for _ in $(seq 1 60); do
    [ -s "$pidfile" ] && break
    sleep 0.25
  done
  [ -s "$pidfile" ] || lros_die "$name: no pid file after 15s (see $LOG_DIR/$logfile)"
  local pid
  pid="$(cat "$pidfile")"
  [ -d "/proc/$pid" ] || lros_die "$name: recorded pid $pid is not running"
  record_instance "$name" "$pid" "$inner" "\"launcher_pid\":$LAUNCHER_PID,\"log\":\"$LOG_DIR/$logfile\""
  lros_log "$name: pid $pid (launcher $LAUNCHER_PID) log $LOG_DIR/$logfile"
}

instance_file() { printf '%s/%s.json' "$RUN_DIR" "$1"; }

record_instance() {
  # record_instance <name> <pid> <argv> [extra-json-without-braces]
  local name="$1" pid="$2" argv="$3" extra="${4:-}" started fp
  started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  fp="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || echo "$argv")"
  fp="${fp//\\/\\\\}"; fp="${fp//\"/\\\"}"
  local esc="${argv//\\/\\\\}"; esc="${esc//\"/\\\"}"
  printf '{"name":"%s","pid":%s,"started_utc":"%s","argv":"%s","proc_fingerprint":"%s","uid":%s%s}\n' \
    "$name" "$pid" "$started" "$esc" "$fp" "$(id -u)" "${extra:+,${extra}}" \
    > "$(instance_file "$name")"
}

read_instance_field() {
  local f; f="$(instance_file "$1")"
  [ -f "$f" ] || return 1
  sed -n "s/.*\"$2\":\([^,}]*\).*/\1/p" "$f" | head -1 | tr -d '"'
}

# Stop exactly the instance we recorded, after proving the pid still matches
# the recorded command fingerprint. Never pkill/killall.
stop_instance() {
  local name="$1" sig="${2:-TERM}" pid fp recorded
  pid="$(read_instance_field "$name" pid || true)"
  [ -n "${pid:-}" ] || { lros_log "instance $name: no record"; return 0; }
  recorded="$(read_instance_field "$name" proc_fingerprint || true)"
  if [ ! -d "/proc/$pid" ]; then
    lros_log "instance $name (pid $pid): already gone"
    rm -f "$(instance_file "$name")"; return 0
  fi
  fp="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  # A name appearing anywhere in the live command line is not proof of identity:
  # match the recorded argv exactly, and refuse when nothing was recorded.
  [ -n "$recorded" ] || lros_die "instance $name: no recorded fingerprint, refusing to signal pid $pid"
  if [ "$fp" != "$recorded" ]; then
    lros_die "instance $name: pid $pid cmdline differs from the recorded fingerprint, refusing to signal"
  fi
  lros_log "instance $name: sending SIG$sig to pid $pid"
  kill -"$sig" "$pid" 2>/dev/null || true
}

wait_instance_gone() {
  local name="$1" timeout="${2:-30}" waited=0 pid
  pid="$(read_instance_field "$name" pid || true)"
  [ -n "${pid:-}" ] || return 0
  while [ -d "/proc/$pid" ] && [ "$waited" -lt "$timeout" ]; do
    sleep 0.5; waited=$((waited + 1))
  done
  if [ -d "/proc/$pid" ]; then
    lros_log "instance $name: pid $pid still alive after ${timeout}s"
    return 1
  fi
  lros_log "instance $name: pid $pid exited"
  rm -f "$(instance_file "$name")"
  return 0
}

free_port() {
  # free_port <port> -> 0 if nothing is listening on 127.0.0.1:<port>
  local port="$1"
  if ss -ltnH "sport = :$port" 2>/dev/null | grep -q ":$port"; then
    return 1
  fi
  return 0
}
