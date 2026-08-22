# Project Layer Integration Plan

**Version 2.** This version replaces Version 1. Version 1 described a plan
for future work. This version describes the integration as it exists
today, against PROJECT_SCHEMA_v2.md. Section 5 keeps the original
migration steps as a historical record, marked complete. Section 6 lists
what is still open.

**Update.** Section 3.5 and Section 4.4 are new. They describe the
raw-packet detection and conversion step added to the Stage 1 (SLAM)
dialog's Source field.

**Update 2.** `decode_raw_packets.py` has now run against real hardware
data and works end to end (Section 3.5). The raw-packet check no longer
skips itself when Backend is Ouster CLI or left blank (Section 4.4). A
converted bag is now recorded on the pipeline itself
(`raw.decoded_path`, PROJECT_SCHEMA_v2.md Section 12.1), not just filled
into the dialog's Source field, so Stage 1 keeps using it and the run
stays tracked in `project.json`. Section 4.2 also now covers the
past-output picker that wires `list_stage_outputs` into Stage 2, 3, 6,
and 7's input fields.

**Update 3.** The main window's Source pipeline selector (Section 4.1)
gets a new "Set Decoded Source..." button, for a decoded bag that
already exists on disk but was never converted through the SLAM
dialog's own auto-convert flow - see Section 4.1. Stage 1's Backend
field and Stage 7's Method field (Section 4.2) are now radio buttons
instead of a preset dropdown paired with a separate free-text field -
both are genuinely closed two-value choices, not a continuous range
with suggested starting points, so the free-text field was redundant
and could be left blank or typo'd with no feedback.

**Update 4.** Two changes to the Stage 1 (SLAM) dialog. First,
`pipeline_core.py` can now read the voxel size a KISS-ICP run will
use straight from its config file, and the dialog can also override
that value - see Section 3.6 and Section 4.4a. Stage 2 (Level)'s
distance-threshold guidance now reads this value back, so it can
suggest a sensible starting point for either backend, not only Ouster
CLI. Second, the SLAM dialog now shows only the fields for the Backend
currently picked - see Section 4.2a.

**Update 5.** `segment_planes.py` (floor/ceiling/wall classification) is
now a wired pipeline stage - `segment`, running in the baseline/scan
pipeline right after Cleanup (not in the diff pipeline - it describes
one cloud's own structure, not a comparison between two clouds). See
Section 3.7 and Section 4.2b, and PROJECT_SCHEMA_v2.md Sections 8, 9,
10.2, and 13.3 for the schema side (this stage is the one exception to
the usual single-file output convention - it writes several files
together into one folder).

**Update 6.** `m3c2_classify.py` (Stage 6) now clusters its flagged
points into discrete damage sites (DBSCAN or HDBSCAN), on top of its
original RMS-threshold filtering - a spatially isolated flagged point
gets rejected as a second, independent false-positive filter, and
surviving clusters get a per-site summary (centroid, point count,
extent, mean/max magnitude). On by default; `build_classify_command()`
and the Stage 6 dialog both gained cluster controls. See Section 3.8 and
Section 4.2c, and PROJECT_SCHEMA_v2.md Section 8's `classify` entry and
Section 3.3 for the schema side.

**Update 7.** Two fixes/changes after real end-to-end testing of Update
5 and 6. First, a real bug: Stage 3 (Cleanup)'s dialog watched the wrong
folder for CloudCompare's new output file (the desired Output folder,
instead of the Input file's own folder, which is where CloudCompare
actually saves a processed cloud) - a successful Cleanup run could still
silently record `project.json`'s `cleanup` stage as `complete` pointing
at a file that was never written. Fixed to watch the Input's folder,
matching Stage 5 (Diff)'s dialog, which already did this correctly.
Second, `segment_planes.py`'s default output changed: it now writes only
the combined `classified.ply` (plus `envelope.ply`/`manifest.json`)
unless a new `write_separate_surfaces` option is turned on - real testing
showed the per-surface files (`floor.ply`, `wall_1.ply`, etc.) being
written automatically wasn't wanted; the combined, classified cloud was
always meant to be the stage's actual deliverable. See Section 3.7
(updated) and PROJECT_SCHEMA_v2.md Section 16's own changelog entry.

**Update 8.** Two more changes after further real testing. First,
another real bug, this time in `level_cloud.py` (Stage 2): its floor-
picking heuristic scored every candidate plane by point count x
horizontality, so a real ceiling - which can have MORE points than a
real floor (a cleaner surface, less clutter) - could win and get treated
as the floor, confirmed on real data. Fixed to pick the LOWEST candidate
that clears `--horizontal-threshold`, not the biggest one - the same
approach `segment_planes.py` already uses (and has validated) to split
floor from ceiling once both are already known to be horizontal.
`build_level_command()` and the Stage 2 dialog both gained a
`horizontal_threshold` control for this. See Section 3.6a (new).

Second, `segment_planes.py`'s tuned defaults changed to match a
combination confirmed to work well on real full-room/compartment scans:
`distance_threshold` 0.02 → 0.05, `max_planes` 10 → 20,
`min_inlier_fraction` 0.015 → 0.003, `cluster_eps` 0.15 → 0.5. Changed in
the script's own argparse defaults, `build_segment_command()`'s Python
defaults, and the Stage 4 dialog's default field values and preset
selector, so manual-mode, project-mode, and GUI runs all agree on the
same starting point. See Section 3.7 (updated) and PROJECT_SCHEMA_v2.md
Section 16's own changelog entry.

**Update 9.** Two more changes after real testing of Stage 5 (Diff).
First, a real bug: CloudCompare's `-M3C2` command treats the FIRST cloud
loaded via `-O` as the "compared" cloud - the only cloud that receives
the M3C2 result, and the only cloud CloudCompare resaves, into ITS OWN
input folder. `build_diff_command()` loads `baseline_ply` first (a
deliberate choice - see Section 3.4a), so the M3C2 result attaches to
and resaves via the baseline cloud, in the baseline's own folder -
confirmed directly from a CloudCompare run log showing the result file
appearing under `baseline/04_cleanup/` instead of the diff's own output
folder. The Stage 5 dialog's own folder-watching code disagreed with
this: it watched `comparison`'s folder instead, so it never found the
file CloudCompare actually wrote. Fixed by watching `baseline`'s folder
instead, matching the cloud order `build_diff_command()` already used -
no change to `build_diff_command()` itself was needed or made. See
Section 3.4a (new) and PROJECT_SCHEMA_v2.md Section 11.3a (new) and
Section 16's own changelog entry.

Second, the "Generate Params File..." button on the Stage 5 dialog
always opened a save-location dialog, even for a project pipeline, where
every other stage output is named and placed automatically. Fixed so a
project pipeline now names and writes the params file automatically,
following the same pattern as every other stage output, inside the
diff's own output folder. The save dialog still appears in manual mode,
or with "Use manual file selection instead" checked. See Section 4.2
(updated) and PROJECT_SCHEMA_v2.md Section 13.4 (new).

**Update 10.** A real bug found on real hardware data, in Stage 6
(Classify): `m3c2_classify.py` labelled its `cluster_id` output field
`"i8"` (64-bit integer) when handing the data to
`plyfile.PlyElement.describe()`. The PLY format has no standard 64-bit
integer type, so `plyfile` raised `ValueError: field type 'i8' not in
[...]` while saving - after clustering itself had already completed and
printed its full cluster list, which is why the failure showed up only
at the save step. Fixed by labelling the field `"i4"` (32-bit) instead,
which comfortably covers the actual range of values (`-1` for noise, or
a small cluster index - real runs have topped out in the low hundreds).
See Section 3.8a (new) and PROJECT_SCHEMA_v2.md Section 16's own
changelog entry.

