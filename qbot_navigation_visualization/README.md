# QBot Navigation Visualization

For fresh installation and the exact live lab procedure, use
[`docs/live_mapping_lab_guide.md`](../docs/live_mapping_lab_guide.md). The
sections below provide package-level technical and playback details.

The Windows DeltaUI_Joseph mapping worker and developer playback workflow are
documented in [`docs/delta_ui_mapping.md`](../docs/delta_ui_mapping.md).

This package creates two lightweight, Foxglove-ready navigation topics in PC
WSL from Ouster ROS 2 topics published by the Jetson. It does not start, stop,
or modify rosbag recording.

## Deployment topology

```text
Jetson: /ouster/points -> topic_tools (5 Hz) -> /ouster/points_viz
                                      | DDS over the existing ROS network
WSL: navigation_visualizer <----------+
     -> /navigation/forward_points
     -> /navigation/local_map
     -> foxglove_bridge :8765
Windows Foxglove Desktop <------------- WebSocket
```

Run `topic_tools` on the Jetson, where `/ouster/points` originates. Running the
throttle in WSL would first transport every full cloud to the PC and would not
save network bandwidth. `/ouster/scan`, `/tf`, and `/tf_static` are consumed
directly in WSL.

## Outputs

| Topic | Type | Purpose |
|---|---|---|
| `/navigation/forward_points` | `sensor_msgs/msg/PointCloud2` | Downsampled 90-degree forward LiDAR view in the configured output frame |
| `/navigation/local_map` | `nav_msgs/msg/OccupancyGrid` | Robot-centred, current-scan local obstacle grid |
| `/navigation/nearest_obstacle` | `std_msgs/msg/Float32` | Nearest valid forward return in metres; NaN when none is available |
| `/navigation/diagnostics` | `diagnostic_msgs/msg/DiagnosticArray` | Input/output rates and stale-topic status |
| `/navigation/global_map` | `nav_msgs/msg/OccupancyGrid` | Session-accumulated Cartographer 2D probability map |
| `/navigation/global_pose` | `geometry_msgs/msg/PoseStamped` | Current Ouster sensor pose in the `map` frame |
| `/navigation/scan_matched_points` | `sensor_msgs/msg/PointCloud2` | Points used for scan-to-submap matching |
| `/navigation/mapping_diagnostics` | `diagnostic_msgs/msg/DiagnosticArray` | Mapping input rates, clock alignment, and rejected data |

The node requires a valid TF from each Ouster message frame to `base_link`.

The two visual products remain independent:

```text
/ouster/points_viz -> forward 90-degree crop -> voxel filter -> /navigation/forward_points
/ouster/scan       -> full 360-degree ray tracing             -> /navigation/local_map
```

The occupancy grid never uses the 3D point cloud. The nearest-obstacle value is
calculated from scan returns inside the same forward 90-degree sector used by
the point-cloud panel.

## 1. Jetson: publish the visualization cloud

Install `topic_tools` if it is not already installed, then publish at 5 Hz:

```bash
source /opt/ros/humble/setup.bash
source ~/ros2/install/setup.bash
ros2 run topic_tools throttle messages \
  /ouster/points 5.0 /ouster/points_viz \
  --ros-args -p lazy:=true
```

This drops complete messages to limit the network rate. The WSL node still
performs the 90-degree crop and voxel filtering, so the Jetson does not perform
the heavier point-level processing.

## 2. WSL: build and run

Copy only `qbot_navigation_visualization` into the WSL ROS workspace. It does
not need to be installed on the Jetson for this topology.

```bash
cd ~/ros2
source /opt/ros/humble/setup.bash
sudo apt update
sudo apt install python3-colcon-common-extensions ros-humble-foxglove-bridge \
  ros-humble-cartographer ros-humble-cartographer-ros \
  ros-humble-nav2-map-server
rosdep install --from-paths src --ignore-src -r -y
colcon build --symlink-install --packages-select qbot_navigation_visualization
source install/setup.bash
ros2 launch qbot_navigation_visualization wsl_navigation_visualization.launch.py
```

