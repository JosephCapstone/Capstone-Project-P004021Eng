# Troubleshooting Sheet: SLAM Pipeline Applet

This sheet follows Simplified Technical English (ASD-STE100) conventions: short sentences, one instruction per step, active voice, and approved terminology.

Each section has a symptom, a cause, and a fix. Find your symptom, then follow the fix.

---

## 1. Symptom: `FileNotFoundError: [WinError 2] The system cannot find the file specified`

**Cause:** Windows cannot find `ouster-cli`. This tool is not on the PATH.

**Fix:**
1. Open a new Command Prompt window.
2. Type this command and press Enter:
   ```
   where ouster-cli
   ```
3. If the command shows a file path, close the current terminal window. Open a new one and try the applet again.
4. If the command shows no file path, go to Section 8 of the PC Setup Guide.

---

## 2. Symptom: `where ouster-cli` shows "Could not find files for the given pattern(s)."

**Cause:** One of two problems exists:
- The `ouster-sdk` package is not installed.
- The package is installed, but its `Scripts` folder is not on PATH.

**Fix:**
1. Type this command and press Enter:
   ```
   pip show ouster-sdk
   ```
2. If the command shows no package details, the package is not installed. Type this command and press Enter:
   ```
   pip install ouster-sdk
   ```
3. If the command shows package details, note the folder path on the **Location** line.
4. Look for a `Scripts` folder next to that location.
5. Add the `Scripts` folder to PATH. Follow Section 8 of the PC Setup Guide.

---

## 3. Symptom: You added a folder to PATH, but the tool is still not found.

**Cause:** One of three problems exists:
- The Command Prompt window is an old window. It does not have the new PATH value.
- The added entry is a file path, not a folder path.
- The added entry is in a new, custom-named variable. It is not in the variable named **Path**.

**Fix:**
1. Close all open Command Prompt windows.
2. Open a new Command Prompt window.
3. Type the `where` command for the tool again.
4. If the tool is still not found, open **Edit environment variables for your account** again.
5. Confirm the entry is in the row named **Path**. Do not use a different variable name.
6. Confirm the entry is a folder path. Remove the file name from the end, if present.

**Example — correct entry:**
```
C:\Users\C Day\AppData\Local\Programs\Python\Python312\Scripts
```

**Example — incorrect entry:**
```
C:\Users\C Day\AppData\Local\Programs\Python\Python312\Scripts\ouster-cli.exe
```

---

## 4. Symptom: A space in the folder path, for example `C:\Users\C Day\...`

**Cause:** This is not a cause of PATH problems. Windows supports spaces in folder paths without special handling in the PATH variable.

**Fix:** No fix is required. Check Sections 1 to 3 for the actual cause.

---

## 5. Symptom: The applet or script runs in IDLE, but fails with a path error.

**Cause:** IDLE can use a different PATH value than a Command Prompt window. This can happen if a tool was installed after IDLE was last opened.

**Fix:**
1. Close IDLE.
2. Open a new Command Prompt window.
3. Navigate to the applet folder. Type this command and press Enter:
   ```
   cd C:\SLAM_Pipeline\gui
   ```
4. Run the script from the Command Prompt window instead of IDLE.

---

## 6. Symptom: A Command Prompt window opens, then closes right away. No error text is visible.

**Cause:** The script finished or crashed. The window closed before you could read the result.

**Fix:**
1. Do not double-click the script file.
2. Open a Command Prompt window first.
3. Navigate to the script folder.
4. Run the script by typing `python` and the file name, for example:
   ```
   python pipeline_applet.py
   ```
5. The window stays open after the command finishes.

**NOTE:** The applet and test scripts in this project include a "Press Enter to close this window" prompt. This prompt keeps the window open even if you do double-click the file.

---

## 7. Symptom: Output files save to an unexpected folder, for example your user folder.

**Cause:** Some scripts save files to the current working folder. This folder depends on how you started the script. Double-clicking, IDLE, and the Start Menu can each use a different working folder.

**Fix:**
1. Check the script output message. It states the full save path.
2. If the path is wrong, run the script from a Command Prompt window.
3. Navigate to your intended folder first, then run the script.

**NOTE:** The current applet version saves SLAM output next to the script file. This removes the dependency on the working folder.

---

## 8. Symptom: `pip install ouster-sdk open3d pandas` gives this error:
```
ERROR: Could not find a version that satisfies the requirement open3d (from versions: none)
ERROR: No matching distribution found for open3d
```

