#!/usr/bin/env python3
"""
Shared Qt scaffolding for every stage's panel/dialog
=======================================================
Split out of stage1_slam_dialog_qt.py once a second stage (Level)
needed the same machinery - importing shared base classes from a file
named after Stage 1 specifically was going to get confusing fast.

Every stageN_..._dialog_qt.py file (stage1_slam_dialog_qt.py,
stage2_level_dialog_qt.py, and so on) imports from here and defines
only ITS OWN fields, via the same pattern:

    class StageNFooFieldsMixin:
        def _build_foo_fields(self, pipeline=None):
            self.add_pipeline_label(pipeline)
            ... self.add_text_field(...), self.add_radio_choice(...), etc.
            self.add_file_field("input", ...)
            self.add_project_picker_button(
                "input", lambda: pm.list_eligible_inputs(self.pipeline, "foo"))

    class StageNFooDialog(QStageDialog, StageNFooFieldsMixin):
        def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
            super().__init__("Stage N: Foo", parent, on_output=on_output, on_status=on_status)
            self._build_foo_fields(pipeline=pipeline)

    class StageNFooPanel(QStagePanel, StageNFooFieldsMixin):
        def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
            super().__init__(parent, on_output=on_output, on_status=on_status)
            self._build_foo_fields(pipeline=pipeline)

`pipeline` is a project_manager.PipelineHandle (baseline/scan for
Stages 1-4, diff for Stages 5-8) or None for manual mode - same
contract as pipeline_applet.py's open_slam_dialog(parent, run_callback,
pipeline=None) and friends. pipeline_applet_qt_template.py is
responsible for deciding WHICH handle a given stage gets (see its
SOURCE_BOUND_STAGES / DIFF_BOUND_STAGES).

IMPORTANT: add_pipeline_label() and add_project_picker_button() are
called UNCONDITIONALLY now, regardless of whether pipeline is None -
no more `if pipeline is not None:` guard around them. Both create their
widgets either way, just hidden when pipeline is None. This is what
lets a panel opened BEFORE any project exists still gain project-mode
input picking once a project is created/opened afterward - the main
window already calls refresh_project_pipeline() on every cached panel
when the active project/pipeline changes (see
pipeline_applet_qt_template.py's _refresh_pipeline_selectors()); before
this, that call had nothing to reveal, because the widgets were never
created in the first place when pipeline was None at construction
time. Rebuilding the whole panel instead was the other option, but
that would also wipe out anything the user had already typed manually
in the meantime - hidden-until-activated widgets avoid that entirely.

Everything a stage's own fields mixin needs (add_text_field,
add_radio_choice, add_pipeline_label, add_project_picker_button, Run
reporting) lives on StageFieldsMixin below - a stage mixin never needs
to touch Qt layout code, or project_manager.py, directly (aside from
groups_fn lambdas, which do call pm.list_eligible_inputs()/
list_side_candidates() - a stage file imports project_manager itself
for that, the same way pipeline_applet.py's dialog functions do).
"""
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QObject, Signal, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QCheckBox, QRadioButton, QButtonGroup, QComboBox, QWidget, QScrollArea,
    QFileDialog, QMessageBox, QSizePolicy, QTreeWidget, QTreeWidgetItem,
    QGroupBox, QToolButton, QFrame,
)

try:
    import project_manager as pm
except ImportError:
    pm = None

try:
    import pipeline_core as core
except ImportError:
    core = None

# Matches pipeline_applet.py's own SCRIPTS_DIR/CONFIGS_DIR exactly: this
# file lives in gui/ alongside pipeline_applet.py, project_manager.py,
# and pipeline_core.py, with scripts/ and configs/ as SEPARATE sibling
# folders under the project root (gui/../scripts, gui/../configs) - not
# co-located with the Qt files themselves. Every stage file's default
# script/config path (e.g. Stage 1's slam_kiss_icp.py,
# kiss_icp_config_indoor.yaml) should build from these, not from
# Path(__file__).resolve().parent, which would silently point at gui/
# instead.
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


class _FieldsDict(dict):
    """self.fields. Tells the page when a field is added (update 28), so the
    page can connect the preset drop-downs and the green mark to the field
    even when the field is built after the drop-down."""

    def __init__(self, owner):
        super().__init__()
        self._owner = owner

    def __setitem__(self, key, ref):
        super().__setitem__(key, ref)
        try:
            self._owner._field_registered(key, ref)
        except Exception:
            pass


_AUTO_FILL_STYLE = "QLineEdit { background-color: #d8f3d8; color: #000000; }"


def project_input_default(pipeline, stage_name):
    """Update 28. The input file of `stage_name` that the project records for
    `pipeline` (the output of the stage before it), or "" when there is none.
    Never raises. A page registers it with register_auto_default() so that a
    pipeline switch changes the Input box."""
    if pm is None or pipeline is None:
        return ""
    try:
        return str(pm.get_input_for_stage(pipeline, stage_name))
    except Exception:
        return ""


def diff_input_default(pipeline, side):
    """Update 28. side 'reference' or 'comparison': the Stage 5 input file that
    the project records for this diff pipeline, or "" (never raises)."""
    if pm is None or pipeline is None or getattr(pipeline, "kind", None) != "diff":
        return ""
    try:
        return str(pm.get_diff_inputs(pipeline)[f"{side}_path"])
    except Exception:
        return ""


def _looks_like_path(text):
    text = str(text).strip()
    return bool(text) and ("/" in text or "\\" in text)


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
    """QMessageBox.information() with a green tick instead of the
    information icon - for the popups that report a completed step
    (Stage Report after a successful run, and the successful end of a
    helper step: decode, Generate Params File, Extract Damage Detail).
    Called through StageFieldsMixin._show_success(), which looks this
    name up at call time, so tests can replace it."""
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
    """pipeline_core.run_streaming() calls on_line/on_done from a
    background thread it spawns internally - Qt widgets can only be
    touched safely from the thread that created them (the main thread,
    here). Emitting a Qt Signal is thread-safe regardless of which
    thread emits it - Qt automatically queues the connected slot call
    onto the receiving object's own thread - which is what makes this
    safe to use as a bridge, without needing to hand-roll any locking.

    A NEW instance per run, kept alive via self._extra_run_bridges on
    the panel/dialog for the run's duration - a QObject with no other
    Python reference gets garbage-collected, the same concern noted in
    about_dialog_qt.py for the non-modal AboutDialog."""
    line_received = Signal(str)
    finished = Signal(int)
    process_started = Signal(object)


