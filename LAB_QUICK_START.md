# DeltaUI_Joseph live mapping lab quick start

This repository has already been overlaid with the mapping integration. The
original `QBot_Platform/DeltaUI` is preserved; use
`QBot_Platform/DeltaUI_Joseph` for the mapping-enabled application.

The production workflow uses two computers:

- DeltaUI_Joseph runs as a native Windows PySide application.
- The mapping worker, Cartographer, map rendering, and Foxglove bridge run in
  Ubuntu 22.04 WSL.
- The existing Jetson at `192.168.137.33` runs the unchanged QBot and recording
  scripts.
- Every ROS process uses domain `7`.

## 1. One-time Windows setup

Open PowerShell in the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\QBot_Platform\requirements.txt
```

## 2. One-time WSL build

Open Ubuntu 22.04. The commands below select the existing `~/ros2_ws` workspace,
or `~/ros2` if that is the workspace present on the lab PC. Adjust the Windows
source path if the checkout is stored somewhere else.

```bash
source /opt/ros/humble/setup.bash

if [ -d "$HOME/ros2_ws" ]; then
  DELTA_ROS_WS="$HOME/ros2_ws"
else
  DELTA_ROS_WS="$HOME/ros2"
fi

mkdir -p "$DELTA_ROS_WS/src"
cp -a \
  /mnt/c/Users/josep/OneDrive/Documents/SLAM_GUI/qbot_current/qbot_navigation_visualization \
  "$DELTA_ROS_WS/src/"

cd "$DELTA_ROS_WS"
rosdep install \
  --from-paths src/qbot_navigation_visualization \
  --ignore-src -r -y
colcon build --symlink-install \
  --packages-select qbot_navigation_visualization
source install/setup.bash
```

If `rosdep` cannot install `nav2_map_server`, mapping still has a compatible
built-in PGM/YAML saver. Cartographer, Ouster ROS, and Foxglove Bridge are still
required.

## 3. Confirm the Jetson scan ring

The OS0-128 must publish ring `64`, the near-horizontal beam at approximately
`-0.26` degrees. Ring `0` points toward the roof and produces a misleading map.

After starting the QBot, check from a Jetson terminal:

```bash
export ROS_DOMAIN_ID=7
source /opt/ros/humble/setup.bash
source ~/ros2/install/setup.bash
ros2 param get /ouster/os_cloud scan_ring
```

The result must be `64`. If it is not, follow **One-time Jetson Ouster setting**
in `docs/live_mapping_lab_guide.md`. The existing `run_qbot.sh` remains
unchanged.

## 4. Run a live mapping session

Connect Windows, WSL, and the Jetson to the lab network, then start
DeltaUI_Joseph from PowerShell in the repository root:

```powershell
.\.venv\Scripts\python.exe .\QBot_Platform\DeltaUI_Joseph
```

Use this button order:

1. Click **Start** in the QBot controls.
2. Wait for QBot state `running`. `starting` only means the command was issued;
   `running` requires the remote process plus fresh scan, IMU, and platform
   feedback topics.
3. Optionally enter a recording number, click **Start Recording**, and wait for
   state `recording`. This requires a real recorder process and growing output.
4. Click **Start Mapping**. This is a separate step; starting the QBot does not
   automatically create a mapping trajectory.
5. Drive slowly and revisit overlapping walls. The accumulated map and red
   current-pose marker appear automatically in DeltaUI_Joseph.
6. Edit the proposed map name if required, then click **Finish & Save**. Wait for
   `saved`; the UI verifies the PGM/YAML pair before stopping the mapper.
7. Confirm the outputs in both locations:
   - WSL: `~/qbot_maps`
   - Windows: `%USERPROFILE%\Documents\DELTA Maps`
8. Click **New Map** before another session.
9. Stop recording and the QBot explicitly when finished. Closing DeltaUI_Joseph does
   not stop active robot, recorder, or mapping processes.

Use **Cancel** instead of **Finish & Save** to stop mapping without creating or
deleting map files. The persistent LiDAR visualizer and Foxglove bridge remain
running between mapping sessions.

## 5. Optional `test_8` playback

Playback is only a developer check; live operation is the acceptance target.
Open DeltaUI_Joseph, then run this in a second PowerShell window:

```powershell
.\.venv\Scripts\python.exe `
  .\qbot_navigation_visualization\tools\run_delta_ui_playback.py `
  --rate 0.5
```

The harness reconstructs ring 64 from the bag's raw Ouster packets and ignores
its stored roof-facing `/ouster/scan`. Raw-packet decoding can lag in playback;
that extra decoding does not occur in the live path.

## Quick troubleshooting

- **Jetson offline:** confirm `ping 192.168.137.33` and SSH access for user
  `nvidia`, then check the lab network.
- **Worker unavailable:** in WSL run
  `tail -100 /tmp/delta_mapping_worker.log` and confirm the package was built in
  this repository, `~/ros2_ws/install`, or `~/ros2/install`.
- **Start Mapping rejected:** confirm `/ouster/scan` and `/ouster/imu` are fresh
  on domain 7 and no old Cartographer process is running.
- **Blank or roof-shaped map:** check `/ouster/os_cloud` reports `scan_ring=64`.
- **Mapping starts but no map appears:** inspect
  `tail -100 /tmp/delta_mapping.log` in WSL.
- **Foxglove:** connect to `ws://localhost:8765`. The DeltaUI_Joseph map panel does not require
  Foxglove to be open.
- **Save copy failure:** the verified WSL files remain safe in `~/qbot_maps`; the
  Windows copy can be retried after resolving the reported path/collision error.

For full setup, networking, topic checks, and Jetson instructions, read
`docs/live_mapping_lab_guide.md`. For backend/API details, read
`docs/delta_ui_mapping.md`.