**Update 11.** `segment_planes.py` now prefixes every `.ply` it writes
with its own output folder's name (for example
`compartment_04_segment_001_classified.ply` instead of a bare
`classified.ply`), so files from different runs stay distinguishable
even when opened outside their own folder - previously every run wrote
the exact same generic filenames, which looked identical to each other
in a viewer's file list. `manifest.json` keeps its fixed, unprefixed
name. No change was needed in `pipeline_core.py` or `pipeline_applet.py`'s
path-handling logic, since both already read paths back from
`manifest.json`'s own fields rather than hardcoding filenames; only
user-facing hint text was updated. See Section 3.7a (new) and
PROJECT_SCHEMA_v2.md Section 13.3 (updated) and Section 16's own
changelog entry.

**Update 12.** Two changes, together implementing what Section 6 had
listed as an open item (carrying `segment`'s `classification` field
through the diff). First, a documentation/display change: the pipeline
stage numbers shown in dialog titles, buttons, hints, and this document
are renumbered so `segment` displays as Stage 4 and `diff`/`classify`/
`surface`/`export` display as Stage 5/6/7/8 (previously `segment`
displayed as "Stage 3.5", with `diff` through `export` as Stage 4-7).
This matches PROJECT_SCHEMA_v2.md Section 5's folder-naming scheme, which
already numbered `05_segment`/`05_diff` through `08_export` this way -
purely a display and documentation change, no internal stage name
(`slam`/`level`/`cleanup`/`segment`/`diff`/`classify`/`surface`/`export`)
or folder path changed. Second, a real behavior change:
`project_manager.get_diff_inputs()` now prefers each side's
`segment.output` over `cleanup.output`, resolved independently per side,
falling back to `cleanup.output` only when that side's `segment` stage
has not completed. `get_diff_inputs()` now also returns
`reference_source_stage` and `comparison_source_stage` (`"segment"` or
`"cleanup"`), recorded on the diff's own stage entry so which stage
supplied each side's cloud is visible in `project.json` without
re-deriving it. See Section 3.4 (updated), Section 3.7 (updated), and
PROJECT_SCHEMA_v2.md Section 11.3 (updated) and Section 16's own
changelog entry.

**Update 13.** A real bug, found on a real Windows machine, reported as
a stage looking "frozen" - the applet's log showed one or two startup
lines, then nothing, for the entire time a long-running stage (KISS-ICP
SLAM on a real capture) actually took to finish. Root cause:
`run_streaming()`'s child Python process fully buffers its own stdout
internally whenever it isn't attached to a real terminal, so its
`print()` output never reached the applet's log until the buffer filled
or the process exited - by which point a long stage's run was already
over. Fixed by passing an explicit `env` to the subprocess with
`PYTHONUNBUFFERED=1` set, which disables that buffering for the child
process specifically, without affecting a non-Python child (CloudCompare)
or losing anything else from the environment (`PATH` and the rest are
still inherited). See Section 3.9 (new) and `troubleshooting_sheet.md`
Section 17 (new). No schema change - this only affects whether console
output streams live; it never changed what any stage actually produced.

## 1. Purpose

This document describes how the project layer works across
`project_manager.py`, `pipeline_core.py`, and `pipeline_applet.py`. This
document assumes the schema in PROJECT_SCHEMA_v2.md. This document does
not change the pipeline stages themselves.

## 2. Module: `project_manager.py`

### 2.1 Concept: the PipelineHandle

Version 1 had one pipeline per project, so a function only needed a
project object to know what it was reading or writing. Version 2 splits
a project into a baseline pipeline, zero or more scan pipelines, and
zero or more diff pipelines (PROJECT_SCHEMA_v2.md Section 4). A function
now also needs to know WHICH pipeline it is reading or writing.

`project_manager.py` solves this with a `PipelineHandle`: a pointer at
one specific pipeline. Get one from a `Project` object:

| Call | Points at |
|---|---|
| `project.baseline_handle()` | The project's one baseline. |
| `project.scan_handle(scan_id)` | One scan, by ID. |
| `project.diff_handle(diff_id)` | One diff, by ID. |

Almost every function below takes a `PipelineHandle`, not a bare
`Project`. This stops a caller from writing one scan's cleanup result
into a different scan or into the baseline by mistake.

### 2.2 Functions

