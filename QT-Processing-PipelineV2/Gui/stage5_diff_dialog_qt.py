#!/usr/bin/env python3
"""
Stage 5: Diff (M3C2) - fields only
=====================================
Defines Stage 5's fields, built field-for-field the same way
open_diff_dialog() builds them in pipeline_applet.py. First stage to
use begin_columns() (qt_stage_base.py) instead of the flat vertical
form every other stage panel uses - Reference (Baseline) and
Comparison sit side by side, per chat.

Diff's two inputs both come from ONE diff PipelineHandle's own
recorded entry["reference"]/entry["comparison"] (not two independent
pipeline selections from the main window), so this needs NO base-class
changes beyond begin_columns() itself - add_project_picker_button() is
called twice, same as any other field, just with
pm.list_side_candidates() instead of pm.list_eligible_inputs() as its
groups_fn (Diff's two sides are picked from a NAMED pipeline's past
cleanup/segment outputs, not "whatever came before this stage" - see
that function's own docstring).

Three helper buttons beyond Run, treated differently on purpose (see
chat): "Load from Stage 3" and "Generate Params File..." call directly
into pipeline_core.py with no subprocess involved, so both are wired
for real, same as Stage 3's "Use Project Baseline". "Check Point
Spacing..." spawns point_spacing.py too, but SYNCHRONOUSLY
(subprocess.run with a timeout, blocking the UI with a wait cursor) -
matches pipeline_applet.py's own check_point_spacing() exactly, which
treats this as a quick diagnostic tool rather than a long-running
stage, unlike Run itself (which streams asynchronously via
_run_streaming_command()). Deliberately NOT ported using the async
streaming mechanism - this is a real, considered difference from the
main Run flow, not an inconsistency.

"Load from Stage 3" uses pm.find_icp_rms_for_path() (NOT
pipeline_core.load_rms_sidecar(), despite the button's own label) -
this was a real bug, caught in chat: load_rms_sidecar() only checks
for a sidecar file next to the EXACT path given, so it silently looked
in whatever folder "Comparison" happened to already point at. If
Comparison is a Stage 4 (Segment) output rather than the Stage 3
(Cleanup) file that was actually ICP-aligned, that folder never has an
RMS sidecar - find_icp_rms_for_path() traces back correctly regardless
of which downstream file Comparison ends up being, matching
_on_comparison_picked()'s already-correct behavior below.

Run standalone:
    pip install PySide6
    python stage5_diff_dialog_qt.py
"""
import sys
import re
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton,
    QButtonGroup, QMessageBox, QFileDialog,
)

from qt_stage_base import (QStageDialog, QStagePanel, LengthFieldRef, SCRIPTS_DIR,
                           fmt_cm)

try:
    import project_manager as pm
except ImportError:
    pm = None

try:
    import pipeline_core as core
except ImportError:
    core = None


