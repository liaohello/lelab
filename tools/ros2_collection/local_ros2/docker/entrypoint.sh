#!/usr/bin/env bash
# Sources the overlay produced by the runtime build, then runs whatever was asked.
# 不用 set -u：/opt/ros/humble/setup.bash 会引用未定义变量（COLCON_PREFIX_PATH 等），
# 严格模式会让入口脚本在 source 阶段就退出。
set -o pipefail
[ -f /opt/ros/humble/setup.bash ] && source /opt/ros/humble/setup.bash
if [ -f /work/out/ws/install/setup.bash ]; then
  source /work/out/ws/install/setup.bash
  export PYTHONPATH="/work/out/ws/src:${PYTHONPATH:-}"
fi
if [ -d /work/lelab/ros2_ws/src/lelab_mobile_agent ]; then
  export PYTHONPATH="/work/lelab/ros2_ws/src/lelab_mobile_agent:${PYTHONPATH:-}"
fi
export CAR_PKG=/work/lelab/ros2_ws/src/lelab_mobile_agent
export OFFLINE_DIR=/work/lelab/tools/ros2_collection/local_ros2/offline
echo "[lelab-image] python=$(python3 -V 2>&1) ros=$ROS_DISTRO rmw=${RMW_IMPLEMENTATION:-default} domain=${ROS_DOMAIN_ID:-0}"
exec "$@"
