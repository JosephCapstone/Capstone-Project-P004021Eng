#!/usr/bin/env python3
"""
Stage 3: Cleanup - fields only
=================================
Defines Stage 3's fields, built field-for-field the same way
open_cleanup_dialog() builds them in pipeline_applet.py. The most
field-rich stage so far - on top of the usual project-mode input
picking, it has:

- A "Use Project Baseline (Cleanup output)" button, visible only for a
  SCAN pipeline (not baseline, not manual mode) - see
  add_conditional_project_button() in qt_stage_base.py.
- A manual-mode registered-baseline preset (add_registered_baseline_preset()),
  entirely unrelated to project mode - always available.
- A "register_as" field for manual-mode baseline registration, also
  unrelated to project mode.

Real Run behavior (build_cleanup_command(), the .ply-snapshot output
resolution, the RMS sidecar, the "no registration RMS found" warning)
matches open_cleanup_dialog()'s build() in pipeline_applet.py exactly -
the trickiest _build_run() so far, since CloudCompare doesn't let a
caller name its own output file directly (see resolve_cleanup_output()
in pipeline_core.py): report is a DEFERRED callable (build_report),
not a plain string, so it can snapshot-diff the input's own folder for
new .ply files AFTER the subprocess exits, rename the real result to
the requested Output path, and only THEN know what to hand
finish_stage() as this stage's real output/extra_fields
(icp_rms/sidecar) via finish_info's "resolve_state" dict.

Run standalone:
    pip install PySide6
    python stage3_cleanup_dialog_qt.py
"""
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from qt_stage_base import QStageDialog, QStagePanel

try:
    import project_manager as pm
except ImportError:
    pm = None

try:
    import pipeline_core as core
except ImportError:
    core = None


