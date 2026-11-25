#!/bin/bash
# Installed on the QBot as /home/nvidia/run_qbot_h.sh.
set -e

cd ~/ros2
source /opt/ros/humble/setup.bash
source install/setup.bash
source ~/hesai_ws/install/setup.bash

setsid ros2 launch qbot_platform qbot_platform_manual_drive_launch.py &
PID1=$!

setsid ros2 run hesai_ros_driver hesai_ros_driver_node --ros-args \
  -p config_path:=/home/nvidia/hesai_ws/src/HesaiLidar_ROS_2.0/config/config.yaml &
PID2=$!

setsid ros2 launch foxglove_bridge foxglove_bridge_launch.xml port:=8765 &
PID3=$!

# Wait for the children to finish so ROS can shut down cleanly.
trap 'kill -INT -- -$PID1 -$PID2 -$PID3 2>/dev/null || true; wait' SIGINT SIGTERM
wait
