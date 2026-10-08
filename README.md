# Capstone-Project-P004021Eng

GitHub repository for D.E.L.T.A. (Explain what it does ...)

## Project Overview
The project consists of three main components:
- QBot Platform - software and configuration for operating the physical hardware
- Processing Pipline - processing of data collected
- Nvidia Omniverse - visualisation

The three components work together to navigate around a space, perform a scan, process data to create a baseline, perform a scan in a changed enviroment and 

## Project Structure
### QBot_Platform
The `QBot_Platform` folder contains the software and configuration required to operate the physical QBot hardware and collect sensor data.

See ['QBot_Platform/README.md'](QBot_Platform/README.md) for furthur infomation.

### Processing Pipeline
(At some point move all processing pipeline related things into this folder)

See ['Processing_Pipeline/README.md'](Processing_Pipeline/README.md) for furthur infomation.

### Nvidia Omniverse
(At some point move all Nvidia Omniverse scripts/files into this folder)

See ['Nvidia_Omniverse/README.md'](Nvidia_Omniverse/README.md') for furthur infomation.



# Everything below here is old and needs to be updated

## Joseph mapping UI

The mapping-enabled UI is kept separate from the original DeltaUI:

- `QBot_Platform/DeltaUI` is the unchanged original application.
- `QBot_Platform/DeltaUI_Joseph` adds QBot/recording state checks, live 2D
  mapping, map preview, save/cancel controls, and the WSL mapping worker.

Start with [LAB_QUICK_START.md](LAB_QUICK_START.md). The full Windows, WSL,
Jetson, Foxglove, and troubleshooting procedure is in
[docs/live_mapping_lab_guide.md](docs/live_mapping_lab_guide.md), while the
backend and state model are documented in
[docs/delta_ui_mapping.md](docs/delta_ui_mapping.md).

The existing Jetson `run_qbot.sh` and recording workflow remain unchanged.

---

## QBot Platform
The [QBot_Platform](QBot_Platform) folder includes everything pertaning to the operation of the physical hardware:
- 'QBot_Platform/qbot_platform' includes all the modified ROS2 Package files including
  - 'launch'
  - 'src'
  - 'CMakeLists.txt'
  - 'package.xml
- DeltaUI Files
- Run scripts for
  - run_qbot.sh activates the QBot Platform and Ouster OS0 LiDAR
  - run_record.sh begins a rosbag recording of relevant topics for processing