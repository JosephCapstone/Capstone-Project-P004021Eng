#!/usr/bin/env python3
"""
Stage 8: Export to USD - fields only
=======================================
Defines Stage 8's fields, built field-for-field the same way
open_export_dialog() builds them in pipeline_applet.py. Last stage - a
diff-bound stage (pipeline.kind == "diff"), same group as Stages 5-7.

Two picker fields, each with a DIFFERENT groups_fn - the one real
departure from every other stage's "one field, one picker, same
project_manager function" pattern:

- "baseline": pm.list_side_candidates(self.pipeline.project, "baseline")
  - always the PROJECT's own baseline cleanup output, regardless of
    which diff is active, since the exported environment layer should
    stay the same no matter which diff produced the change highlight.
    Note the literal string "baseline" here, not
    self.pipeline.entry["reference"] the way Stage 5's own baseline
    field worked - Export's baseline is fixed to the project's baseline
    pipeline specifically, not to whichever pipeline this diff happens
    to be defined against.
- "change": pm.list_eligible_inputs(self.pipeline, "export")
  - the standard per-diff pattern every other field uses.

"detail" (damage detail .ply) gets NO picker button - not a tracked
project stage. It is picked by hand, or filled automatically by a
successful "Extract Damage Detail..." run.

"Extract Damage Detail..." is real: it runs extract_damage_detail.py
via pipeline_core.build_damage_detail_command() (flags confirmed from
the script's own argparse) through _run_utility_command(), so it has
the same running lock and Stop support as a stage run. Ported from
pipeline_applet.py's extract_damage_detail(), with one improvement: the
Tkinter version blocked the UI with subprocess.run(timeout=180). Its two
input fields get picker buttons, the same groups the Tkinter dialog
uses (flagged: the diff's Classify outputs via
list_eligible_inputs(pipeline, "surface"); comparison: the diff's
comparison side via list_side_candidates()).

_build_run() (the main Run button) IS real, with one correction versus
the source build(): that build() still branches on
dlg.export_manual_override (the old auto-resolve mechanism, reading
baseline/change via pm.get_baseline_cleanup_output()/
get_input_for_stage() instead of the fields when a project is active).
Same removed mechanism as Stage 5's own "diff_manual_override" bug,
same fix - baseline and change are read directly from their fields
UNCONDITIONALLY here, matching every other stage's real Run wiring.

Run standalone:
    pip install PySide6
    python stage8_export_dialog_qt.py
"""
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QMessageBox, QFileDialog

from qt_stage_base import QStageDialog, QStagePanel, SCRIPTS_DIR, fmt_cm

try:
    import project_manager as pm
except ImportError:
    pm = None

try:
    import pipeline_core as core
except ImportError:
    core = None


def _default_damage_detail_radius(pipeline):
    """Pre-fill for Extract Damage Detail's Radius: this diff's own
    Stage 5 (Diff) M3C2 normal scale.

    Reads stages.diff.params.normal_scale, which
    pipeline_core.build_diff_command() now records (read from the params
    file at start_stage() time). Falls back to parsing NormalScale= from
    the recorded m3c2_params_file, for diffs run before that change.
    Before this fix, nothing recorded normal_scale at all, so this
    pre-fill was always blank in both the Tkinter and Qt apps (the Qt
    version also read a wrong key path, stages.diff.extra_fields - and
    complete_stage() merges extra fields flat into the stage entry, not
    under an "extra_fields" key).

    Returns "" when nothing is known (manual mode, or Diff not run)."""
    if pipeline is None or pipeline.kind != "diff":
        return ""
    try:
        diff_stage = pipeline.entry.get("stages", {}).get("diff", {}) or {}
    except Exception:
        return ""
    params = diff_stage.get("params", {}) or {}
    value = params.get("normal_scale")
    if value is None and core is not None and pm is not None:
        params_file = diff_stage.get("m3c2_params_file") or params.get("m3c2_params_file")
        if params_file:
            path = Path(params_file)
            if not path.is_absolute():
                path = pm.get_absolute_path(pipeline.project, params_file)
            value = core.read_m3c2_normal_scale(path)
    return f"{value:g}" if isinstance(value, (int, float)) else ""