| Function | Purpose |
|---|---|
| `create_project(root_dir, compartment_name, source_path, source_type, link_raw="copy")` | Creates the project root folder and the `baseline/` folder structure. Copies, moves, or links the raw source into `baseline/01_raw/`. Writes the first `project.json` file, with empty `scans` and `diffs` objects. Returns the `Project` object. |
| `add_scan(project, label, source_path, source_type, link_raw="copy")` | Adds a comparison scan. Creates `scans/<scan_id>/` and its own `01_raw/` through `04_cleanup/` folders. Imports the raw source. Returns the new scan ID. |
| `add_diff(project, label, reference, comparison)` | Adds a diff. `reference` is the string `"baseline"` or a scan ID. `comparison` is a scan ID. Creates `diffs/<diff_id>/` and its own `05_diff/` through `08_export/` folders. Returns the new diff ID. |
| `load_project(project_root)` | Reads `project.json` from a folder. Returns the `Project` object. Raises an error if the file is missing or the file does not match schema version 2. |
| `save_project(project)` | Writes the project object back to `project.json`. Updates the `updated` timestamp. Writes to a temporary file first, then replaces the old file. This step avoids a corrupted file if the write fails partway. |
| `list_scans(project)` / `list_diffs(project)` | Return a sorted list of this project's scan IDs / diff IDs. |
| `get_input_for_stage(handle, stage_name)` | Returns the input path for one stage in one pipeline: the raw source, or its decoded form if `set_decoded_raw_path` recorded one (baseline/scan pipelines, stage `slam` - PROJECT_SCHEMA_v2.md Section 12.1), or the previous stage's recorded output. Raises an error if asked for a diff pipeline's `diff` stage - that stage has two inputs, not one. |
| `set_decoded_raw_path(handle, decoded_path)` | Records a decoded copy of a baseline/scan's raw import (Section 12.1). Does not change `raw.path` - that field keeps recording the true original import. Once set, `get_input_for_stage` for that pipeline's `slam` stage resolves to the decoded copy instead. |
| `get_diff_inputs(handle)` | For a diff-kind handle only. Returns the `reference` side's and `comparison` side's cloud paths, each independently preferring that side's `segment.output` over `cleanup.output` (falling back to `cleanup.output` when `segment` hasn't completed for that side - PROJECT_SCHEMA_v2.md Section 11.3), plus `reference_source_stage`/`comparison_source_stage` (`"segment"` or `"cleanup"`) and `registration_error_used` (copied from the comparison side's own `cleanup.icp_rms`). |
| `get_baseline_cleanup_output(project)` | Returns the project's own baseline cleanup output, regardless of which diff is asking. Used by Stage 8 (Export)'s environment layer. |
| `get_output_path(handle, stage_name, extension)` | Builds the default output path for a stage. Uses the naming convention in PROJECT_SCHEMA_v2.md Section 13. |
| `get_absolute_path(project, relative_path)` | Joins a project-relative path with the project root. |
| `to_relative_path(project, path)` | The reverse: turns an absolute path into a project-relative one. Used when a caller builds an `extra_fields` value the schema documents as a relative path, for example `sidecar` or `m3c2_params_file`. |
| `list_stage_outputs(handle, stage_name)` | Lists every output file actually present in a stage's folder, not just the one recorded as current. Backs a "pick an earlier pass" dropdown. |
| `find_next_stage(handle)` | Returns the first stage, in this pipeline's own order, whose status is not `complete`. Returns `None` if the pipeline is finished. Replaces Version 1's `current_stage` field, which Version 2 does not have (PROJECT_SCHEMA_v2.md Section 7). |
| `start_stage(handle, stage_name, params)` | Sets the stage `status` to `running`. Sets `started` to the current timestamp. Increases `rerun_count` if the stage was already `complete`. Calls `save_project`. |
| `complete_stage(handle, stage_name, output_path, extra_fields, log_path)` | Sets the stage `status` to `complete`. Sets `output`, `completed`, and optionally `log_path` and any stage-specific extra fields, for example `icp_rms`. Calls `save_project`. |
| `fail_stage(handle, stage_name, error_message)` | Sets the stage `status` to `failed`. Calls `save_project`. |
| `promote_baseline(project, stage_name)` | Sets the `baseline` object's promotion fields. This function is a stub. This function connects to the compartment registry in a later phase (Section 6). |

### 2.3 Design Rule

`project_manager.py` does not call `subprocess`. `project_manager.py`
does not build CLI commands. `pipeline_core.py` keeps that
responsibility. `project_manager.py` only reads and writes project state.

## 3. `pipeline_core.py`

### 3.1 The `pipeline` Parameter

Every stage-runner function in `pipeline_core.py` takes one optional
parameter: `pipeline`. Default value: `None`.

- If `pipeline` is `None`, the function runs in manual mode. A caller
  passes every input path by hand, same as before project mode existed.
- If `pipeline` is a `PipelineHandle`, the function resolves its input
  automatically, through `project_manager.get_input_for_stage(pipeline,
  stage_name)` (or `get_diff_inputs(pipeline)` for Stage 5, or
  `get_baseline_cleanup_output(pipeline.project)` for Stage 8's baseline
  input) - and ignores whatever input path the caller also passed in.

Every stage now supports this parameter, not only Stage 1 and 2 as in
Version 1:

| Stage | Function | Pipeline kind |
|---|---|---|
| 1. SLAM | `build_slam_command`, `build_kiss_icp_slam_command` | baseline or scan |
| 2. Level | `build_level_command` | baseline or scan |
| 3. Cleanup | `build_cleanup_command` | baseline or scan |
| 4. Segment | `build_segment_command` | baseline or scan |
| 5. Diff | `build_diff_command` | diff |
| 6. Classify | `build_classify_command` | diff |
| 7. Surface | `build_surface_command` | diff |
| 8. Export | `build_export_command` | diff |

Stage 7 (Surface) is new in this integration. It delegates to
`surface_reconstruction.py`, reconstructing a mesh from Stage 6's
classified change cloud. PROJECT_SCHEMA_v2.md's folder layout (Section 5)
already reserved `07_surface/` for it; Version 1's `pipeline_core.py` and
`pipeline_applet.py` had not wired it up yet.

### 3.2 Project Hooks Around Existing Logic

Each stage-runner function calls `project_manager.start_stage(...)`
before the subprocess call, when `pipeline` is given. `pipeline_core.py`
also provides `finish_stage(pipeline, stage_name, output_path, success,
error_message, extra_fields, log_path)`, called once the subprocess has
actually finished, to report success (`complete_stage`) or failure
(`fail_stage`). This is a separate call because a `build_X_command()`
function returns the command to run, long before that command's
subprocess has finished - `pipeline_applet.py`'s stage dialogs call
`finish_stage` from their own completion handler.

### 3.3 Stage 3: RMS Sidecar and `icp_rms`

Stage 3 parses the CloudCompare log for the ICP RMS value and writes the
sidecar file, same as Version 1. In project mode, the applet also passes
the RMS value, and the sidecar's own relative path (via
`project_manager.to_relative_path`), to `finish_stage`'s `extra_fields` -
so `project.json`'s own `cleanup.icp_rms` and `cleanup.sidecar` fields
get a copy of the same values (PROJECT_SCHEMA_v2.md Section 8).

### 3.4 Stage 5: Reading the Two Inputs

Stage 5 (Diff) is the one stage whose project-mode input resolution
looks different from every other stage. Every other stage has exactly
one input: the previous stage's own output, within the same pipeline.
Stage 5's diff pipeline instead names two OTHER pipelines it compares -
its `reference` and `comparison` (PROJECT_SCHEMA_v2.md Section 11.3) -
so `build_diff_command()` calls `project_manager.get_diff_inputs(pipeline)`
instead of `get_input_for_stage()`, and that call resolves both input
clouds at once, plus `registration_error_used`. As of Update 12, each
side's cloud independently prefers that side's `segment.output` (Stage 4)
over `cleanup.output`, falling back to `cleanup.output` when `segment`
hasn't completed for that side yet (Section 3.7 (updated),
PROJECT_SCHEMA_v2.md Section 11.3) - `get_diff_inputs()` also returns
`reference_source_stage` and `comparison_source_stage` recording which
one it picked for each side.

In manual mode, the "Load from Stage 3" button still reads the RMS
sidecar file directly, same as Version 1.

### 3.4a Fixing Stage 5's Output-File Detection

New in Update 9. `build_diff_command()` builds a CloudCompare command of
the shape `-O <cloud A> -O <cloud B> -M3C2 <params> ... -SAVE_CLOUDS`.
CloudCompare's `-M3C2` treats cloud A (loaded first) as the "compared"
cloud - only cloud A receives the M3C2 distance result (its points
become the M3C2 "core points"), and only cloud A's file gets resaved by
`-SAVE_CLOUDS`, into cloud A's OWN input folder (the same "saves next to
the input, not the desired output" behavior Update 7 found in Stage 3
(Cleanup), just showing up in a different stage).

The function loads `baseline_ply` as cloud A - kept deliberately (see
PROJECT_SCHEMA_v2.md Section 11.3a): anchoring core points to the fixed
baseline reliably catches material LOSS across every diff run against
that baseline, at the cost of under-reporting geometry that only exists
in the newer comparison scan. Bidirectional M3C2 (running it both ways
and merging) would close that gap without giving up the baseline-fixed
run; scoped as a possible later addition in Section 6, not built now.

