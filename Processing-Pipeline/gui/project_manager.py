#!/usr/bin/env python3
"""
project_manager.py
=====================
Implements the project layer described in PROJECT_SCHEMA_v2.md - a JSON
file (project.json) that tracks a compartment's baseline pipeline, its
comparison scans, and the diffs run between them, replacing manual
per-stage file selection. This module is deliberately GUI-free and
subprocess-free (PROJECT_INTEGRATION_PLAN.md Section 2.2, written against
Version 1 but the rule still holds): it only reads and writes project
state. pipeline_core.py stays responsible for actually building/running
stage commands; pipeline_applet.py stays responsible for the GUI.

THIS IS A SCHEMA V2 REWRITE. Version 1 (the previous shape of this file)
assumed one linear SLAM -> Level -> Cleanup -> Diff -> Classify -> Export
pipeline per project. That did not match the real workflow: a compartment
gets ONE baseline scan, then MANY later comparison scans over its life,
and any one comparison scan can be diffed against more than one reference
(the baseline, or an earlier comparison scan). PROJECT_SCHEMA_v2.md
Section 4 explains the reasoning in full; Section 16 lists exactly what
changed. The short version, reflected everywhere below:

  - There is no longer a single top-level `stages` object or
    `current_stage` field - a project can have many independent
    pipelines running at once (the baseline, each scan, each diff), so
    there is no one "next stage" for the whole project.
  - `stages` is now a shape that gets reused three ways: once for the
    top-level `baseline` object, once per entry in `scans`, once per
    entry in `diffs`. This module models that reuse with a
    `PipelineHandle` - a lightweight pointer at "the baseline", "scan
    X", or "diff Y" - so a caller can't accidentally write cleanup's
    result into the wrong scan by passing a bare project around.
  - A diff pipeline's first stage (`diff`, i.e. running M3C2) takes TWO
    inputs - whatever `reference` and `comparison` point at - not one.
    `get_input_for_stage()` deliberately refuses to resolve that stage;
    use `get_diff_inputs()` instead. Every other stage in every pipeline
    (including `classify`/`surface`/`export` within a diff) still has
    exactly one input: the previous stage's own output, same as before.

Two things Version 1's docstring called out as "deliberate additions
beyond what the schema documents" are no longer additions - Version 2
adopted both into the schema proper, so there is nothing left to
reconcile:
  - The top-level `raw` object (now Section 12, `baseline.raw` /
    `scans.<id>.raw`).
  - The `error` field on a failed stage's entry (now in Section 8's
    field table).

What IS still an addition beyond PROJECT_SCHEMA_v2.md, because the doc
deliberately only specifies the *stored* shape, not a query API around
it: `find_next_stage()`, `list_scans()`, `list_diffs()`, and
`get_baseline_cleanup_output()` are pure read helpers over the fields
above - none of them add or change a stored field.
"""

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2

# Stage order within the baseline object and within each scans[<id>] entry
# (PROJECT_SCHEMA_v2.md Section 9.2 / Section 10.2's "stages" field, both
# pointing back to Section 8's shared shape). Order matters here - it
# defines both "the previous stage" for input resolution and stage-name
# validation.
#
# "segment" was added after "cleanup" (PROJECT_SCHEMA_v2.md Section 8's
# stage-specific fields table) - it classifies ONE pipeline's own cleaned
# cloud into floor/ceiling/wall_N/unclassified (segment_planes.py), so it
# belongs with the other per-pipeline stages here, not with the diffs
# stages below. Nothing downstream reads segment's output automatically
# (it is this pipeline's last stage) - a diff's own "diff" stage keeps
# reading cleanup.output directly, unaffected, since M3C2 needs the full
# cleaned cloud (clutter/machinery included), not just the classified
# envelope.
BASELINE_SCAN_STAGE_NAMES = ["slam", "level", "cleanup", "segment"]

# Stage order within each diffs[<id>] entry (Section 11.2). "surface" is
# back in this list - Version 1 deliberately left it out because the
# applet didn't have it wired up yet; Section 5's folder layout documents
# it as a real stage in Version 2, so it belongs here now.
DIFF_STAGE_NAMES = ["diff", "classify", "surface", "export"]

RAW_FOLDER = "01_raw"

# Stage subfolder names, relative to whichever baseline/scan/diff folder
# owns them (PROJECT_SCHEMA_v2.md Section 5).
BASELINE_SCAN_STAGE_FOLDERS = {
    "slam": "02_slam",
    "level": "03_level",
    "cleanup": "04_cleanup",
    "segment": "05_segment",
}
DIFF_STAGE_FOLDERS = {
    "diff": "05_diff",
    "classify": "06_classify",
    "surface": "07_surface",
    "export": "08_export",
}

VALID_SOURCE_TYPES = {"pcap", "osf", "ros1_bag", "ros2_bag"}
VALID_PIPELINE_KINDS = {"baseline", "scan", "diff"}


class ProjectError(Exception):
    """Raised for problems with project state/files - missing project.json,
    schema version mismatch, an unknown stage name, an unknown scan/diff
    ID, a stage whose required predecessor hasn't completed yet, etc."""