# ---------------------------------------------------------------------------
# Small PipelineHandle helpers - shared between a stage's own pipeline
# label, ProjectFilePicker's tree, and pipeline_applet_qt_template.py's
# pipeline-selector combos.
# ---------------------------------------------------------------------------

def pipeline_label_for(kind, pipeline_id):
    """Short display label built from a raw kind/pipeline_id pair
    (rather than a PipelineHandle) - e.g. 'Baseline',
    'Scan: post-storm_2026-09-01'. Needed by ProjectFilePicker, which
    only has the raw kind/id pairs list_eligible_inputs()/
    list_side_candidates() return in each group dict -
    project_manager.py stays GUI-free by design, so it never builds
    display strings itself. Matches pipeline_applet.py's own
    _pipeline_label_for()."""
    if kind == "baseline":
        return "Baseline"
    if kind == "scan":
        return f"Scan: {pipeline_id}"
    return f"Diff: {pipeline_id}"


def pipeline_label(pipeline):
    """Short display label for a PipelineHandle. Matches
    pipeline_applet.py's own _pipeline_label()."""
    if pipeline is None:
        return "(none selected)"
    return pipeline_label_for(pipeline.kind, pipeline.pipeline_id)


# Display names for a project_manager group dict's "stage_name" - used
# by ProjectFilePicker to label each stage's sub-group in its tree.
# "raw" is the sentinel list_eligible_inputs() uses for a pipeline's
# raw import group (Stage 1 has no earlier stage to group by). Matches
# pipeline_applet.py's own STAGE_DISPLAY_NAMES exactly.
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


# ---------------------------------------------------------------------------
# ProjectFilePicker - the grouped file picker behind every project-mode
# input field. Qt port of pipeline_applet.py's ProjectFilePicker
# (a ttk.Treeview popup) - real, non-clickable group headers via
# QTreeWidget for the same reason the Tkinter version used a Treeview
# instead of a fancier combobox: a plain combo box has no way to make a
# header row unselectable.
# ---------------------------------------------------------------------------

class ProjectFilePicker(QDialog):
    """Popup listing every file a project-mode input field could use,
    grouped by pipeline then by stage.

    groups: the group-dict list returned by
    project_manager.list_eligible_inputs()/list_side_candidates() -
    each dict's "path" values are project-relative; this resolves them
    to absolute paths (via project_manager.get_absolute_path()) before
    calling on_pick(), so on_pick always receives something a stage
    panel's field can use directly, same as Browse... would put there.

    Only a leaf (file) row is pickable; picking one (double-click, or
    Choose) calls on_pick(absolute_path) and closes the popup. Picking
    a pipeline or stage header row and pressing Choose does nothing -
    there is nothing to pick there (enforced via each leaf's
    Qt.UserRole data - header rows carry none)."""

    def __init__(self, parent, project, groups, on_pick, empty_text=None):
        super().__init__(parent)
        self.setWindowTitle("Choose a Project File")
        self.resize(640, 420)
        self._project = project
        self._on_pick = on_pick

        layout = QVBoxLayout(self)

        if not groups:
            label = QLabel(empty_text or
                           "No eligible files yet - run an earlier stage first.")
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

            stage_label = STAGE_DISPLAY_NAMES.get(group["stage_name"], group["stage_name"])
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
        """'roomA_cleanup_003.ply (pass 3, current)' for a numbered
        stage output; 'import.bag (decoded)' for a raw import's
        "note"-carrying entries (see list_eligible_inputs()'s idx==0
        branch) - project_manager.py returns raw sequence/is_current/
        note fields, not a pre-built string (GUI-free by design), so
        building this string is this file's job, matching
        pipeline_applet.py's own tree-row formatting."""
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
    """Wraps any checkable button (QRadioButton/QCheckBox) with a
    SEPARATE word-wrapping QLabel instead of relying on the button's own
    .setText().

    This is the direct Qt analog of the bug that started this whole
    detour: ttk.Radiobutton silently ignores wraplength on the Tcl/Tk
    build in use. QRadioButton and QCheckBox have a different version of
    the same limitation - their own text does not wrap by default,
    regardless of widget width. A common quick fix is passing HTML text
    (button.setText("<html>...</html>")) to turn on Qt's rich-text
    renderer, which does wrap - but that wrap then depends on the
    button's width at layout time, which risks the exact "looks right
    until a resize" failure that caused the Tkinter minsize guessing
    loop. A QLabel's word-wrap is a plain, documented layout behavior
    instead, so this pairs a textless button with a labeled one and
    forwards label clicks to the button, keeping the whole row one
    click target (matching the Tkinter anchor="w" full-row behavior)."""
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


# ---------------------------------------------------------------------------
# Field reference wrappers - give every widget a tkinter-Var-like get()/set()
# so build_command()-style code (dlg.fields["key"].get()) will port over
# later with minimal changes, the same way pipeline_applet.py's own
# build_slam_command() reads dlg.fields["backend"].get().
# ---------------------------------------------------------------------------

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
    """A QLineEdit that shows centimetres. get() returns METRES as a
    string ("" if blank; the raw text if not a number, so the caller's
    own float() raises its normal ValueError). set() takes METRES.

    min_cm/max_cm: plausibility range for _check_length_plausibility().
    Values outside it get a confirmation question before a run - the
    main risk of the cm change is someone typing a metre value (0.05)
    into a cm field, which means 0.5 mm."""

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
            ",".join(format_cm_text(part) for part in text.split(",")) if text else "")

    def implausible_values_cm(self):
        values = [_parse_float(part) for part in self.widget.text().split(",")]
        return [v for v in values
                if v is not None and (v < self.min_cm or v > self.max_cm)]


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
        """Matches the tk `var.trace_add("write", callback)` call sites
        used elsewhere in pipeline_applet.py (e.g. backend switching).
        buttonToggled fires once for the button being unchecked and once
        for the button being checked, so callback() runs twice per
        switch - harmless here since it only recomputes visibility."""
        self.group.buttonToggled.connect(lambda *_args: callback())


# ---------------------------------------------------------------------------
# Shared methods for every stage's fields mixin - row builders, project-
# mode wiring, and Run reporting, none of which are specific to any one
# stage. Expects self.form / self.fields / self._layout_stack /
# self.run_button / self.on_output / self.on_status to already exist,
# which both QStageDialog and QStagePanel set up before any stage's own
# _build_*_fields() runs.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Update 29: the "?" help button. A small round button at the end of a field
# row. A click opens a popup with the explanation of the field in three parts:
# what it does, what the options do to the data, and when to change it.
# ---------------------------------------------------------------------------

