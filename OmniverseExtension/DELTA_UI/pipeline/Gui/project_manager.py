#!/usr/bin/env python3
"""Manage baseline, inspection and comparison project records."""

import json
import re
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 3

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

RAW_FOLDER = "00_raw"

BASELINE_SCAN_STAGE_FOLDERS = {
    "slam": "01_slam",
    "level": "02_level",
    "cleanup": "03_cleanup",
    "segment": "04_segment",
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
    """Wraps a project's JSON-serializable data (`.data`, matching PROJECT_SCHEMA_v2.md's top-level fields) together with its on-disk root folder (`.root`)."""

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
        return (
            f"Project(project_id={self.data.get('project_id')!r}, "
            f"scans={list(self.data.get('scans', {}).keys())!r}, "
            f"diffs={list(self.data.get('diffs', {}).keys())!r}, "
            f"root={self.root!r})"
        )


class PipelineHandle:
    """A pointer at exactly one of a project's pipelines: the baseline, one scan, or one diff (PROJECT_SCHEMA_v2.md Section 8's shared `stages` shape, scoped to whichever one this handle points at)."""

    def __init__(self, project, kind, pipeline_id=None):
        if kind not in VALID_PIPELINE_KINDS:
            raise ProjectError(
                f"Unknown pipeline kind '{kind}'. Must be one of: {sorted(VALID_PIPELINE_KINDS)}"
            )
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
        label = (
            self.kind if self.pipeline_id is None else f"{self.kind}:{self.pipeline_id}"
        )
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
    raise ProjectError(
        f"Unknown pipeline kind '{kind}'. Must be one of: {sorted(VALID_PIPELINE_KINDS)}"
    )


def _stage_folder_map_for_kind(kind):
    if kind in ("baseline", "scan"):
        return BASELINE_SCAN_STAGE_FOLDERS
    if kind == "diff":
        return DIFF_STAGE_FOLDERS
    raise ProjectError(
        f"Unknown pipeline kind '{kind}'. Must be one of: {sorted(VALID_PIPELINE_KINDS)}"
    )


def _validate_stage_name(kind, stage_name):
    names = _stage_names_for_kind(kind)
    if stage_name not in names:
        raise ProjectError(
            f"Unknown stage '{stage_name}' for kind='{kind}'. Must be one of: {names}"
        )


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
    raise ProjectError(
        f"Unknown pipeline kind '{kind}'. Must be one of: {sorted(VALID_PIPELINE_KINDS)}"
    )


def _to_relative(project, path):
    """Normalizes a path (absolute or already-relative) to a project-root- relative string with forward slashes, matching PROJECT_SCHEMA_v2.md's own examples (e.g."""
    path = Path(path)
    if path.is_absolute():
        try:
            path = path.relative_to(project.root)
        except ValueError:
            raise ProjectError(
                f"Path {path} is not inside the project root {project.root}"
            )
    return str(path).replace(os.sep, "/")


def _copy_source(source_path, dest_path):
    if source_path.is_dir():
        shutil.copytree(source_path, dest_path)
    else:
        shutil.copy2(source_path, dest_path)


def _import_raw_source(source_path, dest_path, link_raw):
    """link_raw: "copy" (default - safe, doesn't touch the original, always works), "move" (relocates the original into the project - good for large captures you don't need to keep elsewhere; shutil.move handles both files."""
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


def create_project(
    root_dir, compartment_name, source_path, source_type, link_raw="copy"
):
    """Creates the project root folder, the baseline/ subfolder structure (00_raw/ through 04_segment/, Section 5), imports the raw source into baseline/00_raw/, writes the first project.json (with empty `scans` and `diffs`."""
    if source_type not in VALID_SOURCE_TYPES:
        raise ProjectError(
            f"source_type must be one of {sorted(VALID_SOURCE_TYPES)}, "
            f"got '{source_type}'"
        )

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
            "stages": {
                name: {"status": "not_started"} for name in BASELINE_SCAN_STAGE_NAMES
            },
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
    """Adds a new comparison scan (Section 10): creates scans/<scan_id>/00_raw/ through 04_segment/, imports the raw source, and adds a `scans[<scan_id>]` entry with its own `raw` and `stages`."""
    if source_type not in VALID_SOURCE_TYPES:
        raise ProjectError(
            f"source_type must be one of {sorted(VALID_SOURCE_TYPES)}, "
            f"got '{source_type}'"
        )

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
        "stages": {
            name: {"status": "not_started"} for name in BASELINE_SCAN_STAGE_NAMES
        },
    }
    save_project(project)
    return scan_id


