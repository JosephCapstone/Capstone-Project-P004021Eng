#!/usr/bin/env python3
"""
Project creation dialogs
===========================
Qt ports of pipeline_applet.py's NewProjectDialog, NewScanDialog, and
NewDiffDialog - same fields, same project_manager.py calls
(pm.create_project / pm.add_scan / pm.add_diff), same source-type
auto-detection (_guess_source_type below, ported directly). These are
plain QDialogs, not built on qt_stage_base.py's StageFieldsMixin - they
are one-off forms, not a stage's reusable field set.

pipeline_applet_qt_template.py owns creating these and reacting to
on_created; these dialogs never touch the main window directly.

New Project and New Scan copy the raw source into the project. For a
multi-GB bag that copy takes minutes, so it runs on a worker thread
behind a busy dialog (_run_blocking_task) - on the GUI thread, Windows
showed the whole app as "Not Responding". Any failure (ProjectError,
or an OSError such as a full disk) is reported, and a partly created
folder is named so it can be deleted before a retry (project_manager
refuses to reuse an existing folder).

Update 23 - import of a point cloud: if the source is a .ply file, New
Project and New Scan do not expect a sensor recording. They import the
cloud (pm.create_project_from_cloud / pm.add_scan_from_cloud with
pipeline_core.import_point_cloud). The original file goes to 00_raw. The
standard point cloud is recorded as the output of Stage 1, marked as
imported, so Stage 2 (Level) is the next stage. The result is shown in a
message (number of points, size, fields, and the changes that were made).
"""
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QFileDialog, QMessageBox, QProgressDialog,
)

try:
    import project_manager as pm
except ImportError:
    pm = None

try:
    import pipeline_core as core
except ImportError:
    core = None


class _TaskBridge(QObject):
    """Carries a worker thread's result back to the GUI thread (a Qt
    signal emitted from any thread is delivered on the receiver's
    thread) - same pattern as qt_stage_base.py's _RunSignalBridge."""
    succeeded = Signal(object)
    failed = Signal(str)


def _list_names(folder):
    folder = Path(folder) if folder else None
    if folder is None or not folder.is_dir():
        return set()
    return {p.name for p in folder.iterdir()}


def _run_blocking_task(dialog, title, text, fn, on_success, watch_dir=None):
    """Runs fn() on a worker thread while a modal busy dialog shows.
    on_success(result) runs on the GUI thread. On failure, shows the
    error and names any new folder that appeared in watch_dir during
    the attempt (a partly created project or scan folder)."""
    before = _list_names(watch_dir)

    progress = QProgressDialog(text, "", 0, 0, dialog)
    progress.setCancelButton(None)
    progress.setWindowTitle(title)
    progress.setWindowModality(Qt.WindowModal)
    progress.setMinimumDuration(0)
    progress.setAutoClose(False)
    progress.setAutoReset(False)

    bridge = _TaskBridge()
    dialog._task_bridge = bridge  # keep alive until the task finishes

    def on_ok(result):
        progress.close()
        on_success(result)

    def on_fail(message):
        progress.close()
        partial = sorted(_list_names(watch_dir) - before)
        extra = ""
        if partial:
            extra = ("\n\nThis folder was partly created:\n"
                     + "\n".join(str(Path(watch_dir) / name) for name in partial)
                     + "\n\nDelete it before you try again.")
        QMessageBox.critical(dialog, title + " failed", message + extra)

    bridge.succeeded.connect(on_ok)
    bridge.failed.connect(on_fail)

    def worker():
        try:
            result = fn()
        except Exception as e:
            is_project_error = pm is not None and isinstance(e, pm.ProjectError)
            bridge.failed.emit(str(e) if is_project_error else f"{type(e).__name__}: {e}")
            return
        bridge.succeeded.emit(result)

    threading.Thread(target=worker, daemon=True).start()
    progress.show()


