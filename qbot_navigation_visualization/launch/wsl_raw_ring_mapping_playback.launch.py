from pathlib import Path

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
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    package_share = Path(get_package_share_directory("qbot_navigation_visualization"))
    ouster_share = Path(get_package_share_directory("ouster_ros"))
    mapping_launch = PythonLaunchDescriptionSource(
        str(package_share / "launch" / "wsl_2d_mapping.launch.py")
    )

    bag_path = LaunchConfiguration("bag_path")
    playback_rate = LaunchConfiguration("playback_rate")
    scan_ring = LaunchConfiguration("scan_ring")
    configuration_basename = LaunchConfiguration("configuration_basename")
    start_bridge = LaunchConfiguration("start_bridge")
    reconstructed_scan = "/navigation/reconstructed_scan"

    decoder = Node(
        package="ouster_ros",
        executable="os_cloud",
        name="horizontal_ring_decoder",
        output="screen",
        parameters=[
            {
                "proc_mask": "SCAN",
                "scan_ring": ParameterValue(scan_ring, value_type=int),
                "use_system_default_qos": True,
                "pub_static_tf": False,
                "point_cloud_frame": "os_lidar",
            }
        ],
        remappings=[
            ("metadata", "/ouster/metadata"),
            ("lidar_packets", "/ouster/lidar_packets"),
            ("scan", reconstructed_scan),
        ],
    )

    mapping = IncludeLaunchDescription(
        mapping_launch,
        launch_arguments={
            "cloud_topic": "/navigation/unused_points",
            "scan_topic": reconstructed_scan,
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
            "/ouster/metadata",
            "/ouster/lidar_packets",
            "/ouster/imu",
            "--qos-profile-overrides-path",
            str(ouster_share / "config" / "metadata-qos-override.yaml"),
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
            DeclareLaunchArgument("bag_path"),
            DeclareLaunchArgument("playback_rate", default_value="2.0"),
            DeclareLaunchArgument("scan_ring", default_value="64"),
            DeclareLaunchArgument(
                "configuration_basename", default_value="ouster_2d.lua"
            ),
            DeclareLaunchArgument("start_bridge", default_value="true"),
            decoder,
            mapping,
            TimerAction(period=2.0, actions=[playback]),
            RegisterEventHandler(
                OnProcessExit(
                    target_action=playback,
                    on_exit=[TimerAction(period=1.5, actions=[finish_trajectory])],
                )
            ),
        ]
    )
