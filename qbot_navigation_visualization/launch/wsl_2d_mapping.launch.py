from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    package_share = Path(get_package_share_directory("qbot_navigation_visualization"))
    visualization_launch = PythonLaunchDescriptionSource(
        str(package_share / "launch" / "wsl_navigation_visualization.launch.py")
    )

    cloud_topic = LaunchConfiguration("cloud_topic")
    scan_topic = LaunchConfiguration("scan_topic")
    imu_topic = LaunchConfiguration("imu_topic")
    output_frame = LaunchConfiguration("output_frame")
    restamp_local_output = LaunchConfiguration("restamp_local_output")
    start_bridge = LaunchConfiguration("start_bridge")
    start_visualization = LaunchConfiguration("start_visualization")
    use_sim_time = LaunchConfiguration("use_sim_time")
    map_resolution = LaunchConfiguration("map_resolution")
    map_publish_period = LaunchConfiguration("map_publish_period")
    configuration_basename = LaunchConfiguration("configuration_basename")

    mapping_scan_topic = "/navigation/mapping/scan"
    mapping_imu_topic = "/navigation/mapping/imu"

    visualization = IncludeLaunchDescription(
        visualization_launch,
        condition=IfCondition(start_visualization),
        launch_arguments={
            "cloud_topic": cloud_topic,
            "scan_topic": scan_topic,
            "output_frame": output_frame,
            "restamp_output": restamp_local_output,
            "use_sim_time": use_sim_time,
            "start_bridge": start_bridge,
        }.items(),
    )

    mapping_adapter = Node(
        package="qbot_navigation_visualization",
        executable="ouster_mapping_adapter",
        name="ouster_mapping_adapter",
        output="screen",
        parameters=[
            {
                "scan_input_topic": scan_topic,
                "imu_input_topic": imu_topic,
                "scan_output_topic": mapping_scan_topic,
                "imu_output_topic": mapping_imu_topic,
                "mapping_frame": "os_lidar",
                "scan_yaw_offset_radians": 0.0,
                "imu_yaw_offset_radians": 3.141592653589793,
                "use_sim_time": ParameterValue(use_sim_time, value_type=bool),
            }
        ],
    )

    cartographer = Node(
        package="cartographer_ros",
        executable="cartographer_node",
        name="cartographer_node",
        output="screen",
        parameters=[{"use_sim_time": ParameterValue(use_sim_time, value_type=bool)}],
        arguments=[
            "-configuration_directory",
            str(package_share / "config"),
            "-configuration_basename",
            configuration_basename,
        ],
        remappings=[
            ("scan", mapping_scan_topic),
            ("imu", mapping_imu_topic),
            ("tracked_pose", "/navigation/global_pose"),
            ("scan_matched_points2", "/navigation/scan_matched_points"),
        ],
    )

    occupancy_grid = Node(
        package="cartographer_ros",
        executable="cartographer_occupancy_grid_node",
        name="cartographer_occupancy_grid_node",
        output="screen",
        parameters=[{"use_sim_time": ParameterValue(use_sim_time, value_type=bool)}],
        arguments=[
            "-resolution",
            map_resolution,
            "-publish_period_sec",
            map_publish_period,
        ],
        # Keep submap_list on Cartographer's default name. The Humble occupancy
        # node checks that literal name before fetching submap textures.
        remappings=[
            ("map", "/navigation/global_map"),
        ],
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument("cloud_topic", default_value="/ouster/points_viz"),
            DeclareLaunchArgument("scan_topic", default_value="/ouster/scan"),
            DeclareLaunchArgument("imu_topic", default_value="/ouster/imu"),
            DeclareLaunchArgument(
                "output_frame",
                default_value="os_lidar",
                description="Frame for the unchanged current-scan local map",
            ),
            DeclareLaunchArgument("restamp_local_output", default_value="false"),
            DeclareLaunchArgument("start_bridge", default_value="true"),
            DeclareLaunchArgument(
                "start_visualization",
                default_value="true",
                description=(
                    "Start the local LiDAR processor and Foxglove launch. Set false "
                    "when they are already running in a separate terminal."
                ),
            ),
            DeclareLaunchArgument("use_sim_time", default_value="false"),
            DeclareLaunchArgument("map_resolution", default_value="0.05"),
            DeclareLaunchArgument("map_publish_period", default_value="1.0"),
            DeclareLaunchArgument(
                "configuration_basename", default_value="ouster_2d.lua"
            ),
            visualization,
            mapping_adapter,
            cartographer,
            occupancy_grid,
        ]
    )
