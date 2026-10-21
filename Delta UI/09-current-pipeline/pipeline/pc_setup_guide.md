# PC Setup Guide: D.E.L.T.A. Pipeline (Qt Application)

This guide is written in Simplified Technical English (ASD-STE100).

This guide is for the Qt application (`pipeline_applet_qt_template.py`). The old Tkinter application (`pipeline_applet.py`) uses a different folder layout. Do not use this guide for the old application.

## 1. Before You Start

You must have these items:

- A Windows 10 or Windows 11 PC.
- Administrator access to the PC.
- An internet connection.
- The application ZIP file from your team.

You do not need WSL2 for this application. KISS-SLAM (the WSL2 tool) is not part of the Qt application.

## 2. Install Python

### 2a. Choose the Correct Python Version

Some packages that the application uses do not support the newest Python version.

1. Open a web browser.
2. Go to `https://www.python.org/downloads/windows/`.
3. Find a release in the **Python 3.11** series or the **Python 3.12** series.
4. Click the link for the **Windows installer (64-bit)** for that release.

**CAUTION:** Do not use a Python version newer than 3.12. The `open3d` package does not install on newer versions. Stage 2, Stage 4 and the point spacing check of Stage 5 do not operate without `open3d`.

### 2b. Run the Installer

1. Open the installer file.
2. On the first screen, select the checkbox **"Add python.exe to PATH"**.
3. Click **Install Now**.
4. Wait until the installation is complete.
5. Click **Close**.

**NOTE:** The checkbox in step 2 is easy to miss. If you do not select it, Windows cannot find the `python` command.

## 3. Make Sure That Python Is Installed

1. Open a new Command Prompt window.
2. Type this command and push Enter:
   ```
   python --version
   ```
3. Make sure that the window shows a version in the 3.11 series or the 3.12 series.

**NOTE:** Windows reads PATH changes only in new windows. If the command fails, close the window. Open a new Command Prompt window and try again.

## 4. Put the Application Files on the PC

1. Make a folder for the application, for example `C:\DELTA`.
2. Extract the ZIP file into this folder.
3. Make sure that the folder has this layout:
   ```
   C:\DELTA\
       run_pipeline_qt.bat
       requirements.txt
       gui\
           pipeline_applet_qt_template.py
           pipeline_applet_qt_template.ui
           qt_stage_base.py
           stage1_slam_dialog_qt.py  ...  stage8_export_dialog_qt.py
           project_workflow_qt.py
           about_dialog_qt.py
           about_content.json
           pipeline_core.py
           project_manager.py
       scripts\
           slam_kiss_icp.py
           decode_raw_packets.py
           level_cloud.py
           segment_planes.py
           point_spacing.py
           m3c2_classify.py
           surface_reconstruction.py
           blender_surface.py
           extract_damage_detail.py
           usd_export.py
           import_pointcloud.py
       configs\
           kiss_icp_config_indoor.yaml
           kiss_icp_config_hesai.yaml
           ExtraDownsampling.blend
   ```

**CAUTION:** Keep the `gui`, `scripts` and `configs` folders together in the same parent folder. The application finds the scripts and the configuration file from this layout. If you move one folder, the stages cannot find their scripts.

**NOTE:** A path that has a space in it (for example `C:\Users\C Day\DELTA`) is permitted. A short path without spaces is easier to type.

## 5. Install the Python Packages

1. Open a new Command Prompt window.
2. Go to the application folder. Type this command and push Enter:
   ```
   cd C:\DELTA
   ```
3. Type this command and push Enter:
   ```
   python -m pip install -r requirements.txt
   ```
4. Wait until the installation is complete. This can take some minutes.
5. Make sure that no red error text shows at the end.
6. Make sure that PySide6 is installed. PySide6 makes the application window. Type this command and push Enter:
   ```
   python -c "import PySide6; print(PySide6.__version__)"
   ```
7. Make sure that the window shows a version number, for example `6.9.1`. The version must be 6.5 or newer.

**NOTE:** The command in step 3 installs all of the packages in `requirements.txt`. PySide6 is one of these packages. If step 6 shows `ModuleNotFoundError: No module named 'PySide6'`, type this command and push Enter:
```
python -m pip install "PySide6>=6.5"
```
Then do steps 6 and 7 again.

