from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    package_share = Path(get_package_share_directory("qbot_navigation_visualization"))
    default_params = package_share / "config" / "navigation_visualization.yaml"

    params_file = LaunchConfiguration("params_file")
    cloud_topic = LaunchConfiguration("cloud_topic")
    scan_topic = LaunchConfiguration("scan_topic")
    output_frame = LaunchConfiguration("output_frame")
    max_cloud_rate = LaunchConfiguration("max_cloud_rate")
    restamp_output = LaunchConfiguration("restamp_output")
    use_sim_time = LaunchConfiguration("use_sim_time")
    start_bridge = LaunchConfiguration("start_bridge")
    bridge_address = LaunchConfiguration("bridge_address")
    bridge_port = LaunchConfiguration("bridge_port")

    navigation_node = Node(
        package="qbot_navigation_visualization",
        executable="navigation_visualizer",
        name="navigation_visualizer",
        output="screen",
        parameters=[
            params_file,
            {
                "cloud_topic": cloud_topic,
                "scan_topic": scan_topic,
                "output_frame": output_frame,
                "max_cloud_rate": ParameterValue(max_cloud_rate, value_type=float),
                "restamp_output": ParameterValue(restamp_output, value_type=bool),
                "use_sim_time": ParameterValue(use_sim_time, value_type=bool),
            },
        ],
    )

    foxglove_bridge = Node(
        package="foxglove_bridge",
        executable="foxglove_bridge",
        name="wsl_foxglove_bridge",
        output="screen",
        condition=IfCondition(start_bridge),
        parameters=[
            {
                "address": bridge_address,
                "port": ParameterValue(bridge_port, value_type=int),
                "use_sim_time": ParameterValue(use_sim_time, value_type=bool),
                "topic_whitelist": [
                    "^/navigation/.*$",
                    "^/ouster/scan$",
                    "^/tf$",
                    "^/tf_static$",
                    "^/qbot_battery$",
                ],
            }
        ],
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument("params_file", default_value=str(default_params)),
            DeclareLaunchArgument("cloud_topic", default_value="/ouster/points_viz"),
            DeclareLaunchArgument("scan_topic", default_value="/ouster/scan"),
            DeclareLaunchArgument(
                "output_frame",
                default_value="base_link",
                description=(
                    "Frame for derived topics; use os_lidar for bags without a base transform"
                ),
            ),
            DeclareLaunchArgument("max_cloud_rate", default_value="5.0"),
            DeclareLaunchArgument(
                "restamp_output",
                default_value="false",
                description=(
                    "Stamp derived topics at publication time for malformed bag timestamps"
                ),
            ),
            DeclareLaunchArgument("use_sim_time", default_value="false"),
            DeclareLaunchArgument(
                "start_bridge",
                default_value="true",
                description="Start Foxglove Bridge in WSL for the Windows client",
            ),
            DeclareLaunchArgument("bridge_address", default_value="0.0.0.0"),
            DeclareLaunchArgument("bridge_port", default_value="8765"),
            navigation_node,
            foxglove_bridge,
        ]
    )
