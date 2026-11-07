# Recipe for the container path, for a host that DOES have a container engine.
# It is NOT what ran here: this host has no docker/podman and cannot reach
# registry-1.docker.io, so the digest of the base image is unverified and the
# runtime actually used is the bwrap+unshare rootfs in ../README.md.
# Build inputs come from the read-only vendor copy, never from the reference tree.
FROM ros:humble-ros-base-jammy          # amd64; digest NOT pinned, see lock/env.lock.json

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get -y --no-install-recommends \
      python3-colcon-common-extensions ros-humble-rosbag2-storage-mcap \
      ros-humble-rmw-cyclonedds-cpp ros-humble-ros-gz-interfaces \
      python3-pytest python3-numpy python3-pil \
    && rm -rf /var/lib/apt/lists/*
# Deliberately absent: pyserial. The runtime cannot open a real bus.

WORKDIR /ws
COPY ws/src/ros_robot_controller_msgs /ws/src/ros_robot_controller_msgs
SHELL ["/bin/bash", "-c"]
RUN source /opt/ros/humble/setup.bash && colcon --log-base /ws/log build \
      --base-paths src --packages-select ros_robot_controller_msgs \
      --install-base /ws/install --build-base /ws/build

ENV ROS_LOCALHOST_ONLY=1 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp LANG=C.UTF-8
# -p 18121 is bound to 127.0.0.1 by the control process itself; never expose it.
CMD ["bash", "-lc", "source /opt/ros/humble/setup.bash && source /ws/install/setup.bash && python3 -m lelab_mobile_agent.car_control_process --help"]
