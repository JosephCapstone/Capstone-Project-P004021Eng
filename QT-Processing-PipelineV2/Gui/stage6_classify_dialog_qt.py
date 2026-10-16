#!/usr/bin/env python3
"""
Stage 6: Classify - fields only
==================================
Defines Stage 6's fields, built field-for-field the same way
open_classify_dialog() builds them in pipeline_applet.py. A diff-bound
stage (pipeline.kind == "diff"), same group as Stage 5.

"Load RMS & Suggest Threshold" uses pm.find_icp_rms_for_path(), NOT
pipeline_core.load_rms_sidecar() - same real bug already caught and
fixed in Stage 5's "Load from Stage 3" (see that file's own notes):
load_rms_sidecar() only checks for a sidecar file next to the EXACT
path given, so it silently fails whenever "Comparison .ply (from Stage
3)" below points at anything other than the literal Stage 3 (Cleanup)
output - a Stage 4 (Segment) output, for instance. Applied here
proactively before it was independently reported, since the field's
own design made it exactly as likely to happen here as it was in
Stage 5.

Also wired: "Use per-point uncertainty (CloudCompare LOD95)" - a
checkbox toggling m3c2_classify.py's --use-uncertainty mode (flags a
point against ITS OWN local uncertainty value instead of one fixed
--threshold everywhere). Distance threshold becomes conditionally
required: needed unless the checkbox is checked, and if left blank
while checked, None gets passed through (not 0, not ""), matching
build_classify_command()'s own validation
(threshold=None, use_uncertainty=False raises ValueError - checked
here too, before a subprocess ever spawns, same as every other
required-field check in this file).

Also wired: "Find holes" - a checkbox for m3c2_classify.py's --find-holes.
M3C2 gives no distance where the new scan has no points (a hole that opens to
empty space), and Stage 6 used to ignore those points. With the box on, Stage
6 groups them, measures each group and keeps the real holes as damage sites
of the type "hole". It also finds the direction of each change (added or
removed): every damage site gets a "type", and the output file gets the
fields change_inward and change_type. The report lists the sites by type.
"Projection radius" is pre-filled from the M3C2 params file that this diff's
Stage 5 run used (half of its SearchScale), see _default_projection_radius().
The box is off by default, so a run without it is the same as before.

Run standalone:
    pip install PySide6
    python stage6_classify_dialog_qt.py
"""
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton, QMessageBox

from qt_stage_base import QStageDialog, QStagePanel, SCRIPTS_DIR, fmt_cm

try:
    import project_manager as pm
except ImportError:
    pm = None

try:
    import pipeline_core as core
except ImportError:
    core = None


def _default_projection_radius(pipeline):
    """Half of the SearchScale in the M3C2 params file that this diff's
    Stage 5 run used, in METRES as text - or "" when nothing is known
    (manual mode, Stage 5 not run, no params file, no SearchScale line).
    M3C2 finds valid values up to this distance inside a hole, so the
    measured hole is smaller than the real one by this much on each side;
    Find holes uses the value to report the corrected size. Never raises."""
    if pipeline is None or getattr(pipeline, "kind", None) != "diff":
        return ""
    if core is None or pm is None:
        return ""
    try:
        diff_stage = pipeline.entry.get("stages", {}).get("diff", {}) or {}
        params = diff_stage.get("params", {}) or {}
        params_file = params.get("m3c2_params_file") or diff_stage.get("m3c2_params_file")
        if not params_file:
            return ""
        path = Path(params_file)
        if not path.is_absolute():
            path = pm.get_absolute_path(pipeline.project, params_file)
        search_scale = core.read_m3c2_search_scale(path)
    except Exception:
        return ""
    if not search_scale or search_scale <= 0:
        return ""
    return f"{search_scale / 2.0:g}"


def _parse_interior_point(text):
    """'1.2, 0.5, 0.8' (metres) -> (1.2, 0.5, 0.8). Blank -> None. Raises
    ValueError with a plain message for anything else."""
    text = (text or "").strip()
    if not text:
        return None
    parts = [part for part in text.replace(";", ",").replace(" ", ",").split(",") if part]
    try:
        values = tuple(float(part) for part in parts)
    except ValueError:
        values = ()
    if len(values) != 3:
        raise ValueError("'Inside point' must be three numbers in metres, for example "
                         "1.2, 0.5, 0.8 - or leave it blank.")
    return values


