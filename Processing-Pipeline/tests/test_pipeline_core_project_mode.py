"""
test_pipeline_core_project_mode.py
=====================================
Tests the project-mode integration in pipeline_core.py against Schema v2:
  - Manual mode (pipeline=None) is byte-for-byte unaffected
  - Project mode correctly overrides inputs via
    project_manager.get_input_for_stage()/get_diff_inputs(), ignoring
    whatever was manually passed
  - start_stage() actually gets called (status becomes 'running')
  - finish_stage() correctly reports success (-> complete_stage) or
    failure (-> fail_stage), and does nothing when pipeline is None
  - A baseline pipeline flow (slam -> level -> cleanup) behaves correctly
    end to end
  - A diff pipeline flow (diff -> classify -> surface -> export) behaves
    correctly end to end, including the two-input diff resolution and
    export's baseline-from-project / change-from-surface split

REWRITTEN FOR SCHEMA V2: the old version of this file only covered Stage
1/2 (the only stages Version 1's pipeline_core.py actually wired to
project_manager). This version covers the full seven-stage pipeline
across both pipeline kinds (baseline/scan and diff), since that's now
the whole point of the schema split (PROJECT_SCHEMA_v2.md Section 4).
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))
import pipeline_core as core
import project_manager as pm


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        raise AssertionError(label)


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)

    print("=== Manual mode unaffected (pipeline=None, the default) ===")
    cmd_manual = core.build_slam_command("my_capture.pcap", 0.25, "out.ply", meta="meta.json")
    check("manual mode: source used exactly as given", "my_capture.pcap" in cmd_manual)

    cmd_manual_level = core.build_level_command("level_cloud.py", "input.ply", "output.ply")
    check("manual mode (level): input used exactly as given", "input.ply" in cmd_manual_level)

    cmd_manual_cleanup = core.build_cleanup_command("in.ply", "out.ply")
    check("manual mode (cleanup): input used exactly as given", "in.ply" in cmd_manual_cleanup)

    cmd_manual_diff = core.build_diff_command("base.ply", "comp.ply", "params.txt")
    check("manual mode (diff): baseline/comparison used exactly as given",
          "base.ply" in cmd_manual_diff and "comp.ply" in cmd_manual_diff)
    check("manual mode (diff): baseline cloud is loaded FIRST (CloudCompare's "
          "'compared'/Cloud #1 slot - the one that gets the M3C2 result attached "
          "and resaved, keeping the result anchored to fixed baseline query "
          "locations), comparison loaded second as the reference cloud",
          cmd_manual_diff.index("base.ply") < cmd_manual_diff.index("comp.ply"))

    cmd_manual_classify = core.build_classify_command("classify.py", "in.ply", "out.ply", 0.02)
    check("manual mode (classify): input used exactly as given", "in.ply" in cmd_manual_classify)
    check("manual mode (classify): clustering enabled by default",
          "--cluster-method" in cmd_manual_classify and "--cluster-eps" in cmd_manual_classify)

    cmd_manual_classify_hdbscan = core.build_classify_command(
        "classify.py", "in.ply", "out.ply", 0.02, cluster_method="hdbscan")
    check("manual mode (classify): --cluster-eps omitted for hdbscan (dbscan-only arg)",
          "--cluster-eps" not in cmd_manual_classify_hdbscan)

    cmd_manual_classify_nocluster = core.build_classify_command(
        "classify.py", "in.ply", "out.ply", 0.02, cluster=False)
    check("manual mode (classify): cluster=False passes --no-cluster",
          "--no-cluster" in cmd_manual_classify_nocluster)

    cmd_manual_surface = core.build_surface_command("surface.py", "in.ply", "out.ply")
    check("manual mode (surface): input used exactly as given", "in.ply" in cmd_manual_surface)

    cmd_manual_export = core.build_export_command("export.py", "base.ply", "change.ply", "out.usd")
    check("manual mode (export): baseline/change used exactly as given",
          "base.ply" in cmd_manual_export and "change.ply" in cmd_manual_export)

    print("\n=== Set up a real project for project-mode tests ===")
    fake_pcap = tmp / "capture.pcap"
    fake_pcap.write_bytes(b"fake pcap")
    projects_root = tmp / "projects"
    projects_root.mkdir()
    project = pm.create_project(projects_root, "compartment_04", fake_pcap, "pcap")
    baseline = project.baseline_handle()

    print("\n=== Project mode (baseline): input auto-resolution overrides manual source ===")
    raw_path = pm.get_input_for_stage(baseline, "slam")
    cmd_project = core.build_slam_command(
        "THIS_SHOULD_BE_IGNORED.pcap", 0.25, "ignored_output.ply", pipeline=baseline)
    check("manually-passed source was ignored", "THIS_SHOULD_BE_IGNORED.pcap" not in cmd_project)
    check("real project input (raw imported file) used instead", raw_path in cmd_project)

    print("\n=== Project mode: start_stage was actually called ===")
    check("slam stage status is 'running'", baseline.entry["stages"]["slam"]["status"] == "running")
    check("slam params recorded", baseline.entry["stages"]["slam"]["params"]["voxel_size"] == 0.25)

    print("\n=== finish_stage: success path ===")
    slam_output_rel = pm.get_output_path(baseline, "slam", ".ply")
    slam_output_abs = project.root / slam_output_rel
    slam_output_abs.write_bytes(b"fake slam output")
    core.finish_stage(baseline, "slam", slam_output_abs, success=True,
                       extra_fields={"point_spacing": 0.019})
    check("slam status complete", baseline.entry["stages"]["slam"]["status"] == "complete")
    check("find_next_stage advances to level", pm.find_next_stage(baseline) == "level")
    check("extra_fields merged (point_spacing)",
          baseline.entry["stages"]["slam"]["point_spacing"] == 0.019)

    print("\n=== Project mode (level): auto-picks up slam's REAL output as input ===")
    cmd_level_project = core.build_level_command(
        "level_cloud.py", "THIS_SHOULD_ALSO_BE_IGNORED.ply", "ignored.ply", pipeline=baseline)
    check("manually-passed input_ply was ignored",
          "THIS_SHOULD_ALSO_BE_IGNORED.ply" not in cmd_level_project)
    check("slam's real output path used as level's input", str(slam_output_abs) in cmd_level_project)
    check("level status is 'running'", baseline.entry["stages"]["level"]["status"] == "running")

    print("\n=== finish_stage: failure path ===")
    core.finish_stage(baseline, "level", None, success=False, error_message="no planes found")
    check("level status failed", baseline.entry["stages"]["level"]["status"] == "failed")
    check("find_next_stage did NOT advance past level", pm.find_next_stage(baseline) == "level")
    check("error message recorded", baseline.entry["stages"]["level"]["error"] == "no planes found")

    print("\n=== Re-run level after 'fixing' the problem, then succeed ===")
    cmd_level_retry = core.build_level_command(
        "level_cloud.py", "unused.ply", "unused.ply", distance_threshold=0.1, pipeline=baseline)
    check("rerun_count NOT incremented after a failed->retry (confirmed intended)",
          baseline.entry["stages"]["level"]["rerun_count"] == 0)
    level_output_rel = pm.get_output_path(baseline, "level", ".ply")
    level_output_abs = project.root / level_output_rel
    level_output_abs.write_bytes(b"fake leveled output")
    core.finish_stage(baseline, "level", level_output_abs, success=True)
    check("level now complete", baseline.entry["stages"]["level"]["status"] == "complete")
    check("find_next_stage advances to cleanup", pm.find_next_stage(baseline) == "cleanup")

    print("\n=== Project mode (cleanup): align_to is NOT auto-resolved, input is ===")
    cmd_cleanup_project = core.build_cleanup_command(
        "THIS_IGNORED.ply", "out.ply", align_to_ply="explicit_align_target.ply", pipeline=baseline)
    check("manually-passed input_ply was ignored", "THIS_IGNORED.ply" not in cmd_cleanup_project)
    check("level's real output used as cleanup's input", str(level_output_abs) in cmd_cleanup_project)
    check("explicit align_to_ply is honored, not overridden",
          "explicit_align_target.ply" in cmd_cleanup_project)
    check("cleanup status is 'running'", baseline.entry["stages"]["cleanup"]["status"] == "running")
    check("cleanup params recorded align_to",
          baseline.entry["stages"]["cleanup"]["params"]["align_to"] == "explicit_align_target.ply")

    cleanup_output_rel = pm.get_output_path(baseline, "cleanup", ".ply")
    cleanup_output_abs = project.root / cleanup_output_rel
    cleanup_output_abs.write_bytes(b"fake cleaned output")
    core.finish_stage(baseline, "cleanup", cleanup_output_abs, success=True,
                       extra_fields={"icp_rms": 0.0184})
    check("baseline pipeline advances to segment", pm.find_next_stage(baseline) == "segment")
    check("baseline icp_rms recorded", baseline.entry["stages"]["cleanup"]["icp_rms"] == 0.0184)

    print("\n=== Manual mode (segment): write_separate_surfaces off by default ===")
    cmd_segment_manual = core.build_segment_command("segment_planes.py", "in.ply", "out_dir")
    check("manual mode (segment): --write-separate-surfaces omitted by default",
          "--write-separate-surfaces" not in cmd_segment_manual)
    check("manual mode (segment): distance-threshold defaults to 0.05 (tested combo)",
          "0.05" in cmd_segment_manual)
    check("manual mode (segment): max-planes defaults to 20 (tested combo)",
          "20" in cmd_segment_manual)
    check("manual mode (segment): min-inlier-fraction defaults to 0.003 (tested combo)",
          "0.003" in cmd_segment_manual)
    check("manual mode (segment): cluster-eps defaults to 0.5 (tested combo)",
          "0.5" in cmd_segment_manual)
    cmd_segment_manual_opt_in = core.build_segment_command(
        "segment_planes.py", "in.ply", "out_dir", write_separate_surfaces=True)
    check("manual mode (segment): --write-separate-surfaces present when opted in",
          "--write-separate-surfaces" in cmd_segment_manual_opt_in)

    print("\n=== Manual mode (segment): outside-envelope junk filter, on by default ===")
    check("manual mode (segment): --envelope-margin present by default (filter on by default)",
          "--envelope-margin" in cmd_segment_manual)
    check("manual mode (segment): default envelope margin is 0.15",
          "0.15" in cmd_segment_manual)
    check("manual mode (segment): --no-envelope-filter omitted by default",
          "--no-envelope-filter" not in cmd_segment_manual)
    check("manual mode (segment): --write-envelope-filtered omitted by default",
          "--write-envelope-filtered" not in cmd_segment_manual)
    cmd_segment_no_envelope = core.build_segment_command(
        "segment_planes.py", "in.ply", "out_dir", envelope_filter=False)
    check("manual mode (segment): --no-envelope-filter present when turned off",
          "--no-envelope-filter" in cmd_segment_no_envelope)
    check("manual mode (segment): --envelope-margin omitted when filter is off",
          "--envelope-margin" not in cmd_segment_no_envelope)
    cmd_segment_custom_margin = core.build_segment_command(
        "segment_planes.py", "in.ply", "out_dir", envelope_margin=0.3,
        write_envelope_filtered=True)
    check("manual mode (segment): custom envelope margin passed through",
          "--envelope-margin" in cmd_segment_custom_margin and "0.3" in cmd_segment_custom_margin)
    check("manual mode (segment): --write-envelope-filtered present when opted in",
          "--write-envelope-filtered" in cmd_segment_custom_margin)

    print("\n=== Manual mode (level): horizontal_threshold passthrough ===")
    cmd_level_no_horizontal = core.build_level_command("level_cloud.py", "in.ply", "out.ply")
    check("manual mode (level): --horizontal-threshold omitted when not given",
          "--horizontal-threshold" not in cmd_level_no_horizontal)
    cmd_level_horizontal = core.build_level_command(
        "level_cloud.py", "in.ply", "out.ply", horizontal_threshold=0.8)
    check("manual mode (level): --horizontal-threshold passed through when given",
          "--horizontal-threshold" in cmd_level_horizontal and "0.8" in cmd_level_horizontal)

    print("\n=== Project mode (segment): auto-picks up cleanup's REAL output as input ===")
    # complete_stage() requires the output path to sit inside the project
    # root (it records project-relative paths - PROJECT_SCHEMA_v2.md
    # Section 8) - unlike the earlier None-manifest check below, this one
    # needs to live under project.root, not the outer tmp dir.
    segment_out_dir = project.root / "baseline" / "05_segment" / "test_run_001"
    cmd_segment_project = core.build_segment_command(
        "segment_planes.py", "IGNORED.ply", str(segment_out_dir), pipeline=baseline)
    check("manually-passed input_ply was ignored", "IGNORED.ply" not in cmd_segment_project)
    check("cleanup's real output used as segment's input", str(cleanup_output_abs) in cmd_segment_project)
    check("segment status is 'running'", baseline.entry["stages"]["segment"]["status"] == "running")
    check("segment params recorded write_separate_surfaces (default False)",
          baseline.entry["stages"]["segment"]["params"]["write_separate_surfaces"] is False)
    check("segment params recorded envelope_filter (default True)",
          baseline.entry["stages"]["segment"]["params"]["envelope_filter"] is True)
    check("segment params recorded envelope_margin (default 0.15)",
          baseline.entry["stages"]["segment"]["params"]["envelope_margin"] == 0.15)
    check("segment params recorded write_envelope_filtered (default False)",
          baseline.entry["stages"]["segment"]["params"]["write_envelope_filtered"] is False)

    # Simulate what segment_planes.py itself would have written to
    # --output-dir on a real run with write_separate_surfaces OFF (the new
    # default): classified.ply/envelope.ply, but each surface's own 'file'
    # is null in the manifest, since floor.ply etc. were never written -
    # resolve_segment_output() reads this back. Also simulates a real run
    # where the outside-envelope filter found something to flag.
    segment_out_dir.mkdir()
    (segment_out_dir / "classified.ply").write_bytes(b"fake classified cloud")
    (segment_out_dir / "envelope.ply").write_bytes(b"fake envelope cloud")
    (segment_out_dir / "envelope_filtered.ply").write_bytes(b"fake envelope-filtered cloud")
    manifest = {
        "source": str(cleanup_output_abs),
        "surfaces": [{"name": "floor", "file": None,
                      "point_count": 500, "normal": [0.0, 0.0, 1.0], "z_min": 0.0, "z_max": 0.01}],
        "classification_ids": {"0": "unclassified", "1": "floor"},
        "classified_cloud_file": str(segment_out_dir / "classified.ply"),
        "envelope_cloud_file": str(segment_out_dir / "envelope.ply"),
        "envelope_filter_applied": True,
        "envelope_margin": 0.15,
        "n_outside_envelope": 42,
        "envelope_filtered_cloud_file": str(segment_out_dir / "envelope_filtered.ply"),
    }
    (segment_out_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    classified_path, extra_fields = core.resolve_segment_output(segment_out_dir)
    check("resolve_segment_output finds classified.ply",
          classified_path == Path(manifest["classified_cloud_file"]))
    check("resolve_segment_output carries the surfaces list through",
          extra_fields["surfaces"][0]["name"] == "floor")
    check("resolve_segment_output carries a null 'file' when write_separate_surfaces was off",
          extra_fields["surfaces"][0]["file"] is None)
    check("resolve_segment_output carries envelope_output through",
          extra_fields["envelope_output"] == str(segment_out_dir / "envelope.ply"))
    check("resolve_segment_output carries envelope_filter_applied through",
          extra_fields["envelope_filter_applied"] is True)
    check("resolve_segment_output carries envelope_margin_used through",
          extra_fields["envelope_margin_used"] == 0.15)
    check("resolve_segment_output carries n_outside_envelope through",
          extra_fields["n_outside_envelope"] == 42)
    check("resolve_segment_output carries envelope_filtered_output through",
          extra_fields["envelope_filtered_output"] == str(segment_out_dir / "envelope_filtered.ply"))
    check("resolve_segment_output returns None for a folder with no manifest.json",
          core.resolve_segment_output(tmp / "no_such_segment_dir")[0] is None)

    print("\n=== resolve_segment_output: envelope filter fields absent when the run skipped it ===")
    skip_dir = tmp / "segment_skip_test"
    skip_dir.mkdir()
    (skip_dir / "classified.ply").write_bytes(b"x")
    skip_manifest = {
        "classified_cloud_file": str(skip_dir / "classified.ply"),
        "envelope_filter_applied": False,
        "envelope_margin": 0.15,
        "n_outside_envelope": 0,
        "envelope_filtered_cloud_file": None,
    }
    (skip_dir / "manifest.json").write_text(json.dumps(skip_manifest), encoding="utf-8")
    _, skip_extra_fields = core.resolve_segment_output(skip_dir)
    check("resolve_segment_output carries envelope_filter_applied=False through",
          skip_extra_fields["envelope_filter_applied"] is False)
    check("resolve_segment_output omits envelope_filtered_output when null in the manifest",
          "envelope_filtered_output" not in skip_extra_fields)

    core.finish_stage(baseline, "segment", classified_path, success=True, extra_fields=extra_fields)
    check("baseline pipeline fully complete once segment is done", pm.find_next_stage(baseline) is None)
    check("segment classification_ids recorded",
          baseline.entry["stages"]["segment"]["classification_ids"] == {"0": "unclassified", "1": "floor"})

    print("\n=== finish_stage does nothing when pipeline is None (safe to call unconditionally) ===")
    result = core.finish_stage(None, "slam", "whatever.ply", success=True)
    check("returns None / no exception with pipeline=None", result is None)

    print("\n=== Set up a scan + diff for Stage 5-8 project-mode tests ===")
    fake_pcap2 = tmp / "capture2.pcap"
    fake_pcap2.write_bytes(b"scan pcap")
    scan_id = pm.add_scan(project, "post-storm", fake_pcap2, "pcap")
    scan = project.scan_handle(scan_id)
    for stage in ("slam", "level"):
        core.build_slam_command("ignored", 0.25, "ignored", pipeline=scan) if stage == "slam" else \
            core.build_level_command("level_cloud.py", "ignored", "ignored", pipeline=scan)
        out_rel = pm.get_output_path(scan, stage, ".ply")
        (project.root / out_rel).write_bytes(b"x")
        core.finish_stage(scan, stage, project.root / out_rel, success=True)
    core.build_cleanup_command(
        "ignored", "ignored", align_to_ply=pm.get_baseline_cleanup_output(project), pipeline=scan)
    scan_cleanup_rel = pm.get_output_path(scan, "cleanup", ".ply")
    (project.root / scan_cleanup_rel).write_bytes(b"x")
    core.finish_stage(scan, "cleanup", project.root / scan_cleanup_rel, success=True,
                       extra_fields={"icp_rms": 0.021})
    check("scan pipeline advances to segment", pm.find_next_stage(scan) == "segment")

    scan_segment_dir = project.root / "scans" / scan_id / "05_segment" / "test_run_001"
    core.build_segment_command("segment_planes.py", "ignored", str(scan_segment_dir), pipeline=scan)
    scan_segment_dir.mkdir()
    (scan_segment_dir / "classified.ply").write_bytes(b"x")
    (scan_segment_dir / "manifest.json").write_text(json.dumps({
        "classified_cloud_file": str(scan_segment_dir / "classified.ply"),
    }), encoding="utf-8")
    scan_classified_path, scan_extra_fields = core.resolve_segment_output(scan_segment_dir)
    core.finish_stage(scan, "segment", scan_classified_path, success=True, extra_fields=scan_extra_fields)
    check("scan pipeline fully complete once segment is done", pm.find_next_stage(scan) is None)

    diff_id = pm.add_diff(project, "post-storm_vs_baseline", "baseline", scan_id)
    diff = project.diff_handle(diff_id)

    print("\n=== Project mode (diff): TWO inputs resolved from reference/comparison ===")
    # Both the baseline and this scan already completed 'segment' above, so
    # get_diff_inputs() now PREFERS each side's segment.output over its
    # cleanup.output (PROJECT_SCHEMA_v2.md Section 11.3) - the resolved
    # paths below are the two sides' classified.ply files, not their plain
    # cleanup outputs, even though the variable/label names below still say
    # "reference"/"comparison" generically.
    expected_inputs = pm.get_diff_inputs(diff)
    cmd_diff_project = core.build_diff_command(
        "THIS_IGNORED_BASELINE.ply", "THIS_IGNORED_COMPARISON.ply", "params.txt", pipeline=diff)
    check("manually-passed baseline_ply was ignored",
          "THIS_IGNORED_BASELINE.ply" not in cmd_diff_project)
    check("manually-passed comparison_ply was ignored",
          "THIS_IGNORED_COMPARISON.ply" not in cmd_diff_project)
    check("resolved reference used instead", expected_inputs["reference_path"] in cmd_diff_project)
    check("resolved comparison used instead", expected_inputs["comparison_path"] in cmd_diff_project)
    check("registration_error_used is the COMPARISON's icp_rms (0.021), "
          "still sourced from cleanup.icp_rms regardless of which stage "
          "supplied the actual cloud",
          expected_inputs["registration_error_used"] == 0.021)
    check("reference_source_stage is 'segment' (baseline's segment stage completed above)",
          expected_inputs["reference_source_stage"] == "segment")
    check("comparison_source_stage is 'segment' (this scan's segment stage completed above)",
          expected_inputs["comparison_source_stage"] == "segment")
    check("resolved reference path is the baseline's segment classified.ply, not its cleanup.output",
          expected_inputs["reference_path"] == str(classified_path))
    check("resolved comparison path is the scan's segment classified.ply, not its cleanup.output",
          expected_inputs["comparison_path"] == str(scan_classified_path))
    check("project mode (diff): resolved reference/baseline cloud is loaded "
          "FIRST, resolved comparison cloud loaded second - same as manual "
          "mode, so the M3C2 result attaches to and resaves via the "
          "reference/baseline's own file, matching what the Stage 5 dialog "
          "watches for after the CloudCompare subprocess exits",
          cmd_diff_project.index(expected_inputs["reference_path"])
          < cmd_diff_project.index(expected_inputs["comparison_path"]))
    check("diff status is 'running'", diff.entry["stages"]["diff"]["status"] == "running")

    print("\n=== Project mode (diff): M3C2 params file auto-named, no manual save dialog ===")
    # Mirrors what open_diff_dialog()'s "Generate Params File..." button now
    # does for a project pipeline: call get_output_path() itself instead of
    # popping a save-location dialog. Uses the "diff" stage's own output
    # folder with a ".txt" extension, which get_output_path sequences
    # independently from the ".ply" diff output living in that same folder.
    params_path_rel = pm.get_output_path(diff, "diff", ".txt")
    params_path_abs = Path(pm.get_absolute_path(diff.project, params_path_rel))
    diff_stage_folder_abs = diff.root / diff.stage_folders["diff"]
    check("auto-generated params path lands in the diff's own output folder",
          params_path_abs.parent == diff_stage_folder_abs)
    core.generate_m3c2_params_file(params_path_abs, 0.05, 0.025, 0.10, 0.021)
    check("auto-generated params file was actually written", params_path_abs.exists())
    check("auto-generated params filename follows the compartment_diff_NNN pattern",
          params_path_abs.name.startswith(project.data["compartment"] + "_diff_")
          and params_path_abs.suffix == ".txt")

    diff_output_rel = pm.get_output_path(diff, "diff", ".ply")
    diff_output_abs = project.root / diff_output_rel
    diff_output_abs.write_bytes(b"fake m3c2 result")
    core.finish_stage(diff, "diff", diff_output_abs, success=True,
                       extra_fields={"m3c2_params_file": "params.txt",
                                     "registration_error_used": expected_inputs["registration_error_used"]})
    check("find_next_stage advances to classify", pm.find_next_stage(diff) == "classify")

    print("\n=== Project mode (classify): auto-picks up diff's REAL output as input ===")
    classify_output_rel = pm.get_output_path(diff, "classify", ".ply")
    classify_output_abs = project.root / classify_output_rel
    cmd_classify_project = core.build_classify_command(
        "classify.py", "IGNORED.ply", str(classify_output_abs), 0.02, pipeline=diff)
    check("manually-passed input_ply was ignored", "IGNORED.ply" not in cmd_classify_project)
    check("diff's real output used as classify's input", str(diff_output_abs) in cmd_classify_project)
    check("classify status is 'running'", diff.entry["stages"]["classify"]["status"] == "running")
    check("classify params recorded cluster settings",
          diff.entry["stages"]["classify"]["params"]["cluster"] is True and
          diff.entry["stages"]["classify"]["params"]["cluster_method"] == "dbscan")

    # Simulate what m3c2_classify.py itself would have written next to
    # --output on a real run with clustering on (the '*.clusters.json'
    # sidecar - Section 3.3's manifest-absorption pattern, same idea as
    # segment's manifest.json above) - resolve_classify_output() reads it.
    classify_output_abs.write_bytes(b"fake classified output")
    clusters_sidecar = classify_output_abs.with_suffix(".clusters.json")
    clusters_sidecar.write_text(json.dumps({
        "n_flagged": 55, "n_confirmed": 45, "n_noise": 10,
        "clusters": [
            {"cluster_id": 0, "point_count": 30, "centroid": [1.0, 1.0, 1.0],
             "extent": [0.1, 0.1, 0.1], "mean_magnitude": 0.066, "max_magnitude": 0.078},
            {"cluster_id": 1, "point_count": 15, "centroid": [-2.0, 3.0, 0.0],
             "extent": [0.08, 0.08, 0.08], "mean_magnitude": 0.076, "max_magnitude": 0.09},
        ],
    }), encoding="utf-8")

    classify_extra_fields = core.resolve_classify_output(classify_output_abs)
    check("resolve_classify_output carries n_confirmed through",
          classify_extra_fields["n_confirmed"] == 45)
    check("resolve_classify_output carries the clusters list through",
          len(classify_extra_fields["clusters"]) == 2 and
          classify_extra_fields["clusters"][0]["point_count"] == 30)
    check("resolve_classify_output returns {} when no sidecar exists (e.g. --no-cluster runs)",
          core.resolve_classify_output(tmp / "no_such_output.ply") == {})

    core.finish_stage(diff, "classify", classify_output_abs, success=True,
                       extra_fields=classify_extra_fields)
    check("find_next_stage advances to surface", pm.find_next_stage(diff) == "surface")
    check("classify n_confirmed recorded on the stage entry",
          diff.entry["stages"]["classify"]["n_confirmed"] == 45)

    print("\n=== Project mode (surface): auto-picks up classify's REAL output as input ===")
    cmd_surface_project = core.build_surface_command(
        "surface.py", "IGNORED.ply", "ignored_out.ply", pipeline=diff)
    check("manually-passed input_ply was ignored", "IGNORED.ply" not in cmd_surface_project)
    check("classify's real output used as surface's input",
          str(classify_output_abs) in cmd_surface_project)
    surface_output_rel = pm.get_output_path(diff, "surface", ".ply")
    surface_output_abs = project.root / surface_output_rel
    surface_output_abs.write_bytes(b"fake mesh output")
    core.finish_stage(diff, "surface", surface_output_abs, success=True)
    check("find_next_stage advances to export", pm.find_next_stage(diff) == "export")

    print("\n=== Project mode (export): baseline from PROJECT baseline, change from surface ===")
    cmd_export_project = core.build_export_command(
        "usd_export.py", "IGNORED_BASELINE.ply", "IGNORED_CHANGE.ply", "out.usd", pipeline=diff)
    check("manually-passed baseline_ply was ignored", "IGNORED_BASELINE.ply" not in cmd_export_project)
    check("manually-passed change_ply was ignored", "IGNORED_CHANGE.ply" not in cmd_export_project)
    check("project's baseline cleanup output used as --baseline",
          pm.get_baseline_cleanup_output(project) in cmd_export_project)
    check("surface's real output used as --change", str(surface_output_abs) in cmd_export_project)
    export_output_rel = pm.get_output_path(diff, "export", ".usd")
    export_output_abs = project.root / export_output_rel
    export_output_abs.write_bytes(b"fake usd output")
    core.finish_stage(diff, "export", export_output_abs, success=True)
    check("diff pipeline fully complete", pm.find_next_stage(diff) is None)
    check("export output extension respected",
          diff.entry["stages"]["export"]["output"].endswith(".usd"))

    print("\n=== Project mode (diff): get_diff_inputs() falls back to cleanup.output "
          "independently per side when 'segment' hasn't completed for that side ===")
    # New scan that only completes slam/level/cleanup - segment is left
    # 'not_started' on purpose, to exercise the fallback path. The existing
    # baseline (above) already has a completed 'segment' stage, so pairing
    # this scan against the baseline covers BOTH outcomes in one diff:
    # reference (baseline) -> "segment", comparison (this scan) -> "cleanup".
    fake_pcap3 = tmp / "capture3.pcap"
    fake_pcap3.write_bytes(b"scan pcap no segment")
    scan_id_nosegment = pm.add_scan(project, "routine-check", fake_pcap3, "pcap")
    scan_nosegment = project.scan_handle(scan_id_nosegment)
    for stage in ("slam", "level"):
        core.build_slam_command("ignored", 0.25, "ignored", pipeline=scan_nosegment) if stage == "slam" else \
            core.build_level_command("level_cloud.py", "ignored", "ignored", pipeline=scan_nosegment)
        out_rel = pm.get_output_path(scan_nosegment, stage, ".ply")
        (project.root / out_rel).write_bytes(b"x")
        core.finish_stage(scan_nosegment, stage, project.root / out_rel, success=True)
    core.build_cleanup_command(
        "ignored", "ignored", align_to_ply=pm.get_baseline_cleanup_output(project), pipeline=scan_nosegment)
    scan_nosegment_cleanup_rel = pm.get_output_path(scan_nosegment, "cleanup", ".ply")
    scan_nosegment_cleanup_abs = project.root / scan_nosegment_cleanup_rel
    scan_nosegment_cleanup_abs.write_bytes(b"x")
    core.finish_stage(scan_nosegment, "cleanup", scan_nosegment_cleanup_abs, success=True,
                       extra_fields={"icp_rms": 0.033})
    check("this scan's next stage is 'segment' (not yet run - left not_started on purpose)",
          pm.find_next_stage(scan_nosegment) == "segment")

    diff_id_mixed = pm.add_diff(project, "routine-check_vs_baseline", "baseline", scan_id_nosegment)
    diff_mixed = project.diff_handle(diff_id_mixed)
    mixed_inputs = pm.get_diff_inputs(diff_mixed)
    check("reference_source_stage is 'segment' (baseline HAS a completed segment stage)",
          mixed_inputs["reference_source_stage"] == "segment")
    check("comparison_source_stage is 'cleanup' (this scan's segment stage never completed - fallback)",
          mixed_inputs["comparison_source_stage"] == "cleanup")
    check("reference resolves to the baseline's segment classified.ply",
          mixed_inputs["reference_path"] == str(classified_path))
    check("comparison falls back to this scan's own cleanup.output",
          mixed_inputs["comparison_path"] == str(scan_nosegment_cleanup_abs))
    check("registration_error_used is still sourced from cleanup.icp_rms (0.033), "
          "same field regardless of which stage supplied the actual cloud",
          mixed_inputs["registration_error_used"] == 0.033)

    print("\n=== Project mode (diff): get_diff_inputs() raises a clear error when "
          "neither segment NOR cleanup has completed for a side ===")
    fake_pcap4 = tmp / "capture4.pcap"
    fake_pcap4.write_bytes(b"scan pcap no cleanup either")
    scan_id_bare = pm.add_scan(project, "just-imported", fake_pcap4, "pcap")
    scan_bare = project.scan_handle(scan_id_bare)
    diff_id_bare = pm.add_diff(project, "just-imported_vs_baseline", "baseline", scan_id_bare)
    diff_bare = project.diff_handle(diff_id_bare)
    try:
        pm.get_diff_inputs(diff_bare)
        check("get_diff_inputs raised ProjectError for a comparison side with no cleanup output yet",
              False)
    except pm.ProjectError as exc:
        check("get_diff_inputs raised ProjectError for a comparison side with no cleanup output yet",
              "comparison" in str(exc))

    print("\n=== run_streaming(): forces PYTHONUNBUFFERED=1 on the child process env ===")
    # Real bug, confirmed on real hardware: a long-running Python child
    # script (KISS-ICP SLAM, segment_planes.py, decode_raw_packets.py -
    # every build_X_command() that launches "sys.executable <script>.py")
    # fully block-buffers its own stdout whenever it isn't a real terminal,
    # which is true here since stdout is a pipe into run_streaming(). The
    # child's print() output piles up in ITS OWN internal buffer and never
    # reaches on_line() until that buffer fills or the process exits -
    # making the applet's log console look completely frozen ("running",
    # zero output) for the run's entire duration, even though the
    # subprocess is actually working the whole time. A short stage
    # finishes fast enough to hide this; a long one doesn't. The fix is to
    # pass an explicit env to Popen with PYTHONUNBUFFERED=1 set, which
    # disables that buffering for the CHILD process specifically.
    #
    # This test swaps in a fake subprocess.Popen so it can inspect exactly
    # what run_streaming() passes as `env`, without depending on real
    # process timing (which would be slow and flaky in a test) or on
    # whatever PYTHONUNBUFFERED happens to already be set to in whichever
    # environment runs this test suite.
    captured_popen_kwargs = {}

    class _FakeStreamingProcess:
        def __init__(self):
            self.stdout = iter(["fake line 1\n", "fake line 2\n"])
            self.returncode = 0

        def wait(self):
            pass

    def _fake_popen(cmd_to_run, **kwargs):
        captured_popen_kwargs.update(kwargs)
        return _FakeStreamingProcess()

    real_popen = subprocess.Popen
    subprocess.Popen = _fake_popen
    try:
        streamed_lines = []
        done_event = threading.Event()
        core.run_streaming(
            [sys.executable, "-c", "pass"],
            lambda line: streamed_lines.append(line),
            lambda returncode: done_event.set(),
        )
        check("run_streaming's worker thread finished within 2 seconds",
              done_event.wait(timeout=2))
    finally:
        subprocess.Popen = real_popen

    check("run_streaming still delivers the (fake) process's lines to on_line normally",
          streamed_lines == ["fake line 1", "fake line 2"])
    check("run_streaming passes an explicit env to Popen (not the default None, "
          "which would just inherit the parent's own PYTHONUNBUFFERED setting - or "
          "lack of one - unpredictably)",
          captured_popen_kwargs.get("env") is not None)
    check("run_streaming's env sets PYTHONUNBUFFERED=1 for the child process - "
          "this is the actual fix for the 'applet looks frozen, no console output' "
          "bug reported on a real Windows machine running a KISS-ICP SLAM stage",
          captured_popen_kwargs.get("env", {}).get("PYTHONUNBUFFERED") == "1")
    check("run_streaming's env still carries the rest of the real environment through "
          "(e.g. PATH) - not a stripped-down replacement that could break finding "
          "python.exe/CloudCompare on the child's own PATH lookup",
          captured_popen_kwargs.get("env", {}).get("PATH") == os.environ.get("PATH"))

    print("\n=== inspect_rosbag_topics: real metadata.yaml fixture (test_6, from a real crash) ===")
    raw_bag_dir = tmp / "test_6"
    raw_bag_dir.mkdir()
    (raw_bag_dir / "metadata.yaml").write_text("""