The WSL launch starts the visualization node and Foxglove Bridge on port 8765.
It exposes the derived navigation topics, scan, transforms, and QBot battery;
it deliberately does not expose raw `/ouster/points` to Foxglove. The launch is
separate from `ouster_live_record.launch.py` and
`ouster_pcap_to_rosbag.launch.py`.

Before launching, confirm that WSL sees the Jetson data and transform:

```bash
ros2 topic hz /ouster/points_viz
ros2 topic hz /ouster/scan
ros2 run tf2_ros tf2_echo base_link os_lidar
```

The Jetson and WSL shells must use the same `ROS_DOMAIN_ID`. This project
assumes the existing WSL rosbag workflow has already validated DDS discovery.

For a bag that has the complete `base_link` TF chain, bypass the
Jetson-throttled topic and subscribe to the bag's raw point topic:

```bash
ros2 launch qbot_navigation_visualization wsl_navigation_visualization.launch.py \
  cloud_topic:=/ouster/points use_sim_time:=true
```

### Point-cloud-only bag without `base_link`

Use the dedicated playback launch for a recording such as `test_5`, whose
clouds are in `os_lidar` but which has no robot-to-LiDAR transform. It starts
the processor, Foxglove Bridge, and bag playback together. Processing remains
in `os_lidar`, so no fabricated mounting transform is needed. The derived
cloud is stamped at publication time because this recording's embedded Ouster
timestamps do not match its rosbag log time:

```bash
mkdir -p ~/qbot_bags
cp -a /mnt/c/Users/josep/Downloads/test_5/test_5 ~/qbot_bags/

ros2 launch qbot_navigation_visualization wsl_bag_playback.launch.py \
  bag_path:=$HOME/qbot_bags/test_5
```

Keep large SQLite bags on WSL's native filesystem during playback. Reading
this 757 MB cloud bag directly through `/mnt/c` can starve rosbag's read-ahead
queue and deliver the point clouds in short bursts instead of at their recorded
rate.

Only `/ouster/points` and `/tf_static` are replayed. The derived cloud is
published as `/navigation/forward_points` at up to 5 Hz, and the short bag
loops until the launch is stopped with Ctrl+C. In Windows Foxglove,
connect to `ws://localhost:8765`, add a 3D panel, select
`/navigation/forward_points`, and set the display/follow frame to `os_lidar`.

This mode validates the 90-degree crop and voxel filter. It cannot produce the
occupancy grid from `test_5` because that bag does not contain `/ouster/scan`.
For robot-centred live operation, continue using the normal launch with
`output_frame:=base_link` and a valid `base_link -> os_sensor -> os_lidar` TF
chain.

### Scan-only occupancy-grid playback

For a raw-packet bag that already contains `/ouster/scan`, replay only the scan
and static transforms. The grid is centred on `os_lidar` until a measured
`base_link -> os_sensor` transform is available:

```bash
ros2 launch qbot_navigation_visualization wsl_scan_playback.launch.py \
  bag_path:=$HOME/qbot_bags/test_5_scan_2026-08-21
```

In Foxglove, display `/navigation/local_map` in a top-down 3D panel using
`os_lidar` as the display/follow frame. The scan is converted into a fresh
200-by-200 cell grid at 0.1 m resolution for every message; it does not retain
previous scans.

### Live 2D mapping with session memory

The accumulated mapper runs on the PC in WSL and consumes only the Ouster scan
and IMU. Cartographer estimates planar translation by scan matching and uses the
Ouster IMU as its inertial rotation/gravity prior; QBot wheel, command, joint,
and IMU topics are not consumed.

```bash
ros2 launch qbot_navigation_visualization wsl_2d_mapping.launch.py
```

For live lab use, keep `wsl_navigation_visualization.launch.py` running in a
separate terminal and start only the mapper with:

```bash
ros2 launch qbot_navigation_visualization wsl_2d_mapping.launch.py \
  start_visualization:=false start_bridge:=false output_frame:=os_lidar
```

This allows Cartographer to be finalized, stopped, and restarted without
disconnecting the forward LiDAR view or the WSL Foxglove bridge.

