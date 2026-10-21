#!/usr/bin/env python3
"""Qt application for the eight-stage processing pipeline."""

import importlib
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QLabel,
    QMessageBox,
    QFileDialog,
)

GUI_DIR = Path(__file__).resolve().parent
if str(GUI_DIR) not in sys.path:
    sys.path.insert(0, str(GUI_DIR))

from ui_main import Ui_PipelineAppletWindow

try:
    import project_manager as pm
except ImportError:
    pm = None

from project_workflow_qt import NewProjectDialog, NewScanDialog, NewDiffDialog
from qt_stage_base import pipeline_label
from about_dialog_qt import AboutDialog

# stage display name -> full traceback, for every stage panel whose
# module failed to import. Catches Exception, not only ImportError: a
# teammate's machine with a missing package (or a bug in one stage
# file) should cost that one stage, not the whole app - and the
# placeholder page plus the Log tab say WHY, instead of the old
# misleading "panel is not built yet" text.
PANEL_IMPORT_ERRORS = {}


def _load_panel_class(stage_name, module_name, class_name):
    try:
        return getattr(importlib.import_module(module_name), class_name)
    except Exception:
        PANEL_IMPORT_ERRORS[stage_name] = traceback.format_exc()
        return None


# (objectName in the .ui file, display name used for stubs/log lines)
STAGE_BUTTONS = [
    ("stage1Button", "Stage 1: SLAM"),
    ("stage2Button", "Stage 2: Level"),
    ("stage3Button", "Stage 3: Cleanup"),
    ("stage4Button", "Stage 4: Segment"),
    ("stage5Button", "Stage 5: Diff"),
    ("stage6Button", "Stage 6: Classify"),
    ("stage7Button", "Stage 7: Surface"),
    ("stage8Button", "Stage 8: Export"),
]

# (display name, module, class). _build_stage_page() shows an error
# placeholder for any stage whose class failed to load.
STAGE_PANEL_SPECS = [
    ("Stage 1: SLAM", "stage1_slam_dialog_qt", "Stage1SlamPanel"),
    ("Stage 2: Level", "stage2_level_dialog_qt", "Stage2LevelPanel"),
    ("Stage 3: Cleanup", "stage3_cleanup_dialog_qt", "Stage3CleanupPanel"),
    ("Stage 4: Segment", "stage4_segment_dialog_qt", "Stage4SegmentPanel"),
    ("Stage 5: Diff", "stage5_diff_dialog_qt", "Stage5DiffPanel"),
    ("Stage 6: Classify", "stage6_classify_dialog_qt", "Stage6ClassifyPanel"),
    ("Stage 7: Surface", "stage7_surface_dialog_qt", "Stage7SurfacePanel"),
    ("Stage 8: Export", "stage8_export_dialog_qt", "Stage8ExportPanel"),
]
STAGE_PANEL_CLASSES = {
    name: _load_panel_class(name, module, cls)
    for name, module, cls in STAGE_PANEL_SPECS
}

OPTIONAL_STAGES = {"Stage 4: Segment"}
OPTIONAL_STAGE_KEYS = {"segment"}


def stage_title(stage_name):
    return f"{stage_name} (optional)" if stage_name in OPTIONAL_STAGES else stage_name


def next_stage_text(next_stage):
    if next_stage is None:
        return "done"
    return (
        f"{next_stage} (optional)" if next_stage in OPTIONAL_STAGE_KEYS else next_stage
    )


# Stages 1-4 act on the "source" pipeline (baseline or a scan);
# Stages 5-8 act on the "diff" pipeline - matches project_manager.py's
# own BASELINE_SCAN_STAGE_NAMES / DIFF_STAGE_NAMES split exactly.
SOURCE_BOUND_STAGES = {
    "Stage 1: SLAM",
    "Stage 2: Level",
    "Stage 3: Cleanup",
    "Stage 4: Segment",
}
DIFF_BOUND_STAGES = {
    "Stage 5: Diff",
    "Stage 6: Classify",
    "Stage 7: Surface",
    "Stage 8: Export",
}

# status -> (label text template, color). {stage} is filled in by
# _set_run_status(); {detail} only appears for "error".
RUN_STATUS_STYLES = {
    "idle": ("Idle", "#666666"),
    "running": ("Running: {stage}...", "#b8860b"),
    "done": ("Done: {stage}", "#009955"),
    "error": ("Error in {stage}{detail}", "#cc3333"),
}