rosbag2_bagfile_information:
  topics_with_message_count:
    - topic_metadata:
        name: /qbot_imu
        type: sensor_msgs/msg/Imu
      message_count: 4408
    - topic_metadata:
        name: /ouster/metadata
        type: std_msgs/msg/String
      message_count: 1
    - topic_metadata:
        name: /ouster/lidar_packets
        type: ouster_sensor_msgs/msg/PacketMsg
      message_count: 47906
    - topic_metadata:
        name: /ouster/imu_packets
        type: ouster_sensor_msgs/msg/PacketMsg
      message_count: 7485
""", encoding="utf-8")
    info = core.inspect_rosbag_topics(str(raw_bag_dir))
    check("raw-packet bag: has_pointcloud2 is False", info["has_pointcloud2"] is False)
    check("raw-packet bag: raw_lidar_topic found by name match",
          info["raw_lidar_topic"] == "/ouster/lidar_packets")
    check("raw-packet bag: raw_imu_topic found by name match",
          info["raw_imu_topic"] == "/ouster/imu_packets")
    check("raw-packet bag: metadata_topic found by name match",
          info["metadata_topic"] == "/ouster/metadata")
    check("raw-packet bag: raw_lidar_count matches", info["raw_lidar_count"] == 47906)

    print("\n=== inspect_rosbag_topics: already-decoded bag is left alone ===")
    decoded_bag_dir = tmp / "already_decoded"
    decoded_bag_dir.mkdir()
    (decoded_bag_dir / "metadata.yaml").write_text("""