The existing `/navigation/local_map` remains a current-scan, robot-centred grid.
The new `/navigation/global_map` retains observations for the lifetime of the
mapping process and normally refreshes once per second. Restarting the launch
starts a clean map.

An internal adapter places both Ouster streams in `os_lidar` and translates the
sensor's internal-clock timestamps onto the current ROS clock while preserving
their relative timing. It leaves the time-ordered laser samples in their native
frame and applies the fixed 180-degree sensor-to-lidar yaw to the IMU vectors.
The mapper therefore does not subscribe to `/tf_static`.
The provisional `/navigation/global_pose` is the Ouster sensor pose rather than
the measured centre of the QBot.

### Accumulated-map bag playback

Replay a bag once into a clean mapping session with:

```bash
ros2 launch qbot_navigation_visualization wsl_2d_mapping_playback.launch.py \
  bag_path:=$HOME/qbot_bags/test_8
```

Only `/ouster/scan` and `/ouster/imu` are replayed. Playback deliberately does
not loop: replaying the same trajectory repeatedly would append it to the same
SLAM session. At the end of the bag, trajectory 0 is finished so Cartographer
runs its final optimization, while the nodes remain alive so the final map can
still be inspected.

In Foxglove, use `os_lidar` for the local occupancy panel and `map` for the
accumulated-map panel. The supplied layout displays `/navigation/global_map`,
`/navigation/global_pose`, and `/navigation/scan_matched_points` together.

## Select the Ouster scan ring

The Ouster `SCAN` processor converts one selected beam into `/ouster/scan`.
For the OS0-128 metadata captured in `test_8`, ring 64 is -0.26 degrees and is
the closest beam to horizontal. The QBot's existing Ouster
`sensor.composite.launch.xml` should therefore use `scan_ring` 64 while keeping
`SCAN` in `proc_mask`. Ring 63 at +0.46 degrees is a useful adjacent
comparison.

1. Keep the robot stationary and place obstacles to the left, centre, and right.
2. Display `/ouster/scan` and `/ouster/points` together in Foxglove.
3. Try candidate `scan_ring` values in the Ouster launch XML or launch
   argument.
4. Choose the beam whose returns align most closely with a horizontal slice at
   useful obstacle height, then save that value in `os0_driver_params.yaml`.

The Ouster lifecycle driver normally needs to be restarted after changing this
parameter.

## 3. Windows Foxglove

Connect Foxglove Desktop to `ws://localhost:8765`. Windows normally forwards
localhost into WSL; if that is disabled, obtain the WSL address with
`hostname -I` and connect to `ws://<WSL-IP>:8765` instead. Import
`foxglove/qbot_low_light_foxglove-layout.json` using **Layouts > Import from
file**. The layout contains:

- a perspective 90-degree forward cloud panel;
- a top-down local occupancy panel with `/ouster/scan` overlaid;
- a top-down accumulated map with global pose and scan-matched points;
- nearest-obstacle, navigation-diagnostic, and mapping-diagnostic panels.

For the current live setup, select `os_lidar` as the display/follow frame for
the forward and local panels and use the panel's reset-view control if a
Foxglove release adjusts imported camera state. The accumulated-map panel uses
`map`. Topic selections and colours remain part of the imported layout.

## Quick validation

```bash
ros2 topic hz /navigation/forward_points
ros2 topic hz /navigation/local_map
ros2 topic hz /navigation/global_map
ros2 topic echo /navigation/global_pose --once
ros2 topic echo /navigation/nearest_obstacle --once
ros2 topic echo /navigation/diagnostics --once
ros2 topic echo /navigation/mapping_diagnostics --once
```

Expected behaviour:

- the forward cloud contains no points behind the robot or outside +/-45 degrees;
- the local grid is 200 by 200 cells at 0.1 m resolution, with the selected
  output frame at cell `(100, 100)`;
- every scan replaces the grid rather than accumulating a global map;
- the full 360-degree scan can mark free and occupied cells around the robot;
- diagnostics report a warning when either Ouster input is absent for one second.
