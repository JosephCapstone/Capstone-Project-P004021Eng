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

**CAUTION:** Do not use a Python version newer than 3.12. The `open3d` package does not install on newer versions. Stages 2, 4 and 7 do not operate without `open3d`.

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
           extract_damage_detail.py
           usd_export.py
           generate_m3c2_params.py
       configs\
           kiss_icp_config_indoor.yaml
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

**NOTE:** `requirements.txt` sets `kiss-icp` to version 1.2.3. The Stage 1 script is tested with this version. Do not upgrade `kiss-icp` without a test.

**NOTE:** If `open3d` does not install, go to Section 9 of the troubleshooting sheet.

## 6. Install CloudCompare

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

## 10. Do the Self-Tests (Testers)

Do these tests after you install the application. The tests do not need LiDAR data, CloudCompare or `ouster-cli`.

1. Open a new Command Prompt window.
2. Go to the `gui` folder. Type this command and push Enter:
   ```
   cd C:\DELTA\gui
   ```
3. Type each command and push Enter. Wait until each command is complete.
   ```
   python test_project_manager.py
   python test_pipeline_core_project_mode.py
   python test_qt_smoke.py
   ```
4. Make sure that each test shows `ALL TESTS PASSED` or `All ... checks passed` at the end.

**NOTE:** If a test shows `[FAIL]`, the test stops. Copy all of the text in the window into your bug report.

## 11. Stop a Stage

A stage can run for a long time. For example, Stage 4 (Segment) can run for many minutes on a large floor surface.

1. To stop a running stage, click **Stop** at the bottom of the stage page.
2. Click **Yes** to confirm.
3. Make sure that the status line shows "stopped by user".

**CAUTION:** Output files from a stopped run can be incomplete. Do not use them. Run the stage again.

**NOTE:** If you close the application while a stage runs, the application asks for confirmation. If you confirm, the application stops the process and records the stage as failed.

## 12. Quick Reference Checklist

| Item | Check command | Correct result |
|---|---|---|
| Python | `python --version` | A version in the 3.11 series or the 3.12 series |
| Python packages | `python -m pip show PySide6 open3d kiss-icp` | Details for each package |
| Ouster CLI | `where ouster-cli` | A path to `ouster-cli.exe` |
| CloudCompare | `where CloudCompare` | A path to `CloudCompare.exe` |
| Self-tests | `python test_qt_smoke.py` (in `gui`) | `All ... checks passed` |
| Application | Double-click `run_pipeline_qt.bat` | The application window opens |
