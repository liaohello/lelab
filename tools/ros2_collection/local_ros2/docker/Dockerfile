# LanderPi 本地 Humble 开发容器：环境层，不含代码。
# 代码、vendor 消息与构建产物都在运行时只读/可写挂载，避免把未提交源码烧进镜像。
# 与 Pi 上 MentorPi 的区别（刻意为之）：不 privileged、不映射 /dev、不装 pyserial。
FROM ros:humble-ros-base-jammy@sha256:a3f4de78e42cca693a543ece5798aedcf1cae0171fe71c334a047332cc72ef93

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3-colcon-common-extensions \
      python3-pytest python3-numpy python3-pil \
      ros-humble-rmw-cyclonedds-cpp \
      ros-humble-ament-cmake ros-humble-ament-package ros-humble-rosidl-default-generators \
      build-essential cmake pkg-config \
      procps iproute2 curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# 这里不安装 python3-pyserial / ros-humble-ros-serial-driver：容器内结构上打不开真实总线。

COPY tools/ros2_collection/local_ros2/docker/entrypoint.sh /usr/local/bin/lelab-entrypoint.sh
RUN chmod 755 /usr/local/bin/lelab-entrypoint.sh

ENV LANG=C.UTF-8 \
    ROS_LOCALHOST_ONLY=1 \
    RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
    PYTHONDONTWRITEBYTECODE=1 \
    LROS_OFFLINE_PROFILE=simulated

ENTRYPOINT ["/usr/local/bin/lelab-entrypoint.sh"]
CMD ["bash"]
