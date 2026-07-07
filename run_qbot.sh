#!/bin/bash

# This file is located on the QBot in /home/nvidia/

# This script runs the following commands:
# 1. Builds the Ros 2 workspace to compile any update files
# 2. Runs the QBot launch file

# To run this script, type "./run_qbot.sh" in the QBot terminal.

cd ~/ros2
source /opt/ros/humble/setup.bash
colcon build
source install/setup.bash

# export ROS_DOMAIN_ID=7 (USED TO CONFIRM CONNECTION BETWEEN TWO DEVICES ISNT REALLY REQUIRED IF WE NEED WILL UNCOMMENT)
# echo $ROS_DOMAIN_ID

ros2 launch qbot_platform qbot_platform_manual_drive_launch.py