"""Start only the car-side agent in explicit hardware-readonly mode.

This launch intentionally does not start Gazebo, controllers, the watchdog,
simulation world or any actuator publisher/client.
"""
from pathlib import Path
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument(
            "profile_path",
            default_value=str(Path(get_package_share_directory("lelab_mobile_agent")) / "config" / "hardware-readonly-v1.json"),
        ),
        DeclareLaunchArgument("car_root", default_value="/home/pi/lelab_car"),
        DeclareLaunchArgument("port", default_value="8110"),
        DeclareLaunchArgument("host", default_value="127.0.0.1"),
        Node(
            package="lelab_mobile_agent",
            executable="agent",
            name="lelab_mobile_agent",
            parameters=[{
                "profile_path": LaunchConfiguration("profile_path"),
                "car_root": LaunchConfiguration("car_root"),
                "port": LaunchConfiguration("port"),
                "host": LaunchConfiguration("host"),
                "use_sim_time": False,
            }],
            output="screen",
        ),
    ])
