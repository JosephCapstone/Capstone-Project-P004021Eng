#!/bin/bash

# This file is located on the PC in /home/ernie/

# This run script runs the following commands to immediately start the 
# RGB camera viewer without the need to type all of these files

# To run this script in the VSCode WSL Ubuntu 22.04 bash terminal type ./run2.sh

###### THINGS TO NOTE
# 1. THE ONLY WAY THIS CODE WORKS IS IF THE WINDOWS FIREWALL ON THE PRIVATE NETWORK IS 
# TURNED OFF (NEED TO MAKE RULE FOR THIS EXCEPTION)
# I WILL CHANGE THE NAME

source /opt/ros/humble/setup.bash #CAN BE MOVED TO THE BASHRC FILE WHICH AUTODOES THIS WHENEVER A NEW TERMINAL IS OPEN (SAME WITH RUN FOR QBOT)
ros2 daemon start
ros2 topic list

ros2 run rqt_image_view rqt_image_view