class Project:
    """
    Wraps a project's JSON-serializable data (`.data`, matching
    PROJECT_SCHEMA_v2.md's top-level fields) together with its on-disk
    root folder (`.root`). The schema itself doesn't store its own
    location - so moving or renaming the project folder doesn't leave a
    stale path baked into project.json - so the root path is tracked
    here, in memory only, and is never written to disk.

    Use baseline_handle() / scan_handle() / diff_handle() to get a
    PipelineHandle for reading or writing one specific pipeline's stage
    state, rather than reaching into `.data` directly.
    """

    def __init__(self, data, root):
        self.data = data
        self.root = Path(root)

    def baseline_handle(self):
        return PipelineHandle(self, "baseline")

    def scan_handle(self, scan_id):
        return PipelineHandle(self, "scan", scan_id)

    def diff_handle(self, diff_id):
        return PipelineHandle(self, "diff", diff_id)

    def __repr__(self):
        return (f"Project(project_id={self.data.get('project_id')!r}, "
                f"scans={list(self.data.get('scans', {}).keys())!r}, "
                f"diffs={list(self.data.get('diffs', {}).keys())!r}, "
                f"root={self.root!r})")


class PipelineHandle:
    """
    A pointer at exactly one of a project's pipelines: the baseline, one
    scan, or one diff (PROJECT_SCHEMA_v2.md Section 8's shared `stages`
    shape, scoped to whichever one this handle points at). Everything in
    this module that reads or writes stage state takes one of these
    instead of a bare Project + a stage name string, specifically so a
    caller can't accidentally advance the wrong scan's cleanup stage by
    passing the right stage name against the wrong project object.

    Construct via Project.baseline_handle() / .scan_handle(scan_id) /
    .diff_handle(diff_id) rather than calling this directly.
    """

    def __init__(self, project, kind, pipeline_id=None):
        if kind not in VALID_PIPELINE_KINDS:
            raise ProjectError(
                f"Unknown pipeline kind '{kind}'. Must be one of: {sorted(VALID_PIPELINE_KINDS)}")
        if kind in ("scan", "diff") and not pipeline_id:
            raise ProjectError(f"pipeline_id is required for kind='{kind}'")
        self.project = project
        self.kind = kind
        self.pipeline_id = pipeline_id

    @property
    def entry(self):
        """The live dict for this pipeline (baseline object, or one
        scans[<id>]/diffs[<id>] entry) - mutating this mutates
        project.data directly, same as every other function here."""
        if self.kind == "baseline":
            return self.project.data["baseline"]
        if self.kind == "scan":
            scans = self.project.data.get("scans", {})
            if self.pipeline_id not in scans:
                raise ProjectError(f"Unknown scan '{self.pipeline_id}'")
            return scans[self.pipeline_id]
        diffs = self.project.data.get("diffs", {})
        if self.pipeline_id not in diffs:
            raise ProjectError(f"Unknown diff '{self.pipeline_id}'")
        return diffs[self.pipeline_id]

    @property
    def root(self):
        """Absolute path to this pipeline's own folder - baseline/,
        scans/<id>/, or diffs/<id>/ (Section 5)."""
        return self.project.root / _pipeline_folder_rel(self.kind, self.pipeline_id)

    @property
    def stage_names(self):
        return _stage_names_for_kind(self.kind)

    @property
    def stage_folders(self):
        return _stage_folder_map_for_kind(self.kind)

    def __repr__(self):
        label = self.kind if self.pipeline_id is None else f"{self.kind}:{self.pipeline_id}"
        return f"PipelineHandle({label!r}, project={self.project.data.get('project_id')!r})"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _stage_names_for_kind(kind):
    if kind in ("baseline", "scan"):
        return BASELINE_SCAN_STAGE_NAMES
    if kind == "diff":
        return DIFF_STAGE_NAMES
    raise ProjectError(f"Unknown pipeline kind '{kind}'. Must be one of: {sorted(VALID_PIPELINE_KINDS)}")


def _stage_folder_map_for_kind(kind):
    if kind in ("baseline", "scan"):
        return BASELINE_SCAN_STAGE_FOLDERS
    if kind == "diff":
        return DIFF_STAGE_FOLDERS
    raise ProjectError(f"Unknown pipeline kind '{kind}'. Must be one of: {sorted(VALID_PIPELINE_KINDS)}")


def _validate_stage_name(kind, stage_name):
    names = _stage_names_for_kind(kind)
    if stage_name not in names:
        raise ProjectError(f"Unknown stage '{stage_name}' for kind='{kind}'. Must be one of: {names}")


def _pipeline_folder_rel(kind, pipeline_id=None):
    if kind == "baseline":
        return "baseline"
    if kind == "scan":
        if not pipeline_id:
            raise ProjectError("pipeline_id (scan ID) is required for kind='scan'")
        return f"scans/{pipeline_id}"
    if kind == "diff":
        if not pipeline_id:
            raise ProjectError("pipeline_id (diff ID) is required for kind='diff'")
        return f"diffs/{pipeline_id}"
    raise ProjectError(f"Unknown pipeline kind '{kind}'. Must be one of: {sorted(VALID_PIPELINE_KINDS)}")


def _to_relative(project, path):
    """Normalizes a path (absolute or already-relative) to a project-root-
    relative string with forward slashes, matching PROJECT_SCHEMA_v2.md's
    own examples (e.g. "baseline/04_cleanup/....ply") regardless of OS -
    this file may be read by tooling on Windows or in WSL2 (per the
    project's own longer-term plans), so a consistent separator matters."""
    path = Path(path)
    if path.is_absolute():
        try:
            path = path.relative_to(project.root)
        except ValueError:
            raise ProjectError(f"Path {path} is not inside the project root {project.root}")
    return str(path).replace(os.sep, "/")


def _copy_source(source_path, dest_path):
    if source_path.is_dir():
        shutil.copytree(source_path, dest_path)
    else:
        shutil.copy2(source_path, dest_path)


