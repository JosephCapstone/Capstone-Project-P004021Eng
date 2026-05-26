#!/bin/bash

# This file is in /home/nvidia/

# This run script runs the following commands to immediately start the 
# qbot platform launch code without the need to type all of these files


cd ~/ros2
source /opt/ros/humble/setup.bash
source install/setup.bash
ros2 launch qbot_platform qbot_platform_manual_drive_launch.py