def _site_line(c):
    """One report line for one damage site (a cluster of clusters.json).
    A site from Find holes has a type. A hole has a size, not an M3C2
    magnitude."""
    centroid = ", ".join(f"{v:.3f}" for v in c.get("centroid", []))
    kind = c.get("type")
    label = f"site {c.get('cluster_id')}" + (f" [{kind}]" if kind else "")
    line = (f"  {label}: {c.get('point_count')} points, centroid=({centroid}) m, ")
    if kind == "unmatched":
        line += (f"size {fmt_cm(round(c.get('width', 0), 4))} x "
                 f"{fmt_cm(round(c.get('height', 0), 4))}, "
                 f"fill {c.get('fill_fraction', 0):.2f} (no partner in the other scan)")
        return line + "\n"
    if kind == "hole":
        line += (f"size {fmt_cm(round(c.get('width', 0), 4))} x "
                 f"{fmt_cm(round(c.get('height', 0), 4))}, "
                 f"area {c.get('area', 0):.3f} m2")
        if c.get("corrected_area") is not None:
            line += (f" (corrected for the projection radius: "
                     f"{fmt_cm(round(c.get('corrected_width', 0), 4))} x "
                     f"{fmt_cm(round(c.get('corrected_height', 0), 4))}, "
                     f"area {c['corrected_area']:.3f} m2)")
        return line + "\n"
    return line + f"max|d|={fmt_cm(round(c.get('max_magnitude', 0), 4))}\n"


def _inside_text(extra_fields):
    """One report line: how Stage 6 found the inside of the room."""
    method = extra_fields.get("interior_method")
    fallback = extra_fields.get("n_orient_fallback") or 0
    if method == "probe":
        line = "Inside of the room: found automatically"
        if fallback:
            line += (f" (no clear answer for {fallback} points: they use the centre of all "
                     f"points)")
        return line + "\n"
    if method == "interior-point":
        return "Inside of the room: the point that you entered\n"
    if method == "centroid":
        return ("Inside of the room: the centre of all points. The scan has no clear surface "
                "to look at. If the compartment is not a simple box, enter an 'Inside "
                "point'.\n")
    return ""


def _holes_summary_text(extra_fields, clusters):
    """The Find holes lines of the report: how many sites of each type."""
    counts = {"added": 0, "removed": 0, "hole": 0, "changed": 0, "unmatched": 0}
    for c in clusters:
        if c.get("type") in counts:
            counts[c["type"]] += 1
    text = ""
    if not extra_fields.get("direction_found", True):
        text += ("The file has no point normals (nx, ny, nz), so the direction of change "
                 "could not be found. The sites of measured change have the type 'changed'.\n")
    else:
        text += (f"Added: {counts['added']} site(s), "
                 f"{extra_fields.get('n_added_points', '?')} points\n"
                 f"Removed: {counts['removed']} site(s), "
                 f"{extra_fields.get('n_removed_points', '?')} points\n")
        text += _inside_text(extra_fields)
    text += (f"Holes: {counts['hole']} ({extra_fields.get('n_hole_points', '?')} points), "
             f"from {extra_fields.get('n_missing', '?')} points without an M3C2 distance\n")
    if extra_fields.get("find_unmatched") or counts["unmatched"]:
        text += (f"Unmatched (moved or absent objects): {counts['unmatched']} site(s), "
                 f"{extra_fields.get('n_unmatched_points', '?')} points\n")
        if counts["unmatched"]:
            text += ("An unmatched site is a patch with no partner in the other scan, but "
                     "with valid points mixed in. A moved object looks like this. It is not "
                     "a hole. Look at it in CloudCompare (change_type 4).\n")
    if counts["hole"] and not extra_fields.get("projection_radius"):
        text += ("The hole sizes are the measured sizes. The real holes are larger. Enter "
                 "the 'Projection radius' to get the corrected size.\n")
    return text


