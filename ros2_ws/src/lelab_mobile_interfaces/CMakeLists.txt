cmake_minimum_required(VERSION 3.8)
project(lelab_mobile_interfaces)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/MeasuredServoFrame.msg"
  "msg/DriverFailure.msg"
  "msg/DriverHealth.msg"
  "msg/CommandReceipt.msg"
  "msg/TeleopFrame.msg"
  "msg/FreshnessWindowStatus.msg"
  "msg/TransportHello.msg"
  "msg/Preempt.msg"
  "srv/SessionControl.srv"
  "srv/ClearEstop.srv"
  "action/HomeArm.action"
  DEPENDENCIES std_msgs
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