class Stage8ExportFieldsMixin:

    def _build_export_fields(self, pipeline=None):
        self.add_pipeline_label(pipeline)

        default_script = str(SCRIPTS_DIR / "usd_export.py")
        self.add_file_field("script", "USD export script (.py):", [("Python files", "*.py")],
                             default=default_script)

        self.add_file_field("baseline", "Baseline .ply:", [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "baseline", lambda: pm.list_side_candidates(self.pipeline.project, "baseline"))

        self.add_file_field("change", "Change-highlight .ply:", [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "change", lambda: pm.list_eligible_inputs(self.pipeline, "export"))

        section_label = QLabel("Extract Damage Detail (optional, before exporting):")
        section_label.setStyleSheet("font-weight: bold;")
        self.form.addWidget(section_label)
        self.add_file_field(
            "damage_detail_flagged", "Flagged (Classify output) .ply:", [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "damage_detail_flagged", lambda: pm.list_eligible_inputs(self.pipeline, "surface"))
        self.add_file_field(
            "damage_detail_comparison", "Comparison cloud (Stage 3 output) .ply:",
            [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "damage_detail_comparison",
            lambda: pm.list_side_candidates(self.pipeline.project,
                                            self.pipeline.entry["comparison"]))
        radius_default = _default_damage_detail_radius(pipeline)
        self.add_length_field("damage_detail_radius", "Radius (cm):", default_m=radius_default,
                              min_cm=0.1, max_cm=200)
        self.register_auto_default("damage_detail_radius", _default_damage_detail_radius)
        self.add_hint(
            "Radius: how far around each flagged point to pull real geometry from the "
            "comparison cloud, in the same units as the point cloud - pre-filled from this "
            "diff's own recorded Normal scale (Stage 5's own M3C2 parameter) when known, "
            "since that's already a reasonable per-point neighborhood size for this data. "
            "Flagged .ply: pick this from the Classify group when using 'Choose from "
            "project...' elsewhere in this app, not Diff - Classify's own output is what "
            "carries which points were actually flagged as damage; Diff's raw M3C2 result "
            "has no flagged/not-flagged distinction yet.")
        extract_btn = QPushButton("Extract Damage Detail...")
        extract_btn.clicked.connect(self._extract_damage_detail)
        self.form.addWidget(extract_btn)

        self.add_file_field("detail", "Damage detail .ply (optional):", [("PLY files", "*.ply")])
        self.add_hint(
            "Optional. Output from extract_damage_detail.py - real comparison-cloud "
            "geometry near flagged locations, added as a third layer alongside "
            "ChangeHighlight. Unlike ChangeHighlight (a magnitude value plotted at baseline "
            "positions), this shows what the damage/debris actually looks like right now. "
            "Leave blank to skip - the scene works fine without it. Not a tracked project "
            "stage - always picked by hand, or filled automatically by a successful "
            "'Extract Damage Detail...' run above.")

        output_default = self.resolve_project_output_default(pipeline, "export", ".usd")
        self.add_save_field("output", "Output .usd:", default_ext=".usd", default=output_default)
        self.register_auto_default(
            "output", lambda p: self.resolve_project_output_default(p, "export", ".usd"))

        self.add_checkbox("package_usdz", "Also package as .usdz")
        self.add_hint(
            "Most web, AR, and mobile USD viewers only accept .usdz, not raw .usd - check "
            "this if you want to preview the result without Omniverse. Saved next to the "
            ".usd file, same name.")

        self.add_checkbox(
            "downsample", "Downsample point cloud layers (reduces file size / viewer load)")
        self.add_length_field("voxel_size", "Voxel size (cm):", min_cm=0.1, max_cm=100)
        self.add_hint(
            "Off by default. Keeps one representative point per voxel cell - removes "
            "redundant near-duplicate points from overlapping scan passes, not real detail, "
            "as long as the voxel size stays smaller than the smallest feature you care "
            "about seeing. Doesn't affect mesh layers. Use point_spacing.py for a sense of "
            "your data's point spacing before choosing a value.")

        self.form.addStretch(1)

    def _build_run(self):
        """Real Run - matches open_export_dialog()'s build() in
        pipeline_applet.py, with the same correction as Stage 5: baseline
        and change are read directly from their fields unconditionally
        (see module docstring for why). active_pipeline still flows
        through to build_export_command() and finish_info, purely for
        tracking."""
        script = self.require("script", "USD export script")
        baseline = self.require_existing_file("baseline", "Baseline .ply")
        change = self.require("change", "Change-highlight .ply")
        detail = self.fields["detail"].get().strip() or None
        output = self.require("output", "Output .usd")
        package_usdz = self.fields["package_usdz"].get()
        voxel_size = None
        if self.fields["downsample"].get():
            try:
                voxel_size = float(self.fields["voxel_size"].get())
            except ValueError:
                raise ValueError("Voxel size must be a number in cm when downsampling is "
                                 "checked.")

        active_pipeline = self.get_active_pipeline_for_run()
        cmd = core.build_export_command(script, baseline, change, output,
                                         package_usdz=package_usdz, detail_ply=detail,
                                         voxel_size=voxel_size, pipeline=active_pipeline)
        finish_info = {"pipeline": active_pipeline, "stage_name": "export", "output": output}

        report = (
            "=== SUMMARY ===\n"
            f"Baseline used: {baseline}\n"
            f"Change-highlight used: {change}\n"
            + (f"Damage detail used: {detail}\n" if detail else "No damage detail layer.\n")
            + (f"Downsampling: voxel size {fmt_cm(voxel_size)}\n" if voxel_size else
               "No downsampling.\n")
            + f"Script: {script}\n"
            f"Saved to: {output}\n"
            + (f"Also packaged as: {Path(output).with_suffix('.usdz')}\n"
               if package_usdz else "")
            + "\n=== NOTE ===\n"
            "The change input needs a real M3C2/distance field carried through (Stage 7 "
            "carries it by default). If the tool output above has a warning about a "
            "missing M3C2/distance field, re-check which file you pointed this at.\n\n"
            "=== NEXT STEPS ===\n"
            "Open the .usd file in Omniverse (or any USD-compatible viewer) "
            "to check the scene: /World/Compartment/Baseline should look "
            "like the environment in muted grey, and .../ChangeHighlight "
            "should show the change regions colored blue (negative) to "
            "red (positive) by magnitude"
            + (", and .../DamageDetail should show the real current geometry "
               "in the same colors. " if detail else ". ")
            + "Use the .usdz instead for web/AR/mobile viewers."
        )
        return cmd, report, finish_info

    def _extract_damage_detail(self):
        """Runs extract_damage_detail.py and, on success, fills the
        'Damage detail .ply' field. In project mode the result is named
        and placed like every other stage output, inside this diff's own
        export folder (get_output_path(pipeline, "export", ".ply") -
        counted separately from the .usd numbering). In manual mode it
        asks for a save location."""
        flagged = self.fields["damage_detail_flagged"].get().strip()
        comparison = self.fields["damage_detail_comparison"].get().strip()
        if not flagged or not Path(flagged).exists():
            QMessageBox.critical(self, "No flagged file",
                                 "Enter a valid 'Flagged (Classify output) .ply' file first.")
            return
        if not comparison or not Path(comparison).exists():
            QMessageBox.critical(self, "No comparison file",
                                 "Enter a valid 'Comparison cloud (Stage 3 output) .ply' "
                                 "file first.")
            return
        try:
            radius = float(self.fields["damage_detail_radius"].get())
        except ValueError:
            QMessageBox.critical(self, "No radius", "Enter the radius in cm first.")
            return
        if radius <= 0:
            QMessageBox.critical(self, "Radius not valid", "The radius must be more than 0.")
            return
        if not self._check_length_plausibility(keys=("damage_detail_radius",)):
            return

        script_path = SCRIPTS_DIR / "extract_damage_detail.py"
        if not script_path.exists():
            QMessageBox.critical(self, "Missing script",
                                 f"extract_damage_detail.py not found:\n{script_path}")
            return
        if core is None:
            QMessageBox.critical(self, "Not available", "pipeline_core.py could not be imported.")
            return

        active_pipeline = self.get_active_pipeline_for_run()
        if active_pipeline is not None and pm is not None:
            try:
                save_path = str(pm.get_absolute_path(
                    active_pipeline.project,
                    pm.get_output_path(active_pipeline, "export", ".ply")))
            except pm.ProjectError as e:
                QMessageBox.critical(self, "Could not get a project path", str(e))
                return
            auto_named = True
        else:
            save_path, _ = QFileDialog.getSaveFileName(
                self, "Save damage detail as", "damage_detail.ply", "PLY files (*.ply)")
            if not save_path:
                return
            auto_named = False

        cmd = core.build_damage_detail_command(script_path, flagged, comparison, save_path,
                                               radius)

        def on_complete(returncode, cancelled):
            if cancelled or returncode != 0:
                QMessageBox.critical(
                    self, "Extract Damage Detail did not complete",
                    ("You stopped the process." if cancelled else
                     f"extract_damage_detail.py exited with code {returncode}.")
                    + " See the Terminal tab for details. 'Damage detail .ply' was not "
                    "changed.")
                return
            self.fields["detail"].set(save_path)
            self._show_success(
                "Damage detail extracted",
                f"Saved to:\n{save_path}\n\n"
                + ("This is a project run. The app named the file and put it in the "
                   "export folder of this diff.\n\n" if auto_named else "")
                + "'Damage detail .ply' now shows this result. To try a different "
                "radius, change it and extract again. Nothing is final until you run "
                "Export.")

        self._run_utility_command(cmd, on_complete)


class Stage8ExportDialog(QStageDialog, Stage8ExportFieldsMixin):
    """Standalone popup - run this file directly to open just this
    dialog."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__("Stage 8: Export to USD", parent, on_output=on_output,
                          on_status=on_status)
        self._build_export_fields(pipeline=pipeline)


class Stage8ExportPanel(QStagePanel, Stage8ExportFieldsMixin):
    """Embeddable panel - used by pipeline_applet_qt_template.py as a
    page of stageStack."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__(parent, on_output=on_output, on_status=on_status)
        self._build_export_fields(pipeline=pipeline)


def main():
    app = QApplication(sys.argv)
    dlg = Stage8ExportDialog(pipeline=None)
    dlg.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