def _import_raw_source(source_path, dest_path, link_raw):
    """
    link_raw: "copy" (default - safe, doesn't touch the original, always
    works), "move" (relocates the original into the project - good for
    large captures you don't need to keep elsewhere; shutil.move handles
    both files and directories, and falls back to copy+delete internally
    if source/dest are on different volumes), or "link" (symlink instead
    of duplicating data - note this can fail on Windows without Developer
    Mode enabled, a real issue confirmed elsewhere in this project; no
    automatic fallback here, so a failure raises rather than silently
    doing something you didn't ask for).
    """
    if link_raw == "copy":
        _copy_source(source_path, dest_path)
    elif link_raw == "move":
        shutil.move(str(source_path), str(dest_path))
    elif link_raw == "link":
        if source_path.is_dir():
            os.symlink(source_path, dest_path, target_is_directory=True)
        else:
            os.symlink(source_path, dest_path)
    else:
        raise ProjectError("link_raw must be 'copy', 'move', or 'link'")

    return link_raw


def _make_raw_and_stage_folders(root_folder, stage_folder_map):
    (root_folder / RAW_FOLDER).mkdir(parents=True)
    for folder in stage_folder_map.values():
        (root_folder / folder).mkdir()


def _make_stage_only_folders(root_folder, stage_folder_map):
    root_folder.mkdir(parents=True)
    for folder in stage_folder_map.values():
        (root_folder / folder).mkdir()


# ---------------------------------------------------------------------------
# Public API - project lifecycle
# ---------------------------------------------------------------------------

def create_project(root_dir, compartment_name, source_path, source_type, link_raw="copy"):
    """
    Creates the project root folder, the baseline/ subfolder structure
    (01_raw/ through 04_cleanup/, Section 5), imports the raw source into
    baseline/01_raw/, writes the first project.json (with empty `scans`
    and `diffs` objects), and returns the Project object.

    root_dir: the PARENT location the user picked ("the user selects the
    location of the root folder" - Section 5). The actual project root
    created is root_dir/<project_id>, named automatically from the
    compartment and today's date (Section 7's project_id format), so the
    user doesn't have to separately name the folder.

    link_raw: "copy" (default), "move", or "link" - see
    _import_raw_source() for what each does and their tradeoffs.
    """
    if source_type not in VALID_SOURCE_TYPES:
        raise ProjectError(f"source_type must be one of {sorted(VALID_SOURCE_TYPES)}, "
                            f"got '{source_type}'")

    source_path = Path(source_path)
    if not source_path.exists():
        raise ProjectError(f"Source file/folder does not exist: {source_path}")

    project_id = f"{compartment_name}_{_today_str()}"

    root = Path(root_dir) / project_id
    if root.exists():
        raise ProjectError(f"Project folder already exists: {root}")
    root.mkdir(parents=True)

    baseline_root = root / "baseline"
    _make_raw_and_stage_folders(baseline_root, BASELINE_SCAN_STAGE_FOLDERS)

    raw_dest = baseline_root / RAW_FOLDER / source_path.name
    import_method = _import_raw_source(source_path, raw_dest, link_raw)

    now = _now_iso()
    data = {
        "project_id": project_id,
        "compartment": compartment_name,
        "created": now,
        "updated": now,
        "schema_version": SCHEMA_VERSION,
        "baseline": {
            "raw": {
                "path": f"baseline/{RAW_FOLDER}/{source_path.name}",
                "source_type": source_type,
                "import_method": import_method,
            },
            "stages": {name: {"status": "not_started"} for name in BASELINE_SCAN_STAGE_NAMES},
            "is_baseline_candidate": True,
            "promoted": False,
        },
        "scans": {},
        "diffs": {},
    }

    project = Project(data, root)
    save_project(project)
    return project


def add_scan(project, label, source_path, source_type, link_raw="copy"):
    """
    Adds a new comparison scan (Section 10): creates
    scans/<scan_id>/01_raw/ through 04_cleanup/, imports the raw source,
    and adds a `scans[<scan_id>]` entry with its own `raw` and `stages`.

    scan_id is built as `<label>_<date>` (Section 10.1), with today's
    date added automatically - the same convention create_project() uses
    for project_id, so the caller only supplies the meaningful part.

    Returns the new scan_id (a string) - use project.scan_handle(scan_id)
    to get a PipelineHandle for running its SLAM/Level/Cleanup stages.
    """
    if source_type not in VALID_SOURCE_TYPES:
        raise ProjectError(f"source_type must be one of {sorted(VALID_SOURCE_TYPES)}, "
                            f"got '{source_type}'")

    source_path = Path(source_path)
    if not source_path.exists():
        raise ProjectError(f"Source file/folder does not exist: {source_path}")

    scan_id = f"{label}_{_today_str()}"
    if scan_id in project.data.get("scans", {}):
        raise ProjectError(f"A scan named '{scan_id}' already exists in this project.")

    scan_root = project.root / "scans" / scan_id
    if scan_root.exists():
        raise ProjectError(f"Scan folder already exists: {scan_root}")
    _make_raw_and_stage_folders(scan_root, BASELINE_SCAN_STAGE_FOLDERS)

    raw_dest = scan_root / RAW_FOLDER / source_path.name
    import_method = _import_raw_source(source_path, raw_dest, link_raw)

    project.data.setdefault("scans", {})[scan_id] = {
        "raw": {
            "path": f"scans/{scan_id}/{RAW_FOLDER}/{source_path.name}",
            "source_type": source_type,
            "import_method": import_method,
        },
        "stages": {name: {"status": "not_started"} for name in BASELINE_SCAN_STAGE_NAMES},
    }
    save_project(project)
    return scan_id


