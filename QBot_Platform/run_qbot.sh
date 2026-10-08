#!/bin/bash

# This file is located on the QBot in /home/nvidia/

# This script runs the following commands:
# 1. Builds the Ros2 workspace and compiles any update files
# 2. Runs the QBot launch file
# 3. Runs the Ouster OS0 LiDAR launch file
# 4. Launches the Foxglove bridge launch file

# To run this script, use the ALL NEW DeltaUI!!!!!

cd ~/ros2
source /opt/ros/humble/setup.bash
colcon build
source install/setup.bash

setsid ros2 launch qbot_platform qbot_platform_manual_drive_launch.py &
PID1=$!

setsid ros2 launch ouster_ros sensor.launch.xml sensor_hostname:=os-992123000057.local proc_mask:="RAW|PCL|IMU|SCAN|TLM" &
PID2=$!

setsid ros2 launch foxglove_bridge foxglove_bridge_launch.xml port:=8765 &
PID3=$!

trap 'kill -INT -- -$PID1 -$PID2 -$PID3' SIGINT SIGTERM

wait