RUN_WAIT_TOOLTIP = "Wait for the current process to finish."


def _placeholder_page(text):
    page = QWidget()
    layout = QVBoxLayout(page)
    label = QLabel(text)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)
    layout.addWidget(label)
    return page


class PipelineAppletWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.ui = Ui_PipelineAppletWindow()
        self.ui.setupUi(self)

        # stage display name -> the page widget already added to
        # stageStack, so each stage's panel is only built once and
        # reused on repeat clicks instead of being rebuilt from scratch
        # every time its button is pressed. This is also what keeps a
        # stage's field values alive across switches - see module
        # docstring.
        self._stage_pages = {}
        self._is_running = False
        self._running_stage = None
        self._about_dialog = None

        self.active_project = None
        self.active_source_pipeline = None
        self.active_diff_pipeline = None

        self._wire_top_row()
        self._wire_project_controls()
        self._wire_stage_buttons()
        self._log_panel_import_errors()

    def _log_panel_import_errors(self):
        for stage_name, tb in PANEL_IMPORT_ERRORS.items():
            self.ui.appLogConsole.appendPlainText(
                f"ERROR: the {stage_name} panel could not load:\n{tb}"
            )

    # -- top row: About / New Project / Open Project -------------------------

    def _wire_top_row(self):
        self.ui.AboutButton.clicked.connect(self._show_about)
        self.ui.newProjectButton.clicked.connect(self._new_project)
        self.ui.openProjectButton.clicked.connect(self._open_project)

    def _show_about(self):
        """Keeps a persistent reference on self - see about_dialog_qt.py's module docstring for why that's required, not optional, for a non-modal QDialog."""
        if self._about_dialog is None:
            self._about_dialog = AboutDialog(self)
        self._about_dialog.show()
        self._about_dialog.raise_()
        self._about_dialog.activateWindow()

    def _new_project(self):
        if pm is None:
            QMessageBox.critical(
                self, "Not available", "project_manager.py could not be imported."
            )
            return
        dlg = NewProjectDialog(self, on_created=self._set_active_project)
        dlg.exec()

    def _open_project(self):
        if pm is None:
            QMessageBox.critical(
                self, "Not available", "project_manager.py could not be imported."
            )
            return
        folder = QFileDialog.getExistingDirectory(self, "Select project folder")
        if not folder:
            return
        try:
            project = pm.load_project(folder)
        except pm.ProjectError as e:
            QMessageBox.critical(self, "Could not open project", str(e))
            return
        self._set_active_project(project)

    # -- pipeline selectors: New Scan / New Diff / both combos --------------

    def _wire_project_controls(self):
        self.ui.newScanButton.clicked.connect(self._new_scan)
        self.ui.newDiffButton.clicked.connect(self._new_diff)
        self.ui.sourceCombo.currentTextChanged.connect(
            self._on_source_pipeline_selected
        )
        self.ui.diffCombo.currentTextChanged.connect(self._on_diff_pipeline_selected)

    def _new_scan(self):
        if self.active_project is None:
            QMessageBox.critical(
                self, "No active project", "Open or create a project first."
            )
            return
        dlg = NewScanDialog(self, self.active_project, on_created=self._on_scan_added)
        dlg.exec()

    def _new_diff(self):
        if self.active_project is None:
            QMessageBox.critical(
                self, "No active project", "Open or create a project first."
            )
            return
        dlg = NewDiffDialog(self, self.active_project, on_created=self._on_diff_added)
        dlg.exec()

    def _set_active_project(self, project):
        self.active_project = project
        self.active_source_pipeline = project.baseline_handle()
        self.active_diff_pipeline = None
        self.ui.appLogConsole.appendPlainText(
            f"Project opened: {project.data.get('compartment', '?')}"
        )
        self._refresh_pipeline_selectors()

    def _on_scan_added(self, scan_id):
        self.active_source_pipeline = self.active_project.scan_handle(scan_id)
        self.ui.appLogConsole.appendPlainText(f"Scan added: {scan_id}")
        self._refresh_pipeline_selectors()

    def _on_diff_added(self, diff_id):
        self.active_diff_pipeline = self.active_project.diff_handle(diff_id)
        self.ui.appLogConsole.appendPlainText(f"Diff added: {diff_id}")
        self._refresh_pipeline_selectors()

    def _refresh_pipeline_selectors(self):
        """Repopulates both combos from the active project and re-syncs active_source_pipeline / active_diff_pipeline from whatever ends up selected."""
        project = self.active_project

        self.ui.sourceCombo.blockSignals(True)
        self.ui.diffCombo.blockSignals(True)
        self.ui.sourceCombo.clear()
        self.ui.diffCombo.clear()

        if project is not None:
            source_values = ["Baseline"] + [
                f"Scan: {sid}" for sid in pm.list_scans(project)
            ]
            self.ui.sourceCombo.addItems(source_values)
            current_source_label = pipeline_label(self.active_source_pipeline)
            self.ui.sourceCombo.setCurrentText(
                current_source_label
                if current_source_label in source_values
                else source_values[0]
            )

            diff_values = [f"Diff: {did}" for did in pm.list_diffs(project)]
            self.ui.diffCombo.addItems(diff_values)
            if diff_values:
                current_diff_label = pipeline_label(self.active_diff_pipeline)
                self.ui.diffCombo.setCurrentText(
                    current_diff_label
                    if current_diff_label in diff_values
                    else diff_values[0]
                )
            else:
                self.active_diff_pipeline = None

        self.ui.sourceCombo.blockSignals(False)
        self.ui.diffCombo.blockSignals(False)

        self._resolve_source_pipeline_from_combo()
        self._resolve_diff_pipeline_from_combo()
        self._refresh_cached_panels(SOURCE_BOUND_STAGES, self.active_source_pipeline)
        self._refresh_cached_panels(DIFF_BOUND_STAGES, self.active_diff_pipeline)
        self._refresh_project_status()

    def _on_source_pipeline_selected(self, _text):
        self._resolve_source_pipeline_from_combo()
        self._refresh_cached_panels(SOURCE_BOUND_STAGES, self.active_source_pipeline)
        self._refresh_project_status()

    def _resolve_source_pipeline_from_combo(self):
        if self.active_project is None:
            return
        value = self.ui.sourceCombo.currentText()
        if value == "Baseline":
            self.active_source_pipeline = self.active_project.baseline_handle()
        elif value.startswith("Scan: "):
            self.active_source_pipeline = self.active_project.scan_handle(
                value[len("Scan: ") :]
            )

    def _on_diff_pipeline_selected(self, _text):
        self._resolve_diff_pipeline_from_combo()
        self._refresh_cached_panels(DIFF_BOUND_STAGES, self.active_diff_pipeline)
        self._refresh_project_status()

    def _resolve_diff_pipeline_from_combo(self):
        if self.active_project is None:
            return
        value = self.ui.diffCombo.currentText()
        if value.startswith("Diff: "):
            self.active_diff_pipeline = self.active_project.diff_handle(
                value[len("Diff: ") :]
            )

    def _refresh_cached_panels(self, stage_names, pipeline):
        for stage_name in stage_names:
            page = self._stage_pages.get(stage_name)
            if page is not None and hasattr(page, "refresh_project_pipeline"):
                page.refresh_project_pipeline(pipeline)

    def _refresh_project_status(self):
        """Mirrors the Qt processing app's PipelineApp.refresh_project_status()."""
        if self.active_project is None:
            self.ui.statusLabel.setText("No active project")
            return
        compartment = self.active_project.data.get("compartment", "?")
        parts = [f"Project: {compartment}"]

        if self.active_source_pipeline is not None:
            next_stage = pm.find_next_stage(self.active_source_pipeline)
            parts.append(
                f"{pipeline_label(self.active_source_pipeline)} "
                f"next: {next_stage_text(next_stage)}"
            )
            raw = self.active_source_pipeline.entry.get("raw") or {}
            if raw.get("decoded_path"):
                parts.append("decoded source set")
        if self.active_diff_pipeline is not None:
            next_stage = pm.find_next_stage(self.active_diff_pipeline)
            parts.append(
                f"{pipeline_label(self.active_diff_pipeline)} "
                f"next: {next_stage_text(next_stage)}"
            )

        self.ui.statusLabel.setText("   |   ".join(parts))

    # -- stage buttons / stageStack -------------------------------------------

    def _wire_stage_buttons(self):
        for button_name, stage_name in STAGE_BUTTONS:
            button = getattr(self.ui, button_name)
            # name=stage_name binds the current loop value as a default
            # argument, so every button opens ITS OWN stage - without
            # this, all eight buttons would open Stage 8 (the loop
            # variable's final value), a classic Python closure trap.
            button.clicked.connect(
                lambda _checked, name=stage_name: self._show_stage(name)
            )

    def _show_stage(self, stage_name):
        if stage_name not in self._stage_pages:
            page = self._build_stage_page(stage_name)
            self.ui.stageStack.addWidget(page)
            self._stage_pages[stage_name] = page
        page = self._stage_pages[stage_name]
        if hasattr(page, "refresh_registered_baselines"):
            page.refresh_registered_baselines()
        # Auto-filled fields (output paths, Stage 8's damage-sites file)
        # follow what earlier stages have recorded since this page was
        # built. A value the user typed is never replaced - see
        # qt_stage_base.register_auto_default().
        if (
            hasattr(page, "_refresh_auto_defaults")
            and getattr(page, "_active_run", None) is None
        ):
            page._refresh_auto_defaults()
        self.ui.stageStack.setCurrentWidget(page)
        self.ui.stageTitleLabel.setText(stage_title(stage_name))
        self.ui.appLogConsole.appendPlainText(f"Switched to {stage_name}")

    def _pipeline_for_stage(self, stage_name):
        if stage_name in SOURCE_BOUND_STAGES:
            return self.active_source_pipeline
        if stage_name in DIFF_BOUND_STAGES:
            return self.active_diff_pipeline
        return None

    def _build_stage_page(self, stage_name):
        panel_cls = STAGE_PANEL_CLASSES.get(stage_name)
        if panel_cls is not None:
            page = panel_cls(
                pipeline=self._pipeline_for_stage(stage_name),
                on_output=self._append_terminal_line,
                on_status=lambda status, detail="", name=stage_name: self._set_run_status(
                    name, status, detail
                ),
            )
        elif stage_name in PANEL_IMPORT_ERRORS:
            error_line = PANEL_IMPORT_ERRORS[stage_name].strip().splitlines()[-1]
            page = _placeholder_page(
                f"{stage_name} could not load.\n\n{error_line}\n\n"
                "The Log tab shows the full error."
            )
        else:
            page = _placeholder_page(f"{stage_name} panel is not available.")

        # A run could already be in progress on another stage's panel
        # by the time this one gets built (e.g. clicking Stage 2 while
        # Stage 1 is running) - match the current lock state right away
        # instead of waiting for the next status change to catch it.
        self._lock_page(page)
        return page

    def _lock_page(self, page):
        if hasattr(page, "set_run_locked"):
            page.set_run_locked(self._is_running, RUN_WAIT_TOOLTIP)

    def _apply_running_lock(self):
        for page in self._stage_pages.values():
            self._lock_page(page)

    def _append_terminal_line(self, text):
        """Passed into each stage panel as on_output - real subprocess
        stdout/stderr lands here."""
        self.ui.terminalConsole.appendPlainText(text)

    def _set_run_status(self, stage_name, status, detail=""):
        """Passed into each stage panel as on_status. Always names
        WHICH stage the status belongs to. Also the single point
        deciding whether ANY stage is currently running, for
        _apply_running_lock()."""
        template, color = RUN_STATUS_STYLES.get(status, (status, "#666666"))
        text = template.format(
            stage=stage_name, detail=f" - {detail}" if detail else ""
        )
        self.ui.runStatusLabel.setText(text)
        self.ui.runStatusLabel.setStyleSheet(
            f"color: {color}; font-weight: bold; padding: 4px;"
        )

        self._is_running = status == "running"
        self._running_stage = stage_name if self._is_running else None
        self._apply_running_lock()

    def closeEvent(self, event):
        """Asks before closing while a stage runs. On yes, stops the
        process tree and records the stage as failed. Before this, the
        window closed at once: the child process kept running as an
        orphan, and project.json kept the stage as "running"."""
        if not self._is_running:
            event.accept()
            return
        answer = QMessageBox.question(
            self,
            "A process is running",
            f"{self._running_stage} is running.\n\n"
            "If you close the app, the process stops and the stage is recorded as "
            "failed.\n\nClose the app?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            event.ignore()
            return
        page = self._stage_pages.get(self._running_stage)
        if page is not None and hasattr(page, "abort_for_shutdown"):
            page.abort_for_shutdown()
        event.accept()

    def _stub(self, action_name):
        self.ui.appLogConsole.appendPlainText(f"[stub] {action_name} clicked")
        QMessageBox.information(
            self,
            "Template shell",
            f"'{action_name}' is not wired up yet in this template.",
        )


def main():
    app = QApplication(sys.argv)
    window = PipelineAppletWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
