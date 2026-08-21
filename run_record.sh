#!/bin/bash

# This file is located on the QBot in /home/nvidia/

# This script runs the following commands:
# 1. Initialises the Ros2 workspace in the ros2/bags folder
# 2. Start the ROS Bag recording
# 3. Allows for naming of file and manual starting/ending of the recording

# To run this script, use the ALL NEW DeltaUI!!!!!

cd ~/ros2/bags
source /opt/ros/humble/setup.bash
source ~/ros2/install/setup.bash #might not be needed

BAG_NUM=$1

ros2 bag record -o "test_$BAG_NUM" /ouster/lidar_packets /ouster/imu_packets /ouster/metadata /tf /tf_static /qbot_imu /ouster/scan &
