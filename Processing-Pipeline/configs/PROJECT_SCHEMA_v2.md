# Project File Schema

**Version 2.** This version replaces Version 1. Section 16 describes what changed and why.

## 1. Purpose

This document describes the project file. The project file is a JSON file. The project file records the state of one pipeline run for one compartment. The project file replaces manual file selection between stages.

## 2. Scope

This document applies to the project-based workflow. This workflow adds a project layer above the existing pipeline stages. This document does not change the pipeline stages. This document does not change the algorithms in the pipeline stages.

## 3. Relationship to Existing Files

### 3.1 baseline_registry.json

The project file replaces the function of `baseline_registry.json`. A project holds one baseline. A user can promote a project's baseline output to become the active baseline for a compartment. The compartment registry stores the promoted baseline. The compartment registry is a separate system. Section 9 gives more detail.

### 3.2 RMS sidecar files

The RMS sidecar file stays in use. Stage 3 (Cleanup) writes the RMS sidecar file. Stage 3 writes the RMS sidecar file next to the Stage 3 output file. The project file copies the RMS value from the sidecar file into the project file. Stage 5 (Diff) reads the RMS value from the project file. Stage 5 does not read the RMS value from the sidecar file directly.

### 3.3 manifest.json pattern

Some scripts write a `manifest.json` file. Example: `segment_planes.py`. Other scripts write a similarly-purposed sidecar file under a different name. Example: `m3c2_classify.py` writes a `<output>.clusters.json` file next to its output, holding the Step D per-cluster summary, when clustering is enabled. The project file absorbs this pattern regardless of the sidecar file's name. Each stage entry in the project file stores the same kind of record that a manifest or sidecar file stores. New scripts do not need to write a separate manifest file. New scripts write their output record into the project file instead.

## 4. Why Version 2: Baseline vs. Scans vs. Diffs

Version 1 assumed one linear pipeline per project: SLAM, Level, Cleanup, Diff, Classify, Export, in that order, each stage running once. This does not match the real workflow.

A compartment is scanned once to create a baseline. A compartment is scanned again, many times, over the life of the compartment, to check for new damage. Each of these later scans is a **comparison scan**. Each comparison scan needs its own SLAM, Level, and Cleanup run - it is not the same data as the baseline.

A comparison scan can be compared against more than one reference. A comparison scan can be compared against the baseline, to show total change since the baseline was recorded. A comparison scan can also be compared against an earlier comparison scan, to show what changed only since the last check. These are two different M3C2 runs (Diff), producing two different Classify and Export results, from the same comparison scan.

Version 2 splits the project file into three parts to represent this:

- **`baseline`**: one SLAM/Level/Cleanup pipeline. A project has exactly one.
- **`scans`**: zero or more comparison scans, each its own SLAM/Level/Cleanup pipeline.
- **`diffs`**: zero or more Diff/Classify/Export pipelines. Each diff names which two things it compares.

## 5. Folder Structure

Each project has one root folder. The user selects the location of the root folder when the user creates the project. The root folder holds a `baseline` subfolder, a `scans` subfolder, and a `diffs` subfolder.

```
<project_root>/
  project.json
  baseline/
    01_raw/
    02_slam/
    03_level/
    04_cleanup/
    05_segment/
  scans/
    <scan_id>/
      01_raw/
      02_slam/
      03_level/
      04_cleanup/
      05_segment/
    <scan_id>/
      01_raw/
      02_slam/
      03_level/
      04_cleanup/
      05_segment/
  diffs/
    <diff_id>/
      05_diff/
      06_classify/
      07_surface/
      08_export/
```

Note: `baseline/05_segment/` and `diffs/<diff_id>/05_diff/` share the number `05` in their folder name. This is not a collision - they sit in different subtrees (`baseline/` or `scans/<scan_id>/`, versus `diffs/<diff_id>/`), so their paths never overlap.

