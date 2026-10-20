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

Update 25: two more recipe fields.
  - "Island filter size (vertices)", default 20 (RECIPE_DEFAULTS, saved in
    ExtraDownsampling.blend; 1 = off). The recipe deletes each separate
    mesh piece with fewer vertices than this, EXCEPT a piece that has a
    vertex nearest to a damage-site point (cluster_id >= 0) - so a
    small piece at a damage site is never deleted. An input without
    cluster_id has no such protection; the report says so. Always sent
    with --set, like the three lengths.
  - "Only this change type (optional)": blank (default) = all points; 0
    to 4 = only the input points with that change_type. Sent only when
    filled (the .blend file saves -1 = all types).
On the sim4 surface (984 000 vertices) the island filter at 20 removed
13 342 of 16 336 pieces (bubbles from stray points, 90 % 'no change'
points) and no damage-site vertex. Without the protection, the same
filter removed 13 020 damage-site vertices.

Carry fields (update 9): a checkable list, filled from the header of the
input .ply each time the Input field changes
(pipeline_core.carryable_ply_fields() - reads only the header, so it is
fast for any file size). change_inward, scalar_M3C2_distance and cluster_id
are checked by default when the input has them; a field you check or clear stays that
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

from qt_stage_base import QStageDialog, QStagePanel, SCRIPTS_DIR, CONFIGS_DIR, fmt_cm, project_input_default

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
# Checked by default when the input has them: change_inward (Stage 6 with
# Find holes: the same sign on every wall, and a value on the hole points -
# Stage 8 colours the mesh by it when it is there), the M3C2 distance (Stage 8
# colours the mesh by it otherwise), the damage site of each point (Stage 6) and,
# since update 28, the change type of each point (Stage 6 with Find holes).
DEFAULT_CARRY_FIELDS = ["change_inward", "scalar_M3C2_distance", "cluster_id", "change_type"]

# Values saved in configs/ExtraDownsampling.blend (lengths in metres, the
# island filter size in vertices). The fields start with these.
# test_blender_surface.py checks them against the file.
ISLAND_FILTER_INPUT = "Cleaning/Island Filter Size"
CHANGE_TYPE_INPUT = "Filter/Change Type"
RECIPE_DEFAULTS = {
    "Mesh/Voxel Size": 0.025,
    "Mesh/Radius": 0.015,
    "Cleaning/Delete Further": 0.02,
    ISLAND_FILTER_INPUT: 20,
}

# The length fields: (field key, node input path in the .blend file, name
# shown in messages). The island filter and the change type are not
# lengths - see ISLAND_FILTER_INPUT and CHANGE_TYPE_INPUT.
BLENDER_OVERRIDES = [
    ("blender_voxel_size", "Mesh/Voxel Size", "Voxel size"),
    ("blender_radius", "Mesh/Radius", "Volume radius"),
    ("blender_delete_further", "Cleaning/Delete Further", "Delete further"),
]

# Stage 6 change_type values (m3c2_classify.py --find-holes).
CHANGE_TYPE_NAMES = {0: "no change", 1: "added", 2: "removed", 3: "hole", 4: "unmatched"}

METHOD_LABEL = "Blender (Geometry Nodes)"


def parse_island_filter_size(text):
    """'' -> None (keep the .blend value). Otherwise a whole number of 1 or
    more (1 = off), else ValueError."""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = None
    if value is None or value != int(value) or value < 1:
        raise ValueError("Island filter size must be a whole number of 1 or more "
                         "(1 = off), or blank to use the value in the .blend file.")
    return int(value)


