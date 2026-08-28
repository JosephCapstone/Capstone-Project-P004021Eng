from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from pathlib import Path


def generate_launch_description():
    package_share = Path(get_package_share_directory("qbot_navigation_visualization"))
    default_params = package_share / "config" / "navigation_visualization.yaml"

    params_file = LaunchConfiguration("params_file")
    use_sim_time = LaunchConfiguration("use_sim_time")

    return LaunchDescription(
        [
            DeclareLaunchArgument("params_file", default_value=str(default_params)),
            DeclareLaunchArgument(
                "use_sim_time",
                default_value="false",
                description="Set true when replaying a bag with a /clock publisher",
            ),
            Node(
                package="qbot_navigation_visualization",
                executable="navigation_visualizer",
                name="navigation_visualizer",
                output="screen",
                parameters=[params_file, {"use_sim_time": use_sim_time}],
            ),
        ]
    )