class Stage5DiffFieldsMixin:

    def _build_diff_fields(self, pipeline=None):
        self.add_pipeline_label(pipeline)

        (baseline_group, baseline_layout), (comparison_group, comparison_layout) = \
            self.begin_columns(["Reference (Baseline)", "Comparison"])

        self._layout_stack.append(self.form)
        self.form = baseline_layout
        self.add_file_field("baseline", "Baseline .ply:", [("PLY files", "*.ply")],
                             stacked=True)
        self.add_project_picker_button(
            "baseline",
            lambda: pm.list_side_candidates(
                self.pipeline.project, self.pipeline.entry["reference"]))
        self.add_registered_baseline_preset("baseline", stacked=True)
        self.form = self._layout_stack.pop()

        self._layout_stack.append(self.form)
        self.form = comparison_layout
        self.add_file_field("comparison", "Comparison .ply:", [("PLY files", "*.ply")],
                             stacked=True)
        self.add_project_picker_button(
            "comparison",
            lambda: pm.list_side_candidates(
                self.pipeline.project, self.pipeline.entry["comparison"]),
            extra_on_pick=self._on_comparison_picked)
        self.form = self._layout_stack.pop()

        rms_row = self._row("Registration RMS (cm, from Stage 3):")
        rms_edit = QLineEdit()
        rms_row.addWidget(rms_edit, 1)
        load_rms_btn = QPushButton("Load from Stage 3")
        load_rms_btn.clicked.connect(self._load_rms_from_sidecar)
        rms_row.addWidget(load_rms_btn, 0)
        self.form.addLayout(rms_row)
        # Shown in cm; get()/set() stay in metres (see LengthFieldRef).
        self.fields["registration_rms"] = LengthFieldRef(rms_edit, min_cm=0.01, max_cm=50)
        self.add_hint(
            "This feeds M3C2's Level of Detection (LOD) calculation, which is what separates "
            "real change from noise/misalignment. Leave blank if unknown rather than entering "
            "0 - a 0 collapses the LOD toward zero and flags almost everything as "
            "significant, which is worse than not running the significance test at all. "
            "'Load from Stage 3' only ever finds an RMS for a Cleanup-stage file - if "
            "Comparison above is set to a Stage 4 (Segment) output instead, this will "
            "correctly report nothing found (confirmed in testing); pick the Stage 3 "
            "Cleanup file there instead if you want the RMS auto-filled.")

        spacing_row = QHBoxLayout()
        check_spacing_btn = QPushButton("Check Point Spacing (Baseline)...")
        check_spacing_btn.clicked.connect(self._check_point_spacing)
        spacing_row.addWidget(check_spacing_btn)
        spacing_row.addWidget(QLabel("  Use:"))
        # Kept alive on self - QButtonGroup isn't auto-parented into the
        # widget tree by adding its buttons to a layout, unlike QWidgets.
        self._range_choice_group = QButtonGroup(self)
        # Maps each radio button back to its own "min"/"mid"/"max" value,
        # since QButtonGroup has no get()-style accessor the way
        # self.fields[...] wrappers do - _check_point_spacing() below
        # reads this to know which one is currently checked.
        self._range_choice_values = {}
        for value, text in [("min", "Min"), ("mid", "Mid"), ("max", "Max")]:
            radio = QRadioButton(text)
            radio.setChecked(value == "min")
            self._range_choice_group.addButton(radio)
            self._range_choice_values[radio] = value
            spacing_row.addWidget(radio)
        spacing_row.addStretch(1)
        self.form.addLayout(spacing_row)
        self.add_hint(
            "Min = tightest normal scale in the suggested range (best for resolving small "
            "damage/sharp edges, but noisier if too close to raw point spacing). Max = "
            "smoothest (safest against noise, but blunts small features and corners - this "
            "is what caused the earlier rounded-edges result). Mid is a balanced starting "
            "point. Search scale/depth are filled automatically as 0.5x/2x of normal scale, "
            "matching a confirmed-working reference file.")

        self.add_length_field("normal_scale", "Normal scale (diameter, cm):",
                              min_cm=0.1, max_cm=200)
        self.add_length_field("search_scale", "Search/projection scale (diameter, cm):",
                              min_cm=0.05, max_cm=100)
        self.add_length_field("search_depth", "Search depth (max depth, cm):",
                              min_cm=0.1, max_cm=500)
        self.add_hint(
            "Filled automatically by 'Check Point Spacing' above, or set manually / via "
            "CloudCompare's own M3C2 dialog and 'Guess params' if you'd rather. If you edit "
            "Normal scale by hand afterward, update these two to match - CloudCompare's own "
            "'Guess params' does NOT do this automatically, which caused a rounded/blunted-"
            "edges result in testing before this was caught.")

        generate_btn = QPushButton("Generate Params File...")
        generate_btn.clicked.connect(self._generate_params_file)
        self.form.addWidget(generate_btn)

        self.add_file_field("params", "M3C2 params file (.txt):", [("Text files", "*.txt")])
        self.add_hint(
            "Either browse to a params file made via CloudCompare's GUI, or use 'Generate "
            "Params File...' above to write one directly from the values on this page.")

        self.form.addStretch(1)

    def _build_run(self):
        """Real Run - matches open_diff_dialog()'s build() in
        pipeline_applet.py, with one deliberate correction: that build()
        still branches on dlg.diff_manual_override (the old auto-resolve
        mechanism, reading baseline/comparison via pm.get_diff_inputs()
        instead of the fields when a project is active AND the checkbox
        is unchecked). That whole mechanism was removed by the
        ProjectFilePicker redesign - PROJECT_INPUT_PICKER_PLAN.md's own
        principle is that a project-mode field is exactly as
        authoritative as manual mode's always was. So here, baseline and
        comparison are read directly from their fields UNCONDITIONALLY,
        matching every other stage's real Run wiring so far.
        active_pipeline is still passed through to build_diff_command()
        and used for finish_info, purely for tracking
        (start_stage()/finish_stage()), same as every other stage."""
        baseline = self.require_existing_file("baseline", "Baseline .ply")
        comparison = self.require("comparison", "Comparison .ply")
        params = self.require("params", "M3C2 params file")
        registration_rms = self.fields["registration_rms"].get().strip() or None

        active_pipeline = self.get_active_pipeline_for_run()

        log_path = Path(comparison).with_name(Path(comparison).stem + "_m3c2_cc_log.txt")
        cmd = core.build_diff_command(baseline, comparison, params, log_file=log_path,
                                       pipeline=active_pipeline)

        # Watch the BASELINE's own folder, not the comparison's -
        # build_diff_command() loads baseline first, and CloudCompare's
        # -M3C2/-SAVE_CLOUDS resaves the "compared"/core-points cloud
        # (the first one loaded) into its own input folder - see that
        # function's own docstring.
        output_dir = Path(baseline).resolve().parent
        existing_ply_before = set(output_dir.glob("*.ply")) if output_dir.is_dir() else set()

        output_desired = None
        if active_pipeline is not None:
            output_desired = pm.get_absolute_path(
                active_pipeline.project, pm.get_output_path(active_pipeline, "diff", ".ply"))

        resolved_state = {}

        def build_report():
            summary = (
                "=== SUMMARY ===\n"
                f"Baseline: {baseline}\n"
                f"Comparison: {comparison}\n"
                f"Params file used: {params}\n"
                + (f"Registration RMS on hand: {fmt_cm(registration_rms)}. If you used "
                   f"'Generate Params File...' above, this is already baked into the "
                   f"params file. If you browsed to a file made via CloudCompare's GUI "
                   f"instead, make sure its registration-error was set to this value "
                   f"there.\n"
                   if registration_rms else
                   "No registration RMS was entered - if the params file's own "
                   "registration-error value is 0 or unset, the significance test will "
                   "over-flag almost everything as changed.\n")
                + "\n=== NOTE ===\n"
                "CloudCompare saves ALL loaded clouds, not just the M3C2 result - "
                "you'll see multiple output files. The one with the M3C2 distance "
                "scalar field (usually the sparser, more colorful cloud) is the actual "
                "result; the others are just copies of the input clouds.\n"
            )

            if active_pipeline is not None and output_desired:
                resolved, others, error = core.resolve_cleanup_output(
                    baseline, output_dir, existing_ply_before, output_desired)
                if resolved:
                    resolved_state["output"] = resolved
                    try:
                        params_rel = pm.to_relative_path(active_pipeline.project, params)
                    except pm.ProjectError:
                        params_rel = str(params)
                    extra_fields = {"m3c2_params_file": params_rel}
                    if registration_rms:
                        try:
                            extra_fields["registration_error_used"] = float(registration_rms)
                        except ValueError:
                            pass
                    resolved_state["extra_fields"] = extra_fields
                    summary += f"\nSaved to project output: {resolved}\n"
                elif error:
                    summary += f"\n=== OUTPUT ===\n{error}\n"

            summary += (
                "\n=== NEXT STEPS ===\n"
                "Open the result in CloudCompare, set the active scalar field to "
                "'M3C2 distance', and check the color scale - red = positive change, "
                "blue = negative, magnitude = degree of change. Then use it as the "
                "input to Stage 6 (Classify)."
            )

            annotated = core.annotate_log_file(log_path, "Stage 5: Diff (M3C2)", cmd)
            if annotated:
                resolved_state["log_path"] = log_path
                summary += (
                    f"\n\n=== CLOUDCOMPARE LOG (with headers added) ===\n"
                    f"Saved to: {log_path}\n"
                    "Contains core point counts, timing, and any M3C2 warnings (e.g. "
                    "invalid normals) - open it directly for full detail."
                )
            else:
                summary += (
                    "\n\n=== CLOUDCOMPARE LOG ===\n"
                    "No log file was found at the expected path - check the Terminal "
                    "tab instead."
                )
            return summary

        finish_info = None
        if active_pipeline is not None:
            finish_info = {"pipeline": active_pipeline, "stage_name": "diff",
                            "output": output_desired, "resolve_state": resolved_state}
        return cmd, build_report, finish_info

    # -- real, subprocess-free helpers (see module docstring) ---------------

    def _on_comparison_picked(self, path):
        """Auto-fills Registration RMS from this pipeline's own
        recorded icp_rms for whatever cleanup output just got picked as
        Comparison - project_manager.find_icp_rms_for_path()."""
        if pm is None or self.pipeline is None:
            return
        rms = pm.find_icp_rms_for_path(self.pipeline.project, path)
        if rms is not None:
            self.fields["registration_rms"].set(f"{rms:.6f}")

    def _load_rms_from_sidecar(self):
        """Uses pm.find_icp_rms_for_path(), NOT
        pipeline_core.load_rms_sidecar() - see the module docstring for
        why: the latter only checks for a sidecar file next to the
        EXACT path given, so it breaks silently whenever Comparison
        points at a Stage 4 (Segment) output rather than the Stage 3
        (Cleanup) file the RMS was actually recorded against."""
        comparison_path = self.fields["comparison"].get().strip()
        if not comparison_path:
            QMessageBox.information(self, "No comparison file",
                                     "Fill in the Comparison .ply field first.")
            return
        if pm is None or self.pipeline is None:
            QMessageBox.information(
                self, "No active pipeline",
                "This lookup needs an active project pipeline to trace the "
                "comparison file back to its own Stage 3 (Cleanup) run - open or "
                "create a project first, or fill in Registration RMS by hand.")
            return
        rms = pm.find_icp_rms_for_path(self.pipeline.project, comparison_path)
        if rms is not None:
            self.fields["registration_rms"].set(f"{rms:.6f}")
        else:
            QMessageBox.information(
                self, "No RMS found",
                f"No saved RMS found for:\n{comparison_path}\n\n"
                "This is recorded only on a Stage 3 (Cleanup) output that ran with an "
                "'Align to baseline' target. If Comparison above is a Stage 4 (Segment) "
                "output instead, this is expected - pick the underlying Stage 3 Cleanup "
                "file there instead if you want the RMS auto-filled.")

    def _generate_params_file(self):
        try:
            normal_scale = float(self.fields["normal_scale"].get())
            search_scale = float(self.fields["search_scale"].get())
            search_depth = float(self.fields["search_depth"].get())
        except ValueError:
            QMessageBox.critical(
                self, "Missing values",
                "Fill in Normal scale, Search scale, and Search depth as numbers before "
                "generating.")
            return
        reg_rms = self.fields["registration_rms"].get().strip()
        if not reg_rms:
            QMessageBox.critical(
                self, "Missing registration error",
                "Fill in Registration RMS first (or click 'Load from Stage 3') - a "
                "blank/zero value defeats the significance test.")
            return
        try:
            reg_rms_val = float(reg_rms)
        except ValueError:
            QMessageBox.critical(self, "Invalid value", "Registration RMS must be a number.")
            return
        if core is None:
            return
        if not self._check_length_plausibility(
                keys=("normal_scale", "search_scale", "search_depth", "registration_rms")):
            return

        auto_named = False
        if self.pipeline is not None and pm is not None:
            save_path = pm.get_absolute_path(
                self.pipeline.project, pm.get_output_path(self.pipeline, "diff", ".txt"))
            auto_named = True
        else:
            save_path, _ = QFileDialog.getSaveFileName(
                self, "M3C2 params file", "m3c2_params.txt", "Text files (*.txt)")
            if not save_path:
                return

        core.generate_m3c2_params_file(
            save_path, normal_scale, search_scale, search_depth, reg_rms_val)
        self.fields["params"].set(save_path)
        self._show_success(
            "Params file generated",
            f"Saved to:\n{save_path}\n\n"
            + ("This is a project run, so the file was named and placed automatically "
               "inside this diff's own output folder - no save location to pick by "
               "hand.\n\n" if auto_named else "")
            + "Every setting besides these four values (normal/search scale, search depth, "
            "registration error) is copied from a confirmed-working reference file. If you "
            "need to change something else (subsample radius, normal mode, etc.), generate "
            "a file via CloudCompare's own GUI instead, or share that value so the "
            "generator can be extended.")

    def _check_point_spacing(self):
        """Real point-spacing check - matches pipeline_applet.py's
        check_point_spacing() exactly: a SYNCHRONOUS, blocking
        subprocess call (subprocess.run with a timeout), not the async
        streaming pattern _run_streaming_command()/Run itself use -
        matches how the real app treats this as a quick diagnostic
        tool rather than a long-running stage. Shows a wait cursor
        while blocked, same idea as the Tkinter version's
        cursor="watch"."""
        baseline_path = self.fields["baseline"].get().strip()
        if not baseline_path or not Path(baseline_path).exists():
            QMessageBox.critical(
                self, "No baseline file",
                "Fill in a valid Baseline .ply file first (point spacing is "
                "checked against whatever's in that field).")
            return
        script_path = SCRIPTS_DIR / "point_spacing.py"
        if not script_path.exists():
            QMessageBox.critical(
                self, "Missing script", f"point_spacing.py not found in:\n{SCRIPTS_DIR}")
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = subprocess.run(
                [sys.executable, str(script_path), "--input", baseline_path],
                capture_output=True, text=True, timeout=120)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Error running point_spacing.py", str(e))
            return
        QApplication.restoreOverrideCursor()

        output = result.stdout + (("\n" + result.stderr) if result.stderr else "")
        if result.returncode != 0:
            QMessageBox.critical(self, "point_spacing.py failed",
                                  output or "No output captured.")
            return

        match = re.search(
            r"Suggested M3C2 normal diameter range:\s*([\d.]+)\s*to\s*([\d.]+)", output)
        if not match:
            QMessageBox.information(
                self, "Point Spacing (Baseline)",
                output + "\n\n(Couldn't find a suggested range in the output to "
                         "auto-fill from - fill in the fields manually using the "
                         "stats above.)")
            return

        low, high = float(match.group(1)), float(match.group(2))
        choice = next((v for r, v in self._range_choice_values.items() if r.isChecked()),
                      "min")
        normal_scale = {"min": low, "mid": (low + high) / 2, "max": high}[choice]

        # Ratios confirmed exactly from a real working reference params file
        # (SearchScale = 0.5x, SearchDepth = 2x NormalScale) - see
        # generate_m3c2_params.py for the same reference.
        search_scale = normal_scale * 0.5
        search_depth = normal_scale * 2.0

        self.fields["normal_scale"].set(f"{normal_scale:.4f}")
        self.fields["search_scale"].set(f"{search_scale:.4f}")
        self.fields["search_depth"].set(f"{search_depth:.4f}")

        output += (
            f"\n\nThe values above from point_spacing.py are in metres.\n"
            f"Filled in ({choice} of range):\n"
            f"  Normal scale: {fmt_cm(round(normal_scale, 4))}\n"
            f"  Search scale: {fmt_cm(round(search_scale, 4))}  (0.5x normal)\n"
            f"  Search depth: {fmt_cm(round(search_depth, 4))}  (2x normal)\n"
            f"These values replace the values that were in those three fields."
        )
        QMessageBox.information(self, "Point Spacing (Baseline)", output)


class Stage5DiffDialog(QStageDialog, Stage5DiffFieldsMixin):
    """Standalone popup - run this file directly to open just this
    dialog."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__("Stage 5: Diff (M3C2)", parent, on_output=on_output, on_status=on_status)
        self._build_diff_fields(pipeline=pipeline)


class Stage5DiffPanel(QStagePanel, Stage5DiffFieldsMixin):
    """Embeddable panel - used by pipeline_applet_qt_template.py as a
    page of stageStack."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__(parent, on_output=on_output, on_status=on_status)
        self._build_diff_fields(pipeline=pipeline)


def main():
    app = QApplication(sys.argv)
    dlg = Stage5DiffDialog(pipeline=None)
    dlg.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
