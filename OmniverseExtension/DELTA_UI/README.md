# DELTA UI version 08

Isaac Sim viewer with compact damage callouts and a detailed Configuration panel.

## Start

Set `ISAAC_SIM_ROOT` to the Isaac Sim 6.1 installation directory if it is not in the default Windows location. Run `DELTA_Ui_LAUNCHER.bat` (Windows) or `DELTA_Ui_LAUNCHER.sh` (Linux).

To use USD Builder, install Python 3.12 and run `SETUP_PROCESSING.bat` once. On Linux create `.venv` and install `QT-Processing-PipelineV2/requirements.txt`. `DELTA_PIPELINE_PYTHON` can select another compatible environment. `OPEN_PROCESSING_PIPELINE.bat` opens the processing app directly. External stages retain their CloudCompare/Blender prerequisites; see the bundled pipeline setup guide.

## Inspection

Select a damage site or Bounds cube to show a compact card with ID/name, dimensions, severity and baseline displacement. The card follows the site, stays clear of the toolbar, and flips/clamps near the viewport edge. View details opens Configuration with member-point count, full dimensions, damage type, surface and peak displacement. Clearing selection, hiding damage boxes, scene closure and fully clipped/offscreen sites hide the card.

Severity shows `Not assessed` when absent. Signed meanChangeInward is positive for added material and negative for removed material; meanMagnitude is an unsigned fallback. No area-change percentage is invented. XYZ extents are bounding dimensions.

The viewer retains site navigation, temporary naming, camera views, isolation, layer controls, colour swatches, the 30 FPS cap, and Slice / see inside. Slicing is temporary and uses the stage up axis initially; position, flip and through-selected-site controls remain available. Inspection changes use the session layer and do not rewrite source scans.

## Pipeline and validation

The full original eight-stage processing pipeline is included. `PIPELINE_MANIFEST.json` records the exact source/assets from CDAYG commit cb4f23ae2c454a457f0846d8c5da0cb85f83d4ae. Its algorithms are unchanged. The app uses its own `.venv` or DELTA_PIPELINE_PYTHON and does not require another local build.

From this directory run `.venv/Scripts/python.exe -m unittest discover -s tests -v`. On this PC 22 tests passed. Optional Isaac Sim integration: `.venv/Scripts/python.exe tests/run_kit_smoke.py kit_quick_info_smoke.py`. Kit smoke tests generate fixtures, result JSON and screenshots under tests and are excluded from Git.
