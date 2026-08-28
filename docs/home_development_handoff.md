# QBot navigation, mapping, and DeltaUI development handoff

## Purpose

This document contains the context needed to continue development away from the
lab. It records the architecture that was actually exercised on 28 August 2026,
the evidence collected during the live test, the saved artifacts, known
limitations, relevant source files, and a prioritized implementation plan.

The hardware was packed up after the audit. Any IP address or process state in
this document is therefore a historical lab snapshot, not a guaranteed value for
the next session.

## Snapshot summary

- Lab computer: Windows laptop running Ubuntu 22.04 under WSL2.
- ROS distribution: ROS 2 Humble.
- WSL repository: `/home/ernie/Capstone-Project-P004021Eng`.
- Git branch at handoff: `main`.
- Base commit at handoff: `478f136cb501b32d7a70de9b5bf61d5d240d044e`.
- Package name: `qbot_navigation_visualization` (American spelling).
- Lab ROS domain: `0` for this session. The project documentation targets domain
  `7`, but every process must use the same domain; never mix domains.
- Hotspot addresses during the test:
  - WSL: `192.168.137.46/24`
  - Jetson: `192.168.137.33`
- Ouster scan ring: `64`.
- Foxglove URL used from Windows: `ws://localhost:8765`.
- Mapping trajectory 0 was finalized and the mapper shut down cleanly.
- The persistent WSL visualizer and bridge were left running at the time of the
  audit, but this runtime state is not expected to survive shutdown or packing.
- Existing package test result: 54 tests, 0 errors, 0 failures, 8 skipped.

### Critical Git portability warning

At handoff time, Git reported all of the following as **untracked**:

```text
qbot_navigation_visualization/
docs/
build/
install/
log/
```

This means a clone or pull of `main` at the base commit above will **not** contain
the navigation/mapping package, live lab guide, or this handoff. Before moving
development to another machine, deliberately add, commit, and push the source
and documentation directories. Do not add the generated build products:

```bash
# First add appropriate generated-artifact rules to .gitignore, for example:
# build/
# install/
# log/
# **/__pycache__/
# *.pyc

git add .gitignore qbot_navigation_visualization docs
git status --short
git diff --cached --check
git commit -m "Add QBot navigation visualization and development handoff"
git push
```

Review the staged list before committing. The command above is a transfer
checklist, not evidence that a commit or push has already happened.


## Tested system architecture

```text
QBot / Jetson
  run_qbot.sh
    +-- qbot_platform launch
    |     +-- QUARC physical driver model
    |     +-- QBotPlatformDriver
    |     +-- RealsenseCamera
    |     `-- joysticCommands
    +-- ouster_ros os_driver
    |     +-- /ouster/scan
    |     +-- /ouster/imu
    |     +-- /ouster/points
    |     `-- /tf_static
    `-- Jetson foxglove_bridge :8765

  separate topic_tools process
    /ouster/points -> message-rate throttle -> /ouster/points_viz

                     ROS 2 DDS over laptop hotspot
                                  |
                                  v

Laptop / WSL
  persistent visualization launch
    +-- navigation_visualizer
    |     +-- /navigation/local_map
    |     +-- /navigation/nearest_obstacle
    |     +-- /navigation/diagnostics
    |     `-- /navigation/forward_points (requires point cloud)
    `-- wsl_foxglove_bridge :8765

  independent mapping launch
    +-- ouster_mapping_adapter
    |     +-- normalizes scan and IMU frame/timestamps
    |     +-- /navigation/mapping/scan
    |     `-- /navigation/mapping/imu
    +-- cartographer_node
    |     +-- /navigation/global_pose
    |     `-- /navigation/scan_matched_points
    `-- cartographer_occupancy_grid_node
          `-- /navigation/global_map

                                  |
                                  v

Windows
  Foxglove Studio -> ws://localhost:8765
  DeltaUI prototype -> SSH control of the Jetson
