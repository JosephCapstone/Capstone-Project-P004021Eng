# Packaging Migration Guide

How to go from one flat folder to the organized structure. Every instruction
below is a MOVE of a file you already have - never a copy-and-replace, and
never a file to download fresh from here, EXCEPT the three explicitly
marked "REPLACE" and the one marked "NEW" at the end.

## 1. Create the folders

Inside your project root (e.g. `C:\Uni\Capstone\Pipeline-Projectv5\`), create:
```
gui\
scripts\
configs\
tests\
docs\
```

## 2. Move these into gui\

- pipeline_applet.py  -> REPLACE with the version attached to this message
  (adds SCRIPTS_DIR/CONFIGS_DIR constants and updates every script/config
  default path to use them - PROJECT_INPUT_PICKER_PLAN.md-adjacent change,
  not documented there since it predates this restructure)
- pipeline_core.py (your current, correct copy - unchanged, no edits needed)
- project_manager.py (your current, correct copy - unchanged, no edits needed)
- about_content.json (unchanged - stays next to pipeline_applet.py, which is
  what ABOUT_CONTENT_FILE still expects)
- baseline_registry.json, IF it already exists (manual-mode's baseline
  registry - a runtime file the app creates/updates itself, not something
  to create by hand if it doesn't exist yet)
- measure_dialogs.py (the dialog-sizing diagnostic script from earlier in
  our chat, if you kept it - it does `import pipeline_applet as app`
  directly, so it has to stay next to pipeline_applet.py, same as
  about_content.json)

## 3. Move these into scripts\

- decode_raw_packets.py
- slam_kiss_icp.py
- level_cloud.py
- segment_planes.py
- point_spacing.py
- m3c2_classify.py
- surface_reconstruction.py
- usd_export.py
- extract_damage_detail.py
- generate_m3c2_params.py

## 4. Move these into configs\

- kiss_icp_config_indoor.yaml
- kiss_slam_config_indoor.yaml (if you've deployed this one from earlier in
  our chat)

## 5. Move these into tests\

- test_project_manager.py     -> REPLACE with the version attached to this
  message (fixes `sys.path.insert(0, ".")`, which only worked if you
  happened to run the test from inside gui\ - now works from anywhere)
- test_pipeline_core_project_mode.py -> REPLACE, same fix
- test_segment_geometry.py (was likely named claude_test_segment_geometry.py
  on your machine - rename it when you move it) -> REPLACE, fixes its
  direct reference to segment_planes.py's file location

## 6. Move these into docs\

- PROJECT_SCHEMA_v2.md
- PROJECT_INTEGRATION_PLAN.md
- PROJECT_INPUT_PICKER_PLAN.md
- pc_setup_guide.md
- troubleshooting_sheet.md
- CLEANUP_REPORT.md (was likely claude_CLEANUP_REPORT.md on your machine)

Nothing in the code references these .md files by path (checked directly -
every mention in pipeline_applet.py/pipeline_core.py/project_manager.py is
a comment citing a section number, not a file path), so this move needs no
code changes at all.

## 7. Add the launcher

Put `run_pipeline.bat` (NEW - attached to this message) directly in the
project root, next to the gui\/scripts\/configs\/tests\/docs\ folders you
just created. Double-click it to launch the applet from anywhere - it
doesn't matter what folder you're in when you double-click it.

Optional: right-click `run_pipeline.bat` -> Send to -> Desktop (create
shortcut), so you never need to open the project folder at all for normal
use.

## After migrating: verify

Run each test from the project root (not from inside tests\ or gui\ - the
whole point of the fix is that it shouldn't matter, so this is also a
genuine test of the fix, not just a formality):
```
python tests\test_project_manager.py
python tests\test_segment_geometry.py
python tests\test_pipeline_core_project_mode.py
```
The first two should show "ALL TESTS PASSED". The third will very likely
show a real failure or two - not from anything in this migration, but from
that file testing OLD auto-resolve-override behavior we deliberately
removed during the project-picker rework, and apparently never updated
since. Worth fixing as its own small task later, separate from this
migration - flagging it here so it's not mistaken for something broken by
the move itself.
