#!/usr/bin/env python3
"""
Stage 8: Export to USD - fields only
=======================================
Defines Stage 8's fields, built field-for-field the same way
open_export_dialog() builds them in pipeline_applet.py. Last stage - a
diff-bound stage (pipeline.kind == "diff"), same group as Stages 5-7.

Update 24: the page is arranged in the order of the work, so that it is
clear which file goes where:

  "How this stage works"   three short lines
  [Fill empty fields from this diff]   (project mode only)
  "1. Scene files"        one field for each layer of the USD scene, with a
                          note that says what the layer shows and which stage
                          makes the file:
                            required:  Baseline, Change layer
                            optional:  Flagged points, Damage sites, Surface
                                       labels, Damage detail
  "Scene layers" summary  a live list: for each layer, OK / not used / MISSING
  [Make the damage detail file]   a box that opens and closes (closed by
                          default). It has the Extract Damage Detail inputs.
                          Its result fills the 'Damage detail' field above.
  "2. Output"             Output .usd, .usdz, downsampling

Field keys are the same as before update 24 (script, baseline, change,
flagged, clusters, surfaces, detail, output, package_usdz, downsample,
voxel_size, damage_detail_flagged, damage_detail_comparison,
damage_detail_radius).

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

"Extract Damage Detail..." runs extract_damage_detail.py via
pipeline_core.build_damage_detail_command() (flags confirmed from the
script's own argparse) through _run_utility_command(), so it has the same
running lock and Stop support as a stage run. Its two input fields have
picker buttons (flagged: the diff's Classify outputs via
list_eligible_inputs(pipeline, "surface"); comparison: the diff's
comparison side via list_side_candidates()). Update 24: in project mode
both fields are filled from the project records (the Classify output of
this diff, and the comparison cloud that Stage 5 used). A line under them
says whether the Classify file has the change type: the detail points then
carry the change type (added, removed, hole, unmatched) of the nearest
flagged point, and the USD export colours the layer by it.

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
import html
import sys
from pathlib import Path

from PySide6.QtCore import Qt
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


def _suggested_export_input(pipeline, key):
    """Update 28. The file that the project records for the Baseline or the
    Change layer (the same file as the button 'Fill empty fields from this
    diff'), or "" (never raises)."""
    if core is None or pipeline is None:
        return ""
    try:
        return core.suggest_export_inputs(pipeline).get(key) or ""
    except Exception:
        return ""


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


def _default_clusters_json(pipeline):
    """Pre-fill for 'Damage sites (.clusters.json)': the cluster summary
    m3c2_classify.py wrote next to this diff's recorded Classify output
    (pipeline_core.classify_clusters_path()). "" when there is none -
    manual mode, Classify not run yet, or clustering was off."""
    if pipeline is None or pipeline.kind != "diff" or pm is None or core is None:
        return ""
    try:
        output = (pipeline.entry.get("stages", {}).get("classify", {}) or {}).get("output")
    except Exception:
        return ""
    if not output:
        return ""
    path = core.classify_clusters_path(pm.get_absolute_path(pipeline.project, output))
    return str(path) if path.is_file() else ""


def _default_surfaces(pipeline):
    """Pre-fill for 'Surface labels (Stage 4 .ply)': the Stage 4 (Segment)
    cloud of this diff's reference scan - see
    pipeline_core.default_surfaces_for_diff(). "" when there is none
    (manual mode, or the reference side has no Segment run)."""
    if core is None:
        return ""
    return core.default_surfaces_for_diff(pipeline) or ""


def _default_flagged(pipeline):
    """Pre-fill for 'Flagged points (Classify .ply)': this diff's Classify
    output, only when that run used 'Keep all points' - see
    pipeline_core.default_flagged_for_diff(). "" otherwise."""
    if core is None:
        return ""
    return core.default_flagged_for_diff(pipeline) or ""


def _default_detail_flagged(pipeline):
    """Pre-fill for Extract Damage Detail's 'Classify output': this diff's
    recorded Stage 6 output, with or without 'Keep all points' - see
    pipeline_core.default_detail_flagged_for_diff() (update 24)."""
    if core is None:
        return ""
    return core.default_detail_flagged_for_diff(pipeline) or ""


def _default_detail_comparison(pipeline):
    """Pre-fill for Extract Damage Detail's 'New scan': the comparison cloud
    that this diff's Stage 5 run used - see
    pipeline_core.default_detail_comparison_for_diff() (update 24)."""
    if core is None:
        return ""
    return core.default_detail_comparison_for_diff(pipeline) or ""


# What each layer is called in the summary list, in the order of the USD scene.
LAYER_NAMES = {
    "baseline": "Baseline",
    "change": "ChangeHighlight",
    "flagged": "FlaggedPoints",
    "clusters": "DamageSites",
    "surfaces": "Surface labels (on DamageSites)",
    "detail": "DamageDetail",
}
GREEN, RED, GREY = "#008844", "#cc0000", "#777777"


class Stage8ExportFieldsMixin:

    def _add_heading(self, text):
        label = QLabel(text)
        label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        self.form.addWidget(label)
        return label

    def _build_export_fields(self, pipeline=None):
        self.add_pipeline_label(pipeline)

        self.add_hint("Stage 8 makes one USD scene from files of the earlier stages. Each "
                      "file that you select becomes one layer of the scene. Select the two "
                      "required files first. The list 'Scene layers' shows what the scene "
                      "will have.")
        self.add_conditional_project_button(
            "Fill empty fields from this diff",
            lambda p: p is not None and getattr(p, "kind", None) == "diff",
            self._fill_from_project)

        # -- 1. Scene files ----------------------------------------------------
        self._add_heading("1. Scene files")

        self.add_file_field("baseline", "Baseline .ply (required):", [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "baseline", lambda: pm.list_side_candidates(self.pipeline.project, "baseline"))
        self.register_auto_default(
            "baseline", lambda p: _suggested_export_input(p, "baseline"), fill_now=True)

        change_edit = self.add_file_field(
            "change", "Change layer .ply (required):", [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "change", lambda: pm.list_eligible_inputs(self.pipeline, "export"))
        self.register_auto_default(
            "change", lambda p: _suggested_export_input(p, "change"), fill_now=True)

        self.add_file_field("flagged", "Flagged points .ply (optional):",
                            [("PLY files", "*.ply")], default=_default_flagged(pipeline))
        self.register_auto_default("flagged", _default_flagged)
        self.add_project_picker_button(
            "flagged", lambda: pm.list_eligible_inputs(self.pipeline, "surface"))

        self.add_file_field("clusters", "Damage sites (.json, optional):",
                            [("Cluster summary", "*.clusters.json"), ("JSON files", "*.json")],
                            default=_default_clusters_json(pipeline))
        self.register_auto_default("clusters", _default_clusters_json)
        change_edit.textChanged.connect(self._fill_clusters_from_change)

        self.add_file_field("detail", "Damage detail .ply (optional):", [("PLY files", "*.ply")])

        self._scene_summary = QLabel()
        self._scene_summary.setWordWrap(True)
        self._scene_summary.setTextFormat(Qt.RichText)
        self.form.addWidget(self._scene_summary)

        # -- Make the damage detail file (optional, closed by default) ---------
        self._detail_toggle = QPushButton()
        self._detail_toggle.setCheckable(True)
        self._detail_toggle.toggled.connect(self._on_detail_toggled)
        self.form.addWidget(self._detail_toggle)
        self.detail_section = self.begin_section()
        self.add_hint("This step makes the file for the 'Damage detail' field. Click the "
                      "button when the three fields are correct.")
        self.add_file_field(
            "damage_detail_flagged", "Classify output .ply (Stage 6):", [("PLY files", "*.ply")],
            default=_default_detail_flagged(pipeline))
        self.register_auto_default("damage_detail_flagged", _default_detail_flagged)
        self.add_project_picker_button(
            "damage_detail_flagged", lambda: pm.list_eligible_inputs(self.pipeline, "surface"))
        self._detail_status = QLabel()
        self._detail_status.setWordWrap(True)
        self._detail_status.setStyleSheet("font-size: 8pt;")
        self.form.addWidget(self._detail_status)
        self.add_file_field(
            "damage_detail_comparison", "New scan (comparison) .ply:", [("PLY files", "*.ply")],
            default=_default_detail_comparison(pipeline))
        self.register_auto_default("damage_detail_comparison", _default_detail_comparison)
        self.add_project_picker_button(
            "damage_detail_comparison",
            lambda: pm.list_side_candidates(self.pipeline.project,
                                            self.pipeline.entry["comparison"]))
        radius_default = _default_damage_detail_radius(pipeline)
        self.add_length_field("damage_detail_radius", "Radius (cm):", default_m=radius_default,
                              min_cm=0.1, max_cm=200)
        self.register_auto_default("damage_detail_radius", _default_damage_detail_radius)
        # Update 29: the explanations are in "?" popups (what it does, what the
        # options do, when to change it).
        self.add_help(
            "baseline",
            "Layer 'Baseline': the room, in grey. It comes from Stage 3 (Cleanup) or "
            "Stage 4 (Segment) of the baseline scan.",
            when="Change it to use another baseline file. The same file is correct for "
                 "every diff.")
        self.add_help(
            "change",
            "Layer 'ChangeHighlight': the change, in colour. It comes from Stage 7 "
            "(Surface) of this diff. You can also use the Stage 6 (Classify) output. The "
            "file must have the field change_inward or the M3C2 distance.",
            when="Change it to show another change file.")
        self.add_help(
            "flagged",
            "Layer 'FlaggedPoints': only the points of the damage. It comes from Stage 6 "
            "(Classify) of this diff, when it ran with 'Keep all points'. The application "
            "fills this field in that case.",
            effects=[
                ("Set", "the scene has the layer."),
                ("Empty", "the scene is made without the layer."),
            ],
            when="The change layer is a Stage 7 surface of all points. Then this layer "
                 "shows the damage points alone.")
        self.add_help(
            "clusters",
            "Layer 'DamageSites': one see-through box for each damage site (red = added, "
            "blue = removed, purple = hole, teal = unmatched). It comes from the "
            ".clusters.json file that Stage 6 saves next to its output. The application "
            "fills this field from the Classify output of this diff. Each box keeps its "
            "point count and sizes as 'delta:' properties.",
            effects=[
                ("Set", "the scene has the boxes."),
                ("Empty", "the scene is made without the boxes."),
            ],
            when="Change it to use the file of another Classify run.")
        self.add_help(
            "detail",
            "Layer 'DamageDetail': the points of the new scan at the damage, coloured by "
            "change type. It comes from the box 'Make the damage detail file' below. The "
            "button in that box fills this field.",
            effects=[
                ("Set", "the scene has the layer."),
                ("Empty", "the scene is made without the layer."),
            ],
            when="You want to see the real points at the damage sites. You can also select "
                 "a file that you made before.")
        self.add_help(
            "damage_detail_flagged",
            "The Classify output (Stage 6). It gives the places of the damage. The script "
            "takes the points of the new scan that are near these places. Each point gets "
            "the change type of the nearest place: added, removed, hole or unmatched. The "
            "change type is the best value for damage, because it has the same meaning "
            "on every wall.",
            when="Change it to use another Classify run. The file must have the "
                 "change_type field (Stage 6 with 'Find holes').")
        self.add_help(
            "damage_detail_comparison",
            "The new scan (the comparison cloud). The script takes the real points for the "
            "detail file from it.",
            when="Change it to use another scan. In project mode the box fills from the "
                 "pipeline.")
        self.add_help(
            "damage_detail_radius",
            "The distance from each damaged place in which the script takes points of the "
            "new scan. The distance is measured from the place in the baseline. The "
            "pre-filled value is the Normal scale of Stage 5.",
            effects=[
                ("Smaller", "fewer points. Material that moved farther is not found."),
                ("Larger", "more points. Points of nearby surfaces can come in."),
            ],
            when="Use a radius that is larger than the largest change.")

        extract_btn = QPushButton("Extract Damage Detail...")
        extract_btn.clicked.connect(self._extract_damage_detail)
        self.form.addWidget(extract_btn)
        self.end_section()

        # -- 2. Output ---------------------------------------------------------
        self._add_heading("2. Output")
        output_default = self.resolve_project_output_default(pipeline, "export", ".usd")
        self.add_save_field("output", "Output .usd:", default_ext=".usd", default=output_default)
        self.register_auto_default(
            "output", lambda p: self.resolve_project_output_default(p, "export", ".usd"))

        self.add_help(
            "output",
            "The USD file that Stage 8 writes.",
            when="Change it to save under another name. In project mode the name comes "
                 "from the pipeline.")

        # Settings that normally stay at their default values (closed block).
        self.begin_advanced("Advanced")
        default_script = str(SCRIPTS_DIR / "usd_export.py")
        self.add_file_field("script", "USD export script (.py):", [("Python files", "*.py")],
                             default=default_script)

        # Update 28: Surface labels and .usdz are in Advanced.
        self.add_file_field("surfaces", "Surface labels .ply (optional):",
                            [("PLY files", "*.ply")], default=_default_surfaces(pipeline))
        self.register_auto_default("surfaces", _default_surfaces)
        self.add_project_picker_button(
            "surfaces", lambda: pm.list_side_candidates(self.pipeline.project,
                                                        self.pipeline.entry["reference"]))

        self.add_checkbox("package_usdz", "Also package as .usdz")

        self.add_checkbox(
            "downsample", "Downsample point cloud layers (reduces file size / viewer load)")
        self.add_length_field("voxel_size", "Voxel size (cm):", min_cm=0.1, max_cm=100)

        self.add_help(
            "script",
            "The Python script that writes the USD file.",
            when="Change it only to use a changed copy of the script.")
        self.add_help(
            "surfaces",
            "Adds the name of the surface (for example 'Site 03 - wall_2') to each damage "
            "site. It needs 'Damage sites'. It comes from Stage 4 (Segment) of the "
            "reference scan (<name>_envelope_filtered.ply or <name>_classified.ply). The "
            "names come from the manifest.json in the same folder. The application fills "
            "this field with the reference cloud that Stage 5 used.",
            effects=[
                ("Set", "each damage site has a surface name."),
                ("Empty", "the damage sites have no surface name."),
            ],
            when="You want surface names on the damage sites.")
        self.add_help(
            "package_usdz",
            "Also writes a .usdz package next to the .usd file, with the same name.",
            effects=[
                ("On", "you get a .usdz file. Most web, AR and mobile USD viewers accept "
                       "only .usdz."),
                ("Off", "only the .usd file is written."),
            ],
            when="You want to look at the result without Omniverse.")
        self.add_help(
            "downsample",
            "Keeps one representative point for each voxel cell in the point cloud layers. "
            "This removes near-duplicate points from overlapping scan passes. It does not "
            "change the mesh layers.",
            effects=[
                ("On", "the file is smaller and the viewer loads faster."),
                ("Off", "all points stay."),
            ],
            when="The file is too large or the viewer is slow. Use point_spacing.py to "
                 "see the point spacing of your data before you choose a voxel size.")
        self.add_help(
            "voxel_size",
            "The size of the voxel cell for 'Downsample'. It is not used when the box is "
            "off.",
            effects=[
                ("Smaller", "more points stay. The file is larger."),
                ("Larger", "fewer points stay. The file is smaller, but small features can "
                           "be lost."),
            ],
            when="Keep the voxel size smaller than the smallest feature that you must see.")

        self.end_advanced()
        self.keep_field_order("script", "baseline", "change")

        self.form.addStretch(1)

        # Live lists. The summary and the status line follow the fields.
        for key in ("baseline", "change", "flagged", "clusters", "surfaces", "detail"):
            self.fields[key].widget.textChanged.connect(self._update_scene_summary)
        for key in ("damage_detail_flagged", "damage_detail_radius"):
            self.fields[key].widget.textChanged.connect(self._update_detail_status)
        self.fields["detail"].widget.textChanged.connect(self._open_detail_box_if_filled)
        self.detail_section.setVisible(False)
        self._on_detail_toggled(False)
        self._update_scene_summary()
        self._update_detail_status()

    # -- layout helpers ---------------------------------------------------------

    def _on_detail_toggled(self, opened):
        """Opens or closes the box 'Make the damage detail file'."""
        self.detail_section.setVisible(bool(opened))
        self._detail_toggle.setText(
            "Hide: make the damage detail file" if opened else
            "Show: make the damage detail file (optional)")

    def _open_detail_box_if_filled(self, *_args):
        """A path in 'Damage detail' (typed, or made by the button) opens the box."""
        if self.fields["detail"].get().strip() and not self._detail_toggle.isChecked():
            self._detail_toggle.setChecked(True)

    # -- live lists -------------------------------------------------------------

    def _layer_states(self):
        """[(layer name, text, colour)] for the summary list, from the fields."""
        def path(key):
            return self.fields[key].get().strip()

        def state(key, required):
            value = path(key)
            if not value:
                return ("MISSING", RED) if required else ("not used", GREY)
            if not Path(value).is_file():
                return ("FILE NOT FOUND", RED)
            return ("OK", GREEN)

        rows = []
        for key in ("baseline", "change"):
            text, colour = state(key, True)
            rows.append((LAYER_NAMES[key], text, colour))

        text, colour = state("flagged", False)
        if text == "OK" and path("change") and \
                self._same_file(path("flagged"), path("change")):
            text, colour = "not used (same file as the change layer)", GREY
        rows.append((LAYER_NAMES["flagged"], text, colour))

        text, colour = state("clusters", False)
        rows.append((LAYER_NAMES["clusters"], text, colour))

        text, colour = state("surfaces", False)
        if text == "OK" and not path("clusters"):
            text, colour = "not used (it needs 'Damage sites')", GREY
        rows.append((LAYER_NAMES["surfaces"], text, colour))

        text, colour = state("detail", False)
        if text == "OK":
            has_type = core.flagged_has_change_type(path("detail")) if core else None
            if has_type is True:
                text = "OK - coloured by change type"
            elif has_type is False:
                text = "OK - coloured by the magnitude (the file has no change type)"
        rows.append((LAYER_NAMES["detail"], text, colour))
        return rows

    @staticmethod
    def _same_file(a, b):
        try:
            return Path(a).resolve() == Path(b).resolve()
        except OSError:
            return False

    def _update_scene_summary(self, *_args):
        rows = self._layer_states()
        lines = "".join(
            f"<tr><td style='padding-right:12px'>{html.escape(name)}</td>"
            f"<td style='color:{colour}'>{html.escape(text)}</td></tr>"
            for name, text, colour in rows)
        self._scene_summary.setText(
            "<b>Scene layers</b> (what Run will make)"
            f"<table style='font-size:8pt'>{lines}</table>")

    def _update_detail_status(self, *_args):
        """The line under 'Classify output .ply': does the file have the change
        type, and is the radius larger than the largest change."""
        value = self.fields["damage_detail_flagged"].get().strip()
        text, colour = "", GREY
        if value and core is not None:
            has_type = core.flagged_has_change_type(value)
            if has_type is True:
                text, colour = ("Change type: found. The detail points get the change type "
                                "of the nearest flagged point.", GREEN)
            elif has_type is False:
                text, colour = ("Change type: NOT found. Run Stage 6 with 'Find holes' to "
                                "get it. Without it, the detail points get the M3C2 "
                                "distance (the magnitude) only.", RED)
            elif Path(value).is_file():
                text, colour = "The header of this file cannot be read.", RED
            largest = core.largest_change_in_classify(value)
            if largest is not None:
                try:
                    radius = float(self.fields["damage_detail_radius"].get())
                except ValueError:
                    radius = None
                if radius is not None and radius < largest:
                    text += (f" The largest change is {largest * 100:.1f} cm and the radius "
                             f"is {radius * 100:.1f} cm: make the radius larger.")
                    colour = RED
        self._detail_status.setText(text)
        self._detail_status.setStyleSheet(f"font-size: 8pt; color: {colour};")

    # -- buttons ----------------------------------------------------------------

    def _fill_from_project(self, pipeline):
        """'Fill empty fields from this diff': puts the files that the project
        records into the EMPTY fields Baseline and Change layer, and runs the
        automatic fill of the optional fields. A field that has a file is not
        changed. The user selects the files with 'Choose from project...' as
        before; this button only saves the clicks."""
        if core is None or pipeline is None:
            return
        suggestion = core.suggest_export_inputs(pipeline)
        filled, kept, missing = [], [], []
        for key, name in (("baseline", "Baseline"), ("change", "Change layer")):
            if self.fields[key].get().strip():
                kept.append(name)
            elif suggestion[key]:
                self.fields[key].set(suggestion[key])
                filled.append(f"{name}: {suggestion[key + '_from']}")
            else:
                missing.append(name)
        self._refresh_auto_defaults(only_empty=True)
        lines = []
        if filled:
            lines.append("Filled:\n  " + "\n  ".join(filled))
        if kept:
            lines.append("Not changed (the field has a file): " + ", ".join(kept))
        if missing:
            lines.append("No file found in the project for: " + ", ".join(missing)
                         + ".\nRun the earlier stage, or use 'Choose from project...' "
                         "or Browse.")
        if not lines:
            lines.append("Nothing to fill.")
        QMessageBox.information(self, "Fill empty fields from this diff", "\n\n".join(lines))

    def _fill_clusters_from_change(self, change_path):
        """If the Change input is a Classify output with a cluster summary
        next to it, and 'Damage sites' is empty, fill it in - covers
        manual mode, where there is no recorded Classify output."""
        if self.fields["clusters"].get().strip() or core is None:
            return
        change_path = change_path.strip()
        if not change_path:
            return
        candidate = core.classify_clusters_path(change_path)
        if candidate.is_file():
            self.fields["clusters"].set(str(candidate))

    def _build_run(self):
        """Real Run - matches open_export_dialog()'s build() in
        pipeline_applet.py, with the same correction as Stage 5: baseline
        and change are read directly from their fields unconditionally
        (see module docstring for why). active_pipeline still flows
        through to build_export_command() and finish_info, purely for
        tracking."""
        script = self.require("script", "USD export script")
        baseline = self.require_existing_file("baseline", "Baseline .ply")
        change = self.require("change", "Change layer .ply")
        detail = self.fields["detail"].get().strip() or None
        if detail and not Path(detail).is_file():
            raise ValueError(f"'Damage detail .ply' points at a file that does not exist:\n"
                             f"{detail}\n\nClear the field to export without this layer.")
        clusters = self.fields["clusters"].get().strip() or None
        if clusters and not Path(clusters).is_file():
            raise ValueError(f"'Damage sites (.clusters.json)' points at a file that does "
                             f"not exist:\n{clusters}\n\nClear the field to export without "
                             f"markers.")
        flagged = self.fields["flagged"].get().strip() or None
        if flagged and not Path(flagged).is_file():
            raise ValueError(f"'Flagged points' points at a file that does not exist:\n"
                             f"{flagged}\n\nClear the field to export without this layer.")
        flagged_same_as_change = bool(flagged) and \
            Path(flagged).resolve() == Path(change).resolve()
        if flagged_same_as_change:
            flagged = None  # the change layer already shows these points
        surfaces = self.fields["surfaces"].get().strip() or None
        if surfaces and not Path(surfaces).is_file():
            raise ValueError(f"'Surface labels' points at a file that does not exist:\n"
                             f"{surfaces}\n\nClear the field to export without surface "
                             f"labels.")
        output = self.require("output", "Output .usd")
        package_usdz = self.fields["package_usdz"].get()
        voxel_size = None
        if self.fields["downsample"].get():
            try:
                voxel_size = float(self.fields["voxel_size"].get())
            except ValueError:
                raise ValueError("Voxel size must be a number in cm when downsampling is "
                                 "checked.")

        detail_has_type = (core.flagged_has_change_type(detail)
                           if (detail and core is not None) else None)

        active_pipeline = self.get_active_pipeline_for_run()
        cmd = core.build_export_command(script, baseline, change, output,
                                         package_usdz=package_usdz, detail_ply=detail,
                                         voxel_size=voxel_size, clusters_json=clusters,
                                         surfaces_ply=surfaces, flagged_ply=flagged,
                                         pipeline=active_pipeline)
        finish_info = {"pipeline": active_pipeline, "stage_name": "export", "output": output}

        report = (
            "=== SUMMARY ===\n"
            f"Baseline used: {baseline}\n"
            f"Change-highlight used: {change}\n"
            + (f"Damage detail used: {detail}"
               + (" (coloured by change type)\n" if detail_has_type is True else
                  " (coloured by the magnitude: the file has no change type)\n"
                  if detail_has_type is False else "\n")
               if detail else "No damage detail layer.\n")
            + (f"Flagged points from: {flagged}\n" if flagged else
               "Flagged points not used: the file is the same as the change input.\n"
               if flagged_same_as_change else "")
            + (f"Damage sites from: {clusters}\n" if clusters else
               "No damage site markers.\n")
            + (f"Surface labels from: {surfaces}\n" if (clusters and surfaces) else
               "Surface labels not used: there are no damage sites to label.\n"
               if surfaces else "")
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
               + ("coloured by change type: red = added, blue = removed, purple = hole, "
                  "teal = unmatched" if detail_has_type is True else
                  "in the same colors as ChangeHighlight")
               if detail else "")
            + (". .../FlaggedPoints shows only the flagged points; hide "
               "ChangeHighlight in the Stage panel to see only the damage" if flagged else "")
            + (". .../DamageSites has one see-through box per damage site - select a "
               "Site_NN prim to see its point count and magnitudes under 'Raw USD "
               "Properties'"
               + (", and the surface it is on (delta:surface)" if surfaces else "")
               + ". " if clusters else ". ")
            + "The scene is in metres with Z up. "
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
            QMessageBox.critical(self, "No Classify file",
                                 "Enter a valid 'Classify output .ply (Stage 6)' file first.")
            return
        if not comparison or not Path(comparison).exists():
            QMessageBox.critical(self, "No new scan file",
                                 "Enter a valid 'New scan (comparison) .ply' file first.")
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

        has_type = core.flagged_has_change_type(flagged)
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
                + ("Each point has the change type of the nearest flagged point. The "
                   "USD export colours the layer by it.\n\n" if has_type is True else
                   "The Classify file has no change type, so each point has the M3C2 "
                   "distance only. Run Stage 6 with 'Find holes' to get the change "
                   "type.\n\n" if has_type is False else "")
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
