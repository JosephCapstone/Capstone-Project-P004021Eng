#!/bin/bash
# Installed on the QBot as /home/nvidia/run_record_h.sh.
set -e

BAG_NUM=${1:-}
if [[ ! "$BAG_NUM" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$ ]]; then
  echo "Usage: $0 RECORDING_NAME (1-32 letters, numbers, underscores or hyphens)" >&2
  exit 1
fi

source /opt/ros/humble/setup.bash
source ~/ros2/install/setup.bash
source ~/hesai_ws/install/setup.bash
mkdir -p ~/bags
cd ~/bags

exec ros2 bag record -s sqlite3 -o "test_hesai_$BAG_NUM" \
  /qbot_imu \
  /lidar_points \
  /lidar_packets \
  /lidar_ptp \
  /lidar_corrections \
  /lidar_packets_loss \
  /qbot_speed_feedback \
  /qbot_joint \
  /tf \
  /tf_static
