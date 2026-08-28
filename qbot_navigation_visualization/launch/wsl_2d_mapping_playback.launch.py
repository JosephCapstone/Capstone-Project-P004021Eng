from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    ExecuteProcess,
    IncludeLaunchDescription,
    RegisterEventHandler,
    TimerAction,
)
from launch.event_handlers import OnProcessExit
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    package_share = get_package_share_directory("qbot_navigation_visualization")
    mapping_launch = PythonLaunchDescriptionSource(
        f"{package_share}/launch/wsl_2d_mapping.launch.py"
    )

    bag_path = LaunchConfiguration("bag_path")
    playback_rate = LaunchConfiguration("playback_rate")
    configuration_basename = LaunchConfiguration("configuration_basename")
    start_bridge = LaunchConfiguration("start_bridge")

    mapping = IncludeLaunchDescription(
        mapping_launch,
        launch_arguments={
            "cloud_topic": "/navigation/unused_points",
            "scan_topic": "/ouster/scan",
            "imu_topic": "/ouster/imu",
            "output_frame": "os_lidar",
            "restamp_local_output": "true",
            "use_sim_time": "false",
            "start_bridge": start_bridge,
            "configuration_basename": configuration_basename,
        }.items(),
    )

    playback = ExecuteProcess(
        cmd=[
            "ros2",
            "bag",
            "play",
            bag_path,
            "--rate",
            playback_rate,
            "--topics",
            "/ouster/scan",
            "/ouster/imu",
        ],
        output="screen",
    )

    finish_trajectory = ExecuteProcess(
        cmd=[
            "ros2",
            "service",
            "call",
            "/finish_trajectory",
            "cartographer_ros_msgs/srv/FinishTrajectory",
            "{trajectory_id: 0}",
        ],
        output="screen",
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "bag_path",
                description="Path to a rosbag directory containing metadata.yaml",
            ),
            DeclareLaunchArgument("playback_rate", default_value="1.0"),
            DeclareLaunchArgument(
                "configuration_basename", default_value="ouster_2d.lua"
            ),
            DeclareLaunchArgument("start_bridge", default_value="true"),
            mapping,
            TimerAction(period=2.0, actions=[playback]),
            RegisterEventHandler(
                OnProcessExit(
                    target_action=playback,
                    on_exit=[TimerAction(period=1.0, actions=[finish_trajectory])],
                )
            ),
        ]
    )