```

The WSL visualizer/bridge and mapper are deliberately separate. Mapping can be
finished, saved, stopped, and restarted without disconnecting Foxglove or the
local scan view.

## Live audit results

### Network and discovery

The Jetson was reachable from WSL with 0% packet loss, but latency varied from
approximately 6 ms to 137 ms. This jitter is material for bursty DDS traffic.
Both machines used `ROS_DOMAIN_ID=0` and `ROS_LOCALHOST_ONLY=0` during the test.

### Jetson health

- Ouster lifecycle state: `active [3]`.
- Ouster scan ring: `64`.
- Memory: about 6.0 GiB available out of 7.3 GiB.
- Disk: about 182 GiB free; 18% used.
- Load average during the audit: approximately 5.29 / 4.76 / 4.00.
- Notable CPU consumption:
  - `qbot_platform_driver_interface`: approximately 159% CPU.
  - joystick `command`: approximately 135% CPU.
  - RGB-D node: approximately 17% CPU.
  - Ouster driver: approximately 14% CPU.
  - point-cloud throttle: approximately 2% CPU.

The high platform and joystick CPU consumption must be treated as a production
blocker until profiled and corrected.

### Topic health measured in WSL

| Topic | Observed state | Meaning |
|---|---:|---|
| `/ouster/scan` | about 10 Hz | Healthy enough for local and global 2D mapping |
| `/ouster/imu` | about 54 Hz in the initial audit | Reached the mapper, but burst timing was irregular |
| `/navigation/local_map` | about 10 Hz | Healthy scan-derived local occupancy output |
| `/navigation/nearest_obstacle` | 0.407 m sample | Valid scan-derived output |
| `/qbot_battery` | 12.43 V sample | Present; other capacity fields are NaN |
| `/qbot_speed_feedback` | zero motion sample | Robot stationary at measurement time |
| `/ouster/points_viz` | 0 Hz | Publisher discovered, but no cloud arrived in WSL |
| `/navigation/forward_points` | 0 Hz | Cannot publish without `/ouster/points_viz` |

The navigation diagnostics correctly reported a fresh scan and a stale/missing
cloud. Local scan mapping worked even though the 3D forward-cloud panel was
empty.

### Point-cloud transport

The throttle on the Jetson was configured correctly:

```text
input_topic: /ouster/points
output_topic: /ouster/points_viz
msgs_per_sec: 5.0
lazy: false
```

Local Jetson measurements during the initial check were approximately 2.4 Hz
for `/ouster/points` and 1.6 Hz for `/ouster/points_viz`. WSL discovered the
publisher but received no complete messages.

The important architectural issue is that `topic_tools throttle messages`
reduces message frequency, not message size. Each selected message remains a
complete OS0-128 point cloud and is still too large or fragmented for reliable
hotspot delivery.

### Mapping quality and timestamp warnings

The mapping session produced a live global map at exactly 1 Hz and a valid
global pose. It inserted 13 submaps and completed final optimization. However:

- The mapping adapter logged six rate-limited non-monotonic timestamp errors.
- Cartographer logged 293 `Dropped ... earlier points` warning events.
- Live diagnostics showed a cumulative rejected-timestamp count that was still
  increasing while mapping.

The adapter protected Cartographer by rejecting regressing timestamps, so the
session completed, but this is evidence of input timing/order instability. The
hotspot, DDS burst delivery, and Ouster timestamp mode all need investigation.

### Saved map

The map was saved outside the repository on the lab WSL filesystem:

```text
/home/ernie/qbot_maps/first_test.pgm
/home/ernie/qbot_maps/first_test.yaml
```

Properties:

- 301 x 278 cells.
- 0.05 m/cell.
- Approximate bounding area: 15.05 x 13.9 m.
- Origin: `[-7.18, -3.6, 0]`.
- 1,281 occupied pixels.
- 17,667 free pixels.
- 64,730 unknown pixels (about 77% of the bounding box).

Checksums from the lab machine:

```text
c005ce77054754468241c9fb7c083fc6500f2756b557023a7a68a4fe9cde3c88  first_test.pgm
8c6bb3578155fc616aeae816e19e89927ce9a5cc45e2fdd6233885a3b629cd93  first_test.yaml
```

These files are not automatically transferred by Git. Copy them separately if
they are needed at home. The Windows path on the lab computer was:

```text
\\wsl.localhost\Ubuntu-22.04\home\ernie\qbot_maps
```

Only PGM/YAML were saved. No Cartographer `.pbstream` was saved, so `first_test`
is a static occupancy map and cannot resume the original Cartographer session.

## Source file guide

Edit source files, not generated `build/`, `install/`, or `log/` artifacts.

### Current UI and Jetson control

| File | Purpose and current issue |
|---|---|
| `QBot_Platform/DeltaUI` | PySide6 prototype. Hardcoded old IP and credentials, blocking SSH calls on the UI thread, no command-result checking, no state machine, placeholder camera panel, and no live ROS health model. |
| `run_qbot.sh` | Jetson start script. Builds the entire ROS workspace on every runtime start, then launches QBot, Ouster, and a Jetson bridge. Domain and scan ring are not explicit arguments. |
| `QBot_Platform/qbot_platform/launch/qbot_platform_manual_drive_launch.py` | Starts the QUARC model, camera, joystick command node, and platform interface. |
| `QBot_Platform/qbot_platform/src/command.cpp` | Joystick publisher. Contains an unbounded `while (rclcpp::ok())` loop inside a 100 ms timer callback with no rate limiter. Likely cause of the observed 135% CPU. |
| `QBot_Platform/qbot_platform/src/qbot_platform_driver_interface.cpp` | 16 ms platform communication loop, battery/IMU/joint/speed feedback, and command watchdog. Observed at about 159% CPU and needs profiling. |
| `QBot_Platform/qbot_platform/src/rgbd.cpp` | RealSense image publisher used for future native UI camera display. |
| `QBot_Platform/qbot_platform/package.xml` | Currently invalid in this checkout: empty maintainer email and placeholder description/license. This caused colcon package-identification warnings. |

The current UI embeds a recording name directly into a shell command and uses
`pkill` patterns for stopping. Treat both as unsafe until inputs and process
ownership are explicit.

### WSL navigation and mapping

| File | Purpose |
|---|---|
| `qbot_navigation_visualization/package.xml` | ROS dependencies and package metadata. |
| `qbot_navigation_visualization/CMakeLists.txt` | Builds both runtime executables, core libraries, and tests. |
| `qbot_navigation_visualization/src/navigation_visualizer.cpp` | Point-cloud crop/voxel output, local scan occupancy grid, nearest obstacle, and diagnostics. |
| `qbot_navigation_visualization/src/ouster_mapping_adapter.cpp` | ROS node that normalizes scan/IMU timestamps and publishes mapping diagnostics. |
| `qbot_navigation_visualization/src/ouster_mapping_adapter_core.cpp` | Testable timestamp and frame-normalization logic. |
| `qbot_navigation_visualization/src/local_grid_builder.cpp` | Current-scan local occupancy-grid implementation. |
| `qbot_navigation_visualization/config/navigation_visualization.yaml` | Cloud FOV, voxel size, range limits, 0.1 m local grid, and stale timeout. |
| `qbot_navigation_visualization/config/ouster_2d.lua` | Main Cartographer 2D configuration. Alternate tuning files are in the same directory. |
| `qbot_navigation_visualization/launch/wsl_navigation_visualization.launch.py` | Persistent local visualizer and WSL Foxglove bridge. |
| `qbot_navigation_visualization/launch/wsl_2d_mapping.launch.py` | Independent live mapping adapter, Cartographer, and occupancy-grid node. |
| `qbot_navigation_visualization/foxglove/qbot_low_light_foxglove-layout.json` | Tested Foxglove layout. Local panels follow `os_lidar`; global panel follows `map`. |
| `qbot_navigation_visualization/test/` | Unit tests for local-grid generation and mapping normalization. |
| `docs/live_mapping_lab_guide.md` | Full lab bring-up, Foxglove, finish/save, troubleshooting, and acceptance instructions. |

## Reproducing the development environment at home

Keep the workspace on WSL's native Linux filesystem, not `/mnt/c`.

```bash
source /opt/ros/humble/setup.bash