Because cloud A is `baseline_ply`, the M3C2 result attaches to and
resaves via the baseline cloud - confirmed directly from a CloudCompare
run log showing the result file appearing under `baseline/04_cleanup/`
rather than the diff's own output folder. The Stage 5 dialog's `build()`
computed `output_dir = Path(comparison).resolve().parent` and watched
THAT folder for a new file - correct in structure (watch the input's own
folder, exactly like Stage 3's Update-7 fix), but watching the wrong
cloud's folder, since `comparison_ply` is cloud B, not cloud A.

Fixed by watching `Path(baseline).resolve().parent` instead, matching
the cloud order `build_diff_command()` already uses -
`core.resolve_cleanup_output()` is now also called with `baseline` (not
`comparison`) as its `input_ply` argument, since that is the file whose
stem CloudCompare's saved filename actually starts with. No change was
made to `build_diff_command()`'s cloud order itself.

### 3.5 Raw Packet Detection and Conversion

New in this update. `pipeline_core.py` adds two functions for a ROS2 bag
that holds raw Ouster sensor packets instead of a decoded point cloud
topic:

- `inspect_rosbag_topics(bag_folder)`: Reads a ROS2 bag folder's
  `metadata.yaml` file. Returns `None` if the folder is not a ROS2 bag.
  Otherwise returns a dict that lists the bag's topics, whether a
  `PointCloud2` topic exists, and the names of any raw lidar packet
  topic, raw IMU packet topic, and metadata topic found.
- `build_decode_command(decode_script, bag_folder, output_bag, ...)`:
  Builds the command line that runs the new `decode_raw_packets.py`
  script. This script converts raw packets into a `PointCloud2` (plus
  IMU) ROS2 bag that KISS-ICP can read.

This check runs at the point where a user picks a Source folder for
Stage 1 (SLAM). It does not run inside `create_project` or `add_scan`.
Section 4.4 below describes the dialog behavior. Section 6 lists moving
this check into `create_project`/`add_scan` as an open item.

`decode_raw_packets.py` is a new standalone script, not part of
`pipeline_core.py` itself. It reads the Ouster sensor metadata from the
bag's metadata topic, batches the raw lidar packets into full scans with
the Ouster SDK, and writes a new ROS2 bag with a decoded `PointCloud2`
topic (default name `/ouster/points`). It counts IMU packets found but
does not decode them yet - IMU decoding waits until a SLAM method that
uses IMU data is in use.

This script has now run against real hardware data and works end to
end: a real capture decoded 748 scans from 47,906 lidar packets with 0
packets skipped, producing a bag with a real `PointCloud2` topic that
KISS-ICP's rosbag dataloader reads correctly. Getting there took six
real-data debugging rounds (import path, CDR parsing, packet
construction, a native crash, and finally the actual wrong CDR byte
layout) - the script's own STATUS section keeps the full history, in
case a different bag or a different `ouster_ros` build hits a similar
issue again.

### 3.6 Reading KISS-ICP's Voxel Size

New in Update 4. Ouster CLI's Stage 1 dialog has always shown its voxel
size as a plain field, so its value is easy to read back later. KISS-ICP
has no such field - its voxel size lives inside a config YAML file, or
falls back to kiss-icp's own built-in default if no config is given at
all. This made it hard for Stage 2 (Level)'s guidance to suggest a
sensible starting point when KISS-ICP was the backend used.

`pipeline_core.read_kiss_icp_voxel_size(config_path)` reads the
`mapping.voxel_size` field from a KISS-ICP config YAML. It returns
`None`, not an error, when the path is empty, the file does not exist,
the file is not valid YAML, or the field is simply not set (a real,
valid state - it means kiss-icp's own auto-derived default applies).

`build_kiss_icp_slam_command()` takes a new optional `voxel_size`
parameter. This value, when given, overrides the config's own voxel
size for this one run, without editing the YAML file. It is passed to
`slam_kiss_icp.py` as a new `--voxel-size` flag. This flag is not yet
confirmed against a real installed kiss-icp version - see that script's
own dated note.

Whichever value actually applies - the override, or else the value read
from the config, or else `None` if genuinely unknown - is recorded in
`project.json` under the SAME `params.voxel_size` key Ouster CLI's own
Stage 1 already uses. Stage 2 (Level)'s distance-threshold guidance
reads this one key, so its logic does not need to know which backend
Stage 1 actually used.

### 3.6a Fixing `level_cloud.py`'s Floor-vs-Ceiling Bug

New in Update 8. `level_cloud.py`'s original `pick_floor_candidate()`
scored every candidate plane by `count x |normal.z|` - the biggest,
most-horizontal candidate won, with no notion of where it actually sat.
Confirmed on real data: a real ceiling can have MORE points than a real
floor (a cleaner surface, less obstructed by clutter/machinery), so this
scoring could - and did - pick the ceiling and treat it as the floor,
leveling the whole cloud upside down relative to what was intended.

The fix mirrors `segment_planes.py`'s own already-validated approach to
the same underlying problem: first narrow to candidates that are
actually near-horizontal (`|normal.z| >= horizontal_threshold` - this
alone already rules out walls), then, among those, pick the LOWEST one
by Z centroid, not the biggest. A ceiling and a floor can both clear the
horizontal bar; only one of them is on the bottom.

`pick_floor_candidate()` now takes `horizontal_threshold` (default
`0.7`, matching `segment_planes.py`'s own default) and returns
`(floor_candidate, used_fallback)`. `used_fallback` is `True` only when
NO candidate clears the horizontal bar at all - an unusual scan - in
which case the function falls back to the old count-weighted score
rather than failing outright, and the caller prints a clear warning
rather than silently trusting a possibly-wrong pick.

`build_level_command()` gained a matching optional `horizontal_threshold`
parameter (passed through as `--horizontal-threshold` when given, same
`None`-means-"let the script use its own default" pattern the other
Level parameters already use), and the Stage 2 dialog gained a matching
field. The run report's tool-output section now also prints each
candidate's Z position and whether it cleared the horizontal bar, so a
wrong pick is visible directly in the console output, not just inferable
from a bad-looking leveled result afterward.

### 3.7 Stage `segment`: Classifying a Cloud's Own Structure

New in Update 5. `segment_planes.py` already existed as a standalone
script, written but never run against real data and never wired into
`pipeline_core.py`. It classifies a cleaned cloud's points into floor,
ceiling, individual walls, and unclassified, using the same RANSAC
plane-finding approach Stage 2 (Level) uses, extended to keep every
detected surface instead of stopping once it finds the floor.

Updated in Update 8: the script's own argparse defaults, and matching
defaults in `build_segment_command()` and the Stage 4 dialog, changed
to `distance_threshold=0.05`, `max_planes=20`, `min_inlier_fraction=0.003`,
`cluster_eps=0.5` - a combination confirmed to work well on real full-
room/compartment scans (the old defaults were tuned for a tighter, more
conservative starting point that undercounted real surfaces on this kind
of scan). `cluster_min_points`, `horizontal_threshold`,
`max_horizontal_z_span`, and the merge-related parameters are unchanged.

`build_segment_command()` follows the same `pipeline` parameter pattern
as every other stage (Section 3.1), with one real difference:
`segment_planes.py` writes into one folder, not one output file - so
this function takes `output_dir`, not `output_ply`. What lands in that
folder depends on `write_separate_surfaces` (default `False`): off, it's
just a combined `classified.ply` (carrying a `classification` field), an
`envelope.ply`, and a `manifest.json` - one cloud, not a file per
detected surface. On, it also gets a cloud per detected surface
(`floor.ply`, `wall_1.ply`, and so on) and an `unclassified.ply`.

The off-by-default behavior replaced an earlier version that always
wrote the per-surface files - real end-to-end testing showed that wasn't
what was wanted (the classified/combined cloud, carrying every point
with a `classification` field, was always meant to be the stage's real
deliverable; the per-surface files were a secondary, tuning-oriented
byproduct that shouldn't appear unasked). `write_separate_surfaces` makes
that byproduct opt-in instead of automatic.

`resolve_segment_output(output_dir)` reads `manifest.json` back once a
run finishes, and returns the pieces project mode needs: the path to
`classified.ply` (this stage's recorded `output`), and an `extra_fields`
dict with `envelope_output`, `classification_ids`, and `surfaces` -
absorbing `manifest.json`'s own content into `project.json`
(PROJECT_SCHEMA_v2.md Section 3.3's stated design), so nothing needs to
read that file separately once a run is recorded. Each surface's own
`file` entry is `null` when `write_separate_surfaces` was off - the
surface is still listed by name/count/normal/Z-range, just without a
file to point at. This mirrors Stage 3 (Cleanup)'s own `icp_rms`/
`sidecar` hand-off (Section 3.3 above) - `resolve_segment_output()`
returns plain paths, and the caller (the dialog, in `pipeline_applet.py`)
converts them to project-relative paths itself via
`project_manager.to_relative_path()`, the same division of responsibility
every other stage's extra-fields handling already uses.

Stage `segment` (Stage 4) is this pipeline's LAST baseline/scan stage
(PROJECT_SCHEMA_v2.md Section 9/10.2's stage order: `slam`, `level`,
`cleanup`, `segment`). As of Update 12, Stage 5 (Diff)'s
`get_diff_inputs()` DOES read from `segment`'s own output when it is
available: for each side independently, it prefers `segment.output` (the
combined `<name>_classified.ply`, carrying every input point plus a
`classification` field) over `cleanup.output`, falling back to
`cleanup.output` only when that side's `segment` stage has not completed
yet (PROJECT_SCHEMA_v2.md Section 11.3). This is safe because `segment`'s
combined output holds the exact same points as `cleanup.output` - only
`envelope.ply`, which `get_diff_inputs()` never reads, actually drops
points (interior clutter and machinery). So M3C2 always sees the full
cleaned cloud either way; what changes is whether the `classification`
field rides along into Stage 5's own output cloud. `segment` stays
optional - a diff never blocks waiting for it, and each side of a diff
can independently be in a `segment`-done or `segment`-not-done state.
Not yet confirmed: whether CloudCompare's `-SAVE_CLOUDS` actually
preserves a pre-existing custom scalar field like `classification`
alongside the new M3C2 distance field it computes, when the input cloud
already carries one - see Section 6's open item.

### 3.7a Giving Segment's Output Files Unique Names

New in Update 11. `segment_planes.py` used to write fixed, generic
filenames every run - `classified.ply`, `envelope.ply`, `unclassified.ply`,
`floor.ply`, `wall_1.ply`, and so on - regardless of which folder they
landed in. Each run's own OUTPUT FOLDER was already uniquely named
(Section 13.1's sequence numbering, reused as a folder name per Section
13.3), but the files inside it were not - opening several runs' results
together in a viewer (for example CloudCompare's DB tree) showed
multiple entries that all just read "classified.ply", with no way to
tell which run each one came from without checking the full folder path.

Fixed by prefixing every `.ply` this stage writes with its own output
folder's name (`output_dir.name` - falls back to `"segment"` if that
name is somehow empty, which should not happen in normal use) - so a
run in `compartment_04_segment_001/` now writes
`compartment_04_segment_001_classified.ply`,
`compartment_04_segment_001_envelope.ply`, and so on.
`manifest.json` keeps its fixed, unprefixed name deliberately -
`resolve_segment_output()` looks for it there by that exact name, and it
is a machine-read sidecar rather than something opened directly in a
viewer, so the collision this fix addresses does not apply to it. No
change was needed in `pipeline_core.py` or `pipeline_applet.py`'s actual
path-handling logic: both already read every file path back from
`manifest.json`'s own fields (`classified_cloud_file`, `envelope_cloud_file`,
each surface's `file`) rather than hardcoding any filename, so they pick
up the new names automatically. Only the applet's user-facing hint text
(Section 4.2b) needed updating, since it previously named the bare
filenames directly in its explanations.

### 3.8 Stage `classify`: Clustering Flagged Points Into Damage Sites

New in Update 6. `m3c2_classify.py` already thresholded the raw M3C2
distance field into a flagged/not-flagged split (the RMS-based magnitude
check, unchanged). It now adds a second pass on top: the flagged points
get clustered by 3D position (DBSCAN, the same algorithm `segment_planes.py`
already validated on this sensor/environment, or HDBSCAN for
variable-density flagged-point sets), and any flagged point with no
nearby flagged neighbors is treated as spatial noise - a sensor artifact
or registration error, not real damage - and rejected. Real damage now
has to be corroborated both statistically (the threshold) and spatially
(the cluster), rather than by the threshold alone. Surviving clusters
get a per-site aggregate: centroid, point count, bounding extent, mean
and max M3C2 magnitude.

`build_classify_command()` keeps its existing signature and adds
`cluster` (bool, default `True`), `cluster_method` (`"dbscan"` or
`"hdbscan"`), `cluster_eps` (DBSCAN only), `cluster_min_samples`, and
`min_cluster_size`. Unlike `segment`, `classify`'s output stays a single
file (Section 13.1's normal convention is untouched) - clustering does
not change what `output_ply` points at. What changes is a new sidecar
file, `<output>.clusters.json`, written next to it when clustering is
enabled, holding the per-cluster summary.

`resolve_classify_output(output_ply)` reads that sidecar back once a run
finishes and returns an `extra_fields` dict (`n_flagged`, `n_confirmed`,
`n_noise`, `clusters`) - absorbing the sidecar's content into
`project.json` per PROJECT_SCHEMA_v2.md Section 3.3, the same idea as
`resolve_segment_output()`, but simpler: since `classify`'s output path
never moves, this function only needs to return `extra_fields`, not
resolve an output path too. Returns `{}` (never raises) if no sidecar
exists - clustering was disabled, or the run failed before Step D.

Turning clustering off (`cluster=False`, `--no-cluster` on the script)
restores the exact original threshold-only behavior: no `cluster_id`
field on the output cloud, no sidecar file, no `extra_fields`. This
keeps every existing manual-mode and project-mode caller working
unchanged if it never opts into the new parameters.

### 3.8a Fixing the `cluster_id` Field's PLY Type

New in Update 10. A real bug, found on real hardware data: the
`cluster_id` field added to `classify`'s output cloud (Section 3.8) was
labelled `"i8"` when handed to `plyfile.PlyElement.describe()` - numpy
shorthand for a 64-bit integer. The PLY format itself has no standard
64-bit integer type; `plyfile`'s own type table tops out at `"i4"`
(32-bit). `PlyElement.describe()` raised `ValueError: field type 'i8'
not in [...]` the moment it reached that field - after clustering itself
had already finished and printed its full cluster list, which is why the
traceback showed up only at the save step, not during clustering.

Fixed by labelling the field `"i4"` instead, in both places it gets
added (the `--keep-all` branch, which keeps every point, and the default
branch, which keeps only flagged-and-confirmed points). `cluster_id`
only ever holds `-1` (noise) or a small cluster index - real runs so far
have topped out in the low hundreds - nowhere near int32's roughly 2.1
billion ceiling, so nothing is lost by narrowing from 64-bit to 32-bit.
The in-memory `cluster_id` array used for the clustering computation
itself (`np.int64`, Section 3.8) is unaffected - only the two places that
copy its values into the output PLY's structured array now cast to
`np.int32` first, matching the corrected field width.

### 3.9 Fixing a Long-Running Stage's Frozen-Looking Console Output

New in Update 13. A real bug found on a real Windows machine: a KISS-ICP
SLAM run against a real, large capture showed one or two startup lines
(including a harmless `SyntaxWarning` from inside the installed
`kiss_icp` package itself, unrelated to this project's own code), then
nothing - the applet's log stayed blank and the run looked frozen,
"running", for the whole time it actually took KISS-ICP to process the
capture.

`run_streaming()` (Section 3, this file's own subprocess runner - every
stage that launches `sys.executable <script>.py`, meaning SLAM, Level,
Segment, Classify, Surface, Export, and the decode step, not just
KISS-ICP specifically) reads a child process's combined stdout/stderr
line by line and forwards each line to the applet's log callback as it
arrives. This depends on the CHILD process actually flushing its own
output as it prints, not batching it up internally. CPython fully
block-buffers a script's stdout whenever that stdout isn't a real,
interactive terminal - true here, since `run_streaming()` reads it
through a pipe. A short-running stage (Level, Classify on a modest
cloud) finishes fast enough that this is invisible - the whole buffer
flushes at exit before a user would notice. A genuinely long-running
stage does not: its `print()` calls pile up in the CHILD's own internal
buffer and never reach `run_streaming()`'s `for line in
process.stdout:` loop until that buffer fills (rare, for short text
lines) or the process exits - by which point the run is already over,
so the log shows nothing the entire time it mattered.

`bufsize=1` on the `subprocess.Popen(...)` call does NOT fix this - that
setting controls how THIS (parent) process reads the pipe once data
arrives, not whether the CHILD flushes its own writes in the first
place, which is the actual bottleneck.

Fixed by passing an explicit `env` to `Popen()`: a copy of this
process's own environment (`os.environ`, so `PATH` and everything else
a child needs to find `python.exe`/`CloudCompare`/etc. is preserved),
with `PYTHONUNBUFFERED` set to `"1"`. This is a standard CPython
environment variable that disables output buffering for the CHILD
process specifically. Setting it is harmless for a non-Python child
(CloudCompare) - a non-Python program has no reason to look at it.
Confirmed with a real timing test (a script that prints five lines with
a short delay between each): without this fix, all five lines arrive
simultaneously, at the moment the process exits; with it, each line
arrives live, as the child actually prints it, in an environment with no
`PYTHONUNBUFFERED` of its own already set - matching a real Windows
machine, where this variable is not set by default.

This bug never changed what any stage actually produced - a run that
finished successfully before this fix wrote the exact same output file
it does now. The only difference is whether the console showed progress
while a long stage ran, or looked frozen until it either finished or
failed. See PROJECT_SCHEMA_v2.md (no schema change - the project file
format itself is unaffected) and `troubleshooting_sheet.md` Section 17.

## 4. `pipeline_applet.py`

### 4.1 Top-Level Controls

The main window has four project buttons: **New Project**, **Open
Project**, **New Scan**, **New Diff**.

- **New Project**: asks for a folder location, a compartment name, and a
  raw source file. Calls `project_manager.create_project(...)`. This
  creates the project's baseline pipeline.
- **Open Project**: asks for a project folder. Calls
  `project_manager.load_project(...)`.
- **New Scan**: asks for a label and a raw source file. Calls
  `project_manager.add_scan(...)`. Requires an open project.
- **New Diff**: asks for a label, a reference (the baseline or an
  existing scan), and a comparison (an existing scan). Calls
  `project_manager.add_diff(...)`. Requires an open project with at
  least one scan.

The main window has two pipeline selectors, because Version 2 can have
more than one pipeline in progress at once:

- **Source pipeline**: the baseline, or one scan. Stage 1, 2, 3, and 4
  buttons act on whichever one is picked here.
- **Diff pipeline**: one diff. Stage 5, 6, 7, and 8 buttons act on
  whichever one is picked here.

A status label shows each selected pipeline's next stage (via
`project_manager.find_next_stage`), or "done" if every stage in that
pipeline is complete, and now also shows "decoded source set" when the
active Source pipeline's `raw` object has a `decoded_path` recorded
(PROJECT_SCHEMA_v2.md Section 12.1).

A **Set Decoded Source...** button sits next to the Source pipeline
selector. It asks for a folder, then calls
`project_manager.set_decoded_raw_path(active_source_pipeline, folder)`
on whichever pipeline the selector currently has picked. This closes a
real gap: the Source pipeline selector only chooses WHICH pipeline
(baseline or a scan) - it had no way to also choose which raw variant
(the original import, or an already-decoded copy) that pipeline's Stage
1 should use. The SLAM dialog's own auto-convert flow (Section 4.4) only
fires when a folder still looks like it needs decoding (no
`PointCloud2` topic yet); it has nothing to offer once a decoded bag
already exists - for example, one produced by running
`decode_raw_packets.py` directly instead of through this applet. Before
this button, the only way to point a pipeline at an already-existing
decoded bag was to open the SLAM dialog and check "Use manual file
selection instead" by hand, every time the dialog reopened.

### 4.2 Stage Dialog Changes

Each stage dialog gets a header panel, shown only when the matching
pipeline selector has a pipeline picked:

```
Pipeline: compartment_04 / Scan: post-storm_2026-09-01   Input: scans/post-storm_2026-09-01/04_cleanup/....ply (auto)
[ ] Use manual file selection instead
```

Behavior:

- When the checkbox is not checked, the dialog pre-fills the input field
  with `project_manager.get_input_for_stage(pipeline, stage_name)`. The
  dialog disables manual browsing for that field.
- When the checkbox is checked, the dialog behaves as it does in manual
  mode. The user browses to a file by hand.
- The output-name field is always editable. The output-name field is
  pre-filled with `project_manager.get_output_path(...)` when a pipeline
  is picked, or left blank in manual mode.

Stage 5 (Diff) and Stage 8 (Export) each need TWO auto-filled input
fields, not one (Diff: baseline and comparison; Export: baseline and
change), so these two dialogs use their own header, built the same way
but filling two fields from `get_diff_inputs()` / from
`get_baseline_cleanup_output()` plus `get_input_for_stage()`.

New in Update 9. Stage 5 (Diff)'s **Generate Params File...** button
also checks whether a project pipeline is active (same `active_pipeline`
test the dialog's own `build()` already uses: a pipeline is picked, and
"Use manual file selection instead" is unchecked). When it is, the
button calls `project_manager.get_output_path(pipeline, "diff", ".txt")`
itself and writes the file there directly - no save-location dialog. The
`.txt` extension sequences independently of the diff stage's own `.ply`
output in that same folder (`get_output_path` only counts existing files
matching the extension it was asked for), so the two never collide. The
save-location dialog (`filedialog.asksaveasfilename`) still appears in
manual mode, or with the checkbox checked - there is no project folder
to place the file in automatically in either of those cases. See
PROJECT_SCHEMA_v2.md Section 13.4.

Stage 3 (Cleanup) also gets a **Use Project Baseline** button, shown only
when the active source pipeline is a scan. This fills the "Align to"
field with `project_manager.get_baseline_cleanup_output(project)` - the
usual choice when cleaning up a comparison scan, since a later "vs.
baseline" diff needs this scan's cleanup step to have actually aligned
to the real baseline.

Stage 2 (Level), Stage 3 (Cleanup), Stage 6 (Classify), and Stage 7
(Surface) each also get a dropdown, shown only when their pipeline's
previous stage has more than one output on disk: "pick a specific past
output of the previous stage", backed by
`project_manager.list_stage_outputs`. Picking an entry here also checks
"Use manual file selection instead" for this dialog, the same way the
existing checkbox already works, since picking a non-default input is a
deliberate one-off override - the run this produces will not be
recorded into `project.json`, same trade-off the checkbox has always
had. This is the dropdown `list_stage_outputs`'s own docstring already
described as its intended purpose; it previously had no caller anywhere
in the applet.

Stage 1's Backend field and Stage 7's Method field are radio buttons
(`StageDialog.add_radio_choice`), not the preset-dropdown-plus-free-text
pattern every other choice field in this applet uses
(`add_preset_selector`). The difference: Backend ("ouster"/"kiss_icp")
and Method ("poisson"/"ball_pivoting") are genuinely closed, two-value
choices where nothing outside that set is meaningful - not a continuous
value like voxel size or a distance threshold, where the preset options
are just common starting points and a user might legitimately want a
value between or beyond them. A closed choice backed by a free-text
field could be left blank or typo'd with no feedback - this is exactly
what caused a real bug earlier (a blank Backend field silently broke
the raw-packet check). Radio buttons make an invalid or blank value
structurally impossible instead of something to keep guarding against.

### 4.2a Showing Only the Fields for the Picked Backend

New in Update 4. Before this change, the SLAM dialog showed every
field for BOTH backends at once, each one labeled `[Ouster CLI]` or
`[KISS-ICP]` to show which one it applied to. This used more space
than needed and made the dialog look more complex than the actual
choice in front of the user at any one time - only one backend's
fields ever matter for a given run.

`StageDialog` gets two new methods, `begin_section()` and
`end_section()`. A call to `begin_section()` opens a child area that
holds a group of fields; every field added between it and the matching
`end_section()` call belongs to that group. The whole group can then be
shown or hidden together as one unit.

The SLAM dialog wraps Ouster CLI's own fields (voxel size, visualize)
in one group, and KISS-ICP's own fields (script, config, voxel size
override, dataloader, topic) in a second group. A small piece of code
watches the Backend radio buttons: picking "Ouster CLI" shows the
first group and hides the second; picking "KISS-ICP" does the reverse.
Only one group is ever visible at a time.

### 4.4a KISS-ICP Voxel Size in the SLAM Dialog

New in Update 4. The KISS-ICP field group (Section 4.2a) shows a small
line of text next to the Config field, updated live as the Config path
changes: the voxel size read from that config file (Section 3.6), or a
note that none could be read.

Below that sits a new "Voxel size override" field, left blank by
default. A value here overrides the config's own voxel size for this
one run, the same way Ouster CLI's Backend already has its own voxel
size field. Leaving it blank uses the config's own value.

### 4.2b Stage 4 (Segment) Dialog

New in Update 5. A new dialog, opened from a new "4. Segment" button
next to Stage 3's own button, follows the header/input-picker pattern
Section 4.2 already describes, with one difference: its Output field is
a folder, not a file (`segment_planes.py` writes several files together
- Section 3.7), so it uses a plain `filedialog.askdirectory()` folder
picker instead of `add_save_field()`. The default folder name reuses
Stage 1-8's own sequence-numbering convention (PROJECT_SCHEMA_v2.md
Section 13.1) applied to a folder instead of a file - counted directly
from existing subfolders in this pipeline's `05_segment/` directory, not
through `project_manager.get_output_path()` itself (that function's own
sequence counter globs for FILES with a given extension, which would
never see a past run's own subfolder and would keep returning the same
name forever).

The dialog exposes every tuning knob `segment_planes.py`'s own CLI
already has: the RANSAC distance threshold, max planes, min plane size,
horizontal threshold, and max horizontal Z span (Section 3.7's
"classify"-not-"floor-only" extension of Stage 2/Level's own approach),
plus the stray-point cluster filter and split-plane merge, both on by
default, each with its own tolerance fields, plus a "write separate
surfaces" checkbox (Section 3.7), off by default, controlling whether
`<name>_floor.ply`/`<name>_wall_N.ply`/`<name>_unclassified.ply` also
get written alongside `<name>_classified.ply` (`<name>` being the
output folder's own name - Section 3.7a).

Once a run finishes, the report is built by calling
`resolve_segment_output()` (Section 3.7) against the output folder,
converting whatever paths it returns to project-relative
(`project_manager.to_relative_path()`) before recording them - the same
`resolve_state` mechanism Stage 3 (Cleanup) and Stage 5 (Diff) already
use for a report that needs to inspect what a run actually produced
before recording it (Section 4.3 below).

### 4.2c Stage 6 (Classify) Dialog: Cluster Controls

New in Update 6. The existing Classify dialog (threshold, RMS lookup,
keep-all) gains a "Cluster flagged points into damage sites" checkbox,
on by default, plus the same knobs `m3c2_classify.py`'s CLI exposes:
method (a radio choice between DBSCAN and HDBSCAN, following the
`add_radio_choice()` convention Section 4.2a's rationale describes -
genuinely discrete options, not a continuous value with suggested
presets), cluster gap tolerance (DBSCAN only), cluster density, and
minimum damage-site size.

Once a run finishes, the report calls `resolve_classify_output()`
(Section 3.8) against the (already-known) output path, and lists what it
finds: flagged/confirmed/rejected-as-noise counts and each surviving
cluster's centroid, point count, and max magnitude. This reuses the same
`resolve_state` mechanism as the Segment dialog (Section 4.2b), but
simpler - since `classify`'s output path doesn't move, `resolved_state`
only ever needs to carry `extra_fields`, never a resolved output path.
If clustering is off, or the run produced no sidecar file, the report
says so plainly instead of showing stale or fabricated cluster numbers.

### 4.3 Report Popup and Project-Mode Recording

The report popup shown after each stage run is built BEFORE the applet
calls `finish_stage`, not after, so a stage whose real output filename is
only known once CloudCompare has actually run (Stage 3 and Stage 5) can
resolve that real filename first, then record the real filename - not
the name that was merely asked for. The report popup gets one added
line, in project mode: "Saved to project: `<relative path>`".

### 4.4 Raw Packet Detection in the SLAM Dialog

Stage 1 (SLAM)'s Source field checks a picked ROS2 bag folder for raw
Ouster packets, through `pipeline_core.inspect_rosbag_topics`. This check
runs two ways: on its own, right after a folder is picked with the
Browse button, and by hand, with a "Check Source for Raw Packets..."
button next to the field, for a path typed in directly.

The check does nothing in these cases:

- The folder is not a ROS2 bag.
- The bag already has a `PointCloud2` topic.

The check does NOT skip itself based on Backend. Ouster CLI can already
read raw packets directly and does not strictly need a decoded copy, but
the dialog still offers one - a decoded copy stays useful for comparing
backends side by side, for a future SLAM method, or for use outside this
pipeline through a plain `ros2 bag play`. An earlier version of this
check skipped itself whenever Backend was Ouster CLI, and also treated a
blank Backend field the same way with no message shown at all - both
were bugs, not intended behavior, and are fixed.

If the bag holds raw packets and has no `PointCloud2` topic, the dialog
asks the user to confirm a conversion, regardless of Backend. On yes, it
runs `decode_raw_packets.py` in the background. Once the run finishes
with no error, it records the new decoded bag folder (named `<bag folder
name>_decoded`, next to the original) as this pipeline's
`raw.decoded_path` (PROJECT_SCHEMA_v2.md Section 12.1) when a project
pipeline is set up for this dialog in its normal auto-resolve state, or
fills the Source field directly and switches to manual file selection
otherwise (no pipeline set up, or the user had already checked "Use
manual file selection instead"). If a decoded copy already exists at
that path, the dialog skips the run and applies the same logic to the
existing copy instead of running the conversion again.

An earlier version of this step only filled the Source field and told
the user "Source has been updated to use it" - true for the field's
display, but not for what a project-mode Run with auto-resolve on
actually did: `build_slam_command()` re-resolves its input from the
pipeline whenever one is set, ignoring what the field displays, so the
decoded bag was silently NOT used. Recording `raw.decoded_path` on the
pipeline (rather than only filling the field) fixes this while keeping
the run tracked in `project.json`, same as any other auto-resolved run.

This step is wired into the SLAM dialog's Source field only. It is not
part of `create_project`'s or `add_scan`'s own raw-source import step.
Section 6 lists extending it to those two entry points as an open item.

### 4.5 Stage Locking

Not yet implemented. Every stage button stays enabled regardless of
whether its pipeline's previous stage has completed, in both manual mode
and project mode. A user can already see which stage is next, per
pipeline, from the status label (Section 4.1) - Section 6 lists actually
disabling a not-yet-reachable stage's button as an open item.

Note: this subsection was 4.4 before this update. It moved to 4.5 to
make room for Section 4.4 (Raw Packet Detection in the SLAM Dialog).

## 5. Migration Steps (Historical Record)

This section is the original Version 1 plan. Every step below is now
complete.

1. ~~Write `project_manager.py`. Test it alone, with no GUI and no
   `pipeline_core.py` changes.~~ Done. Rewritten for schema v2; see
   `test_project_manager.py`.
2. ~~Add the optional project parameter to the Stage 1 (SLAM) and Stage 2
   (Level) functions in `pipeline_core.py`.~~ Done.
3. ~~Add the "New Project" and "Open Project" buttons to
   `pipeline_applet.py`. Wire them to Stage 1 and Stage 2 dialogs only,
   for a first working slice.~~ Done, and since extended to every stage.
4. ~~Add the project parameter to Stage 3 (Cleanup) and Stage 5 (Diff).
   Handle the RMS hand-off.~~ Done.
5. ~~Add the project parameter to Stage 6, 7, and 8.~~ Done. This step
   also added Stage 7 (Surface) as a wired stage for the first time -
   Version 1 had deliberately left it out.
6. **Add the stage-locking rule, once all eight stages support project
   mode.** Not done - see Section 4.4 and Section 6.
7. **Add the `promote_baseline` connection, once the compartment
   registry exists.** Not done - the compartment registry does not exist
   yet (PROJECT_SCHEMA_v2.md Section 3.1). See Section 6.

## 6. Open Items for a Later Phase

- Confirming CloudCompare's `-SAVE_CLOUDS` preserves a pre-existing
  custom scalar field (`classification`, written by `segment`) alongside
  the new M3C2 distance field it computes, when Stage 5 (Diff) is given a
  side's `segment.output` as input (implemented in Update 12 -
  PROJECT_SCHEMA_v2.md Section 11.3, Section 3.4 and Section 3.7 above).
  Not confirmed either way yet - if CloudCompare drops the
  `classification` field during the M3C2 run, a diff whose input came
  from `segment.output` would still work correctly for the M3C2 result
  itself, just without the classification field carried through onto
  Stage 5's own output cloud.
- Connecting `promote_baseline` to the compartment registry system, once
  that system exists. Right now `promote_baseline` only updates a
  project's own `baseline` object fields (PROJECT_SCHEMA_v2.md Section
  9), and `pipeline_core.py`'s separate `baseline_registry.json`
  mechanism keeps serving manual mode in the meantime (see
  `pipeline_core.py`'s own module docstring on this split).
- Stage locking (Section 4.5): disabling a stage's button in the applet
  when its pipeline's previous stage has not completed yet, per pipeline
  and per stage, now that every stage supports project mode.
- Extending the raw-packet check (Section 4.4) to `create_project`'s and
  `add_scan`'s own raw-source import step, so a raw bag gets flagged and
  converted at import time too, not only when picked as a Stage 1
  Source.
- `decode_raw_packets.py` opens a Reader over the input bag once now
  (fixed - it used to open a second one just to re-read the first
  message for its diagnostic hex dump). If a future bag needs more than
  one pass for a different reason, keep that in mind before adding one
  back without a specific need.
- Deciding whether AprilTag or ArUco fiducial detection writes its
  result into `project.json`, for example as a `compartment_detected`
  field on a `slam` stage entry.
- Deciding whether WSL2-dispatched backends write their subprocess
  output path back through the same `complete_stage` call, or need a
  separate path-translation step first.
- Keeping `about_content.json`'s stage descriptions and this document in
  step with any future schema or code change - both were updated
  together with this Version 2 rewrite, but nothing enforces that they
  stay in sync automatically.
- Running Stage 5 (Diff) bidirectionally, to close the blind spot
  described in PROJECT_SCHEMA_v2.md Section 11.3a: `build_diff_command()`
  currently loads `reference`/baseline as the compared/core-points cloud,
  which reliably catches material LOSS (a hole measured from a baseline
  point that still exists) but can under-report geometry that only
  EXISTS in the newer `comparison` scan (added debris, an outward bulge),
  since there is no baseline point there to anchor a core point on. A
  bidirectional run would need:
  - Two separate CloudCompare `-M3C2` subprocess runs instead of one -
    the current baseline-first order, plus a second run with the load
    order reversed (`comparison` first). Same params file works for
    both; only the `-O` order changes.
  - Watching BOTH clouds' folders for a new file afterward (Section
    11.3a: each run's result lands next to whichever cloud it loaded
    first), not just one.
  - A merge step to combine the two result clouds into one before
    handing off to Stage 6 (Classify) - concatenating both PLYs (same
    M3C2 distance scalar field in each, so this is a straightforward
    row-stack, not a true per-point correspondence problem) via either
    CloudCompare's own `-MERGE_CLOUDS` or a small new Python script using
    `plyfile`, matching the pattern `segment_planes.py` and
    `m3c2_classify.py` already use for reading/writing PLY data.
  - No change needed in `m3c2_classify.py` itself - it already thresholds
    and spatially clusters (DBSCAN/HDBSCAN) whatever single PLY it's
    given, so nearby flagged points from either direction naturally group
    into the same damage-site cluster without needing exact point-to-point
    matching between the two directions.
  - Roughly double the CloudCompare compute time per diff (two full M3C2
    runs instead of one), plus the merge step - worth confirming this
    cost is acceptable before building it, since a large cleaned cloud's
    M3C2 run is already the slowest step in the pipeline.
  - A Stage 5 dialog option to turn this on per-run (recommended default
    off, since the baseline-anchored single run is cheaper and already
    catches the more likely damage type for this project - material
    loss), not a silent behavior change.
  Decided against for now (see PROJECT_SCHEMA_v2.md Section 16): kept
  Stage 5 baseline-anchored, single-direction, and accepted the
  added-geometry blind spot as a known trade-off rather than take on this
  extra cost right away.
