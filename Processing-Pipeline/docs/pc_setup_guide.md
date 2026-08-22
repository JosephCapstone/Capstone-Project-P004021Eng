# PC Setup Guide: SLAM Pipeline Applet

This guide follows Simplified Technical English (ASD-STE100) conventions: short sentences, one instruction per step, active voice, and approved terminology.

## 1. Before You Start

You need these items:
- A Windows PC with administrator access.
- An internet connection.
- The applet's files, organized into folders. See Section 7 for the full list and layout.

## 2. Install Python

1. Open a web browser.
2. Go to `https://www.python.org/downloads/`.
3. Click the button to download the Python 3 installer.

**CAUTION:** Do not download the newest Python version by default. Go to Section 2a first to choose the correct version.

### 2a. Choose the Correct Python Version

Some required packages do not yet support the newest Python release.

1. Go to `https://www.python.org/downloads/windows/`.
2. Find a release in the **Python 3.11** or **Python 3.12** series.
3. Click the link for the **Windows installer (64-bit)** for that release.

**NOTE:** Python 3.11 and Python 3.12 have full support from all packages in this guide. Newer Python versions may not.

### 2b. Run the Installer

1. Run the downloaded installer file.
2. On the first screen, select the checkbox labelled **"Add python.exe to PATH."**
3. Click **Install Now**.
4. Wait for the installation to finish.
5. Click **Close**.

**NOTE:** The checkbox in step 2 is easy to miss. If you do not select it, Windows cannot find the `python` command later.

## 3. Confirm the Python Installation

1. Open a new Command Prompt window.
2. Type this command and press Enter:
   ```
   python --version
   ```
3. Confirm the window shows a Python version number in the 3.11 or 3.12 series.

**NOTE:** If the command fails, close the window. Open a new Command Prompt window and try again. Windows only reads PATH changes in new windows.

**NOTE:** If your PC already had a different Python version installed before Section 2, `python --version` might show that other version instead of the one you just installed - Windows only guarantees this for the most recently installed version, and only if its own PATH entry sits first. Use the `py` launcher to target a specific version by number instead of relying on plain `python`/`pip`:
```
py -0
```
Lists every Python version Windows knows about, each with its own version number. Then prefix every command with `py -3.12` (or whichever of 3.11/3.12 you actually installed), for example:
```
py -3.12 --version
py -3.12 -m pip install ouster-sdk
py -3.12 pipeline_applet.py
```
Use this `py -3.12` prefix on every command in Section 4 and Section 9 if step 3 above showed an unexpected version, or if you already know this PC has more than one Python installed.

## 4. Install the Required Python Packages

**NOTE:** If you're using the `py -3.12` prefix (see the NOTE at the end of Section 3), apply it to every command below too, for example `py -3.12 -m pip install ouster-sdk` instead of `pip install ouster-sdk` - otherwise packages can end up installed into the wrong Python version's environment, one `python`/`pip` doesn't actually run from.

1. In the same Command Prompt window, type this command and press Enter:
   ```
   pip install ouster-sdk
   ```
2. Wait for the installation to finish.
3. Confirm no red error text appears at the end.
4. Type this command and press Enter:
   ```
   pip install open3d
   ```
5. Confirm no red error text appears at the end.
6. Type this command and press Enter:
   ```
   pip install kiss-icp==1.2.3
   ```
7. Confirm no red error text appears at the end.
8. Type this command and press Enter:
   ```
   pip install usd-core plyfile
   ```
9. Confirm no red error text appears at the end.
10. Type this command and press Enter:
    ```
    pip install scikit-learn
    ```
11. Confirm no red error text appears at the end.
12. Type this command and press Enter:
    ```
    pip install pyyaml
    ```
13. Confirm no red error text appears at the end.