def add_diff(project, label, reference, comparison):
    """Adds a new diff pipeline (Section 11): creates diffs/<diff_id>/05_diff/ through 08_export/, and adds a `diffs[<diff_id>]` entry naming which two things it compares."""
    if reference != "baseline" and reference not in project.data.get("scans", {}):
        raise ProjectError(
            f"reference must be the string 'baseline' or an existing scan ID, got '{reference}'"
        )
    if comparison not in project.data.get("scans", {}):
        raise ProjectError(
            f"comparison must be an existing scan ID, got '{comparison}'"
        )

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
            f"expects {SCHEMA_VERSION}. There is no automatic migration between "
            f"schema versions - a project created under an older version needs "
            f"to be re-imported by hand as a new baseline. (Version 1 -> 2: "
            f"PROJECT_SCHEMA_v2.md Section 16. Version 2 -> 3: baseline/scan "
            f"stage folder numbers changed - raw import moved from '01_raw' to "
            f"'00_raw' and every stage folder shifted down by one to match its "
            f"own stage number directly; deliberately not migrated, since every "
            f"schema_version 2 project so far has been test data.)"
        )

    return Project(data, root)


def save_project(project):
    """Writes the project back to project.json, updating the 'updated' timestamp."""
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
    """Get input for stage."""
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
    """Records a decoded form of this pipeline's raw import (e.g."""
    if handle.kind not in ("baseline", "scan"):
        raise ProjectError(
            f"set_decoded_raw_path() only applies to a baseline or scan pipeline "
            f"(kind='{handle.kind}' has no raw import to decode)."
        )
    raw = handle.entry.get("raw")
    if not raw or not raw.get("path"):
        raise ProjectError(
            "No raw source recorded for this pipeline - was it created "
            "with create_project() or add_scan()?"
        )
    raw["decoded_path"] = _to_relative(handle.project, decoded_path)
    save_project(handle.project)


def get_diff_inputs(handle):
    """Resolves a diff pipeline's two source clouds (Section 11.3): the `reference` side (either the project's baseline, or a named scan) and the `comparison` side (always a named scan)."""
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
            raise ProjectError(
                f"This diff's reference '{reference}' is not 'baseline' "
                f"or a known scan."
            )
        ref_stages = scans[reference]["stages"]

    scans = project.data.get("scans", {})
    if comparison not in scans:
        raise ProjectError(
            f"This diff's comparison '{comparison}' is not a known scan."
        )
    comp_stages = scans[comparison]["stages"]

    def _resolve_side(stages, side_label):
        segment = stages.get("segment", {})
        segment_output = segment.get("output")
        if segment_output:
            filtered = segment.get("envelope_filtered_output")
            if filtered and (project.root / filtered).is_file():
                return filtered, "segment"
            return segment_output, "segment"
        cleanup_output = stages.get("cleanup", {}).get("output")
        if cleanup_output:
            return cleanup_output, "cleanup"
        raise ProjectError(
            f"This diff's {side_label} has no completed cleanup stage yet - "
            f"it must complete before this diff can run."
        )

    ref_output, ref_source = _resolve_side(ref_stages, f"reference ('{reference}')")
    comp_output, comp_source = _resolve_side(
        comp_stages, f"comparison ('{comparison}')"
    )

    comp_cleanup = comp_stages.get("cleanup", {})

    return {
        "reference_path": str(project.root / ref_output),
        "comparison_path": str(project.root / comp_output),
        "reference_source_stage": ref_source,
        "comparison_source_stage": comp_source,
        "reference_outside_removed": is_envelope_filtered_path(ref_output),
        "comparison_outside_removed": is_envelope_filtered_path(comp_output),
        "registration_error_used": comp_cleanup.get("icp_rms"),
    }


def is_envelope_filtered_path(path):
    """True if path is a Stage 4 (Segment) copy with the points outside
    the room removed (<name>_envelope_filtered.ply)."""
    return Path(str(path)).name.endswith("envelope_filtered.ply")


def envelope_filtered_sibling(path):
    """For a Stage 4 (Segment) <name>_classified.ply, returns the Path of
    the same pass's <name>_envelope_filtered.ply if that file exists,
    otherwise None. Used by Stage 5 (Diff) to offer the copy without the
    points outside the room."""
    path = Path(str(path))
    if not path.name.endswith("_classified.ply"):
        return None
    sibling = path.with_name(
        path.name[: -len("_classified.ply")] + "_envelope_filtered.ply"
    )
    return sibling if sibling.is_file() else None