sudo apt update
sudo apt install -y \
  git \
  python3-colcon-common-extensions \
  python3-rosdep \
  ros-humble-cartographer \
  ros-humble-cartographer-ros \
  ros-humble-foxglove-bridge \
  ros-humble-nav2-map-server
```

Clone or pull the branch that contains this handoff, then install dependencies
and build only the WSL package:

```bash
mkdir -p ~/ros2/src
cd ~/ros2/src
# Clone the repository/branch used by the team, or copy the existing checkout.

rosdep install \
  --from-paths ~/ros2/src/Capstone-Project-P004021Eng/qbot_navigation_visualization \
  --ignore-src -r -y

cd ~/ros2
colcon build --symlink-install \
  --packages-select qbot_navigation_visualization \
  --cmake-args -DCMAKE_BUILD_TYPE=Release

source ~/ros2/install/setup.bash
colcon test --packages-select qbot_navigation_visualization
colcon test-result --verbose
```

The exact clone directory may differ; adjust paths accordingly. Before leaving
the lab or changing machines, record and push the active branch and commit:

```bash
git branch --show-current
git rev-parse HEAD
git status --short
```

## Known limitations and required investigations

### 1. Replace message-only point-cloud throttling

Preferred design: crop and voxel-filter on the Jetson, then send a smaller
forward cloud to WSL. Options, in recommended order:

1. Deploy the existing forward-cloud processing logic on the Jetson and publish
   only the reduced `/navigation/forward_points` product.
2. Extract a dedicated Jetson `PointCloud2` filter node that crops to the
   required FOV/range, removes unused fields if safe, and voxelizes before DDS.
3. Evaluate a point-cloud transport/compression plugin only after measuring CPU
   and latency; avoid adding complexity without benchmarks.
4. Reduce Ouster resolution/frequency only if it does not compromise mapping or
   recording requirements.

Acceptance target: WSL receives the reduced cloud continuously for at least ten
minutes with a documented bandwidth, rate, latency, and zero stale-cloud
diagnostics.

### 2. Fix timestamp ordering before tuning Cartographer

Do not hide the warning by loosening Cartographer parameters first. Establish
where ordering is lost:

1. Record scan/IMU header stamps and receive times locally on the Jetson.
2. Repeat at WSL and compare sequence regressions and inter-arrival bursts.
3. Record Ouster timestamp mode and PTP/NTP state.
4. Test the mapping adapter on the Jetson. Its output topics are small and may
   cross the hotspot more reliably after normalization.
5. If reordering remains necessary, add a bounded time-window reorder buffer
   with explicit metrics for buffered, released, late, and dropped messages.
6. Preserve the existing shared timestamp alignment across scan and IMU.

Acceptance target: a full mapping drive with zero timestamp regressions, no
growing rejected-message counter, and no Cartographer earlier-point warnings.

### 3. Profile and fix Jetson CPU usage

First fix the obvious joystick loop in `command.cpp`:

- Poll once per timer invocation, or run a controlled loop with an explicit
  rate/sleep in a dedicated thread.
- Close the game controller during node destruction, not after an unreachable
  infinite callback loop.
- Keep command publication at a deliberate rate such as 10-50 Hz.
- Preserve the current arming/dead-man semantics and verify that stale commands
  resolve to zero velocity.

Then profile `qbot_platform_driver_interface` rather than guessing. Its intended
timer period is 16 ms, so sustained 159% CPU is not expected without additional
work or spinning. Collect per-thread CPU, call timing for `pstream_receive` and
`pstream_send`, callback duration, publish rate, and executor activity.

Acceptance target: bounded documented CPU under the full camera/Ouster/QBot
load, with no loss of the 16 ms command/feedback deadline.

### 4. Establish the robot TF and odometry model

Current mapping deliberately uses `os_lidar`. It does not fabricate the unknown
mounting transform. Before navigation:

1. Measure `base_link -> os_sensor -> os_lidar` accurately.
2. Add the transform to the robot description/static TF publisher.
3. Derive wheel odometry from joint or speed feedback and validate signs,
   scaling, wheel radius, wheelbase, and covariance.
4. Establish `map -> odom -> base_link -> os_lidar`.
5. Decide whether to fuse wheel, QBot IMU, and/or Ouster IMU using
   `robot_localization`.

The current `/navigation/global_pose` is the Ouster sensor pose, not the robot
centre.

### 5. Save both occupancy maps and Cartographer state

The UI/backend should save:

- PGM/YAML for `nav2_map_server` and human inspection.
- `.pbstream` for Cartographer state, debugging, and possible continuation.
- Session metadata: date, map name, ROS domain, sensor configuration, scan ring,
  resolution, warnings, software commit, and checksums.

Confirm the installed `cartographer_ros_msgs/srv/WriteState` request schema with
`ros2 interface show` before implementing the `.pbstream` call.

## Recommended DeltaUI architecture

Do not continue adding shell commands directly to button callbacks. Split the
application into a UI layer and a supervised control/telemetry layer.

```text
Qt MainWindow
  +-- Connection/status view
  +-- Camera/local-map view
  +-- Driver/recording controls
  +-- Mapping lifecycle controls
  +-- Map library/export view
  `-- Structured log/diagnostic view

Qt signals/slots
          |
          v

Worker/backend layer
  +-- configuration and secret handling
  +-- SSH/service client for Jetson
  +-- WSL process supervisor
  +-- ROS graph/topic/diagnostic monitor
  +-- mapping state machine
  +-- map saver/state writer
  `-- diagnostic-bundle exporter