**Cause:** The `open3d` package does not yet publish support for the newest Python releases. Your Python version is too new for this package.

**Fix:**
1. Type this command and press Enter:
   ```
   python --version
   ```
2. If the version is 3.13 or higher, install a second, older Python version.
3. Go to Section 2a of the PC Setup Guide.
4. Install Python 3.11 or Python 3.12.
5. Use the `py` launcher to run commands with the correct version, for example:
   ```
   py -3.12 -m pip install open3d
   py -3.12 pipeline_applet.py
   ```

**NOTE:** `open3d` is required by Stage 2 (Level) and the point spacing/plane segmentation/surface reconstruction helper scripts - not optional. Installing an older Python version (Section 2a of the setup guide) is the fix, not skipping this package.

---

## 9. Symptom: `pip install open3d` still fails after installing an older Python version.

**Cause:** The command uses the wrong Python version. The default `python` command may still point to the newer, unsupported version.

**Fix:**
1. Type this command and press Enter:
   ```
   py -0
   ```
2. Confirm this command lists all installed Python versions.
3. Use the `py -3.12` prefix for all commands related to this project, for example:
   ```
   py -3.12 -m pip install ouster-sdk open3d usd-core plyfile
   py -3.12 pipeline_applet.py
   ```

---

## 10. Quick Diagnostic Checklist

Run these commands in order. Stop at the first command that fails, and use the matching section above.

| Step | Command | Section if it fails |
|---|---|---|
| 1 | `python --version` | 8, 9 |
| 2 | `pip show ouster-sdk` | 2 |
| 3 | `pip show scikit-learn` | 14 |
| 4 | `where ouster-cli` | 1, 2, 3 |
| 5 | `where CloudCompare` | 3 |
| 6 | `python pipeline_applet.py` | 6, 7 |

---

## 11. Symptom: Stage 3 (Cleanup) reports success, but a later stage cannot read its output.

Stage 4 (Segment) or Stage 5 (Diff) may show an error like this:
```
[Open3D WARNING] Read PLY failed: unable to open file: ...
ValueError: '...' loaded but has zero points.
```
This happens even though Stage 3's own run report showed no error, and `project.json` shows Stage 3 (Cleanup) as `complete`.

**Cause:** CloudCompare does not save its cleaned/aligned result into the folder you picked in Stage 3's Output field. It saves the result into the SAME folder as the loaded INPUT cloud instead - normal CloudCompare CLI behavior, not a crash. An older version of `pipeline_applet.py` watched the wrong folder for this new file (the Output folder, not the Input folder), so it could not find it. When this happens, the applet still recorded Stage 3 as `complete`, pointing at a file that was never actually written to that spot.

**Fix:**
1. Confirm you are running the current version of `pipeline_applet.py` (from the latest `delta_project_manager_v2.zip`). This version watches the Input folder, matching how Stage 5 (Diff) already worked.
2. Re-run Stage 3 (Cleanup) for the affected baseline or scan, with the same settings as before.
3. Check the report popup. It should show a real `Saved to:` line under `=== OUTPUT ===`, not an error message.
4. Re-run Stage 4 (Segment) or Stage 5 (Diff) afterward.

**NOTE:** Re-running Stage 3 always overwrites its `project.json` entry with the new run's real result (Section 8.2 of the schema). You do not need to edit `project.json` by hand, and you do not need to manually delete the old, broken output file first.

---

## 12. Symptom: Stage 5 (Diff/M3C2) saves its result cloud into the WRONG folder, for example the baseline's own `04_cleanup` folder instead of the diff's output folder.

The CloudCompare log shows a line like this, naming a folder that is not the diff's output folder:
```
[I/O] File 'C:/.../baseline/04_cleanup/<name>_M3C2_<timestamp>.ply' saved successfully
```

**Cause:** CloudCompare's M3C2 command loads two clouds and treats the FIRST one loaded as the "compared" cloud. Only the compared cloud receives the M3C2 result, and CloudCompare saves that cloud back into ITS OWN input folder - the same "saves next to the input, not the desired output" behavior described in Section 11, but for a different stage. `pipeline_core.py` loads the baseline cloud first on purpose (see the NOTE below), so the M3C2 result attaches to the baseline cloud and saves into the baseline's own folder. An older version of `pipeline_applet.py`'s Stage 4 dialog watched the comparison scan's folder for the new file instead, so it never found it.