rosbag2_bagfile_information:
  topics_with_message_count:
    - topic_metadata:
        name: /ouster/points
        type: sensor_msgs/msg/PointCloud2
      message_count: 247
""", encoding="utf-8")
    info2 = core.inspect_rosbag_topics(str(decoded_bag_dir))
    check("decoded bag: has_pointcloud2 is True", info2["has_pointcloud2"] is True)
    check("decoded bag: pointcloud2_topic recorded", info2["pointcloud2_topic"] == "/ouster/points")

    print("\n=== inspect_rosbag_topics: not a ROS2 bag folder at all ===")
    not_a_bag_dir = tmp / "not_a_bag"
    not_a_bag_dir.mkdir()
    check("non-bag folder returns None", core.inspect_rosbag_topics(str(not_a_bag_dir)) is None)

    print("\n=== build_decode_command ===")
    decode_cmd = core.build_decode_command(
        "decode_raw_packets.py", str(raw_bag_dir), str(raw_bag_dir) + "_decoded",
        lidar_topic=info["raw_lidar_topic"], imu_topic=info["raw_imu_topic"],
        metadata_topic=info["metadata_topic"])
    check("decode command includes input bag", str(raw_bag_dir) in decode_cmd)
    check("decode command includes resolved lidar topic", "/ouster/lidar_packets" in decode_cmd)
    check("decode command includes resolved metadata topic", "/ouster/metadata" in decode_cmd)

    print("\n=== read_kiss_icp_voxel_size ===")
    kiss_config = tmp / "kiss_config.yaml"
    kiss_config.write_text(
        "mapping:\n  voxel_size: 0.05\n  max_points_per_voxel: 20\n", encoding="utf-8")
    check("reads voxel_size from a real config file",
          core.read_kiss_icp_voxel_size(str(kiss_config)) == 0.05)
    check("None for a blank/falsy path", core.read_kiss_icp_voxel_size("") is None)
    check("None for a path that doesn't exist",
          core.read_kiss_icp_voxel_size(str(tmp / "no_such_config.yaml")) is None)
    no_voxel_config = tmp / "kiss_config_no_voxel.yaml"
    no_voxel_config.write_text("mapping:\n  max_points_per_voxel: 20\n", encoding="utf-8")
    check("None when the config has no mapping.voxel_size (e.g. left at kiss-icp's own "
          "auto-derived default)",
          core.read_kiss_icp_voxel_size(str(no_voxel_config)) is None)

    print("\n=== read_kiss_icp_min_range ===")
    kiss_config_min_range = tmp / "kiss_config_min_range.yaml"
    kiss_config_min_range.write_text(
        "data:\n  min_range: 0.3\n  max_range: 15.0\n", encoding="utf-8")
    check("reads min_range from a real config file",
          core.read_kiss_icp_min_range(str(kiss_config_min_range)) == 0.3)
    check("None for a blank/falsy path", core.read_kiss_icp_min_range("") is None)
    check("None for a path that doesn't exist",
          core.read_kiss_icp_min_range(str(tmp / "no_such_config.yaml")) is None)
    no_min_range_config = tmp / "kiss_config_no_min_range.yaml"
    no_min_range_config.write_text("data:\n  max_range: 15.0\n", encoding="utf-8")
    check("None when the config has no data.min_range (e.g. left at kiss-icp's own "
          "default of 0.0/no cropping)",
          core.read_kiss_icp_min_range(str(no_min_range_config)) is None)

    print("\n=== build_kiss_icp_slam_command: voxel_size resolution and recording ===")
    kiss_fake_pcap = tmp / "kiss_capture.bag"
    kiss_fake_pcap.write_bytes(b"fake")
    kiss_project = pm.create_project(projects_root, "compartment_kiss", kiss_fake_pcap, "ros1_bag")
    kiss_baseline = kiss_project.baseline_handle()

    cmd_kiss_no_override = core.build_kiss_icp_slam_command(
        "slam_kiss_icp.py", "IGNORED_IN_PROJECT_MODE.bag", "out.ply",
        config=str(kiss_config), pipeline=kiss_baseline)
    kiss_params_1 = kiss_baseline.entry["stages"]["slam"]["params"]
    check("manually-passed source was ignored (same as every other stage)",
          "IGNORED_IN_PROJECT_MODE.bag" not in cmd_kiss_no_override)
    check("backend recorded as kiss_icp", kiss_params_1["backend"] == "kiss_icp")
    check("no override given -> effective voxel_size read from the config",
          kiss_params_1["voxel_size"] == 0.05)
    check("voxel_size_overridden is False when nothing was overridden",
          kiss_params_1["voxel_size_overridden"] is False)
    check("no --voxel-size flag on the command line when nothing was overridden",
          "--voxel-size" not in cmd_kiss_no_override)
    check("no min_range in this config -> effective min_range is None",
          kiss_params_1["min_range"] is None)
    check("min_range_overridden is False when nothing was overridden",
          kiss_params_1["min_range_overridden"] is False)
    check("no --min-range flag on the command line when nothing was overridden",
          "--min-range" not in cmd_kiss_no_override)

    cmd_kiss_override = core.build_kiss_icp_slam_command(
        "slam_kiss_icp.py", "IGNORED_IN_PROJECT_MODE.bag", "out.ply",
        config=str(kiss_config), voxel_size=0.08, min_range=0.35, pipeline=kiss_baseline)
    kiss_params_2 = kiss_baseline.entry["stages"]["slam"]["params"]
    check("explicit override wins over the config's own value",
          kiss_params_2["voxel_size"] == 0.08)
    check("voxel_size_overridden is True when one was given",
          kiss_params_2["voxel_size_overridden"] is True)
    check("--voxel-size appears on the command line when overridden",
          "--voxel-size" in cmd_kiss_override and "0.08" in cmd_kiss_override)
    check("explicit min_range override wins over the config's own (absent) value",
          kiss_params_2["min_range"] == 0.35)
    check("min_range_overridden is True when one was given",
          kiss_params_2["min_range_overridden"] is True)
    check("--min-range appears on the command line when overridden",
          "--min-range" in cmd_kiss_override and "0.35" in cmd_kiss_override)

    print("\n=== Stage 2 (Level)'s voxel_size lookup works the same for either backend ===")
    slam_params_lookup = ((kiss_baseline.entry.get("stages", {}) or {}).get("slam", {}) or {}
                           ).get("params", {}) or {}
    check("Level's lookup expression reads back KISS-ICP's recorded voxel_size",
          slam_params_lookup.get("voxel_size") == 0.08)
    check("Level's lookup expression can also tell which backend recorded it",
          slam_params_lookup.get("backend") == "kiss_icp")

    cmd_ouster_again = core.build_slam_command(
        "IGNORED.pcap", 0.3, "out2.ply", pipeline=kiss_baseline)
    ouster_params_lookup = ((kiss_baseline.entry.get("stages", {}) or {}).get("slam", {}) or {}
                             ).get("params", {}) or {}
    check("Ouster CLI records voxel_size under the SAME key as KISS-ICP",
          ouster_params_lookup.get("voxel_size") == 0.3)
    check("Ouster CLI never sets a 'backend' key (only build_kiss_icp_slam_command does)",
          "backend" not in ouster_params_lookup)

print("\nALL TESTS PASSED")
