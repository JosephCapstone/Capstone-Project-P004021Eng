#!/bin/bash

# This file is located on the QBot in /home/nvidia/

# This script runs the following commands:
# 1. Initialises the Ros2 workspace in the ros2/bags folder
# 2. Start the ROS Bag recording
# 3. Allows for naming of file and manual starting/ending of the recording

# To run this script, type "./run_record.sh" in the QBot terminal.
# Type the number (#) of the current test, the name of the ros bag will be "test_#"
# Press ENTER to start the recording
# To end the recording press "q"

cd ~/ros2/bags
source /opt/ros/humble/setup.bash
source ~/ros2/install/setup.bash

read -p 'Recording Number ' BAG_NUM
read -p 'Press ENTER to start recording...'

ros2 bag record -o "test_$BAG_NUM" /ouster/lidar_packets /ouster/imu_packets /ouster/metadata /tf /tf_static /qbot_imu &
BAG_PID=$!

read -n 1 -p "Press q to stop recording: " KEY

if [ "$KEY" = "q" ]; then
    kill $BAG_PID
fi
