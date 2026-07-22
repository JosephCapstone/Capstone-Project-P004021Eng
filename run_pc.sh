#!/bin/bash

# This file is located on the PC in /home/ernie/

# This script runs the following commands:
# 1. List's all topics broadcasted from the QBot to confirm connection
# 2. Runs the RGB camera viewer

# To run this script, type "./run_pc.sh" in the VSCode WSL Ubuntu 22.04 bash terminal

## THINGS TO NOTE ##
# - THE ONLY WAY THIS CODE WORKS IS IF THE WINDOWS FIREWALL ON THE PRIVATE NETWORK IS TURNED OFF 
#   (A fix is to add a rule to allow data from the qbot to be allowed through the firewall)

source /opt/ros/humble/setup.bash
ros2 daemon start
ros2 topic list

ros2 run rqt_image_view rqt_image_view
rqt_graph