def add_diff(project, label, reference, comparison):
    """
    Adds a new diff pipeline (Section 11): creates
    diffs/<diff_id>/05_diff/ through 08_export/, and adds a
    `diffs[<diff_id>]` entry naming which two things it compares.

    reference: either the literal string "baseline", or an existing scan
    ID from project.data["scans"] - whichever this diff treats as the
    baseline side of the comparison (Section 11.2).
    comparison: an existing scan ID - the scan being compared against
    `reference`. Must already exist (add it with add_scan() first).

    A single scan can appear as `comparison` in more than one diff, to
    compare it against more than one reference (Section 11.4) - this
    function doesn't restrict that.

    diff_id is built as `<label>_<date>` (Section 11.1), same convention
    as add_scan()'s scan_id.

    Returns the new diff_id (a string) - use project.diff_handle(diff_id)
    to get a PipelineHandle for running its Diff/Classify/Surface/Export
    stages, and get_diff_inputs() to resolve its two source clouds.
    """
    if reference != "baseline" and reference not in project.data.get("scans", {}):
        raise ProjectError(
            f"reference must be the string 'baseline' or an existing scan ID, got '{reference}'")
    if comparison not in project.data.get("scans", {}):
        raise ProjectError(f"comparison must be an existing scan ID, got '{comparison}'")

    diff_id = f"{label}_{_today_str()}"
    if diff_id in project.data.get("diffs", {}):
        raise ProjectError(f"A diff named '{diff_id}' already exists in this project.")

    diff_root = project.root / "diffs" / diff_id
    if diff_root.exists():
        raise ProjectError(f"Diff folder already exists: {diff_root}")
    _make_stage_only_folders(diff_root, DIFF_STAGE_FOLDERS)

    project.data.setdefault("diffs", {})[diff_id] = {
        "reference": reference,
        "comparison": comparison,
        "stages": {name: {"status": "not_started"} for name in DIFF_STAGE_NAMES},
    }
    save_project(project)
    return diff_id


def load_project(project_root):
    """Reads project.json from a folder. Raises ProjectError if the file
    is missing or its schema_version doesn't match what this code expects
    (Version 1 project files are not compatible - Section 16 - and need
    to be re-imported by hand as a new Version 2 baseline)."""
    root = Path(project_root)
    project_json_path = root / "project.json"
    if not project_json_path.exists():
        raise ProjectError(f"No project.json found in: {root}")

    try:
        data = json.loads(project_json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ProjectError(f"project.json in {root} is not valid JSON: {e}")

    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ProjectError(
            f"Project schema version mismatch: file has {version!r}, this code "
            f"expects {SCHEMA_VERSION}. There is no automatic migration from "
            f"Version 1 (PROJECT_SCHEMA_v2.md Section 16) - a Version 1 project's "
            f"data would need to be re-imported by hand as a new Version 2 baseline."
        )

    return Project(data, root)


def save_project(project):
    """
    Writes the project back to project.json, updating the 'updated'
    timestamp. Writes to a temp file first, then atomically replaces the
    real file (Path.replace() is atomic on the same volume on both
    Windows and POSIX) - so a write that fails partway (e.g. disk full,
    process killed) can't leave a corrupted project.json behind.
    """
    project.data["updated"] = _now_iso()
    project_json_path = project.root / "project.json"
    tmp_path = project_json_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(project.data, indent=2), encoding="utf-8")
    tmp_path.replace(project_json_path)
    return project


def list_scans(project):
    """Sorted list of this project's scan IDs."""
    return sorted(project.data.get("scans", {}).keys())


def list_diffs(project):
    """Sorted list of this project's diff IDs."""
    return sorted(project.data.get("diffs", {}).keys())


# ---------------------------------------------------------------------------
# Public API - per-pipeline stage state (all take a PipelineHandle)
# ---------------------------------------------------------------------------

def get_input_for_stage(handle, stage_name):
    """
    Returns the ABSOLUTE path (as a string) to a SPECIFIC stage's input
    within this pipeline - the previous stage's recorded output, or (for
    the baseline/scan pipelines' first stage, 'slam') the raw imported
    source, OR that source's decoded form if one has been recorded via
    set_decoded_raw_path() (e.g. a raw-Ouster-packet bag that
    decode_raw_packets.py converted to a bag of decoded points - see that
    function's docstring). Preferring the decoded form here, rather than
    always the original raw import, is what makes a converted bag
    actually get used for this pipeline's SLAM stage - not just displayed
    in a dialog's input field.

    Does NOT work for a diff pipeline's first stage ('diff') - that stage
    takes two inputs (whatever `reference` and `comparison` point at,
    Section 11.3), not one predecessor's output. Call get_diff_inputs()
    for that stage instead; this function raises ProjectError if asked
    for it, rather than silently returning something misleading.

    This resolves purely from stage_name's fixed position in this
    pipeline's own stage order - not from any notion of "the pipeline's
    current stage" (Version 2 has no such field; see find_next_stage()
    if that's what's actually wanted). That distinction matters for a
    stage dialog re-opened to redo an earlier stage after the pipeline
    has already progressed further: this always resolves what THIS
    stage specifically needs (e.g. Level always needs SLAM's output),
    regardless of how far the pipeline has since moved on.
    """
    _validate_stage_name(handle.kind, stage_name)
    stage_names = handle.stage_names
    idx = stage_names.index(stage_name)

    if idx == 0:
        if handle.kind in ("baseline", "scan"):
            raw = handle.entry.get("raw")
            if not raw or not raw.get("path"):
                raise ProjectError(
                    "No raw source recorded for this pipeline - was it created "
                    "with create_project() or add_scan()?"
                )
            return str(handle.project.root / (raw.get("decoded_path") or raw["path"]))
        raise ProjectError(
            "Stage 'diff' takes two inputs (its reference and comparison, "
            "Section 11.3) - use get_diff_inputs(handle) instead of "
            "get_input_for_stage() for this stage."
        )

    previous_stage = stage_names[idx - 1]
    previous_entry = handle.entry["stages"].get(previous_stage, {})
    output = previous_entry.get("output")
    if not output:
        raise ProjectError(
            f"Stage '{previous_stage}' has no recorded output yet - it must "
            f"complete before stage '{stage_name}' can run."
        )
    return str(handle.project.root / output)


