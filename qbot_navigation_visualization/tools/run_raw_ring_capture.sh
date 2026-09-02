#!/usr/bin/env bash

set -eo pipefail

if [[ $# -ne 4 ]]; then
  echo "usage: $0 RING CONFIGURATION_BASENAME RESULT_NAME OUTPUT_DIRECTORY" >&2
  exit 2
fi

ring=$1
configuration_basename=$2
result_name=$3
output_directory=$4
launch_pid=""

cleanup() {
  if [[ -n "$launch_pid" ]]; then
    kill -TERM -- "-$launch_pid" 2>/dev/null || true
    sleep 2
    kill -KILL -- "-$launch_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

source /opt/ros/humble/setup.bash
source /home/josep/ros2_ws/install/setup.bash
cd /home/josep/ros2_ws

setsid ros2 launch qbot_navigation_visualization \
  wsl_raw_ring_mapping_playback.launch.py \
  bag_path:=/home/josep/qbot_bags/test_8 \
  playback_rate:=2.0 \
  scan_ring:="$ring" \
  start_bridge:=false \
  configuration_basename:="$configuration_basename" \
  > "/tmp/qbot_${result_name}.log" 2>&1 &
launch_pid=$!

python3 \
  /home/josep/ros2_ws/src/qbot_navigation_visualization/tools/capture_mapping_result.py \
  --name "$result_name" \
  --output-dir "$output_directory" \
  --seconds 34
