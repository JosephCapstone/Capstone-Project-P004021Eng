#!/bin/bash

# This file is located in /home/nvidia/

# This run script runs the following commands to immediately start the 
# qbot platform launch code without the need to type all of these files

# To run this script in the Qbot terminal type "./run.sh"

cd ~/ros2
source /opt/ros/humble/setup.bash
source install/setup.bash

# export ROS_DOMAIN_ID=7 (USED TO CONFIRM CONNECTION BETWEEN TWO DEVICES ISNT REALLY REQUIRED IF WE NEED WILL UNCOMMENT)
# echo $ROS_DOMAIN_ID

ros2 launch qbot_platform qbot_platform_manual_drive_launch.py