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

Run check (added): before every Run, Stage5RunCheckMixin compares the M3C2
params file with the fields on the page (registration RMS, normal scale, search
scale, search depth). A params file does not change when a field changes, so an
old file would give an old registration error without any message. If the file
and the page differ, Run asks: No stops the run, Yes runs with the file as it is.
The mixin hooks into _check_length_plausibility(), the check that
qt_stage_base._on_run_clicked() already runs first and that stops the run quietly
when it returns False.

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
                           fmt_cm, diff_input_default, _same_value)

try:
    import project_manager as pm
except ImportError:
    pm = None

try:
    import pipeline_core as core
except ImportError:
    core = None



def _outside_note(path):
    """Report text for a Diff input: says if the points outside the room
    (Stage 4's 'outside_envelope' junk) were removed or not."""
    name = Path(str(path)).name
    if name.endswith("_envelope_filtered.ply"):
        return " (points outside the room removed)"
    if name.endswith("_classified.ply"):
        return (" (all points - points outside the room NOT removed. To remove them, "
                "run Stage 4 with 'Also write a separate copy...' on)")
    return ""

PARAMS_TOLERANCE_M = 1e-6   # 0.0001 cm: the params file stores the same numbers as the fields


def read_m3c2_params_file(path):
    """Reads the values that Stage 5 cares about from an M3C2 params .txt file
    (an INI-style file: 'Key=Value' lines). Returns a dict. A key that is not in
    the file, or not a number, is left out. Returns {} if the file cannot be read."""
    wanted = {"NormalScale", "SearchScale", "SearchDepth", "RegistrationError"}
    values = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                key, separator, text = line.strip().partition("=")
                if not separator:
                    continue
                if key in wanted:
                    try:
                        values[key] = float(text)
                    except ValueError:
                        pass
                elif key == "RegistrationErrorEnabled":
                    values[key] = text.strip().lower() == "true"
    except OSError:
        return {}
    return values


def params_differences(file_values, field_values):
    """Compares the params file with the fields on the page.
    file_values: dict from read_m3c2_params_file().
    field_values: dict {"NormalScale": metres or None, ...} from the page fields.
    Returns a list of (label, file_value_m, page_value_m) for each value that
    is different. A field that is blank (None) is not compared."""
    labels = (("RegistrationError", "Registration RMS"),
              ("NormalScale", "Normal scale"),
              ("SearchScale", "Search/projection scale"),
              ("SearchDepth", "Search depth"))
    different = []
    for key, label in labels:
        page_value = field_values.get(key)
        file_value = file_values.get(key)
        if page_value is None or file_value is None:
            continue
        if abs(page_value - file_value) > PARAMS_TOLERANCE_M:
            different.append((label, file_value, page_value))
    return different


