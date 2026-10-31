"""Bring up the LeLab mobile manipulator collection stack.

Components: Gazebo Harmonic, robot_state_publisher, ros_gz bridge, the
mecanum/arm controllers and (optionally) the car-side agent and RViz.
"""
from __future__ import annotations

import os
from pathlib import Path

from ament_index_python.packages import get_package_prefix, get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    EmitEvent,
    ExecuteProcess,
    LogInfo,
    OpaqueFunction,
    RegisterEventHandler,
    SetEnvironmentVariable,
    TimerAction,
)
from launch.conditions import IfCondition
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import Command, LaunchConfiguration
from launch_ros.actions import Node


def _launch_setup(context):
    world = LaunchConfiguration("world").perform(context)
    gui = LaunchConfiguration("gui").perform(context).lower() == "true"
    rviz = LaunchConfiguration("rviz").perform(context).lower() == "true"
    agent = LaunchConfiguration("agent").perform(context).lower() == "true"
    agent_port = LaunchConfiguration("agent_port").perform(context)
    car_root = LaunchConfiguration("car_root").perform(context)

    sim_share = Path(get_package_share_directory("lelab_mobile_sim"))
    desc_share = Path(get_package_share_directory("lelab_mobile_description"))
    control_prefix = Path(get_package_prefix("gz_ros2_control"))

    resource_paths = [str(sim_share), str(desc_share)]
    if os.environ.get("GZ_SIM_RESOURCE_PATH"):
        resource_paths.append(os.environ["GZ_SIM_RESOURCE_PATH"])
    plugin_paths = [str(control_prefix / "lib")]
    if os.environ.get("GZ_SIM_SYSTEM_PLUGIN_PATH"):
        plugin_paths.append(os.environ["GZ_SIM_SYSTEM_PLUGIN_PATH"])

    actions = [
        SetEnvironmentVariable("GZ_SIM_RESOURCE_PATH", ":".join(resource_paths)),
        SetEnvironmentVariable("GZ_SIM_SYSTEM_PLUGIN_PATH", ":".join(plugin_paths)),
    ]

    world_path = str(sim_share / "worlds" / f"{world}.sdf")
    gz_cmd = ["gz", "sim", "-r", "-v", "3", world_path]
    if not gui:
        gz_cmd = ["gz", "sim", "-r", "-s", "--headless-rendering", "-v", "3", world_path]
    gz = ExecuteProcess(cmd=gz_cmd, output="screen", name="gz_sim")
    actions.append(LogInfo(msg=f"[lelab] gz: {' '.join(gz_cmd)}"))
    actions.append(gz)
    actions.append(
        RegisterEventHandler(
            OnProcessExit(target_action=gz, on_exit=[EmitEvent(event=Shutdown(reason="gz_sim exited"))])
        )
    )

    robot_description = Command(
        ["xacro ", str(desc_share / "urdf" / "robot.urdf.xacro")]
    )
    actions.append(
        Node(
            package="robot_state_publisher",
            executable="robot_state_publisher",
            name="robot_state_publisher",
            parameters=[{"robot_description": robot_description, "use_sim_time": True}],
            output="screen",
        )
    )

    actions.append(
        Node(
            package="ros_gz_bridge",
            executable="parameter_bridge",
            name="ros_gz_bridge",
            parameters=[
                {
                    "config_file": str(sim_share / "config" / "bridge.yaml"),
                    "use_sim_time": True,
                }
            ],
            output="screen",
        )
    )

    spawn = ExecuteProcess(
        cmd=[
            "ros2", "run", "lelab_mobile_sim", "spawn_robot.py",
            "--world", world,
            "--name", "lelab_mobile",
            "--x", "0.0", "--y", "0.0", "--z", "0.0005",
            "--output", str(Path(car_root) / "runtime" / "lelab_mobile.sdf"),
        ],
        output="screen",
        name="spawn_robot",
    )
    actions.append(TimerAction(period=4.0, actions=[spawn]))

    def controller_spawner(controller: str, delay: float):
        return TimerAction(
            period=delay,
            actions=[
                ExecuteProcess(
                    cmd=[
                        "ros2", "run", "controller_manager", "spawner", controller,
                        "--controller-manager", "/controller_manager",
                        "--controller-manager-timeout", "30",
                    ],
                    output="screen",
                    name=f"spawner_{controller}",
                )
            ],
        )

    actions.extend(
        [
            controller_spawner("arm_controller", 10.0),
            controller_spawner("gripper_controller", 13.0),
        ]
    )

    if agent:
        actions.append(
            TimerAction(
                period=11.0,
                actions=[
                    Node(
                        package="lelab_mobile_agent",
                        executable="watchdog",
                        name="lelab_safety_watchdog",
                        parameters=[{"use_sim_time": False}],
                        output="screen",
                    )
                ],
            )
        )
        actions.append(
            TimerAction(
                period=15.0,
                actions=[
                    Node(
                        package="lelab_mobile_agent",
                        executable="agent",
                        name="lelab_mobile_agent",
                        parameters=[
                            {
                                "use_sim_time": True,
                                "port": int(agent_port),
                                "car_root": car_root,
                                "model_sdf": str(Path(car_root) / "runtime" / "lelab_mobile.sdf"),
                            }
                        ],
                        output="screen",
                    )
                ],
            )
        )

    if rviz:
        actions.append(
            TimerAction(
                period=6.0,
                actions=[
                    Node(
                        package="rviz2",
                        executable="rviz2",
                        name="rviz2",
                        arguments=["-d", str(desc_share / "rviz" / "collection.rviz")],
                        parameters=[{"use_sim_time": True}],
                        output="screen",
                        condition=IfCondition(LaunchConfiguration("rviz")),
                    )
                ],
            )
        )

    return actions


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument("world", default_value="pick_place"),
            DeclareLaunchArgument("gui", default_value="false"),
            DeclareLaunchArgument("rviz", default_value="false"),
            DeclareLaunchArgument("agent", default_value="true"),
            DeclareLaunchArgument("agent_port", default_value="8110"),
            DeclareLaunchArgument(
                "car_root",
                default_value=os.path.expanduser("~/Desktop/lelab/work/ros2_collection/car"),
            ),
            OpaqueFunction(function=_launch_setup),
        ]
    )