def parse_change_type(text):
    """'' -> None (all points). Otherwise 0 to 4, else ValueError."""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw not in {"0", "1", "2", "3", "4"}:
        raise ValueError("Only this change type must be 0, 1, 2, 3 or 4, or blank for "
                         "all points.")
    return int(raw)


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

        input_edit = self.add_file_field("input", "Classified change cloud .ply:",
                                         [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "input", lambda: pm.list_eligible_inputs(self.pipeline, "surface"))
        self.register_auto_default(
            "input", lambda p: project_input_default(p, "surface"), fill_now=True)

        output_default = self.resolve_project_output_default(pipeline, "surface", ".ply")
        self.add_save_field("output", "Output mesh .ply:", default_ext=".ply",
                            default=output_default)
        self.register_auto_default(
            "output", lambda p: self.resolve_project_output_default(p, "surface", ".ply"))

        # Update 28: the Blender program field is in Advanced (below). This line is
        # outside Advanced and shows only when Blender is NOT found, in red.
        self.blender_status_label = QLabel()
        self.blender_status_label.setWordWrap(True)
        self.form.addWidget(self.blender_status_label)

        self.add_length_field("blender_voxel_size", "Voxel size (cm):",
                              default_m=str(RECIPE_DEFAULTS["Mesh/Voxel Size"]),
                              min_cm=0.1, max_cm=50)

        row = self._row("Carry fields:")
        carry_list = QListWidget()
        carry_list.setMaximumHeight(110)
        # Update 28: the entries fill the width of the box (several columns, then a
        # new row). A scroll bar shows only when the rows do not fit in the height.
        carry_list.setFlow(QListWidget.LeftToRight)
        carry_list.setWrapping(True)
        carry_list.setResizeMode(QListWidget.Adjust)
        carry_list.setMovement(QListWidget.Static)
        carry_list.setSpacing(6)
        carry_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        row.addWidget(carry_list, 1)
        self.form.addLayout(row)
        carry_status = QLabel()
        carry_status.setWordWrap(True)
        carry_status.setStyleSheet("color: #777777; font-size: 8pt;")
        self.form.addWidget(carry_status)
        self.fields["carry_fields"] = CarryFieldsRef(carry_list, carry_status)
        input_edit.textChanged.connect(self._update_carry_fields)
        self._update_carry_fields()

        # Update 29: the explanations are in "?" popups (what it does, what the
        # options do, when to change it).
        self.add_help(
            "input",
            "The point cloud that the mesh is made from. Usually the Stage 6 (Classify) "
            "output.",
            when="Change it to make a mesh from a different cloud. With 'Keep all points' "
                 "in Stage 6, the mesh covers the full surface, and Stage 8 shows the "
                 "flagged points as a separate layer.")
        self.add_help(
            "output",
            "The mesh file that Stage 7 writes.",
            when="Change it to save under another name. In project mode the name comes "
                 "from the pipeline.")
        self.add_help(
            "blender_voxel_size",
            "The size of the cells of the volume that Blender makes around the points. The "
            "mesh is the surface of this volume.",
            effects=[
                ("Smaller", "more detail. The run is slower and the file is larger."),
                ("Larger", "the run is faster. The surface can close over small damage."),
            ],
            when="The mesh is too coarse, or small damage is not in the mesh (lower the "
                 "value). The run is too slow or the file is too large (raise the value).")
        self.add_help(
            carry_list,
            "The fields of the input .ply that are copied to the mesh. Each field that you "
            "check is copied from the nearest input point to each mesh vertex. The list "
            "shows the fields of the input file.",
            effects=[
                ("change_type", "the type of each point: 0 no change, 1 added, 2 removed, "
                                "3 hole, 4 unmatched (Stage 6 with Find holes)."),
                ("change_inward", "the same sign on every wall, and a value on the hole "
                                  "areas (Stage 6 with Find holes). Stage 8 colours the "
                                  "mesh by it when it is there."),
                ("scalar_M3C2_distance", "the M3C2 distance of each point."),
                ("cluster_id", "the damage site of each point."),
            ],
            when="Check the fields that you need in Stage 8. You can check more than one "
                 "field. Clear all fields to carry nothing. The mesh file keeps each "
                 "value, but writes each field as a decimal number (a cluster_id of 3 is "
                 "written as 3.0).",
            note="Normals and colours are not in the list. Blender cannot carry them.")

        # Settings that normally stay at their default values (closed block).
        self.begin_advanced("Advanced")

        # Update 28: the Blender program (path and Browse) is in Advanced.
        found = core.find_blender_executable() if core is not None else None
        exe_edit = self.add_file_field(
            "blender_exe", "Blender program (blender.exe):",
            [("Blender", "blender.exe blender Blender"), ("All files", "*.*")],
            default=found or "")
        exe_edit.textChanged.connect(self._update_blender_status)
        self._update_blender_status()
        self.add_help(
            exe_edit,
            "The Blender program that makes the mesh. Blender 5.2 or newer is necessary.",
            when="The red line above the Voxel size field says that Blender is not found. "
                 "The app looks in the default installation folder and in the Steam "
                 "folder. If Blender is in a different folder, click Browse... and select "
                 "blender.exe.")

        self.add_file_field("script", "Blender script (.py):", [("Python files", "*.py")],
                            default=str(DEFAULT_BLENDER_SCRIPT))
        self.add_help(
            "script",
            "The Python script that Blender runs.",
            when="Change it only to use a changed copy of the script.")

        self.add_file_field(
            "blend_file", ".blend file (recipe):", [("Blender files", "*.blend")],
            default=str(DEFAULT_BLEND_FILE) if DEFAULT_BLEND_FILE.exists() else "")
        self.add_help(
            "blend_file",
            "The recipe: one object with a Geometry Nodes modifier. The recipe removes stray "
            "points, makes a volume around the remaining points, and makes the surface of "
            "that volume. The app puts the input points into the recipe object, runs the "
            "modifier one time, and saves the result. The app does not change the .blend "
            "file.",
            when="Change it to use a different recipe. The four defaults below are the "
                 "values saved in ExtraDownsampling.blend. The app always sends the values "
                 "on this page to Blender. If the .blend file does not have these inputs, "
                 "clear the fields: a blank field keeps the value saved in the .blend "
                 "file. The Terminal tab shows the values that Blender used.")
        self.add_text_field("blender_object", "Object name (optional):")
        self.add_help(
            "blender_object",
            "The name of the recipe object in the .blend file.",
            when="The .blend file has more than one object with a Geometry Nodes "
                 "modifier. Leave the box empty to use the only such object.")

        self.add_length_field("blender_radius", "Volume radius (cm):",
                              default_m=str(RECIPE_DEFAULTS["Mesh/Radius"]),
                              min_cm=0.1, max_cm=50)
        self.add_help(
            "blender_radius",
            "The size of the sphere around each point that makes the volume.",
            effects=[
                ("Smaller", "the surface stays nearer to the points. Holes can occur."),
                ("Larger", "gaps between points close. The surface can move away from "
                           "the points."),
            ],
            when="The mesh has gaps (raise the value), or the mesh is too far from the "
                 "points (lower the value).")

        self.add_length_field("blender_delete_further", "Delete further (cm):",
                              default_m=str(RECIPE_DEFAULTS["Cleaning/Delete Further"]),
                              min_cm=0.1, max_cm=50)
        self.add_help(
            "blender_delete_further",
            "Before the recipe makes the surface, it removes each point that has no "
            "neighbour nearer than this distance.",
            effects=[
                ("Smaller", "more isolated points are removed."),
                ("Larger", "more isolated points stay in the mesh."),
            ],
            when="Real thin parts are missing from the mesh (raise the value), or stray "
                 "points make bubbles (lower the value).")

        self.add_text_field("blender_island_filter", "Island filter size (vertices):",
                            default=str(RECIPE_DEFAULTS[ISLAND_FILTER_INPUT]))
        self.add_help(
            "blender_island_filter",
            "After the recipe makes the surface, it deletes each separate piece of the "
            "mesh that has fewer vertices than this value. These pieces are small bubbles "
            "around stray points near the walls. A piece that touches a damage site (an "
            "input point with cluster_id 0 or more) is always kept, at any size.",
            effects=[
                ("1", "the filter is off."),
                ("Smaller", "fewer pieces are deleted."),
                ("Larger", "more pieces are deleted. A real small piece can go."),
            ],
            when="The mesh has many small bubbles (raise the value), or a small real "
                 "piece is missing (lower the value).",
            note="If the input has no cluster_id, the recipe cannot see the damage sites "
                 "and deletes every small piece.")

        self.add_text_field("blender_change_type", "Only this change type (optional):")
        self.add_help(
            "blender_change_type",
            "Makes the surface only from the input points with this Stage 6 change_type: "
            "0 no change, 1 added, 2 removed, 3 hole, 4 unmatched. The input must have "
            "the change_type field.",
            effects=[
                ("Empty", "all points are used."),
                ("0 to 4", "only the points of this type are used."),
            ],
            when="You need a mesh of one type of change. Added and removed points are "
                 "often scattered, so a surface of one of these types can be many small "
                 "pieces.")

        self.end_advanced()
        self.keep_field_order("script", "input", "output")

        self.form.addStretch(1)

    def _update_carry_fields(self, *_args):
        path = self.fields["input"].get().strip()
        fields = core.carryable_ply_fields(path) if (core is not None and path) else None
        self.fields["carry_fields"].fill(fields)

    def _update_blender_status(self):
        """Red line outside Advanced: only when Blender is not found. When
        Blender is found, the line is hidden (the text is kept for tests)."""
        path = self.fields["blender_exe"].get().strip()
        if path and Path(path).is_file():
            self.blender_status_label.setText(f"Blender found: {path}")
            self.blender_status_label.setStyleSheet("color: #009955; font-size: 8pt;")
            self.blender_status_label.setVisible(False)
        else:
            self.blender_status_label.setText(
                "Blender not found. Open Advanced and click Browse... next to 'Blender "
                "program', or install Blender 5.2 or newer.")
            self.blender_status_label.setStyleSheet(
                "color: #bb0000; font-size: 9pt; font-weight: bold;")
            self.blender_status_label.setVisible(True)

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
                  "Browse... next to 'Blender program (blender.exe)' in Advanced and "
                  "select blender.exe.")
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
        island_size = parse_island_filter_size(self.fields["blender_island_filter"].get())
        if island_size is not None:
            overrides[ISLAND_FILTER_INPUT] = island_size
        change_type = parse_change_type(self.fields["blender_change_type"].get())
        if change_type is not None:
            overrides[CHANGE_TYPE_INPUT] = change_type
        input_fields = (core.read_ply_vertex_fields(input_ply) or []) if core else []
        input_names = {name for name, _type in input_fields}

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
        if island_size is None:
            value_lines += "Island filter: value in the .blend file\n"
        else:
            marker = (" (default)" if island_size == RECIPE_DEFAULTS[ISLAND_FILTER_INPUT]
                      else " (changed)")
            if island_size <= 1:
                value_lines += f"Island filter: off (1){marker}\n"
            elif "cluster_id" in input_names:
                value_lines += (f"Island filter: {island_size} vertices{marker}. Small pieces "
                                f"at a damage site (cluster_id 0 or more) are kept.\n")
            else:
                value_lines += (f"Island filter: {island_size} vertices{marker}.\n"
                                "WARNING: the input has no cluster_id field. The recipe cannot "
                                "see the damage sites, so it also deletes small pieces at "
                                "damage. Set the island filter to 1 to keep every piece.\n")
        if change_type is None:
            value_lines += "Change type: all points\n"
        else:
            value_lines += (f"Change type: only {change_type} "
                            f"({CHANGE_TYPE_NAMES[change_type]})\n")
            if "change_type" not in input_names:
                value_lines += ("NOTE: the input has no change_type field, so the recipe "
                                "uses all points.\n")
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
