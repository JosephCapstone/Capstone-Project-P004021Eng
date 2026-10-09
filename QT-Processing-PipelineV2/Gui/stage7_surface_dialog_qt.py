#!/usr/bin/env python3
"""
Stage 7: Surface - fields only
=================================
Defines Stage 7's fields. A diff-bound stage (pipeline.kind == "diff"),
same group as Stages 5-6.

One method: Blender (Geometry Nodes). Runs scripts/blender_surface.py
inside Blender (background, no window) against a .blend file that holds
the recipe as a Geometry Nodes modifier - by default
configs/ExtraDownsampling.blend (stray-point removal, Points to Volume,
Volume to Mesh). See pipeline_core.build_blender_surface_command() and
blender_surface.py's own docstring. Needs Blender 5.2 or newer; the
panel finds it via pipeline_core.find_blender_executable() and shows
whether it was found.

Update 8 (2026-10-09): Poisson and Ball Pivoting (Open3D,
surface_reconstruction.py) were removed from this panel - in checks on
real data the Blender method was clearly better. The script and
pipeline_core.build_surface_command() are kept (the Tkinter app still
uses them), so the methods can come back here if needed.

Same layout as the other stages: script, input, output, then each
parameter with its default value in the field and a hint below it. The
three recipe fields show the values saved in ExtraDownsampling.blend
(RECIPE_DEFAULTS) and are always passed to Blender with --set, so the
value on the page is the value used. test_blender_surface.py checks
that RECIPE_DEFAULTS still match the .blend file. A blank field keeps
the value saved in the .blend file (for a different recipe that does not
have these inputs). They show cm and pass metres, like every other small
length in this app.

Carry fields (update 9): a checkable list, filled from the header of the
input .ply each time the Input field changes
(pipeline_core.carryable_ply_fields() - reads only the header, so it is
fast for any file size). scalar_M3C2_distance and cluster_id are checked
by default when the input has them; a field you check or clear stays that
way when the input changes. Every checked field is carried onto the mesh
(one --carry-field each).

report is a plain string, not a deferred callable - blender_surface.py
writes directly to the requested Output path, so there is nothing to
resolve after the run.

Run standalone:
    pip install PySide6
    python stage7_surface_dialog_qt.py
"""
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QListWidget, QListWidgetItem

from qt_stage_base import QStageDialog, QStagePanel, SCRIPTS_DIR, CONFIGS_DIR, fmt_cm

try:
    import project_manager as pm
except ImportError:
    pm = None

try:
    import pipeline_core as core
except ImportError:
    core = None


DEFAULT_BLEND_FILE = CONFIGS_DIR / "ExtraDownsampling.blend"
DEFAULT_BLENDER_SCRIPT = SCRIPTS_DIR / "blender_surface.py"
# Checked by default when the input has them: the M3C2 distance (Stage 8
# colours the mesh by it) and the damage site of each point (Stage 6).
DEFAULT_CARRY_FIELDS = ["scalar_M3C2_distance", "cluster_id"]

# Values saved in configs/ExtraDownsampling.blend, in metres. The fields
# start with these. test_blender_surface.py checks them against the file.
RECIPE_DEFAULTS = {
    "Mesh/Voxel Size": 0.025,
    "Mesh/Radius": 0.015,
    "Cleaning/Delete Further": 0.02,
}

# (field key, node input path in the .blend file, name shown in messages)
BLENDER_OVERRIDES = [
    ("blender_voxel_size", "Mesh/Voxel Size", "Voxel size"),
    ("blender_radius", "Mesh/Radius", "Volume radius"),
    ("blender_delete_further", "Cleaning/Delete Further", "Delete further"),
]

METHOD_LABEL = "Blender (Geometry Nodes)"


def _looks_like_m3c2_distance(name):
    lowered = name.lower()
    return "m3c2" in lowered and "distance" in lowered and "uncertain" not in lowered


