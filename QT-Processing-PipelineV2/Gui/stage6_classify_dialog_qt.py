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

Run standalone:
    pip install PySide6
    python stage6_classify_dialog_qt.py
"""
import sys

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
            "automatically rather than guessing.")

        load_rms_btn = QPushButton("Load RMS & Suggest Threshold")
        load_rms_btn.clicked.connect(self._load_rms_and_suggest)
        self.form.addWidget(load_rms_btn)

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

        self.add_checkbox("keep_all", "Keep all points (add a flag field instead of filtering)")
        self.add_hint(
            "Off by default: only flagged points are kept, giving a clean change-highlight "
            "cloud for Stage 7. Turn this on if you want to inspect the threshold's effect "
            "in CloudCompare before committing.")

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
        QMessageBox.information(
            self, "Threshold suggested",
            f"Registration RMS: {fmt_cm(round(rms, 6))}\n"
            f"Multiplier: {multiplier}x\n"
            f"Suggested threshold: {fmt_cm(round(suggested_threshold, 6))} "
            f"(filled in above)\n\n"
            "This is a starting point, not a guarantee - check the flagged percentage "
            "after running (0% or ~100% means it needs adjusting either direction).")

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

        active_pipeline = self.get_active_pipeline_for_run()
        cmd = core.build_classify_command(
            script, input_ply, output, threshold, keep_all,
            cluster=cluster, cluster_method=cluster_method, cluster_eps=cluster_eps,
            cluster_min_samples=cluster_min_samples, min_cluster_size=min_cluster_size,
            use_uncertainty=use_uncertainty, pipeline=active_pipeline)

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
                f"Saved to: {output}\n"
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
                        f"Damage sites found: {len(clusters)}\n"
                    )
                    for c in clusters:
                        centroid = ", ".join(f"{v:.3f}" for v in c.get("centroid", []))
                        summary += (
                            f"  site {c.get('cluster_id')}: {c.get('point_count')} points, "
                            f"centroid=({centroid}) m, "
                            f"max|d|={fmt_cm(round(c.get('max_magnitude', 0), 4))}\n")
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
                "not 0% (threshold too high) or nearly 100% (threshold too low).\n\n"
                "=== NEXT STEPS ===\n"
                + ("Open in CloudCompare to see which points got flagged before "
                   "deciding on a final threshold.\n"
                   if keep_all else
                   "Use this file as the input to Stage 7 (Surface), or directly as "
                   "the --change input to Stage 8 (Export) if reconstruction isn't "
                   "needed.\n")
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