HELP_POPUP_WIDTH = 420
_HELP_BUTTON_STYLE = (
    "QToolButton { border: 1px solid #888888; border-radius: 10px; background: #f0f0f0;"
    " color: #000000; font-weight: bold; }"
    "QToolButton:hover { background: #ffffdc; }")
_HELP_POPUP_STYLE = (
    "QFrame#helpPopup { background: #ffffdc; border: 1px solid #767676; }"
    "QLabel { background: transparent; color: #000000; font-size: 9pt; }")


def help_html(what, effects=None, when=None, note=None):
    """The rich text of a help popup. what: one sentence. effects: a list of
    (label, text) pairs, for example ("Smaller (1-2 cm)", "strict ..."). when:
    one sentence for 'Change it when'. note: an optional last line."""
    from html import escape
    parts = [f"<b>What it does:</b> {escape(what)}"]
    for label, text in (effects or []):
        parts.append(f"<b>{escape(label)}:</b> {escape(text)}")
    if when:
        parts.append(f"<b>Change it when:</b> {escape(when)}")
    if note:
        parts.append(f"<i>{escape(note)}</i>")
    return "<br>".join(parts)


class HelpButton(QToolButton):
    """The "?" button. Click: show the popup under the button."""

    def __init__(self, html, parent=None):
        super().__init__(parent)
        self.help_html = html
        self.setText("?")
        self.setFixedSize(20, 20)
        self.setStyleSheet(_HELP_BUTTON_STYLE)
        self.setToolTip("Click for an explanation")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.popup = None
        self.clicked.connect(self.show_popup)

    def show_popup(self):
        if self.popup is None:
            popup = QFrame(self.window(), Qt.Popup)
            popup.setObjectName("helpPopup")
            popup.setStyleSheet(_HELP_POPUP_STYLE)
            layout = QVBoxLayout(popup)
            layout.setContentsMargins(8, 6, 8, 6)
            label = QLabel(self.help_html)
            label.setTextFormat(Qt.RichText)
            label.setWordWrap(True)
            label.setFixedWidth(HELP_POPUP_WIDTH)
            layout.addWidget(label)
            self.popup = popup
        self.popup.adjustSize()
        # Under the button, right edges level; keep the popup on the screen.
        point = self.mapToGlobal(self.rect().bottomRight())
        x = point.x() - self.popup.width()
        y = point.y() + 2
        screen = self.screen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left(), min(x, area.right() - self.popup.width()))
            if y + self.popup.height() > area.bottom():
                y = self.mapToGlobal(self.rect().topRight()).y() - self.popup.height() - 2
        self.popup.move(x, y)
        self.popup.show()