class Stage6ClassifyFieldsMixin:

    def _build_classify_fields(self, pipeline=None):
        self.add_pipeline_label(pipeline)

        default_script = str(SCRIPTS_DIR / "m3c2_classify.py")
        self.add_file_field("script", "Classify script (.py):", [("Python files", "*.py")],
                             default=default_script)
        self.add_file_field("input", "M3C2 diff result .ply:", [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "input", lambda: pm.list_eligible_inputs(self.pipeline, "classify"))

        output_default = self.resolve_project_output_default(pipeline, "classify", ".ply")
        self.add_save_field("output", "Output .ply:", default_ext=".ply", default=output_default)
        self.register_auto_default(
            "output", lambda p: self.resolve_project_output_default(p, "classify", ".ply"))

        self.add_file_field(
            "comparison_for_rms", "Comparison .ply (from Stage 3, for RMS lookup):",
            [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "comparison_for_rms",
            lambda: pm.list_side_candidates(
                self.pipeline.project, self.pipeline.entry["comparison"]))
        self.add_hint(
            "Optional - only needed for the 'Load RMS' button below. This should be the "
            "Stage 3 (Cleanup) file specifically, not a Stage 4 (Segment) output or the "
            "M3C2 result itself - a Segment output here will correctly report no RMS "
            "found, since the RMS is recorded on the Cleanup entry, not carried forward "
            "onto files derived from it.")

        self.add_preset_selector("Threshold multiplier:", [
            ("2x RMS - more sensitive, more false positives from noise",
             {"rms_multiplier": "2.0"}),
            ("2.5x RMS - balanced (default)", {"rms_multiplier": "2.5"}),
            ("3x RMS - more conservative, may miss smaller real damage",
             {"rms_multiplier": "3.0"}),
        ])
        self.add_text_field("rms_multiplier", "Threshold multiplier:", default="2.5")

        self.add_length_field("threshold", "Distance threshold (cm):", default_m="0.02",
                              min_cm=0.05, max_cm=100)
        self.add_hint(
            "Points with |M3C2 distance| below this are treated as noise and dropped. Too "
            "low = false positives from scan noise; too high = real damage gets filtered "
            "out. A reasonable starting point is roughly 2-3x the registration RMS from "
            "Stage 3's ICP log - use 'Load RMS' below to compute and fill this in "
            "automatically rather than guessing. Then use 'Check Threshold Against the "
            "Diff File': the 2-3x rule is wrong when the distances have a heavy tail.")

        load_rms_btn = QPushButton("Load RMS & Suggest Threshold")
        load_rms_btn.clicked.connect(self._load_rms_and_suggest)
        self.form.addWidget(load_rms_btn)

        check_threshold_btn = QPushButton("Check Threshold Against the Diff File...")
        check_threshold_btn.setToolTip(
            "Reads the M3C2 distances of the diff file. Shows how many points the "
            "Distance threshold above flags, and how many a bell curve would flag. "
            "Warns when the distances have a heavy tail.")
        check_threshold_btn.clicked.connect(self._check_threshold_against_diff)
        self.form.addWidget(check_threshold_btn)
        self.add_hint(
            "'Check Threshold Against the Diff File' reads the distances of the file in "
            "'M3C2 diff result .ply' (5 to 30 seconds). A threshold of 2-3x the spread "
            "is correct only when the distances follow a bell curve. With a heavy tail "
            "it flags too many points, and they join into very large sites. The check "
            "warns you and suggests a threshold from the 95th percentile.")

        self.add_checkbox(
            "use_uncertainty",
            "Use per-point uncertainty (CloudCompare LOD95) instead of/with the fixed "
            "threshold")
        self.add_hint(
            "When on, a point is flagged if its M3C2 distance exceeds ITS OWN local "
            "uncertainty value (CloudCompare's LOD95: 1.96 x (local roughness + "
            "registration error)) rather than one fixed number everywhere. A single fixed "
            "threshold has to clear the worst local uncertainty anywhere in the cloud, "
            "which can bury real change in this scan's better-registered, lower-noise "
            "regions - this adapts per point instead. Leave 'Distance threshold' above "
            "blank to use this alone, or fill it in too for an extra safety margin on top "
            "of the per-point test (a point must then clear both). Requires an uncertainty "
            "field in the input file - CloudCompare writes one automatically for any M3C2 "
            "run.")

        self.add_checkbox("keep_all", "Keep all points (add a flag field instead of filtering)",
                          default=True)
        self.add_hint(
            "On by default. The output keeps every point, with a 'classified' field (1 = "
            "above the threshold) and, with clustering, a 'cluster_id' field (the damage "
            "site, -1 = none). Stage 7 then makes a full surface from all points, and "
            "Stage 8 shows only the flagged points as a separate layer (FlaggedPoints, "
            "filled in automatically). Turn this off to keep only the flagged points: the "
            "Stage 7 surface then covers only the damage areas, which usually gives a "
            "broken surface.")

        self.add_checkbox("cluster", "Cluster flagged points into damage sites", default=True)
        self.add_hint(
            "On by default. Groups the flagged points by 3D position (DBSCAN or HDBSCAN) "
            "so a spatially isolated flagged point - the profile of sensor noise or a "
            "registration artifact, not real damage - gets rejected as a second, "
            "independent filter on top of the distance threshold above. Surviving clusters "
            "(damage sites) get a per-site summary: centroid, point count, bounding extent, "
            "mean/max M3C2 magnitude. Turn this off to restore the original threshold-only "
            "behavior.")

        self.add_radio_choice("cluster_method", "Clustering method:", [
            ("DBSCAN - fixed radius, validated on this sensor/environment already (same "
             "algorithm segment_planes.py uses)", "dbscan"),
            ("HDBSCAN - adapts to varying density automatically, worth trying if flagged-"
             "point density varies a lot by surface angle/distance from scanner", "hdbscan"),
        ], default="dbscan")

        self.add_length_field(
            "cluster_eps", "Cluster gap tolerance (cm, DBSCAN only):", default_m="0.05",
            min_cm=0.1, max_cm=200)
        self.add_hint(
            "Max gap between flagged points to still count as the same damage site. M3C2 "
            "core point spacing is usually finer than a full-cloud plane segmentation pass, "
            "so this defaults tighter than Stage 4's own cluster gap tolerance - check "
            "point_spacing.py if sites split or merge unexpectedly. Not used when the "
            "method above is HDBSCAN.")

        self.add_text_field(
            "cluster_min_samples", "Cluster density (min neighbors):", default="4")
        self.add_hint(
            "How many flagged neighbors a point needs to seed a cluster at all - "
            "DBSCAN/HDBSCAN's own density parameter.")

        self.add_text_field("min_cluster_size", "Minimum damage site size (points):",
                             default="4")
        self.add_hint(
            "A separate, explicit size floor - e.g. 'require at least 4 flagged points "
            "total to count as a real site.' Clusters smaller than this are folded into "
            "the rejected/noise count, same as any other isolated point.")

        self.add_checkbox("find_holes", "Also find holes, and show added and removed material",
                          default=True)
        self.add_hint(
            "M3C2 gives no distance where the new scan has no points at all (for example a "
            "hole that opens to empty space). Stage 6 normally ignores these points. When "
            "this box is on, Stage 6 groups them, measures each group, and keeps the real "
            "holes as damage sites of the type 'hole'. It also finds the direction of each "
            "change: 'added' (the surface moved into the room) or 'removed' (the surface "
            "moved away from the room). Each damage site gets a type. The output file gets "
            "the fields change_inward (+ added, - removed, the same on every wall) and "
            "change_type. Clustering must be on: with clustering off, this option does "
            "nothing. The box is on by default. With it off, Stage 6 gives the same "
            "result as before.")

        self.add_length_field(
            "projection_radius", "Projection radius (cm, Find holes):", default_m="",
            min_cm=0.1, max_cm=200)
        self.register_auto_default("projection_radius", _default_projection_radius)
        self.add_hint(
            "Half of the Search scale in the M3C2 params file. M3C2 finds valid values up to "
            "this distance inside a hole, so the measured hole is smaller than the real hole. "
            "With this value the report also gives the corrected size. It is filled in from "
            "the params file of this diff's Stage 5 run. Blank = no correction. Used only "
            "when Find holes is on.")

        self.add_text_field("min_hole_points", "Smallest hole (points, Find holes):",
                            default="100")
        self.add_length_field(
            "min_hole_width", "Smallest hole width (cm, Find holes):", default_m="0.05",
            min_cm=0.5, max_cm=200)
        self.add_hint(
            "A group of missing points is a hole only when it has at least this many points "
            "and its short side is at least this wide. This removes thin strips along edges "
            "and corners. Two more tests are always on: a ring of valid M3C2 values around "
            "the hole (this removes shadows) and an empty inside (this removes patchy "
            "dropout).")

        self.add_text_field("interior_point",
                            "Inside point (x, y, z in m, optional, Find holes):")
        self.add_hint(
            "Blank = automatic. Stage 6 looks along each surface normal: a line into the "
            "room hits the opposite wall, and a line away from the room hits nothing. This "
            "also works in an L-shaped compartment. Enter a point only if the report says "
            "that the inside of the room could not be found for many points. Enter a point "
            "inside the room, in the coordinates of the file.")

        self.form.addStretch(1)

    def _load_rms_and_suggest(self):
        """See module docstring: uses pm.find_icp_rms_for_path(), not
        pipeline_core.load_rms_sidecar()."""
        comparison_path = self.fields["comparison_for_rms"].get().strip()
        if not comparison_path:
            QMessageBox.information(
                self, "No comparison file",
                "Fill in the 'Comparison .ply (from Stage 3)' field first - the same "
                "Stage 3 Cleanup file you used as Stage 5's Comparison input.")
            return
        if pm is None or self.pipeline is None:
            QMessageBox.information(
                self, "No active pipeline",
                "This lookup needs an active project pipeline to find the comparison "
                "file's own Stage 3 (Cleanup) run - open or create a project first, or "
                "fill in Distance threshold by hand.")
            return
        rms = pm.find_icp_rms_for_path(self.pipeline.project, comparison_path)
        if rms is None:
            QMessageBox.information(
                self, "No RMS found",
                f"No saved RMS found for:\n{comparison_path}\n\n"
                "This is recorded only on a Stage 3 (Cleanup) output that ran with an "
                "'Align to baseline' target. If this field points at a Stage 4 (Segment) "
                "output instead, use the underlying Stage 3 Cleanup file here instead.")
            return
        try:
            multiplier = float(self.fields["rms_multiplier"].get())
        except ValueError:
            QMessageBox.critical(self, "Invalid multiplier",
                                  "Threshold multiplier must be a number.")
            return

        suggested_threshold = rms * multiplier
        self.fields["threshold"].set(f"{suggested_threshold:.6f}")
        message = (
            f"Registration RMS: {fmt_cm(round(rms, 6))}\n"
            f"Multiplier: {multiplier}x\n"
            f"Suggested threshold: {fmt_cm(round(suggested_threshold, 6))} "
            f"(filled in above)\n\n"
            "This is a starting point, not a guarantee - check the flagged percentage "
            "after running (0% or ~100% means it needs adjusting either direction).")
        # The diff file is the real test of the threshold: look at its distances.
        diff_path = self.fields["input"].get().strip()
        if diff_path and Path(diff_path).exists() and core is not None:
            check = core.m3c2_threshold_check(diff_path, suggested_threshold)
            if check is not None:
                message += "\n\nCheck against the diff file:\n" + core.threshold_check_text(check)
        QMessageBox.information(self, "Threshold suggested", message)

    def _check_threshold_against_diff(self):
        """Read the M3C2 distances of the input file and compare them with the
        Distance threshold on the page (core.m3c2_threshold_check). Offers to
        put the 95th-percentile threshold in the field. A synchronous call
        with a wait cursor, same as Stage 5's 'Check Point Spacing'."""
        diff_path = self.fields["input"].get().strip()
        if not diff_path or not Path(diff_path).exists():
            QMessageBox.critical(
                self, "No diff file",
                "Fill in a valid 'M3C2 diff result .ply' first (the Stage 5 output).")
            return
        try:
            threshold = float(self.fields["threshold"].get().strip())
        except ValueError:
            QMessageBox.critical(
                self, "No threshold",
                "Fill in the Distance threshold first. The check compares it with the "
                "distances of the diff file.")
            return
        if core is None:
            QMessageBox.critical(self, "Not available",
                                 "pipeline_core.py is not loaded, so the check cannot run.")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            check = core.m3c2_threshold_check(diff_path, threshold)
        finally:
            QApplication.restoreOverrideCursor()
        if check is None:
            QMessageBox.warning(
                self, "Check Threshold",
                "No estimate. The file has no 'scalar_M3C2_distance' field, it has fewer "
                "than 100 distances, or numpy and plyfile are not installed.")
            return
        text = core.threshold_check_text(check)
        percentile = check["percentile_threshold"]
        if check["heavy_tail"]:
            answer = QMessageBox.question(
                self, "Check Threshold",
                text + f"\n\nPut {percentile * 100:.2f} cm in 'Distance threshold'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self.fields["threshold"].set(f"{percentile:.6f}")
        else:
            QMessageBox.information(self, "Check Threshold", text)

    def _build_run(self):
        """Real Run - matches open_classify_dialog()'s build() in
        pipeline_applet.py exactly, including the deferred report
        callable (build_report) that reads the *.clusters.json sidecar
        AFTER the subprocess exits, via resolve_classify_output().
        Simpler than Stage 3/4/5's own resolve_state use: Classify's
        output path is already known up front (m3c2_classify.py writes
        directly to it, no CloudCompare-style renaming needed), so
        resolve_classify_output() only ever returns extra_fields, never
        a resolved output path - resolved_state["output"] is never set
        here, matching that function's own docstring."""
        script = self.require("script", "Classify script")
        input_ply = self.require("input", "M3C2 diff result .ply")
        output = self.require("output", "Output .ply")
        threshold_text = self.fields["threshold"].get().strip()
        use_uncertainty = self.fields["use_uncertainty"].get()
        if threshold_text:
            try:
                threshold = float(threshold_text)
            except ValueError:
                raise ValueError("Distance threshold must be a number in cm, for example 2.")
        elif use_uncertainty:
            threshold = None
        else:
            raise ValueError("Distance threshold is required unless 'Use per-point "
                              "uncertainty' is checked.")
        keep_all = self.fields["keep_all"].get()
        cluster = self.fields["cluster"].get()
        cluster_method = self.fields["cluster_method"].get()
        try:
            cluster_eps = float(self.fields["cluster_eps"].get())
            cluster_min_samples = int(self.fields["cluster_min_samples"].get())
            min_cluster_size = int(self.fields["min_cluster_size"].get())
        except ValueError:
            raise ValueError("Cluster gap tolerance must be a number in cm; cluster density and "
                              "minimum damage site size must be whole numbers.")

        find_holes = self.fields["find_holes"].get()
        holes_need_clustering = bool(find_holes and not cluster)
        if holes_need_clustering:
            find_holes = False      # no clustering: nothing to find holes with
        hole_kwargs = {}
        if find_holes:
            try:
                hole_min_points = int(self.fields["min_hole_points"].get())
            except ValueError:
                raise ValueError("'Smallest hole (points)' must be a whole number.")
            width_text = self.fields["min_hole_width"].get().strip()
            radius_text = self.fields["projection_radius"].get().strip()
            try:
                hole_min_width = float(width_text) if width_text else 0.05
                projection_radius = float(radius_text) if radius_text else 0.0
            except ValueError:
                raise ValueError("'Smallest hole width' and 'Projection radius' must be "
                                 "numbers in cm.")
            interior_point = _parse_interior_point(self.fields["interior_point"].get())
            hole_kwargs = dict(find_holes=True, hole_min_points=hole_min_points,
                               hole_min_width=hole_min_width,
                               projection_radius=projection_radius,
                               interior_point=interior_point)

        active_pipeline = self.get_active_pipeline_for_run()
        cmd = core.build_classify_command(
            script, input_ply, output, threshold, keep_all,
            cluster=cluster, cluster_method=cluster_method, cluster_eps=cluster_eps,
            cluster_min_samples=cluster_min_samples, min_cluster_size=min_cluster_size,
            use_uncertainty=use_uncertainty, pipeline=active_pipeline, **hole_kwargs)

        resolved_state = {}

        def build_report():
            mode_text = ("keep all points, flag field added" if keep_all
                         else "filtered to flagged points only")
            if use_uncertainty and threshold is not None:
                threshold_text_out = (f"{fmt_cm(threshold)} fixed floor, AND per-point "
                                       f"uncertainty (a point must clear both)")
            elif use_uncertainty:
                threshold_text_out = "per-point uncertainty (CloudCompare LOD95) only"
            else:
                threshold_text_out = f"{fmt_cm(threshold)} fixed"
            summary = (
                "=== SUMMARY ===\n"
                f"Input: {input_ply}\n"
                f"Threshold: {threshold_text_out}\n"
                f"Mode: {mode_text}\n"
                f"Clustering: {'on (' + cluster_method + ')' if cluster else 'off'}\n"
                + ("Find holes: off (it needs clustering, and clustering is off)\n"
                   if holes_need_clustering else
                   f"Find holes: {'on' if find_holes else 'off'}\n")
                + f"Saved to: {output}\n"
            )

            if cluster:
                extra_fields = core.resolve_classify_output(output)
                if not extra_fields:
                    summary += (
                        "\n=== OUTPUT ===\n"
                        "No *.clusters.json sidecar found next to the output file - the "
                        "run likely failed before reaching the clustering step, or "
                        "produced no flagged points at all. Check the Terminal tab "
                        "above.\n"
                    )
                else:
                    resolved_state["extra_fields"] = extra_fields
                    clusters = extra_fields.get("clusters", [])
                    summary += (
                        f"\nFlagged (Step A): {extra_fields.get('n_flagged', '?')}, "
                        f"confirmed damage sites (Step B/C): "
                        f"{extra_fields.get('n_confirmed', '?')}, "
                        f"rejected as spatial noise: {extra_fields.get('n_noise', '?')}\n"
                    )
                    if extra_fields.get("find_holes"):
                        summary += _holes_summary_text(extra_fields, clusters)
                    summary += f"Damage sites found: {len(clusters)}\n"
                    for c in clusters:
                        summary += _site_line(c)
                    if not clusters:
                        summary += (
                            "No damage sites survived clustering - consider lowering the "
                            "cluster density / minimum site size, or raising the cluster "
                            "gap tolerance (DBSCAN), if real damage is being rejected as "
                            "noise.\n")

            summary += (
                "\n=== NOTE ===\n"
                "Point counts and the flagged percentage are in the tool output "
                "above - check that a sensible fraction of points got flagged, "
                "not 0% (threshold too high) or nearly 100% (threshold too low).\n"
                + ("" if find_holes else
                   ("Find holes was ticked, but clustering is off, so this run did not "
                    "look for holes. Turn clustering on to find them.\n"
                    if holes_need_clustering else
                    "Find holes was off. Holes that open to empty space have no M3C2 "
                    "distance, so this run did not look at them. Tick 'Also find holes' "
                    "to include them.\n"))
                + "\n=== NEXT STEPS ===\n"
                + ("Open in CloudCompare to see which points got flagged before "
                   "deciding on a final threshold. Or use this file as the input to "
                   "Stage 7 (Surface) for a full surface: Stage 8 then shows only the "
                   "flagged points as the FlaggedPoints layer.\n"
                   if keep_all else
                   "Use this file as the input to Stage 7 (Surface), or directly as "
                   "the --change input to Stage 8 (Export) if reconstruction isn't "
                   "needed.\n")
                + ("In Stage 7, leave 'change_inward' ticked in the 'Carry fields' list "
                   "(it is ticked by default). Stage 8 then colours the mesh by "
                   "change_inward: red where material was added and blue where material "
                   "was removed (and for a hole), on every wall.\n"
                   if find_holes else "")
            )
            return summary

        finish_info = {"pipeline": active_pipeline, "stage_name": "classify",
                        "output": output, "resolve_state": resolved_state}
        return cmd, build_report, finish_info


class Stage6ClassifyDialog(QStageDialog, Stage6ClassifyFieldsMixin):
    """Standalone popup - run this file directly to open just this
    dialog."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__("Stage 6: Classify", parent, on_output=on_output, on_status=on_status)
        self._build_classify_fields(pipeline=pipeline)


class Stage6ClassifyPanel(QStagePanel, Stage6ClassifyFieldsMixin):
    """Embeddable panel - used by pipeline_applet_qt_template.py as a
    page of stageStack."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__(parent, on_output=on_output, on_status=on_status)
        self._build_classify_fields(pipeline=pipeline)


def main():
    app = QApplication(sys.argv)
    dlg = Stage6ClassifyDialog(pipeline=None)
    dlg.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
