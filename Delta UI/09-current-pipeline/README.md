# DELTA UI

Isaac Sim viewer and eight-stage point-cloud processing pipeline.

Bundled pipeline updated on 21 October 2026 from the upstream
`QT-Processing-PipelineV2` branch, commit `e024463667c4e093d1a7607dcb503eb06b368563`.
The DELTA `pipeline/Gui/main.py` entry point forwards to the upstream Qt app;
USD Builder and OPEN_PROCESSING_PIPELINE.bat use this bundled version.

## Launch on Windows

1. Install Isaac Sim 6.1.
2. Open `DELTA_Ui_LAUNCHER.bat` in a text editor. Change `ISAAC_SIM_LOCATION` near the top to the folder containing `kit\kit.exe`. The standard standalone Downloads folder is also detected automatically.
3. Double-click `DELTA_Ui_LAUNCHER.bat` and choose **Open USD**.

An `ISAAC_SIM_ROOT` environment variable overrides the configured location. The viewer can open existing USD scenes without installing the processing packages. Damage cards require a scene exported with damage-site metadata.

## Set up processing once

Install Python 3.12 with the Python launcher, then double-click `SETUP_PROCESSING.bat`. It creates `.venv` beside the launcher and installs `pipeline/requirements.txt`.

In DELTA, click **USD Builder**. `OPEN_PROCESSING_PIPELINE.bat` opens the same application directly. If using an existing compatible environment, set `DELTA_PIPELINE_PYTHON` to its Python executable.

CloudCompare with the M3C2 plugin is required for Cleanup/Diff. Blender 5.2+ is required by the supplied surface recipe. Choose their executables in the pipeline when needed; `DELTA_BLENDER` can also specify Blender's location. Keep the entire `pipeline` folder together. Copy scan files and any referenced USD assets separately.

## Inspection

Select a damage site for its ID, dimensions, severity and baseline displacement. **View details** opens the fuller Configuration panel. Missing severity is shown as **Not assessed**. Baseline values describe exported displacement, not an inferred percentage change.

Site navigation, temporary names, camera views, isolation, layer controls and slicing are available in the viewer. Inspection changes remain in the USD session layer. Temporary names and slicing reset when the scene closes.

Slicing filters points and hides mesh faces beyond the cutting plane, including
unbound scan meshes that bypass RTX section planes. Mesh cut edges follow the
authored faces; crossing faces are omitted. Large meshes can take a few seconds
for the first cut or an axis change. Reset restores the original mesh holes.

## Folder contents

- `exts/`: the viewer extension.
- `pipeline/`: the processing app, shared helpers and required recipe assets.
- `tests/`: developer checks; not needed for normal use.
- `.venv/` and `logs/`: created locally by setup or processing.

## Linux

Set the installation path near the top of `DELTA_Ui_LAUNCHER.sh`, or export `ISAAC_SIM_ROOT`, and run `bash DELTA_Ui_LAUNCHER.sh`. For processing, create `.venv` with Python 3.12 and install `pipeline/requirements.txt` using that environment's pip.

## Developer checks

From this folder, run `.venv/Scripts/python.exe -m unittest discover -s tests -v` and `.venv/Scripts/python.exe tests/check_project_workflow.py`. Optional Isaac Sim integration: `.venv/Scripts/python.exe tests/run_kit_smoke.py kit_quick_info_smoke.py`. Generated test results and images stay under `tests/` and are ignored by Git.

## Local version layout

This is version 09, relocated into Capstone/Delta UI on 21 October 2026.
Scans/, Processed/ and Sample scenes/ hold the retained local data.
Launchers resolve this version relative to their own folder.
