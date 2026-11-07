[car-arm-stack] wrappers built locally: lelab_car_arm_stack.uxZ9aB/{controller_manager,ros_robot_controller}.sh
[car-arm-stack] camera wrapper enabled
#!/usr/bin/env bash
set +u
echo $$ > '/home/ubuntu/lelab_car/car_arm_stack/ros_robot_controller.pid'
for f in /home/ubuntu/ros2_ws/.typerc /opt/ros/humble/setup.bash /home/ubuntu/ros2_ws/install/setup.bash /home/ubuntu/third_party_ros2/third_party_ws/install/setup.bash; do [ -f "$f" ] && source "$f"; done
[ -f '/home/ubuntu/lelab_car/car_arm_stack/params.env' ] && source '/home/ubuntu/lelab_car/car_arm_stack/params.env'
  [ -n '/home/ubuntu/lelab_car/vendor' ] && export PYTHONPATH='/home/ubuntu/lelab_car/vendor'${PYTHONPATH:+:$PYTHONPATH}
[ -n "${SERVO_PARAMS:-}" ] || { echo 'missing SERVO_PARAMS - run --check first'; exit 1; }
[ -n "${MACHINE_TYPE:-}" ] || { echo 'MACHINE_TYPE is unset - ros_robot_controller would crash'; exit 1; }
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-0}
cd '/home/ubuntu/lelab_car'
exec ros2 run ros_robot_controller ros_robot_controller
#!/usr/bin/env bash
set +u
echo $$ > '/home/ubuntu/lelab_car/car_arm_stack/controller_manager.pid'
for f in /home/ubuntu/ros2_ws/.typerc /opt/ros/humble/setup.bash /home/ubuntu/ros2_ws/install/setup.bash /home/ubuntu/third_party_ros2/third_party_ws/install/setup.bash; do [ -f "$f" ] && source "$f"; done
[ -f '/home/ubuntu/lelab_car/car_arm_stack/params.env' ] && source '/home/ubuntu/lelab_car/car_arm_stack/params.env'
  [ -n '/home/ubuntu/lelab_car/vendor' ] && export PYTHONPATH='/home/ubuntu/lelab_car/vendor'${PYTHONPATH:+:$PYTHONPATH}
[ -n "${SERVO_PARAMS:-}" ] || { echo 'missing SERVO_PARAMS - run --check first'; exit 1; }
[ -n "${MACHINE_TYPE:-}" ] || { echo 'MACHINE_TYPE is unset - ros_robot_controller would crash'; exit 1; }
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-0}
cd '/home/ubuntu/lelab_car'
exec ros2 run servo_controller servo_controller --ros-args --params-file "$SERVO_PARAMS"