class CarryFieldsRef:
    """The 'Carry fields' list: one checkable row per field of the input
    .ply. get() returns the checked names as 'a, b' (the same string form
    the other field refs use); set('a, b') checks exactly those names.

    self.wanted remembers the selection, so it survives a change of input:
    a field that the new input does not have is not shown, and it is
    checked again if a later input has it."""

    def __init__(self, list_widget, status_label):
        self.widget = list_widget
        self.status_label = status_label
        self.wanted = list(DEFAULT_CARRY_FIELDS)
        self.user_changed = False
        self._filling = False
        list_widget.itemChanged.connect(self._on_item_changed)

    def available(self):
        return [self.widget.item(i).text() for i in range(self.widget.count())]

    def get(self):
        return ", ".join(self.widget.item(i).text() for i in range(self.widget.count())
                         if self.widget.item(i).checkState() == Qt.Checked)

    def set(self, value):
        self.wanted = core.split_field_names(value) if core is not None else \
            [v.strip() for v in str(value or "").split(",") if v.strip()]
        self.user_changed = True
        self._apply_checks()

    def fill(self, fields):
        """fields: the input's carryable field names, or None when the
        input cannot be read."""
        self._filling = True
        self.widget.clear()
        for name in fields or []:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.widget.addItem(item)
        self._filling = False
        self._apply_checks()
        if fields is None:
            self.status_label.setText("Select an input .ply to see its fields.")
        elif not fields:
            self.status_label.setText("The input has no extra fields - nothing to carry.")
        else:
            self.status_label.setText(
                f"{len(fields)} field(s) in the input. Each checked field is copied onto "
                f"the mesh.")

    def _apply_checks(self):
        self._filling = True
        for i in range(self.widget.count()):
            item = self.widget.item(i)
            name = item.text()
            checked = name in self.wanted or (
                not self.user_changed and _looks_like_m3c2_distance(name))
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self._filling = False

    def _on_item_changed(self, item):
        if self._filling:
            return
        self.user_changed = True
        name = item.text()
        if item.checkState() == Qt.Checked and name not in self.wanted:
            self.wanted.append(name)
        elif item.checkState() != Qt.Checked and name in self.wanted:
            self.wanted.remove(name)


