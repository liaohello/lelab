cmake_minimum_required(VERSION 3.16)
project(lelab_mobile_sim LANGUAGES NONE)

find_package(ament_cmake REQUIRED)

install(DIRECTORY launch config worlds
  DESTINATION share/${PROJECT_NAME}
)
install(PROGRAMS scripts/spawn_robot.py
  DESTINATION lib/${PROJECT_NAME}
)
ament_package()