```

### Recommended process boundary

The most direct option is to run DeltaUI inside WSL/WSLg as a ROS-aware PySide6
application:

- Run a `rclpy` executor in a worker thread.
- Send immutable data to the Qt thread through signals.
- Never update widgets from the ROS executor thread.
- Use supervised processes/services for launch and shutdown rather than shell
  string interpolation.
- Keep Foxglove external initially and add an **Open Foxglove** button.

If DeltaUI must remain a native Windows executable, create a small WSL backend
service/CLI with a stable structured protocol rather than parsing arbitrary
shell output through repeated `wsl.exe` calls.

### Suggested module split

```text
delta_ui/
  app.py
  config.py
  models.py
  ros_monitor.py
  jetson_client.py
  process_supervisor.py
  mapping_controller.py
  map_store.py
  diagnostics.py
  ui/
    main_window.py
    status_panel.py
    mapping_panel.py
    camera_panel.py
    map_panel.py
    log_panel.py
  tests/
```

The exact names are flexible; the separation of responsibilities is the
important part.

### Runtime state model

Use explicit observable states rather than inferring readiness from topic names:

```text
DISCONNECTED
  -> CONNECTING
  -> DRIVERS_STARTING
  -> DRIVERS_READY
  -> VIEWER_READY
  -> MAPPING
  -> FINALIZING
  -> SAVING
  -> MAP_SAVED
  -> DRIVERS_READY