class Stage7SurfaceFieldsMixin:

    def _build_surface_fields(self, pipeline=None):
        self.add_pipeline_label(pipeline)

        self.add_file_field("script", "Blender script (.py):", [("Python files", "*.py")],
                            default=str(DEFAULT_BLENDER_SCRIPT))

        input_edit = self.add_file_field("input", "Classified change cloud .ply:",
                                         [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "input", lambda: pm.list_eligible_inputs(self.pipeline, "surface"))
        self.add_hint(
            "Usually the Stage 6 (Classify) output. With 'Keep all points' in Stage 6, the "
            "mesh covers the full surface, and Stage 8 shows the flagged points as a "
            "separate layer.")

        output_default = self.resolve_project_output_default(pipeline, "surface", ".ply")
        self.add_save_field("output", "Output mesh .ply:", default_ext=".ply",
                            default=output_default)
        self.register_auto_default(
            "output", lambda p: self.resolve_project_output_default(p, "surface", ".ply"))

        found = core.find_blender_executable() if core is not None else None
        exe_edit = self.add_file_field(
            "blender_exe", "Blender program (blender.exe):",
            [("Blender", "blender.exe blender Blender"), ("All files", "*.*")],
            default=found or "")
        self.blender_status_label = QLabel()
        self.blender_status_label.setWordWrap(True)
        self.form.addWidget(self.blender_status_label)
        exe_edit.textChanged.connect(self._update_blender_status)
        self._update_blender_status()
        self.add_hint(
            "Blender 5.2 or newer. The app finds Blender in its default installation folder "
            "or in the Steam folder. If Blender is in a different folder, click Browse... "
            "and select blender.exe.")

        self.add_file_field(
            "blend_file", ".blend file (recipe):", [("Blender files", "*.blend")],
            default=str(DEFAULT_BLEND_FILE) if DEFAULT_BLEND_FILE.exists() else "")
        self.add_hint(
            "The recipe: one object with a Geometry Nodes modifier. The recipe removes stray "
            "points, makes a volume around the remaining points, and makes the surface of "
            "that volume. The app puts the input points into the recipe object, runs the "
            "modifier one time, and saves the result. The app does not change the .blend "
            "file.")
        self.add_text_field("blender_object", "Object name (optional):")
        self.add_hint(
            "Leave blank to use the only object in the .blend file that has a Geometry "
            "Nodes modifier.")

        self.add_length_field("blender_voxel_size", "Voxel size (cm):",
                              default_m=str(RECIPE_DEFAULTS["Mesh/Voxel Size"]),
                              min_cm=0.1, max_cm=50)
        self.add_hint(
            "Default 2.5 cm. The size of the volume cells. A smaller value gives more "
            "detail, a slower run and a larger file. A larger value can close the surface "
            "over small damage.")

        self.add_length_field("blender_radius", "Volume radius (cm):",
                              default_m=str(RECIPE_DEFAULTS["Mesh/Radius"]),
                              min_cm=0.1, max_cm=50)
        self.add_hint(
            "Default 1.5 cm. The size of the sphere around each point. Increase it to close "
            "gaps between points. Decrease it to keep the surface nearer to the points, but "
            "holes can occur. In a test on real data, 2 cm moved the surface away from the "
            "points: 11% of the points were more than 5 cm from the mesh (1.5 cm: almost "
            "none).")

        self.add_length_field("blender_delete_further", "Delete further (cm):",
                              default_m=str(RECIPE_DEFAULTS["Cleaning/Delete Further"]),
                              min_cm=0.1, max_cm=50)
        self.add_hint(
            "Default 2 cm. Before the recipe makes the surface, it removes each point that "
            "has no neighbour nearer than this distance. Increase it to keep more isolated "
            "points.")
        self.add_hint(
            "The three defaults are the values saved in ExtraDownsampling.blend. The app "
            "always sends the values on this page to Blender. If you use a different .blend "
            "file that does not have these inputs, clear the three fields: a blank field "
            "keeps the value saved in the .blend file. The Terminal tab shows the values "
            "that Blender used.")

        row = self._row("Carry fields:")
        carry_list = QListWidget()
        carry_list.setMaximumHeight(110)
        row.addWidget(carry_list, 1)
        self.form.addLayout(row)
        carry_status = QLabel()
        carry_status.setWordWrap(True)
        carry_status.setStyleSheet("color: #777777; font-size: 8pt;")
        self.form.addWidget(carry_status)
        self.fields["carry_fields"] = CarryFieldsRef(carry_list, carry_status)
        input_edit.textChanged.connect(self._update_carry_fields)
        self._update_carry_fields()
        self.add_hint(
            "The list shows the fields of the input .ply. Each checked field is copied from "
            "the nearest input point onto each mesh vertex. Default: scalar_M3C2_distance "
            "(Stage 8 colours the mesh by it) and cluster_id (the damage site of each point), "
            "when the input has them. You can check more than one field. Clear all fields to "
            "carry nothing. The mesh file keeps each value, but writes each field as a "
            "decimal number (a cluster_id of 3 is written as 3.0). Normals and colours are "
            "not in the list: Blender cannot carry them.")

        self.form.addStretch(1)

    def _update_carry_fields(self, *_args):
        path = self.fields["input"].get().strip()
        fields = core.carryable_ply_fields(path) if (core is not None and path) else None
        self.fields["carry_fields"].fill(fields)

    def _update_blender_status(self):
        path = self.fields["blender_exe"].get().strip()
        if path and Path(path).is_file():
            self.blender_status_label.setText(f"Blender found: {path}")
            self.blender_status_label.setStyleSheet("color: #009955; font-size: 8pt;")
        else:
            self.blender_status_label.setText(
                "Blender not found. Install Blender 5.2 or newer, or click Browse... and "
                "select blender.exe.")
            self.blender_status_label.setStyleSheet("color: #bb0000; font-size: 8pt;")

    # -- Run ----------------------------------------------------------------

    def _build_run(self):
        """Real Run. report is a plain string, not a deferred callable -
        see module docstring."""
        blender_script = self.require_existing_file("script", "Blender script")
        input_ply = self.require("input", "Classified change cloud .ply")
        output = self.require("output", "Output mesh .ply")
        blender_exe = self.fields["blender_exe"].get().strip()
        if not blender_exe or not Path(blender_exe).is_file():
            raise ValueError(
                "Blender program not found"
                + (f":\n{blender_exe}" if blender_exe else ".")
                + "\n\nInstall Blender 5.2 or newer (see pc_setup_guide.md), or click "
                  "Browse... next to 'Blender program' and select blender.exe.")
        blend_file = self.require_existing_file("blend_file", ".blend file")
        object_name = self.fields["blender_object"].get().strip() or None
        carry_fields = core.split_field_names(self.fields["carry_fields"].get())

        overrides = {}
        for key, node_path, human_name in BLENDER_OVERRIDES:
            text = self.fields[key].get().strip()  # metres (LengthFieldRef)
            if not text:
                continue
            try:
                value = float(text)
            except ValueError:
                raise ValueError(f"{human_name} must be a number in cm, or blank to use "
                                 f"the value in the .blend file.")
            if value <= 0:
                raise ValueError(f"{human_name} must be more than 0, or blank.")
            overrides[node_path] = value

        active_pipeline = self.get_active_pipeline_for_run()
        cmd = core.build_blender_surface_command(
            blender_exe, blend_file, blender_script, input_ply, output,
            object_name=object_name, overrides=overrides, carry_field=carry_fields,
            pipeline=active_pipeline)
        finish_info = {"pipeline": active_pipeline, "stage_name": "surface", "output": output}

        def value_text(node_path):
            if node_path not in overrides:
                return "value in the .blend file"
            value = overrides[node_path]
            default = RECIPE_DEFAULTS.get(node_path)
            is_default = default is not None and abs(value - default) < 1e-9
            return fmt_cm(value) + (" (default)" if is_default else " (changed)")

        value_lines = "".join(f"{human_name}: {value_text(node_path)}\n"
                              for _key, node_path, human_name in BLENDER_OVERRIDES)
        report = (
            "=== SUMMARY ===\n"
            f"Input: {input_ply}\n"
            f"Method: {METHOD_LABEL}\n"
            f".blend file: {blend_file}\n"
            + value_lines
            + (f"Carried fields: {', '.join(carry_fields)}\n" if carry_fields else
               "No field carried through.\n")
            + f"Saved to: {output}\n\n"
            "=== NOTE ===\n"
            "The Terminal tab shows the values that Blender used, the vertex and face "
            "counts, and the surface area. A large input (millions of points) can take some "
            "minutes and some GB of memory.\n\n"
            "=== NEXT STEPS ===\n"
            "Open the mesh in CloudCompare or Omniverse to check it visually, then "
            "use it as the Change-highlight input to Stage 8 (Export)."
        )
        return cmd, report, finish_info


class Stage7SurfaceDialog(QStageDialog, Stage7SurfaceFieldsMixin):
    """Standalone popup - run this file directly to open just this
    dialog."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__("Stage 7: Surface", parent, on_output=on_output, on_status=on_status)
        self._build_surface_fields(pipeline=pipeline)


class Stage7SurfacePanel(QStagePanel, Stage7SurfaceFieldsMixin):
    """Embeddable panel - used by pipeline_applet_qt_template.py as a
    page of stageStack."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__(parent, on_output=on_output, on_status=on_status)
        self._build_surface_fields(pipeline=pipeline)


def main():
    app = QApplication(sys.argv)
    dlg = Stage7SurfaceDialog(pipeline=None)
    dlg.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
