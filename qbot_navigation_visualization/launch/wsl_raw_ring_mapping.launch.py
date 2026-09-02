"""
Decode one horizontal Ouster ring and feed it into the normal mapper.

This launch intentionally does not play a bag or finish a trajectory.  The
Delta mapping worker owns those lifecycle operations so playback behaves like
the live UI workflow.
"""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    package_share = Path(get_package_share_directory("qbot_navigation_visualization"))
    mapping_launch = PythonLaunchDescriptionSource(
        str(package_share / "launch" / "wsl_2d_mapping.launch.py")
    )
    scan_ring = LaunchConfiguration("scan_ring")
    configuration_basename = LaunchConfiguration("configuration_basename")
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
            "start_visualization": "false",
            "start_bridge": "false",
            "configuration_basename": configuration_basename,
        }.items(),
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument("scan_ring", default_value="64"),
            DeclareLaunchArgument(
                "configuration_basename",
                default_value="ouster_2d_horizontal_tuned.lua",
            ),
            decoder,
            mapping,
        ]
    )
