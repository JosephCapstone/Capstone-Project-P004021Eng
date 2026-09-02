# QBot live LiDAR and 2D mapping lab guide

This guide starts from a fresh GitHub clone and ends with two independent live
WSL processes:

1. a persistent 90-degree LiDAR view and Foxglove bridge;
2. a Cartographer mapping session that can be finalized, saved, stopped, and
   restarted without disconnecting the LiDAR view.

The QBot's existing platform launch and raw-packet recording are not replaced.

## System layout

```text
QBot Jetson
  run_qbot.sh
    -> QBot platform driver
    -> Ouster driver
    -> existing Jetson Foxglove bridge
  topic_tools throttle
    /ouster/points -> /ouster/points_viz at 5 Hz
              |
              | ROS 2 DDS, ROS_DOMAIN_ID=7
              v
Laptop WSL
  navigation_visualizer
    -> /navigation/forward_points
    -> /navigation/local_map
  Cartographer
    -> /navigation/global_map
    -> /navigation/global_pose
  WSL Foxglove bridge on localhost:8765
              |
              v
Windows Foxglove Desktop
```

The WSL bridge is named `wsl_foxglove_bridge`, so it can coexist with the
bridge already started on the Jetson. Connect Windows Foxglove to the WSL
bridge because the derived navigation and mapping topics originate in WSL.

## Values used by this project

| Setting | Value |
|---|---|
| ROS distribution | Humble |
| ROS domain | `7` |
| Mapping/display sensor frame | `os_lidar` |
| Ouster scan ring | `64` |
| Throttled point topic | `/ouster/points_viz` |
| Throttled point rate | `5.0 Hz` |
| Foxglove WebSocket | `ws://localhost:8765` |
| Global map topic | `/navigation/global_map` |
| Saved map format | PGM image plus YAML metadata |

Ring 64 was selected from the captured OS0-128 metadata because it is the beam
closest to horizontal. Do not substitute a fabricated `base_link` transform;
use `os_lidar` until the mounting transform has been measured.

Domain 7 is the project target, but every ROS process must use the same value.
DeltaUI_Joseph exports its configured domain (7 by default) when it starts
`run_qbot.sh`. If you intentionally use the original DeltaUI, verify its domain
and use that same value in every WSL and Jetson shell for the lab session. Never
split the machines across domains.

## 1. One-time Windows and WSL preparation

### Confirm the installed WSL distribution

Open PowerShell:

```powershell
wsl --list --verbose
wsl --version
```

This project expects WSL 2 and Ubuntu 22.04.

### Enable mirrored WSL networking

ROS 2 DDS discovery uses UDP multicast. Default WSL NAT often prevents clean
discovery of a Jetson on the physical LAN. Create or edit
`%USERPROFILE%\.wslconfig` in Windows:

```ini
[wsl2]
networkingMode=mirrored
firewall=true
```

Then restart WSL:

```powershell
wsl --shutdown
wsl -d Ubuntu-22.04
```

Microsoft documents mirrored mode as providing multicast support and direct
LAN reachability: <https://learn.microsoft.com/windows/wsl/networking>.

### Windows network and firewall

Only mark the Ethernet network Private when it is a trusted university/lab
network. In an Administrator PowerShell terminal, first inspect the interfaces:

```powershell
Get-NetConnectionProfile
```

If the lab connection is shown as Public, replace `Ethernet` below with its
actual interface alias:

```powershell
Set-NetConnectionProfile -InterfaceAlias "Ethernet" -NetworkCategory Private
```

Keep Windows Firewall enabled. If mirrored WSL cannot receive ROS traffic, add
a Hyper-V firewall rule for the DDS UDP range used by domain 7:

```powershell
New-NetFirewallHyperVRule `
  -Name "QBot-ROS2-DDS-Domain7" `
  -DisplayName "QBot ROS 2 DDS Domain 7" `
  -Direction Inbound `
  -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' `
  -Protocol UDP `
  -LocalPorts 9150-9250
```

If that cmdlet is unavailable, update WSL and use the Microsoft-documented
Hyper-V firewall procedure. Do not turn off the entire Windows firewall.

## 2. One-time WSL installation from GitHub

Open Ubuntu 22.04 in Windows Terminal or VS Code WSL. Keep the ROS workspace on
the native WSL filesystem, not under `/mnt/c`:

