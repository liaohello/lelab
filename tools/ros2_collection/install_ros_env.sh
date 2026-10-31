#!/usr/bin/env bash
# Install an isolated ROS 2 Jazzy + Gazebo Harmonic user-space environment.
# No sudo or system package changes are performed.  The environment lives in
# work/ros2_collection/envs/ros_jazzy and is intentionally outside the source
# packages.  A resolved package lock is written next to the environment.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="${LELAB_ROS_WORK:-$ROOT/work/ros2_collection}"
ENV_PREFIX="${LELAB_ROS_ENV:-$WORK/envs/ros_jazzy}"
TOOLS="$WORK/tools"
MAMBA_ROOT_PREFIX="$WORK/mamba"
LOG_DIR="$WORK/logs"
mkdir -p "$TOOLS" "$LOG_DIR" "$(dirname "$ENV_PREFIX")"

export MAMBA_ROOT_PREFIX

if [[ ! -x "$TOOLS/micromamba" ]]; then
  echo "Downloading micromamba..."
  curl -Ls -o "$TOOLS/micromamba" \
    https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-linux-64
  chmod +x "$TOOLS/micromamba"
fi

PACKAGES=(
  python=3.12
  ros-jazzy-desktop
  ros-jazzy-ros-gz
  ros-jazzy-gz-ros2-control
  ros-jazzy-rosbag2
  ros-jazzy-rosbag2-storage-mcap
  ros-jazzy-rosbag2-storage-default-plugins
  ros-jazzy-ros-gz-image
  ros-jazzy-cv-bridge
  ros-jazzy-image-transport
  ros-jazzy-teleop-twist-keyboard
  ros-jazzy-ros2-control
  ros-jazzy-ros2-controllers
  colcon-common-extensions
  make
  ninja
  gcc_linux-64
  gxx_linux-64
)

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$LOG_DIR/install-ros-jazzy-$STAMP.log"

echo "Installing into $ENV_PREFIX; log: $LOG"
"$TOOLS/micromamba" create -y -p "$ENV_PREFIX" \
  -c conda-forge -c robostack-jazzy \
  "${PACKAGES[@]}" 2>&1 | tee "$LOG"

"$TOOLS/micromamba" list -p "$ENV_PREFIX" --explicit > "$ENV_PREFIX/package-lock.txt"
"$TOOLS/micromamba" list -p "$ENV_PREFIX" --json > "$ENV_PREFIX/package-list.json"
echo "Environment ready: $ENV_PREFIX"
echo "Lock: $ENV_PREFIX/package-lock.txt"