def set_decoded_raw_path(handle, decoded_path):
    """
    Records a decoded form of this pipeline's raw import (e.g. the output
    of decode_raw_packets.py run against a raw-Ouster-packet bag) on the
    `raw` object's `decoded_path` field - an addition beyond
    PROJECT_SCHEMA_v2.md Section 12, in the same spirit as
    find_next_stage()/list_scans()/etc (module docstring): it's a query/
    convenience field, not something the schema needs to specify, and it
    does not replace or overwrite `raw.path` - that field keeps recording
    the TRUE original import for provenance, exactly as before.

    Once this is set, get_input_for_stage(handle, <first stage>) resolves
    to `decoded_path` instead of `path` - so a pipeline whose raw source
    got decoded stays in project mode (still tracked, still recorded)
    with its SLAM stage actually reading the decoded bag, rather than the
    caller needing to fall back to manual file selection (which would
    also stop this stage's completion from being recorded at all - see
    finish_stage()).

    handle must be a baseline or scan pipeline (the only kinds with a
    `raw` object at all - Section 9/10). Raises ProjectError otherwise,
    or if this pipeline has no `raw` object recorded yet.
    """
    if handle.kind not in ("baseline", "scan"):
        raise ProjectError(
            f"set_decoded_raw_path() only applies to a baseline or scan pipeline "
            f"(kind='{handle.kind}' has no raw import to decode).")
    raw = handle.entry.get("raw")
    if not raw or not raw.get("path"):
        raise ProjectError(
            "No raw source recorded for this pipeline - was it created "
            "with create_project() or add_scan()?"
        )
    raw["decoded_path"] = _to_relative(handle.project, decoded_path)
    save_project(handle.project)


def get_diff_inputs(handle):
    """
    Resolves a diff pipeline's two source clouds (Section 11.3): the
    `reference` side (either the project's baseline, or a named scan) and
    the `comparison` side (always a named scan).

    Each side independently prefers its own `segment.output`
    (<name>_classified.ply - every point from `cleanup.output`, PLUS a
    `classification` field naming which surface each point belongs to,
    Section 13.3) over `cleanup.output`, falling back to `cleanup.output`
    only when that side hasn't completed Segment (Stage 4) yet. Segment
    stays optional - a diff still runs fine with only Cleanup done on
    either or both sides - but when it HAS run, using its output instead
    means Stage 5 (Diff)'s own M3C2 result carries the classification
    field through, so later stages can filter or group by surface type
    (floor/ceiling/wall/unclassified) instead of losing that information
    at the diff step. Point coverage is identical either way - Segment's
    combined output holds the exact same points as Cleanup's, just with
    the field added - so this never changes which points M3C2 sees, only
    whether they arrive pre-labeled.

    handle must be a diff-kind PipelineHandle (project.diff_handle(...)).

    Returns a dict:
        {
            "reference_path": <absolute path, str>,
            "comparison_path": <absolute path, str>,
            "reference_source_stage": "segment" or "cleanup",
            "comparison_source_stage": "segment" or "cleanup",
            "registration_error_used": <float or None>,
        }

    reference_source_stage / comparison_source_stage record which stage
    each path actually came from - a caller (the Stage 5 dialog's report,
    in particular) can use this to tell the user plainly whether the
    classification field is actually present on a given run's input,
    rather than silently leaving it ambiguous.

    registration_error_used is copied from the COMPARISON side's cleanup
    stage `icp_rms` (Section 8: "copied from the source cleanup stage's
    icp_rms at the time Stage 5 ran") - the comparison cloud is the one
    that was actually ICP-aligned during its own Cleanup stage, so its
    icp_rms is the registration error this diff's M3C2 run should use.
    It's None if that cleanup run didn't record one (e.g. it wasn't
    aligned to anything) - callers should treat that the same way
    pipeline_core.py's parse_registration_rms() always has: as "can't run
    the significance test properly", never as license to substitute 0.
    This is read from `ref_stages`/`comp_stages`'s own `cleanup` entry
    regardless of which stage `*_source_stage` names - `segment` doesn't
    record its own `icp_rms`, it only adds a field to the same points
    Cleanup already aligned.
    """
    if handle.kind != "diff":
        raise ProjectError("get_diff_inputs() only applies to a diff pipeline handle.")

    project = handle.project
    diff_entry = handle.entry
    reference = diff_entry["reference"]
    comparison = diff_entry["comparison"]

    if reference == "baseline":
        ref_stages = project.data["baseline"]["stages"]
    else:
        scans = project.data.get("scans", {})
        if reference not in scans:
            raise ProjectError(f"This diff's reference '{reference}' is not 'baseline' "
                                f"or a known scan.")
        ref_stages = scans[reference]["stages"]

    scans = project.data.get("scans", {})
    if comparison not in scans:
        raise ProjectError(f"This diff's comparison '{comparison}' is not a known scan.")
    comp_stages = scans[comparison]["stages"]

    def _resolve_side(stages, side_label):
        segment_output = stages.get("segment", {}).get("output")
        if segment_output:
            return segment_output, "segment"
        cleanup_output = stages.get("cleanup", {}).get("output")
        if cleanup_output:
            return cleanup_output, "cleanup"
        raise ProjectError(
            f"This diff's {side_label} has no completed cleanup stage yet - "
            f"it must complete before this diff can run.")

    ref_output, ref_source = _resolve_side(ref_stages, f"reference ('{reference}')")
    comp_output, comp_source = _resolve_side(comp_stages, f"comparison ('{comparison}')")

    comp_cleanup = comp_stages.get("cleanup", {})

    return {
        "reference_path": str(project.root / ref_output),
        "comparison_path": str(project.root / comp_output),
        "reference_source_stage": ref_source,
        "comparison_source_stage": comp_source,
        "registration_error_used": comp_cleanup.get("icp_rms"),
    }