**NOTE:** `pyyaml` (imported as `yaml`) is required, not optional - `pipeline_core.py` itself uses it, not just an individual stage script. Most importantly, it's needed to read a ROS2 bag's `metadata.yaml` (used when auto-detecting a bag's topic names, for Stage 1) - without it, that specific operation fails immediately with a clear `RuntimeError: Reading a ROS2 bag's metadata.yaml needs PyYAML. Run: pip install pyyaml`. It's also used (more gracefully - fails silent, no crash) when reading a KISS-ICP config's own `voxel_size` for display in the Stage 1 dialog.

**NOTE:** `ouster-sdk` installs the `ouster-cli` tool. This package is required. If you plan to import ROS bag files in Stage 1, this should also pull in the `rosbags` package it depends on for reading them - if Stage 1 gives an import error mentioning `rosbags` specifically when you try a bag source, run `pip install rosbags` directly. `decode_raw_packets.py` (raw-packet conversion) also needs `rosbags` directly - same fix if it's missing there.

**NOTE:** `kiss-icp` is required for this project - Stage 1's KISS-ICP backend is not an optional alternative here, pin it to `==1.2.3` deliberately, not a loose suggestion. `slam_kiss_icp.py`'s own `load_config()` call was verified against this specific version's source; a plain `pip install kiss-icp` grabs whatever's newest on PyPI at install time instead, which can silently drift to a different version between two PCs set up at different times - confirmed to actually happen and break that call (a real cross-PC setup failure, not a hypothetical one). Its own defaults assume vehicle-scale outdoor odometry and will badly under-populate an indoor/compartment map (confirmed: roughly 190x fewer points without a properly scaled config) - `kiss_icp_config_indoor.yaml` (bundled with these files) is already tuned for this project's compartment scale and is the default in Stage 1's KISS-ICP config field, so this shouldn't need regenerating per-scan. Only rebuild it with `kiss_icp_dump_config` if it's genuinely missing or a kiss-icp version upgrade changes its available fields - and if you do deliberately move past 1.2.3, expect to re-verify `slam_kiss_icp.py`'s `load_config()` call against whatever version you land on.

**NOTE:** A third SLAM option, KISS-SLAM (adds loop closure - useful if you see doubled/ghosted walls at turns), is not covered by this guide. It requires WSL2 and a separate Linux-side build (Bonxai, MapClosures, g2o), and is not yet wired into the applet as a Stage 1 backend - currently run by hand from a WSL2 terminal. Ask if you need the WSL2 setup steps.

**NOTE:** `usd-core` and `plyfile` are required for Stage 8 (Export to USD). `usd-core` provides the USD Python bindings. `plyfile` reads point cloud files while keeping custom fields, for example the M3C2 distance field - also needed by Stage 6 (Classify) for the same reason.

**NOTE:** `scikit-learn` is required for Stage 6 (Classify)'s clustering step (`m3c2_classify.py`, imports `sklearn.cluster`). Clustering is turned ON by default, so this package is required for a normal Stage 6 run, not just an optional extra - skipping it gives a `ModuleNotFoundError: No module named 'sklearn'` partway through Stage 6, after point flagging already finished. The package name to install (`scikit-learn`) is different from the name Python imports it under (`sklearn`) - this is normal for this package, not a typo. If clustering is set to HDBSCAN (DBSCAN is the default), scikit-learn version 1.3 or newer is required specifically - `sklearn.cluster.HDBSCAN` didn't exist before that release. A plain `pip install scikit-learn` on a fresh machine gets a recent-enough version automatically; this only matters if a machine already has an older scikit-learn installed for something else and it doesn't get upgraded.

**NOTE:** `scipy` is required for Stage 7 (Surface)'s default behavior, not just an optional extra for manual use. Stage 7 IS a wired applet stage, and its "Carry field" option defaults to `M3C2 distance` (not blank) - so a normal, default Stage 7 run tries to use scipy. Missing scipy doesn't crash the run, though: `surface_reconstruction.py` catches this specific case, prints a warning, and writes the mesh without the carried field instead - a silent quality loss (no color-by-magnitude data on the mesh) rather than an error, easy to miss if you're not looking for it. Install with `pip install scipy` to avoid this.

**NOTE:** `open3d` is required by Stage 2 (Level) and the point spacing/plane segmentation/surface reconstruction helper scripts - not optional despite the package name suggesting otherwise. If it fails to install, go to Section 9 in the troubleshooting sheet.

**NOTE:** `numpy` is used directly by nearly every processing script in this project. You don't need to install it separately - both `ouster-sdk` and `open3d` require it themselves, so it comes along automatically as part of those two installs above, in the normal order this guide already has you install them in.

## 5. Confirm the Ouster SDK Installation

1. In the Command Prompt window, type this command and press Enter:
   ```
   where ouster-cli
   ```
2. Confirm the window shows a file path, for example:
   ```
   C:\Users\<your username>\AppData\Local\Programs\Python\Python312\Scripts\ouster-cli.exe
   ```

**NOTE:** If the command shows "Could not find files for the given pattern(s)", the `Scripts` folder is not on PATH. Go to Section 8.

## 6. Install CloudCompare

1. Open a web browser.
2. Go to `https://www.cloudcompare.org/`.
3. Click the link to download CloudCompare for Windows.
4. Run the installer.
5. Accept the default installation folder, unless you have a reason to change it.
6. Click **Install**.
7. Wait for the installation to finish.
8. Click **Finish**.

**NOTE:** Write down the installation folder. You need this path in Section 8.

## 7. Get the Applet Files

**Note:** these files come organized into five folders (`gui`, `scripts`, `configs`, `tests`, `docs`) plus one launcher, not one flat folder. See `PACKAGING_MIGRATION.md` if you have an older, flat-folder copy of these files and want to reorganize it into this layout.

1. Create a new folder for the project, for example `C:\SLAM_Pipeline`.
2. Inside it, create five subfolders: `gui`, `scripts`, `configs`, `tests`, `docs`.
3. Copy these files into `gui`:
   - `pipeline_applet.py`
   - `pipeline_core.py`
   - `project_manager.py`
   - `about_content.json`
4. Copy these files into `scripts`:
   - `level_cloud.py`
   - `m3c2_classify.py`
   - `usd_export.py`
   - `generate_m3c2_params.py`
   - `point_spacing.py`
   - `surface_reconstruction.py`
   - `segment_planes.py`
   - `extract_damage_detail.py`
   - `slam_kiss_icp.py`
   - `decode_raw_packets.py`
5. Copy these files into `configs`:
   - `kiss_icp_config_indoor.yaml`
6. Copy `run_pipeline.bat` into the project root (`C:\SLAM_Pipeline` itself, next to the five folders - not inside any of them).

**NOTE:** `project_manager.py` is required, not optional. Without it, the applet still opens, but every project-mode feature (New Project, Open Project, the "Choose from project..." file picker) silently does not work - `pipeline_applet.py` falls back to treating the project layer as unavailable rather than showing an error, so a missing `project_manager.py` can be easy to miss.

**NOTE:** `surface_reconstruction.py` and `segment_planes.py` ARE wired into the applet's stage buttons (Stage 7 and Stage 4). `extract_damage_detail.py` is NOT wired to any stage button - run it manually from the command line, then browse its output `.ply` into Export's optional "Damage detail" field by hand.

**NOTE:** `decode_raw_packets.py` is required if you plan to import raw, undecoded Ouster packet captures (the common case) - Stage 1's Source field offers to run it automatically the first time it detects a raw source. Not required if every capture you'll use is already decoded.

## 8. Add Tools to the PATH

Do this step if `ouster-cli` or `CloudCompare` did not respond in Section 5, or if you plan to use the Cleanup and Diff stages.

1. Click the Windows Start button.
2. Type `environment variables`.
3. Click **Edit environment variables for your account**.
4. In the top list, find the row labelled **Path**.
5. Select the **Path** row.
6. Click **Edit**.
7. Click **New**.
8. Type the full folder path for the tool. Use a folder, not a file. Examples:
   ```
   C:\Users\<your username>\AppData\Local\Programs\Python\Python312\Scripts
   C:\Program Files\CloudCompare
   ```
9. Click **OK** on each open window to save the change.
10. Close all open Command Prompt windows.
11. Open a new Command Prompt window.
12. Repeat the commands in Section 5 to confirm the change.

**CAUTION:** Add the folder path only. Do not add the full path to the `.exe` file. Windows PATH entries must be folders.

**CAUTION:** Add the path to the existing **Path** variable. Do not create a new variable with a different name. Windows does not read a custom-named variable as part of the search path.

## 9. Start the Applet

**Easiest way:** double-click `run_pipeline.bat`, in the project root (next to the `gui`/`scripts`/`configs`/`tests`/`docs` folders, not inside any of them). Skip to step 4 below to confirm it worked.

To start it manually instead:

1. Open a new Command Prompt window.
2. Type this command and press Enter (adjust the path to match where you put the project):
   ```
   cd C:\SLAM_Pipeline\gui
   ```
3. Type this command and press Enter:
   ```
   python pipeline_applet.py
   ```
4. Confirm the applet window opens, showing: a row of four buttons (New Project, Open Project, New Scan, New Diff); a row with Source pipeline and Diff pipeline selectors; two rows of stage buttons (Stage 1 through Stage 8, plus About); and a log area below them.

## 10. Quick Reference Checklist

| Item | Check command | Expected result |
|---|---|---|
| Python | `python --version` | Shows a version in the 3.11 or 3.12 series |
| pip packages | `pip show ouster-sdk` | Shows package details |
| scikit-learn | `pip show scikit-learn` | Shows package details |
| PyYAML | `pip show pyyaml` | Shows package details |
| kiss-icp | `pip show kiss-icp` | Version line reads `1.2.3` |
| Ouster CLI | `where ouster-cli` | Shows a file path |
| CloudCompare | `where CloudCompare` | Shows a file path |
| project_manager.py present | Check the `gui` folder | File exists alongside `pipeline_applet.py` |
| Applet | Double-click `run_pipeline.bat`, or `python pipeline_applet.py` from inside `gui` | Opens the applet window |