class Stage3CleanupFieldsMixin:

    def _build_cleanup_fields(self, pipeline=None):
        self.add_pipeline_label(pipeline)

        self.add_file_field("input", "Input .ply:", [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "input", lambda: pm.list_eligible_inputs(self.pipeline, "cleanup"))

        self.add_preset_selector("SOR preset:", [
            ("Conservative - keeps more points, safer on sparse scans",
             {"sor_neighbors": "10", "sor_std_dev": "2.0"}),
            ("Balanced - good default", {"sor_neighbors": "6", "sor_std_dev": "1.0"}),
            ("Aggressive - removes more noise, risk of losing real detail",
             {"sor_neighbors": "6", "sor_std_dev": "0.5"}),
        ])
        self.add_text_field("sor_neighbors", "SOR neighbors:", default="6")
        self.add_text_field("sor_std_dev", "SOR std dev:", default="1.0")
        self.add_hint("Neighbors = how many nearby points are checked per point (higher = "
                       "smoother statistic, slower). Std dev = how many standard deviations "
                       "from the local average counts as an outlier (lower = more aggressive "
                       "removal). For precise tuning, CloudCompare's own SOR dialog shows a "
                       "live preview - dial it in there once, then reuse the same numbers "
                       "here.")

        self.add_file_field("align_to", "Align to (optional baseline .ply):",
                             [("PLY files", "*.ply")])

        self.add_conditional_project_button(
            "Use Project Baseline (Cleanup output)",
            condition_fn=lambda p: p is not None and p.kind == "scan",
            on_click=self._use_project_baseline)
        self.add_hint(
            "Fills 'Align to' with this project's own baseline pipeline's cleanup output - "
            "the usual choice for a comparison scan (a later diff's registration-error "
            "figure reads this scan's icp_rms from whichever cleanup run actually happened, "
            "so aligning to the real project baseline here is what makes a later "
            "'vs. baseline' diff meaningful).")

        self.add_registered_baseline_preset("align_to")
        self.add_hint("Optional. If set, runs ICP to fine-align this cloud onto the baseline "
                       "after cleanup - use this when comparing a later scan to a stored "
                       "baseline. Leave blank when just cleaning a standalone map - for "
                       "example, when this cloud IS the baseline you're creating.")

        output_default = self.resolve_project_output_default(pipeline, "cleanup", ".ply")
        self.add_save_field("output", "Output .ply:", default_ext=".ply", default=output_default)
        self.register_auto_default(
            "output", lambda p: self.resolve_project_output_default(p, "cleanup", ".ply"))
        self.add_hint("The name you want the cleaned result saved as. CloudCompare actually "
                       "names the file itself when it runs - this gets detected and renamed "
                       "to what you asked for automatically afterward (an explicit-naming "
                       "CloudCompare flag was tried here previously but proved unreliable in "
                       "real testing, so this detect-and-rename approach replaced it).")

        self.add_text_field(
            "register_as", "Also register this output as a manual-mode baseline for:",
            default="")
        self.add_hint(
            "Optional, and separate from project mode. If set, this run's cleaned output "
            "becomes the active entry in the standalone baseline_registry.json (selectable "
            "above, and in Stage 5's manual mode) - use this for manual-mode work outside a "
            "project. In project mode, the project's own baseline pipeline already IS the "
            "record that matters - no separate registration needed for that.")

        self.form.addStretch(1)

    def _use_project_baseline(self, pipeline):
        if pm is None:
            return
        try:
            path = pm.get_baseline_cleanup_output(pipeline.project)
        except pm.ProjectError as e:
            QMessageBox.information(self, "Baseline not ready", str(e))
            return
        self.fields["align_to"].set(path)

    def _build_run(self):
        """Real Run - matches open_cleanup_dialog()'s build() in
        pipeline_applet.py exactly, including the deferred report
        callable (build_report) that only runs AFTER the subprocess
        exits."""
        input_ply = self.require("input", "Input .ply")
        output = self.require("output", "Output .ply")
        align_to = self.fields["align_to"].get().strip() or None
        if align_to and not Path(align_to).exists():
            raise ValueError(
                f"'Align to' points at a file that doesn't exist:\n{align_to}\n\n"
                "If you picked this from a preset dropdown, that entry may be stale "
                "(the file was moved/renamed/deleted since it was registered). "
                "Either fix the path or clear the field.")
        register_as = self.fields["register_as"].get().strip() or None
        try:
            neighbors = int(self.fields["sor_neighbors"].get())
            std_dev = float(self.fields["sor_std_dev"].get())
        except ValueError:
            raise ValueError("SOR neighbors must be an integer and std dev a number.")

        active_pipeline = self.get_active_pipeline_for_run()

        # CloudCompare doesn't auto-write a distinct RMS/ICP report file -
        # its stats only appear in the console unless we ask for -LOG_FILE.
        # This path is one we choose, so we know exactly where to look.
        log_path = Path(output).with_name(Path(output).stem + "_cc_log.txt")
        cmd = core.build_cleanup_command(input_ply, output, neighbors, std_dev,
                                          align_to, log_file=log_path,
                                          pipeline=active_pipeline)

        # Snapshot .ply files now, before running, so the actual output can
        # be identified afterward by what's new - see resolve_cleanup_output.
        # This watches INPUT's own folder, not the desired OUTPUT's folder:
        # CloudCompare's -SAVE_CLOUDS saves each loaded cloud next to itself
        # (it has no concept of "the folder the caller wants"), the same
        # behavior Stage 5 (Diff)'s own snapshot already accounts for.
        input_dir = Path(input_ply).resolve().parent
        existing_ply_before = set(input_dir.glob("*.ply")) if input_dir.is_dir() else set()

        resolved_state = {}

        def build_report():
            summary = (
                "=== SUMMARY ===\n"
                f"Input cleaned: {input_ply}\n"
                f"SOR settings: {neighbors} neighbors, {std_dev} std dev\n"
                + (f"Aligned (ICP) to: {align_to}\n" if align_to else
                   "No alignment performed (no baseline given).\n")
            )

            resolved, others, error = core.resolve_cleanup_output(
                input_ply, input_dir, existing_ply_before, output)

            if error:
                summary += f"\n=== OUTPUT ===\n{error}\n"
            else:
                summary += f"\nSaved to: {resolved}\n"
                resolved_state["output"] = resolved
                if others:
                    summary += (
                        f"(Plus {len(others)} other new file(s) CloudCompare saved - "
                        f"{', '.join(f.name for f in others)} - a throwaway resave of "
                        f"the baseline when aligning, not an extra result.)\n"
                    )

            if align_to:
                rms = core.parse_registration_rms(log_path)
                if rms is None:
                    QMessageBox.warning(
                        self, "Registration RMS not found",
                        "Couldn't find an RMS value in this run's CloudCompare log.\n\n"
                        "This means Stage 5 (Diff)'s M3C2 significance test won't have "
                        "a real registration-error figure to work with. Leave that "
                        "field blank there rather than entering 0 - a 0 collapses "
                        "M3C2's Level of Detection calculation, which flags almost "
                        "every point as significant, which is worse than not running "
                        "the significance test at all.")
                    summary += (
                        "\n=== REGISTRATION RMS ===\n"
                        "Not found in the CloudCompare log - the significance test "
                        "in Stage 5 (Diff) won't have a real registration-error value "
                        "to use. Leave that field blank there rather than entering 0.")
                elif resolved:
                    sidecar = core.save_rms_sidecar(resolved, rms, log_path)
                    sidecar_path = Path(resolved).with_name(Path(resolved).stem + "_rms.json")
                    resolved_state.setdefault("extra_fields", {})["icp_rms"] = rms
                    if active_pipeline is not None:
                        try:
                            resolved_state["extra_fields"]["sidecar"] = \
                                pm.to_relative_path(active_pipeline.project, sidecar_path)
                        except pm.ProjectError:
                            resolved_state["extra_fields"]["sidecar"] = str(sidecar_path)
                    else:
                        resolved_state["extra_fields"]["sidecar"] = str(sidecar_path)
                    summary += (
                        f"\n=== REGISTRATION RMS ===\n"
                        f"{rms:.6f} m - saved alongside this output, so Stage 5 (Diff) "
                        f"can look it up automatically when you select this file as "
                        f"the comparison cloud.\n"
                        f"Note: a higher RMS from an informal/handheld capture (no "
                        f"fixed scan position) is expected, not necessarily a "
                        f"problem - it should trend lower once captures come from a "
                        f"fixed test rig. Treat this as a per-run diagnostic, not a "
                        f"pass/fail number on its own.")

            if register_as and resolved:
                core.register_baseline(register_as, resolved)
                summary += (
                    f"\nRegistered as the manual-mode active baseline for "
                    f"'{register_as}' - pick it from the dropdown in Stage 3/5 next "
                    f"time instead of browsing to this file.\n")
            elif register_as and not resolved:
                summary += (
                    "\nCouldn't register as baseline - the output file couldn't be "
                    "confidently identified (see OUTPUT section above).\n")

            summary += (
                "\n=== NEXT STEPS ===\n"
                "Open the cleaned cloud in CloudCompare to confirm alignment, "
                "then use it (plus a matching cleaned baseline/comparison) in "
                "Stage 5 (Diff)."
            )

            annotated = core.annotate_log_file(log_path, "Stage 3: Cleanup", cmd)
            if annotated:
                resolved_state["log_path"] = log_path
                summary += (
                    f"\n\n=== CLOUDCOMPARE LOG (with headers added) ===\n"
                    f"Saved to: {log_path}\n"
                    "This file now has clear section headers wrapped around "
                    "CloudCompare's raw output (RMS/fitness stats for ICP runs "
                    "are in there) - open it directly for the full detail.")
            else:
                summary += (
                    "\n\n=== CLOUDCOMPARE LOG ===\n"
                    "No log file was found at the expected path - check the "
                    "Terminal tab for RMS/alignment stats instead.")
            return summary

        finish_info = {"pipeline": active_pipeline, "stage_name": "cleanup",
                        "output": output, "resolve_state": resolved_state}
        return cmd, build_report, finish_info


class Stage3CleanupDialog(QStageDialog, Stage3CleanupFieldsMixin):
    """Standalone popup - run this file directly to open just this
    dialog."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__("Stage 3: Cleanup", parent, on_output=on_output, on_status=on_status)
        self._build_cleanup_fields(pipeline=pipeline)


class Stage3CleanupPanel(QStagePanel, Stage3CleanupFieldsMixin):
    """Embeddable panel - used by pipeline_applet_qt_template.py as a
    page of stageStack."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__(parent, on_output=on_output, on_status=on_status)
        self._build_cleanup_fields(pipeline=pipeline)


def main():
    app = QApplication(sys.argv)
    dlg = Stage3CleanupDialog(pipeline=None)
    dlg.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
