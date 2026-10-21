#!/usr/bin/env python3
"""Shared Qt fields, project pickers and processing controls."""

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QObject, Signal, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QCheckBox,
    QRadioButton,
    QButtonGroup,
    QComboBox,
    QWidget,
    QScrollArea,
    QFileDialog,
    QMessageBox,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QGroupBox,
)

try:
    import project_manager as pm
except ImportError:
    pm = None

try:
    import pipeline_core as core
except ImportError:
    core = None

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"


def _same_value(a, b):
    """True when two field strings hold the same value - numerically if
    both parse (a cm field round-trips 0.05 as '0.05' but a resolver may
    give '0.050'), else as plain strings."""
    fa, fb = _parse_float(a), _parse_float(b)
    if fa is not None and fb is not None:
        return abs(fa - fb) <= 1e-12 * max(1.0, abs(fa), abs(fb))
    return str(a).strip() == str(b).strip()


def _tick_pixmap(size=48, device_pixel_ratio=1.0):
    """A green circle with a white tick, drawn with QPainter so it looks
    the same in every Qt style (the built-in style icons differ between
    Windows styles, and QMessageBox has no standard tick icon)."""
    pixmap = QPixmap(int(size * device_pixel_ratio), int(size * device_pixel_ratio))
    pixmap.setDevicePixelRatio(device_pixel_ratio)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#2e7d32"))
    painter.drawEllipse(QRectF(2, 2, size - 4, size - 4))
    pen = QPen(QColor("white"))
    pen.setWidthF(size * 0.1)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    path = QPainterPath()
    path.moveTo(size * 0.28, size * 0.52)
    path.lineTo(size * 0.44, size * 0.67)
    path.lineTo(size * 0.72, size * 0.36)
    painter.drawPath(path)
    painter.end()
    return pixmap


def show_success_message(parent, title, text):
    """QMessageBox.information() with a green tick instead of the information icon - for the popups that report a completed step (Stage Report after a successful run, and the successful end of a helper step: decode,."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    ratio = parent.devicePixelRatioF() if parent is not None else 1.0
    box.setIconPixmap(_tick_pixmap(48, ratio))
    box.setStandardButtons(QMessageBox.Ok)
    box.exec()


class RunCheckError(ValueError):
    """A pre-run check failed for a reason other than a missing field -
    shown with its own dialog title instead of "Missing input"."""

    title = "Check the output path"


class _RunSignalBridge(QObject):
    """pipeline_core.run_streaming() calls on_line/on_done from a background thread it spawns internally - Qt widgets can only be touched safely from the thread that created them (the main thread, here)."""

    line_received = Signal(str)
    finished = Signal(int)
    process_started = Signal(object)


# ---------------------------------------------------------------------------
# Small PipelineHandle helpers - shared between a stage's own pipeline
# label, ProjectFilePicker's tree, and main.py's
# pipeline-selector combos.
# ---------------------------------------------------------------------------


def pipeline_label_for(kind, pipeline_id):
    """Short display label built from a raw kind/pipeline_id pair (rather than a PipelineHandle) - e.g."""
    if kind == "baseline":
        return "Baseline"
    if kind == "scan":
        return f"Scan: {pipeline_id}"
    return f"Diff: {pipeline_id}"


def pipeline_label(pipeline):
    """Short display label for a PipelineHandle. Matches
    the Qt processing app's own _pipeline_label()."""
    if pipeline is None:
        return "(none selected)"
    return pipeline_label_for(pipeline.kind, pipeline.pipeline_id)


STAGE_DISPLAY_NAMES = {
    "raw": "Raw import",
    "slam": "SLAM",
    "level": "Level",
    "cleanup": "Cleanup",
    "segment": "Segment",
    "diff": "Diff",
    "classify": "Classify",
    "surface": "Surface",
    "export": "Export",
}