```bash
sudo apt update
sudo apt install -y \
  git \
  python3-colcon-common-extensions \
  python3-rosdep \
  ros-humble-cartographer \
  ros-humble-cartographer-ros \
  ros-humble-foxglove-bridge \
  ros-humble-nav2-map-server

mkdir -p ~/ros2/src
cd ~/ros2/src
git clone --branch codex/live-slam-lab-ready \
  https://github.com/JosephCapstone/Capstone-Project-P004021Eng.git capstone
```

After this feature branch is merged, omit the `--branch` argument to use
`main`.

Install package dependencies and build only the WSL package:

```bash
source /opt/ros/humble/setup.bash
rosdep update
rosdep install \
  --from-paths ~/ros2/src/capstone/qbot_navigation_visualization \
  --ignore-src -r -y

cd ~/ros2
colcon build --symlink-install \
  --packages-select qbot_navigation_visualization \
  --cmake-args -DCMAKE_BUILD_TYPE=Release
source ~/ros2/install/setup.bash
```

Add these environment values to every WSL terminal used in the lab:

```bash
export ROS_DOMAIN_ID=7
export ROS_LOCALHOST_ONLY=0
```

They may be appended to `~/.bashrc` if domain 7 is always used for this QBot.

## 3. One-time Jetson Ouster setting

The repository's current `run_qbot.sh` already launches:

```bash
ros2 launch ouster_ros sensor.launch.xml \
  sensor_hostname:=os-992123000057.local \
  proc_mask:="RAW|PCL|IMU|SCAN|TLM"
```

Therefore `SCAN` is already enabled. The remaining driver setting is ring 64.

### Locate the XML actually used by ROS

On the Jetson:

```bash
source /opt/ros/humble/setup.bash
source ~/ros2/install/setup.bash
ros2 pkg prefix --share ouster_ros
```

`sensor.launch.xml` is normally a link to `sensor.composite.launch.xml`. Find
the source copy so that a future build does not overwrite the change:

```bash
find ~/ros2/src -path '*ouster-ros*/launch/sensor.composite.launch.xml' -print
grep -n 'proc_mask\|scan_ring' \
  ~/ros2/src/ouster-ros/ouster-ros/launch/sensor.composite.launch.xml
```

Adjust the path if the first command reports a different checkout location.
Confirm the XML contains `SCAN` in `proc_mask`, then change:

```xml
<arg name="scan_ring" default="0" ... />
```

to:

```xml
<arg name="scan_ring" default="64" ... />
```

If `run_qbot.sh` is ever allowed to change, passing `scan_ring:=64` on its
Ouster launch command is preferable to changing the vendor default. This guide
does not modify `run_qbot.sh`.

Rebuild only the Ouster packages if the workspace is not symlink-installed:

```bash
cd ~/ros2
source /opt/ros/humble/setup.bash
colcon build --symlink-install --packages-up-to ouster_ros \
  --cmake-args -DCMAKE_BUILD_TYPE=Release
source ~/ros2/install/setup.bash
```

### Install the point-cloud throttle

```bash
sudo apt update
sudo apt install -y ros-humble-topic-tools
```

The throttle subscribes locally on the Jetson, so the full cloud does not need
to cross to WSL before being reduced to 5 Hz.

## 4. Start the QBot and Ouster

Use the DeltaUI_Joseph **Start** action or the existing Jetson command. If starting it from PuTTY:

```bash
cd /home/nvidia
export ROS_DOMAIN_ID=7
export ROS_LOCALHOST_ONLY=0
./run_qbot.sh
```

When using DeltaUI_Joseph, confirm its launched ROS processes inherit domain 7. A
quick check from another Jetson shell is `ROS_DOMAIN_ID=7 ros2 node list`. If
that returns no QBot nodes but domain 0 does, use 0 consistently until the
DeltaUI_Joseph environment is updated.

In another Jetson PuTTY terminal, use the same ROS domain and start the cloud
throttle:

```bash
source /opt/ros/humble/setup.bash
source ~/ros2/install/setup.bash
export ROS_DOMAIN_ID=7
export ROS_LOCALHOST_ONLY=0

ros2 run topic_tools throttle messages \
  /ouster/points 5.0 /ouster/points_viz \
  --ros-args -p lazy:=false
```

Leave this terminal open while using the live point-cloud view. Stopping this
command stops only `/ouster/points_viz`; it does not stop the Ouster driver or
the raw-packet recording.

### Verify the Jetson topics

In a third Jetson shell if necessary:

```bash
source /opt/ros/humble/setup.bash
source ~/ros2/install/setup.bash
export ROS_DOMAIN_ID=7

ros2 param get /ouster/os_cloud scan_ring
ros2 topic hz /ouster/scan
ros2 topic hz /ouster/imu
ros2 topic hz /ouster/points
ros2 topic hz /ouster/points_viz
```

