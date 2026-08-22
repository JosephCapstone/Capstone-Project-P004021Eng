"""
test_project_manager.py
==========================
Real end-to-end exercise of project_manager.py's full Schema v2 lifecycle
- not just isolated logic checks, since this module has no external
dependencies and can actually be run directly. Creates a real temp
project, adds a scan and a diff, runs each pipeline through several
stages (including a failure and a re-run), and asserts on the resulting
project.json at each point.

REWRITTEN FOR SCHEMA V2 (PROJECT_SCHEMA_v2.md): the old version of this
file tested Version 1's flat `stages` + `current_stage` shape. Version 2
has no such thing - state lives under `baseline`, `scans`, and `diffs`,
each reached through a PipelineHandle. This file's structure mirrors
that: it exercises the baseline pipeline, then adds a scan and drives its
own slam/level/cleanup, then adds a diff comparing that scan to the
baseline and drives diff/classify/surface/export.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))
import project_manager as pm


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        raise AssertionError(label)


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)

    # --- Set up fake raw source files to import ---
    fake_source_dir = tmp / "incoming"
    fake_source_dir.mkdir()
    fake_pcap = fake_source_dir / "capture.pcap"
    fake_pcap.write_bytes(b"fake pcap bytes")

    projects_root = tmp / "projects"
    projects_root.mkdir()

    print("=== create_project (baseline) ===")
    project = pm.create_project(
        root_dir=projects_root,
        compartment_name="compartment_04",
        source_path=fake_pcap,
        source_type="pcap",
    )
    check("project_id format", project.data["project_id"].startswith("compartment_04_"))
    check("schema_version is 2", project.data["schema_version"] == 2)
    check("no top-level current_stage field", "current_stage" not in project.data)
    check("no top-level stages field", "stages" not in project.data)
    check("scans starts empty", project.data["scans"] == {})
    check("diffs starts empty", project.data["diffs"] == {})
    check("all baseline stages initialized not_started (slam/level/cleanup/segment)",
          all(project.data["baseline"]["stages"][s]["status"] == "not_started"
              for s in pm.BASELINE_SCAN_STAGE_NAMES))
    check("project root folder exists", project.root.is_dir())
    check("baseline folder exists", (project.root / "baseline").is_dir())
    for folder in [pm.RAW_FOLDER] + list(pm.BASELINE_SCAN_STAGE_FOLDERS.values()):
        check(f"baseline folder created: {folder}", (project.root / "baseline" / folder).is_dir())
    check("raw source imported into baseline/01_raw/",
          (project.root / "baseline" / "01_raw" / "capture.pcap").exists())
    check("project.json written to disk", (project.root / "project.json").exists())
    check("baseline.is_baseline_candidate default True",
          project.data["baseline"]["is_baseline_candidate"] is True)
    check("baseline.promoted default False", project.data["baseline"]["promoted"] is False)

    baseline = project.baseline_handle()
    check("baseline_handle kind", baseline.kind == "baseline")

    print("\n=== get_input_for_stage (baseline slam, reads raw) ===")
    slam_input = pm.get_input_for_stage(baseline, "slam")
    check("slam input resolves to imported raw file",
          slam_input == str(project.root / "baseline" / "01_raw" / "capture.pcap"))

    print("\n=== set_decoded_raw_path switches slam input to the decoded form ===")
    fake_decoded_bag = project.root / "baseline" / "01_raw" / "capture_decoded"
    fake_decoded_bag.mkdir()
    pm.set_decoded_raw_path(baseline, fake_decoded_bag)
    check("raw.path is untouched (still the true original import)",
          project.data["baseline"]["raw"]["path"] == "baseline/01_raw/capture.pcap")
    check("raw.decoded_path recorded, relative to project root",
          project.data["baseline"]["raw"]["decoded_path"] == "baseline/01_raw/capture_decoded")
    check("slam input now resolves to the decoded bag, not the raw import",
          pm.get_input_for_stage(baseline, "slam") == str(fake_decoded_bag))

    print("\n=== get_output_path (baseline slam, first run) ===")
    slam_output_rel = pm.get_output_path(baseline, "slam", ".ply")
    check("first output is sequence 001",
          slam_output_rel == "baseline/02_slam/compartment_04_slam_001.ply")

    print("\n=== start_stage -> complete_stage (baseline slam) ===")
    pm.start_stage(baseline, "slam", params={"voxel_size": 0.25})
    check("status running after start_stage", baseline.entry["stages"]["slam"]["status"] == "running")
    check("rerun_count 0 on first run", baseline.entry["stages"]["slam"]["rerun_count"] == 0)

    slam_output_abs = project.root / slam_output_rel
    slam_output_abs.write_bytes(b"fake ply data")
    pm.complete_stage(baseline, "slam", slam_output_abs, extra_fields={
        "source_type": "pcap", "point_spacing": 0.021
    })
    check("status complete after complete_stage", baseline.entry["stages"]["slam"]["status"] == "complete")
    check("find_next_stage advances to level", pm.find_next_stage(baseline) == "level")
    check("output stored as relative path with forward slashes",
          baseline.entry["stages"]["slam"]["output"] == "baseline/02_slam/compartment_04_slam_001.ply")
    check("extra_fields merged in (point_spacing)",
          baseline.entry["stages"]["slam"]["point_spacing"] == 0.021)

    print("\n=== get_input_for_stage (baseline level, reads slam's output) ===")
    level_input = pm.get_input_for_stage(baseline, "level")
    check("level input resolves to slam's output", level_input == str(slam_output_abs))

    print("\n=== fail_stage (baseline level) ===")
    pm.start_stage(baseline, "level")
    pm.fail_stage(baseline, "level", error_message="no planes found")
    check("status failed", baseline.entry["stages"]["level"]["status"] == "failed")
    check("find_next_stage did NOT advance past level", pm.find_next_stage(baseline) == "level")
    check("error message recorded", baseline.entry["stages"]["level"]["error"] == "no planes found")

    print("\n=== re-run level after fixing the problem ===")
    pm.start_stage(baseline, "level", params={"distance_threshold": 0.1})
    level_output_rel = pm.get_output_path(baseline, "level", ".ply")
    level_output_abs = project.root / level_output_rel
    level_output_abs.write_bytes(b"fake leveled ply")
    pm.complete_stage(baseline, "level", level_output_abs, log_path=level_output_abs)
    check("find_next_stage advances to cleanup", pm.find_next_stage(baseline) == "cleanup")
    check("level entry is a single, overwritten record (no stray failed data)",
          baseline.entry["stages"]["level"]["status"] == "complete")
    check("log_path recorded relative",
          baseline.entry["stages"]["level"]["log_path"] == level_output_rel)

    print("\n=== rerun_count increments on a genuine re-run of a COMPLETE stage ===")
    pm.start_stage(baseline, "level")  # level was already complete - this is a real rerun
    check("rerun_count incremented to 1", baseline.entry["stages"]["level"]["rerun_count"] == 1)
    pm.complete_stage(baseline, "level", level_output_abs)  # put it back to complete

    print("\n=== get_output_path sequence numbering (second slam-like run in same folder) ===")
    (project.root / "baseline" / "02_slam" / "compartment_04_slam_002.ply").write_bytes(b"x")
    next_seq = pm.get_output_path(baseline, "slam", ".ply")
    check("sequence correctly continues from existing files (003)",
          next_seq == "baseline/02_slam/compartment_04_slam_003.ply")

    print("\n=== complete baseline cleanup (needed for scans/diffs below) ===")
    pm.start_stage(baseline, "cleanup")
    cleanup_output_rel = pm.get_output_path(baseline, "cleanup", ".ply")
    (project.root / cleanup_output_rel).write_bytes(b"fake baseline cleanup")
    pm.complete_stage(baseline, "cleanup", project.root / cleanup_output_rel,
                       extra_fields={"icp_rms": 0.0184})
    check("baseline cleanup complete, next stage is segment", pm.find_next_stage(baseline) == "segment")
    check("get_baseline_cleanup_output resolves",
          pm.get_baseline_cleanup_output(project) == str(project.root / cleanup_output_rel))

    print("\n=== complete baseline segment (new stage, after cleanup) ===")
    segment_input = pm.get_input_for_stage(baseline, "segment")
    check("segment input resolves to cleanup's output (not diff/level)",
          segment_input == str(project.root / cleanup_output_rel))
    pm.start_stage(baseline, "segment", params={"distance_threshold": 0.02, "max_planes": 10})
    segment_output_rel = pm.get_output_path(baseline, "segment", ".ply")
    check("segment output path lives under baseline/05_segment/",
          segment_output_rel == "baseline/05_segment/compartment_04_segment_001.ply")
    # The real applet uses a FOLDER for this stage's actual output (PROJECT_SCHEMA_v2.md
    # Section 13.3) - project_manager.py itself doesn't need to know that; it just
    # records whatever path string complete_stage() is given, same as any other stage.
    segment_classified_rel = "baseline/05_segment/compartment_04_segment_001/classified.ply"
    (project.root / segment_classified_rel).parent.mkdir(parents=True, exist_ok=True)
    (project.root / segment_classified_rel).write_bytes(b"fake classified cloud")
    pm.complete_stage(baseline, "segment", project.root / segment_classified_rel, extra_fields={
        "classification_ids": {"0": "unclassified", "1": "floor", "2": "wall_1"},
        "envelope_output": "baseline/05_segment/compartment_04_segment_001/envelope.ply",
        "surfaces": [{"name": "floor", "file": "baseline/05_segment/compartment_04_segment_001/floor.ply",
                      "point_count": 1000, "normal": [0.0, 0.0, 1.0], "z_min": 0.0, "z_max": 0.01}],
    })
    check("baseline fully complete once segment is done", pm.find_next_stage(baseline) is None)
    check("segment output recorded", baseline.entry["stages"]["segment"]["output"] == segment_classified_rel)
    check("segment classification_ids recorded",
          baseline.entry["stages"]["segment"]["classification_ids"] == {"0": "unclassified", "1": "floor", "2": "wall_1"})
    check("segment surfaces list recorded",
          baseline.entry["stages"]["segment"]["surfaces"][0]["name"] == "floor")

    print("\n=== save_project / load_project round-trip ===")
    pm.save_project(project)
    reloaded = pm.load_project(project.root)
    check("reloaded project_id matches", reloaded.data["project_id"] == project.data["project_id"])
    check("reloaded baseline matches", reloaded.data["baseline"] == project.data["baseline"])

    print("\n=== load_project error handling ===")
    try:
        pm.load_project(tmp / "does_not_exist")
        check("raises ProjectError for missing project.json", False)
    except pm.ProjectError:
        check("raises ProjectError for missing project.json", True)

    bad_project_dir = tmp / "bad_version_project"
    bad_project_dir.mkdir()
    (bad_project_dir / "project.json").write_text(json.dumps({"schema_version": 1}))
    try:
        pm.load_project(bad_project_dir)
        check("raises ProjectError for schema version 1 (no migration)", False)
    except pm.ProjectError:
        check("raises ProjectError for schema version 1 (no migration)", True)

    print("\n=== atomic save doesn't leave a .tmp file behind ===")
    check("no leftover .json.tmp file", not (project.root / "project.json.tmp").exists())

    print("\n=== promote_baseline stub ===")
    pm.promote_baseline(project, "cleanup")
    check("baseline.promoted set True", project.data["baseline"]["promoted"] is True)
    check("baseline.promoted_stage recorded", project.data["baseline"]["promoted_stage"] == "cleanup")

    try:
        pm.promote_baseline(project, "slam")  # slam already complete - fine
        check("promote_baseline accepts a complete stage", True)
    except pm.ProjectError:
        check("promote_baseline accepts a complete stage", False)

    print("\n=== unknown stage name validation ===")
    try:
        pm.start_stage(baseline, "not_a_real_stage")
        check("rejects unknown stage name", False)
    except pm.ProjectError:
        check("rejects unknown stage name", True)

    try:
        pm.start_stage(baseline, "diff")  # 'diff' isn't a baseline/scan stage
        check("rejects a diff-only stage name on a baseline handle", False)
    except pm.ProjectError:
        check("rejects a diff-only stage name on a baseline handle", True)

    print("\n=== add_scan: a comparison scan gets its own pipeline ===")
    fake_pcap2 = fake_source_dir / "capture2.pcap"
    fake_pcap2.write_bytes(b"scan pcap bytes")
    scan_id = pm.add_scan(project, "post-storm", fake_pcap2, "pcap")
    check("scan_id format", scan_id.startswith("post-storm_"))
    check("scan listed", scan_id in pm.list_scans(project))
    check("scan folder structure created",
          all((project.root / "scans" / scan_id / f).is_dir()
              for f in [pm.RAW_FOLDER] + list(pm.BASELINE_SCAN_STAGE_FOLDERS.values())))
    check("scan raw imported",
          (project.root / "scans" / scan_id / "01_raw" / "capture2.pcap").exists())

    scan = project.scan_handle(scan_id)
    check("scan_handle kind", scan.kind == "scan")
    check("scan stages independent of baseline (all not_started)",
          all(scan.entry["stages"][s]["status"] == "not_started" for s in pm.BASELINE_SCAN_STAGE_NAMES))
    check("baseline stages untouched by adding a scan",
          baseline.entry["stages"]["slam"]["status"] == "complete")

    print("\n=== drive the scan through slam -> level -> cleanup (aligned to baseline) ===")
    for stage in ("slam", "level"):
        pm.start_stage(scan, stage)
        out_rel = pm.get_output_path(scan, stage, ".ply")
        (project.root / out_rel).write_bytes(b"x")
        pm.complete_stage(scan, stage, project.root / out_rel)
    pm.start_stage(scan, "cleanup", params={"align_to": pm.get_baseline_cleanup_output(project)})
    scan_cleanup_rel = pm.get_output_path(scan, "cleanup", ".ply")
    (project.root / scan_cleanup_rel).write_bytes(b"x")
    pm.complete_stage(scan, "cleanup", project.root / scan_cleanup_rel,
                       extra_fields={"icp_rms": 0.021, "sidecar": scan_cleanup_rel + ".rms.json"})
    check("scan cleanup complete, next stage is segment", pm.find_next_stage(scan) == "segment")
    check("scan output paths live under scans/<id>/, not baseline/",
          scan.entry["stages"]["cleanup"]["output"].startswith(f"scans/{scan_id}/"))

    pm.start_stage(scan, "segment")
    scan_segment_rel = "scans/" + scan_id + "/05_segment/compartment_04_segment_001/classified.ply"
    (project.root / scan_segment_rel).parent.mkdir(parents=True, exist_ok=True)
    (project.root / scan_segment_rel).write_bytes(b"x")
    pm.complete_stage(scan, "segment", project.root / scan_segment_rel)
    check("scan fully complete once segment is done", pm.find_next_stage(scan) is None)

    print("\n=== add_diff: validation of reference/comparison ===")
    try:
        pm.add_diff(project, "bad", "not_a_real_scan", scan_id)
        check("rejects an unknown reference", False)
    except pm.ProjectError:
        check("rejects an unknown reference", True)
    try:
        pm.add_diff(project, "bad2", "baseline", "not_a_real_scan")
        check("rejects an unknown comparison", False)
    except pm.ProjectError:
        check("rejects an unknown comparison", True)

    diff_id = pm.add_diff(project, "post-storm_vs_baseline", "baseline", scan_id)
    check("diff_id format", diff_id.startswith("post-storm_vs_baseline_"))
    check("diff listed", diff_id in pm.list_diffs(project))
    check("diff folder structure created (diff/classify/surface/export)",
          all((project.root / "diffs" / diff_id / f).is_dir()
              for f in pm.DIFF_STAGE_FOLDERS.values()))
    check("no raw/ folder under a diff (diffs don't import raw sources)",
          not (project.root / "diffs" / diff_id / "01_raw").exists())

    diff = project.diff_handle(diff_id)
    check("diff_handle kind", diff.kind == "diff")
    check("diff.reference recorded", diff.entry["reference"] == "baseline")
    check("diff.comparison recorded", diff.entry["comparison"] == scan_id)
    check("diff stages are diff/classify/surface/export, all not_started",
          list(diff.entry["stages"].keys()) == pm.DIFF_STAGE_NAMES and
          all(diff.entry["stages"][s]["status"] == "not_started" for s in pm.DIFF_STAGE_NAMES))

    print("\n=== a scan can be compared against more than one reference ===")
    scan_id2 = pm.add_scan(project, "routine-check", fake_source_dir / "capture.pcap", "pcap",
                            link_raw="copy")
    for stage in ("slam", "level", "cleanup"):
        pm.start_stage(project.scan_handle(scan_id2), stage)
        h = project.scan_handle(scan_id2)
        out_rel = pm.get_output_path(h, stage, ".ply")
        (project.root / out_rel).write_bytes(b"x")
        pm.complete_stage(h, stage, project.root / out_rel,
                           extra_fields={"icp_rms": 0.015} if stage == "cleanup" else None)
    diff_id2 = pm.add_diff(project, "post-storm_vs_previous", scan_id2, scan_id)
    check("second diff on the same comparison scan, different reference",
          project.data["diffs"][diff_id2]["comparison"] == scan_id and
          project.data["diffs"][diff_id2]["reference"] == scan_id2)

    print("\n=== get_input_for_stage refuses the diff pipeline's first stage ===")
    try:
        pm.get_input_for_stage(diff, "diff")
        check("raises ProjectError for stage 'diff' (needs get_diff_inputs)", False)
    except pm.ProjectError:
        check("raises ProjectError for stage 'diff' (needs get_diff_inputs)", True)

    print("\n=== get_diff_inputs resolves reference='baseline' correctly ===")
    # get_diff_inputs() prefers each side's OWN segment.output over its
    # cleanup.output, resolved independently per side (PROJECT_SCHEMA_v2.md
    # Section 11.3). Both the baseline (see "complete baseline segment"
    # above) and this scan (see "drive the scan through slam -> level ->
    # cleanup" above) completed 'segment', so both sides resolve to their
    # segment output here, not their cleanup output.
    inputs = pm.get_diff_inputs(diff)
    check("reference_path is the baseline's segment output, not its cleanup output",
          inputs["reference_path"] == str(project.root / segment_classified_rel))
    check("reference_source_stage is 'segment' (baseline's segment stage completed)",
          inputs["reference_source_stage"] == "segment")
    check("comparison_path is the scan's segment output, not its cleanup output",
          inputs["comparison_path"] == str(project.root / scan_segment_rel))
    check("comparison_source_stage is 'segment' (this scan's segment stage completed)",
          inputs["comparison_source_stage"] == "segment")
    check("registration_error_used copied from COMPARISON's icp_rms (0.021, not baseline's 0.0184) "
          "- still sourced from cleanup.icp_rms even though the comparison cloud itself came from segment",
          inputs["registration_error_used"] == 0.021)

    print("\n=== get_diff_inputs resolves reference=<scan id> correctly ===")
    # scan_id2 (see "a scan can be compared against more than one reference"
    # above) only completes slam/level/cleanup - segment is left
    # not_started - so its side falls back to cleanup.output.
    inputs2 = pm.get_diff_inputs(project.diff_handle(diff_id2))
    scan_id2_cleanup_rel = project.scan_handle(scan_id2).entry["stages"]["cleanup"]["output"]
    check("reference_path is scan_id2's cleanup output (segment never ran for scan_id2 - fallback)",
          inputs2["reference_path"] == str(project.root / scan_id2_cleanup_rel))
    check("reference_source_stage is 'cleanup' for scan_id2 (no completed segment stage)",
          inputs2["reference_source_stage"] == "cleanup")
    check("comparison_path is scan_id's segment output, not its cleanup output "
          "(same scan as above - it DID complete segment)",
          inputs2["comparison_path"] == str(project.root / scan_segment_rel))
    check("comparison_source_stage is 'segment' for scan_id",
          inputs2["comparison_source_stage"] == "segment")

    print("\n=== set_decoded_raw_path rejects a diff pipeline (no raw object at all) ===")
    try:
        pm.set_decoded_raw_path(diff, "/some/decoded/bag")
        check("raises ProjectError for a diff pipeline", False)
    except pm.ProjectError:
        check("raises ProjectError for a diff pipeline", True)

    print("\n=== drive the diff through diff -> classify -> surface -> export ===")
    pm.start_stage(diff, "diff", params={"m3c2_params_file": "m3c2.txt"})
    diff_output_rel = pm.get_output_path(diff, "diff", ".ply")
    check("diff output path lives under diffs/<id>/05_diff/",
          diff_output_rel == f"diffs/{diff_id}/05_diff/compartment_04_diff_001.ply")
    (project.root / diff_output_rel).write_bytes(b"x")
    pm.complete_stage(diff, "diff", project.root / diff_output_rel,
                       extra_fields={"m3c2_params_file": "m3c2.txt", "registration_error_used": 0.021})
    check("diff stage complete, registration_error_used recorded",
          diff.entry["stages"]["diff"]["registration_error_used"] == 0.021)

    for stage in ("classify", "surface", "export"):
        pm.start_stage(diff, stage)
        out_ext = ".usd" if stage == "export" else ".ply"
        out_rel = pm.get_output_path(diff, stage, out_ext)
        (project.root / out_rel).write_bytes(b"x")
        pm.complete_stage(diff, stage, project.root / out_rel)
    check("diff pipeline fully complete", pm.find_next_stage(diff) is None)
    check("export output extension respected",
          diff.entry["stages"]["export"]["output"].endswith(".usd"))

    print("\n=== explicit copy/move/link import modes (add_scan) ===")
    src_copy = fake_source_dir / "for_copy.pcap"
    src_copy.write_bytes(b"copy me")
    copy_scan_id = pm.add_scan(project, "copy-test", src_copy, "pcap", link_raw="copy")
    check("copy mode: original file still exists", src_copy.exists())
    check("copy mode: import_method recorded as copy",
          project.data["scans"][copy_scan_id]["raw"]["import_method"] == "copy")

    src_move = fake_source_dir / "for_move.pcap"
    src_move.write_bytes(b"move me")
    move_scan_id = pm.add_scan(project, "move-test", src_move, "pcap", link_raw="move")
    check("move mode: original file NO LONGER exists at old location", not src_move.exists())
    check("move mode: file now exists in project",
          (project.root / "scans" / move_scan_id / "01_raw" / "for_move.pcap").exists())

    try:
        bad_mode_src = fake_source_dir / "bad_mode.pcap"
        bad_mode_src.write_bytes(b"x")
        pm.add_scan(project, "bad-mode-test", bad_mode_src, "pcap", link_raw="teleport")
        check("rejects invalid link_raw value", False)
    except pm.ProjectError:
        check("rejects invalid link_raw value", True)

    print("\n=== duplicate scan/diff IDs are rejected ===")
    try:
        pm.add_scan(project, "post-storm", fake_source_dir / "capture.pcap", "pcap")
        check("rejects a duplicate scan_id (same label, same day)", False)
    except pm.ProjectError:
        check("rejects a duplicate scan_id (same label, same day)", True)

    try:
        pm.add_diff(project, "post-storm_vs_baseline", "baseline", scan_id)
        check("rejects a duplicate diff_id (same label, same day)", False)
    except pm.ProjectError:
        check("rejects a duplicate diff_id (same label, same day)", True)

print("\nALL TESTS PASSED")