**Fix:**
1. Confirm you are running the current version of `pipeline_applet.py` (from the latest `delta_project_manager_v2.zip`). This version watches the baseline's own folder for the new M3C2 result file, matching the folder CloudCompare actually saves into.
2. Re-run Stage 5 (Diff) for the affected diff pipeline, with the same params file and settings as before.
3. Check the report popup. It should show a real `Saved to project output:` line, not an error message.

**NOTE:** Loading the baseline cloud first (and so keeping the M3C2 result anchored to the baseline's own points) is a deliberate choice, not an oversight - it reliably catches material LOSS (a hole shows up as a gap measured from a baseline point that still exists) across every diff run against that baseline. The trade-off: geometry that only exists in the newer comparison scan (added debris, a bulge) has no baseline point to anchor a core point on, so it can be under-reported. This is a known, accepted limitation for now, not a bug - see PROJECT_SCHEMA_v2.md Section 11.3a.

---

## 13. Symptom: The Stage 5 (Diff) dialog asks you to pick a save location for the M3C2 params file, every time.

**Cause:** An older version of `pipeline_applet.py` always opened a "Save As" dialog for the params file, even when running a project pipeline (a pipeline with a Baseline/Diff header at the top of the dialog).

**Fix:**
1. Confirm you are running the current version of `pipeline_applet.py`. For a project pipeline (a pipeline selected in the main window's Source/Diff pipeline selector), clicking **Generate Params File...** now names and places the file automatically, inside that diff's own output folder, following the same numbered-filename pattern as every other stage output (Section 13.1 of the schema). No save dialog opens.
2. The save dialog only opens if the Stage 5 dialog has no pipeline set (opened outside a project, with no pipeline selected in the main window). There is no separate "manual override" setting anymore that can force this on a project pipeline - project mode and manual mode both just read whatever's in the dialog's own fields; the difference is only whether a pipeline is active to auto-name the file.

---

## 14. Symptom: Stage 6 (Classify) flags points successfully, then fails with `ModuleNotFoundError: No module named 'sklearn'`.

The Command Prompt window shows a traceback like this, after a line stating how many points were flagged:
```
Clustering 7122 flagged points (method=dbscan)...
Traceback (most recent call last):
  ...
  File "...\m3c2_classify.py", line 121, in cluster_flagged_points
    from sklearn.cluster import DBSCAN
ModuleNotFoundError: No module named 'sklearn'
[exited with code 1]
```

**Cause:** The `scikit-learn` package is not installed. Stage 6 (Classify) needs it for its clustering step (`m3c2_classify.py`), which groups flagged points into damage sites and rejects spatially isolated points as likely noise. Clustering is turned ON by default, so this package is required for a normal Stage 6 run, not an optional extra. This is not a bug - point flagging itself completed correctly (the log line above the traceback confirms this) before the clustering step failed on the missing package.

**Fix:**
1. Open a Command Prompt window.
2. Type this command and press Enter:
   ```
   pip install scikit-learn
   ```
3. Confirm no red error text appears at the end.
4. Re-run Stage 6 (Classify) with the same settings as before.

**NOTE:** The package name to install (`scikit-learn`) is different from the name Python imports it under (`sklearn`) - this is normal for this package, not a typo. If you installed an older, separate Python version for `open3d` (Section 8, Section 9), use the matching `py -3.12` prefix for this command too, for example `py -3.12 -m pip install scikit-learn`.

**NOTE:** To run Stage 6 without clustering instead of installing this package, UNCHECK "Cluster flagged points into damage sites" in the Stage 6 dialog - this restores the original threshold-only behavior and does not need `scikit-learn`. This is a working fallback, not the recommended long-term fix, since clustering's isolated-point rejection is a real second false-positive filter.

---

## 15. Symptom: Stage 6 (Classify) clusters flagged points successfully, prints the cluster list, then fails with `ValueError: field type 'i8' not in [...]` while saving the output file.

The Command Prompt window shows a traceback like this, after the full list of found clusters:
```
cluster 158: 4 points, centroid=(-3.039, -2.286, 0.362), mean|d|=0.4926, max|d|=0.5384
Traceback (most recent call last):
  File "...\plyfile.py", line 1467, in _lookup_type
    type_str = _data_types[type_str]
KeyError: 'i8'

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "...\m3c2_classify.py", line 346, in main
    out_element = PlyElement.describe(filtered_data, "vertex")
  ...
ValueError: field type 'i8' not in ['int8', 'i1', 'char', ... 'float64', 'f8', 'double']
[exited with code 1]
```

**Cause:** A real bug in `m3c2_classify.py`, not a missing package or a setup problem. The `cluster_id` field it adds to the output cloud was labelled with the PLY type string `"i8"`, meant as numpy dtype shorthand for a 64-bit integer. The PLY format itself has no standard 64-bit integer type at all, though - `plyfile`'s own type table only goes up to 32-bit (`"i4"`/`int32`/`int`, the last three entries the error message lists together), so `PlyElement.describe()` fails the moment it reaches that field, after clustering itself already completed correctly (the full cluster list printed above the traceback confirms this).

**Fix:**
1. Confirm you are running the current version of `m3c2_classify.py` (from the latest `delta_project_manager_v2.zip`). This version labels the `cluster_id` field `"i4"` (32-bit) instead of `"i8"` (64-bit) - more than enough range for a cluster count that only ever reaches a few hundred, and the largest integer width the PLY format actually supports.
2. Re-run Stage 6 (Classify) with the same settings as before. Clustering itself does not need to run again from scratch - this was purely a save-time failure after clustering had already finished.

**NOTE:** This is a good example of why Section 14 said the traceback location matters: both this bug and Section 14's missing-package bug happen AFTER "Clustering N flagged points..." prints, but Section 14 fails immediately inside `cluster_flagged_points` (before any cluster list prints), while this one fails at the very end, after the full cluster list already printed, while saving the output file. Check how far the cluster list got before the traceback to tell which section applies.

---

## 16. Symptom: Stage 5 (Diff) used the classified cloud from Stage 4 (Segment) for one side, but the raw cleanup cloud for the other side, and I expected both sides to match.

**Cause:** Not a bug, and not automatic - as of the project-input-picker rework (PROJECT_INTEGRATION_PLAN.md Update 16), you pick each side's input explicitly, via the "Choose from project..." button on the Stage 5 dialog's Baseline/Comparison fields. Nothing in `pipeline_core.py` prefers one over the other anymore. If the two sides ended up different, that's whatever was picked for each - not an automatic fallback.

**Fix:** No fix is needed if this is expected. The compared points are the same cloud either way - Stage 4 (Segment) only adds a `classification` field on top of Stage 3's own output, so which one was picked does not change the M3C2 result.

1. To confirm which file was actually used for a given diff run, check that run's own report popup, or the `params`/`output` fields under that diff's `diff` stage entry in `project.json` - not `reference_source_stage`/`comparison_source_stage` (older versions of this document pointed here; nothing currently writes those fields, so they will not help).
2. To make both sides use Stage 4 (Segment)'s classified output, re-run Stage 5 (Diff) and explicitly pick each side's `segment.output` via "Choose from project..." instead of `cleanup.output`.

---

## 17. Symptom: A stage looks frozen. The window shows "running". No new console lines appear, for a long time.

This happens most often on Stage 1 (SLAM) with the KISS-ICP backend, on Stage 4 (Segment), or on a raw-packet decode - the stages that run the longest on a real, large capture. The stage may show one or two lines, for example a `SyntaxWarning` from a third-party package, then nothing.

**Cause:** A real bug in an older version of `pipeline_core.py`. Every stage that runs a Python script (SLAM, Level, Segment, Classify, Surface, Export, decode) launches that script as a separate process, and reads its console output line by line to show it in the applet's log. Python fully buffers a script's own output internally whenever that output is not going straight to a visible terminal window - true here, since the applet reads it through a pipe. A short-running stage finishes fast enough that this is not noticeable. A long-running stage (real SLAM data, a big point cloud, a real decode) can run for minutes without a single line reaching the log, even though the process is genuinely working the whole time.

**Fix:**
1. Confirm you are running the current version of `pipeline_core.py` (from the latest `delta_project_manager_v2.zip`). This version sets the `PYTHONUNBUFFERED` environment variable for every Python script it launches, which turns this buffering off. Console lines now appear as the script prints them, not only once it finishes.
2. If a stage still looks frozen after this fix, wait for it to finish or fail on its own before assuming it is stuck. A real SLAM run or a real decode can still take several minutes on a large capture - the fix restores live progress messages, it does not make the underlying work faster.
3. If a stage genuinely never finishes (many minutes with no CPU activity in Task Manager for `python.exe`), that is a separate problem, not this one. Check the input file and the config file for that stage.

**NOTE:** This bug did not change any result a stage produced. A run that finished successfully before this fix produced the same output file as it does now - the only difference is whether the console showed progress while it ran.

---

## 18. Symptom: Stage 1 (SLAM) with the KISS-ICP backend produces a noisy map, or the map does not form correctly, even on a stable robot platform.

**Cause:** The sensor may see part of its own mount hardware on the robot. This can happen even on a very stable mount - platform stability affects drift over time, not whether the mount itself sits inside the sensor's field of view. Each of these near-sensor points sits at the same spot relative to the sensor in every single frame. Added together across a whole capture, they form a dense, fixed-looking blob of noise close to the sensor in the final map. They can also throw off the SLAM solver itself: it can read this fixed blob as real unmoving geometry and align each frame partly to it instead of to the room, which can produce a map that fails to form correctly, not just a noisy-looking one.

The KISS-ICP config file's `data.min_range` field controls this. It is `0.0` by default in `kiss_icp_config_indoor.yaml` - no near-sensor points are removed.

**Fix:**
1. Open the Stage 1 (SLAM) dialog, with Backend set to KISS-ICP.
2. Check the "This config's own min_range" label. If it reads `0.0` or "Could not read a min_range from this config", no near-sensor cropping is active.
3. Enter a value in the "Min range override" field. Start small, for example `0.2` to `0.3` meters.
4. Run Stage 1 and check the resulting map in CloudCompare. If the near-sensor blob is gone and the map now forms correctly, the override is working. If some of the blob remains, raise the value a little and run again.
5. Do not raise this value further than needed. A value set too high also removes real nearby room detail, not just mount noise - so stop as soon as the blob is gone.
6. If this does not fix a noisy or broken map, the cause is likely something else - for example, a genuinely difficult capture (fast motion, few visible features, reflective surfaces). Check the frame count and point count in the console output, and compare against a capture that worked correctly.

**NOTE:** This is a per-run override, not a permanent change to the config file. Each Stage 1 run with KISS-ICP records whichever value actually applied - the override, or the config's own value, or "none" if neither is set - in `project.json` under that pipeline's `slam` stage entry, alongside the voxel size used for that same run.

---

## 19. Symptom: Stage 4 (Segment)'s outside-envelope filter flags real clutter near a wall as junk, or does not flag obvious junk far from the room.

**Cause:** The filter checks each unclassified point against the room's own footprint and height range, worked out from the detected floor/ceiling/wall points, with a margin of slack added. If the margin is too small, real clutter that sits close to a wall can fall just outside the margin and get flagged by mistake. If the margin is too large, real junk that sits just past the walls can fall inside the margin and go unflagged.

**Fix:**
1. Open the Stage 4 (Segment) dialog and check the "Envelope margin (m)" field. The default is `0.15`.
2. If real clutter near a wall is getting flagged, raise the margin a little (for example `0.25`) and run Stage 4 again.
3. If junk beyond the walls is not getting flagged, lower the margin a little (for example `0.1`) and run Stage 4 again.
4. Open the resulting `<name>_classified.ply` in CloudCompare and color by the `outside_envelope` field to check the result visually before trusting it.
5. Do not raise the margin further than needed just to silence flags - the point of this field is to separate real clutter from junk, not to flag nothing at all.

---

## 20. Symptom: Stage 4 (Segment)'s run report says the outside-envelope filter was skipped for this run.

**Cause:** The filter needs a real room footprint to check points against, worked out from the detected floor/ceiling/wall points. This needs at least 3 of those points, and they cannot all sit in a straight line (a straight line cannot form a 2D shape). A scan with very few detected surfaces - for example, only a floor and no walls found at all - can leave too little to work with.

**Fix:**
1. Check the surface list in the run report or in `manifest.json`. If few or no walls were detected, that is the more likely problem to fix first - see Section 4 (Segment)'s own Distance threshold / max planes fields, and the guidance in `about_content.json`'s Stage 4 entry, on getting more real walls to survive detection.
2. Once more real surfaces are detected, run Stage 4 (Segment) again - the filter runs automatically whenever it has enough to work with, no separate flag needs to be set.
3. This is not an error - the run still completes normally either way, with every point's `outside_envelope` field left at `0` (unevaluated) rather than a guess.
