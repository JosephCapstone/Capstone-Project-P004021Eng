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

from qt_stage_base import QStageDialog, QStagePanel, SCRIPTS_DIR, fmt_cm, project_input_default

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


def _default_registration_error_was(pipeline):
    """The registration error that this diff's Stage 5 run used, in METRES as
    text, or "" when nothing is known (manual mode, Stage 5 not run, no value
    entered in Stage 5). Stage 6 needs it to replace the value in the
    uncertainty field. Never raises."""
    if pipeline is None or getattr(pipeline, "kind", None) != "diff":
        return ""
    try:
        diff_stage = pipeline.entry.get("stages", {}).get("diff", {}) or {}
        value = float(diff_stage.get("registration_error_used"))
    except (TypeError, ValueError, AttributeError):
        return ""
    return f"{value:g}" if value > 0 else ""


def _default_comparison_for_rms(pipeline):
    """Update 28. The Stage 3 (Cleanup) output of the scan that this diff
    compares, for the 'Load RMS' button, or "" (never raises)."""
    if pm is None or pipeline is None or getattr(pipeline, "kind", None) != "diff":
        return ""
    try:
        scan = pipeline.project.scan_handle(pipeline.entry["comparison"])
        output = scan.entry["stages"]["cleanup"].get("output")
        return str(pipeline.project.root / output) if output else ""
    except Exception:
        return ""


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
        self.add_hint("Classify finds the damage in the M3C2 result. It flags the points "
                      "that moved more than their uncertainty and the distance threshold, "
                      "and groups them into damage sites.")

        self.add_file_field("input", "M3C2 diff result .ply:", [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "input", lambda: pm.list_eligible_inputs(self.pipeline, "classify"))
        self.register_auto_default(
            "input", lambda p: project_input_default(p, "classify"), fill_now=True)

        output_default = self.resolve_project_output_default(pipeline, "classify", ".ply")
        self.add_save_field("output", "Output .ply:", default_ext=".ply", default=output_default)
        self.register_auto_default(
            "output", lambda p: self.resolve_project_output_default(p, "classify", ".ply"))

        self.add_length_field("threshold", "Distance threshold (cm):", default_m="0.03",
                              min_cm=0.05, max_cm=100)

        check_threshold_btn = QPushButton("Check Threshold Against the Diff File...")
        check_threshold_btn.setToolTip(
            "Reads the M3C2 distances of the diff file. Shows how many points the "
            "Distance threshold above flags, and how many a bell curve would flag. "
            "Suggests a threshold. Warns when the distances have a heavy tail.")
        check_threshold_btn.clicked.connect(self._check_threshold_against_diff)
        self.form.addWidget(check_threshold_btn)

        # Update 28: the RMS lookup (Basic): Comparison file, multiplier and the button.
        self.add_file_field(
            "comparison_for_rms", "Comparison .ply (from Stage 3, for RMS lookup):",
            [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "comparison_for_rms",
            lambda: pm.list_side_candidates(
                self.pipeline.project, self.pipeline.entry["comparison"]))
        self.register_auto_default(
            "comparison_for_rms", _default_comparison_for_rms, fill_now=True)

        self.add_preset_selector("Threshold multiplier:", [
            ("2x RMS - more sensitive, more false positives from noise",
             {"rms_multiplier": "2.0"}),
            ("2.5x RMS - balanced (default)", {"rms_multiplier": "2.5"}),
            ("3x RMS - more conservative, may miss smaller real damage",
             {"rms_multiplier": "3.0"}),
        ])
        self.add_text_field("rms_multiplier", "Threshold multiplier:", default="2.5")

        load_rms_btn = QPushButton("Load RMS && Suggest Threshold")
        load_rms_btn.clicked.connect(self._load_rms_and_suggest)
        self.form.addWidget(load_rms_btn)

        self.add_radio_choice("registration_mode", "Registration error in the uncertainty:", [
            ("Measure it from the distances (recommended). Stage 6 uses the robust spread "
             "of the distances, and at least 0.2 cm", "auto"),
            ("Keep the value that Stage 5 used", "keep"),
        ], default="auto")
        self.add_length_field(
            "registration_error_was",
            "Registration error that Stage 5 used (cm):",
            default_m=_default_registration_error_was(pipeline),
            min_cm=0.01, max_cm=50)
        # Update 28: the value follows the selected diff pipeline.
        self.register_auto_default("registration_error_was", _default_registration_error_was)

        self.add_checkbox("cluster", "Cluster flagged points into damage sites", default=True)

        self.add_checkbox("cluster_auto",
                          "Set the three cluster values from the core point spacing (auto)",
                          default=True)

        self.add_checkbox("find_holes", "Also find holes, and show added and removed material",
                          default=True)

        self.add_checkbox("size_limit", "Reject damage sites that are too small or too thin",
                          default=False)
        self.add_length_field(
            "size_min_width", "Smallest width of a site (cm):", default_m="0.10",
            min_cm=0.5, max_cm=500)
        self.add_text_field("size_min_area", "Smallest area of a site (cm2):", default="300")
        self.add_text_field("size_min_fill", "Smallest fill (0 to 1):", default="0.35")

        self.add_checkbox("shape_limit",
                          "Reject damage sites on a join of two surfaces, and thin strips",
                          default=True)
        self.add_text_field("shape_min_flatness",
                            "Smallest normal share (0 to 1):", default="0.55")
        self.add_text_field("shape_max_aspect",
                            "Largest long side / short side:", default="4.5")

        self.add_checkbox("keep_all", "Keep all points (add a flag field instead of filtering)",
                          default=True)

        # Update 29: the explanations are in "?" popups (what it does, what the
        # options do, when to change it).
        self.add_help(
            "input",
            "The M3C2 result of Stage 5. Classify reads the distance and the uncertainty "
            "of each point from it.",
            when="Change it to classify a different result. In project mode the box fills "
                 "from the selected pipeline.")
        self.add_help(
            "output",
            "The file that Classify writes.",
            when="Change it to save under another name. In project mode the name comes "
                 "from the pipeline.")
        self.add_help(
            "threshold",
            "The smallest M3C2 distance that Classify flags. A point with a smaller "
            "distance counts as noise. The value is the size of the smallest damage that "
            "Classify can find together with the uncertainty of each point.",
            effects=[
                ("Smaller", "smaller damage is found. Scan noise gives false sites."),
                ("Larger", "fewer false sites. Real damage that is smaller than the value "
                           "is removed."),
                ("Empty", "Classify uses the uncertainty alone."),
            ],
            when="The report shows many false sites (raise the value), or real damage is "
                 "missing (lower the value). 'Check Threshold Against the Diff File...' "
                 "reads the distances of the M3C2 file (5 to 30 seconds), suggests a "
                 "threshold and shows how many points it flags. It warns when many points "
                 "are far from zero (a heavy tail). These points can join into very large "
                 "sites, and a larger threshold does not solve this. Use 'Remove the "
                 "smooth background' in Advanced.",
            note="Do not use 2 to 3 times the registration RMS of Stage 3 on a SLAM map. The "
                 "RMS can be several centimetres, and the threshold then hides real damage.")
        self.add_help(
            "comparison_for_rms",
            "The Stage 3 (Cleanup) file of the comparison scan. 'Load RMS & Suggest "
            "Threshold' reads the registration RMS that Stage 3 saved for this file.",
            when="You use 'Load RMS & Suggest Threshold'. Select the Stage 3 Cleanup file, "
                 "not a Stage 4 (Segment) output and not the M3C2 result. The RMS is saved "
                 "for the Cleanup file only, and a Segment output reports no RMS.")
        self.add_help(
            "rms_multiplier",
            "The factor for the threshold. 'Load RMS & Suggest Threshold' sets the "
            "Distance threshold to this factor times the registration RMS.",
            effects=[
                ("Smaller (2)", "a more sensitive threshold. Noise gives more false "
                                "positives."),
                ("Larger (3)", "a more conservative threshold. Small real damage can be "
                               "missed."),
            ],
            when="You use 'Load RMS & Suggest Threshold'. The result is a starting point. "
                 "Check the percentage of flagged points after the run.")
        self.add_help(
            "registration_mode",
            "The uncertainty of CloudCompare includes a registration error. Stage 5 takes "
            "this value from the ICP RMS of Stage 3. On a SLAM map the ICP RMS can be "
            "large, and the uncertainty then hides small damage.",
            effects=[
                ("Measure it from the distances", "Classify replaces the value with the "
                                                  "robust spread of the distances. The "
                                                  "uncertainty then fits the real offset "
                                                  "of the scans."),
                ("Keep the value that Stage 5 used", "the uncertainty stays as Stage 5 "
                                                     "made it."),
            ],
            when="Use the measured value for a SLAM map. Keep the Stage 5 value when the "
                 "scans are aligned very well and the ICP RMS is small.")
        self.add_help(
            "registration_error_was",
            "The registration error that Stage 5 used. Classify must know this value to "
            "replace it. In project mode it reads the value from the project.",
            when="You work in manual mode: enter the value of the RegistrationError line of "
                 "the M3C2 params file. If the box is empty, Classify does not change the "
                 "uncertainty, and the report says so.")
        self.add_help(
            "cluster",
            "Groups the flagged points by 3D position (DBSCAN or HDBSCAN). A flagged point "
            "that is alone is the profile of sensor noise or a registration artifact. The "
            "grouping rejects it, as a second filter in addition to the distance "
            "threshold. Each damage site that stays gets a summary: centroid, point count, "
            "extent, and mean and maximum M3C2 distance.",
            effects=[
                ("On", "the flagged points become damage sites. Isolated points are "
                       "rejected."),
                ("Off", "only the threshold filters. The options that need clustering do "
                        "nothing."),
            ],
            when="Keep it on for a normal run.")
        self.add_help(
            "cluster_auto",
            "Classify measures the spacing of the core points in the input file. From it, "
            "Classify sets the gap tolerance, the density and the smallest site size. Fixed "
            "values do not follow the spacing. For example, a gap of 2 cm with a density of "
            "50 finds nothing in a cloud with a core spacing of 5 mm.",
            effects=[
                ("On", "the three values come from the spacing. The report shows the "
                       "values that were used."),
                ("Off", "Classify uses the three fields in Advanced (gap tolerance, "
                        "density, minimum site size)."),
            ],
            when="Keep it on for a normal run. Turn it off to set the three values by hand.")
        self.add_help(
            "find_holes",
            "M3C2 gives no distance where the new scan has no points at all, for example a "
            "hole that opens to empty space. Classify normally ignores these points. With "
            "this option, Classify groups them, measures each group, and keeps the real "
            "holes as damage sites of the type 'hole'. It also finds the direction of each "
            "change: 'added' (the surface moved into the room) or 'removed' (the surface "
            "moved away from the room). The output file gets the fields change_inward "
            "(+ added, - removed, the same on every wall) and change_type.",
            effects=[
                ("On", "holes, added material and removed material are found. Each damage "
                       "site gets a type."),
                ("Off", "Classify finds the sites by distance only."),
            ],
            when="Keep it on for a normal run.",
            note="Clustering must be on. With clustering off, this option does nothing.")
        self.add_help(
            "size_limit",
            "Most false sites of a SLAM map are small or thin: a few centimetres wide, a "
            "line along an edge, or a band of offset. A site of the type added, removed or "
            "changed stays only if its narrowest width, its area and its fill are all at "
            "or above the limits. The fill is the share of the cells inside the outline of "
            "the site that hold a point (a full patch is near 1, a ring is low). The numbers "
            "do not depend on how the site is turned or on the point spacing. Holes and "
            "unmatched sites are not tested. The rejected sites are in the report and in "
            "the .clusters.json file.",
            effects=[
                ("On", "small and thin sites are rejected."),
                ("Off", "no site is rejected for its size or shape."),
            ],
            when="The report shows many small or thin false sites. The three fields below "
                 "set the limits. Raise a limit to reject more sites.",
            note="CAUTION: damage that is smaller than the limits is removed too. Clustering "
                 "must be on.")
        self.add_help(
            "size_min_width",
            "The smallest width of a site (its narrowest width).",
            effects=[
                ("Smaller", "narrow sites stay."),
                ("Larger", "narrow sites are rejected. Real narrow damage is rejected too."),
            ],
            when="Narrow bands of offset stay as sites (raise the value). Not used when "
                 "'Reject damage sites that are too small or too thin' is off.")
        self.add_help(
            "size_min_area",
            "The smallest area of a site, in square centimetres.",
            effects=[
                ("Smaller", "small sites stay."),
                ("Larger", "small sites are rejected. Real small damage is rejected too."),
            ],
            when="Many small false sites stay (raise the value). Not used when the box "
                 "above is off.")
        self.add_help(
            "size_min_fill",
            "The smallest fill of a site, from 0 to 1. The fill is the share of the cells "
            "inside the outline of the site that hold a point. A full patch is near 1. A "
            "ring or a line is low.",
            effects=[
                ("Smaller", "sites with holes or a ring shape stay."),
                ("Larger", "only full patches stay."),
            ],
            when="Ring-shaped or line-shaped false sites stay (raise the value). Not used "
                 "when the box above is off.")
        self.add_help(
            "shape_limit",
            "Many false sites lie where two surfaces meet: the foot of a wall, the line "
            "between a wall and the ceiling, a corner. The surface normals there point in "
            "two or three directions, and the site is often a long strip. Two tests reject "
            "such sites. The normal share is the share of the points of the site whose "
            "normal is within 20 degrees of the main normal of the site. Damage on one flat "
            "surface is near 1. The long side divided by the short side rejects strips. "
            "The rejected sites are in the report and in the .clusters.json file. Holes "
            "and unmatched sites are not tested.",
            effects=[
                ("On", "sites on a join of two surfaces, and long thin strips, are "
                       "rejected."),
                ("Off", "no site is rejected for its shape."),
            ],
            when="The report shows false sites along edges and corners.",
            note="CAUTION: damage on a curved surface (a pipe, a rounded hull) has a lower "
                 "normal share and can be rejected. If the report shows a real site as "
                 "rejected, set the share lower or turn this box off. Clustering must be on.")
        self.add_help(
            "shape_min_flatness",
            "The smallest normal share of a site, from 0 to 1.",
            effects=[
                ("Smaller", "sites on a join of surfaces stay. Damage on a curved surface "
                            "is not rejected."),
                ("Larger", "more sites are rejected. Damage on a curved surface can be "
                           "rejected."),
            ],
            when="False sites on edges stay (raise the value), or real damage on a curved "
                 "surface is rejected (lower the value). Not used when 'Reject damage sites "
                 "on a join of two surfaces' is off.")
        self.add_help(
            "shape_max_aspect",
            "The largest ratio of the long side to the short side of a site. A longer "
            "site is a strip and is rejected.",
            effects=[
                ("Smaller", "more strips are rejected. Real long damage, such as a crack, "
                            "can be rejected."),
                ("Larger", "long strips stay."),
            ],
            when="Long false strips stay (lower the value), or real long damage is "
                 "rejected (raise the value). Not used when the box above is off.")
        self.add_help(
            "keep_all",
            "Controls which points the output file holds.",
            effects=[
                ("On", "the output keeps every point. It has a 'classified' field (1 = above "
                       "the threshold) and, with clustering, a 'cluster_id' field (the damage "
                       "site, -1 = none). Stage 7 then makes a full surface from all points, "
                       "and Stage 8 shows only the flagged points as a separate layer "
                       "(FlaggedPoints, filled in automatically)."),
                ("Off", "the output keeps only the flagged points. The Stage 7 surface then "
                        "covers only the damage areas. This usually gives a broken "
                        "surface."),
            ],
            when="Keep it on when you use Stage 7 and Stage 8.")

        # Settings that normally stay at their default values. The block is closed
        # when the page opens. Hidden fields still go to the run.
        self.begin_advanced("Advanced")

        default_script = str(SCRIPTS_DIR / "m3c2_classify.py")
        self.add_file_field("script", "Classify script (.py):", [("Python files", "*.py")],
                             default=default_script)
        self.add_help(
            "script",
            "The Python script that does the classification.",
            when="Change it only to use a changed copy of the script.")

        self.add_checkbox(
            "use_uncertainty",
            "Use the uncertainty of each point (CloudCompare LOD95) together with the "
            "distance threshold",
            default=True)
        self.add_help(
            "use_uncertainty",
            "Classify flags a point only if its M3C2 distance is larger than its own "
            "uncertainty. The uncertainty is the LOD95 of CloudCompare: 1.96 x (local "
            "roughness + registration error). The point must also clear the Distance "
            "threshold. The uncertainty adapts to the local noise, and the threshold sets "
            "the smallest damage. The input file must have an uncertainty field (CloudCompare "
            "writes one in each M3C2 run).",
            effects=[
                ("On", "a point must clear its own uncertainty and the threshold."),
                ("Off", "only the Distance threshold decides."),
            ],
            when="Keep it on for a normal run. To use the uncertainty alone, leave "
                 "'Distance threshold' empty.")

        self.add_checkbox("remove_background",
                          "Remove the smooth background from the distances", default=False)
        self.add_length_field(
            "background_radius", "Background radius (cm):", default_m="0.5",
            min_cm=10, max_cm=300)
        self.add_help(
            "remove_background",
            "Two scans of one room never fit perfectly. A SLAM map has a smooth offset that "
            "changes slowly over the surface. This offset can be as large as a small "
            "damage. It makes false sites, and it hides real damage. With this option, "
            "Classify subtracts the local median of the distances (the points within the "
            "radius on the same surface) before the tests. A damage has sharp edges, so it "
            "stays. The input file must have the normals (nx, ny, nz). The output file gets "
            "the field 'background'.",
            effects=[
                ("On", "the smooth offset is removed. Real damage is more clear and there "
                       "are fewer false sites from the offset."),
                ("Off", "the distances are used as they are."),
            ],
            when="The map is a SLAM map, or the scans are not aligned perfectly. With "
                 "perfect alignment the option does not change the result.",
            note="CAUTION: a damage that covers more than half of the circle of the radius is "
                 "part of the background, and Classify removes it.")
        self.add_help(
            "background_radius",
            "The radius of the circle in which Classify takes the local median of the "
            "distances.",
            effects=[
                ("Smaller", "the background follows fast changes. Large damage can be "
                            "removed as background."),
                ("Larger", "the background is smoother. Fast offset changes stay in the "
                           "distances."),
            ],
            when="Use a radius that is larger than the largest damage that you expect. Not "
                 "used when 'Remove the smooth background' is off.")

        self.add_checkbox("persist", "Keep only the sites that a second scale also finds",
                          default=False)
        self.add_file_field("persist_file", "Second M3C2 result .ply (other scale):",
                            [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "persist_file", lambda: pm.list_eligible_inputs(self.pipeline, "classify"))
        # Update 28: a file of the old diff is cleared on a pipeline switch.
        self.register_auto_default("persist_file", lambda p: "")
        self.add_length_field(
            "persist_distance", "Persistence distance (cm):", default_m="0.10",
            min_cm=1, max_cm=200)
        self.add_text_field("persist_share", "Persistence share (0 to 1):", default="0.5")
        self.add_help(
            "persist",
            "A false site often comes from the scale of one M3C2 run only: a band of offset "
            "between two scans, or noise on a curved surface. It changes when the Normal "
            "scale changes. Real damage does not. To use this option, run Stage 5 a second "
            "time on the same two scans with another Normal scale (for example half of the "
            "first), and give that result in the next field. Classify runs on both files "
            "with the same settings. The rejected sites are in the report and in the "
            ".clusters.json file. Holes and unmatched sites are not tested. The second run "
            "uses the same registration error settings as this run.",
            effects=[
                ("On", "a site stays only if the second run also finds it."),
                ("Off", "one run decides."),
            ],
            when="The report shows many false sites that you cannot remove with the "
                 "other options.",
            note="Clustering must be on.")
        self.add_help(
            "persist_file",
            "The second M3C2 result, from a Stage 5 run on the same two scans with another "
            "Normal scale.",
            when="'Keep only the sites that a second scale also finds' is on. Select the "
                 "result of the second run.")
        self.add_help(
            "persist_distance",
            "How close a point of a site of the second run must be to a point of a site "
            "of this run.",
            effects=[
                ("Smaller", "the two runs must agree more closely. More sites are "
                            "rejected."),
                ("Larger", "fewer sites are rejected."),
            ],
            when="Real sites are rejected because the two runs place them differently "
                 "(raise the value). Not used when the option is off.")
        self.add_help(
            "persist_share",
            "The share of the points of a site that must have a point of a site of the "
            "second run within the persistence distance (0.5 = half).",
            effects=[
                ("Smaller", "fewer sites are rejected."),
                ("Larger", "more sites are rejected. Real sites that the second run finds "
                           "only in part can be rejected."),
            ],
            when="False sites stay (raise the value), or real sites are rejected (lower "
                 "the value). Not used when the option is off.")

        self.add_radio_choice("cluster_method", "Clustering method:", [
            ("DBSCAN - fixed radius (the same algorithm as segment_planes.py)", "dbscan"),
            ("HDBSCAN - adapts to varying density automatically", "hdbscan"),
        ], default="dbscan")
        self.add_help(
            "cluster_method",
            "The algorithm that groups the flagged points into damage sites.",
            effects=[
                ("DBSCAN", "uses a fixed gap tolerance. It is the same algorithm that "
                           "Stage 4 uses."),
                ("HDBSCAN", "adapts to a different point density automatically."),
            ],
            when="Use HDBSCAN when the density of the flagged points changes much with the "
                 "angle to the surface or the distance from the scanner.")

        self.add_length_field(
            "cluster_eps", "Cluster gap tolerance (cm, DBSCAN only):", default_m="0.05",
            min_cm=0.1, max_cm=200)
        self.add_help(
            "cluster_eps",
            "The largest gap between flagged points that still counts as the same damage "
            "site. The core point spacing of M3C2 is usually finer than the spacing of a "
            "full-cloud plane search, so this value is smaller than the gap of Stage 4.",
            effects=[
                ("Smaller", "sites split into pieces."),
                ("Larger", "separate sites merge."),
            ],
            when="Sites split or merge when they should not. Use point_spacing.py to see the "
                 "point spacing of your data.",
            note="Used only when 'auto' is off and the method is DBSCAN.")

        self.add_text_field(
            "cluster_min_samples", "Cluster density (min neighbors):", default="50")
        self.add_help(
            "cluster_min_samples",
            "How many flagged neighbors a point needs to start a cluster. This is the "
            "density value of DBSCAN and HDBSCAN.",
            effects=[
                ("Smaller", "sparse groups of points become clusters. More noise can "
                            "join."),
                ("Larger", "only dense groups become clusters. On a coarse cloud, real "
                           "damage can be missed."),
            ],
            when="The cloud is coarse and damage is missed (lower the value). A fine cloud "
                 "needs a larger value.",
            note="Used only when 'auto' is off.")

        self.add_text_field("min_cluster_size", "Minimum damage site size (points):",
                             default="75")
        self.add_help(
            "min_cluster_size",
            "The smallest number of flagged points that counts as a damage site. Smaller "
            "clusters are counted as rejected noise.",
            effects=[
                ("Smaller", "small groups count as sites. More false sites."),
                ("Larger", "small groups are rejected. Small real damage can be rejected."),
            ],
            when="The cloud is coarse (lower the value), or many tiny false sites stay "
                 "(raise the value).",
            note="Used only when 'auto' is off.")

        self.add_checkbox("unmatched_support",
                          "Keep an unmatched site only if it has a reason (Find holes)",
                          default=True)
        self.add_help(
            "unmatched_support",
            "An unmatched site is a patch with no M3C2 distance that is not empty like a "
            "hole. It is a real change when the second scan has no points there at all (the "
            "object is gone), or when the valid points around it show a change (the object "
            "moved). It is a false site when the second scan has points there and the valid "
            "points around it show no change: the second scan was only weak on a surface "
            "that did not change (a grazing angle, a join of surfaces). Classify keeps a "
            "site if at least a quarter of its points have no point of the second scan, or "
            "at least half of the valid core points around it show a change. A site where "
            "the second scan has no points because it never looked there (a corner it did "
            "not see) is still kept: the file cannot tell a gap from a removed object. The "
            "rejected sites are in the report.",
            effects=[
                ("On", "unmatched sites without a reason are rejected."),
                ("Off", "all unmatched sites stay."),
            ],
            when="The report shows many unmatched false sites.",
            note="Used only when Find holes is on.")

        self.add_checkbox("label_noise",
                          "Also label the rejected noise points as added/removed (old behaviour)",
                          default=False)
        self.add_help(
            "label_noise",
            "Normally only the points of a kept site have change_type 1 (added) or 2 "
            "(removed). All other points have 0. The 'classified' field (1 = above the "
            "threshold) and change_inward (the signed distance, for all points) do not "
            "change. Colour the cloud by change_type or by cluster_id to see only the "
            "sites.",
            effects=[
                ("On", "every flagged point gets change_type 1 or 2, also the points that "
                       "the clustering or a site limit rejected. On a large scan this can "
                       "be many points, shown as added or removed material with no site."),
                ("Off", "only the points of a kept site get change_type 1 or 2."),
            ],
            when="You need the old labels, for example to compare with an old result.",
            note="Used only when Find holes is on.")

        self.add_length_field(
            "projection_radius", "Projection radius (cm, Find holes):", default_m="",
            min_cm=0.1, max_cm=200)
        self.register_auto_default("projection_radius", _default_projection_radius)
        self.add_help(
            "projection_radius",
            "Half of the Search scale in the M3C2 params file. M3C2 finds valid values up "
            "to this distance inside a hole, so the measured hole is smaller than the real "
            "hole. With this value the report also gives the corrected size. The box is "
            "filled from the params file of the Stage 5 run of this diff.",
            effects=[
                ("Value", "the report gives the corrected hole size."),
                ("Empty", "no correction."),
            ],
            when="You work in manual mode: enter half of the Search scale of Stage 5.",
            note="Used only when Find holes is on.")

        self.add_text_field("min_hole_points", "Smallest hole (points, Find holes):",
                            default="100")
        self.add_length_field(
            "min_hole_width", "Smallest hole width (cm, Find holes):", default_m="0.05",
            min_cm=0.5, max_cm=200)
        self.add_help(
            "min_hole_points",
            "A group of missing points is a hole only when it has at least this many "
            "points. Two more tests are always on: a ring of valid M3C2 values around the "
            "hole (this removes shadows) and an empty inside (this removes patchy "
            "dropout).",
            effects=[
                ("Smaller", "small groups count as holes."),
                ("Larger", "only big groups count as holes."),
            ],
            when="Small false holes stay (raise the value), or a small real hole is "
                 "missing (lower the value).",
            note="Used only when Find holes is on.")
        self.add_help(
            "min_hole_width",
            "A group of missing points is a hole only when its short side is at least this "
            "wide. This removes thin strips along edges and corners.",
            effects=[
                ("Smaller", "thin strips can count as holes."),
                ("Larger", "narrow holes are rejected."),
            ],
            when="Thin strips along edges are called holes (raise the value).",
            note="Used only when Find holes is on.")

        self.add_checkbox("density_drop",
                          "Find holes also by the drop in point density (Find holes)",
                          default=True)
        self.add_text_field("density_drop_ratio", "Density drop limit (ratio, 0 to 1):",
                            default="0.25")
        self.add_help(
            "density_drop",
            "Where a hole opens in the new scan, M3C2 can still give a distance for the "
            "points at its edge, because the cylinder reaches the points around the hole. "
            "That distance is not reliable. The number of points of the new scan in the "
            "cylinder drops, and this shows the hole. Classify treats such a point as "
            "'no data'. Stage 5 must write the two count fields (the M3C2 params file has "
            "ExportDensityAtProjScale=true; 'Generate Params File...' sets it). A Stage 5 "
            "result from an older params file has no count fields. Classify then does "
            "nothing for this option, and the report says so.",
            effects=[
                ("On", "holes are also found by the drop in density. Some more false "
                       "sites can appear."),
                ("Off", "holes are found only from the missing distances."),
            ],
            when="A hole is missed because M3C2 still gives distances at its edge.",
            note="Used only when Find holes is on.")
        self.add_help(
            "density_drop_ratio",
            "A point counts as 'no data' if the count of the new scan in its cylinder is "
            "less than this ratio times the normal ratio of the two scans (the median). "
            "0.25 means less than a quarter.",
            effects=[
                ("Smaller", "only a strong drop counts. Weak holes are missed."),
                ("Larger", "a small drop also counts. More false holes."),
            ],
            when="Holes are missed (raise the value), or false holes appear (lower the "
                 "value). Not used when the density option is off.")

        self.add_text_field("interior_point",
                            "Inside point (x, y, z in m, optional, Find holes):")
        self.add_help(
            "interior_point",
            "A point inside the room, in the coordinates of the file. Classify uses it to "
            "know which side of a surface is the inside of the room. With an empty box, "
            "Classify looks along each surface normal: a line into the room hits the "
            "opposite wall, and a line away from the room hits nothing. This also works in "
            "an L-shaped compartment.",
            effects=[
                ("Empty", "automatic."),
                ("Point", "Classify uses your point."),
            ],
            when="The report says that the inside of the room could not be found for many "
                 "points.",
            note="Used only when Find holes is on.")

        self.end_advanced()
        self.keep_field_order("script", "input", "output")

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
        put the suggested threshold (the larger of the floor and 4 x the robust spread) in the field. A synchronous call
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
        suggested = check["suggested_threshold"]
        # Offer the suggestion when it is not the value in the field (0.1 cm tolerance).
        if abs(suggested - threshold) > 0.001:
            answer = QMessageBox.question(
                self, "Check Threshold",
                text + f"\n\nPut {suggested * 100:.2f} cm in 'Distance threshold'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self.fields["threshold"].set(f"{suggested:.6f}")
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
        registration_error = None
        registration_error_was = None
        registration_note = ""
        if use_uncertainty and self.fields["registration_mode"].get() == "auto":
            was_text = self.fields["registration_error_was"].get().strip()
            if was_text:
                try:
                    registration_error_was = float(was_text)
                except ValueError:
                    raise ValueError("'Registration error that Stage 5 used' must be a "
                                     "number in cm.")
                registration_error = "auto"
            else:
                registration_note = (
                    "The registration error that Stage 5 used is not known, so Stage 6 did "
                    "not change the uncertainty. Enter the value in 'Registration error "
                    "that Stage 5 used' to measure it from the distances.\n")
        remove_background = None
        if self.fields["remove_background"].get():
            radius_text = self.fields["background_radius"].get().strip()
            try:
                remove_background = float(radius_text)
            except ValueError:
                raise ValueError("'Background radius' must be a number in cm, for example 50.")
            if remove_background <= 0:
                raise ValueError("'Background radius' must be larger than 0.")
        persist_with = None
        persist_distance = 0.10
        persist_share = 0.5
        if self.fields["persist"].get():
            persist_with = self.fields["persist_file"].get().strip()
            if not persist_with:
                raise ValueError("Give the second M3C2 result file (other scale), or turn "
                                 "off 'Keep only the sites that a second scale also finds'.")
            if str(persist_with) == str(input_ply):
                raise ValueError("The second M3C2 result must be another file than the first. "
                                 "Run Stage 5 again with another Normal scale.")
            try:
                persist_distance = float(self.fields["persist_distance"].get())
                persist_share = float(self.fields["persist_share"].get())
            except ValueError:
                raise ValueError("'Persistence distance' must be a number in cm and "
                                 "'Persistence share' a number between 0 and 1.")
            if not 0.0 <= persist_share <= 1.0:
                raise ValueError("'Persistence share' must be between 0 and 1, for "
                                 "example 0.5.")
            if persist_distance <= 0:
                raise ValueError("'Persistence distance' must be larger than 0.")
        min_site_width = min_site_area = min_site_fill = 0.0
        if self.fields["size_limit"].get():
            try:
                min_site_width = float(self.fields["size_min_width"].get())
                min_site_area = float(self.fields["size_min_area"].get()) / 1.0e4   # cm2 to m2
                min_site_fill = float(self.fields["size_min_fill"].get())
            except ValueError:
                raise ValueError("'Smallest width' must be a number in cm, 'Smallest area' a "
                                 "number in cm2 and 'Smallest fill' a number between 0 and 1.")
            if min_site_width < 0 or min_site_area < 0:
                raise ValueError("The size limits must not be negative.")
            if not 0.0 <= min_site_fill <= 1.0:
                raise ValueError("'Smallest fill' must be between 0 and 1, for example 0.35.")
        min_site_flatness = max_site_aspect = 0.0
        if self.fields["shape_limit"].get():
            try:
                min_site_flatness = float(self.fields["shape_min_flatness"].get())
                max_site_aspect = float(self.fields["shape_max_aspect"].get())
            except ValueError:
                raise ValueError("'Smallest normal share' must be a number between 0 and 1, and "
                                 "'Largest long side / short side' must be a number.")
            if not 0.0 <= min_site_flatness <= 1.0:
                raise ValueError("'Smallest normal share' must be between 0 and 1, for "
                                 "example 0.55.")
            if max_site_aspect < 0:
                raise ValueError("'Largest long side / short side' must not be negative.")
        keep_all = self.fields["keep_all"].get()
        cluster = self.fields["cluster"].get()
        cluster_method = self.fields["cluster_method"].get()
        cluster_auto = bool(self.fields["cluster_auto"].get())
        if cluster_auto:
            cluster_eps, cluster_min_samples, min_cluster_size = "auto", "auto", "auto"
        else:
            try:
                cluster_eps = float(self.fields["cluster_eps"].get())
                cluster_min_samples = int(self.fields["cluster_min_samples"].get())
                min_cluster_size = int(self.fields["min_cluster_size"].get())
            except ValueError:
                raise ValueError("Cluster gap tolerance must be a number in cm; cluster density "
                                 "and minimum damage site size must be whole numbers.")

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
            density_drop = None
            if self.fields["density_drop"].get():
                try:
                    density_drop = float(self.fields["density_drop_ratio"].get())
                except ValueError:
                    raise ValueError("'Density drop limit' must be a number between 0 and 1.")
                if not 0.0 < density_drop < 1.0:
                    raise ValueError("'Density drop limit' must be larger than 0 and "
                                     "smaller than 1, for example 0.25.")
            hole_kwargs = dict(find_holes=True, hole_min_points=hole_min_points,
                               density_drop=density_drop,
                               hole_min_width=hole_min_width,
                               projection_radius=projection_radius,
                               interior_point=interior_point,
                               unmatched_support=bool(self.fields["unmatched_support"].get()),
                               label_noise=bool(self.fields["label_noise"].get()))

        active_pipeline = self.get_active_pipeline_for_run()
        cmd = core.build_classify_command(
            script, input_ply, output, threshold, keep_all,
            cluster=cluster, cluster_method=cluster_method, cluster_eps=cluster_eps,
            cluster_min_samples=cluster_min_samples, min_cluster_size=min_cluster_size,
            use_uncertainty=use_uncertainty, pipeline=active_pipeline,
            registration_error=registration_error,
            registration_error_was=registration_error_was,
            remove_background=remove_background,
            persist_with=persist_with, persist_distance=persist_distance,
            persist_share=persist_share,
            min_site_width=min_site_width if cluster else 0.0,
            min_site_area=min_site_area if cluster else 0.0,
            min_site_fill=min_site_fill if cluster else 0.0,
            min_site_flatness=min_site_flatness if cluster else 0.0,
            max_site_aspect=max_site_aspect if cluster else 0.0, **hole_kwargs)

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
                + (f"Registration error in the uncertainty: measured from the distances "
                   f"(Stage 5 used {fmt_cm(registration_error_was)})\n"
                   if registration_error else "")
                + registration_note
                + (f"Smooth background removed: radius {fmt_cm(remove_background)}\n"
                   if remove_background else "")
                + (f"Persistence: kept only the sites that {persist_with} also finds "
                   f"(share {persist_share:g}, distance {fmt_cm(persist_distance)})\n"
                   if persist_with and cluster else "")
                + (f"Size limits: width at least {fmt_cm(min_site_width)}, area at least "
                   f"{min_site_area * 1e4:g} cm2, fill at least {min_site_fill:g}\n"
                   if (min_site_width > 0 or min_site_area > 0 or min_site_fill > 0) and cluster
                   else "")
                + (f"Shape limits: normal share at least {min_site_flatness:g}, long side at "
                   f"most {max_site_aspect:g} times the short side\n"
                   if (min_site_flatness > 0 or max_site_aspect > 0) and cluster else "")
                + f"Mode: {mode_text}\n"
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
                    if hole_kwargs.get("density_drop"):
                        if extra_fields.get("n_density_low") is not None:
                            summary += (f"Density drop: {extra_fields['n_density_low']} core "
                                        f"points have a count of the new scan below "
                                        f"{hole_kwargs['density_drop']:g} times the normal "
                                        "ratio. They are treated as points without a "
                                        "distance.\n")
                        else:
                            summary += ("Density drop was on, but the input file has no "
                                        "point-count fields, so it did nothing. Make the "
                                        "params file again with 'Generate Params File...' "
                                        "(it has ExportDensityAtProjScale=true) and run "
                                        "Stage 5 again.\n")
                    if extra_fields.get("n_size_rejected") is not None:
                        summary += (f"Size limits: {extra_fields['n_size_rejected']} site(s) "
                                    f"rejected as too small, too thin or too empty "
                                    f"({extra_fields.get('n_size_rejected_points', 0)} "
                                    "points).\n")
                        for r in extra_fields.get("size_rejected", []):
                            at = ", ".join(f"{v:.2f}" for v in r["centroid"])
                            summary += (f"  Rejected: {r['point_count']} points at ({at}); "
                                        f"{'; '.join(r['reasons'])}.\n")
                    auto = extra_fields.get("cluster_auto")
                    if auto:
                        summary += (f"Cluster values (auto): the core spacing is "
                                    f"{auto['spacing'] * 1000:.1f} mm. Gap tolerance "
                                    f"{fmt_cm(extra_fields.get('cluster_eps') or auto['eps'])}, "
                                    f"density {extra_fields.get('cluster_min_samples')}, "
                                    f"smallest site {extra_fields.get('min_cluster_size')} "
                                    "points.\n")
                    if extra_fields.get("n_shape_rejected") is not None:
                        summary += (f"Shape limits: {extra_fields['n_shape_rejected']} site(s) "
                                    f"rejected as lying on more than one surface or as a thin "
                                    f"strip ({extra_fields.get('n_shape_rejected_points', 0)} "
                                    "points).\n")
                        for r in extra_fields.get("shape_rejected", []):
                            at = ", ".join(f"{v:.2f}" for v in r["centroid"])
                            summary += (f"  Rejected: {r['point_count']} points at ({at}); "
                                        f"{'; '.join(r['reasons'])}.\n")
                    if extra_fields.get("unmatched_support"):
                        summary += (f"Unmatched support: "
                                    f"{extra_fields.get('n_unmatched_support_rejected', 0)} "
                                    "unmatched site(s) rejected (the second scan has points "
                                    "there and the valid points around show no change).\n")
                        for r in extra_fields.get("unmatched_support_rejected", []):
                            at = ", ".join(f"{v:.2f}" for v in r["centroid"])
                            summary += (f"  Rejected: {r['point_count']} points at ({at}).\n")
                    if extra_fields.get("n_unlabelled_flagged"):
                        summary += (f"change_type: {extra_fields['n_unlabelled_flagged']} flagged "
                                    "points are in no kept site. They have change_type 0.\n")
                    if persist_with and cluster:
                        if extra_fields.get("n_persist_rejected") is not None:
                            summary += (f"Persistence: {extra_fields['n_persist_rejected']} "
                                        f"site(s) rejected because the second scale does "
                                        f"not find them "
                                        f"({extra_fields.get('n_persist_rejected_points', 0)} "
                                        "points).\n")
                            for r in extra_fields.get("persist_rejected", []):
                                at = ", ".join(f"{v:.2f}" for v in r["centroid"])
                                summary += (f"  Rejected: {r['point_count']} points at ({at}); "
                                            f"{r['persist_share'] * 100:.0f} % of the points "
                                            "have a partner in the second run.\n")
                        else:
                            summary += ("Persistence was on, but there were no sites to "
                                        "test.\n")
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