class Stage5RunCheckMixin:
    """Put this class BEFORE the stage base class in the class list (see
    Stage5DiffDialog and Stage5DiffPanel below).

    qt_stage_base.py calls self._check_length_plausibility() at the start of every
    Run, and a False answer stops the run quietly. This class adds one more check
    to that call: the M3C2 params file must have the same values as the fields on
    the page. A params file does not change when a field changes. An old file gives
    an old registration error, and Stage 5 then flags the wrong points."""

    def _check_length_plausibility(self, keys=None):
        if not super()._check_length_plausibility(keys):
            return False
        if keys is not None:        # a helper button, not the main Run
            return True
        return self._confirm_params_match_fields()

    def _confirm_params_match_fields(self):
        """True when the run can go on."""
        path = self.fields["params"].get().strip()
        if not path or not Path(path).is_file():
            return True             # _build_run() reports a missing file
        file_values = read_m3c2_params_file(path)
        if not file_values:
            return True             # not a file that this check can read
        field_values = {}
        for key, field in (("NormalScale", "normal_scale"), ("SearchScale", "search_scale"),
                           ("SearchDepth", "search_depth"),
                           ("RegistrationError", "registration_rms")):
            try:
                field_values[key] = float(self.fields[field].get())
            except ValueError:
                field_values[key] = None    # blank, or not a number
        different = params_differences(file_values, field_values)
        rms_switched_off = (field_values.get("RegistrationError") is not None
                            and file_values.get("RegistrationErrorEnabled") is False)
        if not different and not rms_switched_off:
            return True
        lines = [f"  {label}: file {fmt_cm(file_value)}, page {fmt_cm(page_value)}"
                 for label, file_value, page_value in different]
        if rms_switched_off:
            lines.append("  The registration error is switched off in the file.")
        answer = QMessageBox.question(
            self, "The params file is not the same as the page",
            "The M3C2 params file has other values than the fields on this page:\n\n"
            + "\n".join(lines)
            + f"\n\nFile: {path}\n\n"
            "Stage 5 uses the values in the file. It does not use the values on the page.\n\n"
            "To write the page values into the file, click No. Then push "
            "'Generate Params File...' and push Run again.\n\n"
            "Run with the params file as it is?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes


def _default_registration_rms(pipeline):
    """Update 28. The ICP RMS of the Stage 3 run of the Comparison scan, in
    metres as text, or "" (never raises)."""
    if pm is None or pipeline is None or getattr(pipeline, "kind", None) != "diff":
        return ""
    try:
        value = pm.get_diff_inputs(pipeline)["registration_error_used"]
        return f"{float(value):.6f}" if value is not None else ""
    except Exception:
        return ""


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
        self.register_auto_default(
            "baseline", lambda p: diff_input_default(p, "reference"), fill_now=True)
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
        self.register_auto_default(
            "comparison", lambda p: diff_input_default(p, "comparison"), fill_now=True)
        self.form = self._layout_stack.pop()

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
            if value == "min":
                first_range_radio = radio
        spacing_row.addStretch(1)
        self.form.addLayout(spacing_row)

        self.add_length_field("normal_scale", "Normal scale (diameter, cm):",
                              min_cm=0.1, max_cm=200)
        self.add_length_field("search_scale", "Search/projection scale (diameter, cm):",
                              min_cm=0.05, max_cm=100)
        self.add_length_field("search_depth", "Search depth (max depth, cm):",
                              min_cm=0.1, max_cm=500)

        rms_row = self._row("Registration RMS (cm, from Stage 3):")
        rms_edit = QLineEdit()
        rms_row.addWidget(rms_edit, 1)
        load_rms_btn = QPushButton("Load from Stage 3")
        load_rms_btn.clicked.connect(self._load_rms_from_sidecar)
        rms_row.addWidget(load_rms_btn, 0)
        self.form.addLayout(rms_row)
        # Shown in cm; get()/set() stay in metres (see LengthFieldRef).
        self.fields["registration_rms"] = LengthFieldRef(rms_edit, min_cm=0.01, max_cm=50)
        # Update 28: the RMS of the Comparison scan follows the selected diff pipeline.
        self.register_auto_default("registration_rms", _default_registration_rms,
                                   fill_now=True)
        estimate_btn = QPushButton("Check Registration (quick)...")
        estimate_btn.setToolTip(
            "Estimates the registration error from a sample of the two scans. "
            "No CloudCompare run. Uses the Normal scale, Search scale and Search "
            "depth above.")
        estimate_btn.clicked.connect(self._check_registration_quick)
        self.form.addWidget(estimate_btn)

        generate_btn = QPushButton("Generate Params File...")
        generate_btn.clicked.connect(self._generate_params_file)
        self.form.addWidget(generate_btn)
        # Update 28: one highlighted note for all four values (it replaces the
        # three separate notes), and a red line that shows only when a value
        # changed after the last Generate.
        params_note = QLabel(
            "NOTE: A params file does not change when you change Registration RMS, Normal "
            "scale, Search scale or Search depth. Push 'Generate Params File...' again "
            "after a change. Make sure that 'M3C2 params file' shows the new file before "
            "you push Run.")
        params_note.setWordWrap(True)
        params_note.setStyleSheet(
            "background-color: #fff3cd; color: #5c4400; border: 1px solid #e0b800; "
            "padding: 4px;")
        self.form.addWidget(params_note)
        self.params_stale_label = QLabel(
            "A value changed after the last params file was made. Push 'Generate Params "
            "File...' again.")
        self.params_stale_label.setWordWrap(True)
        self.params_stale_label.setStyleSheet("color: #bb0000; font-weight: bold;")
        self.params_stale_label.setVisible(False)
        self.form.addWidget(self.params_stale_label)
        self._params_snapshot = None
        for key in self.PARAMS_VALUE_KEYS:
            self.fields[key].widget.textChanged.connect(self._update_params_stale)

        self.add_file_field("params", "M3C2 params file (.txt):", [("Text files", "*.txt")])

        # Update 29: the explanations are in "?" popups (what it does, what the
        # options do, when to change it).
        self.add_help(
            "baseline",
            "The reference cloud: the compartment as it was before the change. Stage 5 "
            "measures the distance from this cloud to the comparison cloud.",
            when="Change it to compare against another baseline. 'Choose from project...' "
                 "lists the files of this project. 'Or pick a manual-mode registered "
                 "baseline' lists the baselines of baseline_registry.json.",
            note="For a Stage 4 (Segment) output, use the file marked 'outside-room points "
                 "removed' (<name>_envelope_filtered.ply). It does not have the scan noise "
                 "and the returns from beyond the walls, which can show as false damage. If "
                 "you choose <name>_classified.ply and the filtered copy exists, Run asks "
                 "which one to use.")
        self.add_help(
            "comparison",
            "The cloud that you compare to the baseline: the later scan, with the possible "
            "damage.",
            when="Change it to compare another scan. In project mode the box fills from "
                 "the selected pipeline.",
            note="For a Stage 4 (Segment) output, use the file marked 'outside-room points "
                 "removed' (<name>_envelope_filtered.ply). It does not have the scan noise "
                 "and the returns from beyond the walls, which can show as false damage. If "
                 "you choose <name>_classified.ply and the filtered copy exists, Run asks "
                 "which one to use.")
        self.add_help(
            first_range_radio,
            "Selects the Normal scale that 'Check Point Spacing' puts in the box, from the "
            "range that it suggests. The button also sets Search scale to 0.5 times and "
            "Search depth to 2 times the Normal scale.",
            effects=[
                ("Min", "the smallest scale of the range. It resolves small damage and "
                        "sharp edges. The result is noisier when the scale is close to the "
                        "point spacing."),
                ("Mid", "a balance of the two. It is a good first value."),
                ("Max", "the largest scale of the range. It is the safest against noise. "
                        "It rounds small features and corners."),
            ],
            when="Use it before you push 'Check Point Spacing'. Change it when the result "
                 "is too noisy (use a larger scale) or too round (use a smaller scale).")
        self.add_help(
            "normal_scale",
            "The diameter of the sphere that M3C2 uses to find the surface direction "
            "(the normal) at each point.",
            effects=[
                ("Smaller", "small damage and sharp edges are resolved. The result is "
                            "noisier."),
                ("Larger", "the result is smoother and safer against noise. Small features "
                           "and corners become round."),
            ],
            when="'Check Point Spacing' fills the box. Change it by hand if the result is "
                 "too noisy or too round. You can also use 'Guess params' of CloudCompare.",
            note="After you change the Normal scale by hand, change Search scale and "
                 "Search depth to match. CloudCompare 'Guess params' does not do this "
                 "automatically. A Search scale that does not match gives round edges.")
        self.add_help(
            "search_scale",
            "The diameter of the cylinder that M3C2 uses to find the points on each side "
            "of the surface (projection scale).",
            effects=[
                ("Smaller", "fewer points in the cylinder. The distances are noisier."),
                ("Larger", "more points in the cylinder. The distances are smoother and "
                           "small features are blurred."),
            ],
            when="You changed the Normal scale. Set this value to 0.5 times the Normal "
                 "scale. 'Check Point Spacing' fills it.")
        self.add_help(
            "search_depth",
            "The length of the cylinder along the normal. M3C2 measures only changes "
            "that are inside this depth.",
            effects=[
                ("Smaller", "a change that is deeper than this value gets no distance."),
                ("Larger", "deeper changes are measured. A cylinder that is too long can "
                           "find a different surface behind the first one."),
            ],
            when="You changed the Normal scale. Set this value to 2 times the Normal "
                 "scale. 'Check Point Spacing' fills it. Raise it if you must measure "
                 "deeper damage.")
        self.add_help(
            "registration_rms",
            "The registration error of the comparison scan from Stage 3 (ICP RMS). M3C2 "
            "uses it to calculate the Level of Detection (LOD). The LOD separates real "
            "change from noise and misalignment.",
            effects=[
                ("Smaller", "a small LOD. More points count as significant change."),
                ("Larger", "a large LOD. Fewer points count as change. Small damage can "
                           "be missed."),
                ("Empty", "the significance test uses no registration error."),
            ],
            when="'Load from Stage 3' fills the box from the Cleanup file of the "
                 "comparison. 'Check Registration (quick)' estimates the value in 5 to 30 "
                 "seconds, before you run Stage 5. Fill Normal scale, Search scale and "
                 "Search depth first. On a SLAM map the ICP RMS can be large. Stage 6 can "
                 "replace it with a value that it measures from the distances, and it reads "
                 "the value that you enter here. You do not need to run Stage 5 again.",
            note="Do not enter 0. A 0 makes the LOD almost zero and almost every point "
                 "counts as significant. Leave the box empty if you do not know the value.")
        self.add_help(
            "params",
            "The M3C2 parameter file (.txt) that CloudCompare reads when Run starts.",
            when="Use 'Generate Params File...' to write the file from the values on this "
                 "page. Or browse to a file that you made in the CloudCompare GUI. Make "
                 "the file again after you change a value.")

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
        baseline = self._offer_envelope_filtered("baseline", baseline, "Baseline")
        comparison = self._offer_envelope_filtered("comparison", comparison, "Comparison")
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
                f"Baseline: {baseline}{_outside_note(baseline)}\n"
                f"Comparison: {comparison}{_outside_note(comparison)}\n"
                f"Params file used: {params}\n"
                + (f"Registration RMS on hand: {fmt_cm(registration_rms)}. This value is in "
                   f"the params file only if you pushed 'Generate Params File...' after you "
                   f"entered it. If you selected a file that you made earlier, or a file made "
                   f"in CloudCompare, the file keeps its own registration error. Make sure "
                   f"that value is the same.\n"
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

            # Registration check: the spread of the M3C2 distances (robust).
            # The block goes to the TOP of the report, with a one-line verdict,
            # so that it is not missed in the long report.
            # The ICP RMS of Stage 3 is a point-to-point distance. M3C2 works
            # along the surface normal, so the two can differ a lot (real test:
            # ICP 5.35 cm, M3C2 spread 0.31 cm). This estimate is the one that
            # the M3C2 significance test needs. It never stops the run.
            result_ply = None
            if resolved_state.get("output"):
                result_ply = core.find_m3c2_result([resolved_state["output"]])
            if result_ply is None:
                result_ply = core.find_m3c2_result(
                    core.find_new_ply_files(output_dir, existing_ply_before))
            estimate = core.robust_m3c2_spread(result_ply) if result_ply else None
            registration_block = ""
            if estimate:
                try:
                    entered_rms = float(registration_rms) if registration_rms else None
                except ValueError:
                    entered_rms = None
                registration_block = (
                    "=== REGISTRATION CHECK: "
                    + core.registration_headline(entered_rms, estimate) + " ===\n"
                    + core.registration_advice(entered_rms, estimate) + "\n\n")
                if "extra_fields" in resolved_state:
                    resolved_state["extra_fields"]["registration_estimate_robust"] = \
                        round(estimate["robust_sigma"], 6)
                    resolved_state["extra_fields"]["m3c2_median_offset"] = \
                        round(estimate["median"], 6)
            else:
                registration_block = (
                    "=== REGISTRATION CHECK: NO ESTIMATE ===\n"
                    "No estimate: the result file with the M3C2 distance was not found, "
                    "or it has too few distances, or the Python package plyfile is not "
                    "installed.\n\n")

            summary += (
                "\n=== NEXT STEPS ===\n"
                "Open the result in CloudCompare, set the active scalar field to "
                "'M3C2 distance', and check the color scale - red = positive change, "
                "blue = negative, magnitude = degree of change. Then use it as the "
                "input to Stage 6 (Classify)."
            )

            summary = registration_block + summary

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

    def _offer_envelope_filtered(self, key, path, label):
        """Update 6. If path is a Stage 4 (Segment) <name>_classified.ply
        and the same pass also wrote <name>_envelope_filtered.ply (the
        same points without the unclassified points outside the room),
        asks which one to use. Yes puts the filtered copy in the field and
        returns it. No keeps path. Any other file is returned as it is."""
        sibling = pm.envelope_filtered_sibling(path) if pm is not None else None
        if sibling is None:
            return path
        answer = QMessageBox.question(
            self, "Use the copy without the points outside the room?",
            f"{label} is a Stage 4 (Segment) cloud with all of its points:\n{path}\n\n"
            f"The same pass also has a copy without the points outside the room:\n"
            f"{sibling.name}\n\n"
            "The points outside the room are scan noise and returns from beyond the "
            "walls. In M3C2 they can show as false damage.\n\n"
            "Yes: use the copy without these points (recommended).\n"
            "No: use the file as it is.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer == QMessageBox.Yes:
            self.fields[key].set(str(sibling))
            return str(sibling)
        return path

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

    PARAMS_VALUE_KEYS = ("registration_rms", "normal_scale", "search_scale", "search_depth")

    def _params_values(self):
        return tuple(self.fields[k].get().strip() for k in self.PARAMS_VALUE_KEYS)

    def _update_params_stale(self, *_args):
        """Red line: shown only when one of the four values is not the value
        that the last 'Generate Params File...' of this page used."""
        snapshot = getattr(self, "_params_snapshot", None)
        stale = snapshot is not None and not all(
            _same_value(a, b) for a, b in zip(snapshot, self._params_values()))
        label = getattr(self, "params_stale_label", None)
        if label is not None:
            label.setVisible(bool(stale))

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
        self._params_snapshot = self._params_values()
        self._update_params_stale()
        self._show_success(
            "Params file generated",
            f"Saved to:\n{save_path}\n\n"
            + ("This is a project run, so the file was named and placed automatically "
               "inside this diff's own output folder - no save location to pick by "
               "hand.\n\n" if auto_named else "")
            + "Every setting besides these four values (normal/search scale, search depth, "
            "registration error) is copied from a reference file. If you need to change "
            "something else (subsample radius, normal mode, etc.), make a file with the "
            "CloudCompare GUI instead.")

    def _check_registration_quick(self):
        """Estimate the registration error before Stage 5 runs, without
        CloudCompare (core.quick_registration_estimate). Shows the result and
        asks if it must be put in the Registration RMS field. A synchronous
        call with a wait cursor, same as 'Check Point Spacing'."""
        baseline = self.fields["baseline"].get().strip()
        comparison = self.fields["comparison"].get().strip()
        for path, name in ((baseline, "Baseline"), (comparison, "Comparison")):
            if not path or not Path(path).exists():
                QMessageBox.critical(
                    self, f"No {name.lower()} file",
                    f"Fill in a valid {name} .ply file first.")
                return
        baseline = self._offer_envelope_filtered("baseline", baseline, "Baseline")
        comparison = self._offer_envelope_filtered("comparison", comparison, "Comparison")

        scales = {}
        for key, name in (("normal_scale", "Normal scale"),
                          ("search_scale", "Search scale"),
                          ("search_depth", "Search depth")):
            try:
                scales[key] = float(self.fields[key].get().strip())
            except ValueError:
                QMessageBox.critical(
                    self, f"{name} is empty",
                    f"Fill in the {name} first. Push 'Check Point Spacing (Baseline)...' "
                    "to fill the three scale fields.")
                return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            estimate, message = core.quick_registration_estimate(
                baseline, comparison, scales["normal_scale"], scales["search_scale"],
                scales["search_depth"])
        finally:
            QApplication.restoreOverrideCursor()
        if estimate is None:
            QMessageBox.warning(self, "Check Registration (quick)", message)
            return

        try:
            entered = self.fields["registration_rms"].get().strip()
            entered_rms = float(entered) if entered else None
        except ValueError:
            entered_rms = None
        sigma = estimate["robust_sigma"]
        text = (
            f"{core.registration_headline(entered_rms, estimate)}\n\n"
            f"This is an estimate from {estimate['n_core']} sample points. It does not "
            "use CloudCompare.\n\n"
            + core.registration_advice(entered_rms, estimate)
            + f"\n\nPut {sigma * 100:.2f} cm in 'Registration RMS'?\n"
            "After that, push 'Generate Params File...' again.")
        answer = QMessageBox.question(
            self, "Check Registration (quick)", text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer == QMessageBox.StandardButton.Yes:
            self.fields["registration_rms"].set(f"{sigma:.6f}")

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
            f"These values replace the values that were in those three fields.\n"
            f"The params file does not change yet. Push 'Generate Params File...' "
            f"to write these values into the file."
        )
        QMessageBox.information(self, "Point Spacing (Baseline)", output)


class Stage5DiffDialog(Stage5RunCheckMixin, QStageDialog, Stage5DiffFieldsMixin):
    """Standalone popup - run this file directly to open just this
    dialog."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__("Stage 5: Diff (M3C2)", parent, on_output=on_output, on_status=on_status)
        self._build_diff_fields(pipeline=pipeline)


class Stage5DiffPanel(Stage5RunCheckMixin, QStagePanel, Stage5DiffFieldsMixin):
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