Rule: each stage writes its output only into its own subfolder, inside its own baseline/scan/diff folder. Rule: a stage does not write into another stage's subfolder, and a scan or diff does not write into another scan or diff's folder. This rule keeps the CloudCompare output-detection step reliable, because the detection step scans one small folder, not a shared folder.

## 6. Project File Location

The project file has the name `project.json`. The project file sits in the project root folder. One project has exactly one `project.json` file.

## 7. Top-Level Fields

| Field | Type | Description |
|---|---|---|
| `project_id` | string | Unique ID for the project. Format: `<compartment>_<date>`. Example: `compartment_04_2026-08-12`. |
| `compartment` | string | Name of the compartment. This name must match a name in the compartment registry, if the compartment registry entry exists. |
| `created` | string | Creation timestamp, ISO 8601 format. |
| `updated` | string | Timestamp of the last write to this file, ISO 8601 format. |
| `schema_version` | integer | Version number of this schema. Current value: `2`. |
| `baseline` | object | The baseline pipeline and its promotion status. See Section 9. |
| `scans` | object | Comparison scan pipelines, one entry per scan. See Section 10. |
| `diffs` | object | Diff/Classify/Export pipelines, one entry per diff. See Section 11. |

There is no top-level `current_stage` field in Version 2. Version 1 had one, because Version 1 had one linear pipeline. Version 2 has many independent pipelines (the baseline, each scan, each diff), so there is no single "next stage" for the whole project. Section 8.3 describes how to find the next stage for one specific pipeline.

## 8. Common Structure: the `stages` Object

The baseline, each scan, and each diff all hold a `stages` object, in the same shape. This section describes that shared shape once. Sections 9, 10, and 11 describe which stage names appear in each case.

Each stage entry can hold these fields:

| Field | Type | Description |
|---|---|---|
| `status` | string | One of: `not_started`, `running`, `complete`, `failed`. |
| `output` | string | Relative path to the stage output file, from the project root. |
| `params` | object | The parameters used for this stage run. |
| `started` | string | Start timestamp, ISO 8601 format. |
| `completed` | string | Completion timestamp, ISO 8601 format. Present only if `status` is `complete`. |
| `rerun_count` | integer | Number of times this stage has completed and then run again. Starts at `0`. Only increases when a stage that already has `status: complete` runs again - a retry after `status: failed` does not increase this, since that run never completed the first time. |
| `error` | string | The error message from the most recent failed run. Present only if `status` is `failed`. |
| `log_path` | string | Relative path to the saved run log, if the applet saves logs. |

Stage-specific fields:

- **`cleanup`**: adds `icp_rms` (number) and `sidecar` (string, relative path to the RMS sidecar file).
- **`diff`**: adds `m3c2_params_file` (string, relative path), `registration_error_used` (number, copied from the source cleanup stage's `icp_rms` at the time Stage 5 ran), `reference_source_stage` (string, either `"segment"` or `"cleanup"` - which stage actually supplied the `reference` side's cloud, per Section 11.3), and `comparison_source_stage` (string, same values, for the `comparison` side).
- **`slam`**: adds `source_type` (string, one of `pcap`, `osf`, `ros1_bag`, `ros2_bag`) and `point_spacing` (number, if computed).
- **`segment`**: adds `classification_ids` (object, mapping a small integer written into the output cloud's `classification` field to a surface name - `0` always means `unclassified`, other keys map to `floor`, `ceiling`, or `wall_N`), `envelope_output` (string, relative path to the envelope-only cloud - floor+ceiling+walls combined, no interior/clutter - present only if at least one point matched a detected surface), and `surfaces` (array of objects, one per detected surface: `name`, `file`, `point_count`, `normal` (3 numbers), `z_min`, `z_max` - the same record `segment_planes.py`'s own `manifest.json` holds, absorbed here per Section 3.3 instead of needing a separate file). `file` is a relative path to that one surface's own cloud, or `null` when `params.write_separate_surfaces` was `false` (the default - see Section 13.3) - every surface is always listed by name/count/normal/Z-range either way, only its own individual `.ply` file is conditional.
- **`classify`**: when clustering is enabled (the default - see `params.cluster`), adds `n_flagged` (integer, points that passed the magnitude threshold), `n_confirmed` (integer, flagged points that also survived spatial clustering - the count actually written to the output cloud unless `params.keep_all` is `true`), `n_noise` (integer, flagged points rejected as spatially isolated - the second false-positive filter), and `clusters` (array of objects, one per surviving damage site: `cluster_id`, `point_count`, `centroid` (3 numbers), `extent` (3 numbers, bounding box size), `mean_magnitude`, `max_magnitude` - the same record `m3c2_classify.py`'s own `<output>.clusters.json` sidecar holds, absorbed here per Section 3.3 instead of needing a separate file). Absent entirely when `params.cluster` is `false` (clustering disabled, original threshold-only behavior).

### 8.1 Rule: Stage Advancement Within One Pipeline

Within one `stages` object (the baseline's, one scan's, or one diff's), a stage is ready to run once the stage before it, in that pipeline's own stage order, has `status: complete`. A failed run does not block a retry of the same stage. A failed run does not let a later stage run early.

### 8.2 Rule: Re-running a Stage

A user can re-run a stage that already has `status: complete`. When this happens, the applet overwrites that stage's entry. The applet increases `rerun_count` by `1`. The applet does not create a second, separate record for the old run. This project file does not support branching pipeline histories in this version.

### 8.3 Finding the Next Stage for One Pipeline

To find the next stage to run for the baseline, a scan, or a diff: check that pipeline's own stage order (Section 9, 10, or 11). Find the first stage, in order, whose `status` is not `complete`. That is the next stage. If every stage is `complete`, that pipeline has finished.

## 9. The `baseline` Object

| Field | Type | Description |
|---|---|---|
| `raw` | object | The imported raw source. See Section 12. |
| `stages` | object | Stage entries for `slam`, `level`, `cleanup`, `segment`, in that order. See Section 8. |
| `is_baseline_candidate` | boolean | `true` if this project's baseline output is eligible to become a registered baseline. |
| `promoted` | boolean | `true` if a user has promoted this baseline to the compartment registry. |
| `promoted_stage` | string | The stage whose output was promoted. Example: `cleanup`. Present only if `promoted` is `true`. |
| `promoted_timestamp` | string | ISO 8601 timestamp of promotion. Present only if `promoted` is `true`. |

A project has exactly one `baseline` object. A project does not require promotion. Most projects run comparisons against an already-registered baseline and never touch the `promoted` field. The compartment registry is the system of record for approved baselines. The compartment registry is out of scope for this document.

## 10. The `scans` Object

The `scans` object holds zero or more comparison scans. Each key is a **scan ID**. Each value is a scan entry.

### 10.1 Scan ID Format

```
<label>_<date>
```

The user supplies `<label>` when importing the scan's raw source. `<date>` is added automatically. Example: `post-storm_2026-09-01`.

### 10.2 Scan Entry Fields

| Field | Type | Description |
|---|---|---|
| `raw` | object | The imported raw source. See Section 12. |
| `stages` | object | Stage entries for `slam`, `level`, `cleanup`, `segment`, in that order. See Section 8. |

A scan does not have its own Diff, Classify, or Export stages. Those live in the `diffs` object (Section 11), because one scan can be compared against more than one reference.

## 11. The `diffs` Object

The `diffs` object holds zero or more diff pipelines. Each key is a **diff ID**. Each value is a diff entry.

### 11.1 Diff ID Format

```
<label>_<date>
```

Same format as a scan ID (Section 10.1). The user supplies the label when setting up the diff run.

### 11.2 Diff Entry Fields

| Field | Type | Description |
|---|---|---|
| `reference` | string | What this diff treats as the baseline side of the comparison. Either the literal string `"baseline"`, or a scan ID from the `scans` object. |
| `comparison` | string | The scan ID being compared against `reference`. Must exist in the `scans` object. |
| `stages` | object | Stage entries for `diff`, `classify`, `surface`, `export`, in that order. See Section 8. |

### 11.3 Rule: What a Diff Reads From

A diff's `reference` and `comparison` fields point at a baseline or scan entry. Stage 5 (Diff)'s two input clouds each come from whichever entry `reference` and `comparison` point to - not from a separate copy. If `reference` is `"baseline"`, its stages come from the top-level `baseline` object. If `reference` is a scan ID, its stages come from that entry in `scans`. The same rule applies independently to `comparison`.

For each side, Stage 5 (Diff) prefers that side's `segment.output` over its `cleanup.output`, and records which one it used:

- If that side's `segment` stage has `status: complete`, Stage 5 reads `segment.output` (the combined `<name>_classified.ply`, Section 13.3) for that side, and records `"segment"` in `reference_source_stage` or `comparison_source_stage` (Section 8), whichever side this is.
- Otherwise, Stage 5 falls back to that side's `cleanup.output`, and records `"cleanup"` instead.

Each side is resolved on its own - one diff can read `segment.output` for `reference` while falling back to `cleanup.output` for `comparison`, if only one side has run `segment` so far. A diff never blocks waiting for `segment` to run; `segment` stays an OPTIONAL stage (Section 9, Section 10.2).

This preference costs nothing in point coverage either way: `segment`'s combined-cloud output (`<name>_classified.ply`) holds the exact same points as `cleanup.output` - every input point, unchanged - plus an added `classification` field. (Only `<name>_envelope.ply`, which Stage 5 never reads, actually drops points - interior clutter and machinery, keeping just floor/ceiling/wall.) So the M3C2 result itself does not change based on which stage supplied the cloud; what changes is whether the `classification` field rides along into Stage 5's own output cloud, for use in later stages or in CloudCompare. `registration_error_used` is still sourced from that side's `cleanup.icp_rms` regardless of which stage supplied the cloud - `segment` does not record its own registration error.

### 11.3a Rule: Which Cloud Carries the M3C2 Result

CloudCompare's M3C2 command loads two clouds and treats the first one loaded as the "compared" cloud. Only the compared cloud receives the M3C2 distance result (its points become the M3C2 "core points" - the actual locations distance gets measured at), and only the compared cloud's file gets resaved by CloudCompare - into that cloud's OWN input folder, not the diff's output folder.

`pipeline_core.build_diff_command()` loads the `reference`/baseline cloud first, a deliberate choice (see Section 16): it keeps the M3C2 result anchored to the same fixed set of query locations - the baseline's own points - across every diff run against that baseline. This reliably catches material LOSS: a hole or missing chunk shows up as a large measured gap from a baseline point that still exists, even where the comparison scan itself has few or no points left in that spot.

The trade-off, kept deliberately for now: geometry that only EXISTS in the newer `comparison` scan (added debris, an outward bulge) has no baseline point to anchor a core point on, so it can be under-reported. Running M3C2 a second time with the load order reversed (`comparison` as the compared/core-points cloud) and merging both result sets would close that gap - not implemented yet. See PROJECT_INTEGRATION_PLAN.md Section 6 for what that would involve.

### 11.4 Example: Comparing One Scan Two Ways

A comparison scan can appear as `comparison` in more than one diff entry, to compare it against more than one reference:

```
"diffs": {
  "post-storm_2026-09-01_vs_baseline": {
    "reference": "baseline",
    "comparison": "post-storm_2026-09-01",
    "stages": { ... }
  },
  "post-storm_2026-09-01_vs_previous": {
    "reference": "routine-check_2026-08-01",
    "comparison": "post-storm_2026-09-01",
    "stages": { ... }
  }
}
```

The first entry shows total change since the baseline. The second entry shows what changed only since the previous scan.

## 12. The `raw` Object

The baseline and each scan hold a `raw` object, recording the imported raw source.

| Field | Type | Description |
|---|---|---|
| `path` | string | Relative path to the imported source, from the project root. |
| `source_type` | string | One of `pcap`, `osf`, `ros1_bag`, `ros2_bag`. |
| `import_method` | string | One of `copy`, `move`, `link`, recording how the source was brought into the project folder. |
| `decoded_path` | string | Optional. Relative path to a decoded copy of `path`, from the project root. Present only after a user decodes the raw source. See Section 12.1. |

### 12.1 The `decoded_path` Field

Some raw sources hold raw sensor packets, not decoded points. Some tools need decoded points as input, not raw packets. `decode_raw_packets.py` makes a decoded copy of a raw source.

The `path` field does not change when a user makes a decoded copy. The `path` field always keeps the record of the true original import.

`decoded_path` records the location of the decoded copy. When `decoded_path` is present, Stage 1 (SLAM) uses the file at `decoded_path` as its input. Stage 1 does not use the file at `path` in this case. This field is optional. This field is present only after a user decodes the raw source for this baseline or scan.

(For clarity across this document: the baseline/scan pipeline runs Stage 1 (SLAM), Stage 2 (Level), Stage 3 (Cleanup), and Stage 4 (Segment), in that order. The diff pipeline runs Stage 5 (Diff), Stage 6 (Classify), Stage 7 (Surface), and Stage 8 (Export), in that order. These are the same eight stages the schema's `stages` objects track under their internal names `slam`/`level`/`cleanup`/`segment` and `diff`/`classify`/`surface`/`export` - the stage numbers are a display convention only, and do not appear anywhere in the JSON itself.)

## 13. Output File Naming Convention

### 13.1 Default Pattern

```
<compartment>_<stage>_<sequence>.<extension>
```

- `<compartment>`: the compartment name from the project file.
- `<stage>`: the stage short name. Example: `cleanup`.
- `<sequence>`: a three-digit number, starting at `001`. The sequence number counts files in that stage's own subfolder, inside that stage's own baseline/scan/diff folder. The sequence number does not count files anywhere else.
- `<extension>`: the file extension for that stage's output type. Example: `.ply`.

Example, baseline: `baseline/04_cleanup/compartment_04_cleanup_001.ply`
Example, a scan: `scans/post-storm_2026-09-01/02_slam/compartment_04_slam_001.ply`

The scan ID or diff ID does not need to appear in the file name, because the folder path already identifies which scan or diff the file belongs to.

### 13.2 Customization

The stage dialog shows the generated name in an editable text field. The generated name is pre-filled. A user can change the name before the run starts. The applet does not require a user to browse to a save location. The applet still allows a user to change the file name text.

### 13.3 Exception: Stage `segment`'s Output Folder

Stage `segment` does not write one output file - it writes into a folder. What it writes there depends on `params.write_separate_surfaces` (Section 8):

- `false` (the default): a combined `<name>_classified.ply` (every input point, carrying a `classification` field), a `<name>_envelope.ply` (the classified surfaces only), and a `manifest.json`. No per-surface files.
- `true`: the above, PLUS one cloud per detected surface (`<name>_floor.ply`, `<name>_wall_1.ply`, and so on) and a `<name>_unclassified.ply` (points that matched no surface).

`<name>` is the output folder's own name (see below) - every `.ply` this stage writes is prefixed with it, so files stay distinguishable even if copied or opened outside their folder (for example, several runs' output opened together in a viewer, where a bare `classified.ply` from each run would otherwise look identical). `manifest.json` keeps its fixed, unprefixed name - it is read back by path, not opened directly in a viewer, so the same collision risk does not apply to it.

For this stage, the applet reuses Section 13.1's sequence number as a FOLDER name instead of a file name - for example `baseline/05_segment/compartment_04_segment_001/`, making `<name>` above `compartment_04_segment_001` - and `segment_planes.py` writes its files inside that one folder. The stage's recorded `output` field (Section 8) points at `<name>_classified.ply` inside that folder - the one file that carries every point, useful to carry into the rest of a pipeline as a single cloud rather than juggling separate per-surface files. Whatever files exist, and `manifest.json` itself, stay on disk inside the same folder for reference; their content is also copied into `project.json` (`classification_ids`, `envelope_output`, `surfaces` - Section 8), so nothing needs to read `manifest.json` directly once a run is recorded.

### 13.4 Stage `diff`'s M3C2 Params File

The M3C2 params file (recorded as `m3c2_params_file` - Section 8) is not the stage's main `.ply` output, but it follows the same Section 13.1 pattern with a `.txt` extension instead - for example `diffs/<diff_id>/05_diff/compartment_04_diff_001.txt`. This sequence number counts only `.txt` files in that folder, separately from the `.ply` sequence, so the two do not collide even when a params file and its diff output share the same number.

The Stage 5 (Diff) dialog's "Generate Params File..." button builds this path and writes the file automatically for a project pipeline - it does not ask the user to pick a save location. A save-location dialog only appears when running Stage 5 outside a project, or with "Use manual file selection instead" checked.

## 14. Example Project File

```json
{
  "project_id": "compartment_04_2026-08-12",
  "compartment": "compartment_04",
  "created": "2026-08-12T09:15:00Z",
  "updated": "2026-09-01T14:10:00Z",
  "schema_version": 2,
  "baseline": {
    "raw": {
      "path": "baseline/01_raw/capture.pcap",
      "source_type": "pcap",
      "import_method": "copy"
    },
    "stages": {
      "slam": {
        "status": "complete",
        "output": "baseline/02_slam/compartment_04_slam_001.ply",
        "source_type": "pcap",
        "point_spacing": 0.021,
        "started": "2026-08-12T09:16:00Z",
        "completed": "2026-08-12T09:22:00Z",
        "rerun_count": 0
      },
      "level": {
        "status": "complete",
        "output": "baseline/03_level/compartment_04_level_001.ply",
        "params": {"distance_threshold": 0.03, "max_planes": 6, "min_inlier_fraction": 0.15, "horizontal_threshold": 0.7},
        "started": "2026-08-12T09:23:00Z",
        "completed": "2026-08-12T09:26:00Z",
        "rerun_count": 0
      },
      "cleanup": {
        "status": "complete",
        "output": "baseline/04_cleanup/compartment_04_cleanup_001.ply",
        "icp_rms": 0.0184,
        "sidecar": "baseline/04_cleanup/compartment_04_cleanup_001.rms.json",
        "started": "2026-08-12T09:27:00Z",
        "completed": "2026-08-12T09:35:00Z",
        "rerun_count": 0
      },
      "segment": {
        "status": "complete",
        "output": "baseline/05_segment/compartment_04_segment_001/classified.ply",
        "params": {"distance_threshold": 0.05, "max_planes": 20, "min_inlier_fraction": 0.003, "cluster_eps": 0.5, "write_separate_surfaces": false},
        "envelope_output": "baseline/05_segment/compartment_04_segment_001/envelope.ply",
        "classification_ids": {"0": "unclassified", "1": "floor", "2": "ceiling", "3": "wall_1", "4": "wall_2"},
        "surfaces": [
          {"name": "floor", "file": null, "point_count": 88213, "normal": [0.0, 0.0, 1.0], "z_min": -0.01, "z_max": 0.01},
          {"name": "ceiling", "file": null, "point_count": 61042, "normal": [0.0, 0.0, 1.0], "z_min": 2.39, "z_max": 2.41}
        ],
        "started": "2026-08-12T09:36:00Z",
        "completed": "2026-08-12T09:41:00Z",
        "rerun_count": 0
      }
    },
    "is_baseline_candidate": true,
    "promoted": false
  },
  "scans": {
    "post-storm_2026-09-01": {
      "raw": {
        "path": "scans/post-storm_2026-09-01/01_raw/capture.pcap",
        "source_type": "pcap",
        "import_method": "copy"
      },
      "stages": {
        "slam": {"status": "complete", "output": "scans/post-storm_2026-09-01/02_slam/compartment_04_slam_001.ply", "rerun_count": 0},
        "level": {"status": "complete", "output": "scans/post-storm_2026-09-01/03_level/compartment_04_level_001.ply", "rerun_count": 0},
        "cleanup": {"status": "complete", "output": "scans/post-storm_2026-09-01/04_cleanup/compartment_04_cleanup_001.ply", "icp_rms": 0.021, "rerun_count": 0},
        "segment": {"status": "not_started"}
      }
    }
  },
  "diffs": {
    "post-storm_2026-09-01_vs_baseline": {
      "reference": "baseline",
      "comparison": "post-storm_2026-09-01",
      "stages": {
        "diff": {"status": "running", "started": "2026-09-01T14:10:00Z", "rerun_count": 0},
        "classify": {"status": "not_started"},
        "surface": {"status": "not_started"},
        "export": {"status": "not_started"}
      }
    }
  }
}
```

A `classify` stage entry, once complete with clustering enabled (the default), looks like this:

```json
{
  "classify": {
    "status": "complete",
    "output": "diffs/post-storm_2026-09-01_vs_baseline/06_classify/compartment_04_classify_001.ply",
    "params": {"threshold": 0.045, "keep_all": false, "cluster": true, "cluster_method": "dbscan", "cluster_eps": 0.05, "cluster_min_samples": 4, "min_cluster_size": 4},
    "n_flagged": 55,
    "n_confirmed": 45,
    "n_noise": 10,
    "clusters": [
      {"cluster_id": 0, "point_count": 30, "centroid": [1.0, 1.0, 1.0], "extent": [0.09, 0.11, 0.08], "mean_magnitude": 0.066, "max_magnitude": 0.078},
      {"cluster_id": 1, "point_count": 15, "centroid": [-2.0, 3.0, 0.0], "extent": [0.07, 0.08, 0.06], "mean_magnitude": 0.076, "max_magnitude": 0.09}
    ],
    "started": "2026-09-01T14:12:00Z",
    "completed": "2026-09-01T14:12:40Z",
    "rerun_count": 0
  }
}
```

## 15. Backward Compatibility

The applet supports two modes: project mode and manual mode. In manual mode, the user browses to input files and output locations, same as the original behavior. In manual mode, the applet does not write a `project.json` file. A user selects manual mode by not opening or creating a project. The applet does not force a user into project mode.

## 16. Changes From Version 1

Version 1 assumed one SLAM/Level/Cleanup/Diff/Classify/Export pipeline per project. This did not allow more than one comparison scan against a baseline, and did not allow comparing a scan against more than one reference. Version 2 changes:

- Removed the top-level `stages` object and `current_stage` field.
- Added the top-level `baseline` object, now holding its own `raw` and `stages` (Section 9), in addition to the promotion fields it already held in Version 1.
- Added the top-level `scans` object, holding zero or more comparison scans, each with its own `raw` and `stages` (Section 10).
- Added the top-level `diffs` object, holding zero or more Diff/Classify/Export pipelines, each naming which two things it compares (Section 11).
- Added the `raw` object shape as its own documented section (Section 12) - Version 1 needed this for Stage 1 to find its input, but never documented it formally.
- Added the `decoded_path` field to the `raw` object (Section 12.1), so a baseline or scan whose raw source is decoded (see `decode_raw_packets.py`) keeps using that decoded copy for Stage 1, while `path` still records the true original import.
- Added the `error` field to a stage entry (Section 8), holding the error message from a failed run.
- The folder structure (Section 5) now nests stage subfolders inside a `baseline/`, `scans/<scan_id>/`, or `diffs/<diff_id>/` folder, instead of the stage subfolders sitting directly in the project root.
- `schema_version` is now `2`. A project file with `schema_version: 1` is not compatible with Version 2 tooling. There is no automatic migration in this version - a Version 1 project's data would need to be re-imported as a Version 2 baseline by hand.
- Added a `segment` stage to the baseline/scan pipeline (Section 8, Section 9, Section 10.2), after `cleanup`. Added Section 13.3, describing this stage's one exception to the Section 13.1 file-naming convention (a folder instead of a single file, since `segment_planes.py` writes several files together).
- Added spatial clustering to the `classify` stage (Section 8). `m3c2_classify.py` now groups its flagged points into damage sites by 3D position (DBSCAN or HDBSCAN) on top of its original magnitude threshold, rejecting spatially isolated flagged points as a second false-positive filter. On by default; does not change `classify`'s file-naming convention (still one output file, per Section 13.1) - the per-cluster summary rides along as a `<output>.clusters.json` sidecar, absorbed into the stage entry per Section 3.3, same as `segment`'s `manifest.json`.
- Changed `segment`'s default output: `segment_planes.py` now writes only the combined `classified.ply` (plus `envelope.ply` and `manifest.json`) unless `params.write_separate_surfaces` is `true` (Section 8, Section 13.3) - per-surface files (`floor.ply`, `wall_1.ply`, `unclassified.ply`) are opt-in, not automatic. A `surfaces[].file` entry (Section 8) is `null` when that surface's own file wasn't written.
- Fixed `level`'s floor-picking heuristic (`level_cloud.py`): it now picks the LOWEST candidate plane that clears `horizontal_threshold`, instead of scoring all candidates by point count - confirmed on real data that a real ceiling can have more points than a real floor, which the old scoring could pick by mistake. Added a `horizontal_threshold` field to `level`'s `params` (Section 8's generic `params` field already covers this; no new schema field needed).
- Changed `segment`'s tuned defaults (`distance_threshold` 0.02 → 0.05, `max_planes` 10 → 20, `min_inlier_fraction` 0.015 → 0.003, `cluster_eps` 0.15 → 0.5), matching a combination confirmed to work well on real full-room/compartment scans.
- Fixed Stage 5 (Diff)'s output-file detection: the Stage 5 dialog now watches the `reference`/baseline cloud's own folder for CloudCompare's new M3C2 result file, matching `pipeline_core.build_diff_command()`'s own cloud load order (baseline loaded first - Section 11.3a). The dialog previously watched the `comparison` cloud's folder instead, disagreeing with the command about which cloud CloudCompare would actually treat as the result-bearing "compared" cloud - confirmed directly from a CloudCompare run log showing the result landing in the baseline's own folder.
- Added Section 13.4, describing the M3C2 params file's own Section 13.1-style auto-naming. The Stage 5 dialog's "Generate Params File..." button now names and writes this file automatically for a project pipeline, instead of always asking the user to pick a save location.
- Fixed a real bug in `m3c2_classify.py`: the `cluster_id` field added to a `classify` stage's output cloud was labelled `"i8"` (64-bit integer), but the PLY format has no standard 64-bit integer type, so `plyfile.PlyElement.describe()` raised `ValueError` while saving, after clustering itself had already completed correctly. Changed to `"i4"` (32-bit) - ample range for a cluster count that only ever reaches a few hundred. No schema field changed; this only affects how `cluster_id` is written inside the output `.ply` file, not `classify`'s recorded `clusters` array (Section 8), which was never affected by this bug.
- Renumbered the display stage numbers so `segment` becomes Stage 4 and `diff`/`classify`/`surface`/`export` become Stage 5/6/7/8 (previously `segment` displayed as "Stage 3.5", with `diff` through `export` as Stage 4-7). This matches the folder-naming scheme (Section 5), which already numbered `05_segment`/`05_diff` through `08_export` this way. Purely a display and documentation change - no stage's internal name (Section 8's `slam`/`level`/`cleanup`/`segment`/`diff`/`classify`/`surface`/`export` keys) or folder path changed.
- Changed `get_diff_inputs()` (Section 11.3) so a diff prefers each side's `segment.output` over `cleanup.output`, falling back to `cleanup.output` only when that side's `segment` stage has not completed. Added `reference_source_stage` and `comparison_source_stage` to the `diff` stage's recorded fields (Section 8), so which stage supplied each side's cloud is visible in `project.json` without re-deriving it. This resolves the "carrying `segment`'s `classification` field through Stage 5 (Diff)" item PROJECT_INTEGRATION_PLAN.md previously listed as open.
