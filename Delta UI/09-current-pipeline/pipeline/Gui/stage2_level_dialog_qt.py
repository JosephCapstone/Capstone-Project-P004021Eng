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

Update 23 - Skip Level: in project mode (baseline or scan pipeline), the
button "Skip Level" records Stage 2 as complete with the Stage 1 file as its
output (project_manager.skip_level). Use it for a cloud that is already
levelled, or for a pair that is already registered (Level turns each cloud on
its own floor). Without a project, the button is hidden.

Update 26 - Remove points below the floor: the check box (off by default)
and two fields. Level then removes the small groups of stray points far below
the local floor (level_cloud.py --remove-below-floor). The tool output always
counts these points.

Run standalone:
    pip install PySide6
    python stage2_level_dialog_qt.py
"""
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from qt_stage_base import QStageDialog, QStagePanel, SCRIPTS_DIR, fmt_cm, project_input_default

try:
    import project_manager as pm
except ImportError:
    pm = None

try:
    import pipeline_core as core
except ImportError:
    core = None


def _stage1_voxel_size(pipeline):
    """The voxel size (metres) that Stage 1 of this pipeline recorded, or None."""
    if pipeline is None:
        return None
    try:
        params = ((pipeline.entry.get("stages", {}) or {}).get("slam", {}) or {}).get(
            "params", {}) or {}
        value = float(params.get("voxel_size"))
    except (TypeError, ValueError, AttributeError):
        return None
    return value if value > 0 else None


def _stage1_voxel_preset(pipeline):
    """Update 28. The preset 'Stage 1 voxel size': its text names the value of the
    selected pipeline. Without a recorded value it has no values (it matches
    nothing and writes nothing)."""
    value = _stage1_voxel_size(pipeline)
    if value is None:
        return "Stage 1 voxel size (not recorded)", {}
    return (f"Stage 1 voxel size ({fmt_cm(value)}) - the value that the SLAM map used",
            {"distance_threshold": f"{value}"})


def _default_distance_threshold(pipeline):
    """Box default: the Stage 1 voxel size of the pipeline, else 2 cm."""
    value = _stage1_voxel_size(pipeline)
    return f"{value}" if value is not None else "0.02"


class Stage2LevelFieldsMixin:

    def _build_level_fields(self, pipeline=None):
        self.add_pipeline_label(pipeline)

        self.add_file_field("input", "Raw SLAM output .ply:", [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "input", lambda: pm.list_eligible_inputs(self.pipeline, "level"))
        self.register_auto_default(
            "input", lambda p: project_input_default(p, "level"), fill_now=True)

        output_default = self.resolve_project_output_default(pipeline, "level", ".ply")
        self.add_save_field("output", "Output .ply:", default_ext=".ply", default=output_default)
        self.register_auto_default(
            "output", lambda p: self.resolve_project_output_default(p, "level", ".ply"))

        self.add_hint("The Stage 1 map keeps the tilt that the sensor had at the first "
                      "frame. Level finds the floor and turns the cloud so that the floor "
                      "is flat at Z = 0. Run it before Stage 3 (Cleanup).")

        self.add_conditional_project_button(
            "Skip Level (use the Stage 1 output)",
            lambda p: p is not None and getattr(p, "kind", None) in ("baseline", "scan"),
            self._skip_level_clicked)
        self.add_hint("Skip Level records this stage as done. The Stage 1 file is then the "
                      "input of Stage 3. Use it for a cloud that is already level, or for a "
                      "baseline and a scan that are already registered to each other: Level "
                      "turns each cloud on its own floor, and two different turns break the "
                      "registration. To undo, run Stage 2.")

        # Stage 1's actual voxel size, read back from what start_stage() recorded
        # for THIS pipeline's "slam" stage (both backends record the same
        # "voxel_size" key). The box and the first preset follow the selected
        # pipeline (update 28).
        slam_params = {}
        if pipeline is not None:
            slam_params = ((pipeline.entry.get("stages", {}) or {}).get("slam", {}) or {}).get(
                "params", {}) or {}
        stage1_voxel_size = slam_params.get("voxel_size")

        self.add_preset_selector("Distance threshold preset:", [
            ("Stage 1 voxel size (not recorded)", {}, _stage1_voxel_preset),
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
        # Update 28: the box follows the Stage 1 voxel size of the selected pipeline.
        self.register_auto_default("distance_threshold", _default_distance_threshold)

        # Update 29: the explanations are in "?" popups (what it does, what the
        # options do, when to change it).
        self.add_help(
            "input",
            "The point cloud to level. Usually the map from Stage 1.",
            when="Change it to level a different cloud. In project mode the box fills from "
                 "the selected pipeline.")
        self.add_help(
            "output",
            "The file that Level writes.",
            when="Change it to save under another name. In project mode the name comes from "
                 "the pipeline.")
        self.add_help(
            "distance_threshold",
            "How far a point can be from a plane and still count as part of that plane.",
            effects=[
                ("Smaller (1-2 cm)", "strict. Only very flat surfaces are found. Level can "
                                     "find no plane on a coarse map."),
                ("Larger (10-25 cm)", "loose. Planes are found on a coarse map. A very "
                                      "large value lets points of nearby surfaces join the "
                                      "plane."),
            ],
            when="Level finds no planes. Raise the value to at least the Stage 1 voxel size. "
                 "The first preset is that value.")

        # Settings that normally stay at their default values (closed block).
        self.begin_advanced("Advanced")
        default_script = str(SCRIPTS_DIR / "level_cloud.py")
        self.add_file_field("script", "Level script (.py):", [("Python files", "*.py")],
                             default=default_script)
        self.add_text_field("max_planes", "Max planes to search:", default="6")
        self.add_text_field("min_inlier_fraction", "Min plane size (fraction of all points):",
                             default="0.02")
        self.add_text_field("horizontal_threshold", "Horizontal threshold:", default="0.7")
        self.add_checkbox("remove_below_floor",
                          "Remove stray points far below the floor", default=False)
        self.add_length_field("below_floor_margin", "Below the floor by more than (cm):",
                              default_m="0.30", min_cm=2, max_cm=500)
        self.add_text_field("below_floor_keep_area", "Keep a group of at least this area (cm2):",
                            default="2500")

        self.add_help(
            "script",
            "The Python script that does the leveling.",
            when="Change it only to use a changed copy of the script.")
        self.add_help(
            "max_planes",
            "How many candidate planes Level searches before it gives up on a floor.",
            effects=[
                ("Smaller", "faster. The floor can be missed when larger surfaces use up "
                            "the count."),
                ("Larger", "slower. Smaller surfaces are also tried."),
            ],
            when="Level reports no floor although the room has one.")
        self.add_help(
            "min_inlier_fraction",
            "The smallest plane that Level accepts, as a share of all points "
            "(0.02 = 2 %).",
            effects=[
                ("Smaller", "small planes are accepted. A small surface can be taken as "
                            "the floor."),
                ("Larger", "only big planes are accepted. A small floor can be missed."),
            ],
            when="The floor is small compared with the whole cloud, for example a small "
                 "compartment with much clutter around it. Lower the value.")
        self.add_help(
            "horizontal_threshold",
            "How flat a plane must be to be a floor candidate. The value is the vertical "
            "part of the plane normal (|normal.z|). Level picks the lowest candidate, "
            "not the largest, because a ceiling can have more points than the floor.",
            effects=[
                ("Smaller (0.5)", "tilted planes also qualify."),
                ("Larger (0.9-1.0)", "only almost flat planes qualify."),
            ],
            when="Level finds no floor. Look at the horizontality value of the candidates "
                 "in the run report first. Lower the value only if the floor does not "
                 "reach the limit.")
        self.add_help(
            "remove_below_floor",
            "Removes stray points far below the floor (for example ghost returns under the "
            "deck). Such points go into Stage 5 and can show as damage. Level finds the "
            "local floor in each 50 cm cell, so a floor that is a little tilted or bent "
            "by SLAM drift is followed.",
            effects=[
                ("On", "stray points below the floor are removed. The report counts them."),
                ("Off", "no point is removed. The report still counts the points below "
                        "the floor."),
            ],
            when="The report shows points below the floor.",
            note="CAUTION: a flat layer of stray points with more than 2 % of all points "
                 "(for example a mirror image of the floor) is taken as the floor. Level "
                 "then levels on it. Check the floor and its Z in the console output.")
        self.add_help(
            "below_floor_margin",
            "How far below the local floor a point must be to count as a stray point. "
            "Used when the box above is on.",
            effects=[
                ("Smaller (10 cm)", "points closer to the floor are removed. Points of "
                                    "real damage can be removed too."),
                ("Larger (50 cm)", "only points very far below the floor are removed."),
            ],
            when="Keep the default (30 cm) unless stray points stay near the floor. Do not "
                 "make the value small.")
        self.add_help(
            "below_floor_keep_area",
            "The footprint area (cm2) at which a group of points below the floor is kept. "
            "Points below the floor that touch each other are one group. A group with a "
            "smaller footprint is removed. A larger group is kept because it can be real, "
            "for example a hole in the deck through which the space below is visible. "
            "The default 2500 cm2 is 50 cm x 50 cm.",
            effects=[
                ("Smaller", "more groups are kept. Fewer points are removed."),
                ("Larger", "more groups are removed, also bigger ones."),
            ],
            when="A large group of stray points stays after the run (raise the value), or "
                 "the report shows a real hole that was removed (lower it).")

        self.end_advanced()
        self.keep_field_order("script", "input", "output")

        self.form.addStretch(1)

    def _skip_level_clicked(self, pipeline):
        """Records Stage 2 as skipped (project_manager.skip_level) after the
        user confirms. The Stage 1 file becomes the Level output."""
        if pm is None or pipeline is None:
            return
        answer = QMessageBox.question(
            self, "Skip Level",
            "Skip Stage 2 (Level) for this pipeline?\n\n"
            "Stage 2 is then recorded as complete. The Stage 1 file is used as the "
            "input of Stage 3 (Cleanup). The file is not changed or copied.\n\n"
            "To undo this, run Stage 2.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        try:
            output = pm.skip_level(pipeline)
        except pm.ProjectError as e:
            QMessageBox.critical(self, "Level cannot be skipped", str(e))
            return
        self._report_status("done", "Level skipped")
        self._refresh_auto_defaults()
        QMessageBox.information(
            self, "Level skipped",
            f"Stage 2 is recorded as complete (skipped).\n\nThe Stage 3 input is:\n{output}")

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
        remove_below_floor = bool(self.fields["remove_below_floor"].get())
        below_floor_margin = None
        below_floor_keep_area = None
        if remove_below_floor:
            try:
                below_floor_margin = float(self.fields["below_floor_margin"].get())
                below_floor_keep_area = float(self.fields["below_floor_keep_area"].get()) / 1.0e4  # cm2 to m2
            except ValueError:
                raise ValueError("\"Below the floor by more than (cm)\" and \"Keep a group of "
                                 "at least this area (cm2)\" must be numbers.")
            if below_floor_margin <= 0:
                raise ValueError("\"Below the floor by more than (cm)\" must be larger than 0.")
            if below_floor_keep_area < 0:
                raise ValueError("\"Keep a group of at least this area (cm2)\" must be 0 or more.")
        active_pipeline = self.get_active_pipeline_for_run()
        finish_info = {"pipeline": active_pipeline, "stage_name": "level", "output": output}
        cmd = core.build_level_command(script, input_ply, output,
                                        distance_threshold=distance_threshold,
                                        max_planes=max_planes,
                                        min_inlier_fraction=min_inlier_fraction,
                                        horizontal_threshold=horizontal_threshold,
                                        pipeline=active_pipeline,
                                        remove_below_floor=remove_below_floor,
                                        below_floor_margin=below_floor_margin,
                                        below_floor_keep_area=below_floor_keep_area)

        if remove_below_floor:
            below_floor_summary = (
                f"Remove points below the floor: yes. Below the floor by more than "
                f"{fmt_cm(below_floor_margin)}. A group with a footprint of less than "
                f"{below_floor_keep_area * 1e4:g} cm2 is removed. A larger group is kept.\n")
            below_floor_note = (
                "The tool output lists the points below the floor, the groups that were "
                "removed (number of points), and each group that was kept (size, position, "
                "depth). Look at each kept group. A hole in the deck is real. A mirror "
                "image of the room below the floor is not real, and the function does not "
                "remove it.\n\n")
        else:
            below_floor_summary = ("Remove points below the floor: no. The tool output still "
                                   "counts the points below the floor.\n")
            below_floor_note = ""

        report = (
            "=== SUMMARY ===\n"
            f"Input: {input_ply}\n"
            f"Saved to: {output}\n"
            f"Distance threshold: {fmt_cm(distance_threshold)}\n"
            f"Max planes searched: {max_planes}\n"
            f"Min plane size: {min_inlier_fraction * 100:.1f}% of all points\n"
            f"Horizontal threshold: {horizontal_threshold}\n"
            f"{below_floor_summary}\n"
            "=== NOTE ===\n"
            f"{below_floor_note}"
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