Expected results:

- `scan_ring` is `64`;
- scan, IMU, and point topics publish continuously;
- `/ouster/points_viz` is approximately 5 Hz.

Use Ctrl+C after each `ros2 topic hz` measurement.

## 5. Confirm Jetson-to-WSL discovery

Open a fresh WSL terminal:

```bash
source /opt/ros/humble/setup.bash
source ~/ros2/install/setup.bash
export ROS_DOMAIN_ID=7
export ROS_LOCALHOST_ONLY=0

ros2 daemon stop
ros2 daemon start
ros2 topic list | sort
```

Confirm these topics appear:

```text
/ouster/points_viz
/ouster/scan
/ouster/imu
```

Measure them from WSL:

```bash
ros2 topic hz /ouster/points_viz
ros2 topic hz /ouster/scan
ros2 topic hz /ouster/imu
```

Do not continue to mapping until all three are visible in WSL.

## 6. Start the persistent LiDAR view and Foxglove bridge

In WSL terminal 1:

```bash
source /opt/ros/humble/setup.bash
source ~/ros2/install/setup.bash
export ROS_DOMAIN_ID=7
export ROS_LOCALHOST_ONLY=0

ros2 launch qbot_navigation_visualization \
  wsl_navigation_visualization.launch.py \
  output_frame:=os_lidar
```

This process consumes `/ouster/points_viz` and `/ouster/scan`, then publishes:

```text
/navigation/forward_points
/navigation/local_map
/navigation/nearest_obstacle
/navigation/diagnostics
```

It also starts the WSL Foxglove bridge on port 8765. Leave this terminal
running when stopping or restarting mapping.

## 7. Connect Foxglove on Windows

1. Open Foxglove Desktop.
2. Select **Open connection**.
3. Select **Foxglove WebSocket**.
4. Enter `ws://localhost:8765`.
5. Import this layout from the WSL checkout:

```text
\\wsl.localhost\Ubuntu-22.04\home\<WSL-user>\ros2\src\capstone\qbot_navigation_visualization\foxglove\qbot_low_light_foxglove-layout.json
```

Replace `<WSL-user>` with the output of `whoami` in WSL. The forward and local
panels follow `os_lidar`; the accumulated-map panel uses `map`.

At this point the 90-degree live LiDAR FOV and current-scan local occupancy
view work even if Cartographer is not running.

## 8. Start an independent mapping session

In WSL terminal 2:

```bash
source /opt/ros/humble/setup.bash
source ~/ros2/install/setup.bash
export ROS_DOMAIN_ID=7
export ROS_LOCALHOST_ONLY=0

ros2 launch qbot_navigation_visualization \
  wsl_2d_mapping.launch.py \
  start_visualization:=false \
  start_bridge:=false \
  output_frame:=os_lidar
```

`start_visualization:=false` prevents a second visualizer and bridge. The
mapping process consumes `/ouster/scan` and `/ouster/imu` and publishes:

```text
/navigation/global_map
/navigation/global_pose
/navigation/scan_matched_points
/navigation/mapping_diagnostics
```

The map is session-scoped. Driving slowly, avoiding abrupt starts, and
revisiting distinctive geometry generally gives Cartographer better scan
matches. The map normally refreshes once per second.

### Live checks

In WSL terminal 3:

```bash
source /opt/ros/humble/setup.bash
source ~/ros2/install/setup.bash
export ROS_DOMAIN_ID=7

ros2 topic hz /navigation/forward_points
ros2 topic hz /navigation/global_map
ros2 topic echo /navigation/global_pose --once
ros2 topic echo /navigation/mapping_diagnostics --once
```

## 9. Finish and save a map

Do not press Ctrl+C in the mapping terminal until the map has been saved.

In WSL terminal 3, finish Cartographer trajectory 0:

```bash
ros2 service call /finish_trajectory \
  cartographer_ros_msgs/srv/FinishTrajectory \
  "{trajectory_id: 0}"
```

Confirm the response status reports success. The finished trajectory no longer
accepts new scan data. Wait at least two seconds for final optimization and a
new occupancy-grid publication:

```bash
ros2 topic echo /navigation/global_map --once --field info
```

Create the output directory and save the map. Change the run name each time:

```bash
mkdir -p ~/qbot_maps

ros2 run nav2_map_server map_saver_cli \
  -t /navigation/global_map \
  -f "$HOME/qbot_maps/lab_map_001" \
  --fmt pgm \
  --mode trinary
```