**NOTE:** `requirements.txt` sets `kiss-icp` to version 1.2.3. The Stage 1 script works with this version. Do not upgrade `kiss-icp`. Other versions can give errors.

**NOTE:** The package `rosbags` comes with `ouster-sdk` (version 0.9.x). The Stage 1 bag reader works with `rosbags` 0.9.23 and with `rosbags` 0.11.0. You do not need to change the version. The package `kiss-icp` does not install `rosbags` itself.

**NOTE:** If `open3d` does not install, go to Sections 8 and 9 of the troubleshooting sheet.

## 6. Install CloudCompare and Blender

### 6a. Install CloudCompare

Stage 3 (Cleanup) and Stage 5 (Diff) use CloudCompare.

1. Open a web browser.
2. Go to `https://www.cloudcompare.org/`.
3. Download CloudCompare for Windows.
4. Open the installer.
5. Use the default installation folder.
6. Click **Install**.
7. Wait until the installation is complete.
8. Click **Finish**.
9. Write down the installation folder. You use this folder in Section 7.

### 6b. Install Blender

Stage 7 (Surface) uses Blender to make the mesh.

1. Open a web browser.
2. Go to `https://www.blender.org/download/`.
3. Download Blender for Windows. Use version 5.2 or newer.
4. Open the installer.
5. Follow the instructions in the installer. Use the default installation folder.
6. Wait until the installation is complete.
7. Click **Finish**.

**CAUTION:** Use Blender 5.2 or newer. The file `configs\ExtraDownsampling.blend` was saved with Blender 5.2. An older Blender version can give a wrong result or an error.

**NOTE:** You do not have to add Blender to PATH. The application finds Blender in its default installation folder (`C:\Program Files\Blender Foundation\Blender <version>`) or in the Steam folder. If you installed Blender in a different folder, open **Advanced** in Stage 7. Click **Browse...** next to "Blender program" and select `blender.exe`. You can also set the environment variable `DELTA_BLENDER` or `BLENDER_EXE` to the full path of `blender.exe`.

**NOTE:** In Stage 7, a red line shows above the field "Voxel size" only when the application does not find Blender. When the application finds Blender, no line shows.

**NOTE:** A large input (millions of points) needs some minutes and some GB of memory.

## 7. Add the Tools to PATH

Do this procedure if a command in Section 8 fails.

1. Click the Windows **Start** button.
2. Type `environment variables`.
3. Click **Edit environment variables for your account**.
4. In the top list, select the row **Path**.
5. Click **Edit**.
6. Click **New**.
7. Type the full path of the folder that contains the tool. Examples:
   ```
   C:\Users\<your username>\AppData\Local\Programs\Python\Python312\Scripts
   C:\Program Files\CloudCompare
   ```
8. Do steps 6 and 7 again for each folder that is not on PATH.
9. Click **OK** on each window.
10. Close all Command Prompt windows.

**CAUTION:** Type the folder path only. Do not type the path of the `.exe` file. Windows PATH entries must be folders.

**CAUTION:** Add the folders to the existing **Path** row. Do not make a new variable with a different name. Windows does not use a variable with a different name to find tools.

## 8. Make Sure That the Tools Are Available

1. Open a new Command Prompt window.
2. Type this command and push Enter:
   ```
   where ouster-cli
   ```
3. Make sure that the window shows a path that ends in `ouster-cli.exe`.
4. Type this command and push Enter:
   ```
   where CloudCompare
   ```
5. Make sure that the window shows a path that ends in `CloudCompare.exe`.

**NOTE:** If a command shows "Could not find files for the given pattern(s)", the folder of that tool is not on PATH. Do the procedure in Section 7.

## 9. Start the Application

1. Open the application folder in File Explorer.
2. Double-click `run_pipeline_qt.bat`.
3. Make sure that the application window opens.

**NOTE:** At the first start, the application makes the file `gui\ui_pipeline_applet_qt_template.py` from the `.ui` file. This is automatic. It occurs again only when the `.ui` file changes.

**NOTE:** If the application does not start, the Command Prompt window stays open and shows the error. Copy all of the error text into your bug report.

**NOTE:** If a stage page shows "could not load", open the **Log** tab. The Log tab shows the full error. Usually the cause is a Python package that is not installed. Do Section 5 again.

**NOTE:** The **?** button at the end of a field row opens a short explanation of the field. The explanation tells what the field does, what a smaller or larger value does, and when to change it. Click anywhere else to close it.