Any state -> ERROR with actionable recovery information
```

State decisions must use node existence, publisher count, message freshness,
diagnostics, service availability, and owned-process status. A topic name alone
is insufficient: after mapping stopped, `/navigation/global_map` still appeared
because Foxglove retained a subscription, but publisher count was zero.

### UI controls and safety invariants

Required controls/status:

- Configurable Jetson host, ROS domain, WSL bridge port, and map directory.
- Connect/reconnect with verified SSH host key and key-based authentication.
- Driver start/stop with duplicate-start prevention.
- Recording start/stop with validated names and explicit active state.
- Persistent viewer start/stop separate from mapper start/stop.
- Topic-rate and freshness indicators for scan, IMU, reduced cloud, local map,
  global map, battery, and camera.
- Timestamp rejection and normalization error counters.
- Mapping start, finish, save, cancel/recover, and new-session controls.
- Map preview, export, checksums, and metadata.
- Emergency/safety state that is visually distinct from ordinary software stop.

Invariants:

- Never launch two copies of the same managed service.
- Never claim success before checking command/service results.
- Never stop the mapper before the requested artifacts are confirmed on disk.
- Never overwrite an existing map without an explicit decision.
- Sanitize map and bag names; do not interpolate user text into a shell command.
- Finishing a Cartographer trajectory is irreversible for that session and
  requires clear UI confirmation.
- Keep mapping stop independent from QBot/Ouster driver stop.
- Treat stale diagnostics and missing publishers as failures even when a topic
  remains visible in the graph.

### Logging design

`DeltaUI` currently appends optimistic text but does not capture remote output.
Replace this with structured events containing:

- wall-clock and ROS time;
- severity;
- host and component;
- operation/session ID;
- command/service result;
- stderr/stdout or ROS log excerpt;
- remediation hint.

For the Jetson, prefer named `systemd` services and `journald` over detached SSH
shells. For WSL ROS launches, retain their process handles and ROS log directory.
Add **Export diagnostics** to collect configuration, versions, topic graph,
rates, active parameters, recent logs, software commit, and map metadata without
including passwords or private keys.

### Credentials and configuration

The prototype contains an old hardcoded address, plaintext password, and
`AutoAddPolicy`. Do not duplicate those values into new code or documentation.

- Use a configuration file or settings dialog for non-secret values.
- Use SSH keys or the operating-system credential store for secrets.
- Verify known host keys.
- Apply connection and command timeouts.
- Rotate/remove any credential that has already been committed.
- Make the ROS domain explicit in every supervised process.

## Navigation integration after mapping/UI stabilization

PGM/YAML alone do not provide autonomous navigation. After TF and odometry are
validated:

1. Load a selected map with `nav2_map_server`.
2. Configure AMCL or another localization source.
3. Supply robot footprint, costmaps, velocity/acceleration limits, and recovery
   behaviours.
4. Validate localization while stationary and under manual driving.
5. Add goal controls to the UI only after command arbitration and emergency stop
   behaviour are proven.
6. Ensure joystick/manual commands and Nav2 commands cannot fight for `/cmd_vel`;
   use an explicit command multiplexer and priority policy.

## Prioritized implementation plan

### P0 - make the existing platform safe and reproducible

1. Fix `qbot_platform/package.xml` metadata so colcon can identify the package.
2. Fix/rate-limit the joystick polling loop and test arming/stale-command safety.
3. Profile and reduce platform-driver CPU.
4. Remove `colcon build` from runtime startup.
5. Replace hardcoded IP/credentials and blocking SSH in DeltaUI.
6. Create owned/supervised Jetson and WSL services with explicit domain and
   duplicate-start protection.
7. Add result-aware logging and a basic state machine.

### P1 - make sensor transport and mapping deterministic

1. Produce a reduced point cloud on the Jetson.
2. Locate and fix scan/IMU timestamp regressions.
3. Add timing/order/bandwidth diagnostic metrics.
4. Save `.pbstream` plus PGM/YAML and session metadata.
5. Repeat the same route over Ethernet and hotspot to quantify network effects.

### P2 - integrate the operator workflow

1. Implement camera, battery, network, topic, and diagnostics panels.
2. Implement separate viewer and mapping controls.
3. Implement finish/save/new-map state transitions.
4. Implement map preview/library/export.
5. Add an Open Foxglove action using the supplied layout.

### P3 - integrate localization and Nav2

1. Measure TF and validate wheel odometry.
2. Add sensor fusion if required.
3. Load maps and validate localization.
4. Add command arbitration, safety controls, and then navigation goals.

## Test strategy

### Unit tests

- Existing grid and timestamp-normalization tests must remain green.
- Add reorder-buffer tests including wrap/reset/regression cases.
- Add state-machine transition tests.
- Mock SSH/process results, timeouts, partial output, disconnects, and duplicate
  starts.
- Validate/sanitize map and recording names.
- Test map collision and incomplete-save recovery.

### Offline integration tests

- Use recorded Ouster scan/IMU bags for repeatable mapping.
- Inject delayed and reordered messages and verify metrics/recovery.
- Replay reduced point clouds at controlled bandwidth.
- Verify map finalization, PGM/YAML, `.pbstream`, checksums, and metadata.
- Run the UI against mocked ROS and service backends without hardware.

### Lab acceptance tests

- Drivers start once and stop cleanly through the UI.
- The UI correctly identifies active domain and Jetson address.
- Scan, IMU, reduced cloud, camera, battery, and feedback remain fresh.
- CPU is within documented limits for at least 30 minutes.
- Mapping produces no timestamp-order warnings.
- Finishing/saving does not disconnect the persistent viewer.
- PGM/YAML and `.pbstream` are non-empty and reopen correctly.
- Restarting mapping starts a clean trajectory.
- Driver/recording/mapping failures produce actionable UI errors.
- Manual stop/arming behaviour remains safe during network loss.

## Existing commands for reference

These are the tested manual operations. Do not run duplicate copies.

### Jetson throttle

```bash
source /opt/ros/humble/setup.bash
source ~/ros2/install/setup.bash
export ROS_DOMAIN_ID=0
export ROS_LOCALHOST_ONLY=0