Verify both outputs:

```bash
ls -lh ~/qbot_maps/lab_map_001.pgm \
       ~/qbot_maps/lab_map_001.yaml
```

The YAML contains resolution and origin metadata and refers to the PGM image.
Copy both files together with WinSCP, or open the directory in Windows:

```bash
explorer.exe "$(wslpath -w ~/qbot_maps)"
```

## 10. Stop and restart only mapping

After saving, press Ctrl+C in WSL terminal 2. Do not stop WSL terminal 1.

To begin a clean map, rerun the mapping command from section 8. A new
Cartographer process starts with trajectory 0 and an empty session. Foxglove
remains connected and the LiDAR FOV remains live throughout.

To stop only the live FOV later, press Ctrl+C in WSL terminal 1. To stop the
QBot and Ouster, use the DeltaUI_Joseph/QBot shutdown procedure.

## 11. Updating the WSL checkout

```bash
cd ~/ros2/src/capstone
git pull --ff-only

cd ~/ros2
source /opt/ros/humble/setup.bash
colcon build --symlink-install \
  --packages-select qbot_navigation_visualization \
  --cmake-args -DCMAKE_BUILD_TYPE=Release
source ~/ros2/install/setup.bash
```

## Troubleshooting

### WSL cannot see any Jetson topics

- Confirm both machines use `ROS_DOMAIN_ID=7` and `ROS_LOCALHOST_ONLY=0`.
- Confirm the laptop can ping the Jetson and both are on the same trusted LAN.
- Run `ros2 daemon stop` after changing the domain, then restart it.
- Confirm WSL mirrored networking is active.
- Check the Windows network category and Hyper-V firewall rule.
- Test discovery with a small QBot topic before blaming the large point cloud.

### Scan is absent but point cloud exists

- On the Jetson, inspect `ros2 param get /ouster/os_cloud proc_mask`.
- Confirm the active mask contains `SCAN`.
- Confirm `scan_ring` is 64.
- If XML was edited in `src` without a symlink build, rebuild and resource the
  workspace before restarting `run_qbot.sh`.

### `/ouster/points_viz` is absent

- Confirm `ros-humble-topic-tools` is installed on the Jetson.
- Confirm the throttle terminal is still running.
- Confirm `/ouster/points` itself publishes.
- Restart only the throttle command; the driver does not need to be restarted.

### The local panels are empty

- Confirm the viewer was launched with `output_frame:=os_lidar`.
- In Foxglove, set the forward/local panel display and follow frame to
  `os_lidar`.
- Check `/navigation/diagnostics` for stale input warnings.

### The global map is absent or frozen

- Confirm both `/ouster/scan` and `/ouster/imu` publish in WSL.
- Check `/navigation/mapping_diagnostics` for rate, clock, or rejected-data
  warnings.
- If the Ouster sensor clock reset, stop and restart only the mapping process.
- If `/finish_trajectory` was already called, start a new mapping session;
  finished trajectory 0 cannot accept more data.

### Foxglove cannot connect to localhost:8765

- Confirm WSL terminal 1 is running and did not report a port conflict.
- Run `ss -ltn | grep 8765` in WSL.
- Connect to `ws://localhost:8765`, not the Jetson IP, for WSL-derived topics.
- If localhost forwarding is unavailable, run `hostname -I` in WSL and try
  `ws://<WSL-IP>:8765`.

### Map saving times out

- Confirm `ros-humble-nav2-map-server` is installed.
- Confirm `/navigation/global_map` publishes before running `map_saver_cli`.
- Keep the mapping nodes alive until both output files are written.
- Use a filename stem in a directory that already exists.

### A saved map is blank or badly distorted

- Inspect the accumulated map in Foxglove before saving.
- Confirm the scan ring is 64 rather than the Ouster default ring 0.
- Drive more slowly and include overlapping/revisited geometry.
- Start a clean mapping session after any Ouster driver or clock restart.

## Lab acceptance checklist

- Existing QBot driving and raw-packet recording still work.
- Jetson reports Ouster scan ring 64.
- WSL receives `/ouster/scan`, `/ouster/imu`, and `/ouster/points_viz`.
- `/ouster/points_viz` is approximately 5 Hz.
- Foxglove displays the live 90-degree LiDAR view in `os_lidar`.
- `/navigation/global_map` accumulates while the QBot moves.
- Stopping mapping does not stop the LiDAR view or Foxglove connection.
- Map saving creates a non-empty `.pgm` and matching `.yaml`.
- Restarting mapping produces a clean new map.