def _guess_source_type(path_str):
    """Maps a source file/folder to one of project_manager.py's
    VALID_SOURCE_TYPES ('pcap', 'osf', 'ros1_bag', 'ros2_bag'), auto-
    detecting from what's given. Returns None if it can't tell. Ported
    directly from pipeline_applet.py's module-level function of the
    same name.

    Update 23: a .ply file gives 'ply'. This is NOT one of VALID_SOURCE_TYPES.
    It means an import of a point cloud that was made outside the pipeline
    (project_manager.create_project_from_cloud / add_scan_from_cloud)."""
    path = Path(path_str)
    if not path.exists():
        return None
    if path.is_dir():
        if (path / "metadata.yaml").exists():
            return "ros2_bag"
        return None
    suffix = path.suffix.lower()
    if suffix == ".pcap":
        return "pcap"
    if suffix == ".osf":
        return "osf"
    if suffix == ".bag":
        return "ros1_bag"
    if suffix == ".ply":
        return "ply"
    return None


def _import_text(entry):
    """The message after an import: made from the record of the Stage 1
    entry (the stage 'slam' of the new baseline or scan)."""
    stage = entry["stages"]["slam"]
    info = {
        "n_points": stage.get("n_points"),
        "extent": stage.get("extent"),
        "median_spacing": stage.get("median_spacing"),
        "fields": stage.get("fields"),
        "notes": stage.get("import_notes", []),
    }
    if core is not None:
        return core.import_point_cloud_text(info)
    return f"Points: {info['n_points']}"


def _hint(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #777777; font-size: 8pt;")
    return label


def _source_field(parent_layout, row, label_text):
    """A dual File/Folder source-picker row, shared by NewProjectDialog
    and NewScanDialog. Returns the QLineEdit holding the picked path."""
    parent_layout.addWidget(QLabel(label_text), row, 0)
    edit = QLineEdit()
    parent_layout.addWidget(edit, row, 1)
    btn_row = QHBoxLayout()
    file_btn = QPushButton("File...")
    folder_btn = QPushButton("Folder...")
    btn_row.addWidget(file_btn)
    btn_row.addWidget(folder_btn)
    parent_layout.addLayout(btn_row, row, 2)

    def pick_file():
        path, _ = QFileDialog.getOpenFileName(None, label_text)
        if path:
            edit.setText(path)

    def pick_folder():
        path = QFileDialog.getExistingDirectory(None, label_text)
        if path:
            edit.setText(path)

    file_btn.clicked.connect(pick_file)
    folder_btn.clicked.connect(pick_folder)
    return edit


_SOURCE_ERROR = ("Expected a .pcap, .osf, or .bag file, a folder containing a "
                 "ROS2 bag (a metadata.yaml file alongside .db3 files), or a "
                 ".ply point cloud file.")


class NewProjectDialog(QDialog):
    """Asks for a folder location, a compartment name, and a raw source
    file - then creates the project (and its baseline pipeline) and
    hands it back via on_created."""

    def __init__(self, parent, on_created):
        super().__init__(parent)
        self.setWindowTitle("New Project")
        self.on_created = on_created

        form = QGridLayout(self)
        row = 0

        form.addWidget(QLabel("Project location (parent folder):"), row, 0)
        self.location_edit = QLineEdit()
        form.addWidget(self.location_edit, row, 1)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_location)
        form.addWidget(browse_btn, row, 2)
        row += 1

        form.addWidget(_hint(
            "The actual project folder is created here, named automatically from "
            "the compartment name and today's date. It will hold a baseline/ "
            "subfolder for this project's one baseline pipeline."), row, 0, 1, 3)
        row += 1

        form.addWidget(QLabel("Compartment name:"), row, 0)
        self.compartment_edit = QLineEdit()
        form.addWidget(self.compartment_edit, row, 1, 1, 2)
        row += 1

        self.source_edit = _source_field(
            form, row, "Baseline raw source (pcap / OSF / .bag / ROS2 bag folder / .ply cloud):")
        row += 1

        form.addWidget(_hint(
            "Source type (pcap/OSF/ROS1 bag/ROS2 bag) is auto-detected from what "
            "you pick above. A .ply file is imported as a point cloud that is "
            "already made: Stage 1 (SLAM) is skipped, and Stage 2 (Level) is next. "
            "The coordinates must be in metres."), row, 0, 1, 3)
        row += 1

        create_btn = QPushButton("Create Project")
        create_btn.clicked.connect(self._create)
        form.addWidget(create_btn, row, 0, 1, 3)

    def _browse_location(self):
        path = QFileDialog.getExistingDirectory(self, "Project location")
        if path:
            self.location_edit.setText(path)

    def _create(self):
        if pm is None:
            QMessageBox.critical(self, "Not available", "project_manager.py could not be imported.")
            return

        location = self.location_edit.text().strip()
        compartment = self.compartment_edit.text().strip()
        source = self.source_edit.text().strip()

        if not location or not compartment or not source:
            QMessageBox.critical(self, "Missing input", "Fill in all three fields.")
            return

        source_type = _guess_source_type(source)
        if source_type is None:
            QMessageBox.critical(
                self, "Can't determine source type",
                f"Couldn't tell what kind of source this is from:\n{source}\n\n"
                + _SOURCE_ERROR)
            return

        def done(project):
            if source_type == "ply":
                QMessageBox.information(
                    self, "Point cloud imported",
                    "The point cloud is the output of Stage 1. The next stage is "
                    "Stage 2 (Level).\n\n"
                    + _import_text(project.data["baseline"]))
            self.accept()
            self.on_created(project)

        if source_type == "ply":
            if core is None:
                QMessageBox.critical(self, "Not available", "pipeline_core.py could not be imported.")
                return
            _run_blocking_task(
                self, "Import point cloud",
                "Creating the project and importing the point cloud.\n"
                "A large file can take some minutes.",
                lambda: pm.create_project_from_cloud(
                    location, compartment, source, core.import_point_cloud),
                done, watch_dir=location)
            return

        _run_blocking_task(
            self, "Create project",
            "Creating the project and copying the raw source.\n"
            "A large source can take some minutes.",
            lambda: pm.create_project(location, compartment, source, source_type),
            done, watch_dir=location)