ros2 run topic_tools throttle messages \
  /ouster/points 5.0 /ouster/points_viz \
  --ros-args -p lazy:=false
```

This command is functionally correct but insufficient for hotspot point-cloud
transport because it does not reduce message size.

### Persistent WSL visualization

```bash
source /opt/ros/humble/setup.bash
source /path/to/workspace/install/setup.bash
export ROS_DOMAIN_ID=0
export ROS_LOCALHOST_ONLY=0

ros2 launch qbot_navigation_visualization \
  wsl_navigation_visualization.launch.py \
  output_frame:=os_lidar
```

### Independent live mapping

```bash
ros2 launch qbot_navigation_visualization \
  wsl_2d_mapping.launch.py \
  start_visualization:=false \
  start_bridge:=false \
  output_frame:=os_lidar
```

### Finish and save

```bash
ros2 service call /finish_trajectory \
  cartographer_ros_msgs/srv/FinishTrajectory \
  "{trajectory_id: 0}"

# Wait for final optimization and confirm a fresh global map before saving.
ros2 topic echo /navigation/global_map --once --field info

mkdir -p ~/qbot_maps
ros2 run nav2_map_server map_saver_cli \
  -t /navigation/global_map \
  -f "$HOME/qbot_maps/map_name" \
  --fmt pgm \
  --mode trinary
