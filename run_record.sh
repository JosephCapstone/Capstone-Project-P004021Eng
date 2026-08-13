#!/bin/bash

# This file is located on the QBot in /home/nvidia/

# This script runs the following commands:
# 1. Initialises the Ros2 workspace in the ros2/bags folder
# 2. Start the ROS Bag recording
# 3. Allows for manual start and ending of the recording

# To run this script, type "./run_record.sh" in the QBot terminal.

cd ~/ros2/bags
source /opt/ros/humble/setup.bash
source install/setup.bash ## UNSURE IF THIS LINE IS REQUIRED

read -p 'Recording Number ' BAG_NUM
read -p 'Press ENTER to start recording...'

ros2 bag record -o Test "$BAG_NUM" /ouster/points /ouster/imu /ouster/metadata /tf /tf_static /qbot_imu &
BAG_PID=$!

read -n 1 -p "Press q to stop recording: " KEY

if [ "$KEY" = "q" ]; then
    kill $BAG_PID
fi


# Changed the name to be test # will see if that works
# made the big folder lowercase as to fit in lol