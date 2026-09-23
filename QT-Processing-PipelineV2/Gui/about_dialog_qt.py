#!/usr/bin/env python3
"""
About window
===============
Qt port of pipeline_applet.py's AboutDialog. Loads stage-by-stage
reference content (Purpose/Inputs/Outputs/What to expect/Next steps)
from about_content.json, which sits next to this file - edit that JSON
to change what's shown, no code change needed, same as the Tkinter
version.

Deliberately non-modal, per chat: .show() rather than .exec(), and
Qt.WindowType.Window (an independent top-level window, not tied to the
main window's own stacking/taskbar behavior) so it can be freely
positioned and read alongside whatever stage panel is currently
showing. This isn't a departure from the Tkinter version either - its
AboutDialog is a plain tk.Toplevel with no grab_set() call, which is
already non-modal there too.

pipeline_applet_qt_template.py owns creating this and MUST keep a
persistent reference to the instance (not just call
AboutDialog(self).show() inline) - a non-modal QDialog with no other
Python reference gets garbage-collected the moment the creating call
returns, which would make the window vanish immediately after opening.
"""
import json
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QPushButton, QTextBrowser

ABOUT_CONTENT_FILE = Path(__file__).resolve().parent / "about_content.json"


def _load_about_content():
    """Loads stage descriptions from about_content.json, which sits
    next to this file (gui/, alongside pipeline_applet_qt_template.py
    and pipeline_applet.py). Ported directly from pipeline_applet.py's
    own load_about_content()."""
    if not ABOUT_CONTENT_FILE.exists():
        return {"About": {"Notice": f"about_content.json not found in "
                                     f"{ABOUT_CONTENT_FILE.parent} - nothing to show."}}
    try:
        with open(ABOUT_CONTENT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return {"About": {"Error": f"Could not read about_content.json: {e}"}}


def _build_about_html(content):
    """Turns the {"Stage title": {"Field label": "Field text"}} content
    into HTML for QTextBrowser.setHtml() - heading/subheading/body
    styling matches the Tkinter version's text tag configuration
    (heading: bold white; subheading: bold light blue; body: plain
    light grey, all on a dark background)."""
    parts = ['<div style="font-family: \'Segoe UI\', sans-serif;">']
    for stage_title, fields in content.items():
        parts.append(
            f'<h2 style="color:#ffffff; margin-top:18px; margin-bottom:4px;">'
            f'{escape(stage_title)}</h2>')
        for label, text in fields.items():
            parts.append(
                f'<h3 style="color:#99ccff; margin-top:8px; margin-bottom:2px;">'
                f'{escape(label)}</h3>')
            paragraph = escape(text).replace("\n", "<br>")
            parts.append(
                f'<p style="color:#dddddd; margin-top:0; margin-bottom:4px;">'
                f'{paragraph}</p>')
    parts.append("</div>")
    return "".join(parts)


class AboutDialog(QDialog):
    """Non-modal 'About: Pipeline Stages' window. See module docstring
    for why it's non-modal and why the caller must hold a reference."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About: Pipeline Stages")
        self.resize(560, 520)
        self.setWindowFlag(Qt.WindowType.Window, True)

        layout = QVBoxLayout(self)

        self.text_view = QTextBrowser()
        self.text_view.setOpenExternalLinks(False)
        self.text_view.setStyleSheet(
            "QTextBrowser { background-color: #111111; padding: 12px; border: none; }")
        self.text_view.setHtml(_build_about_html(_load_about_content()))
        layout.addWidget(self.text_view, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignHCenter)