class NewScanDialog(QDialog):
    """Asks for a label and a raw source file, then adds a new
    comparison scan to the currently open project and hands the new
    scan_id back via on_created."""

    def __init__(self, parent, project, on_created):
        super().__init__(parent)
        self.setWindowTitle("New Scan")
        self.project = project
        self.on_created = on_created

        form = QGridLayout(self)
        row = 0

        title = QLabel(f"Project: {project.data.get('compartment', '?')}")
        title.setStyleSheet("font-weight: bold;")
        form.addWidget(title, row, 0, 1, 3)
        row += 1

        form.addWidget(QLabel("Label (e.g. 'post-storm', 'routine-check'):"), row, 0)
        self.label_edit = QLineEdit()
        form.addWidget(self.label_edit, row, 1, 1, 2)
        row += 1

        form.addWidget(_hint(
            "Today's date is added automatically, matching the format already "
            "used for scan/diff IDs (e.g. 'post-storm_2026-09-01')."), row, 0, 1, 3)
        row += 1

        self.source_edit = _source_field(
            form, row, "Raw source (pcap / OSF / .bag / ROS2 bag folder / .ply cloud):")
        row += 1

        form.addWidget(_hint(
            "A .ply file is imported as a point cloud that is already made: "
            "Stage 1 (SLAM) is skipped, and Stage 2 (Level) is next. "
            "The coordinates must be in metres."), row, 0, 1, 3)
        row += 1

        add_btn = QPushButton("Add Scan")
        add_btn.clicked.connect(self._create)
        form.addWidget(add_btn, row, 0, 1, 3)

    def _create(self):
        label = self.label_edit.text().strip()
        source = self.source_edit.text().strip()
        if not label or not source:
            QMessageBox.critical(self, "Missing input", "Fill in both fields.")
            return

        source_type = _guess_source_type(source)
        if source_type is None:
            QMessageBox.critical(
                self, "Can't determine source type",
                f"Couldn't tell what kind of source this is from:\n{source}\n\n"
                + _SOURCE_ERROR)
            return

        def done(scan_id):
            if source_type == "ply":
                QMessageBox.information(
                    self, "Point cloud imported",
                    "The point cloud is the output of Stage 1. The next stage is "
                    "Stage 2 (Level).\n\n"
                    + _import_text(self.project.data["scans"][scan_id]))
            self.accept()
            self.on_created(scan_id)

        if source_type == "ply":
            if core is None:
                QMessageBox.critical(self, "Not available", "pipeline_core.py could not be imported.")
                return
            _run_blocking_task(
                self, "Import point cloud",
                "Adding the scan and importing the point cloud.\n"
                "A large file can take some minutes.",
                lambda: pm.add_scan_from_cloud(
                    self.project, label, source, core.import_point_cloud),
                done, watch_dir=Path(self.project.root) / "scans")
            return

        _run_blocking_task(
            self, "Add scan",
            "Adding the scan and copying the raw source.\n"
            "A large source can take some minutes.",
            lambda: pm.add_scan(self.project, label, source, source_type),
            done, watch_dir=Path(self.project.root) / "scans")