class ProjectFilePicker(QDialog):
    """Popup listing every file a project-mode input field could use, grouped by pipeline then by stage."""

    def __init__(self, parent, project, groups, on_pick):
        super().__init__(parent)
        self.setWindowTitle("Choose a Project File")
        self.resize(640, 420)
        self._project = project
        self._on_pick = on_pick

        layout = QVBoxLayout(self)

        if not groups:
            label = QLabel("No eligible files yet - run an earlier stage first.")
            label.setWordWrap(True)
            layout.addWidget(label)
            close_btn = QPushButton("Close")
            close_btn.clicked.connect(self.reject)
            layout.addWidget(close_btn)
            return

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemDoubleClicked.connect(lambda item, _col: self._pick_item(item))
        layout.addWidget(self.tree, 1)

        pipeline_nodes = {}
        for group in groups:
            key = (group["pipeline_kind"], group["pipeline_id"])
            pipeline_node = pipeline_nodes.get(key)
            if pipeline_node is None:
                pipeline_node = QTreeWidgetItem([pipeline_label_for(*key)])
                self.tree.addTopLevelItem(pipeline_node)
                pipeline_nodes[key] = pipeline_node

            stage_label = STAGE_DISPLAY_NAMES.get(
                group["stage_name"], group["stage_name"]
            )
            stage_node = QTreeWidgetItem([stage_label])
            pipeline_node.addChild(stage_node)

            for file_info in group["files"]:
                leaf = QTreeWidgetItem([self._file_display_text(file_info)])
                leaf.setData(0, Qt.ItemDataRole.UserRole, file_info["path"])
                stage_node.addChild(leaf)

        self.tree.expandAll()

        button_row = QHBoxLayout()
        choose_btn = QPushButton("Choose")
        choose_btn.clicked.connect(self._on_choose)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(choose_btn)
        button_row.addWidget(cancel_btn)
        layout.addLayout(button_row)

    @staticmethod
    def _file_display_text(file_info):
        """'roomA_cleanup_003.ply (pass 3, current)' for a numbered stage output; 'import.bag (decoded)' for a raw import's "note"-carrying entries (see list_eligible_inputs()'s idx==0 branch) - project_manager.py returns raw."""
        name = Path(file_info["path"]).name
        parts = []
        if file_info.get("sequence") is not None:
            parts.append(f"pass {file_info['sequence']}")
        if file_info.get("is_current"):
            parts.append("current")
        if file_info.get("note"):
            # 'import.bag (decoded)'; a Segment pass's filtered copy shows
            # 'x_envelope_filtered.ply (pass 2, outside-room points removed)'.
            parts.append(file_info["note"])
        return f"{name} ({', '.join(parts)})" if parts else name

    def _on_choose(self):
        item = self.tree.currentItem()
        if item is not None:
            self._pick_item(item)

    def _pick_item(self, item):
        rel_path = item.data(0, Qt.ItemDataRole.UserRole)
        if rel_path is None:
            return  # a pipeline/stage header row - nothing to pick
        absolute_path = pm.get_absolute_path(self._project, rel_path)
        self._on_pick(absolute_path)
        self.accept()


def _wrap_toggle(button, text):
    """Wraps any checkable button (QRadioButton/QCheckBox) with a SEPARATE word-wrapping QLabel instead of relying on the button's own .setText()."""
    button.setText("")
    container = QWidget()
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 2, 0, 2)
    row.setSpacing(6)
    label = QLabel(text)
    label.setWordWrap(True)
    row.addWidget(button, 0, Qt.AlignTop)
    row.addWidget(label, 1)
    label.mousePressEvent = lambda _event: button.click()
    return container


def _qt_filter(filetypes):
    """Tkinter filetypes are (label, pattern) tuples with ';'-separated
    patterns; Qt filters are 'Label (*.ext *.ext2)' strings with
    space-separated patterns. Converts one to the other."""
    if not filetypes:
        return "All files (*.*)"
    parts = [f"{name} ({pattern.replace(';', ' ')})" for name, pattern in filetypes]
    return ";;".join(parts)


class LineEditRef:
    def __init__(self, widget: QLineEdit):
        self.widget = widget

    def get(self):
        return self.widget.text()

    def set(self, value):
        self.widget.setText(str(value))


# ---------------------------------------------------------------------------
# Centimetre display for small lengths (added 2026-09-23)
#
# Rule: the GUI SHOWS centimetres, everything else stays in METRES -
# scripts, KISS-ICP YAML, project.json, the M3C2 params file,
# CloudCompare, USD. The conversion happens in exactly one place: the
# field ref. LengthFieldRef.get() returns METRES (as a string), and
# LengthFieldRef.set() takes METRES. So every stage's _build_run(),
# preset dict, pre-fill and project record keeps working in metres
# unchanged - only the widget text is in cm.
#
# Ranges (Stage 1 min range, map max range) deliberately stay plain
# metre fields - decided in chat: "800 cm" reads worse than "8 m".
# ---------------------------------------------------------------------------


def _parse_float(text):
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def format_cm_text(meters):
    """Metres (number or numeric string) -> centimetre text for a field,
    e.g. 0.25 -> "25", 0.0425 -> "4.25". Non-numeric input is returned
    unchanged (so a bad value stays visible instead of disappearing)."""
    value = _parse_float(meters)
    if value is None:
        return "" if meters is None else str(meters)
    return f"{round(value * 100.0, 6):g}"


def fmt_cm(meters):
    """For reports and labels: 0.25 -> "25 cm". None -> "unknown"."""
    if meters is None:
        return "unknown"
    return f"{format_cm_text(meters)} cm"


def _meters_text_from_cm(text):
    value = _parse_float(text)
    if value is None:
        return str(text)  # blank or not a number - the caller's float() reports it
    return f"{value / 100.0:.10g}"


class LengthFieldRef(LineEditRef):
    """A QLineEdit that shows centimetres."""

    def __init__(self, widget, min_cm=0.1, max_cm=500.0):
        super().__init__(widget)
        self.min_cm = min_cm
        self.max_cm = max_cm

    def get(self):
        text = self.widget.text().strip()
        return _meters_text_from_cm(text) if text else ""

    def set(self, value):
        self.widget.setText(format_cm_text(value))

    def implausible_values_cm(self):
        text = self.widget.text().strip()
        value = _parse_float(text)
        if not text or value is None:
            return []
        return [value] if (value < self.min_cm or value > self.max_cm) else []