```

Keep the mapping nodes alive until all requested map artifacts have been
verified, then stop only the mapping launch.

## Before the next lab session

- Ensure the relevant branch, this handoff, and any code changes are committed
  and pushed.
- Copy `first_test.pgm` and `first_test.yaml` if they are needed as evidence.
- Optionally retain these lab ROS logs outside Git for debugging:
  - `/home/ernie/.ros/log/cartographer_node_14823_1787881554406.log`
  - `/home/ernie/.ros/log/ouster_mapping_adapter_14821_1787881554384.log`
  - `/home/ernie/.ros/log/foxglove_bridge_14706_1787881545583.log`
- Do not commit passwords, SSH keys, machine-specific generated setup files, or
  large rosbag databases.
- Record the new Jetson address and active domain after every network change.
- Prefer a wired link for baseline timing/bandwidth measurements before
  evaluating hotspot behaviour.
- Read `docs/live_mapping_lab_guide.md` before operating hardware.

## Definition of a production-ready milestone

The first production-ready milestone is reached when the UI can safely and
repeatably:

1. connect to a configured Jetson using secure authentication;
2. start/observe/stop owned driver services without duplicates;
3. show fresh camera, battery, scan, reduced-cloud, and diagnostics data;
4. start an independent mapping session;
5. map without timestamp regressions;
6. finish and save PGM/YAML plus `.pbstream` with metadata;
7. recover cleanly from network/process failure; and
8. do all of the above within documented CPU and bandwidth limits.

Autonomous Nav2 control should follow this milestone, not be combined with the
first UI/control refactor.