def get_baseline_cleanup_output(project):
    """
    Convenience for Stage 8 (Export)'s "environment" input, which is
    always the project's OWN baseline pipeline's cleanup output -
    regardless of which diff is being exported, since a diff's
    `reference` might itself be an earlier scan rather than the baseline,
    but the exported scene's static environment layer should still be
    the project's true baseline (PROJECT_SCHEMA_v2.md doesn't spell this
    out explicitly since Export sits outside what Section 11.3 defines,
    but it's the only reading consistent with Section 4's "baseline vs.
    scans vs. diffs" model - the environment doesn't change per-diff).

    Returns an absolute path (str). Raises ProjectError if the project's
    baseline hasn't completed its cleanup stage yet.
    """
    entry = project.data["baseline"]["stages"].get("cleanup", {})
    output = entry.get("output")
    if not output:
        raise ProjectError("This project's baseline has no completed cleanup stage yet.")
    return str(project.root / output)


def get_output_path(handle, stage_name, extension):
    """
    Builds the default output path for a stage, per PROJECT_SCHEMA_v2.md
    Section 13.1: <compartment>_<stage>_<sequence>.<extension>, with the
    sequence a 3-digit number counting existing files in THIS pipeline's
    own stage subfolder only (scanned directly from disk, not from an
    internal counter - Section 13.1 defines the sequence in terms of
    what's actually in the folder, which stays correct even if
    project.json and the folder ever drift apart). The scan/diff ID
    itself does not need to appear in the filename (Section 13.1) because
    the folder path already identifies which pipeline it belongs to.

    Returns a path RELATIVE to the project root (matching the format
    project.json itself stores in each stage's 'output' field) - use
    get_absolute_path() for an absolute path.
    """
    _validate_stage_name(handle.kind, stage_name)

    folder_name = handle.stage_folders[stage_name]
    folder = handle.root / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    extension = extension if extension.startswith(".") else f".{extension}"
    compartment = handle.project.data["compartment"]
    prefix = f"{compartment}_{stage_name}_"

    existing_numbers = []
    for f in folder.glob(f"{prefix}*{extension}"):
        stem = f.name[len(prefix):-len(extension)]
        if stem.isdigit():
            existing_numbers.append(int(stem))

    next_seq = (max(existing_numbers) + 1) if existing_numbers else 1
    filename = f"{prefix}{next_seq:03d}{extension}"
    rel_folder = _pipeline_folder_rel(handle.kind, handle.pipeline_id)
    return f"{rel_folder}/{folder_name}/{filename}"


def get_absolute_path(project, relative_path):
    """Convenience: joins a project-relative path (as stored in
    project.json, or returned by get_output_path) with the project root."""
    return str(project.root / relative_path)


def to_relative_path(project, path):
    """
    Public wrapper around the same project-root-relative normalization
    save_project()/complete_stage() already use internally for `output`
    and `log_path` - exposed for callers building extra_fields values
    that Section 8's field table also documents as relative paths (e.g.
    cleanup's `sidecar`, diff's `m3c2_params_file`), so those stay
    consistent with the rest of a stage entry instead of ending up as a
    bare absolute path.

    Raises ProjectError if `path` isn't inside the project root (e.g. a
    user-picked M3C2 params file saved somewhere else entirely) - callers
    with a value that's legitimately allowed to live outside the project
    should catch that and fall back to storing the absolute path instead
    of letting the whole operation fail over one non-relative field.
    """
    return _to_relative(project, path)


def _scan_stage_folder(handle, stage_name):
    """
    Returns every output file that actually exists in ONE stage's
    folder - not just the one currently recorded as that stage's
    "output" in project.json - sorted newest-first by sequence number.

    Shared scanning logic behind list_stage_outputs(),
    list_eligible_inputs(), and list_side_candidates()
    (PROJECT_INPUT_PICKER_PLAN.md Section 4.3) - callers already know
    stage_name is valid for handle.kind (list_eligible_inputs() draws
    it from handle.stage_names itself), so this does not re-validate it.

    Returns a list of dicts, newest (highest sequence) first:
        [{"path": "<relative path>", "sequence": <int>, "is_current": <bool>}, ...]
    "is_current" marks whichever one matches this stage's currently
    recorded "output" field in project.json - purely informational
    (PROJECT_INPUT_PICKER_PLAN.md Section 3: nothing gets pre-selected
    from this anymore), not necessarily the highest sequence number.
    """
    folder_name = handle.stage_folders[stage_name]
    folder = handle.root / folder_name
    if not folder.is_dir():
        return []

    compartment = handle.project.data["compartment"]
    prefix = f"{compartment}_{stage_name}_"
    current_output = handle.entry["stages"].get(stage_name, {}).get("output")
    rel_folder = _pipeline_folder_rel(handle.kind, handle.pipeline_id)

    results = []
    for f in sorted(folder.iterdir()):
        if not f.is_file() or not f.name.startswith(prefix):
            continue
        stem_after_prefix = f.stem[len(prefix):]
        if not stem_after_prefix.isdigit():
            continue
        rel_path = f"{rel_folder}/{folder_name}/{f.name}"
        results.append({
            "path": rel_path,
            "sequence": int(stem_after_prefix),
            "is_current": rel_path == current_output,
        })

    results.sort(key=lambda r: r["sequence"], reverse=True)
    return results