def _find_row_layout(layout, widget):
    """The layout (a row) that holds widget directly. Looks into nested
    layouts and into the layouts of child widgets (a toggle row, an Advanced
    block). None when widget is in no layout under `layout`."""
    for i in range(layout.count()):
        item = layout.itemAt(i)
        sub = item.layout()
        if sub is not None:
            if sub.indexOf(widget) >= 0:
                return sub
            found = _find_row_layout(sub, widget)
            if found is not None:
                return found
        child = item.widget()
        if child is not None and child is not widget and child.layout() is not None:
            if child.layout().indexOf(widget) >= 0:
                return child.layout()
            found = _find_row_layout(child.layout(), widget)
            if found is not None:
                return found
    return None


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

    # -- Basic / Advanced view -----------------------------------------------
    #
    # begin_advanced() ... end_advanced() wraps the settings that normally
    # stay at their default values. The block is CLOSED when the page opens.
    # A closed block only hides the widgets: every field keeps its key in
    # self.fields and keeps its value, so _build_run() reads a hidden field
    # exactly like a shown one. The header shows how many fields in the
    # block differ from the value they had when the block was built, so a
    # changed setting is never hidden without a sign.

    def begin_advanced(self, title="Advanced"):
        if not hasattr(self, "_advanced_sections"):
            self._advanced_sections = []
        box = QWidget()
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(0, 8, 0, 0)
        box_layout.setSpacing(4)
        header = QToolButton()
        header.setCheckable(True)
        header.setChecked(False)
        header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        header.setArrowType(Qt.RightArrow)
        header.setAutoRaise(True)
        header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        header.setStyleSheet("QToolButton { font-weight: bold; text-align: left; }")
        body = QWidget()
        body.setVisible(False)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(16, 0, 0, 0)
        body_layout.setSpacing(4)
        box_layout.addWidget(header)
        box_layout.addWidget(body)
        self.form.addWidget(box)
        info = {
            "box": box, "header": header, "body": body, "title": title,
            "keys_before": set(self.fields.keys()), "keys": [], "baseline": {},
        }
        header.toggled.connect(lambda checked, i=info: self._set_advanced_open(i, checked))
        self._advanced_sections.append(info)
        self._layout_stack.append(self.form)
        self.form = body_layout
        self._advanced_open_info = info
        return info

    def end_advanced(self):
        info = self._advanced_open_info
        self.form = self._layout_stack.pop()
        info["keys"] = [k for k in self.fields.keys() if k not in info["keys_before"]]
        self.reset_advanced_baseline(info)
        body = info["body"]
        for cls, signal in ((QLineEdit, "textChanged"), (QCheckBox, "toggled"),
                            (QRadioButton, "toggled"), (QComboBox, "currentIndexChanged")):
            for widget in body.findChildren(cls):
                getattr(widget, signal).connect(
                    lambda *_a, i=info: self._refresh_advanced_header(i))
        self._refresh_advanced_header(info)
        return info

    def keep_field_order(self, *keys):
        """Put these keys first in self.fields, in this order. A field that
        moved into an Advanced block is built later than before. The order of
        self.fields is part of the page contract (script, input, output
        first), so a page calls this at the end of its builder."""
        first = {k: self.fields[k] for k in keys if k in self.fields}
        rest = {k: v for k, v in self.fields.items() if k not in first}
        self.fields.clear()
        self.fields.update(first)
        self.fields.update(rest)

    def reset_advanced_baseline(self, info=None):
        """Take the current values as the 'default' for the changed count.
        Without an argument, do it for every Advanced block."""
        for item in ([info] if info is not None else getattr(self, "_advanced_sections", [])):
            item["baseline"] = {}
            for key in item["keys"]:
                try:
                    item["baseline"][key] = self.fields[key].get()
                except Exception:
                    pass
            self._refresh_advanced_header(item)

    def _rebase_advanced_key(self, key):
        """A value that the page filled in by itself (an automatic default) is
        not a change by the user: take it as the new default of its block."""
        for info in getattr(self, "_advanced_sections", []):
            if key in info["baseline"]:
                try:
                    info["baseline"][key] = self.fields[key].get()
                except Exception:
                    continue
                self._refresh_advanced_header(info)

    def advanced_changed_count(self, info):
        count = 0
        for key, old in info["baseline"].items():
            try:
                new = self.fields[key].get()
            except Exception:
                continue
            if isinstance(old, str) or isinstance(new, str):
                if not _same_value(old, new):
                    count += 1
            elif new != old:
                count += 1
        return count

    def _refresh_advanced_header(self, info):
        count = self.advanced_changed_count(info)
        text = info["title"]
        if count:
            text += f"  ({count} changed from default)"
        info["header"].setText(text)

    def _set_advanced_open(self, info, is_open):
        info["body"].setVisible(is_open)
        info["header"].setArrowType(Qt.DownArrow if is_open else Qt.RightArrow)
        if info["header"].isChecked() != is_open:
            info["header"].setChecked(is_open)

    def open_advanced(self, open_all=True):
        for info in getattr(self, "_advanced_sections", []):
            self._set_advanced_open(info, bool(open_all))

    def _reveal_advanced_for_error(self, message):
        """Open each closed Advanced block whose field labels appear in an
        error message, so the user can see the field that was refused."""
        text = str(message).lower()
        for info in getattr(self, "_advanced_sections", []):
            if info["header"].isChecked():
                continue
            for label in info["body"].findChildren(QLabel):
                name = label.text().strip().rstrip(":").lower()
                if len(name) > 3 and name in text:
                    self._set_advanced_open(info, True)
                    break

    def begin_columns(self, titles):
        """Starts len(titles) side-by-side QGroupBox columns (a
        QHBoxLayout added to self.form) - a deliberate visual departure
        from every other stage panel's flat vertical form, for a stage
        that's fundamentally about comparing two (or more) things
        rather than describing one (see chat: Stage 5 Diff's Reference/
        Comparison). Returns a list of (group_box, group_layout) pairs,
        left to right in the same order as titles.

        To build a column's fields, push/pop self.form around it the
        same way begin_section()/end_section() do internally:

            (group, layout) = self.begin_columns(["A", "B"])[0]
            self._layout_stack.append(self.form)
            self.form = layout
            self.add_text_field(...)
            self.form = self._layout_stack.pop()

        Every FIELD inside a column still goes through the exact same
        add_file_field()/add_project_picker_button()/etc. calls as any
        other stage - only the visual grouping differs, not the field-
        building mechanics."""
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

    def add_length_field(self, key, label, default_m="", min_cm=0.1, max_cm=500.0,
                         ref_cls=None):
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
        return self.add_length_field(key, label, default_m, min_cm, max_cm,
                                     ref_cls=LengthListFieldRef)

    def _check_length_plausibility(self, keys=None):
        """Returns True to go ahead. Asks for confirmation when a VISIBLE
        cm field holds a value outside its plausible range - most likely
        a metre value typed into a cm field. keys: limit the check to
        these field keys (for a helper button that uses only some
        fields); None checks every length field (the main Run)."""
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
            self, "Check the length values",
            "These values are in centimetres, and they are not usual values:\n\n"
            + "\n".join(problems)
            + "\n\nIf you typed a value in metres, click No and change it to "
              "centimetres (for example, 0.05 m is 5 cm).\n\nRun with these values?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    @staticmethod
    def _label_for_widget(widget):
        """The row label text next to a field widget, for messages."""
        parent_layout = widget.parentWidget().layout() if widget.parentWidget() else None
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
        """stacked=True puts the label on its own line above the field
        instead of beside it - for use inside a narrower column (see
        Stage 5 (Diff)'s side-by-side Reference/Comparison layout),
        where the usual fixed-width label reservation would crowd a
        half-width column much more than a full-width one."""
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
            path, _ = QFileDialog.getOpenFileName(self, label, "", _qt_filter(filetypes))
            if path:
                edit.setText(path)

        browse.clicked.connect(do_browse)
        self.form.addLayout(row)
        self.fields[key] = LineEditRef(edit)
        return edit

    def add_file_or_folder_field(self, key, label, filetypes=None, default="", on_picked=None):
        row = self._row(label)
        edit = QLineEdit(default)
        row.addWidget(edit, 1)
        file_btn = QPushButton("File...")
        folder_btn = QPushButton("Folder...")
        row.addWidget(file_btn, 0)
        row.addWidget(folder_btn, 0)

        def pick_file():
            path, _ = QFileDialog.getOpenFileName(self, label, "", _qt_filter(filetypes))
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
        """A single Browse-to-a-folder field, for a stage whose output
        is a whole folder rather than one file (e.g. Stage 4 (Segment),
        which writes several .ply files plus a manifest.json together -
        PROJECT_SCHEMA_v2.md Section 13.3). Not built on
        add_save_field() - that one is for picking a single file's save
        location, and Qt's save-file dialog has no folder-only mode."""
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

    def add_help(self, target, what, effects=None, when=None, note=None):
        """Adds a "?" help button at the end of the row of a field.
        target: a field key (the row of that field), or a widget in the row.
        what / effects / when / note: see help_html(). Returns the button."""
        widget = target
        if isinstance(target, str):
            row = getattr(self, "_help_rows", {}).get(target)
            if row is None:
                ref = self.fields[target]
                widget = getattr(ref, "widget", None)
                if widget is None:
                    raise ValueError(f"add_help: no row for the field {target!r}")
        else:
            row = None
        if row is None:
            row = _find_row_layout(self.form_root_layout(), widget)
        if row is None or not isinstance(row, QHBoxLayout):
            raise ValueError("add_help: the widget is not in a row of a field")
        button = HelpButton(help_html(what, effects, when, note))
        row.addWidget(button, 0, Qt.AlignTop)
        if not hasattr(self, "_help_buttons"):
            self._help_buttons = []
        self._help_buttons.append(button)
        return button

    def form_root_layout(self):
        """The top layout of the page (self.form can be the layout of an
        open Advanced block while the page is built)."""
        root = self._layout_stack[0] if getattr(self, "_layout_stack", None) else self.form
        return root

    def add_preset_selector(self, label, presets, hint=None):
        """A drop-down of presets. Each preset is (text, {field_key: value}) or,
        for a preset whose value comes from the selected pipeline (update 28),
        (text, {}, resolver): resolver(pipeline) -> (text, {field_key: value}).
        The page calls the resolver again on every pipeline switch, so the text
        and the value follow the pipeline. A resolver that gives no values
        (the pipeline does not know the value) gives a preset that matches
        nothing and writes nothing.

        Update 28: the drop-down follows the fields. It shows the preset whose
        values all equal the values in the boxes, and 'Custom' when no preset
        does (a box that you edit, or a value that a page fills in). A preset
        with no values and no resolver ('Keep the field as it is') is the
        'Custom' entry; if there is none, the drop-down gets its own 'Custom'
        entry at the end."""
        row = self._row(label)
        combo = QComboBox()
        texts = [p[0] for p in presets]
        empty = [i for i, p in enumerate(presets) if not p[1] and len(p) < 3]
        if empty:
            custom_index = empty[0]
        else:
            texts.append("Custom (own value)")
            custom_index = len(texts) - 1
        combo.addItems(texts)
        row.addWidget(combo, 1)
        self.form.addLayout(row)

        record = {
            "combo": combo, "custom": custom_index,
            "values": {i: dict(p[1]) for i, p in enumerate(presets) if p[1]},
            "dynamic": {i: p[2] for i, p in enumerate(presets) if len(p) > 2},
            "keys": {k for p in presets for k in p[1]},
        }

        def on_select(index):
            for field_key, field_value in record["values"].get(index, {}).items():
                self.fields[field_key].set(field_value)

        combo.currentIndexChanged.connect(on_select)
        if not hasattr(self, "_presets"):
            self._presets = []
        self._presets.append(record)
        self._sync_presets()
        if hint:
            self.add_hint(hint)
        return combo

    def _refresh_dynamic_presets(self, record):
        """Ask each resolver of a preset list for its text and values (the
        selected pipeline can have changed)."""
        combo = record["combo"]
        for index, resolver in record["dynamic"].items():
            try:
                text, values = resolver(getattr(self, "pipeline", None))
            except Exception:
                continue
            values = dict(values or {})
            if values:
                record["values"][index] = values
                record["keys"].update(values)
            else:
                record["values"].pop(index, None)
            if combo.itemText(index) != text:
                combo.blockSignals(True)
                combo.setItemText(index, text)
                combo.blockSignals(False)

    def _sync_presets(self):
        """Set each preset drop-down to the preset that matches the boxes,
        or to 'Custom'. The signals of the drop-down are blocked: the sync
        never writes a value into a box."""
        for record in getattr(self, "_presets", []):
            self._refresh_dynamic_presets(record)
            if not all(k in self.fields for k in record["keys"]):
                continue        # a box is not built yet - the page syncs again later
            index = record["custom"]
            for i, values in record["values"].items():
                if all(_same_value(self.fields[k].get(), v) for k, v in values.items()):
                    index = i
                    break
            combo = record["combo"]
            if combo.currentIndex() != index:
                combo.blockSignals(True)
                combo.setCurrentIndex(index)
                combo.blockSignals(False)

    def _field_registered(self, key, ref):
        widget = getattr(ref, "widget", None)
        watched = self.__dict__.setdefault("_watched_fields", set())
        if isinstance(widget, QLineEdit) and key not in watched:
            watched.add(key)
            widget.textChanged.connect(lambda _text, k=key: self._on_field_edited(k))
        self._sync_presets()

    def _on_field_edited(self, key):
        """Any change of a box: an edit by the user ends the green mark of
        that box (a value that the page sets itself is handled in
        _refresh_auto_defaults, which sets the mark after the value)."""
        if not self.__dict__.get("_auto_setting"):
            self._set_auto_highlight(key, False)
        self._sync_presets()

    def _set_auto_highlight(self, key, on):
        field = self.fields.get(key)
        widget = getattr(field, "widget", None)
        if not isinstance(widget, QLineEdit):
            return
        saved = self.__dict__.setdefault("_auto_highlight_saved", {})
        if on:
            saved.setdefault(key, widget.styleSheet())
            widget.setStyleSheet(_AUTO_FILL_STYLE)
        elif key in saved:
            widget.setStyleSheet(saved.pop(key))
        legend = getattr(self, "_highlight_legend", None)
        if legend is not None:
            legend.setVisible(bool(saved))

    def add_radio_choice(self, key, label, options, default=None, hint=None):
        if label:
            label_row = QHBoxLayout()
            label_row.addWidget(QLabel(label), 1)
            self.form.addLayout(label_row)
            if not hasattr(self, "_help_rows"):
                self._help_rows = {}
            self._help_rows[key] = label_row
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
        """Small context line naming which project pipeline this panel
        is acting on - e.g. 'Pipeline: roomA / Baseline'. Purely
        informational: no input resolution, no checkbox. Always
        created; hidden (not omitted) when pipeline is None, so
        refresh_project_pipeline() can reveal it later without
        inserting a new widget into an already-built layout."""
        self.pipeline = pipeline
        label = QLabel()
        label.setStyleSheet("color: #009955; font-weight: bold;")
        label.setWordWrap(True)
        self.form.addWidget(label)
        self._pipeline_label_widget = label
        self._update_pipeline_label_widget()
        legend = QLabel("Green boxes were filled in from the selected pipeline. "
                        "A box that you edit is not green any more.")
        legend.setWordWrap(True)
        legend.setStyleSheet("color: #2e7d32; font-size: 8pt;")
        legend.setVisible(False)
        self.form.addWidget(legend)
        self._highlight_legend = legend
        return label

    def _update_pipeline_label_widget(self):
        if self.pipeline is None:
            self._pipeline_label_widget.hide()
            return
        compartment = self.pipeline.project.data.get("compartment", "?")
        self._pipeline_label_widget.setText(
            f"Pipeline: {compartment} / {pipeline_label(self.pipeline)}")
        self._pipeline_label_widget.show()

    def add_project_picker_button(self, field_key, groups_fn, extra_on_pick=None,
                                  empty_text=None):
        """Adds a "Choose from project..." button below field_key's own
        field - field_key's field must already have been added (via
        add_file_field()/add_file_or_folder_field()) before this call.
        Opens a ProjectFilePicker built from groups_fn() (a zero-
        argument callable, typically
        `lambda: pm.list_eligible_inputs(self.pipeline, "<stage>")` -
        referencing self.pipeline, not a fixed local variable, so both
        a later pipeline SWITCH and a later ACTIVATION are picked up
        automatically on the next click, with nothing here needing to
        rebind the button itself).

        Always created; hidden (not omitted) when self.pipeline is None
        at the time this is called - see add_pipeline_label() and the
        module docstring for why. self.pipeline.project is looked up
        fresh when the button is actually clicked (never None by then,
        since the button is hidden/disabled otherwise), not captured
        here at add-time.

        extra_on_pick: optional callable(absolute_path), run right
        after the field is set - e.g. Stage 1's Source field also runs
        its raw-packet check on whatever gets picked this way, matching
        what Browse-ing to the same kind of path already does.

        Matches pipeline_applet.py's own add_project_picker_button(),
        with one deliberate simplification: that version had a
        below=True/False split so the button could squeeze onto a
        field's own grid row when there was space, and drop to its own
        row only for a crowded one (SLAM's Source, which already has
        File.../Folder...). Qt's flexible QHBoxLayout rows don't lend
        themselves to retroactively appending a widget to an already-
        built row the way Tkinter's fixed grid columns do, and "always
        its own row" reads fine in Qt's vertical flow regardless, so
        this always places it on its own row and drops that split."""
        def on_pick(path):
            self.fields[field_key].set(path)
            if extra_on_pick:
                extra_on_pick(path)

        def open_picker():
            if self.pipeline is None:
                return
            ProjectFilePicker(self, self.pipeline.project, groups_fn(), on_pick,
                              empty_text=empty_text).exec()

        button = QPushButton("Choose from project...")
        button.clicked.connect(open_picker)
        self.form.addWidget(button)
        self._picker_buttons = getattr(self, "_picker_buttons", None) or []
        self._picker_buttons.append(button)
        button.setVisible(self.pipeline is not None)
        return button

    def add_conditional_project_button(self, text, condition_fn, on_click):
        """A button whose visibility depends on the CURRENT
        self.pipeline, re-evaluated by refresh_project_pipeline() on
        every switch/activation - a finer-grained test than
        add_project_picker_button()'s simple "some pipeline is active
        or not". condition_fn(pipeline) -> bool; on_click(pipeline) is
        called when clicked (pipeline is never None by then, since the
        button is hidden otherwise).

        Used by Stage 3 (Cleanup)'s "Use Project Baseline" button,
        which should only show for a SCAN pipeline
        (pipeline.kind == "scan"), not for baseline itself and not in
        manual mode - a plain "pipeline is not None" test isn't
        specific enough there."""
        button = QPushButton(text)
        button.clicked.connect(lambda: on_click(self.pipeline))
        self.form.addWidget(button)
        self._conditional_buttons = getattr(self, "_conditional_buttons", None) or []
        self._conditional_buttons.append((button, condition_fn))
        button.setVisible(condition_fn(self.pipeline))
        return button

    def add_registered_baseline_preset(
            self, field_key, label="Or pick a manual-mode registered baseline:", stacked=False):
        """Adds a preset dropdown of every manual-mode registered
        baseline (baseline_registry.json, via
        pipeline_core.list_compartments()/get_active_baseline()) that
        fills field_key when picked. UNRELATED to project mode - this
        stays available regardless of self.pipeline, matching
        pipeline_applet.py's own StageDialog.add_registered_baseline_preset(),
        used by both Stage 3 (Cleanup)'s "align_to" field and Stage 5
        (Diff)'s "baseline" field.

        stacked=True puts the label above the combo instead of beside
        it, matching add_file_field()'s own stacked= option - same
        narrow-column reason (Stage 5's side-by-side layout).

        Always creates the row (hidden if the registry is currently
        empty), and re-scans the registry - via
        refresh_registered_baselines(), called by the main window every
        time this panel becomes the visible stage (see _show_stage()) -
        rather than reading it once at panel-construction time and
        never again. A registry read only once would go stale for the
        rest of the session the moment a NEW baseline gets registered
        (e.g. via this same stage's own "register_as" field) after this
        panel was already built and cached; Tkinter's version doesn't
        have this problem since it rebuilds the whole dialog fresh on
        every open, so "read once" there still means "read every open" -
        that guarantee doesn't carry over to a cached Qt panel without
        this refresh call."""
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

        self._baseline_preset_widgets = getattr(self, "_baseline_preset_widgets", None) or []
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
        """Re-scans baseline_registry.json and updates every
        add_registered_baseline_preset() widget on this panel. Called
        by the main window's _show_stage() every time this panel
        becomes the visible stage - a no-op on any panel that never
        called add_registered_baseline_preset() in the first place."""
        for container, combo in getattr(self, "_baseline_preset_widgets", []):
            self._refresh_baseline_preset_widget(container, combo)

    def refresh_project_pipeline(self, pipeline):
        """Re-points an ALREADY-BUILT panel at a PipelineHandle -
        covers both a SWITCH (this panel already had a project active,
        now a different one is) and ACTIVATION (this panel was built
        with pipeline=None, before any project existed, and one has
        now been created/opened). Called by the main window's
        _refresh_pipeline_selectors() on every cached panel whenever
        the active project/pipeline changes - see
        pipeline_applet_qt_template.py.

        No-op only for a panel that never called add_pipeline_label()
        at all - not a real case for any stage built via the standard
        _build_*_fields(pipeline=...) pattern, since that always calls
        add_pipeline_label() unconditionally now, even with
        pipeline=None."""
        if getattr(self, "_pipeline_label_widget", None) is None:
            return
        old_key = self._pipeline_key(getattr(self, "pipeline", None))
        self.pipeline = pipeline
        self._update_pipeline_label_widget()
        for button in getattr(self, "_picker_buttons", []):
            button.setVisible(pipeline is not None)
        for button, condition_fn in getattr(self, "_conditional_buttons", []):
            button.setVisible(condition_fn(pipeline))
        self._refresh_auto_defaults(switched=self._pipeline_key(pipeline) != old_key)
        self._sync_presets()

    @staticmethod
    def _pipeline_key(pipeline):
        if pipeline is None:
            return None
        project = getattr(pipeline, "project", None)
        return (str(getattr(project, "root", "")), getattr(pipeline, "kind", None),
                getattr(pipeline, "pipeline_id", None))

    def resolve_project_output_default(self, pipeline, stage_name, extension):
        """Returns this stage's default project-mode Output path (an
        absolute path string, following PROJECT_SCHEMA_v2.md's
        <compartment>_<stage>_<sequence>.<ext> naming), or "" if no
        pipeline is active or the path can't be resolved yet. A
        Plain resolver: each stage registers it with
        register_auto_default() (below), which re-runs it on every
        pipeline switch/activation and after every successful run.

        Why that matters here and not in pipeline_applet.py: the
        Tkinter app rebuilt each dialog on every open, so its one-time
        default was always fresh. This Qt app CACHES each panel, so a
        one-time default went stale - confirmed: Stage 1 built on
        Baseline kept pointing at baseline/01_slam/..._001.ply after
        switching to a scan, which would have written the scan's map
        into the baseline folder (and re-used _001 on every re-run)."""
        if pipeline is None or pm is None:
            return ""
        try:
            return pm.get_absolute_path(
                pipeline.project, pm.get_output_path(pipeline, stage_name, extension))
        except pm.ProjectError:
            return ""

    # -- auto-filled defaults ---------------------------------------------

    def register_auto_default(self, key, resolver, fill_now=False):
        """Marks field `key` as auto-filled: resolver(pipeline) -> str
        (or "" when nothing applies, e.g. manual mode).

        The field is re-filled from resolver:
        - on every pipeline switch/activation (refresh_project_pipeline),
        - after every successful run (so an output path moves on to the
          next sequence number instead of overwriting the last result),
        - at Run time, if the field is empty (so "clear the field and
          press Run" always gives the correct project path).

        A value the user typed or browsed to is not replaced, with one
        exception (update 28): on a pipeline switch, a file path that points
        inside the project (for example a file that was chosen from the old
        pipeline) is replaced, because it belongs to the old pipeline. A path
        outside the project stays. A box that a switch fills in is green.

        fill_now=True fills an empty field from resolver at once (the page
        build); the value is not green."""
        self._auto_defaults = getattr(self, "_auto_defaults", None) or {}
        if fill_now and not self.fields[key].get().strip():
            try:
                value = str(resolver(getattr(self, "pipeline", None)) or "")
            except Exception:
                value = ""
            if value:
                self._auto_setting = True
                try:
                    self.fields[key].set(value)
                finally:
                    self._auto_setting = False
                self._rebase_advanced_key(key)
        self._auto_defaults[key] = {"resolver": resolver,
                                     "last": self.fields[key].get().strip()}

    def _in_project(self, value):
        pipeline = getattr(self, "pipeline", None)
        project = getattr(pipeline, "project", None)
        root = getattr(project, "root", None)
        if root is None or not _looks_like_path(value):
            return False
        try:
            Path(value).resolve().relative_to(Path(root).resolve())
            return True
        except (ValueError, OSError):
            return False

    def _refresh_auto_defaults(self, only_empty=False, switched=False):
        for key, entry in (getattr(self, "_auto_defaults", None) or {}).items():
            current = self.fields[key].get().strip()
            if only_empty and current:
                continue
            if current and not _same_value(current, entry["last"]):
                # The user edited the box. On a pipeline switch, a project
                # path of the old pipeline still has to change.
                if not (switched and self._in_project(current)):
                    continue
            try:
                new_value = entry["resolver"](getattr(self, "pipeline", None)) or ""
            except Exception:
                new_value = ""
            new_value = str(new_value)
            if new_value != current:
                self._auto_setting = True
                try:
                    self.fields[key].set(new_value)
                finally:
                    self._auto_setting = False
                self._rebase_advanced_key(key)
                self._set_auto_highlight(key, bool(switched and new_value))
            elif switched:
                self._set_auto_highlight(key, False)
            entry["last"] = new_value

    # -- project-mode output location check --------------------------------

    # Field keys that hold a stage's own output location. Checked by
    # _check_project_outputs() before any run in project mode.
    PROJECT_OUTPUT_KEYS = ("output", "output_dir")

    def _check_project_outputs(self):
        """Project mode only: every output field must point inside the
        ACTIVE pipeline's own folder (pipeline.root). Raises
        RunCheckError otherwise - BEFORE _build_run() calls a
        build_*_command(), so start_stage() never runs for a run that
        cannot be recorded.

        Two failure modes this blocks, both confirmed in testing:
        - outside the project: project_manager.complete_stage() raised
          ProjectError AFTER the run finished, which left the app locked
          in "Running" with every Run button disabled;
        - inside the project but in ANOTHER pipeline's folder: a scan's
          result lands in the baseline's folder and is recorded against
          the scan."""
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
                    f"Run again.")

    # -- validation helpers for Run --------------------------------------
    # Used by a stage's own _build_run() (see below) - matches
    # pipeline_applet.py's StageDialog.require()/require_existing_file()
    # exactly, including the stale-preset-entry hint in the file-missing
    # error message.

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
                "fix the path or browse to the right file.")
        return val

    def get_active_pipeline_for_run(self):
        """Matches pipeline_applet.py's simplified
        StageDialog.get_active_pipeline_for_run() (post-
        ProjectFilePicker-redesign version) - just the currently
        assigned pipeline, or None in manual mode. No manual-override
        check needed anymore; that whole mechanism was removed - a
        project-mode panel's fields are exactly as authoritative as
        manual mode's always were."""
        return getattr(self, "pipeline", None)

    # -- Run execution (real) -------------------------------------------
    # A stage gets real Run behavior by defining its own _build_run(),
    # matching pipeline_applet.py's build_command_fn contract exactly:
    # a zero-arg callable returning (cmd, report) or
    # (cmd, report, finish_info). cmd is a subprocess argument list
    # (from a pipeline_core.py build_*_command() call). report is a
    # string OR a zero-arg callable (deferred - called only after the
    # run succeeds, so it can inspect files the run just created).
    # finish_info, if given: {"pipeline":..., "stage_name":...,
    # "output":..., "resolve_state": {...} (optional)} - when
    # "pipeline" is a real PipelineHandle, pipeline_core.finish_stage()
    # is called automatically once the run completes. A stage with no
    # _build_run() falls back to the layout-only _stub_run() - stages
    # get wired up for real one at a time without breaking the rest.

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
            self._reveal_advanced_for_error(e)
            QMessageBox.critical(self, getattr(e, "title", "Missing input"), str(e))
            return
        except Exception as e:
            # Anything else (project_manager.ProjectError from
            # start_stage(), an OSError, a bug) - never let it escape a Qt
            # slot silently.
            self._report_output(f"ERROR: could not start the run: {type(e).__name__}: {e}")
            QMessageBox.critical(self, "Could not start the run",
                                 f"{type(e).__name__}: {e}")
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
            "Stop the running process and all processes that it started.")
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
        self._active_run = {"process": None, "cancel": False, "finish_info": finish_info}
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
            self, "Stop the process",
            "Stop the running process?\n\n"
            "The stage is recorded as failed. Output files from this run can be "
            "incomplete.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
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
        """Main window closeEvent(): stops this panel's process and
        records the stage as failed NOW, because the normal on_done
        callback will not arrive once the app exits. Without this, the
        stage stayed "running" in project.json and the child process
        (python.exe / CloudCompare.exe) kept running as an orphan."""
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
                core.finish_stage(info["pipeline"], info["stage_name"], None,
                                  success=False,
                                  error_message="Stopped: the app was closed during the run")
            except Exception:
                pass

    def _run_streaming_command(self, cmd, on_line, on_complete):
        """Generic real-subprocess runner: bridges
        pipeline_core.run_streaming()'s background-thread callbacks onto
        the Qt main thread via a _RunSignalBridge kept alive on self
        until the process exits. Also routes the Popen handle to
        _on_process_started() so Stop can reach it. Callers go through
        _run_real_command() (a stage's main Run) or
        _run_utility_command() (a helper step) - both handle the lock
        and Stop state; this only runs the process.

        on_line(str) is called per output line, on_complete(returncode)
        once the process exits - same shape as
        pipeline_core.run_streaming()'s own on_line/on_done, just made
        thread-safe for Qt."""
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
            on_process=lambda process: bridge.process_started.emit(process))

    def _release_bridge(self, bridge):
        bridges = getattr(self, "_extra_run_bridges", None) or []
        if bridge in bridges:
            bridges.remove(bridge)

    def _run_utility_command(self, cmd, on_complete):
        """Runs a helper subprocess that is NOT a tracked stage run
        (Stage 1's raw-packet decode, Stage 8's Extract Damage Detail)
        with the same protection as a stage run: it engages the
        cross-panel running lock, disables this panel's fields, and can
        be stopped with Stop. Returns False (and runs nothing) if any
        process is already running.

        on_complete(returncode, cancelled) is called on the main
        thread once the process exits."""
        if self._active_run is not None or self._external_lock:
            QMessageBox.information(self, "Process running",
                                    "Wait for the current process to finish.")
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
                QMessageBox.critical(self, "Error after the process finished",
                                     f"{type(e).__name__}: {e}")

        self._run_streaming_command(cmd, self._report_output, done)
        return True

    def _run_real_command(self, cmd, report, finish_info):
        if core is None:
            self._report_output("pipeline_core.py could not be imported - cannot run for real.")
            self._report_status("error", "pipeline_core.py not available")
            return

        self._begin_run(finish_info)
        self._report_output("$ " + " ".join(str(c) for c in cmd))
        self._run_streaming_command(
            cmd,
            on_line=self._report_output,
            on_complete=lambda code: self._on_run_process_done(code, report, finish_info))

    def _on_run_process_done(self, returncode, report, finish_info):
        """Matches pipeline_applet.py's
        StageDialog._on_stage_complete(): resolves a deferred (callable)
        report AFTER the run finishes so it can inspect files the run
        just created, then - if finish_info names a real pipeline -
        calls pipeline_core.finish_stage() to record success/failure
        back onto it. On success, also appends the project-relative
        save path to the report and shows it in a popup.

        Exception-safe by design: the final _report_status() call sits
        in a `finally`, so the main window's running lock ALWAYS
        releases. Confirmed bug before this: a finish_stage() exception
        (e.g. an output path outside the project) skipped that call and
        left every Run button disabled until restart."""
        already_recorded = bool(self._active_run and self._active_run.get("shutdown"))
        cancelled = self._end_run()
        success = (returncode == 0) and not cancelled
        if cancelled:
            self._report_output("[stopped by user]")
        else:
            status = ("finished successfully" if returncode == 0
                      else f"exited with code {returncode}")
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
                    error_message = None if success else f"Exited with code {returncode}"
                core.finish_stage(
                    pipeline, finish_info["stage_name"], output,
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
                self._report_status("error", "the run finished, but the project record failed")
            elif cancelled:
                self._report_status("error", "stopped by user")
            elif success:
                self._report_status("done")
            else:
                self._report_status("error", f"exited with code {returncode}")

        if record_error is not None:
            self._report_output(
                f"ERROR: could not record this run in the project: "
                f"{type(record_error).__name__}: {record_error}")
            QMessageBox.warning(
                self, "Project record failed",
                "The process finished, but the app could not record the result in "
                f"the project.\n\n{type(record_error).__name__}: {record_error}")

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
        """Stubbed Run: reports "running" immediately, then - after a
        short artificial delay, purely so the "running" state is
        actually visible for a moment instead of flashing and
        disappearing within the same click - reports the placeholder
        output and flips to "done". Once a stage's Run calls
        pipeline_core.py's run_streaming() for real, delete the
        QTimer.singleShot() wrapper: a real subprocess supplies its own
        delay, and _report_status("error", ...) plugs in exactly where
        pipeline_applet.py's own run_command() already computes
        "exited with code {returncode}" on a non-zero return."""
        self._report_status("running")
        QTimer.singleShot(800, self._finish_stub_run)

    def _finish_stub_run(self):
        self._report_output(
            "Run is stubbed. This build tests dialog layout only, not pipeline execution.")
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
        self.fields = _FieldsDict(self)
        self._layout_stack = []
        self.on_output = on_output
        self.on_status = on_status
        # A standalone popup closes itself on a successful Run, matching
        # pipeline_applet.py's self.destroy() - unlike QStagePanel, this
        # container has no reason to stay open once its one job is done.
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
    """Embeddable counterpart of QStageDialog - same fields, same
    internal QScrollArea, but a plain QWidget instead of a modal
    QDialog, so it can sit as one page of the main window's
    QStackedWidget (stageStack in pipeline_applet_qt_template.ui) and
    swap in/out when a Stage button is pressed, instead of popping up
    over the window."""

    def __init__(self, parent=None, on_output=None, on_status=None):
        super().__init__(parent)
        self.fields = _FieldsDict(self)
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
