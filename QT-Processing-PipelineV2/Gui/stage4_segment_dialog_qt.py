#!/usr/bin/env python3
"""
Stage 4: Segment - fields only
=================================
Defines Stage 4's fields, built field-for-field the same way
open_segment_dialog() builds them in pipeline_applet.py. The last
stage in the "Per Scan" group (Stages 1-4).

One structural difference from every stage so far: Segment's output is
a FOLDER, not a single file - segment_planes.py writes several files
together (one cloud per detected surface, a combined
<name>_classified.ply, an envelope file, a manifest.json) into one
directory. That needs qt_stage_base.py's add_folder_field() rather
than add_save_field(), and _default_segment_output_dir() below (ported
directly from pipeline_applet.py's module-level function of the same
name) rather than resolve_project_output_default() - see that
function's own docstring for why get_output_path() itself can't be
reused for a folder.

Real Run behavior (build_segment_command(), the manifest.json-based
output resolution via resolve_segment_output()) matches
open_segment_dialog()'s build() in pipeline_applet.py exactly - report
is again a DEFERRED callable (see Stage 3's own note on why), since
the manifest naming every detected surface, the envelope files, and
which classified-cloud path this stage's "output" should record all
only exist once segment_planes.py has actually finished writing them.

Run standalone:
    pip install PySide6
    python stage4_segment_dialog_qt.py
"""
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QLabel

from qt_stage_base import QStageDialog, QStagePanel, SCRIPTS_DIR, fmt_cm

try:
    import project_manager as pm
except ImportError:
    pm = None

try:
    import pipeline_core as core
except ImportError:
    core = None


def _default_segment_output_dir(pipeline):
    """Computes a fresh sequence-numbered folder name for Stage 4's
    output, e.g. 'compartment_segment_001' - the same naming
    convention project_manager.get_output_path() uses for every other
    stage's single-file output, just applied to a folder instead.
    Ported directly from pipeline_applet.py's _default_segment_output_dir():
    counts existing SUBFOLDERS directly rather than reusing
    get_output_path() itself, since that greps for FILES with a
    matching extension - which would never see a past run's own
    subfolder (a directory has no matching extension) and would keep
    returning '001' forever.

    Returns "" if pipeline is None (manual mode). Registered with
    register_auto_default(), so it re-resolves on every pipeline switch
    and after every successful run (see qt_stage_base.py)."""
    if pipeline is None:
        return ""
    folder = pipeline.root / pipeline.stage_folders["segment"]
    compartment = pipeline.project.data["compartment"]
    prefix = f"{compartment}_segment_"
    existing_numbers = []
    if folder.is_dir():
        for entry in folder.iterdir():
            if entry.is_dir() and entry.name.startswith(prefix):
                stem = entry.name[len(prefix):]
                if stem.isdigit():
                    existing_numbers.append(int(stem))
    next_seq = (max(existing_numbers) + 1) if existing_numbers else 1
    return str(folder / f"{prefix}{next_seq:03d}")