class NewDiffDialog(QDialog):
    """Asks for a label, a reference (the project's baseline or an
    existing scan), and a comparison (an existing scan), then adds a
    new diff pipeline and hands the new diff_id back via on_created."""

    def __init__(self, parent, project, on_created):
        super().__init__(parent)
        self.setWindowTitle("New Diff")
        self.project = project
        self.on_created = on_created

        form = QGridLayout(self)
        row = 0

        title = QLabel(f"Project: {project.data.get('compartment', '?')}")
        title.setStyleSheet("font-weight: bold;")
        form.addWidget(title, row, 0, 1, 3)
        row += 1

        scan_ids = pm.list_scans(project) if pm is not None else []
        if not scan_ids:
            warning = QLabel(
                "This project has no scans yet - add one with 'New Scan' before "
                "creating a diff (a diff always needs at least one scan as its "
                "comparison side).")
            warning.setWordWrap(True)
            warning.setStyleSheet("color: #bb0000;")
            form.addWidget(warning, row, 0, 1, 3)
            row += 1

        form.addWidget(QLabel("Label (e.g. 'post-storm_vs_baseline'):"), row, 0)
        self.label_edit = QLineEdit()
        form.addWidget(self.label_edit, row, 1, 1, 2)
        row += 1

        form.addWidget(QLabel("Reference (baseline side):"), row, 0)
        self.reference_combo = QComboBox()
        self.reference_combo.addItems(["baseline"] + scan_ids)
        form.addWidget(self.reference_combo, row, 1, 1, 2)
        row += 1

        form.addWidget(_hint(
            "Either the project's own baseline, or an earlier scan - to show what "
            "changed only since that scan, rather than since the baseline."),
            row, 0, 1, 3)
        row += 1

        form.addWidget(QLabel("Comparison (scan being checked):"), row, 0)
        self.comparison_combo = QComboBox()
        self.comparison_combo.addItems(scan_ids)
        form.addWidget(self.comparison_combo, row, 1, 1, 2)
        row += 1

        add_btn = QPushButton("Add Diff")
        add_btn.clicked.connect(self._create)
        form.addWidget(add_btn, row, 0, 1, 3)

    def _create(self):
        if pm is None:
            QMessageBox.critical(self, "Not available", "project_manager.py could not be imported.")
            return

        label = self.label_edit.text().strip()
        reference = self.reference_combo.currentText().strip()
        comparison = self.comparison_combo.currentText().strip()
        if not label or not reference or not comparison:
            QMessageBox.critical(self, "Missing input", "Fill in all three fields.")
            return

        try:
            diff_id = pm.add_diff(self.project, label, reference, comparison)
        except pm.ProjectError as e:
            QMessageBox.critical(self, "Could not add diff", str(e))
            return

        self.accept()
        self.on_created(diff_id)
