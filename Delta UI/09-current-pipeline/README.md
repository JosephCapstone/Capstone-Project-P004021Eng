# DELTA UI — version 09

Isaac Sim viewer with selected-damage information, slicing, navigation and an eight-stage point-cloud processing pipeline.

## Run

1. Install Isaac Sim 6.1. Set `ISAAC_SIM_ROOT` to its installation folder, or edit `ISAAC_SIM_LOCATION` in `DELTA_Ui_LAUNCHER.bat`. The standard standalone Downloads installation is detected automatically.
2. Double-click **DELTA_Ui_LAUNCHER.bat**, then choose **Open USD**.
3. For processing, install Python 3.12 and double-click **SETUP_PROCESSING.bat** once.
4. Click **USD Builder**, or double-click **OPEN_PROCESSING_PIPELINE.bat**.

Processing runs separately from Isaac Sim. `DELTA_PIPELINE_PYTHON` can select an existing compatible Python environment.

CloudCompare with M3C2 is required for Cleanup and Diff. Blender 5.2+ is required for Surface; the Geometry Nodes recipe is included. Set `DELTA_BLENDER` if Blender is not detected. See [the pipeline setup guide](pipeline/pc_setup_guide.md) for details.

## Contents

- `exts/`: inspection viewer.
- `pipeline/`: processing source, sensor profiles and Blender recipe.
- `tests/`: regression and Isaac Sim checks.

The pipeline comes from upstream `QT-Processing-PipelineV2` commit `e024463667c4e093d1a7607dcb503eb06b368563`, dated 21 October 2026. The viewer retains version 08's damage cards and inspection tools. Duplicate legacy interfaces and unused helper files have been removed.

Damage measurements come from the exported USD metadata. Missing severity is shown as **Not assessed**. Inspection edits use the USD session layer.

## Developer checks

From this folder:

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -v
.venv/Scripts/python.exe tests/run_kit_smoke.py kit_quick_info_smoke.py
```

Validated locally: 22 regression tests, processing startup and Isaac Sim damage-card integration passed. Full scan processing has not been rerun for this release.

## Linux

Set `ISAAC_SIM_ROOT` and run `bash DELTA_Ui_LAUNCHER.sh`. Create `.venv` using Python 3.12 and install `pipeline/requirements.txt`.