class Stage4SegmentFieldsMixin:

    def _build_segment_fields(self, pipeline=None):
        self.add_pipeline_label(pipeline)
        optional_note = QLabel(
            "Optional stage. You can go from Stage 3 (Cleanup) directly to Stage 5 (Diff): "
            "Stage 5 then uses the Stage 3 output. Without Stage 4, the points outside the "
            "room are not removed, and Stage 8 cannot show surface names on the damage "
            "sites.")
        optional_note.setWordWrap(True)
        optional_note.setStyleSheet("color: #b8860b; font-weight: bold;")
        self.form.addWidget(optional_note)

        default_script = str(SCRIPTS_DIR / "segment_planes.py")
        self.add_file_field("script", "Segment script (.py):", [("Python files", "*.py")],
                             default=default_script)
        self.add_file_field("input", "Cleaned .ply (ideally leveled first):",
                             [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "input", lambda: pm.list_eligible_inputs(self.pipeline, "segment"))

        self.add_folder_field(
            "output_dir", "Output folder:", default=_default_segment_output_dir(pipeline))
        self.register_auto_default("output_dir", _default_segment_output_dir)
        self.add_hint(
            "A fresh, sequence-numbered folder each run keeps every past run's own files "
            "from overwriting an earlier run's - pre-filled above, matching every other "
            "stage's own output naming convention (just used as a folder name here instead "
            "of a file name). Every .ply this run writes is also prefixed with this folder's "
            "own name, so files stay distinguishable even if opened outside their folder. "
            "The one file that matters most for carrying into the rest of a pipeline, "
            "<name>_classified.ply (every input point, with a 'classification' field), is "
            "what gets recorded as this stage's own output.")

        self.add_checkbox(
            "write_separate_surfaces",
            "Also write each surface as its own separate .ply file", default=False)
        self.add_hint(
            "Off by default - <name>_classified.ply already carries every point through "
            "the rest of the pipeline as one cloud. Turn this on to ALSO get "
            "<name>_floor.ply, <name>_wall_1.ply, and so on, plus <name>_unclassified.ply, "
            "written into the output folder alongside <name>_classified.ply - useful for "
            "visually tuning parameters per surface, or so each surface becomes its own "
            "separate prim in Omniverse (click a wall in the Stage panel to hide it). "
            "manifest.json always lists every detected surface's name, point count, "
            "normal, and Z range either way - only its 'file' entry per surface is left "
            "blank when this is off.")

        self.add_checkbox(
            "envelope_filter",
            "Flag unclassified points that sit outside the room as junk", default=True)
        self.add_length_field("envelope_margin", "Envelope margin (cm):", default_m="0.15",
                              min_cm=0.5, max_cm=200)
        self.add_checkbox(
            "write_envelope_filtered",
            "Also write a separate copy with outside-envelope points removed", default=True)
        self.add_hint(
            "On by default. The detected floor/ceiling/wall points already describe the "
            "room's own footprint and height range - any unclassified point that falls "
            "outside BOTH of those (with the margin's worth of slack) is very unlikely to "
            "be real interior content, and much more likely scan noise or a stray return "
            "from beyond the walls. This adds an 'outside_envelope' field (0/1) to "
            "<name>_classified.ply - labeled, not removed; every point stays in that file "
            "either way, same as the 'classification' field already works. Margin: how "
            "much slack (in both the footprint and the height range) before a point "
            "counts as outside - a real wall's own points scatter a little around its "
            "true position, so some slack avoids flagging real clutter near a wall as "
            "junk; too large a value can let real outside junk through unflagged instead. "
            "The separate-copy box (on by default) ALSO writes <name>_envelope_filtered.ply - "
            "<name>_classified.ply with the flagged points actually removed. Stage 5 (Diff) "
            "uses this copy, so the junk outside the room does not go into M3C2 and cannot "
            "show as false damage. If no point is outside the room, the copy is not written "
            "(it would be the same as <name>_classified.ply).")

        self.add_preset_selector("Distance threshold preset:", [
            ("Default (5 cm, 20 max planes) - tested combo for a full room/compartment scan",
             {"distance_threshold": "0.05", "max_planes": "20",
              "min_inlier_fraction": "0.003", "cluster_eps": "0.5"}),
            ("Tight (2 cm) - matches level_cloud.py's own script default",
             {"distance_threshold": "0.02"}),
            ("Medium (10 cm) - try if a real wall/floor doesn't survive as its own plane",
             {"distance_threshold": "0.1"}),
            ("Loose (25 cm)", {"distance_threshold": "0.25"}),
        ])
        self.add_length_field("distance_threshold", "Distance threshold (cm):", default_m="0.05",
                              min_cm=0.1, max_cm=200)
        self.add_hint(
            "RANSAC plane-fit tolerance - same idea as Stage 2 (Level)'s own field. Roughly "
            "match or exceed the voxel size used in Stage 1 (SLAM), for the same reason "
            "Level's own hint explains: coarser voxels quantize points further from a "
            "perfect plane than a tight tolerance allows.")
        self.add_text_field("max_planes", "Max planes to search:", default="20")
        self.add_text_field(
            "min_inlier_fraction", "Min plane size (fraction of all points):", default="0.003")
        self.add_hint(
            "A candidate plane must contain at least this fraction of all points to be "
            "accepted at all. Raise this (e.g. 0.015) if too many small, spurious surfaces "
            "are getting accepted instead of falling into 'unclassified'.")
        self.add_text_field("horizontal_threshold", "Horizontal threshold:", default="0.7")
        self.add_length_field("max_horizontal_z_span", "Max horizontal Z span (cm):",
                              default_m="0.3", min_cm=0.5, max_cm=500)
        self.add_hint(
            "Horizontal threshold: how close to perfectly flat (|normal.z|, 1.0 = exactly "
            "horizontal) a plane must be to count as floor/ceiling rather than a wall. Max "
            "horizontal Z span: a near-horizontal candidate whose points span more Z than "
            "this is rejected as a likely diagonal-slice artifact rather than a real "
            "floor/ceiling/table - a real one should only span its own thickness/noise, "
            "not the better part of a meter.")

        self.add_checkbox(
            "cluster_filter", "Filter each surface to its largest connected cluster",
            default=True)
        self.add_length_field("cluster_eps", "Cluster gap tolerance (cm):", default_m="0.5",
                              min_cm=0.5, max_cm=500)
        self.add_text_field("cluster_min_points", "Cluster min points:", default="20")
        self.add_hint(
            "RANSAC's inlier test only checks distance to the infinite plane equation, not "
            "a surface's real physical boundary - this keeps only the largest "
            "spatially-connected group of points per plane (DBSCAN), moving disconnected "
            "stray points to 'unclassified' instead of leaving them mixed into e.g. "
            "floor.ply. Gap tolerance: max gap between points to still count as connected. "
            "Min points: minimum points to count as a real cluster at all. Both are "
            "ignored if the checkbox above is unchecked.")

        self.add_checkbox(
            "merge_coplanar", "Merge split detections of the same physical plane",
            default=True)
        self.add_text_field(
            "merge_normal_cos", "Merge normal similarity (1.0 = exactly parallel):",
            default="0.98")
        self.add_length_field("merge_distance", "Merge plane distance (cm):", default_m="0.1",
                              min_cm=0.1, max_cm=200)
        self.add_hint(
            "Confirmed on real data: a wall obstructed mid-span by clutter can get "
            "detected as two separate, duplicate-looking surfaces instead of one - and a "
            "ceiling split this way loses its other half to a misleading "
            "'horizontal_surface_N' label instead of being combined back in. Both fields "
            "below are ignored if the checkbox above is unchecked.")

        self.form.addStretch(1)

    def _build_run(self):
        """Real Run - matches open_segment_dialog()'s build() in
        pipeline_applet.py exactly, including the deferred report
        callable (build_report) that reads manifest.json AFTER the
        subprocess exits, via resolve_segment_output()."""
        script = self.require("script", "Segment script")
        input_ply = self.require("input", "Cleaned .ply")
        output_dir = self.require("output_dir", "Output folder")
        try:
            distance_threshold = float(self.fields["distance_threshold"].get())
            max_planes = int(self.fields["max_planes"].get())
            min_inlier_fraction = float(self.fields["min_inlier_fraction"].get())
            horizontal_threshold = float(self.fields["horizontal_threshold"].get())
            max_horizontal_z_span = float(self.fields["max_horizontal_z_span"].get())
        except ValueError:
            raise ValueError("Distance threshold, min plane size, horizontal threshold, and "
                              "max horizontal Z span must be numbers; max planes must be a "
                              "whole number.")
        cluster_filter = self.fields["cluster_filter"].get()
        merge_coplanar = self.fields["merge_coplanar"].get()
        write_separate_surfaces = self.fields["write_separate_surfaces"].get()
        envelope_filter = self.fields["envelope_filter"].get()
        write_envelope_filtered = self.fields["write_envelope_filtered"].get()
        try:
            cluster_eps = float(self.fields["cluster_eps"].get())
            cluster_min_points = int(self.fields["cluster_min_points"].get())
            merge_normal_cos = float(self.fields["merge_normal_cos"].get())
            merge_distance = float(self.fields["merge_distance"].get())
            envelope_margin = float(self.fields["envelope_margin"].get())
        except ValueError:
            raise ValueError("Cluster gap tolerance, merge normal similarity, merge plane "
                              "distance, and envelope margin must be numbers; cluster min "
                              "points must be a whole number.")

        active_pipeline = self.get_active_pipeline_for_run()
        cmd = core.build_segment_command(
            script, input_ply, output_dir,
            distance_threshold=distance_threshold, max_planes=max_planes,
            horizontal_threshold=horizontal_threshold,
            max_horizontal_z_span=max_horizontal_z_span,
            min_inlier_fraction=min_inlier_fraction, cluster_filter=cluster_filter,
            cluster_eps=cluster_eps, cluster_min_points=cluster_min_points,
            merge_coplanar=merge_coplanar, merge_normal_cos=merge_normal_cos,
            merge_distance=merge_distance, write_separate_surfaces=write_separate_surfaces,
            envelope_filter=envelope_filter, envelope_margin=envelope_margin,
            write_envelope_filtered=write_envelope_filtered,
            pipeline=active_pipeline)

        resolved_state = {}

        def build_report():
            summary = (
                "=== SUMMARY ===\n"
                f"Input: {input_ply}\n"
                f"Output folder: {output_dir}\n"
                f"Distance threshold: {fmt_cm(distance_threshold)}, max planes: {max_planes}\n"
            )

            classified_path, extra_fields = core.resolve_segment_output(output_dir)
            if classified_path is None:
                summary += (
                    "\n=== OUTPUT ===\n"
                    "No manifest.json found in the output folder, or it had no usable "
                    "classified-cloud record - the run likely failed before writing one. "
                    "Check the Terminal tab above for the actual error.\n"
                )
            else:
                resolved_state["output"] = classified_path
                if active_pipeline is not None:
                    converted = {}
                    if "envelope_output" in extra_fields:
                        try:
                            converted["envelope_output"] = pm.to_relative_path(
                                active_pipeline.project, extra_fields["envelope_output"])
                        except pm.ProjectError:
                            converted["envelope_output"] = extra_fields["envelope_output"]
                    if "classification_ids" in extra_fields:
                        converted["classification_ids"] = extra_fields["classification_ids"]
                    if "surfaces" in extra_fields:
                        converted_surfaces = []
                        for surface in extra_fields["surfaces"]:
                            surface = dict(surface)
                            if surface.get("file"):
                                try:
                                    surface["file"] = pm.to_relative_path(
                                        active_pipeline.project, surface["file"])
                                except pm.ProjectError:
                                    pass
                            converted_surfaces.append(surface)
                        converted["surfaces"] = converted_surfaces
                    if "envelope_filtered_output" in extra_fields:
                        try:
                            converted["envelope_filtered_output"] = pm.to_relative_path(
                                active_pipeline.project,
                                extra_fields["envelope_filtered_output"])
                        except pm.ProjectError:
                            converted["envelope_filtered_output"] = \
                                extra_fields["envelope_filtered_output"]
                    for passthrough_key in ("envelope_filter_applied", "envelope_margin_used",
                                             "n_outside_envelope"):
                        if passthrough_key in extra_fields:
                            converted[passthrough_key] = extra_fields[passthrough_key]
                    resolved_state["extra_fields"] = converted

                surface_names = [s["name"] for s in extra_fields.get("surfaces", [])]
                summary += (
                    f"\nFound {len(surface_names)} surface(s): "
                    f"{', '.join(surface_names) or '(none)'}\n"
                    f"Combined classified cloud saved to: {classified_path}\n"
                )
                if "envelope_output" in extra_fields:
                    summary += (
                        f"Envelope-only cloud (ready-made input for Stage 7/Surface's "
                        f"unified shell reconstruction): {extra_fields['envelope_output']}\n")
                if extra_fields.get("envelope_filter_applied"):
                    summary += (
                        f"Envelope-based filter (margin="
                        f"{extra_fields.get('envelope_margin_used')} m): "
                        f"{extra_fields.get('n_outside_envelope', 0)} unclassified point(s) "
                        f"flagged 'outside_envelope=1' (likely scan noise/junk beyond the "
                        f"walls) - kept in classified.ply, not removed.\n")
                    if "envelope_filtered_output" in extra_fields:
                        summary += (
                            f"Envelope-filtered cloud (outside-envelope points removed): "
                            f"{extra_fields['envelope_filtered_output']}\n")
                elif envelope_filter:
                    summary += (
                        "Envelope-based filter: skipped for this run - not enough "
                        "detected floor/ceiling/wall surface to derive a room footprint. "
                        "Check the tool output above for the exact reason.\n")

            summary += (
                "\n=== NEXT STEPS ===\n"
                f"Open {Path(classified_path).name if classified_path else 'the classified cloud'} "
                "in CloudCompare and color by the 'classification' field to check the "
                "surfaces look right - a wall misclassified as ceiling (or vice versa) is "
                "the first thing to check against the surface list above. If a real wall "
                "seems to be missing, check the unclassified cloud (same output folder, "
                "'<name>_unclassified.ply' - only written when 'Also write each surface as "
                "its own separate .ply file' is checked) for a flat-ish cluster of points "
                "rather than a scattered one - that's usually it, broken up by clutter or "
                "debris stuck to it."
            )
            return summary

        finish_info = {"pipeline": active_pipeline, "stage_name": "segment",
                        "output": output_dir, "resolve_state": resolved_state}
        return cmd, build_report, finish_info


class Stage4SegmentDialog(QStageDialog, Stage4SegmentFieldsMixin):
    """Standalone popup - run this file directly to open just this
    dialog."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__("Stage 4: Segment", parent, on_output=on_output, on_status=on_status)
        self._build_segment_fields(pipeline=pipeline)


class Stage4SegmentPanel(QStagePanel, Stage4SegmentFieldsMixin):
    """Embeddable panel - used by pipeline_applet_qt_template.py as a
    page of stageStack."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__(parent, on_output=on_output, on_status=on_status)
        self._build_segment_fields(pipeline=pipeline)


def main():
    app = QApplication(sys.argv)
    dlg = Stage4SegmentDialog(pipeline=None)
    dlg.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
