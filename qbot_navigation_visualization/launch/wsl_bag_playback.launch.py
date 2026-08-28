from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    ExecuteProcess,
    IncludeLaunchDescription,
    TimerAction,
)
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    package_share = get_package_share_directory("qbot_navigation_visualization")
    visualization_launch = PythonLaunchDescriptionSource(
        f"{package_share}/launch/wsl_navigation_visualization.launch.py"
    )

    bag_path = LaunchConfiguration("bag_path")
    playback_rate = LaunchConfiguration("playback_rate")

    visualization = IncludeLaunchDescription(
        visualization_launch,
        launch_arguments={
            "cloud_topic": "/ouster/points",
            "output_frame": "os_lidar",
            "restamp_output": "true",
            "use_sim_time": "false",
            "start_bridge": "true",
        }.items(),
    )

    # Give the processor and bridge time to subscribe before the first cloud.
    playback = TimerAction(
        period=2.0,
        actions=[
            ExecuteProcess(
                cmd=[
                    "ros2", "bag", "play", bag_path,
                    "--rate", playback_rate,
                    "--loop",
                    "--topics", "/ouster/points", "/tf_static",
                ],
                output="screen",
            )
        ],
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "bag_path",
                description="Path to a rosbag directory containing metadata.yaml",
            ),
            DeclareLaunchArgument("playback_rate", default_value="1.0"),
            visualization,
            playback,
        ]
    )