def list_stage_outputs(handle, stage_name):
    """
    Returns every output file that actually exists in a stage's folder -
    not just the one currently recorded as that stage's "output" in
    project.json - sorted newest-first by sequence number.

    Meant to back a GUI dropdown: "which of Level's outputs do you want
    to feed into Cleanup?" - rather than the pipeline silently picking one
    file behind the scenes (get_input_for_stage's job), this makes every
    past attempt visible and explicitly selectable. A file's sequence
    number IS its "pass" - Section 13.1's naming convention already
    encodes this, so no separate pass counter is needed.

    Returns a list of dicts, newest (highest sequence) first:
        [{"path": "<relative path>", "sequence": <int>, "is_current": <bool>}, ...]
    "is_current" marks whichever one matches this stage's currently
    recorded "output" field in project.json (a sensible dropdown default,
    for a caller that still wants one), not necessarily the highest
    sequence number.
    """
    _validate_stage_name(handle.kind, stage_name)
    return _scan_stage_folder(handle, stage_name)


def list_eligible_inputs(handle, stage_name):
    """
    Lists every file this pipeline's own EARLIER stages have produced -
    everything stage_name could actually be pointed at as its input,
    grouped by which stage produced it (PROJECT_INPUT_PICKER_PLAN.md
    Section 4.1). Meant to back a GUI picker: the user picks the exact
    file to use, instead of a stage always being pointed automatically
    at the immediately-previous stage's current output.

    Returns a list of group dicts, one per earlier stage that has at
    least one file on disk, in this pipeline's own stage order (earliest
    first) - a stage with nothing on disk yet contributes no group:
        {
            "pipeline_kind": handle.kind,
            "pipeline_id": handle.pipeline_id,
            "stage_name": <an earlier stage's name, or "raw">,
            "files": [{"path": ..., "sequence": ..., "is_current": ...}, ...],
        }
    Deliberately returns raw kind/stage identifiers, not display
    strings - formatting a label from these is a GUI concern, kept out
    of this module by design (module docstring).

    If stage_name is the first stage of a baseline or scan pipeline
    ("slam"), there is no earlier STAGE - instead, one group is
    returned for the pipeline's raw import: stage_name "raw", holding
    the original import and, if one has been recorded via
    set_decoded_raw_path(), the decoded form too. Each file dict in
    that group has "sequence": None and "is_current": None (a raw
    import isn't numbered like a stage output) and a "note" of
    "original import" or "decoded" instead.

    Raises ProjectError if stage_name is a diff pipeline's first stage
    ("diff") - that stage's two inputs come from two DIFFERENT
    pipelines, not from earlier stages of handle's own pipeline. Use
    list_side_candidates() for each side of that stage instead.
    """
    _validate_stage_name(handle.kind, stage_name)
    stage_names = handle.stage_names
    idx = stage_names.index(stage_name)

    if idx == 0:
        if handle.kind == "diff":
            raise ProjectError(
                "Stage 'diff' takes two inputs from two different pipelines "
                "(its reference and comparison) - use list_side_candidates() "
                "for each side instead of list_eligible_inputs().")
        raw = handle.entry.get("raw")
        if not raw or not raw.get("path"):
            return []
        files = [{"path": raw["path"], "sequence": None, "is_current": None,
                  "note": "original import"}]
        if raw.get("decoded_path"):
            files.append({"path": raw["decoded_path"], "sequence": None,
                           "is_current": None, "note": "decoded"})
        return [{
            "pipeline_kind": handle.kind,
            "pipeline_id": handle.pipeline_id,
            "stage_name": "raw",
            "files": files,
        }]

    groups = []
    for earlier_stage in stage_names[:idx]:
        files = _scan_stage_folder(handle, earlier_stage)
        if files:
            groups.append({
                "pipeline_kind": handle.kind,
                "pipeline_id": handle.pipeline_id,
                "stage_name": earlier_stage,
                "files": files,
            })
    return groups


def list_side_candidates(project, pipeline_ref):
    """
    Lists Cleanup and Segment output files for ONE baseline/scan
    pipeline, named by pipeline_ref ("baseline", or a known scan ID) -
    PROJECT_INPUT_PICKER_PLAN.md Section 4.2. Used for a diff's
    Reference/Comparison inputs (each side's pipeline is already fixed
    at add_diff() time - only the STAGE within it needs picking) and
    for Export's Baseline/environment input (pipeline_ref is always
    "baseline" there).

    Returns a list of group dicts, same shape as list_eligible_inputs()
    - one group each for "cleanup" and "segment", in that order, skipped
    if empty. 0, 1, or 2 groups.

    Raises ProjectError if pipeline_ref is not "baseline" or a known
    scan ID.
    """
    if pipeline_ref == "baseline":
        handle = project.baseline_handle()
    else:
        if pipeline_ref not in project.data.get("scans", {}):
            raise ProjectError(
                f"pipeline_ref must be 'baseline' or a known scan ID, got '{pipeline_ref}'")
        handle = project.scan_handle(pipeline_ref)

    groups = []
    for stage_name in ("cleanup", "segment"):
        files = _scan_stage_folder(handle, stage_name)
        if files:
            groups.append({
                "pipeline_kind": handle.kind,
                "pipeline_id": handle.pipeline_id,
                "stage_name": stage_name,
                "files": files,
            })
    return groups