def get_baseline_cleanup_output(project):
    """Get baseline cleanup output."""
    entry = project.data["baseline"]["stages"].get("cleanup", {})
    output = entry.get("output")
    if not output:
        raise ProjectError(
            "This project's baseline has no completed cleanup stage yet."
        )
    return str(project.root / output)


def get_output_path(handle, stage_name, extension):
    """Builds the default output path for a stage, per PROJECT_SCHEMA_v2.md Section 13.1: <compartment>_<stage>_<sequence>.<extension>, with the sequence a 3-digit number counting existing files in THIS pipeline's own stage."""
    _validate_stage_name(handle.kind, stage_name)

    folder_name = handle.stage_folders[stage_name]
    folder = handle.root / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    extension = extension if extension.startswith(".") else f".{extension}"
    compartment = handle.project.data["compartment"]
    prefix = f"{compartment}_{stage_name}_"

    existing_numbers = []
    for f in folder.glob(f"{prefix}*{extension}"):
        stem = f.name[len(prefix) : -len(extension)]
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
    """Public wrapper around the same project-root-relative normalization save_project()/complete_stage() already use internally for `output` and `log_path` - exposed for callers building extra_fields values that Section."""
    return _to_relative(project, path)


def _scan_stage_folder(handle, stage_name, include_envelope_filtered=False):
    """Returns every output file that actually exists in ONE stage's folder - not just the one currently recorded as that stage's "output" in project.json - sorted newest-first by sequence number."""
    folder_name = handle.stage_folders[stage_name]
    folder = handle.root / folder_name
    if not folder.is_dir():
        return []

    compartment = handle.project.data["compartment"]
    prefix = f"{compartment}_{stage_name}_"
    current_output = handle.entry["stages"].get(stage_name, {}).get("output")
    rel_folder = _pipeline_folder_rel(handle.kind, handle.pipeline_id)

    if stage_name == "segment":
        results = []
        for sub in sorted(folder.iterdir()):
            if not sub.is_dir() or not sub.name.startswith(prefix):
                continue
            stem_after_prefix = sub.name[len(prefix) :]
            if not stem_after_prefix.isdigit():
                continue
            classified = sub / f"{sub.name}_classified.ply"
            if not classified.is_file():
                continue
            rel_path = f"{rel_folder}/{folder_name}/{sub.name}/{classified.name}"
            filtered = sub / f"{sub.name}_envelope_filtered.ply"
            if include_envelope_filtered and filtered.is_file():
                results.append(
                    {
                        "path": f"{rel_folder}/{folder_name}/{sub.name}/{filtered.name}",
                        "sequence": int(stem_after_prefix),
                        "is_current": False,
                        "note": "outside-room points removed",
                        "order": 0,
                    }
                )
            results.append(
                {
                    "path": rel_path,
                    "sequence": int(stem_after_prefix),
                    "is_current": rel_path == current_output,
                    "order": 1,
                }
            )
        # Newest pass first; inside one pass, the filtered copy first.
        results.sort(key=lambda r: (-r["sequence"], r["order"]))
        for r in results:
            del r["order"]
        return results

    results = []
    others = []
    for f in sorted(folder.iterdir()):
        if not f.is_file():
            continue
        stem_after_prefix = f.stem[len(prefix) :] if f.name.startswith(prefix) else ""
        rel_path = f"{rel_folder}/{folder_name}/{f.name}"
        if stem_after_prefix.isdigit():
            results.append(
                {
                    "path": rel_path,
                    "sequence": int(stem_after_prefix),
                    "is_current": rel_path == current_output,
                }
            )
        elif _is_hand_named_ply(f, prefix):
            others.append(
                (
                    f.stat().st_mtime,
                    {
                        "path": rel_path,
                        "sequence": None,
                        "is_current": rel_path == current_output,
                        "note": "other file (renamed or added by hand)",
                    },
                )
            )

    results.sort(key=lambda r: r["sequence"], reverse=True)
    others.sort(key=lambda item: item[0], reverse=True)
    results.extend(entry for _, entry in others)
    return results


# CloudCompare saves extra copies of the clouds that it loaded next to the real
# output. Their names are the output name plus a time stamp or "M3C2". They are
# not outputs, so the picker does not offer them.
_CLOUDCOMPARE_EXTRA = re.compile(r"(M3C2|\d{4}[-_]\d{2}[-_]\d{2})", re.IGNORECASE)


