# DeltaUI_Joseph mapping integration

`DeltaUI_Joseph` is the mapping-enabled Windows PySide application. The
original `QBot_Platform/DeltaUI` remains unchanged. Its backend facade uses SSH
for the existing Jetson scripts and connects to a ROS-aware worker in WSL.
The worker owns mapping processes, observes ROS topics and services, and serves
the live accumulated map to DeltaUI_Joseph on `http://127.0.0.1:8766`.

Closing DeltaUI_Joseph does not stop the QBot, recording, or an active mapping
session. Reopening it reconnects to the existing WSL worker.

## Installation

Install the Windows dependencies from PowerShell:

```powershell
py -m pip install -r QBot_Platform\requirements.txt
```

Launch the mapping-enabled UI (the original `DeltaUI` remains available):

```powershell
py QBot_Platform\DeltaUI_Joseph
```

Install the WSL dependencies and rebuild:

```bash
sudo apt update
sudo apt install -y \
  ros-humble-nav2-map-server \
  ros-humble-cartographer-ros \
  ros-humble-foxglove-bridge

cd ~/ros2_ws
source /opt/ros/humble/setup.bash
colcon build --symlink-install --packages-select qbot_navigation_visualization
source install/setup.bash
```

The Windows backend first checks `DELTA_ROS_WS`, then this repository at
`~/Capstone-Project-P004021Eng`, `~/ros2_ws`, and `~/ros2`. It starts the worker and persistent visualization
automatically. Configuration can be overridden with:

| Variable | Default |
|---|---|
| `DELTA_JETSON_HOST` | `192.168.137.33` |
| `DELTA_JETSON_USERNAME` | `nvidia` |
| `DELTA_JETSON_PASSWORD` | `nvidia` |
| `DELTA_WSL_DISTRO` | `Ubuntu-22.04` |
| `DELTA_ROS_WS` | `/home/ernie/Capstone-Project-P004021Eng` |
| `DELTA_ROS_DOMAIN_ID` | `7` |
| `DELTA_WORKER_URL` | `http://127.0.0.1:8766` |
| `DELTA_MAPS_WINDOWS_DIR` | Windows `Documents\DELTA Maps` |

## Mapping controls

- **Start Mapping** becomes available only when the worker reports `ready` and
  the live scan and IMU topics are fresh.
- **Finish & Save** finishes Cartographer trajectory 0, validates the saved
  PGM/YAML, stops only mapping, and copies the files from `~/qbot_maps` to the
  configured Windows directory without overwriting an existing map.
- **Cancel** stops only the active mapping session and does not write a map.
- **New Map** clears the previous in-memory preview after a saved, cancelled,
  or failed session. It never deletes saved files.

The UI states come from the Jetson process table, recorded-file growth, ROS
topic freshness, ROS node discovery, and `/finish_trajectory` availability.
Messages saying a command was accepted are not treated as proof that a process
is running.

`nav2_map_server` is the preferred saver. If it is not installed, the worker
uses its built-in Nav2-compatible trinary PGM/YAML writer and reports that
backend in its saved-path state; output validation is identical in both paths.

## Rosbag playback demonstration

Playback is intentionally not exposed in the production UI. Open DeltaUI_Joseph so the worker is running, then run this developer
harness from Windows PowerShell:

```powershell
python .\qbot_navigation_visualization\tools\run_delta_ui_playback.py `
  --bag-path /home/josep/qbot_bags/test_8 `
  --scan-ring 64 `
  --rate 2.0
```

For `test_8`, the worker ignores the stored roof-facing `/ouster/scan`. It
reconstructs `/navigation/reconstructed_scan` from `/ouster/lidar_packets` and
the captured metadata using ring 64, the closest OS0-128 beam to horizontal.
This is the same raw-packet path used for the ring-comparison tuning results.

The normal DeltaUI_Joseph map panel shows the rosbag-generated occupancy map and
robot pose. When playback completes, use the UI buttons, or exercise saving from the
harness with a unique name:

```powershell
python .\qbot_navigation_visualization\tools\run_delta_ui_playback.py `
  --bag-path /home/josep/qbot_bags/test_8 `
  --scan-ring 64 `
  --rate 2.0 `
  --finish-and-save delta_map_playback_001
```

Use `--cancel-after 5` instead to verify cancellation. After either save or
cancel, press **New Map** before starting another session.

## Live lab acceptance

1. Start the QBot and wait for `running`, not merely a launch log message.
2. Confirm the recording state becomes `recording` only after its output grows.
3. Start mapping and confirm the map version, known-cell count, and pose update.
4. Finish and save, then verify both WSL and Windows paths shown by the backend.
5. Press New Map, run another session, and verify Cancel leaves no new files.
6. Confirm the persistent Foxglove view remains connected between sessions.
7. Stop the QBot and confirm its process disappears and ROS topics become stale.