class LengthListFieldRef(LengthFieldRef):
    """Comma-separated list of lengths (Stage 7 ball radii). Same
    contract as LengthFieldRef, applied to each item."""

    def get(self):
        text = self.widget.text().strip()
        if not text:
            return ""
        return ",".join(_meters_text_from_cm(part) for part in text.split(","))

    def set(self, value):
        text = str(value or "").strip()
        self.widget.setText(
            ",".join(format_cm_text(part) for part in text.split(",")) if text else ""
        )

    def implausible_values_cm(self):
        values = [_parse_float(part) for part in self.widget.text().split(",")]
        return [
            v for v in values if v is not None and (v < self.min_cm or v > self.max_cm)
        ]


class CheckBoxRef:
    def __init__(self, widget: QCheckBox):
        self.widget = widget

    def get(self):
        return self.widget.isChecked()

    def set(self, value):
        self.widget.setChecked(bool(value))


class RadioGroupRef:
    """Wraps a QButtonGroup so .get()/.set() work on the OPTION VALUE
    (e.g. "kiss_icp"), not the Qt button object - same value-based
    contract as Tkinter's add_radio_choice()."""

    def __init__(self):
        self.group = QButtonGroup()
        self.group.setExclusive(True)
        self._value_by_button = {}
        self._button_by_value = {}

    def add_option(self, button, value):
        self.group.addButton(button)
        self._value_by_button[button] = value
        self._button_by_value[value] = button

    def get(self):
        return self._value_by_button.get(self.group.checkedButton())

    def set(self, value):
        button = self._button_by_value.get(value)
        if button:
            button.setChecked(True)

    def trace_add(self, _mode, callback):
        """Matches the tk `var.trace_add("write", callback)` call sites used elsewhere in the processing app (e.g."""
        self.group.buttonToggled.connect(lambda *_args: callback())


# ---------------------------------------------------------------------------
# Shared methods for every stage's fields mixin - row builders, project-
# mode wiring, and Run reporting, none of which are specific to any one
# stage. Expects self.form / self.fields / self._layout_stack /
# self.run_button / self.on_output / self.on_status to already exist,
# which both QStageDialog and QStagePanel set up before any stage's own
# _build_*_fields() runs.
# ---------------------------------------------------------------------------


