#!/usr/bin/env python3
"""
Stage 2: Level - fields + real Run
=====================================
Defines Stage 2's fields, built field-for-field the same way
open_level_dialog() builds them in pipeline_applet.py - now including
the dynamic Distance threshold default/hint that reads this pipeline's
own recorded Stage 1 voxel_size back out of project.json (previously a
TODO here, deferred until this real-wiring pass).

_build_run() matches open_level_dialog()'s build() exactly: same
validation, same pipeline_core.build_level_command() call, same report
text. Returning (cmd, report, finish_info) from _build_run() is what
makes qt_stage_base.py's _on_run_clicked() take the real-execution
path instead of falling back to the layout-only stub.

Run standalone:
    pip install PySide6
    python stage2_level_dialog_qt.py
"""
import sys

from PySide6.QtWidgets import QApplication

from qt_stage_base import QStageDialog, QStagePanel, SCRIPTS_DIR, fmt_cm

try:
    import project_manager as pm
except ImportError:
    pm = None

try:
    import pipeline_core as core
except ImportError:
    core = None


class Stage2LevelFieldsMixin:

    def _build_level_fields(self, pipeline=None):
        self.add_pipeline_label(pipeline)

        default_script = str(SCRIPTS_DIR / "level_cloud.py")
        self.add_file_field("script", "Level script (.py):", [("Python files", "*.py")],
                             default=default_script)
        self.add_file_field("input", "Raw SLAM output .ply:", [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "input", lambda: pm.list_eligible_inputs(self.pipeline, "level"))

        output_default = self.resolve_project_output_default(pipeline, "level", ".ply")
        self.add_save_field("output", "Output .ply:", default_ext=".ply", default=output_default)
        self.register_auto_default(
            "output", lambda p: self.resolve_project_output_default(p, "level", ".ply"))

        self.add_hint("ouster-cli's SLAM has no gravity/leveling step, so the whole map "
                       "inherits whatever tilt the sensor had at the very first frame. "
                       "This finds the floor via RANSAC and rotates the cloud so it's "
                       "level, sitting at Z=0. Worth doing before Stage 3 (Cleanup)'s "
                       "ICP alignment, which works better on an already-level cloud.")

        # Stage 1's actual voxel size, read back from what start_stage() recorded
        # for THIS pipeline's "slam" stage - works for either backend, since
        # build_kiss_icp_slam_command() records the same "voxel_size" key Ouster
        # CLI always has (its override if one was given, else whatever was read
        # from its config, else None if genuinely unknown - e.g. KISS-ICP with no
        # config at all). A ONE-TIME read at panel-construction time, same as
        # every other project-mode default in this file - not refreshed by
        # refresh_project_pipeline() on a later switch or activation.
        slam_params = {}
        if pipeline is not None:
            slam_params = ((pipeline.entry.get("stages", {}) or {}).get("slam", {}) or {}).get(
                "params", {}) or {}
        stage1_voxel_size = slam_params.get("voxel_size")
        stage1_backend_label = (
            "KISS-ICP" if slam_params.get("backend") == "kiss_icp" else "Ouster CLI")

        self.add_preset_selector("Distance threshold preset:", [
            ("Tight (2 cm) - script default, needs fine/dense data",
             {"distance_threshold": "0.02"}),
            ("Medium (10 cm) - reasonable if Stage 1 voxel size was ~10-15 cm",
             {"distance_threshold": "0.1"}),
            ("Loose (25 cm) - try if 'no planes found', or Stage 1 voxel size was ~25 cm+",
             {"distance_threshold": "0.25"}),
        ])
        distance_threshold_default = (
            f"{stage1_voxel_size}" if stage1_voxel_size is not None else "0.02")
        self.add_length_field("distance_threshold", "Distance threshold (cm):",
                              default_m=distance_threshold_default, min_cm=0.1, max_cm=200)
        if stage1_voxel_size is not None:
            voxel_source_note = (
                f" This pipeline's Stage 1 ({stage1_backend_label}) used voxel_size = "
                f"{fmt_cm(stage1_voxel_size)}, recorded in project.json - the field above "
                f"starts there, matching it.")
        elif pipeline is not None and slam_params.get("backend") == "kiss_icp":
            voxel_source_note = (
                " This pipeline's Stage 1 used KISS-ICP with no voxel_size found (no "
                "config given, or none read from it) - kiss-icp's own auto-derived "
                "default applied, so there's nothing specific to match here; the field "
                "above kept the script's own 2 cm default.")
        else:
            voxel_source_note = ""
        self.add_hint("RANSAC plane-fit tolerance. If you get 'no planes found at all', "
                       "this is almost always too tight for your data - raise it, "
                       "roughly matching or exceeding the voxel size used in Stage 1 "
                       "SLAM, since coarser voxels quantize points further from a "
                       "perfect plane than a tight tolerance allows." + voxel_source_note)

        self.add_text_field("max_planes", "Max planes to search:", default="6")
        self.add_text_field("min_inlier_fraction", "Min plane size (fraction of all points):",
                             default="0.02")
        self.add_hint("Lower the min plane size if a real floor/wall is smaller relative "
                       "to the whole cloud than usual (e.g. a small compartment scanned "
                       "alongside a lot of surrounding clutter).")

        self.add_text_field("horizontal_threshold", "Horizontal threshold:", default="0.7")
        self.add_hint("How close to vertical (|normal.z|) a candidate plane must be to "
                       "even be eligible as the floor. The lowest candidate that clears "
                       "this bar is picked as the floor, not the biggest - a real ceiling "
                       "can have MORE points than a real floor (cleaner surface, less "
                       "clutter), so a biggest-plane-wins approach could pick the ceiling "
                       "by mistake, confirmed on real data. Only loosen this (lower it) "
                       "if a real floor/ceiling isn't clearing the bar at all - check the "
                       "run report's candidate list for its horizontality value first.")

        self.form.addStretch(1)

    def _build_run(self):
        """Real Run - matches open_level_dialog()'s build() in
        pipeline_applet.py exactly."""
        script = self.require("script", "Level script")
        input_ply = self.require("input", "Raw SLAM output .ply")
        output = self.require("output", "Output .ply")
        try:
            distance_threshold = float(self.fields["distance_threshold"].get())
            max_planes = int(self.fields["max_planes"].get())
            min_inlier_fraction = float(self.fields["min_inlier_fraction"].get())
            horizontal_threshold = float(self.fields["horizontal_threshold"].get())
        except ValueError:
            raise ValueError("Distance threshold (cm)/min plane size/horizontal threshold "
                              "must be numbers, max planes must be a whole number.")
        active_pipeline = self.get_active_pipeline_for_run()
        finish_info = {"pipeline": active_pipeline, "stage_name": "level", "output": output}
        cmd = core.build_level_command(script, input_ply, output,
                                        distance_threshold=distance_threshold,
                                        max_planes=max_planes,
                                        min_inlier_fraction=min_inlier_fraction,
                                        horizontal_threshold=horizontal_threshold,
                                        pipeline=active_pipeline)

        report = (
            "=== SUMMARY ===\n"
            f"Input: {input_ply}\n"
            f"Saved to: {output}\n"
            f"Distance threshold: {fmt_cm(distance_threshold)}\n"
            f"Max planes searched: {max_planes}\n"
            f"Min plane size: {min_inlier_fraction * 100:.1f}% of all points\n"
            f"Horizontal threshold: {horizontal_threshold}\n\n"
            "=== NOTE ===\n"
            "The tool output above lists each candidate plane it found, its Z "
            "position, and which one it picked as the floor - the LOWEST candidate "
            "that clears the horizontal threshold, not the biggest one (a real "
            "ceiling can have more points than a real floor). If the chosen floor "
            "has very few points, or the console shows a low-horizontality warning "
            "or a fallback warning, check the leveled result visually in "
            "CloudCompare before trusting it. If it found no planes at all, try a "
            "larger distance threshold above and re-run.\n\n"
            "=== NEXT STEPS ===\n"
            "Open the leveled cloud in CloudCompare to confirm the floor actually "
            "looks horizontal now. Then use it as the input to Stage 3 (Cleanup)."
        )
        return cmd, report, finish_info


class Stage2LevelDialog(QStageDialog, Stage2LevelFieldsMixin):
    """Standalone popup - run this file directly to open just this
    dialog."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__("Stage 2: Level", parent, on_output=on_output, on_status=on_status)
        self._build_level_fields(pipeline=pipeline)


class Stage2LevelPanel(QStagePanel, Stage2LevelFieldsMixin):
    """Embeddable panel - used by pipeline_applet_qt_template.py as a
    page of stageStack."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__(parent, on_output=on_output, on_status=on_status)
        self._build_level_fields(pipeline=pipeline)


def main():
    app = QApplication(sys.argv)
    dlg = Stage2LevelDialog(pipeline=None)
    dlg.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
