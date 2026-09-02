# Ouster to ROS 2 Bag Capture Plan

The immediate goal is to make the Ouster OS0 data a ROS 2 dataset, not just a
PCAP or Ouster Studio artifact. Use PCAP/JSON at home for replay tests, but use
ROS 2 bags as the benchmark format.

## Install Workspace

On the PC/WSL or Jetson workspace:

```bash
mkdir -p ~/ros2/src
cd ~/ros2/src
git clone --branch humble-devel --recurse-submodules https://github.com/ouster-lidar/ouster-ros.git
git clone https://github.com/JosephCapstone/Capstone-Project-P004021Eng.git capstone
```

The useful packages are:

```text
capstone/QBot_Platform/qbot_platform
capstone/qbot_slam_bringup
ouster-ros/ouster-ros
ouster-ros/ouster-sensor-msgs
```

Build:

```bash
cd ~/ros2
source /opt/ros/humble/setup.bash
rosdep update
rosdep install --from-paths src --ignore-src -r -y
colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Release
source install/setup.bash
```

## Home Test: PCAP + JSON to ROS Bag

Use this for the recordings you already have:

```bash
ros2 launch qbot_slam_bringup ouster_pcap_to_rosbag.launch.py \
  pcap_file:=/path/to/recording.pcap \
  metadata:=/path/to/metadata.json \
  bag_dir:=~/qbot_bags \
  bag_name:=home_pcap_test_001
```

Check:

```bash
ros2 bag info ~/qbot_bags/home_pcap_test_001
ros2 bag play ~/qbot_bags/home_pcap_test_001
ros2 topic echo /ouster/metadata --once
```

## Uni Test: Live Ouster to ROS Bag

First find the sensor IP/hostname. Then run:

```bash
ros2 launch qbot_slam_bringup ouster_live_record.launch.py \
  sensor_hostname:=<OUSTER_IP_OR_HOSTNAME> \
  bag_dir:=~/qbot_bags \
  bag_name:=stationary_001 \
  x:=0.0 y:=0.0 z:=0.2 roll:=0.0 pitch:=0.0 yaw:=0.0
```

If packets do not arrive, set `udp_dest` to the IP address of the machine that
is running the launch file:

```bash
ros2 launch qbot_slam_bringup ouster_live_record.launch.py \
  sensor_hostname:=<OUSTER_IP_OR_HOSTNAME> \
  udp_dest:=<THIS_MACHINE_IP> \
  bag_name:=stationary_001
```

## Topics Recorded

The live launch records:

```text
/ouster/points
/ouster/points2
/ouster/imu
/ouster/metadata
/ouster/lidar_packets
/ouster/imu_packets
/ouster/scan
/tf
/tf_static
/cmd_vel
/qbot_speed_feedback
/qbot_battery
/qbot_imu
/qbot_joint
/camera/color_image
/camera/depth_image
/camera/color/image_raw
/camera/color/camera_info
/camera/depth/image_rect_raw
/camera/depth/camera_info
/camera/imu
```

Missing topics are acceptable during early tests; the recorder will use the
ones that exist.

The current QBot package publishes `qbot_speed_feedback`, `qbot_imu`,
`qbot_joint`, `qbot_battery`, `cmd_vel`, `camera/color_image`, and
`camera/depth_image`. It does not currently publish a standard `/odom` topic,
so wheel-odometry integration should be a follow-up before navigation or formal
trajectory benchmarking.

## Tomorrow's Minimum Good Dataset

Record these short bags:

```text
stationary_001        30 seconds, robot still
handheld_slow_pan_001 30-60 seconds, LiDAR not mounted yet
qbot_straight_001     slow straight drive if mounting is safe
qbot_square_001       slow square loop if mounting is safe
environment_001       normal exploration run
```

If the LiDAR is not mounted, do not force it. A stable temporary bracket is
useful only if it is rigid and the LiDAR cannot rotate or slide. For SLAM,
unknown motion between the LiDAR and robot body is worse than no mount.

## Mounting Notes

For a first printed bracket, prioritize:

```text
rigid mount
known forward direction
known height above base_link
clear cable strain relief
no occlusion by QBot body
no wobble during turns
easy measurement of x/y/z/roll/pitch/yaw
```

Write approximate transform values into the launch command. Replace them with
measured values later.
