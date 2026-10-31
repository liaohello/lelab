cmake_minimum_required(VERSION 3.16)
project(lelab_mobile_description LANGUAGES NONE)

find_package(ament_cmake REQUIRED)

install(DIRECTORY urdf meshes rviz config
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