def _is_hand_named_ply(path, prefix):
    """True for a .ply file in a stage folder that does not have the standard
    numbered name and is not a CloudCompare extra copy."""
    if path.suffix.lower() != ".ply":
        return False
    if path.name.startswith(prefix):
        rest = path.stem[len(prefix) :]
        digits = len(rest) - len(rest.lstrip("0123456789"))
        if digits and _CLOUDCOMPARE_EXTRA.search(rest[digits:]):
            return False
    return True


def list_stage_outputs(handle, stage_name):
    """Returns every output file that actually exists in a stage's folder - not just the one currently recorded as that stage's "output" in project.json - sorted newest-first by sequence number."""
    _validate_stage_name(handle.kind, stage_name)
    return _scan_stage_folder(handle, stage_name)


def list_eligible_inputs(handle, stage_name):
    """Lists every file this pipeline's own EARLIER stages have produced - everything stage_name could actually be pointed at as its input, grouped by which stage produced it (PROJECT_INPUT_PICKER_PLAN.md Section 4.1)."""
    _validate_stage_name(handle.kind, stage_name)
    stage_names = handle.stage_names
    idx = stage_names.index(stage_name)

    if idx == 0:
        if handle.kind == "diff":
            raise ProjectError(
                "Stage 'diff' takes two inputs from two different pipelines "
                "(its reference and comparison) - use list_side_candidates() "
                "for each side instead of list_eligible_inputs()."
            )
        raw = handle.entry.get("raw")
        if not raw or not raw.get("path"):
            return []
        files = [
            {
                "path": raw["path"],
                "sequence": None,
                "is_current": None,
                "note": "original import",
            }
        ]
        if raw.get("decoded_path"):
            files.append(
                {
                    "path": raw["decoded_path"],
                    "sequence": None,
                    "is_current": None,
                    "note": "decoded",
                }
            )
        return [
            {
                "pipeline_kind": handle.kind,
                "pipeline_id": handle.pipeline_id,
                "stage_name": "raw",
                "files": files,
            }
        ]

    groups = []
    for earlier_stage in stage_names[:idx]:
        files = _scan_stage_folder(handle, earlier_stage)
        if files:
            groups.append(
                {
                    "pipeline_kind": handle.kind,
                    "pipeline_id": handle.pipeline_id,
                    "stage_name": earlier_stage,
                    "files": files,
                }
            )
    return groups


def list_side_candidates(project, pipeline_ref):
    """Lists Cleanup and Segment output files for ONE baseline/scan pipeline, named by pipeline_ref ("baseline", or a known scan ID) - PROJECT_INPUT_PICKER_PLAN.md Section 4.2."""
    if pipeline_ref == "baseline":
        handle = project.baseline_handle()
    else:
        if pipeline_ref not in project.data.get("scans", {}):
            raise ProjectError(
                f"pipeline_ref must be 'baseline' or a known scan ID, got '{pipeline_ref}'"
            )
        handle = project.scan_handle(pipeline_ref)

    groups = []
    for stage_name in ("cleanup", "segment"):
        files = _scan_stage_folder(handle, stage_name, include_envelope_filtered=True)
        if files:
            groups.append(
                {
                    "pipeline_kind": handle.kind,
                    "pipeline_id": handle.pipeline_id,
                    "stage_name": stage_name,
                    "files": files,
                }
            )
    return groups


def find_icp_rms_for_path(project, path):
    """Looks up the recorded icp_rms for a Cleanup stage output, given its path (absolute or already project-relative) - a direct dict lookup over the project's baseline and every scan, since a Cleanup stage's own entry."""
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
    """Finds the next stage to run for this pipeline (PROJECT_SCHEMA_v2.md Section 8.3): the first stage, in this pipeline's own stage order, whose status is not 'complete'."""
    for name in handle.stage_names:
        if handle.entry["stages"].get(name, {}).get("status") != "complete":
            return name
    return None


def start_stage(handle, stage_name, params=None):
    """Sets the stage's status to 'running', records the start time, and saves."""
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
    """Sets the stage's status to 'complete', records the output path (stored relative to the project root) and completion time, optionally records log_path (Section 8's documented field, relative to the project root, e.g."""
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
    """STUB - per PROJECT_INTEGRATION_PLAN.md Section 2.1 (written against Version 1, but the plan still holds), this connects to the compartment registry in a later phase (that system doesn't exist yet -."""
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
        "the Qt processing app), not run directly. Running it this way defines "
        "everything above but doesn't DO anything, which is why a double-click "
        "just opens and immediately closes a console window - nothing crashed, "
        "there's just nothing here that acts on its own.\n"
        "\n"
        "To actually see this module do something, run:\n"
        "    python test_project_manager.py\n"
    )