def find_icp_rms_for_path(project, path):
    """
    Looks up the recorded icp_rms for a Cleanup stage output, given its
    path (absolute or already project-relative) - a direct dict lookup
    over the project's baseline and every scan, since a Cleanup stage's
    own entry already names its own output path; not a filesystem
    search. Backs the Diff dialog's Registration RMS auto-fill when a
    comparison file is picked via list_side_candidates()
    (PROJECT_INPUT_PICKER_PLAN.md Section 6.1).

    Returns the RMS (a float), or None if `path` isn't inside this
    project, isn't a recorded Cleanup output, or that Cleanup run has
    no icp_rms recorded on it (e.g. it wasn't aligned to anything).
    """
    try:
        rel = _to_relative(project, path)
    except ProjectError:
        return None
    candidates = [project.data["baseline"]]
    candidates.extend(project.data.get("scans", {}).values())
    for entry in candidates:
        cleanup = entry.get("stages", {}).get("cleanup", {})
        if cleanup.get("output") == rel:
            return cleanup.get("icp_rms")
    return None


def find_next_stage(handle):
    """
    Finds the next stage to run for this pipeline (PROJECT_SCHEMA_v2.md
    Section 8.3): the first stage, in this pipeline's own stage order,
    whose status is not 'complete'. Returns None if every stage in this
    pipeline is already complete.

    Not a stored field - Version 2 deliberately dropped the old
    top-level `current_stage` field (Section 7) because a project can
    have many independent pipelines in flight at once, so there's no
    single "next stage" for the whole project, only for one pipeline at
    a time. This is that per-pipeline query, computed on read.
    """
    for name in handle.stage_names:
        if handle.entry["stages"].get(name, {}).get("status") != "complete":
            return name
    return None


def start_stage(handle, stage_name, params=None):
    """
    Sets the stage's status to 'running', records the start time, and
    saves. If this stage was already 'complete' (i.e. this is a re-run),
    increments rerun_count and overwrites the old entry - per
    PROJECT_SCHEMA_v2.md Section 8.2, re-running does not create a
    second, separate record.
    """
    _validate_stage_name(handle.kind, stage_name)
    entry = handle.entry
    stage_entry = entry["stages"].get(stage_name, {"status": "not_started"})
    is_rerun = stage_entry.get("status") == "complete"
    rerun_count = stage_entry.get("rerun_count", 0) + (1 if is_rerun else 0)

    new_entry = {
        "status": "running",
        "started": _now_iso(),
        "rerun_count": rerun_count,
    }
    if params:
        new_entry["params"] = params

    entry["stages"][stage_name] = new_entry
    save_project(handle.project)


def complete_stage(handle, stage_name, output_path, extra_fields=None, log_path=None):
    """
    Sets the stage's status to 'complete', records the output path
    (stored relative to the project root) and completion time, optionally
    records log_path (Section 8's documented field, relative to the
    project root, e.g. an annotated CloudCompare log), and merges in any
    stage-specific extra fields (e.g. icp_rms/sidecar for cleanup,
    m3c2_params_file/registration_error_used for diff, source_type/
    point_spacing for slam).

    Unlike Version 1's complete_stage(), this does NOT advance any
    top-level "current stage" pointer - Version 2 has no such field
    (Section 7). Call find_next_stage(handle) when the next stage to run
    is actually needed.
    """
    _validate_stage_name(handle.kind, stage_name)
    entry = handle.entry
    stage_entry = entry["stages"].setdefault(stage_name, {})
    stage_entry["status"] = "complete"
    stage_entry["completed"] = _now_iso()
    stage_entry["output"] = _to_relative(handle.project, output_path)
    if log_path:
        stage_entry["log_path"] = _to_relative(handle.project, log_path)
    if extra_fields:
        stage_entry.update(extra_fields)

    save_project(handle.project)


def fail_stage(handle, stage_name, error_message=None):
    """
    Sets the stage's status to 'failed'. Does not touch any "next stage"
    tracking (Section 8.1: "A failed run does not let a later stage run
    early") - so a user can fix whatever went wrong and re-run the same
    stage without anything thinking it's ready to move on.
    """
    _validate_stage_name(handle.kind, stage_name)
    entry = handle.entry
    stage_entry = entry["stages"].setdefault(stage_name, {})
    stage_entry["status"] = "failed"
    if error_message:
        stage_entry["error"] = str(error_message)
    save_project(handle.project)


def promote_baseline(project, stage_name):
    """
    STUB - per PROJECT_INTEGRATION_PLAN.md Section 2.1 (written against
    Version 1, but the plan still holds), this connects to the
    compartment registry in a later phase (that system doesn't exist
    yet - PROJECT_SCHEMA_v2.md Section 3.1 confirms it's still a separate,
    out-of-scope system). For now this only updates the project's OWN
    `baseline` object fields (Section 9) - it does not write anywhere
    outside this project.

    Always applies to the project's baseline pipeline specifically (a
    scan can't be promoted - only a project's one baseline can, per
    Section 9's promotion fields), so this takes a Project rather than a
    PipelineHandle.
    """
    _validate_stage_name("baseline", stage_name)
    baseline = project.data["baseline"]
    stage_entry = baseline["stages"].get(stage_name)
    if not stage_entry or stage_entry.get("status") != "complete":
        raise ProjectError(f"Cannot promote '{stage_name}' - it has not completed.")

    baseline["promoted"] = True
    baseline["promoted_stage"] = stage_name
    baseline["promoted_timestamp"] = _now_iso()
    save_project(project)


if __name__ == "__main__":
    print(__doc__)
    print(
        "This file is a library module - it's meant to be imported by other "
        "scripts (test_project_manager.py, and pipeline_core.py / "
        "pipeline_applet.py), not run directly. Running it this way defines "
        "everything above but doesn't DO anything, which is why a double-click "
        "just opens and immediately closes a console window - nothing crashed, "
        "there's just nothing here that acts on its own.\n"
        "\n"
        "To actually see this module do something, run:\n"
        "    python test_project_manager.py\n"
    )