class StageFieldsMixin:

    # -- layout -------------------------------------------------------------

    def begin_section(self):
        section = QWidget()
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(4)
        self.form.addWidget(section)
        self._layout_stack.append(self.form)
        self.form = section_layout
        return section

    def end_section(self):
        self.form = self._layout_stack.pop()

    def begin_columns(self, titles):
        """Starts len(titles) side-by-side QGroupBox columns (a QHBoxLayout added to self.form) - a deliberate visual departure from every other stage panel's flat vertical form, for a stage that's fundamentally about comparing."""
        row = QHBoxLayout()
        self.form.addLayout(row)
        columns = []
        for title in titles:
            group = QGroupBox(title)
            group_layout = QVBoxLayout(group)
            row.addWidget(group, 1)
            columns.append((group, group_layout))
        return columns

    def _row(self, label_text):
        row = QHBoxLayout()
        if label_text is not None:
            label = QLabel(label_text)
            label.setMinimumWidth(170)  # UNVERIFIED GUESS - see test notes
            label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            row.addWidget(label, 0)
        return row

    # -- field builders -------------------------------------------------------

    def add_text_field(self, key, label, default=""):
        row = self._row(label)
        edit = QLineEdit(default)
        row.addWidget(edit, 1)
        self.form.addLayout(row)
        self.fields[key] = LineEditRef(edit)
        return edit

    def add_length_field(
        self, key, label, default_m="", min_cm=0.1, max_cm=500.0, ref_cls=None
    ):
        """A length field shown in CENTIMETRES. `label` should say (cm).
        default_m is in METRES, like every value this field's ref
        returns or accepts - see LengthFieldRef."""
        row = self._row(label)
        edit = QLineEdit()
        row.addWidget(edit, 1)
        self.form.addLayout(row)
        ref = (ref_cls or LengthFieldRef)(edit, min_cm=min_cm, max_cm=max_cm)
        ref.set(default_m)
        self.fields[key] = ref
        return edit

    def add_length_list_field(self, key, label, default_m="", min_cm=0.1, max_cm=500.0):
        return self.add_length_field(
            key, label, default_m, min_cm, max_cm, ref_cls=LengthListFieldRef
        )

    def _check_length_plausibility(self, keys=None):
        """Returns True to go ahead."""
        problems = []
        for key, ref in self.fields.items():
            if keys is not None and key not in keys:
                continue
            if not isinstance(ref, LengthFieldRef):
                continue
            if not ref.widget.isVisibleTo(self):
                continue  # e.g. the other SLAM backend's section
            for value in ref.implausible_values_cm():
                label = self._label_for_widget(ref.widget)
                problems.append(f"{label} {value:g} cm")
        if not problems:
            return True
        answer = QMessageBox.question(
            self,
            "Check the length values",
            "These values are in centimetres, and they are not usual values:\n\n"
            + "\n".join(problems)
            + "\n\nIf you typed a value in metres, click No and change it to "
            "centimetres (for example, 0.05 m is 5 cm).\n\nRun with these values?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    @staticmethod
    def _label_for_widget(widget):
        """The row label text next to a field widget, for messages."""
        parent_layout = (
            widget.parentWidget().layout() if widget.parentWidget() else None
        )
        if parent_layout is not None:
            for i in range(parent_layout.count()):
                item = parent_layout.itemAt(i)
                row = item.layout() if item is not None else None
                if row is None or row.indexOf(widget) < 0:
                    continue
                first = row.itemAt(0).widget()
                if isinstance(first, QLabel):
                    return first.text()
        return "Value:"

    def add_file_field(self, key, label, filetypes=None, default="", stacked=False):
        """stacked=True puts the label on its own line above the field instead of beside it - for use inside a narrower column (see Stage 5 (Diff)'s side-by-side Reference/Comparison layout), where the usual fixed-width label."""
        if stacked:
            self.form.addWidget(QLabel(label))
            row = QHBoxLayout()
        else:
            row = self._row(label)
        edit = QLineEdit(default)
        row.addWidget(edit, 1)
        browse = QPushButton("Browse...")
        row.addWidget(browse, 0)

        def do_browse():
            path, _ = QFileDialog.getOpenFileName(
                self, label, "", _qt_filter(filetypes)
            )
            if path:
                edit.setText(path)

        browse.clicked.connect(do_browse)
        self.form.addLayout(row)
        self.fields[key] = LineEditRef(edit)
        return edit

    def add_file_or_folder_field(
        self, key, label, filetypes=None, default="", on_picked=None
    ):
        row = self._row(label)
        edit = QLineEdit(default)
        row.addWidget(edit, 1)
        file_btn = QPushButton("File...")
        folder_btn = QPushButton("Folder...")
        row.addWidget(file_btn, 0)
        row.addWidget(folder_btn, 0)

        def pick_file():
            path, _ = QFileDialog.getOpenFileName(
                self, label, "", _qt_filter(filetypes)
            )
            if path:
                edit.setText(path)
                if on_picked:
                    on_picked(path)

        def pick_folder():
            path = QFileDialog.getExistingDirectory(self, label)
            if path:
                edit.setText(path)
                if on_picked:
                    on_picked(path)

        file_btn.clicked.connect(pick_file)
        folder_btn.clicked.connect(pick_folder)
        self.form.addLayout(row)
        self.fields[key] = LineEditRef(edit)
        return edit

    def add_save_field(self, key, label, default_ext=".ply", default=""):
        row = self._row(label)
        edit = QLineEdit(default)
        row.addWidget(edit, 1)
        browse = QPushButton("Browse...")
        row.addWidget(browse, 0)

        def do_browse():
            path, _ = QFileDialog.getSaveFileName(self, label, "", f"*{default_ext}")
            if path:
                edit.setText(path)

        browse.clicked.connect(do_browse)
        self.form.addLayout(row)
        self.fields[key] = LineEditRef(edit)
        return edit

    def add_folder_field(self, key, label, default=""):
        """A single Browse-to-a-folder field, for a stage whose output is a whole folder rather than one file (e.g."""
        row = self._row(label)
        edit = QLineEdit(default)
        row.addWidget(edit, 1)
        browse = QPushButton("Browse...")
        row.addWidget(browse, 0)

        def do_browse():
            path = QFileDialog.getExistingDirectory(self, label)
            if path:
                edit.setText(path)

        browse.clicked.connect(do_browse)
        self.form.addLayout(row)
        self.fields[key] = LineEditRef(edit)
        return edit

    def add_checkbox(self, key, label, default=False):
        box = QCheckBox()
        box.setChecked(default)
        self.form.addWidget(_wrap_toggle(box, label))
        self.fields[key] = CheckBoxRef(box)
        return box

    def add_hint(self, text):
        """Small muted, wrapped label for inline guidance - matches the
        Tkinter version's role exactly, and QLabel word-wrap here is
        reliable (unlike the button-text case handled by _wrap_toggle)."""
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("color: #777777; font-size: 8pt;")
        self.form.addWidget(label)

    def add_preset_selector(self, label, presets, hint=None):
        row = self._row(label)
        combo = QComboBox()
        combo.addItems([p[0] for p in presets])
        preset_map = {p[0]: p[1] for p in presets}
        row.addWidget(combo, 1)
        self.form.addLayout(row)

        def on_select(index):
            values = preset_map.get(combo.itemText(index))
            if values:
                for field_key, field_value in values.items():
                    self.fields[field_key].set(field_value)

        combo.currentIndexChanged.connect(on_select)
        if hint:
            self.add_hint(hint)
        return combo

    def add_radio_choice(self, key, label, options, default=None, hint=None):
        if label:
            self.form.addWidget(QLabel(label))
        ref = RadioGroupRef()
        for display_label, value in options:
            radio = QRadioButton()
            radio.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.form.addWidget(_wrap_toggle(radio, display_label))
            ref.add_option(radio, value)
        ref.set(default if default is not None else options[0][1])
        self.fields[key] = ref
        if hint:
            self.add_hint(hint)
        return ref

    # -- project mode: pipeline label + "Choose from project..." ------------
    # Both ALWAYS create their widget, hidden if pipeline is None -
    # see module docstring for why (letting a panel opened before any
    # project existed still activate later, without a full rebuild).

    def add_pipeline_label(self, pipeline):
        """Small context line naming which project pipeline this panel is acting on - e.g."""
        self.pipeline = pipeline
        label = QLabel()
        label.setStyleSheet("color: #009955; font-weight: bold;")
        label.setWordWrap(True)
        self.form.addWidget(label)
        self._pipeline_label_widget = label
        self._update_pipeline_label_widget()
        return label

    def _update_pipeline_label_widget(self):
        if self.pipeline is None:
            self._pipeline_label_widget.hide()
            return
        compartment = self.pipeline.project.data.get("compartment", "?")
        self._pipeline_label_widget.setText(
            f"Pipeline: {compartment} / {pipeline_label(self.pipeline)}"
        )
        self._pipeline_label_widget.show()

    def add_project_picker_button(self, field_key, groups_fn, extra_on_pick=None):
        """Adds a "Choose from project..." button below field_key's own field - field_key's field must already have been added (via add_file_field()/add_file_or_folder_field()) before this call."""

        def on_pick(path):
            self.fields[field_key].set(path)
            if extra_on_pick:
                extra_on_pick(path)

        def open_picker():
            if self.pipeline is None:
                return
            ProjectFilePicker(self, self.pipeline.project, groups_fn(), on_pick).exec()

        button = QPushButton("Choose from project...")
        button.clicked.connect(open_picker)
        self.form.addWidget(button)
        self._picker_buttons = getattr(self, "_picker_buttons", None) or []
        self._picker_buttons.append(button)
        button.setVisible(self.pipeline is not None)
        return button

    def add_conditional_project_button(self, text, condition_fn, on_click):
        """A button whose visibility depends on the CURRENT self.pipeline, re-evaluated by refresh_project_pipeline() on every switch/activation - a finer-grained test than add_project_picker_button()'s simple "some pipeline is."""
        button = QPushButton(text)
        button.clicked.connect(lambda: on_click(self.pipeline))
        self.form.addWidget(button)
        self._conditional_buttons = getattr(self, "_conditional_buttons", None) or []
        self._conditional_buttons.append((button, condition_fn))
        button.setVisible(condition_fn(self.pipeline))
        return button

    def add_registered_baseline_preset(
        self,
        field_key,
        label="Or pick a manual-mode registered baseline:",
        stacked=False,
    ):
        """Adds a preset dropdown of every manual-mode registered baseline (baseline_registry.json, via pipeline_core.list_compartments()/get_active_baseline()) that fills field_key when picked."""
        container = QWidget()
        if stacked:
            outer = QVBoxLayout(container)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.addWidget(QLabel(label))
            combo = QComboBox()
            outer.addWidget(combo)
        else:
            row = QHBoxLayout(container)
            row.setContentsMargins(0, 0, 0, 0)
            label_widget = QLabel(label)
            label_widget.setMinimumWidth(170)  # UNVERIFIED GUESS - see test notes
            row.addWidget(label_widget, 0)
            combo = QComboBox()
            row.addWidget(combo, 1)
        self.form.addWidget(container)

        def on_select(index):
            path = combo.itemData(index)
            if path:
                self.fields[field_key].set(path)

        combo.currentIndexChanged.connect(on_select)

        self._baseline_preset_widgets = (
            getattr(self, "_baseline_preset_widgets", None) or []
        )
        self._baseline_preset_widgets.append((container, combo))
        self._refresh_baseline_preset_widget(container, combo)
        return combo

    @staticmethod
    def _refresh_baseline_preset_widget(container, combo):
        if core is None:
            container.hide()
            return
        known_compartments = core.list_compartments()
        if not known_compartments:
            container.hide()
            return
        combo.blockSignals(True)
        combo.clear()
        for name in known_compartments:
            path = core.get_active_baseline(name)
            combo.addItem(f"{name} -> {Path(path).name}", path)
        combo.blockSignals(False)
        container.show()

    def refresh_registered_baselines(self):
        """Re-scans baseline_registry.json and updates every add_registered_baseline_preset() widget on this panel."""
        for container, combo in getattr(self, "_baseline_preset_widgets", []):
            self._refresh_baseline_preset_widget(container, combo)

    def refresh_project_pipeline(self, pipeline):
        """Re-points an ALREADY-BUILT panel at a PipelineHandle - covers both a SWITCH (this panel already had a project active, now a different one is) and ACTIVATION (this panel was built with pipeline=None, before any."""
        if getattr(self, "_pipeline_label_widget", None) is None:
            return
        self.pipeline = pipeline
        self._update_pipeline_label_widget()
        for button in getattr(self, "_picker_buttons", []):
            button.setVisible(pipeline is not None)
        for button, condition_fn in getattr(self, "_conditional_buttons", []):
            button.setVisible(condition_fn(pipeline))
        self._refresh_auto_defaults()

    def resolve_project_output_default(self, pipeline, stage_name, extension):
        """Returns this stage's default project-mode Output path (an absolute path string, following PROJECT_SCHEMA_v2.md's <compartment>_<stage>_<sequence>.<ext> naming), or "" if no pipeline is active or the path can't be."""
        if pipeline is None or pm is None:
            return ""
        try:
            return pm.get_absolute_path(
                pipeline.project, pm.get_output_path(pipeline, stage_name, extension)
            )
        except pm.ProjectError:
            return ""

    # -- auto-filled defaults ---------------------------------------------

    def register_auto_default(self, key, resolver):
        """Marks field `key` as auto-filled: resolver(pipeline) -> str (or "" when nothing applies, e.g."""
        self._auto_defaults = getattr(self, "_auto_defaults", None) or {}
        self._auto_defaults[key] = {
            "resolver": resolver,
            "last": self.fields[key].get().strip(),
        }

    def _refresh_auto_defaults(self, only_empty=False):
        for key, entry in (getattr(self, "_auto_defaults", None) or {}).items():
            current = self.fields[key].get().strip()
            if only_empty and current:
                continue
            if current and not _same_value(current, entry["last"]):
                continue  # user-edited - keep it
            try:
                new_value = entry["resolver"](getattr(self, "pipeline", None)) or ""
            except Exception:
                new_value = ""
            new_value = str(new_value)
            if new_value != current:
                self.fields[key].set(new_value)
            entry["last"] = new_value

    # -- project-mode output location check --------------------------------

    # Field keys that hold a stage's own output location. Checked by
    # _check_project_outputs() before any run in project mode.
    PROJECT_OUTPUT_KEYS = ("output", "output_dir")

    def _check_project_outputs(self):
        """Project mode only: every output field must point inside the ACTIVE pipeline's own folder (pipeline.root)."""
        pipeline = getattr(self, "pipeline", None)
        if pipeline is None:
            return
        root = Path(pipeline.root).resolve()
        for key in self.PROJECT_OUTPUT_KEYS:
            ref = self.fields.get(key)
            if not isinstance(ref, LineEditRef):
                continue
            value = ref.get().strip()
            if not value:
                continue
            try:
                Path(value).resolve().relative_to(root)
            except ValueError:
                raise RunCheckError(
                    f"The output path is not in the folder of the active pipeline "
                    f"({pipeline_label(pipeline)}).\n\n"
                    f"Output path:\n{value}\n\n"
                    f"Project runs must save in this folder:\n{root}\n\n"
                    f"To use the correct project path, clear the output field and click "
                    f"Run again."
                )

    def require(self, key, human_name):
        val = self.fields[key].get().strip()
        if not val:
            raise ValueError(f"'{human_name}' is required.")
        return val

    def require_existing_file(self, key, human_name):
        val = self.require(key, human_name)
        if not Path(val).exists():
            raise ValueError(
                f"'{human_name}' points at a file that doesn't exist:\n{val}\n\n"
                "If you picked this from a preset dropdown, that entry may be stale "
                "(the file was moved/renamed/deleted since it was registered). Either "
                "fix the path or browse to the right file."
            )
        return val

    def get_active_pipeline_for_run(self):
        """Get active pipeline for run."""
        return getattr(self, "pipeline", None)

    def _on_run_clicked(self):
        if getattr(self, "_active_run", None) is not None:
            return
        build_fn = getattr(self, "_build_run", None)
        if build_fn is None:
            self._stub_run()
            return
        self._refresh_auto_defaults(only_empty=True)
        if not self._check_length_plausibility():
            return
        try:
            self._check_project_outputs()
            result = build_fn()
        except ValueError as e:
            QMessageBox.critical(self, getattr(e, "title", "Missing input"), str(e))
            return
        except Exception as e:
            # Anything else (project_manager.ProjectError from
            # start_stage(), an OSError, a bug) - never let it escape a Qt
            # slot silently.
            self._report_output(
                f"ERROR: could not start the run: {type(e).__name__}: {e}"
            )
            QMessageBox.critical(
                self, "Could not start the run", f"{type(e).__name__}: {e}"
            )
            return
        if len(result) == 3:
            cmd, report, finish_info = result
        else:
            cmd, report = result
            finish_info = None
        self._run_real_command(cmd, report, finish_info)

    # -- run lifecycle: lock, Stop, shutdown -------------------------------

    def _show_success(self, title, text):
        show_success_message(self, title, text)

    def _build_run_buttons(self, outer_layout):
        """Run + Stop row at the bottom of both containers. Stop is
        enabled only while THIS panel has a process running."""
        row = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.run_button.clicked.connect(self._on_run_clicked)
        row.addWidget(self.run_button, 1)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip(
            "Stop the running process and all processes that it started."
        )
        self.stop_button.clicked.connect(self._on_stop_clicked)
        row.addWidget(self.stop_button, 0)
        outer_layout.addLayout(row)
        self._active_run = None
        self._external_lock = False

    def set_run_locked(self, locked, tooltip=""):
        """Called by the main window's cross-panel running lock. Blocks
        this panel's Run button AND any helper subprocess
        (_run_utility_command) while another panel is running."""
        self._external_lock = locked
        self.run_button.setEnabled(not locked)
        self.run_button.setToolTip(tooltip if locked else "")

    def _begin_run(self, finish_info=None):
        self._active_run = {
            "process": None,
            "cancel": False,
            "finish_info": finish_info,
        }
        self._report_status("running")
        # Qt cascades the disabled state to every field inside
        # self.content. Run/Stop live outside self.content.
        self.content.setEnabled(False)
        self.stop_button.setEnabled(True)

    def _end_run(self):
        """Returns True if the user asked for a stop during this run."""
        run = self._active_run
        self._active_run = None
        self.content.setEnabled(True)
        self.stop_button.setEnabled(False)
        return bool(run and run["cancel"])

    def _on_process_started(self, process):
        run = self._active_run
        if run is None:
            return
        run["process"] = process
        if run["cancel"] and core is not None:
            # Stop was clicked before the process handle arrived.
            core.terminate_process_tree(process)

    def _on_stop_clicked(self):
        if self._active_run is None:
            return
        answer = QMessageBox.question(
            self,
            "Stop the process",
            "Stop the running process?\n\n"
            "The stage is recorded as failed. Output files from this run can be "
            "incomplete.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.request_stop()

    def request_stop(self):
        run = self._active_run
        if run is None or run["cancel"]:
            return
        run["cancel"] = True
        self.stop_button.setEnabled(False)
        self._report_output("[stop requested]")
        if run["process"] is not None and core is not None:
            core.terminate_process_tree(run["process"])

    def abort_for_shutdown(self):
        """Main window closeEvent(): stops this panel's process and records the stage as failed NOW, because the normal on_done callback will not arrive once the app exits."""
        run = self._active_run
        if run is None:
            return
        run["cancel"] = True
        run["shutdown"] = True  # the late on_done must not overwrite this record
        if run["process"] is not None and core is not None:
            core.terminate_process_tree(run["process"])
        info = run["finish_info"]
        if info and info.get("pipeline") is not None and core is not None:
            try:
                core.finish_stage(
                    info["pipeline"],
                    info["stage_name"],
                    None,
                    success=False,
                    error_message="Stopped: the app was closed during the run",
                )
            except Exception:
                pass

    def _run_streaming_command(self, cmd, on_line, on_complete):
        """Generic real-subprocess runner: bridges pipeline_core.run_streaming()'s background-thread callbacks onto the Qt main thread via a _RunSignalBridge kept alive on self until the process exits."""
        if core is None:
            on_line("pipeline_core.py could not be imported - cannot run for real.")
            on_complete(-1)
            return

        bridge = _RunSignalBridge()
        self._extra_run_bridges = getattr(self, "_extra_run_bridges", None) or []
        self._extra_run_bridges.append(bridge)  # kept alive - see _RunSignalBridge's
        # own docstring for why this matters
        bridge.line_received.connect(on_line)
        bridge.process_started.connect(self._on_process_started)
        bridge.finished.connect(on_complete)
        bridge.finished.connect(lambda _code, b=bridge: self._release_bridge(b))
        core.run_streaming(
            cmd,
            lambda line: bridge.line_received.emit(line),
            lambda code: bridge.finished.emit(code),
            on_process=lambda process: bridge.process_started.emit(process),
        )

    def _release_bridge(self, bridge):
        bridges = getattr(self, "_extra_run_bridges", None) or []
        if bridge in bridges:
            bridges.remove(bridge)

    def _run_utility_command(self, cmd, on_complete):
        """Runs a helper subprocess that is NOT a tracked stage run (Stage 1's raw-packet decode, Stage 8's Extract Damage Detail) with the same protection as a stage run: it engages the cross-panel running lock, disables this."""
        if self._active_run is not None or self._external_lock:
            QMessageBox.information(
                self, "Process running", "Wait for the current process to finish."
            )
            return False
        self._begin_run(None)
        self._report_output("$ " + " ".join(str(c) for c in cmd))

        def done(returncode):
            cancelled = self._end_run()
            if cancelled:
                self._report_output("[stopped by user]")
                self._report_status("error", "stopped by user")
            elif returncode == 0:
                self._report_output("[finished successfully]")
                self._report_status("done")
            else:
                self._report_output(f"[exited with code {returncode}]")
                self._report_status("error", f"exited with code {returncode}")
            try:
                on_complete(returncode, cancelled)
            except Exception as e:
                QMessageBox.critical(
                    self, "Error after the process finished", f"{type(e).__name__}: {e}"
                )

        self._run_streaming_command(cmd, self._report_output, done)
        return True

    def _run_real_command(self, cmd, report, finish_info):
        if core is None:
            self._report_output(
                "pipeline_core.py could not be imported - cannot run for real."
            )
            self._report_status("error", "pipeline_core.py not available")
            return

        self._begin_run(finish_info)
        self._report_output("$ " + " ".join(str(c) for c in cmd))
        self._run_streaming_command(
            cmd,
            on_line=self._report_output,
            on_complete=lambda code: self._on_run_process_done(
                code, report, finish_info
            ),
        )

    def _on_run_process_done(self, returncode, report, finish_info):
        """On run process done."""
        already_recorded = bool(self._active_run and self._active_run.get("shutdown"))
        cancelled = self._end_run()
        success = (returncode == 0) and not cancelled
        if cancelled:
            self._report_output("[stopped by user]")
        else:
            status = (
                "finished successfully"
                if returncode == 0
                else f"exited with code {returncode}"
            )
            self._report_output(f"[{status}]")

        pipeline = finish_info.get("pipeline") if finish_info else None
        if already_recorded:
            pipeline = None  # abort_for_shutdown() already recorded the failure
        record_error = None
        try:
            if success and callable(report):
                try:
                    report = report()
                except Exception as e:
                    report = f"(Could not build the full report: {e})"

            if pipeline is not None and core is not None:
                output = finish_info.get("output")
                extra_fields = None
                log_path = None
                resolve_state = finish_info.get("resolve_state")
                if resolve_state:
                    output = resolve_state.get("output") or output
                    extra_fields = resolve_state.get("extra_fields")
                    log_path = resolve_state.get("log_path")
                if cancelled:
                    error_message = "Stopped by user"
                else:
                    error_message = (
                        None if success else f"Exited with code {returncode}"
                    )
                core.finish_stage(
                    pipeline,
                    finish_info["stage_name"],
                    output,
                    success=success,
                    error_message=error_message,
                    extra_fields=extra_fields,
                    log_path=log_path,
                )
                if success and isinstance(report, str):
                    try:
                        rel = str(Path(output).relative_to(pipeline.project.root))
                    except (ValueError, TypeError):
                        rel = str(output)
                    report = (report or "") + f"\n\nSaved to project: {rel}"
        except Exception as e:
            record_error = e
        finally:
            if record_error is not None:
                self._report_status(
                    "error", "the run finished, but the project record failed"
                )
            elif cancelled:
                self._report_status("error", "stopped by user")
            elif success:
                self._report_status("done")
            else:
                self._report_status("error", f"exited with code {returncode}")

        if record_error is not None:
            self._report_output(
                f"ERROR: could not record this run in the project: "
                f"{type(record_error).__name__}: {record_error}"
            )
            QMessageBox.warning(
                self,
                "Project record failed",
                "The process finished, but the app could not record the result in "
                f"the project.\n\n{type(record_error).__name__}: {record_error}",
            )

        if success:
            self._refresh_auto_defaults()
            if record_error is None and isinstance(report, str) and report:
                self._show_success("Stage Report", report)
            if record_error is None and getattr(self, "_close_on_run_success", False):
                self.close()

    # -- Run stubbing / reporting (layout-only fallback) ---------------
    # Generic across every stage - a stage's own fields mixin never needs
    # to redefine these, only connect self.run_button to _stub_run() (the
    # base __init__s below already do even that).

    def _stub_run(self):
        """Stubbed Run: reports "running" immediately, then - after a short artificial delay, purely so the "running" state is actually visible for a moment instead of flashing and disappearing within the same click - reports."""
        self._report_status("running")
        QTimer.singleShot(800, self._finish_stub_run)

    def _finish_stub_run(self):
        self._report_output(
            "Run is stubbed. This build tests dialog layout only, not pipeline execution."
        )
        self._report_status("done")

    def _report_output(self, text):
        if self.on_output:
            self.on_output(text)
        else:
            QMessageBox.information(self, "Layout prototype", text)

    def _report_status(self, status, detail=""):
        if self.on_status:
            self.on_status(status, detail)


# ---------------------------------------------------------------------------
# Two containers - a modal popup, and an embeddable panel. Both set up
# self.form / self.fields / self._layout_stack / self.run_button /
# self.on_output / self.on_status the same way, and both wire
# self.run_button straight to _stub_run() - so a stage's own
# _build_*_fields() never has to remember to do that itself.
# ---------------------------------------------------------------------------


class QStageDialog(QDialog, StageFieldsMixin):
    """Modal popup form - open a stage's dialog on top of the window.
    Useful for testing one stage's own layout in isolation (run that
    stage's file directly), independent of the main window's
    stageStack."""

    def __init__(self, title, parent=None, on_output=None, on_status=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 720)  # UNVERIFIED GUESS - see test notes
        self.fields = {}
        self._layout_stack = []
        self.on_output = on_output
        self.on_status = on_status
        self._close_on_run_success = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll, 1)

        self.content = QWidget()
        scroll.setWidget(self.content)
        self.form = QVBoxLayout(self.content)
        self.form.setContentsMargins(12, 12, 12, 12)
        self.form.setSpacing(4)

        self._build_run_buttons(outer)


class QStagePanel(QWidget, StageFieldsMixin):
    """Embeddable counterpart of QStageDialog - same fields, same internal QScrollArea, but a plain QWidget instead of a modal QDialog, so it can sit as one page of the main window's QStackedWidget (stageStack in main.ui)."""

    def __init__(self, parent=None, on_output=None, on_status=None):
        super().__init__(parent)
        self.fields = {}
        self._layout_stack = []
        self.on_output = on_output
        self.on_status = on_status
        # An embedded panel stays open on a successful Run, unlike
        # QStageDialog - it's a cached, reused page of the main
        # window's stageStack, not a one-shot popup. Closing it would
        # destroy the cached widget and lose every field's state.
        self._close_on_run_success = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll, 1)

        self.content = QWidget()
        scroll.setWidget(self.content)
        self.form = QVBoxLayout(self.content)
        self.form.setContentsMargins(12, 12, 12, 12)
        self.form.setSpacing(4)

        self._build_run_buttons(outer)