## 10. Stop a Stage

A stage can run for a long time. For example, Stage 4 (Segment) can run for many minutes on a large floor surface.

1. To stop a running stage, click **Stop** at the bottom of the stage page.
2. Click **Yes** to confirm.
3. Make sure that the status line shows "stopped by user".

**CAUTION:** Output files from a stopped run can be incomplete. Do not use them. Run the stage again.

**NOTE:** If you close the application while a stage runs, the application asks for confirmation. If you confirm, the application stops the process and records the stage as failed.

## 11. Quick Reference Checklist

| Item | Check command | Correct result |
|---|---|---|
| Python | `python --version` | A version in the 3.11 series or the 3.12 series |
| PySide6 | `python -c "import PySide6; print(PySide6.__version__)"` | Version 6.5 or newer |
| Other Python packages | `python -m pip show open3d kiss-icp` | Details for each package |
| Ouster CLI | `where ouster-cli` | A path to `ouster-cli.exe` |
| CloudCompare | `where CloudCompare` | A path to `CloudCompare.exe` |
| Blender | Open Stage 7 in the application | No red line "Blender not found" shows above "Voxel size" |
| Application | Double-click `run_pipeline_qt.bat` | The application window opens |

## 12. Use a Hesai Sensor (Stage 1)

The Hesai XT32 lidar replaces the Ouster OS0-128. Stage 1 reads the Hesai bag with the KISS-ICP backend. The Ouster CLI backend cannot read a Hesai bag.

### 12a. Record a Bag

1. Start the Hesai driver on the Jetson.
2. Record the topic `/lidar_points`. It has the message type `sensor_msgs/msg/PointCloud2`.
3. Copy the bag folder to the PC. The folder has a `.db3` file and the file `metadata.yaml`.

**NOTE:** The pipeline does not use the topic `/lidar_packets`. The application cannot read its message type.

**NOTE:** A bag that you record with the sensor on a table does not show how SLAM operates on the robot. Also record a bag with the sensor on the QBot.

### 12b. Run Stage 1

1. Open Stage 1.
2. Set **Backend** to **KISS-ICP** (the first choice).
3. Click **Folder...** and select the bag folder.
4. Set **Sensor preset** to **Hesai XT32**. Open **Advanced**. The field **Config .yaml** then shows `kiss_icp_config_hesai.yaml`.
5. Select the output file and click **Run**.
6. Read the output text. Make sure that it shows these lines:
   - `ROS2 bag read by this script`
   - `Time field 'timestamp': float64`
   - `Deskew used the time field 'timestamp' for every frame`

**CAUTION:** If the output text shows `WITHOUT deskew`, send the output text to the team. The map can have errors when the sensor moves.

**NOTE:** The output text also shows how many points at exactly (0, 0, 0) the reader removed. The Hesai driver writes these points when a return did not come back. They are not real points.

**NOTE:** If the Ouster CLI backend is selected, Stage 1 shows a message and does not start. Change **Backend** to **KISS-ICP**.

### 12c. Settings

- The config `kiss_icp_config_hesai.yaml` has the same values as the Ouster config, with one change: `min_range` is 0.1 m. This value removes only the points at (0, 0, 0).
- The other values (`voxel_size`, `max_range`) are not tuned for the Hesai sensor. A Hesai frame has fewer points than an Ouster frame (about 13 000 against about 131 000). Tune the values with a recording where the sensor moves.
- The sensor can see parts of the QBot. If it does, open **Advanced** and set **Min range override** to a value larger than the distance to these parts. Measure the distance on a recording with the sensor on the QBot.

### 12d. If the Bag Does Not Read

| Message | Cause | Action |
|---|---|---|
| `The bag has more than one PointCloud2 topic` | The bag has two or more point topics. | Type the topic in **Rosbag topic** (for example `/lidar_points`). |
| `The bag has no PointCloud2 topic` | The bag has only raw packets or other data. | Record the topic `/lidar_points` again. |
| `Bag contains no type definitions` | The bag was read by an old Stage 1 script with `rosbags` 0.10 or newer. | Use the Stage 1 script that comes with this application (`scripts\slam_kiss_icp.py`). |
| `WITHOUT deskew` | The points have no time field, or the time cannot be used. | Send the output text to the team. |
